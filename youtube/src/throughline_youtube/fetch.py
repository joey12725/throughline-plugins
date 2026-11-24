#!/usr/bin/env python3
"""Standard-library YouTube history, Watch Later and subscriptions watcher.

    python fetch.py poll --state STATE.json --out FILE.jsonl.gz [--full]

Reads the owner's own YouTube watch history (``FEhistory``), Watch Later
(``VLWL``) and subscriptions (``FEchannels``) through YouTube's web client API
(InnerTube) with the owner's browser cookies, compares each poll with the
previous one and writes raw records as JSONL (header, records, footer; the same
export pattern as the other connectors):

- ``history_item``: a video under a history day heading ("Today",
  "Yesterday", "Monday", "Nov 3", "Nov 3, 2024"), id ``<video>:<date>``. The
  history page shows a video once, under the day it was last watched; a
  video that moves to a later day yields a new item, so the union of items
  over time is one record per video per viewing day. ``progress`` is the
  resume bar the first time the item was seen (pinned in the state).
- ``session``: one viewing session found by diffing two polls: the video is
  new at the top of the feed, moved above videos that were ahead of it, or
  its resume bar changed. Fields: progress before and after, fraction and
  estimated seconds watched, the poll window and an estimated start and end.
  Watching a video in parts across polls yields one session per part.
- ``watch_later``: every Watch Later entry (playlist item id ``setVideoId``)
  with its first-seen time; entries gone from a complete snapshot are kept
  with ``removed=true`` and ``removed_seen``.
- ``subscription``: every subscribed channel (``channel_id``) with its
  first-seen time; channels gone from a complete snapshot are kept with
  ``removed=true`` and ``removed_seen``.
- ``poll``: counts and timings of the poll (reported only).

The first poll is a baseline: it records history items, Watch Later and
subscriptions but no sessions. ``--full`` pages through the whole history
(backfill); normal polls read the first ``--history-pages`` pages (default 3,
about 60 to 150 videos), enough for a poll every 10 to 30 minutes.

Cookies are read in place at run time and are never printed, logged or
written anywhere: ``YOUTUBE_COOKIES_FILE`` (Netscape cookies.txt),
``YOUTUBE_COOKIES_FIREFOX`` (a Firefox profile directory or its
cookies.sqlite, opened read-only), ``YOUTUBE_COOKIES_CHROMIUM`` (a Linux
Chromium profile directory or its Cookies database, opened read-only and
decrypted in memory using the OS keyring and OpenSSL libcrypto),
``YOUTUBE_COOKIES_BROWSER`` (a yt-dlp browser selector, decrypted in memory by
an installed yt-dlp; optional, not stdlib) or ``YOUTUBE_COOKIE_HEADER`` (a raw
Cookie header value in the environment). Only the standard library is used
(Python 3.12 to 3.14), so the file can be copied alone to the machine that
holds the browser profile.

This is the unofficial web API, not the YouTube Data API (which cannot read
history or Watch Later). It can change without notice; a response that
cannot be parsed fails the poll without touching the state.
"""

import argparse
import copy
import ctypes
import ctypes.util
import gzip
import hashlib
import http.cookiejar
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

FORMAT = "throughline-youtube-watcher-export"
VERSION = 1
ORIGIN = "https://www.youtube.com"
BROWSE = ORIGIN + "/youtubei/v1/browse?prettyPrint=false"
# Fallback when the home page does not reveal the current web client version.
CLIENT_VERSION = "2.20261101.00.00"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
DURATION = re.compile(r"^\d{1,3}(:\d{2}){1,2}$")
MONTHS = {
    m: i + 1
    for i, names in enumerate(
        [
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ]
    )
    for m in names
}
WEEKDAYS = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]
# Resume-bar changes below this many percentage points are rounding noise.
TOLERANCE = 1.0
# Previous history order kept in the state (videos).
KEEP = 400


class FetchError(Exception):
    """A failure whose message never contains cookies or response bodies."""


# ---------------------------------------------------------------- utilities


def utcnow():
    return datetime.now(UTC).replace(microsecond=0)


def iso(moment):
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_iso(value):
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def text(node):
    """Plain text of an InnerTube text object (simpleText, runs or content)."""
    if node is None:
        return None
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if "simpleText" in node:
            return node["simpleText"]
        if "content" in node and isinstance(node["content"], str):
            return node["content"]
        if "runs" in node:
            return "".join(r.get("text", "") for r in node["runs"]) or None
    return None


def seconds(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    value = str(value).strip()
    if value.isdigit():
        return int(value)
    if not DURATION.match(value):
        return None
    total = 0
    for part in value.split(":"):
        total = total * 60 + int(part)
    return total


def walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk(value)


def zone(name):
    if not name or name.upper() == "UTC" or ZoneInfo is None:
        return UTC
    try:
        return ZoneInfo(name)
    except Exception:
        raise FetchError("Unknown YOUTUBE_TZ time zone") from None


def day_of(label, today):
    """The calendar date of a history section heading, or None.

    Handles English headings: Today, Yesterday, a weekday of the past week,
    "Nov 3" (this year, or last year when that date is still ahead) and
    "Nov 3, 2024". Request hl=en so headings are English.
    """
    if not label:
        return None
    low = label.strip().lower().replace(",", " ")
    words = low.split()
    if low == "today":
        return today
    if low == "yesterday":
        return today - timedelta(days=1)
    if low in WEEKDAYS:
        back = (today.weekday() - WEEKDAYS.index(low)) % 7 or 7
        return today - timedelta(days=back)
    try:
        if len(words) >= 2 and words[0] in MONTHS:
            month, day = MONTHS[words[0]], int(words[1])
            year = int(words[2]) if len(words) > 2 else today.year
        elif len(words) >= 2 and words[1] in MONTHS:  # "3 Nov 2024"
            month, day = MONTHS[words[1]], int(words[0])
            year = int(words[2]) if len(words) > 2 else today.year
        else:
            return None
        found = date(year, month, day)
    except (ValueError, IndexError):
        return None
    if len(words) == 2 and found > today:
        found = date(year - 1, month, day)
    return found


# ---------------------------------------------------------------- cookies


def _netscape(path):
    jar = http.cookiejar.MozillaCookieJar()
    try:
        jar.load(path, ignore_discard=True, ignore_expires=True)
    except (OSError, http.cookiejar.LoadError):
        raise FetchError("YOUTUBE_COOKIES_FILE is not a readable cookies.txt") from None
    return {c.name: c.value for c in jar if c.domain.endswith("youtube.com")}


def _firefox(path):
    path = Path(path)
    if path.is_dir():
        path = path / "cookies.sqlite"
    if not path.is_file():
        raise FetchError("YOUTUBE_COOKIES_FIREFOX has no cookies.sqlite")
    # Read in place without taking a lock or copying the secrets anywhere:
    # immutable read-only mode (changes still in the browser's WAL are missed
    # until it checkpoints, which is fine for long-lived session cookies).
    try:
        con = sqlite3.connect(
            path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
        )
        try:
            rows = con.execute(
                "select name, value from moz_cookies where host like '%youtube.com'"
                " order by expiry"
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        raise FetchError("Firefox cookie database could not be read") from None
    return dict(rows)


def _browser(selector):
    """Cookies decrypted in memory by an installed yt-dlp (optional, not stdlib).

    `selector` is yt-dlp's BROWSER[+KEYRING][:PROFILE][::CONTAINER], e.g.
    `chrome`, `chromium:/home/me/.config/chromium/Profile 1`. Nothing is
    written to disk. A running Chromium on Windows locks its database and
    newer Chrome encrypts cookies so that only the browser can read them.
    """
    try:
        from yt_dlp.cookies import extract_cookies_from_browser
    except ImportError:
        raise FetchError("YOUTUBE_COOKIES_BROWSER needs yt-dlp importable") from None
    name, _, rest = selector.partition(":")
    browser, _, keyring = name.partition("+")
    profile, _, container = rest.partition("::")

    class Quiet:
        def debug(self, *a, **k):
            pass

        info = warning = error = debug

        def progress_bar(self):
            return None

    try:
        jar = extract_cookies_from_browser(
            browser,
            profile or None,
            Quiet(),
            keyring=keyring.upper() or None,
            container=container or None,
        )
    except Exception as error:
        raise FetchError(
            f"Browser cookies could not be read ({type(error).__name__})"
        ) from None
    return {c.name: c.value for c in jar if c.domain.endswith("youtube.com")}


def _header(value):
    out = {}
    for part in value.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _aes128_cbc_decrypt(key, iv, ciphertext):
    """Decrypt AES-128-CBC (PKCS#7 padded) in memory via OpenSSL libcrypto."""
    libname = ctypes.util.find_library("crypto") or "libcrypto.so.3"
    try:
        lib = ctypes.CDLL(libname)
    except OSError:
        raise FetchError("OpenSSL libcrypto is unavailable for Chromium cookie decryption") from None
    lib.EVP_CIPHER_CTX_new.restype = ctypes.c_void_p
    lib.EVP_aes_128_cbc.restype = ctypes.c_void_p
    lib.EVP_DecryptInit_ex.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_char_p,
    ]
    lib.EVP_DecryptUpdate.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_int),
        ctypes.c_char_p,
        ctypes.c_int,
    ]
    lib.EVP_DecryptFinal_ex.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_int),
    ]
    lib.EVP_CIPHER_CTX_free.argtypes = [ctypes.c_void_p]
    ctx = lib.EVP_CIPHER_CTX_new()
    if not ctx:
        raise FetchError("Could not initialize OpenSSL decryption context")
    try:
        if lib.EVP_DecryptInit_ex(ctx, lib.EVP_aes_128_cbc(), None, key, iv) != 1:
            raise FetchError("Could not initialize AES-128-CBC decryption")
        out = ctypes.create_string_buffer(len(ciphertext) + 16)
        n1 = ctypes.c_int(0)
        if (
            lib.EVP_DecryptUpdate(
                ctx, out, ctypes.byref(n1), ciphertext, len(ciphertext)
            )
            != 1
        ):
            raise FetchError("Could not decrypt Chromium cookie payload")
        tail = ctypes.create_string_buffer(32)
        n2 = ctypes.c_int(0)
        if lib.EVP_DecryptFinal_ex(ctx, tail, ctypes.byref(n2)) != 1:
            raise FetchError("Chromium cookie decryption failed (wrong keyring key?)")
        return out.raw[: n1.value] + tail.raw[: n2.value]
    finally:
        lib.EVP_CIPHER_CTX_free(ctx)


def _chromium_keyring_password(app):
    """Look up Chromium's Safe Storage password via libsecret's secret-tool."""
    try:
        result = subprocess.run(
            ["secret-tool", "lookup", "application", app],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise FetchError("secret-tool is unavailable to unlock Chromium cookies") from None
    pw = result.stdout.strip()
    if result.returncode != 0 or not pw:
        raise FetchError(
            f"Chromium keyring password not found for application={app}"
        )
    return pw


def _chromium(path, keyring_app="chromium", password=None):
    """Read Linux Chromium Cookies DB in place and decrypt in memory."""
    path = Path(path).expanduser()
    if path.is_dir():
        path = path / "Cookies"
    if not path.is_file():
        raise FetchError("YOUTUBE_COOKIES_CHROMIUM has no Cookies database")
    try:
        con = sqlite3.connect(
            path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
        )
        try:
            rows = con.execute(
                "select host_key, name, encrypted_value, value from cookies"
                " where host_key like '%youtube.com' order by expires_utc"
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        raise FetchError("Chromium cookie database could not be read") from None
    keys = {}
    cookies = {}
    for host, name, enc, val in rows:
        if val:
            cookies[name] = val
            continue
        if not enc or len(enc) <= 3:
            continue
        prefix = bytes(enc[:3])
        if prefix not in (b"v10", b"v11"):
            continue
        if prefix not in keys:
            pw = (
                b"peanuts"
                if prefix == b"v10"
                else (
                    password
                    if password is not None
                    else _chromium_keyring_password(keyring_app)
                )
            )
            keys[prefix] = hashlib.pbkdf2_hmac("sha1", pw, b"saltysalt", 1, 16)
        pt = _aes128_cbc_decrypt(keys[prefix], b" " * 16, bytes(enc[3:]))
        # Chrome >= 130 prepends SHA256(host_key) to the cookie value.
        if len(pt) >= 32 and pt[:32] == hashlib.sha256(host.encode()).digest():
            pt = pt[32:]
        try:
            cookies[name] = pt.decode("utf-8", "strict")
        except UnicodeDecodeError:
            raise FetchError("Chromium cookie decoded to invalid UTF-8") from None
    return cookies


def load_cookies(env=None):
    env = os.environ if env is None else env
    if env.get("YOUTUBE_COOKIE_HEADER"):
        cookies = _header(env["YOUTUBE_COOKIE_HEADER"])
    elif env.get("YOUTUBE_COOKIES_FILE"):
        cookies = _netscape(env["YOUTUBE_COOKIES_FILE"])
    elif env.get("YOUTUBE_COOKIES_FIREFOX"):
        cookies = _firefox(env["YOUTUBE_COOKIES_FIREFOX"])
    elif env.get("YOUTUBE_COOKIES_CHROMIUM"):
        cookies = _chromium(
            env["YOUTUBE_COOKIES_CHROMIUM"],
            env.get("YOUTUBE_CHROMIUM_KEYRING_APP") or "chromium",
        )
    elif env.get("YOUTUBE_COOKIES_BROWSER"):
        cookies = _browser(env["YOUTUBE_COOKIES_BROWSER"])
    else:
        raise FetchError(
            "No cookies: set YOUTUBE_COOKIES_FILE, YOUTUBE_COOKIES_FIREFOX, "
            "YOUTUBE_COOKIES_CHROMIUM, YOUTUBE_COOKIES_BROWSER or YOUTUBE_COOKIE_HEADER"
        )
    if not (cookies.get("SAPISID") or cookies.get("__Secure-3PAPISID")):
        raise FetchError("Cookies are not a signed-in YouTube session (no SAPISID)")
    return cookies


def authorization(cookies, now=None):
    """The SAPISIDHASH header YouTube's web client sends (no secret is logged)."""
    stamp = str(int(now if now is not None else time.time()))
    parts = []
    for scheme, name in (
        ("SAPISIDHASH", "SAPISID"),
        ("SAPISID1PHASH", "__Secure-1PAPISID"),
        ("SAPISID3PHASH", "__Secure-3PAPISID"),
    ):
        value = cookies.get(name) or (
            cookies.get("__Secure-3PAPISID") if name == "SAPISID" else None
        )
        if value:
            digest = hashlib.sha1(f"{stamp} {value} {ORIGIN}".encode()).hexdigest()
            parts.append(f"{scheme} {stamp}_{digest}")
    return " ".join(parts)


# ---------------------------------------------------------------- transport


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class InnerTube:
    """Minimal signed-in web client. `opener` is a test seam."""

    def __init__(
        self, cookies, tz="UTC", min_interval=1.0, opener=None, sleep=time.sleep
    ):
        self.cookies = cookies
        self.tz = tz
        self.min_interval = min_interval
        self.opener = opener or urllib.request.build_opener(NoRedirect())
        self.sleep = sleep
        self.version = None
        self.requests = 0
        self.last = 0.0

    def _cookie_header(self):
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())

    def _send(self, req, attempts=4):
        for attempt in range(attempts):
            wait = self.min_interval - (time.monotonic() - self.last)
            if wait > 0:
                self.sleep(wait)
            self.last = time.monotonic()
            self.requests += 1
            try:
                with self.opener.open(req, timeout=60) as response:
                    return response.read()
            except urllib.error.HTTPError as error:
                code = error.code
                if code in (401, 403):
                    raise FetchError(
                        f"YouTube refused the session (HTTP {code}); refresh cookies"
                    ) from None
                if code != 429 and code < 500 or attempt == attempts - 1:
                    raise FetchError(f"YouTube request failed (HTTP {code})") from None
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt == attempts - 1:
                    raise FetchError("YouTube connection failed") from None
            self.sleep(min(5 * 2**attempt, 60))
        raise FetchError("YouTube request failed")

    def client_version(self):
        if self.version:
            return self.version
        req = urllib.request.Request(
            ORIGIN + "/?hl=en",
            headers={"User-Agent": USER_AGENT, "Cookie": self._cookie_header()},
        )
        try:
            page = self._send(req, attempts=2).decode("utf-8", "replace")
            found = re.search(r'"INNERTUBE_CLIENT_VERSION":"([0-9.]+)"', page)
            if '"LOGGED_IN":false' in page:
                raise FetchError("Cookies are signed out of YouTube; refresh them")
            self.version = found.group(1) if found else CLIENT_VERSION
        except FetchError as error:
            if "signed out" in str(error):
                raise
            self.version = CLIENT_VERSION
        return self.version

    def browse(self, browse_id=None, continuation=None):
        version = self.client_version()
        body = {
            "context": {
                "client": {
                    "clientName": "WEB",
                    "clientVersion": version,
                    "hl": "en",
                    "gl": "US",
                    "timeZone": self.tz,
                }
            }
        }
        if continuation:
            body["continuation"] = continuation
        else:
            body["browseId"] = browse_id
        req = urllib.request.Request(
            BROWSE,
            data=json.dumps(body).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
                "Cookie": self._cookie_header(),
                "Authorization": authorization(self.cookies),
                "Origin": ORIGIN,
                "X-Origin": ORIGIN,
                "X-Goog-AuthUser": "0",
                "X-Youtube-Client-Name": "1",
                "X-Youtube-Client-Version": version,
            },
        )
        try:
            data = json.loads(self._send(req))
        except ValueError:
            raise FetchError("YouTube returned malformed JSON") from None
        context = (data.get("responseContext") or {}).get(
            "mainAppWebResponseContext"
        ) or {}
        if context.get("loggedOut"):
            raise FetchError(
                "YouTube treated the request as signed out; refresh cookies"
            )
        return data


# ---------------------------------------------------------------- parsing


def _progress(node):
    for item in walk(node):
        if "percentDurationWatched" in item:
            return float(item["percentDurationWatched"])
        if "startPercent" in item and isinstance(item["startPercent"], (int, float)):
            return float(item["startPercent"])
    return None


def _duration(node):
    for item in walk(node):
        for key in ("lengthText", "text"):
            value = text(item.get(key)) if key in item else None
            if value and DURATION.match(value.strip()):
                return seconds(value.strip())
    return None


def _channel(node):
    for item in walk(node):
        endpoint = item.get("browseEndpoint")
        if isinstance(endpoint, dict) and str(endpoint.get("browseId", "")).startswith(
            "UC"
        ):
            return endpoint["browseId"]
    return None


def video_renderer(r, short=False):
    owner = r.get("ownerText") or r.get("shortBylineText") or r.get("longBylineText")
    return {
        "video_id": r.get("videoId"),
        "title": text(r.get("title")) or text(r.get("headline")),
        "channel": text(owner),
        "channel_id": _channel(owner) if owner else None,
        "length_s": seconds(r.get("lengthSeconds"))
        or seconds(text(r.get("lengthText"))),
        "progress": _progress(r.get("thumbnailOverlays")),
        "is_short": short or None,
        "set_video_id": r.get("setVideoId"),
        "index": seconds(text(r.get("index"))),
    }


def lockup(r):
    meta = (r.get("metadata") or {}).get("lockupMetadataViewModel") or {}
    rows = (
        ((meta.get("metadata") or {}).get("contentMetadataViewModel") or {}).get(
            "metadataRows"
        )
    ) or []
    channel = None
    for row in rows:
        for part in row.get("metadataParts") or []:
            channel = text(part.get("text"))
            break
        if channel:
            break
    image = r.get("contentImage")
    return {
        "video_id": r.get("contentId"),
        "title": text(meta.get("title")),
        "channel": channel,
        "channel_id": _channel(meta),
        "length_s": _duration(image),
        "progress": _progress(image),
        "is_short": None,
        "set_video_id": None,
        "index": None,
    }


def short_lockup(r):
    vid = None
    for item in walk(r.get("onTap") or r):
        endpoint = item.get("reelWatchEndpoint")
        if isinstance(endpoint, dict) and endpoint.get("videoId"):
            vid = endpoint["videoId"]
            break
    return {
        "video_id": vid,
        "title": text(((r.get("overlayMetadata") or {}).get("primaryText"))),
        "channel": None,
        "channel_id": None,
        "length_s": None,
        "progress": None,
        "is_short": True,
        "set_video_id": None,
        "index": None,
    }


def parse_page(data):
    """(items with their section heading, continuation token) of one response."""
    items = []
    heading = [None]
    found = [None]

    def visit(node, short=False):
        if isinstance(node, list):
            for value in node:
                visit(value, short)
            return
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            if key == "itemSectionRenderer":
                header = (value.get("header") or {}).get(
                    "itemSectionHeaderRenderer"
                ) or {}
                title = text(header.get("title"))
                if title:
                    heading[0] = title
                visit(value.get("contents"), short)
            elif key in ("videoRenderer", "playlistVideoRenderer", "reelItemRenderer"):
                row = video_renderer(value, short or key == "reelItemRenderer")
                if row["video_id"]:
                    row["section"] = heading[0]
                    items.append(row)
            elif key == "lockupViewModel":
                if (
                    value.get("contentType", "LOCKUP_CONTENT_TYPE_VIDEO")
                    == "LOCKUP_CONTENT_TYPE_VIDEO"
                ):
                    row = lockup(value)
                    if row["video_id"]:
                        row["section"] = heading[0]
                        items.append(row)
            elif key == "shortsLockupViewModel":
                row = short_lockup(value)
                if row["video_id"]:
                    row["section"] = heading[0]
                    items.append(row)
            elif key == "reelShelfRenderer":
                visit(value.get("items"), True)
            elif key == "continuationItemRenderer":
                for item in walk(value):
                    command = item.get("continuationCommand")
                    if isinstance(command, dict) and command.get("token"):
                        found[0] = command["token"]
                        break
            elif isinstance(value, (dict, list)):
                visit(value, short)

    visit(data)
    items = [i for i in items if VIDEO_ID.match(i["video_id"] or "")]
    return items, found[0]


def feed(client, browse_id, pages, log=None):
    """All items of a feed up to `pages` pages (0: no limit) and whether complete."""
    data = client.browse(browse_id=browse_id)
    items, token = parse_page(data)
    if not items and token is None and not _looks_like_feed(data):
        raise FetchError(
            f"Unrecognized {browse_id} response; the web API may have changed"
        )
    seen = set()
    count = 1
    while token and (pages == 0 or count < pages):
        if token in seen:
            raise FetchError("Repeated continuation token")
        seen.add(token)
        more, token = parse_page(client.browse(continuation=token))
        items.extend(more)
        count += 1
        if log and count % 25 == 0:
            log(f"{browse_id}: {count} pages, {len(items)} items")
    return items, token is None, count


def parse_channels_page(data):
    """(subscribed channel items, continuation token) of one FEchannels response."""
    items = []
    seen_ids = set()
    found = [None]

    def visit(node):
        if isinstance(node, list):
            for value in node:
                visit(value)
            return
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            if key == "channelRenderer" and isinstance(value, dict):
                cid = value.get("channelId")
                if cid and str(cid).startswith("UC") and cid not in seen_ids:
                    seen_ids.add(cid)
                    items.append(
                        {
                            "channel_id": cid,
                            "channel": text(value.get("title")),
                            "url": f"{ORIGIN}/channel/{cid}",
                        }
                    )
            elif key == "continuationItemRenderer":
                for item in walk(value):
                    command = item.get("continuationCommand")
                    if isinstance(command, dict) and command.get("token"):
                        found[0] = command["token"]
                        break
            elif isinstance(value, (dict, list)):
                visit(value)

    visit(data)
    return items, found[0]


def channels_feed(client, pages=0, log=None):
    """All subscribed channels from FEchannels up to `pages` pages (0: no limit)."""
    data = client.browse(browse_id="FEchannels")
    items, token = parse_channels_page(data)
    if not items and token is None and not _looks_like_feed(data):
        raise FetchError(
            "Unrecognized FEchannels response; the web API may have changed"
        )
    seen = set()
    count = 1
    while token and (pages == 0 or count < pages):
        if token in seen:
            raise FetchError("Repeated continuation token")
        seen.add(token)
        more, token = parse_channels_page(client.browse(continuation=token))
        items.extend(more)
        count += 1
        if log and count % 25 == 0:
            log(f"FEchannels: {count} pages, {len(items)} items")
    return items, token is None, count


def _looks_like_feed(data):
    return bool(data.get("contents") or data.get("onResponseReceivedActions"))


# ---------------------------------------------------------------- diffing


def record(kind, ident, raw, now):
    return {"kind": kind, "id": ident, "fetched_at": iso(now), "raw": raw}


def public(row):
    return {
        k: row.get(k)
        for k in ("video_id", "title", "channel", "channel_id", "length_s", "is_short")
        if row.get(k) is not None
    }


def history_records(items, today, state, now):
    """history_item records; progress is pinned to the first sighting."""
    pinned = state.setdefault("items", {})
    out, seen = [], set()
    for rank, row in enumerate(items):
        day = day_of(row.get("section"), today)
        ident = f"{row['video_id']}:{day.isoformat() if day else 'undated'}"
        if ident in seen:
            continue
        seen.add(ident)
        first = pinned.get(ident)
        if first is None:
            first = pinned[ident] = {
                "first_seen": iso(now),
                "progress": row.get("progress"),
            }
        raw = public(row) | {
            "day": day.isoformat() if day else None,
            "progress": first["progress"],
            "first_seen": first["first_seen"],
        }
        out.append(record("history_item", ident, raw, now))
    return out


def sessions(items, state, now, today):
    """Viewing sessions between the previous poll and this one."""
    previous = state.get("history")
    current, ranks = [], {}
    for row in items:
        if row["video_id"] not in ranks:
            ranks[row["video_id"]] = len(current)
            current.append(row)
    if not previous:
        return []
    old_rank = {vid: i for i, (vid, _p) in enumerate(previous["order"])}
    old_progress = {vid: p for vid, p in previous["order"]}
    known = state.setdefault("progress", {})
    window_start = parse_iso(previous["at"])
    # suffix[i]: smallest old rank among current[i:].
    suffix = [float("inf")] * (len(current) + 1)
    for i in range(len(current) - 1, -1, -1):
        suffix[i] = min(
            suffix[i + 1], old_rank.get(current[i]["video_id"], float("inf"))
        )
    active = []
    for i, row in enumerate(current):
        vid = row["video_id"]
        behind = suffix[i + 1]
        if vid in old_rank:
            moved = old_rank[vid] > behind
            before = old_progress.get(vid)
            after = row.get("progress")
            changed = (
                before is not None
                and after is not None
                and abs(after - before) >= TOLERANCE
            ) or (before is None) != (after is None)
            if not (moved or changed):
                continue
            kind = (
                "rewatch"
                if moved
                and before is not None
                and after is not None
                and after + TOLERANCE < before
                else ("resume" if before else "progress")
            )
        else:
            if behind == float("inf"):
                continue  # Deeper than the previous poll looked: not new activity.
            day = day_of(row.get("section"), today)
            if day and window_start and day < window_start.date() - timedelta(days=1):
                continue  # An old view newly exposed, not a watch in this window.
            before = known.get(vid)
            after = row.get("progress")
            kind = "new" if before is None else "rewatch"
        if before is not None and after is not None:
            if after + TOLERANCE < before:
                fraction = after / 100  # Restarted: at least the new position.
                kind = "rewatch"
            else:
                fraction = max(after - before, 0) / 100
        elif after is not None:
            fraction = after / 100
        else:
            fraction = None
        length = row.get("length_s")
        active.append(
            dict(
                row=row,
                kind=kind,
                before=before,
                after=after,
                fraction=round(fraction, 4) if fraction is not None else None,
                watched_s=round(fraction * length)
                if fraction is not None and length
                else None,
                rank=i,
            )
        )
    # Back to back from the poll time, most recent (top of the feed) first.
    cursor = now
    out = []
    counter = state.setdefault("session_counts", {})
    for s in active:
        end = cursor
        span = timedelta(seconds=s["watched_s"] or 0)
        start = max(window_start, end - span) if window_start else end - span
        cursor = start
        vid = s["row"]["video_id"]
        counter[vid] = counter.get(vid, 0) + 1
        raw = public(s["row"]) | {
            "session_kind": s["kind"],
            "progress_before": s["before"],
            "progress_after": s["after"],
            "watched_fraction": s["fraction"],
            "watched_s": s["watched_s"],
            "window_start": iso(window_start) if window_start else None,
            "window_end": iso(now),
            "start_est": iso(start),
            "end_est": iso(end),
            "feed_rank": s["rank"],
            "session_number": counter[vid],
        }
        day = day_of(s["row"].get("section"), today)
        raw["day"] = day.isoformat() if day else None
        out.append(record("session", f"{vid}:{iso(now)}", raw, now))
    return out


def watch_later_records(items, complete, state, now):
    wl = state.setdefault("watch_later", {})
    out, current = [], set()
    occurrences = {}
    for row in items:
        vid = row["video_id"]
        occurrences[vid] = occurrences.get(vid, 0) + 1
        ident = row.get("set_video_id") or f"{vid}:{occurrences[vid]}"
        current.add(ident)
        entry = wl.get(ident)
        if entry is None or entry.get("removed_seen"):
            entry = wl[ident] = {"video_id": vid, "first_seen": iso(now)}
        entry["meta"] = public(row)
        out.append(
            record(
                "watch_later",
                ident,
                public(row)
                | {
                    "first_seen": entry["first_seen"],
                    "removed": False,
                    "removed_seen": None,
                },
                now,
            )
        )
    if complete:
        for ident, entry in sorted(wl.items()):
            if ident in current:
                continue
            entry.setdefault("removed_seen", iso(now))
            out.append(
                record(
                    "watch_later",
                    ident,
                    (entry.get("meta") or {"video_id": entry["video_id"]})
                    | {
                        "first_seen": entry["first_seen"],
                        "removed": True,
                        "removed_seen": entry["removed_seen"],
                    },
                    now,
                )
            )
    return out


def subscription_records(items, complete, state, now):
    subs = state.setdefault("subscriptions", {})
    out, current = [], set()
    for row in items:
        cid = row["channel_id"]
        if cid in current:
            continue
        current.add(cid)
        entry = subs.get(cid)
        if entry is None or entry.get("removed_seen"):
            entry = subs[cid] = {"channel_id": cid, "first_seen": iso(now)}
        meta = {
            "channel_id": cid,
            "channel": row.get("channel"),
            "url": row.get("url") or f"{ORIGIN}/channel/{cid}",
        }
        entry["meta"] = meta
        out.append(
            record(
                "subscription",
                cid,
                meta
                | {
                    "first_seen": entry["first_seen"],
                    "removed": False,
                    "removed_seen": None,
                },
                now,
            )
        )
    if complete:
        for cid, entry in sorted(subs.items()):
            if cid in current:
                continue
            entry.setdefault("removed_seen", iso(now))
            out.append(
                record(
                    "subscription",
                    cid,
                    (entry.get("meta") or {"channel_id": cid})
                    | {
                        "first_seen": entry["first_seen"],
                        "removed": True,
                        "removed_seen": entry["removed_seen"],
                    },
                    now,
                )
            )
    return out


def poll(
    client,
    state,
    now=None,
    tz="UTC",
    history_pages=3,
    full=False,
    watch_later=True,
    subscriptions=True,
    log=None,
):
    """One poll: (records, new state). The given state is not modified."""
    state = copy.deepcopy(state or {})
    now = now or utcnow()
    today = now.astimezone(zone(tz)).date()
    started = time.monotonic()
    items, complete, pages = feed(
        client, "FEhistory", 0 if full else history_pages, log
    )
    out = history_records(items, today, state, now)
    found = sessions(items, state, now, today)
    out.extend(found)
    order = []
    seen = set()
    for row in items:
        if row["video_id"] not in seen:
            seen.add(row["video_id"])
            order.append([row["video_id"], row.get("progress")])
    known = state.setdefault("progress", {})
    for vid, progress in order:
        if progress is not None:
            known[vid] = progress
    state["history"] = {"at": iso(now), "order": order[:KEEP]}
    wl_count = 0
    if watch_later:
        wl_items, wl_complete, _ = feed(client, "VLWL", 0, log)
        wl_records = watch_later_records(wl_items, wl_complete, state, now)
        wl_count = len(wl_items)
        out.extend(wl_records)
    sub_count = 0
    if subscriptions:
        sub_items, sub_complete, _ = channels_feed(client, 0, log)
        sub_records = subscription_records(sub_items, sub_complete, state, now)
        sub_count = len(sub_items)
        out.extend(sub_records)
    state["polls"] = state.get("polls", 0) + 1
    out.append(
        record(
            "poll",
            iso(now),
            {
                "history_items": len(items),
                "history_pages": pages,
                "history_complete": complete,
                "sessions": len(found),
                "watch_later_items": wl_count,
                "subscriptions": sub_count,
                "baseline": state["polls"] == 1,
                "requests": client.requests,
                "seconds": round(time.monotonic() - started, 1),
            },
            now,
        )
    )
    return out, state


# ---------------------------------------------------------------- export


def write_export(path, records, meta):
    counts = {}
    for r in records:
        counts[r["kind"]] = counts.get(r["kind"], 0) + 1
    lines = [{"kind": "export.header", "format": FORMAT, "version": VERSION, **meta}]
    lines.extend(records)
    lines.append({"kind": "export.footer", "complete": True, "counts": counts})
    payload = "".join(
        json.dumps(x, ensure_ascii=False, sort_keys=True) + "\n" for x in lines
    )
    if path == "-":
        sys.stdout.write(payload)
        return counts
    partial = path + ".partial"
    opener = gzip.open if path.endswith(".gz") else open
    with opener(partial, "wt", encoding="utf-8") as handle:
        handle.write(payload)
    os.replace(partial, path)
    return counts


def read_export(path):
    """Records of one export file; refuses a file without its footer."""
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as handle:
        lines = [json.loads(line) for line in handle if line.strip()]
    if not lines or lines[0].get("format") != FORMAT:
        raise ValueError(f"{path} is not a YouTube watcher export")
    if lines[-1].get("kind") != "export.footer" or not lines[-1].get("complete"):
        raise ValueError(f"{path} is incomplete (no footer)")
    return lines[0], lines[1:-1]


def load_state(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def save_state(path, state):
    tmp = str(path) + ".tmp"
    Path(tmp).write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("poll", help="poll history and Watch Later once")
    p.add_argument(
        "--state", required=True, help="watcher state JSON (kept between polls)"
    )
    p.add_argument("--out", required=True, help="export path (.jsonl, .jsonl.gz or -)")
    p.add_argument("--full", action="store_true", help="page through the whole history")
    p.add_argument(
        "--history-pages",
        type=int,
        default=int(os.environ.get("YOUTUBE_HISTORY_PAGES", "3")),
    )
    p.add_argument("--no-watch-later", action="store_true")
    p.add_argument("--no-subscriptions", action="store_true")
    p.add_argument(
        "--tz",
        default=os.environ.get("YOUTUBE_TZ", "UTC"),
        help="IANA zone of the account's history days",
    )
    p.add_argument(
        "--min-interval",
        type=float,
        default=float(os.environ.get("YOUTUBE_MIN_INTERVAL", "1.0")),
    )
    args = parser.parse_args(argv)
    try:
        cookies = load_cookies()
        client = InnerTube(cookies, tz=args.tz, min_interval=args.min_interval)
        state = load_state(args.state)
        now = utcnow()
        records, new_state = poll(
            client,
            state,
            now=now,
            tz=args.tz,
            history_pages=args.history_pages,
            full=args.full,
            watch_later=not args.no_watch_later,
            subscriptions=not args.no_subscriptions,
            log=lambda m: print(m, file=sys.stderr),
        )
        counts = write_export(
            args.out, records, {"polled_at": iso(now), "tz": args.tz, "full": args.full}
        )
        save_state(args.state, new_state)
    except FetchError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps({"counts": counts}), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
