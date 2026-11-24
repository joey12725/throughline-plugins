#!/usr/bin/env python3
"""Standard-library fetcher for public Instagram account feeds (posts and stories).

Reads public accounts via the web profile API
(``/api/v1/users/web_profile_info/?username=<handle>``) or public web profile/post
HTML (``/<handle>/`` + ``/p/<code>/``), and optionally active 24h stories
(``/api/v1/feed/reels_media/?reel_ids=<user_id>`` when authenticated via
``INSTAGRAM_FEED_COOKIES_FILE``), writing raw records as JSONL, one JSON object
per line with ``kind``, a stable native ``id`` and the raw payload::

    python fetch.py export --out feed.jsonl [--accounts accounts.json]

Only the Python standard library is used (urllib, json, http.cookiejar,
html.parser), so this one file can be copied to any host with Python 3.12+ and
run there. The Throughline plugin imports the same module to read live.

Credentials: the optional cookies file is named only by the environment
variable INSTAGRAM_FEED_COOKIES_FILE. Cookie values are never printed,
logged or written to the export. Everything else (accounts, limits, state)
may come from options or INSTAGRAM_FEED_* environment variables.

Politeness: a small batch of accounts per run (least recently fetched
first), a per-run deadline, a delay between requests, a per-day request
limit kept in the state file, bounded retries with backoff on 5xx and
transport errors, and a growing cool-down after 401/429 or a login wall
during which no request is made at all. No media is downloaded.
"""

import argparse
import email.utils
import hashlib
import http.client
import http.cookiejar
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

FETCHER_VERSION = 2
DEFAULT_BASE_URL = "https://www.instagram.com"
# The public web client's application id; the endpoint requires it. Not a secret.
DEFAULT_APP_ID = "936619743392459"
HTML_404_BLOCK = 3
KNOWN_POSTS_LIMIT = 200
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
)
HANDLE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
ACCESSIBILITY_DATE = re.compile(
    r"\bon\s+([A-Z][a-z]+\s+\d{1,2},\s+\d{4})\b"
)
ENV_PREFIX = "INSTAGRAM_FEED_"
COOKIES_ENV = ENV_PREFIX + "COOKIES_FILE"

# name: (default, description); env name is INSTAGRAM_FEED_<NAME upper>.
OPTIONS = {
    "accounts": (None, "Comma separated public handles (or use accounts_file)"),
    "accounts_file": (
        None,
        "Accounts file: JSON list of handles or {handle,label,category} objects, "
        'a JSON object with an "accounts" list, or text lines "handle [label]"',
    ),
    "fetch_mode": (
        "api",
        "Profile fetch mode: api (web_profile_info), html (public profile/post HTML), "
        "or auto (api with cookies, html when anonymous)",
    ),
    "stories": (
        "1",
        "1 fetches active 24h stories via /api/v1/feed/reels_media/ when authenticated "
        "(cookies_file); 0 disables",
    ),
    "batch": (
        "12",
        "Accounts fetched per run (rotation, least recently fetched first)",
    ),
    "per_day": (
        "200",
        "HTTP requests allowed per UTC day across runs, retries included",
    ),
    "posts": (
        "12",
        "Newest posts kept per account fetch (the endpoint returns up to 12)",
    ),
    "delay_s": ("2.0", "Seconds between requests (plus up to 1 s jitter)"),
    "deadline_s": ("90", "Stop starting new accounts after this many seconds"),
    "retries": ("3", "Attempts per request on 5xx or transport errors"),
    "cooldown_s": (
        "900",
        "First cool-down after 401/429/login wall; doubles per strike",
    ),
    "max_cooldown_s": ("86400", "Longest cool-down"),
    "timeout_s": ("20", "Socket timeout per request"),
    "prime": (
        "1",
        "1 fetches the home page once per run for anonymous cookies (skipped with a cookies file)",
    ),
    "user_agent": (DEFAULT_USER_AGENT, "User-Agent header"),
    "app_id": (DEFAULT_APP_ID, "X-IG-App-ID header of the public web client"),
    "base_url": (DEFAULT_BASE_URL, "Base URL; tests point it at a local fake server"),
}


class FetchError(Exception):
    """A request failed in a way the caller should see (never contains secrets)."""


class Blocked(FetchError):
    """401, 429, a login wall or a "please wait" answer: stop and cool down."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class LimitReached(FetchError):
    """The per-day request budget is spent."""


def env_options(environ=None):
    environ = os.environ if environ is None else environ
    return {
        name: environ.get(ENV_PREFIX + name.upper(), default)
        for name, (default, _) in OPTIONS.items()
    }


def utc_now():
    return datetime.now(timezone.utc)


def iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value):
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


# ---------------------------------------------------------------- accounts


def _account(entry):
    if isinstance(entry, str):
        entry = {"handle": entry}
    if not isinstance(entry, dict):
        raise ValueError("account entries must be handles or objects")
    handle = str(entry.get("handle") or entry.get("username") or "").strip().lstrip("@")
    if not HANDLE.match(handle):
        raise ValueError(f"invalid Instagram handle: {handle!r}")
    account = {"handle": handle.lower()}
    for key in ("label", "category"):
        if entry.get(key) not in (None, ""):
            account[key] = str(entry[key])
    return account


def parse_accounts(text=None, path=None):
    """Accounts from a comma list and/or a file; order kept, duplicates merged."""
    entries = []
    if text:
        entries.extend(part for part in re.split(r"[,\s]+", text) if part.strip())
    if path:
        with open(path, encoding="utf-8-sig") as handle:
            body = handle.read()
        stripped = body.strip()
        if stripped.startswith("[") or stripped.startswith("{"):
            data = json.loads(stripped)
            if isinstance(data, dict):
                data = data.get("accounts", [])
            if not isinstance(data, list):
                raise ValueError("accounts file must hold a list of accounts")
            entries.extend(data)
        else:
            for line in body.splitlines():
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                handle, _, label = line.partition(" ")
                entries.append({"handle": handle, "label": label.strip() or None})
    accounts, seen = [], {}
    for entry in entries:
        account = _account(entry)
        if account["handle"] in seen:
            seen[account["handle"]].update(
                {k: v for k, v in account.items() if k != "handle"}
            )
            continue
        seen[account["handle"]] = account
        accounts.append(account)
    return accounts


# ---------------------------------------------------------------- cookies


def load_cookies(path, jar):
    """Netscape cookies.txt or a JSON list of {name,value,domain,path} objects."""
    with open(path, encoding="utf-8-sig") as handle:
        body = handle.read()
    if body.lstrip().startswith("["):
        for item in json.loads(body):
            jar.set_cookie(
                http.cookiejar.Cookie(
                    0,
                    str(item["name"]),
                    str(item["value"]),
                    None,
                    False,
                    str(item.get("domain") or ".instagram.com"),
                    True,
                    str(item.get("domain") or ".instagram.com").startswith("."),
                    str(item.get("path") or "/"),
                    True,
                    bool(item.get("secure", True)),
                    item.get("expirationDate") and int(item["expirationDate"]),
                    False,
                    None,
                    None,
                    {},
                )
            )
        return
    mozilla = http.cookiejar.MozillaCookieJar()
    try:
        mozilla.load(path, ignore_discard=True, ignore_expires=True)
    except (http.cookiejar.LoadError, OSError):
        raise FetchError(
            "cookies file is not a readable cookies.txt or JSON list"
        ) from None
    for cookie in mozilla:
        jar.set_cookie(cookie)


class Transport:
    """urllib with a cookie jar; returns (status, headers, body, final_url)."""

    def __init__(self, cookies_file=None, timeout=20, base_url=DEFAULT_BASE_URL):
        self.jar = http.cookiejar.CookieJar()
        self.has_cookies = False
        if cookies_file:
            load_cookies(cookies_file, self.jar)
            self.has_cookies = True
        host = urllib.parse.urlsplit(base_url).hostname or ""
        handlers = [urllib.request.HTTPCookieProcessor(self.jar)]
        if host in ("127.0.0.1", "localhost", "::1"):
            handlers.append(urllib.request.ProxyHandler({}))
        self.opener = urllib.request.build_opener(*handlers)
        self.timeout = timeout

    def csrf(self):
        for cookie in self.jar:
            if cookie.name == "csrftoken":
                return cookie.value
        return None

    def __call__(self, url, headers):
        token = self.csrf()
        if token and headers.get("X-Requested-With") == "XMLHttpRequest":
            headers = dict(headers, **{"X-CSRFToken": token})
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return (
                    response.status,
                    dict(response.headers),
                    response.read(),
                    response.geturl(),
                )
        except urllib.error.HTTPError as error:
            body = error.read() if error.fp else b""
            return error.code, dict(error.headers or {}), body, url


# ---------------------------------------------------------------- HTML helpers


class _JsonScripts(HTMLParser):
    """Collect text of <script type="application/json"> tags."""

    def __init__(self):
        super().__init__()
        self._active = False
        self._buf = []
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            for k, v in attrs:
                if k.lower() == "type" and (v or "").lower() == "application/json":
                    self._active = True
                    self._buf = []
                    return

    def handle_data(self, data):
        if self._active:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._active:
            self._active = False
            self.scripts.append("".join(self._buf))


def _find_key(obj, key):
    out = []
    if isinstance(obj, dict):
        if key in obj:
            out.append(obj[key])
        for value in obj.values():
            out.extend(_find_key(value, key))
    elif isinstance(obj, list):
        for value in obj:
            out.extend(_find_key(value, key))
    return out


def _parse_accessibility_date(text):
    if not isinstance(text, str) or not text:
        return None
    match = ACCESSIBILITY_DATE.search(text)
    if not match:
        return None
    try:
        parsed = datetime.strptime(match.group(1), "%B %d, %Y").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None
    return int(parsed.timestamp())


def _grid_caption_sha(node):
    caption = node.get("caption")
    if isinstance(caption, dict):
        text = caption.get("text") or ""
    elif isinstance(caption, str):
        text = caption
    else:
        edges = ((node.get("edge_media_to_caption") or {}).get("edges")) or []
        text = (((edges[0] or {}).get("node") or {}).get("text") or "") if edges else ""
    return hashlib.sha256(str(text).strip().encode("utf-8")).hexdigest()[:16]


def _parse_profile_html(document, handle):
    """Parse Instagram's logged-out SSR Relay profile HTML into a profile dict."""
    parser = _JsonScripts()
    parser.feed(document)
    target = handle.lower()
    profiles = []
    for script in parser.scripts:
        if "xig_user_by_username" not in script:
            continue
        try:
            payload = json.loads(script)
        except ValueError:
            continue
        profiles.extend(
            p for p in _find_key(payload, "xig_user_by_username") if isinstance(p, dict)
        )
    identity = None
    for profile in profiles:
        if str(profile.get("username") or "").lower() == target:
            identity = profile
            break
    if identity is None:
        return None
    uid = str(identity.get("pk") or identity.get("id") or "").strip() or None
    edges = []
    for profile in profiles:
        profile_user = str(profile.get("username") or "").lower()
        profile_pk = str(profile.get("pk") or profile.get("id") or "").strip()
        if profile_user not in ("", target):
            continue
        if uid and profile_pk and profile_pk != uid:
            continue
        timeline = profile.get("polaris_ordered_timeline_connection")
        if isinstance(timeline, dict) and isinstance(timeline.get("edges"), list):
            edges = [
                e for e in timeline["edges"] if isinstance(e, dict) and isinstance(e.get("node"), dict)
            ]
            break
    user = dict(identity)
    if uid:
        user["id"] = uid
        user["pk"] = uid
    user["username"] = identity.get("username") or target
    user["edge_owner_to_timeline_media"] = {
        "count": len(edges),
        "edges": edges,
    }
    return user


def _parse_post_html(document, expected_code=None):
    """Parse Instagram's SSR Relay post/reel HTML into a full media node dict."""
    parser = _JsonScripts()
    parser.feed(document)
    candidates = []
    for script in parser.scripts:
        if "xig_polaris_media" not in script:
            continue
        try:
            payload = json.loads(script)
        except ValueError:
            continue
        for media in _find_key(payload, "xig_polaris_media"):
            if not isinstance(media, dict):
                continue
            inner = media.get("if_not_gated_logged_out")
            if isinstance(inner, dict):
                candidates.append(inner)
            elif media.get("taken_at") or media.get("taken_at_timestamp"):
                candidates.append(media)
    if expected_code:
        for item in candidates:
            if (item.get("code") or item.get("shortcode")) == expected_code:
                return item
    return candidates[0] if candidates else None


# ---------------------------------------------------------------- fetcher


def _retry_after(headers, now=None):
    """Seconds from a Retry-After header: delta seconds or an HTTP date."""
    for key, value in (headers or {}).items():
        if key.lower() == "retry-after":
            try:
                return max(0, int(float(value)))
            except ValueError:
                pass
            try:
                when = email.utils.parsedate_to_datetime(str(value))
            except (TypeError, ValueError, IndexError):
                return None
            if when is None:
                return None
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return max(0, int((when - (now or utc_now())).total_seconds()))
    return None


# Markers of a soft block in a JSON error body (any 4xx): rate limited,
# login required or a checkpoint challenge.
SOFT_BLOCK = (
    "wait a few minutes",
    "please wait",
    "require_login",
    "login_required",
    "checkpoint_required",
    "challenge_required",
    "rate limit",
)


def _soft_block(data):
    if not isinstance(data, dict):
        return False
    if data.get("require_login") or data.get("spam"):
        return True
    text = " ".join(
        str(data.get(k) or "") for k in ("message", "error_type", "status")
    ).lower()
    return any(marker in text for marker in SOFT_BLOCK)


def _strip_media(user):
    """The profile without the media lists (they become post records)."""
    return {
        k: v
        for k, v in user.items()
        if not (k.startswith("edge_") and isinstance(v, dict) and "edges" in v)
        and not k.startswith("xdt_")
        and k != "polaris_ordered_timeline_connection"
    }


def _nodes(user):
    media = user.get("edge_owner_to_timeline_media") or {}
    nodes = [e.get("node") for e in media.get("edges") or [] if isinstance(e, dict)]
    if not nodes:
        # Newer shape: a v1-style item list under an xdt_ connection.
        for key, value in user.items():
            if key.startswith("xdt_") and isinstance(value, dict):
                nodes = [
                    e.get("node")
                    for e in value.get("edges") or []
                    if isinstance(e, dict)
                ]
                if nodes:
                    break
    return [n for n in nodes if isinstance(n, dict)]


def _shortcode(node):
    return node.get("shortcode") or node.get("code")


def _story_id(item):
    raw = item.get("pk") or item.get("id")
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    return text.split("_", 1)[0]


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _bool_opt(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _extract_reel_items(data, user_id):
    """Extract story item dicts from a /api/v1/feed/reels_media/ JSON payload."""
    if not isinstance(data, dict):
        return []
    uid = str(user_id)
    reels_media = data.get("reels_media")
    if isinstance(reels_media, list):
        for reel in reels_media:
            if not isinstance(reel, dict):
                continue
            reel_id = str(reel.get("id") or (reel.get("user") or {}).get("pk") or "")
            if not uid or not reel_id or reel_id == uid:
                items = reel.get("items")
                if isinstance(items, list):
                    return [i for i in items if isinstance(i, dict)]
    reels = data.get("reels")
    if isinstance(reels, dict):
        reel = reels.get(uid)
        if not isinstance(reel, dict) and len(reels) == 1:
            reel = next(iter(reels.values()))
        if isinstance(reel, dict) and isinstance(reel.get("items"), list):
            return [i for i in reel["items"] if isinstance(i, dict)]
    return []


class Fetcher:
    """One run over a batch of accounts. ``state`` is updated in place.

    ``transport(url, headers) -> (status, headers, body, final_url)``,
    ``clock() -> aware datetime``, ``monotonic() -> seconds`` and
    ``sleep(seconds)`` are seams for tests.
    """

    def __init__(
        self,
        accounts,
        state,
        options=None,
        *,
        cookies_file=None,
        transport=None,
        clock=utc_now,
        monotonic=time.monotonic,
        sleep=time.sleep,
        rng=None,
    ):
        opts = dict(env_options({}))
        opts.update({k: v for k, v in (options or {}).items() if v is not None})
        self.opts = opts
        self.accounts = list(accounts)
        self.state = state
        self.base_url = str(opts["base_url"]).rstrip("/")
        self.fetch_mode = str(opts["fetch_mode"] or "api").strip().lower()
        if self.fetch_mode not in ("api", "html", "auto"):
            raise ValueError("fetch_mode must be api, html or auto")
        self.stories_enabled = _bool_opt(opts["stories"])
        self.batch = int(opts["batch"])
        self.per_day = int(opts["per_day"])
        self.posts = int(opts["posts"])
        self.delay = float(opts["delay_s"])
        self.deadline = float(opts["deadline_s"])
        self.retries = max(1, int(opts["retries"]))
        self.cooldown = int(opts["cooldown_s"])
        self.max_cooldown = int(opts["max_cooldown_s"])
        if self.batch < 1 or self.per_day < 0 or self.posts < 1:
            raise ValueError(
                "batch and posts must be positive and per_day not negative"
            )
        self.transport = transport or Transport(
            cookies_file, float(opts["timeout_s"]), self.base_url
        )
        self.has_cookies = bool(getattr(self.transport, "has_cookies", cookies_file))
        self.prime = _bool_opt(opts["prime"])
        self.clock, self.monotonic, self.sleep = clock, monotonic, sleep
        self.rng = rng or random.Random()
        self.requests_this_run = 0
        # HTML 404 answers in a row before any profile came back as JSON/Relay.
        self.html_404_streak = 0
        self.summary = {
            "accounts": 0,
            "posts": 0,
            "stories": 0,
            "requests": 0,
            "stopped": None,
        }
        # Handles whose fetch failed this run, and {handle: (watermark, oldest)}
        # for a full page that starts after the stored newest post: the posts
        # in between were never seen and the endpoint cannot return them.
        self.failed = []
        self.capped = {}
        state.setdefault("accounts", {})
        state.setdefault("requests", {"day": None, "count": 0})
        state.setdefault("strikes", 0)
        state.setdefault("backoff_until", None)

    def _effective_mode(self):
        if self.fetch_mode == "auto":
            return "api" if self.has_cookies else "html"
        return self.fetch_mode

    # -- limits

    def _budget_left(self):
        day = self.clock().date().isoformat()
        counter = self.state["requests"]
        if counter.get("day") != day:
            counter["day"], counter["count"] = day, 0
        return self.per_day - counter["count"]

    def _spend(self):
        self.state["requests"]["count"] += 1
        self.summary["requests"] += 1

    def cooling_down(self):
        until = parse_iso(self.state.get("backoff_until"))
        return until is not None and self.clock() < until

    def _strike(self, retry_after=None):
        strikes = int(self.state.get("strikes") or 0) + 1
        wait = min(self.max_cooldown, self.cooldown * 2 ** (strikes - 1))
        if retry_after:
            wait = max(wait, min(self.max_cooldown, retry_after))
        self.state["strikes"] = strikes
        self.state["backoff_until"] = iso(self.clock() + timedelta(seconds=wait))

    # -- HTTP

    def _headers(self, referer=None):
        headers = {
            "User-Agent": self.opts["user_agent"],
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "X-IG-App-ID": str(self.opts["app_id"]),
            "X-Requested-With": "XMLHttpRequest",
        }
        if referer:
            headers["Referer"] = referer
        return headers

    def _html_headers(self, referer=None):
        headers = {
            "User-Agent": self.opts["user_agent"],
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin" if referer else "none",
            "Upgrade-Insecure-Requests": "1",
        }
        if referer:
            headers["Referer"] = referer
        return headers

    def _pause(self):
        if self.requests_this_run and self.delay > 0:
            self.sleep(self.delay + self.rng.random())

    def _get(self, url, referer=None, headers=None):
        """Bounded retries on 5xx/transport errors; Blocked on 401/429/login wall."""
        last = None
        req_headers = headers if headers is not None else self._headers(referer)
        for attempt in range(self.retries):
            if self._budget_left() <= 0:
                raise LimitReached("per-day request limit reached")
            self._pause()
            self.requests_this_run += 1
            self._spend()
            try:
                status, resp_headers, body, final = self.transport(url, req_headers)
            except (
                urllib.error.URLError,
                http.client.HTTPException,
                OSError,
                TimeoutError,
            ) as error:
                last = f"transport error ({type(error).__name__})"
            else:
                if status in (401, 429):
                    raise Blocked(
                        f"HTTP {status}", _retry_after(resp_headers, self.clock())
                    )
                if "/accounts/login" in (final or "") or "/challenge" in (final or ""):
                    raise Blocked("login wall")
                if status >= 500:
                    last = f"HTTP {status}"
                else:
                    return status, body
            if attempt + 1 < self.retries:
                self.sleep(min(60.0, 2.0 * 2**attempt))
        raise FetchError(last or "request failed")

    def _prime(self):
        if not self.prime or self.has_cookies:
            return
        try:
            headers = (
                self._html_headers()
                if self._effective_mode() == "html"
                else self._headers()
            )
            self._get(self.base_url + "/", headers=headers)
        except (Blocked, LimitReached):
            raise
        except FetchError:
            pass  # Priming is best effort.

    def profile(self, handle):
        url = (
            self.base_url
            + "/api/v1/users/web_profile_info/?"
            + urllib.parse.urlencode({"username": handle})
        )
        status, body = self._get(url, referer=f"{self.base_url}/{handle}/")
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            if status == 404:
                # Instagram also refuses anonymous clients with an HTML 404
                # page for every handle. A few in a row with no JSON answer
                # is a block, not a run of deleted accounts.
                self.html_404_streak += 1
                if self.html_404_streak >= HTML_404_BLOCK:
                    raise Blocked(
                        "repeated HTML 404 answers (anonymous access refused)"
                    )
                return "not_found", None
            if status >= 400:
                return f"http_{status}", None
            raise Blocked("non-JSON answer (likely a login wall)") from None
        # Instagram also signals rate limits and login walls as 400/403 or
        # 200 answers with a JSON error body; those stop the run like a 429.
        self.html_404_streak = 0
        if status == 404:
            return "not_found", None
        if _soft_block(data):
            raise Blocked(f"soft block (HTTP {status})")
        if status >= 400:
            return f"http_{status}", None
        if isinstance(data, dict) and data.get("status") == "fail":
            return "error", None
        user = ((data or {}).get("data") or {}).get("user")
        if not user:
            return "not_found", None
        return "ok", user

    def profile_html(self, handle):
        url = f"{self.base_url}/{urllib.parse.quote(handle)}/"
        status, body = self._get(url, headers=self._html_headers())
        text = body.decode("utf-8", errors="replace")
        if status == 404:
            self.html_404_streak += 1
            if self.html_404_streak >= HTML_404_BLOCK:
                raise Blocked("repeated HTML 404 answers (anonymous access refused)")
            return "not_found", None
        if status >= 400:
            return f"http_{status}", None
        user = _parse_profile_html(text, handle)
        if not user:
            if "httpErrorPage" in text or "Page Not Found" in text:
                return "not_found", None
            raise Blocked("profile HTML lacked embedded profile payload (login wall)")
        self.html_404_streak = 0
        return "ok", user

    def post_detail_html(self, code, *, is_reel=False, referer=None):
        segment = "reel" if is_reel else "p"
        url = f"{self.base_url}/{segment}/{urllib.parse.quote(str(code))}/"
        status, body = self._get(url, headers=self._html_headers(referer=referer))
        if status >= 400:
            return None
        text = body.decode("utf-8", errors="replace")
        return _parse_post_html(text, expected_code=str(code))

    def stories_for_user(self, user_id, handle):
        url = (
            self.base_url
            + "/api/v1/feed/reels_media/?"
            + urllib.parse.urlencode({"reel_ids": str(user_id)})
        )
        status, body = self._get(
            url, referer=f"{self.base_url}/stories/{urllib.parse.quote(handle)}/"
        )
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            if status == 404:
                return []
            if status >= 400:
                raise FetchError(f"stories HTTP {status}")
            raise Blocked("non-JSON stories answer (likely a login wall)") from None
        if status == 404:
            return []
        if _soft_block(data):
            raise Blocked(f"stories soft block (HTTP {status})")
        if status >= 400:
            raise FetchError(f"stories HTTP {status}")
        if isinstance(data, dict) and data.get("status") == "fail":
            raise FetchError("stories status=fail")
        return _extract_reel_items(data, user_id)

    # -- rotation

    def select(self, select_all=False):
        order = {a["handle"]: i for i, a in enumerate(self.accounts)}
        seen = self.state["accounts"]
        ranked = sorted(
            self.accounts,
            key=lambda a: (
                (seen.get(a["handle"]) or {}).get("last_fetched") or "",
                order[a["handle"]],
            ),
        )
        return ranked if select_all else ranked[: self.batch]

    def _enrich_html_nodes(self, handle, user, known_posts, started):
        """Enrich HTML timeline nodes missing taken_at via /p/<code>/ pages."""
        nodes = _nodes(user)[: self.posts]
        enriched = []
        skip_codes = set()
        new_known = dict(known_posts)
        referer = f"{self.base_url}/{urllib.parse.quote(handle)}/"
        for node in nodes:
            code = _shortcode(node)
            if not code:
                continue
            code = str(code)
            has_ts = bool(_int(node.get("taken_at_timestamp", node.get("taken_at"))))
            caption_sha = _grid_caption_sha(node)
            cached = known_posts.get(code)
            if not has_ts and isinstance(cached, dict) and cached.get("caption_sha") == caption_sha:
                # Already fetched full detail on an earlier committed run and
                # grid caption has not changed: skip redundant HTTP fetch.
                skip_codes.add(code)
                enriched.append(
                    dict(node, taken_at=cached.get("taken_at"))
                    if cached.get("taken_at")
                    else node
                )
                continue
            if not has_ts:
                if self.monotonic() - started > self.deadline:
                    self.summary["stopped"] = "deadline"
                    break
                detail = None
                try:
                    detail = self.post_detail_html(
                        code,
                        is_reel=(node.get("product_type") == "clips"),
                        referer=referer,
                    )
                except (Blocked, LimitReached):
                    raise
                except FetchError:
                    detail = None
                if isinstance(detail, dict) and _int(
                    detail.get("taken_at_timestamp", detail.get("taken_at"))
                ):
                    merged = dict(detail)
                    if not merged.get("accessibility_caption") and node.get(
                        "accessibility_caption"
                    ):
                        merged["accessibility_caption"] = node["accessibility_caption"]
                    node = merged
                else:
                    fallback_ts = _parse_accessibility_date(
                        node.get("accessibility_caption")
                    )
                    if fallback_ts:
                        node = dict(node, taken_at=fallback_ts)
            ts = _int(node.get("taken_at_timestamp", node.get("taken_at")))
            if ts:
                new_known[code] = {"taken_at": ts, "caption_sha": caption_sha}
            enriched.append(node)
        if len(new_known) > KNOWN_POSTS_LIMIT:
            keep = sorted(
                new_known.items(),
                key=lambda kv: _int((kv[1] or {}).get("taken_at")),
                reverse=True,
            )[:KNOWN_POSTS_LIMIT]
            new_known = dict(keep)
        user["edge_owner_to_timeline_media"] = {
            "count": len(enriched),
            "edges": [{"node": n} for n in enriched],
        }
        return skip_codes, new_known

    def run(self, select_all=False):
        """Yield (handle, [raw records]) per fetched account; never raises Blocked."""
        if self.stories_enabled and not self.has_cookies:
            self.summary["stories_auth_required"] = True
        if self.cooling_down():
            self.summary["stopped"] = "cooling_down"
            return
        if self._budget_left() <= 0:
            self.summary["stopped"] = "per_day_limit"
            return
        started = self.monotonic()
        mode = self._effective_mode()
        try:
            self._prime()
        except Blocked as error:
            self._strike(error.retry_after)
            self.summary["stopped"] = "blocked"
            return
        except LimitReached:
            self.summary["stopped"] = "per_day_limit"
            return
        for account in self.select(select_all):
            if self.monotonic() - started > self.deadline:
                self.summary["stopped"] = "deadline"
                return
            handle = account["handle"]
            fetched_at = iso(self.clock())
            try:
                if mode == "html":
                    status, user = self.profile_html(handle)
                else:
                    status, user = self.profile(handle)
            except Blocked as error:
                self._strike(error.retry_after)
                self.summary["stopped"] = "blocked"
                return
            except LimitReached:
                self.summary["stopped"] = "per_day_limit"
                return
            except FetchError as error:
                status, user = "error", None
                self.state["accounts"].setdefault(handle, {})["error"] = str(error)
            if user and user.get("is_private") and not _nodes(user):
                status = "private"
            if status == "error" or status.startswith("http_"):
                self.failed.append(handle)
            entry = self.state["accounts"].setdefault(handle, {})
            skip_codes = set()
            new_known = None
            post_source = "web_profile_html" if mode == "html" else "web_profile_info"
            if user and mode == "html":
                try:
                    skip_codes, new_known = self._enrich_html_nodes(
                        handle, user, entry.get("known_posts") or {}, started
                    )
                except Blocked as error:
                    self._strike(error.retry_after)
                    self.summary["stopped"] = "blocked"
                    return
                except LimitReached:
                    self.summary["stopped"] = "per_day_limit"
                    return
            story_items = []
            if user and self.stories_enabled and self.has_cookies:
                uid = str(user.get("pk") or user.get("id") or "").strip()
                latest_reel = user.get("latest_reel_media")
                if uid and latest_reel != 0:
                    try:
                        story_items = self.stories_for_user(uid, handle)
                        entry.pop("stories_error", None)
                    except Blocked as error:
                        self._strike(error.retry_after)
                        self.summary["stopped"] = "blocked"
                        return
                    except LimitReached:
                        self.summary["stopped"] = "per_day_limit"
                        return
                    except FetchError as error:
                        entry["stories_error"] = str(error)
            records = self.records(
                account,
                status,
                user,
                fetched_at,
                stories=story_items,
                post_source=post_source,
                skip_codes=skip_codes,
            )
            watermark = entry.get("newest_taken_at")
            entry.update(last_fetched=fetched_at, status=status)
            if status != "error":
                entry.pop("error", None)
            if user:
                uid = str(user.get("pk") or user.get("id") or "") or None
                entry["user_id"] = uid
                if "latest_reel_media" in user and user.get("latest_reel_media") is not None:
                    entry["latest_reel_media"] = _int(user.get("latest_reel_media"))
                if new_known is not None:
                    entry["known_posts"] = new_known
                nodes = _nodes(user)
                taken = [
                    _int(n.get("taken_at_timestamp", n.get("taken_at")))
                    for n in nodes[: self.posts]
                ]
                taken = [t for t in taken if t]
                unpinned = [
                    _int(n.get("taken_at_timestamp", n.get("taken_at")))
                    for n in nodes[: self.posts]
                    if not (
                        n.get("pinned_for_users")
                        or n.get("timeline_pinned_user_ids")
                    )
                ]
                unpinned = [t for t in unpinned if t]
                if (
                    watermark
                    and len(nodes) >= self.posts
                    and unpinned
                    and min(unpinned) > watermark
                ):
                    self.capped[handle] = (watermark, min(unpinned))
                if taken:
                    entry["newest_taken_at"] = max(
                        [entry.get("newest_taken_at") or 0, *taken]
                    )
                if story_items:
                    story_ts = [_int(s.get("taken_at")) for s in story_items]
                    story_ts = [t for t in story_ts if t]
                    if story_ts:
                        entry["newest_story_at"] = max(
                            [entry.get("newest_story_at") or 0, *story_ts]
                        )
            self.state["strikes"] = 0
            self.state["backoff_until"] = None
            self.summary["accounts"] += 1
            self.summary["posts"] += sum(r["kind"] == "post" for r in records)
            self.summary["stories"] += sum(r["kind"] == "story" for r in records)
            yield handle, records
            if self.summary["stopped"] == "deadline":
                return

    def records(
        self,
        account,
        status,
        user,
        fetched_at,
        *,
        stories=None,
        post_source="web_profile_info",
        skip_codes=None,
    ):
        handle = account["handle"]
        config = {k: v for k, v in account.items() if k != "handle"}
        base = {"account": handle, "fetched_at": fetched_at, "fetcher": FETCHER_VERSION}
        if not user:
            return [
                dict(base, kind="status", id=f"{handle}:{fetched_at}", status=status)
            ]
        uid = user.get("pk") if user.get("pk") not in (None, "") else user.get("id")
        owner = {
            "id": str(uid) if uid not in (None, "") else None,
            "username": user.get("username") or handle,
            "full_name": user.get("full_name") or None,
            "is_verified": bool(user.get("is_verified")),
        }
        out = [
            dict(
                base,
                kind="profile",
                id=owner["id"] or handle,
                status=status,
                raw=_strip_media(user),
            )
        ]
        configs = {
            a["handle"]: {k: v for k, v in a.items() if k != "handle"}
            for a in self.accounts
        }
        skipped = skip_codes or set()
        for node in _nodes(user)[: self.posts]:
            code = _shortcode(node)
            if not code or str(code) in skipped:
                continue
            # A collaboration post carries its owner's config whichever
            # followed account it was fetched through, so both copies match.
            post_owner = node.get("owner") or node.get("user") or {}
            owner_handle = str(post_owner.get("username") or "").lower()
            owner_uid = post_owner.get("pk") or post_owner.get("id")
            if owner_handle and owner_handle != handle:
                post_config = configs.get(owner_handle, {})
            elif not owner_handle and owner_uid and str(owner_uid) != owner["id"]:
                post_config = {}  # Someone else's post; their handle is unknown.
            else:
                post_config = config
            out.append(
                dict(
                    base,
                    kind="post",
                    id=str(code),
                    source=post_source,
                    account_config=post_config,
                    owner=owner,
                    raw=node,
                )
            )
        for item in stories or []:
            sid = _story_id(item)
            if not sid:
                continue
            out.append(
                dict(
                    base,
                    kind="story",
                    id=sid,
                    source="reels_media",
                    account_config=config,
                    owner=owner,
                    raw=item,
                )
            )
        return out


# ---------------------------------------------------------------- CLI


def _load_state(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return {}


def _save_state(path, state):
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=1, sort_keys=True)
    os.replace(tmp, path)


def export(args, environ=None, transport=None, sleep=time.sleep):
    environ = os.environ if environ is None else environ
    options = env_options(environ)
    for name in OPTIONS:
        value = getattr(args, name, None)
        if value is not None:
            options[name] = value
    accounts = parse_accounts(options["accounts"], options["accounts_file"])
    if not accounts:
        raise SystemExit(
            "no accounts configured (--accounts-file or INSTAGRAM_FEED_ACCOUNTS)"
        )
    state_path = args.state or args.out + ".state.json"
    state = _load_state(state_path)
    fetcher = Fetcher(
        accounts,
        state,
        options,
        cookies_file=environ.get(COOKIES_ENV) or None,
        transport=transport,
        sleep=sleep,
    )
    written = 0
    with open(args.out, "a", encoding="utf-8", newline="\n") as out:
        for _, records in fetcher.run(select_all=args.all):
            for record in records:
                out.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                written += 1
            out.flush()
            _save_state(state_path, state)
    _save_state(state_path, state)
    summary = dict(fetcher.summary, written=written)
    if state.get("backoff_until"):
        summary["backoff_until"] = state["backoff_until"]
    print(json.dumps(summary, sort_keys=True), file=sys.stderr)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    exp = sub.add_parser("export", help="fetch a batch of accounts and append JSONL")
    exp.add_argument("--out", required=True, help="JSONL file to append to")
    exp.add_argument("--state", help="state file (default: <out>.state.json)")
    exp.add_argument(
        "--all", action="store_true", help="fetch every account, not one batch"
    )
    for name, (_, description) in OPTIONS.items():
        exp.add_argument("--" + name.replace("_", "-"), dest=name, help=description)
    check = sub.add_parser(
        "accounts", help="validate and list the configured accounts (no network)"
    )
    check.add_argument("--accounts", dest="accounts")
    check.add_argument("--accounts-file", dest="accounts_file")
    args = parser.parse_args(argv)
    if args.command == "accounts":
        options = env_options()
        accounts = parse_accounts(
            args.accounts or options["accounts"],
            args.accounts_file or options["accounts_file"],
        )
        for account in accounts:
            print(json.dumps(account, sort_keys=True))
        return 0
    export(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
