#!/usr/bin/env python3
"""Standard-library Reddit fetcher for throughline-reddit.

    python fetch.py export --out FILE [options]

Reads the owner's Reddit account listings (saved, upvoted, downvoted,
submitted, comments) through Reddit OAuth, or through a signed-in browser
session (``--transport session``, which needs browser.py next to this file),
and writes raw records as JSONL: one JSON object per line with ``kind``, the
Reddit fullname as ``id`` and the untouched source object as ``raw``.

Only the Python standard library is used (Python 3.12 to 3.14), so the file can
be copied alone to the machine that holds the credentials. Credentials are read
only from environment variables, never from the command line, and are never
printed, logged or written to the output or state files.

Environment: REDDIT_USER_AGENT (required for any request), REDDIT_CLIENT_ID,
REDDIT_CLIENT_SECRET, and either REDDIT_REFRESH_TOKEN or REDDIT_USERNAME plus
REDDIT_PASSWORD (script app), or a ready REDDIT_ACCESS_TOKEN. Non-secret
options may be given as flags or as the REDDIT_* variables listed in
``SETTINGS``. See the package README for the full table.
"""

import argparse
import base64
import copy
import http.client
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

FETCHER_VERSION = "2"
USER_LISTINGS = ("saved", "upvoted", "downvoted", "submitted", "comments")
DEFAULT_LISTINGS = ("saved", "upvoted", "submitted", "comments")
# Listings Reddit returns newest-created first (sort=new): a --since bound may
# stop paging early. Saved and voted lists are ordered by action time, which
# Reddit does not expose, so bounds only filter them.
DATE_ORDERED = frozenset({"submitted", "comments"})
TRANSPORTS = ("oauth", "session")
USER_NAME = re.compile(r"^[A-Za-z0-9_-]{3,20}$")
FULLNAME = re.compile(r"^t[1-6]_[a-z0-9]{1,16}$")
SAFE_ERROR = re.compile(r"^[A-Za-z0-9_ .-]{1,60}$")
MAX_BODY = 16 * 1024 * 1024
HEAD_SIZE = 50
# Consecutive ids from the last head that end an incremental pass.
STOP_RUN = 3

# Non-secret settings: name -> (environment variable, default).
SETTINGS = {
    "user_agent": ("REDDIT_USER_AGENT", None),
    "username": ("REDDIT_USERNAME", None),
    "listings": ("REDDIT_LISTINGS", ",".join(DEFAULT_LISTINGS)),
    "transport": ("REDDIT_TRANSPORT", "oauth"),
    "since": ("REDDIT_SINCE", None),
    "until": ("REDDIT_UNTIL", None),
    "max_pages": ("REDDIT_MAX_PAGES", "10"),
    "page_size": ("REDDIT_PAGE_SIZE", "100"),
    "max_requests": ("REDDIT_MAX_REQUESTS", "200"),
    "max_requests_per_day": ("REDDIT_MAX_REQUESTS_PER_DAY", "2000"),
    "min_interval": ("REDDIT_MIN_INTERVAL", "1.0"),
    "retries": ("REDDIT_RETRIES", "5"),
    "max_wait": ("REDDIT_MAX_WAIT", "300"),
    "timeout": ("REDDIT_TIMEOUT", "30"),
    "api_base": ("REDDIT_API_BASE", "https://oauth.reddit.com"),
    "auth_base": ("REDDIT_AUTH_BASE", "https://www.reddit.com"),
    # session transport: a signed-in headless Chromium (browser.py) with this
    # private persistent profile directory.
    "session_profile": ("REDDIT_SESSION_PROFILE", None),
}
# Secret settings: name -> environment variable. Read from the environment only.
SECRETS = {
    "client_id": "REDDIT_CLIENT_ID",
    "client_secret": "REDDIT_CLIENT_SECRET",
    "refresh_token": "REDDIT_REFRESH_TOKEN",
    "password": "REDDIT_PASSWORD",
    "access_token": "REDDIT_ACCESS_TOKEN",
}


class FetchError(Exception):
    """A failure whose message never contains credentials or response bodies."""


class BudgetExhausted(Exception):
    """A request limit or a long server-requested wait ends the run early."""


class HttpStatus(FetchError):
    def __init__(self, status, what):
        super().__init__(f"{what} returned HTTP {status}")
        self.status = status


def utc_now():
    return datetime.now(timezone.utc)


def iso(moment):
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_time(value, name="time"):
    """ISO date or timestamp; a bare date or a naive time is read as UTC."""
    if value in (None, ""):
        return None
    try:
        moment = datetime.fromisoformat(str(value).strip())
    except ValueError:
        raise FetchError(f"{name} must be an ISO date or timestamp") from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def epoch(value):
    try:
        return datetime.fromtimestamp(float(value), timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def split_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [v.strip() for v in str(value).split(",") if v.strip()]


def check_base(url, name):
    """HTTPS base URL without credentials; plain HTTP only for loopback tests."""
    parts = urllib.parse.urlsplit(url or "")
    loopback = parts.hostname in ("127.0.0.1", "localhost", "::1")
    if not parts.hostname or not (
        parts.scheme == "https" or (parts.scheme == "http" and loopback)
    ):
        raise FetchError(f"{name} must be an https URL (http only for loopback)")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise FetchError(f"{name} must not contain credentials, query or fragment")
    return url.rstrip("/")


class Config:
    """Validated non-secret options."""

    def __init__(self, values):
        def get(name):
            value = values.get(name)
            return SETTINGS[name][1] if value in (None, "") else value

        def number(name, kind, low, high):
            try:
                value = kind(get(name))
            except (TypeError, ValueError):
                raise FetchError(f"{SETTINGS[name][0]} must be a number") from None
            if not low <= value <= high:
                raise FetchError(
                    f"{SETTINGS[name][0]} must be between {low} and {high}"
                )
            return value

        self.user_agent = (get("user_agent") or "").strip()
        if any(ch in self.user_agent for ch in "\r\n"):
            raise FetchError("REDDIT_USER_AGENT must be one line")
        self.username = (get("username") or "").strip() or None
        if self.username and not USER_NAME.fullmatch(self.username):
            raise FetchError("REDDIT_USERNAME is not a valid Reddit username")
        listings = get("listings")
        if str(listings).strip().lower() in ("none", "off", "-"):
            self.listings = ()
        else:
            self.listings = tuple(dict.fromkeys(split_list(listings)))
        unknown = set(self.listings) - set(USER_LISTINGS)
        if unknown:
            raise FetchError(
                "REDDIT_LISTINGS accepts " + ", ".join(USER_LISTINGS) + " or none"
            )
        self.transport = str(get("transport")).strip().lower()
        if self.transport not in TRANSPORTS:
            raise FetchError("REDDIT_TRANSPORT must be oauth or session")
        self.session_profile = get("session_profile")
        if self.transport == "session" and not self.session_profile:
            raise FetchError("REDDIT_TRANSPORT=session requires REDDIT_SESSION_PROFILE")
        self.since = parse_time(get("since"), "REDDIT_SINCE")
        self.until = parse_time(get("until"), "REDDIT_UNTIL")
        if self.since and self.until and self.until < self.since:
            raise FetchError("REDDIT_UNTIL precedes REDDIT_SINCE")
        self.max_pages = number("max_pages", int, 1, 100)
        self.page_size = number("page_size", int, 1, 100)
        self.max_requests = number("max_requests", int, 1, 100000)
        self.max_requests_per_day = number("max_requests_per_day", int, 1, 1000000)
        self.min_interval = number("min_interval", float, 0, 3600)
        self.retries = number("retries", int, 0, 10)
        self.max_wait = number("max_wait", float, 0, 86400)
        self.timeout = number("timeout", float, 1, 600)
        self.api_base = check_base(get("api_base"), "REDDIT_API_BASE")
        self.auth_base = check_base(get("auth_base"), "REDDIT_AUTH_BASE")
        self.full = bool(values.get("full"))

    @classmethod
    def from_env(cls, environ=None, overrides=None):
        environ = os.environ if environ is None else environ
        values = {name: environ.get(env) for name, (env, _) in SETTINGS.items()}
        values.update({k: v for k, v in (overrides or {}).items() if v is not None})
        return cls(values)


class Credentials:
    """OAuth credentials, kept out of repr, output and error messages."""

    def __init__(
        self,
        client_id=None,
        client_secret=None,
        refresh_token=None,
        password=None,
        access_token=None,
    ):
        self.client_id = client_id or None
        self.client_secret = client_secret or ""
        self.refresh_token = refresh_token or None
        self.password = password or None
        self.access_token = access_token or None

    @classmethod
    def from_env(cls, environ=None):
        environ = os.environ if environ is None else environ
        return cls(**{name: environ.get(env) for name, env in SECRETS.items()})

    def can_grant(self, username):
        return bool(
            self.client_id and (self.refresh_token or (username and self.password))
        )

    def usable(self, username):
        return bool(self.access_token) or self.can_grant(username)

    def __repr__(self):
        return "Credentials(<redacted>)"

    def values(self):
        return [
            v
            for v in (
                self.client_id,
                self.client_secret,
                self.refresh_token,
                self.password,
                self.access_token,
            )
            if v
        ]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: they could carry a bearer token to another host."""

    def redirect_request(self, *args, **kwargs):
        return None


def retry_after(headers):
    try:
        return max(0.0, float(headers.get("Retry-After")))
    except (TypeError, ValueError):
        return None


class Client:
    """Polite HTTP access: pacing per lane, budgets, retries and rate limits."""

    def __init__(
        self,
        config,
        credentials,
        state,
        *,
        opener=None,
        sleep=time.sleep,
        clock=time.monotonic,
        now=utc_now,
    ):
        self.config = config
        self.credentials = credentials
        self.state = state
        self.opener = opener or urllib.request.build_opener(NoRedirect)
        self.sleep = sleep
        self.clock = clock
        self.now = now
        self.requests = 0
        self.waited = 0.0
        self._last = {}
        self._hold = {}
        self._token = credentials.access_token
        self._expiry = None

    def _budget(self):
        day = self.now().strftime("%Y-%m-%d")
        usage = self.state.setdefault("requests", {})
        if usage.get("day") != day:
            usage.clear()
            usage.update(day=day, count=0)
        if self.requests >= self.config.max_requests:
            raise BudgetExhausted("per-run request limit reached")
        if usage["count"] >= self.config.max_requests_per_day:
            raise BudgetExhausted("per-day request limit reached")
        self.requests += 1
        usage["count"] += 1

    def _wait(self, seconds):
        if seconds > self.config.max_wait:
            raise BudgetExhausted("Reddit asked for a wait longer than REDDIT_MAX_WAIT")
        if seconds > 0:
            self.waited += seconds
            self.sleep(seconds)

    def _pace(self, lane):
        interval = self.config.min_interval
        due = max(
            self._last.get(lane, float("-inf")) + interval,
            self._hold.get(lane, float("-inf")),
        )
        self._wait(due - self.clock())
        self._last[lane] = self.clock()

    def _note_rate_limit(self, lane, headers):
        """Reddit reports X-Ratelimit-Remaining/-Reset on OAuth responses."""
        try:
            remaining = float(headers.get("X-Ratelimit-Remaining"))
            reset = float(headers.get("X-Ratelimit-Reset"))
        except (TypeError, ValueError):
            return
        if remaining < 1:
            self._hold[lane] = self.clock() + reset + 1

    def request(self, method, url, what, *, lane, headers=None, data=None):
        """Return the body of a 2xx response; retry 429, 5xx and transport errors."""
        attempt = 0
        refreshed = False
        while True:
            sent = {"User-Agent": self.config.user_agent, **(headers or {})}
            if lane == "oauth":
                sent["Authorization"] = "Bearer " + self.token()
            self._budget()
            self._pace(lane)
            request = urllib.request.Request(
                url, data=data, method=method, headers=sent
            )
            status, reply = None, {}
            try:
                with self.opener.open(request, timeout=self.config.timeout) as response:
                    status, reply = response.status, response.headers
                    body = response.read(MAX_BODY + 1)
            except urllib.error.HTTPError as error:
                status, reply = error.code, error.headers or {}
                error.close()
            except (
                urllib.error.URLError,
                http.client.HTTPException,
                socket.timeout,
                TimeoutError,
                ConnectionError,
            ):
                status = None
            self._note_rate_limit(lane, reply)
            if status is not None and 200 <= status < 300:
                if len(body) > MAX_BODY:
                    raise FetchError(f"{what} response is too large")
                return body
            if status == 401 and lane == "oauth" and not refreshed:
                refreshed = True
                if self.credentials.can_grant(self.config.username):
                    self._token = None
                    continue
                raise FetchError(f"{what} was refused: the access token is not valid")
            if status is None or status == 429 or status >= 500:
                if attempt >= self.config.retries:
                    shown = status if status is not None else "transport error"
                    raise FetchError(f"{what} failed after retries ({shown})")
                delay = retry_after(reply)
                if delay is None:
                    delay = min(60.0, 2.0**attempt)
                attempt += 1
                self._wait(delay)
                continue
            raise HttpStatus(status, what)

    def token(self):
        if self._token and (self._expiry is None or self.clock() < self._expiry - 60):
            return self._token
        if not self.credentials.can_grant(self.config.username):
            raise FetchError(
                "no OAuth credentials: set REDDIT_CLIENT_ID with REDDIT_REFRESH_TOKEN, "
                "or REDDIT_USERNAME and REDDIT_PASSWORD, or REDDIT_ACCESS_TOKEN"
            )
        if self.credentials.refresh_token:
            form = {
                "grant_type": "refresh_token",
                "refresh_token": self.credentials.refresh_token,
            }
        else:
            form = {
                "grant_type": "password",
                "username": self.config.username,
                "password": self.credentials.password,
            }
        basic = base64.b64encode(
            f"{self.credentials.client_id}:{self.credentials.client_secret}".encode()
        ).decode()
        body = self.request(
            "POST",
            self.config.auth_base + "/api/v1/access_token",
            "Reddit token endpoint",
            lane="auth",
            headers={
                "Authorization": "Basic " + basic,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data=urllib.parse.urlencode(form).encode(),
        )
        try:
            payload = json.loads(body)
        except ValueError:
            raise FetchError("Reddit token endpoint returned invalid JSON") from None
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            reason = payload.get("error") if isinstance(payload, dict) else None
            detail = (
                f" ({reason})"
                if isinstance(reason, str) and SAFE_ERROR.fullmatch(reason)
                else ""
            )
            raise FetchError("Reddit token endpoint returned no access token" + detail)
        self._token = token
        try:
            self._expiry = self.clock() + float(payload.get("expires_in", 3600))
        except (TypeError, ValueError):
            self._expiry = self.clock() + 3600
        return token

    def get_json(self, base, path, params, what, lane):
        url = base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        body = self.request("GET", url, what, lane=lane)
        try:
            return json.loads(body)
        except ValueError:
            raise FetchError(f"{what} returned invalid JSON") from None


class SessionClient(Client):
    """Client whose Reddit reads go through a signed-in browser (browser.py).

    A signed-in page can load the same-origin ``.json`` listings of its own
    account (``/user/<name>/saved.json`` and so on), so no OAuth app is needed.
    Pacing, budgets and retries are the same as for the OAuth transport.
    """

    def __init__(self, config, credentials, state, *, browser=None, **options):
        super().__init__(config, credentials, state, **options)
        self._browser_module = browser
        self._chrome = None
        self.signed_in_as = None

    def _module(self):
        if self._browser_module is None:
            # browser.py sits next to fetch.py (also standard library only).
            import importlib.util

            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "browser.py")
            spec = importlib.util.spec_from_file_location("reddit_session_browser", path)
            if spec is None or not os.path.exists(path):
                raise FetchError("the session transport needs browser.py next to fetch.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self._browser_module = module
        return self._browser_module

    def chrome(self):
        if self._chrome is not None:
            return self._chrome
        module = self._module()
        try:
            chrome = module.Chrome(self.config.session_profile)
            module.ensure_page(chrome)
            name = module.whoami(chrome)
            if not name:
                if not (self.config.username and self.credentials.password):
                    chrome.close()
                    raise FetchError(
                        "browser session is not signed in and REDDIT_USERNAME/REDDIT_PASSWORD are not set"
                    )
                name = module.login(chrome, self.config.username, self.credentials.password)
        except module.BrowserError as error:
            raise FetchError(f"browser session failed: {error}") from None
        if self.config.username and name.lower() != self.config.username.lower():
            chrome.close()
            raise FetchError("browser session is signed in as a different account")
        self.signed_in_as = name
        self._chrome = chrome
        return chrome

    def close(self):
        if self._chrome is not None:
            self._chrome.close()
            self._chrome = None

    def _retrying(self, what, lane, action):
        attempt = 0
        while True:
            self._budget()
            self._pace(lane)
            try:
                status, body = action()
            except self._module().BrowserError:
                status, body = None, None
            if status is not None and 200 <= status < 300:
                return body
            if status is None or status == 429 or status >= 500:
                if attempt >= self.config.retries:
                    shown = status if status is not None else "browser error"
                    raise FetchError(f"{what} failed after retries ({shown})")
                attempt += 1
                self._wait(min(60.0, 2.0**attempt))
                continue
            raise HttpStatus(status, what)

    def get_json(self, base, path, params, what, lane):
        module = self._module()
        chrome = self.chrome()
        params = dict(params or {})
        url = path if path.endswith(".json") else path + ".json"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        body = self._retrying(what, "session", lambda: self._get(module, chrome, url))
        try:
            return json.loads(body)
        except ValueError:
            raise FetchError(f"{what} returned invalid JSON") from None

    @staticmethod
    def _get(module, chrome, url):
        reply = module.get(chrome, url)
        return reply.get("status"), reply.get("body")

def listing_children(payload, what):
    if not isinstance(payload, dict) or payload.get("kind") != "Listing":
        raise FetchError(f"{what} did not return a listing")
    data = payload.get("data") or {}
    children = [c for c in data.get("children") or [] if isinstance(c, dict)]
    after = data.get("after")
    return children, after if isinstance(after, str) and after else None


def child_fullname(child):
    data = child.get("data") or {}
    name = data.get("name") or (
        f"{child.get('kind')}_{data.get('id')}" if data.get("id") else None
    )
    return name if isinstance(name, str) and FULLNAME.fullmatch(name) else None


class Exporter:
    """Fetch raw records. ``state`` is the incremental cursor (JSON-safe dict).

    Iterate ``records()``; afterwards ``state`` holds the advanced cursor and
    ``summary`` the counts. Personal listings advance their head (the newest
    fullnames seen) only when a pass ends naturally; a pass cut short by a
    request limit is resumed from its ``after`` token by the next run.
    """

    def __init__(self, config, credentials, state=None, **client_options):
        self.config = config
        self.credentials = credentials
        self.state = copy.deepcopy(state or {})
        self.state["version"] = 1
        self.state.setdefault("listings", {})
        client_class = SessionClient if config.transport == "session" else Client
        self.client = client_class(config, credentials, self.state, **client_options)
        self.summary = {
            "listings": {},
            "skipped": [],
            "truncated": None,
        }
        self.account = None

    def in_bounds(self, moment):
        if moment is None:
            return True
        if self.config.since and moment < self.config.since:
            return False
        return not (self.config.until and moment > self.config.until)

    def records(self):
        started = self.client.now()
        try:
            if self.config.listings:
                if self.config.transport == "session" or self.credentials.usable(
                    self.config.username
                ):
                    yield from self.account_records()
                else:
                    self.summary["skipped"].append(
                        {"what": "listings", "reason": "no OAuth credentials"}
                    )
        except BudgetExhausted as reason:
            self.summary["truncated"] = str(reason)
        finally:
            if isinstance(self.client, SessionClient):
                self.client.close()
        yield {
            "kind": "reddit.export_summary",
            "fetcher_version": FETCHER_VERSION,
            "started_at": iso(started),
            "finished_at": iso(self.client.now()),
            "account": self.account,
            "requests": self.client.requests,
            "waited_s": round(self.client.waited, 3),
            **copy.deepcopy(self.summary),
        }

    def resolve_account(self):
        if self.config.username:
            return self.config.username
        if isinstance(self.client, SessionClient):
            self.client.chrome()
            name = self.client.signed_in_as
            if not isinstance(name, str) or not USER_NAME.fullmatch(name):
                raise FetchError("browser session has no valid username")
            return name
        payload = self.client.get_json(
            self.config.api_base,
            "/api/v1/me",
            {"raw_json": 1},
            "Reddit identity",
            "oauth",
        )
        name = payload.get("name") if isinstance(payload, dict) else None
        if not isinstance(name, str) or not USER_NAME.fullmatch(name):
            raise FetchError("Reddit identity response has no valid username")
        return name

    def account_records(self):
        self.account = self.resolve_account()
        for listing in self.config.listings:
            yield from self.listing_records(listing)

    def listing_records(self, listing):
        """One pass over an account listing, newest first.

        A pass ends at the listing's end, at ``max_pages``, at the ``since``
        bound of a date-ordered listing, or at a run of ``STOP_RUN``
        consecutive ids from the last head. A single known id is not enough:
        saving or voting on an item again moves it to the top of the listing,
        above items that are still new.
        """
        config = self.config
        st = self.state["listings"].setdefault(listing, {})
        old_head = list(st.get("head") or [])
        resume = st.get("resume")
        if resume:
            after = resume.get("after")
            stop = set(resume.get("stop") or [])
            head = list(resume.get("head") or [])
        else:
            after = None
            stop = set() if config.full else set(old_head)
            head = []
        need = min(STOP_RUN, len(stop))
        streak = 0
        counts = self.summary["listings"].setdefault(
            listing, {"items": 0, "pages": 0, "end": None}
        )
        path = f"/user/{self.account}/{listing}"
        params = {"limit": config.page_size, "raw_json": 1}
        if listing in DATE_ORDERED:
            params["sort"] = "new"
        pages = 0
        end = None
        oldest = None

        def remember(name):
            # The head holds only ids inside the bounds, so items filtered out
            # by an --until bound are still found by a later run.
            if len(head) < HEAD_SIZE and name not in head:
                head.append(name)

        try:
            while end is None:
                query = dict(params, **({"after": after} if after else {}))
                payload = self.client.get_json(
                    config.api_base, path, query, f"Reddit {listing} listing", "oauth"
                )
                observed = iso(self.client.now())
                children, after = listing_children(payload, f"Reddit {listing} listing")
                pages += 1
                counts["pages"] += 1
                for child in children:
                    name = child_fullname(child)
                    if name is None:
                        continue
                    if name in stop:
                        remember(name)
                        streak += 1
                        if streak >= need:
                            end = "known"
                            break
                        continue
                    streak = 0
                    created = epoch((child.get("data") or {}).get("created_utc"))
                    if (
                        listing in DATE_ORDERED
                        and config.since
                        and created
                        and created < config.since
                    ):
                        end = "since"
                        break
                    if not self.in_bounds(created):
                        continue
                    remember(name)
                    counts["items"] += 1
                    if created and (oldest is None or created < oldest):
                        oldest = created
                    yield {
                        "kind": "reddit.item",
                        "listing": listing,
                        "id": name,
                        "account": self.account,
                        "source": self.config.transport,
                        "observed_at": observed,
                        "raw": child,
                    }
                if end is None and after is None:
                    end = "end"
                elif end is None and pages >= config.max_pages:
                    end = "max_pages"
        except BudgetExhausted:
            st["resume"] = {"after": after, "stop": sorted(stop), "head": head}
            counts["end"] = "truncated"
            raise
        counts["end"] = end
        previous = parse_time(st["completed_at"]) if st.get("completed_at") else None
        if end == "max_pages" and stop and previous:
            # The head was not reached within max_pages: items between the
            # last complete pass and the oldest one read now are lost (the
            # next pass starts from the top again). Saved and voted lists
            # are ordered by an action time Reddit does not expose, so their
            # gap runs to now.
            to = self.client.now()
            if listing in DATE_ORDERED and oldest:
                to = oldest
            if to > previous:
                counts["gap"] = {"from": iso(previous), "to": iso(to)}
        st.pop("resume", None)
        merged = list(dict.fromkeys(head + old_head))[:HEAD_SIZE]
        if merged:
            st["head"] = merged
        st["completed_at"] = iso(self.client.now())


def write_json_atomic(path, value):
    temporary = path + ".part"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True, indent=1)
        handle.write("\n")
    os.replace(temporary, path)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="fetch.py",
        description="Export Reddit account listings as JSONL. "
        "Credentials are read only from REDDIT_* environment variables.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="fetch and write raw JSONL records")
    export.add_argument(
        "--out", required=True, help="output JSONL file, or - for stdout"
    )
    export.add_argument("--append", action="store_true", help="append to --out")
    export.add_argument("--state", help="JSON cursor file, read and then updated")
    export.add_argument(
        "--full",
        action="store_true",
        help="backfill: ignore known heads and page each listing to its bounds",
    )
    flags = {
        "listings": "comma list of " + ", ".join(USER_LISTINGS) + ", or none",
        "transport": "oauth (default) or session (signed-in browser, needs browser.py)",
        "session_profile": "persistent browser profile directory for the session transport",
        "since": "lower bound on item creation time (ISO date or time)",
        "until": "upper bound on item creation time (ISO date or time)",
        "max_pages": "pages per listing per run (default 10)",
        "page_size": "items per page, 1..100 (default 100)",
        "max_requests": "HTTP requests per run (default 200)",
        "max_requests_per_day": "HTTP requests per UTC day, kept in --state (default 2000)",
        "min_interval": "seconds between Reddit requests (default 1)",
        "retries": "retries after 429, 5xx and transport errors (default 5)",
        "max_wait": "longest single wait in seconds before stopping (default 300)",
        "timeout": "seconds per request (default 30)",
        "api_base": "OAuth API base URL",
        "auth_base": "token endpoint base URL",
    }
    for name, help_text in flags.items():
        export.add_argument(
            "--" + name.replace("_", "-"),
            dest=name,
            help=f"{help_text}; env {SETTINGS[name][0]}",
        )
    return parser


def main(argv=None, environ=None):
    environ = os.environ if environ is None else environ
    args = build_parser().parse_args(argv)
    overrides = {
        name: getattr(args, name, None) for name in SETTINGS if hasattr(args, name)
    }
    overrides["full"] = args.full
    try:
        config = Config.from_env(environ, overrides)
        if not config.user_agent:
            raise FetchError(
                "set REDDIT_USER_AGENT, e.g. '<platform>:<app id>:<version> (by /u/<name>)'"
            )
        credentials = Credentials.from_env(environ)
        state = {}
        if args.state and os.path.exists(args.state):
            try:
                with open(args.state, encoding="utf-8") as handle:
                    state = json.load(handle)
            except (OSError, ValueError):
                raise FetchError("--state file cannot be read as JSON") from None
        exporter = Exporter(config, credentials, state)
    except FetchError as error:
        print(f"fetch.py: {error}", file=sys.stderr)
        return 2
    to_stdout = args.out == "-"
    target = args.out if args.append or to_stdout else args.out + ".part"
    handle = (
        sys.stdout
        if to_stdout
        else open(target, "a" if args.append else "w", encoding="utf-8", newline="\n")
    )
    failure = None
    summary = None
    try:
        for record in exporter.records():
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            if record["kind"] == "reddit.export_summary":
                summary = record
    except FetchError as error:
        failure = str(error)
        summary = {
            "kind": "reddit.export_summary",
            "fetcher_version": FETCHER_VERSION,
            "error": failure,
            "requests": exporter.client.requests,
        }
        handle.write(json.dumps(summary, sort_keys=True) + "\n")
    finally:
        if not to_stdout:
            handle.close()
    if not to_stdout and not args.append:
        os.replace(target, args.out)
    if args.state:
        if failure is None:
            # The cursor advances only after a run without a fatal error.
            write_json_atomic(args.state, exporter.state)
        elif "requests" in exporter.state:
            # A failed run still spent requests: keep the per-day count.
            kept = copy.deepcopy(state)
            kept["requests"] = exporter.state["requests"]
            write_json_atomic(args.state, kept)
    shown = {
        k: summary.get(k)
        for k in ("requests", "listings", "skipped", "truncated", "error")
        if summary and k in summary
    }
    print(json.dumps(shown, sort_keys=True), file=sys.stderr)
    if failure is not None:
        print(f"fetch.py: {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
