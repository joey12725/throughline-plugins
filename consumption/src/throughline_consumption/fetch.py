#!/usr/bin/env python3
"""Standard-library fetcher for Throughline media consumption sources.

Reads Spotify (Web API or Composio), a Spotify JSON backup directory, Plex
watch history, Audiobookshelf listening sessions and progress, and YouTube
watch history (Takeout, or messages exported by the Throughline youtube
plugin), and writes raw records as JSONL:

    python fetch.py export --out consumption.jsonl [--state state.json]

Only the Python standard library is used, so this file runs on its own
(Python 3.12 to 3.14) on a host that holds the credentials. Credentials come
only from environment variables (see ENV); they are never accepted on the
command line, never written to the export or state file, and never printed.
The Throughline plugin imports this module for its live mode, so both modes
produce exactly the same records.
"""

import argparse
import copy
import gzip
import hashlib
import html
import io
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zipfile
import zlib
from collections import Counter
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

FORMAT = "throughline-consumption-export"
FORMAT_VERSION = 1
FETCHER_VERSION = "1.0"
BOOK_EXTRACTOR_VERSION = 1
EBOOK_EXTENSIONS = (".epub", ".txt", ".pdf")
SERVICES = ("spotify", "spotify_backup", "plex", "audiobookshelf", "youtube")
USER_AGENT = "throughline-consumption-fetch/" + FETCHER_VERSION

# key: (environment variable, default, secret, description). The plugin builds
# its manifest declarations from this table, so both layers share the names.
ENV = {
    "source": (
        "CONSUMPTION_SOURCE",
        "live",
        False,
        "live, or export:<file or directory of export JSONL files>",
    ),
    "services": (
        "CONSUMPTION_SERVICES",
        None,
        False,
        "Comma separated subset of "
        + ", ".join(SERVICES)
        + "; empty means all configured",
    ),
    "since": (
        "CONSUMPTION_SINCE",
        None,
        False,
        "Backfill lower bound (ISO date or timestamp, inclusive)",
    ),
    "until": (
        "CONSUMPTION_UNTIL",
        None,
        False,
        "Backfill upper bound (exclusive); a bounded run never advances cursors",
    ),
    "max_requests": (
        "CONSUMPTION_MAX_REQUESTS",
        "1000",
        False,
        "HTTP requests allowed per run (0 = unlimited)",
    ),
    "max_requests_per_day": (
        "CONSUMPTION_MAX_REQUESTS_PER_DAY",
        "5000",
        False,
        "HTTP requests allowed per UTC day, counted in the fetch state (0 = unlimited)",
    ),
    "delay_s": (
        "CONSUMPTION_REQUEST_DELAY_S",
        "0.25",
        False,
        "Minimum pause between HTTP requests in seconds",
    ),
    "max_items": (
        "CONSUMPTION_MAX_ITEMS",
        None,
        False,
        "Records exported per service per run (empty = unlimited)",
    ),
    "checkpoint_every": (
        "CONSUMPTION_CHECKPOINT_EVERY",
        "500",
        False,
        "Plugin checkpoint interval in records",
    ),
    "spotify_backup_dir": (
        "CONSUMPTION_SPOTIFY_BACKUP_DIR",
        None,
        False,
        "Directory of Spotify JSON backups (liked songs, playlists)",
    ),
    "spotify_playlists": (
        "CONSUMPTION_SPOTIFY_PLAYLISTS",
        "1",
        False,
        "1 to read playlist entries from the Spotify API, 0 to skip",
    ),
    "spotify_api_base": (
        "CONSUMPTION_SPOTIFY_API_BASE",
        "https://api.spotify.com/v1/",
        False,
        "Spotify Web API base URL",
    ),
    "spotify_token_url": (
        "CONSUMPTION_SPOTIFY_TOKEN_URL",
        "https://accounts.spotify.com/api/token",
        False,
        "Spotify token endpoint",
    ),
    "composio_base": (
        "COMPOSIO_BASE_URL",
        "https://backend.composio.dev/api/v3/",
        False,
        "Composio REST API base URL",
    ),
    "spotify_composio_account": (
        "SPOTIFY_COMPOSIO_ACCOUNT_ID",
        None,
        False,
        "Composio connected account ID of the Spotify connection",
    ),
    "composio_user_id": (
        "COMPOSIO_USER_ID",
        None,
        False,
        "Optional Composio user ID sent with tool executions",
    ),
    "composio_tools": (
        "SPOTIFY_COMPOSIO_TOOLS",
        None,
        False,
        "Optional JSON object overriding Composio tool slugs (recent, saved, playlists, playlist_items)",
    ),
    "composio_mcp_url": (
        "SPOTIFY_COMPOSIO_MCP_URL",
        "https://connect.composio.dev/mcp",
        False,
        "Composio MCP endpoint used for near-real-time Spotify recently played",
    ),
    "spotify_composio_mcp_account": (
        "SPOTIFY_COMPOSIO_MCP_ACCOUNT",
        None,
        False,
        "Composio Spotify account name used by the MCP workbench",
    ),
    "spotify_expected_user": (
        "SPOTIFY_EXPECTED_USER",
        None,
        False,
        "Spotify user ID that the Composio connection must return",
    ),
    "plex_url": ("PLEX_SERVER_URL", None, False, "Plex server HTTP(S) base URL"),
    "plex_accounts": (
        "CONSUMPTION_PLEX_ACCOUNTS",
        None,
        False,
        "Comma separated Plex account IDs or names; empty means all",
    ),
    "plex_page_size": (
        "CONSUMPTION_PLEX_PAGE_SIZE",
        "200",
        False,
        "Plex history page size, 1..1000",
    ),
    "plex_metadata_lookups": (
        "CONSUMPTION_PLEX_METADATA_LOOKUPS",
        "1000",
        False,
        "Plex metadata lookups per run; history stops (and resumes next run) at the limit",
    ),
    "abs_url": (
        "AUDIOBOOKSHELF_URL",
        None,
        False,
        "Audiobookshelf base URL (with or without /api)",
    ),
    "abs_page_size": (
        "CONSUMPTION_ABS_PAGE_SIZE",
        "50",
        False,
        "Audiobookshelf listening-session page size",
    ),
    "abs_item_lookups": (
        "CONSUMPTION_ABS_ITEM_LOOKUPS",
        "300",
        False,
        "Audiobookshelf item lookups per run for progress titles",
    ),
    "kavita_url": (
        "KAVITA_URL",
        None,
        False,
        "Kavita base URL for matching audiobook ebook text (optional)",
    ),
    "book_min_progress": (
        "CONSUMPTION_BOOK_MIN_PROGRESS",
        "0.25",
        False,
        "Minimum Audiobookshelf progress fraction (> threshold, or finished) to ingest ebook text",
    ),
    "book_chunk_chars": (
        "CONSUMPTION_BOOK_CHUNK_CHARS",
        "8000",
        False,
        "Maximum characters per audiobook text chunk message",
    ),
    "book_max_downloads": (
        "CONSUMPTION_BOOK_MAX_DOWNLOADS",
        "0",
        False,
        "Maximum Kavita ebook downloads per run (0 = unlimited)",
    ),
    "youtube_takeout": (
        "CONSUMPTION_YOUTUBE_TAKEOUT",
        None,
        False,
        "Extracted Google Takeout directory or a watch-history.json/.html file",
    ),
    "youtube_messages": (
        "CONSUMPTION_YOUTUBE_MESSAGES",
        None,
        False,
        "JSON/JSONL file or directory of youtube.watch messages exported from Throughline",
    ),
    "youtube_watcher": (
        "CONSUMPTION_YOUTUBE_WATCHER",
        None,
        False,
        "Export file or directory of the youtube plugin's history watcher (fetch.py poll): viewing sessions and history days",
    ),
    "plex_token": ("PLEX_TOKEN", None, True, "Plex server token (header only)"),
    "abs_token": (
        "AUDIOBOOKSHELF_TOKEN",
        None,
        True,
        "Audiobookshelf API token (Bearer header)",
    ),
    "kavita_api_key": (
        "KAVITA_API_KEY",
        None,
        True,
        "Kavita OPDS/Plugin API key (exchanged for a JWT in memory)",
    ),
    "spotify_access_token": (
        "SPOTIFY_ACCESS_TOKEN",
        None,
        True,
        "Spotify access token used as is (no refresh)",
    ),
    "spotify_client_id": (
        "SPOTIFY_CLIENT_ID",
        None,
        True,
        "Spotify application client ID (refresh flow)",
    ),
    "spotify_client_secret": (
        "SPOTIFY_CLIENT_SECRET",
        None,
        True,
        "Spotify application client secret (refresh flow)",
    ),
    "spotify_refresh_token": (
        "SPOTIFY_REFRESH_TOKEN",
        None,
        True,
        "Spotify refresh token; refreshed access tokens stay in memory",
    ),
    "composio_api_key": (
        "COMPOSIO_API_KEY",
        None,
        True,
        "Composio API key for the Spotify toolkit transport",
    ),
    "composio_mcp_token": (
        "SPOTIFY_COMPOSIO_MCP_TOKEN",
        None,
        True,
        "Composio MCP bearer token for Spotify recently played",
    ),
}
SECRET_KEYS = tuple(k for k, v in ENV.items() if v[2])

# Keys whose values drift without any change to what was consumed. Dropping
# them keeps re-exports of the same play, like or entry byte-identical.
VOLATILE_SPOTIFY = {"available_markets", "popularity", "preview_url", "is_playable"}
PLEX_METADATA_KEYS = (
    "ratingKey",
    "guid",
    "type",
    "title",
    "originalTitle",
    "year",
    "duration",
    "index",
    "parentIndex",
    "parentTitle",
    "parentGuid",
    "parentRatingKey",
    "grandparentTitle",
    "grandparentGuid",
    "grandparentRatingKey",
    "librarySectionID",
    "librarySectionTitle",
    "studio",
    "originallyAvailableAt",
)
DEFAULT_COMPOSIO_TOOLS = {
    "recent": "SPOTIFY_GET_RECENTLY_PLAYED_TRACKS",
    "saved": "SPOTIFY_GET_USER_S_SAVED_TRACKS",
    "playlists": "SPOTIFY_GET_CURRENT_USER_S_PLAYLISTS",
    "playlist_items": "SPOTIFY_GET_PLAYLIST_ITEMS",
}
COMPOSIO_PAYLOAD_PREFIX = "THROUGHLINE_SPOTIFY_SNAPSHOT_B64="


class FetchError(RuntimeError):
    """A source failed. Messages never contain credentials or response bodies."""


class HttpStatus(FetchError):
    def __init__(self, status, label):
        super().__init__(f"{label} failed (HTTP {status})")
        self.status = status


class ToolFailed(FetchError):
    """A Composio tool execution reported failure (for example no access)."""


class LimitReached(Exception):
    """A per-run or per-day limit stopped a service; the rest resumes next run."""


# ---------------------------------------------------------------- time helpers


def utc_now():
    return datetime.now(timezone.utc)


def parse_time(value):
    """ISO timestamp or date to an aware datetime; naive values are UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        text = str(value).strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            text += "T00:00:00+00:00"
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def iso(moment):
    return moment.astimezone(timezone.utc).isoformat()


def ms(moment):
    return int(moment.timestamp() * 1000)


def ms_iso(value):
    return iso(datetime.fromtimestamp(int(value) / 1000, timezone.utc))


def known_time(value):
    """A source timestamp, or None for missing and epoch-zero placeholders."""
    try:
        moment = parse_time(value)
    except (TypeError, ValueError):
        return None
    if moment is None or moment.year < 1971:
        return None
    return moment


def digest(value, length=24):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:length]


def redact(text, secrets):
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    return text


# ---------------------------------------------------------------- config


class Config:
    def __init__(self, env=None, **overrides):
        env = os.environ if env is None else env
        values = {}
        for key, (name, default, _secret, _description) in ENV.items():
            value = env.get(name)
            values[key] = default if value in (None, "") else value
        values.update({k: v for k, v in overrides.items() if v is not None})
        self.values = values
        self.secrets = {k: values[k] for k in SECRET_KEYS}
        self.services = (
            {s.strip() for s in values["services"].split(",") if s.strip()}
            if values["services"]
            else None
        )
        unknown = (self.services or set()) - set(SERVICES)
        if unknown:
            raise ValueError(
                "Unknown CONSUMPTION_SERVICES: " + ", ".join(sorted(unknown))
            )
        self.since = parse_time(values["since"])
        self.until = parse_time(values["until"])
        if self.since and self.until and self.until <= self.since:
            raise ValueError("CONSUMPTION_UNTIL must be after CONSUMPTION_SINCE")
        self.max_requests = int(values["max_requests"] or 0)
        self.max_requests_per_day = int(values["max_requests_per_day"] or 0)
        self.delay_s = float(values["delay_s"] or 0)
        self.max_items = int(values["max_items"]) if values["max_items"] else None
        self.plex_page_size = int(values["plex_page_size"])
        if not 1 <= self.plex_page_size <= 1000:
            raise ValueError("CONSUMPTION_PLEX_PAGE_SIZE must be between 1 and 1000")
        self.abs_page_size = max(1, int(values["abs_page_size"]))
        self.plex_metadata_lookups = int(values["plex_metadata_lookups"])
        self.abs_item_lookups = int(values["abs_item_lookups"])
        self.book_min_progress = float(values["book_min_progress"])
        if not 0.0 <= self.book_min_progress <= 1.0:
            raise ValueError("CONSUMPTION_BOOK_MIN_PROGRESS must be between 0 and 1")
        self.book_chunk_chars = max(500, int(values["book_chunk_chars"]))
        self.book_max_downloads = max(0, int(values["book_max_downloads"] or 0))
        tools = dict(DEFAULT_COMPOSIO_TOOLS)
        if values["composio_tools"]:
            tools.update(json.loads(values["composio_tools"]))
        self.composio_tools = tools
        for key in (
            "plex_url",
            "abs_url",
            "kavita_url",
            "spotify_api_base",
            "composio_base",
            "composio_mcp_url",
        ):
            if values[key]:
                check_url(values[key], ENV[key][0])

    def get(self, key):
        return self.values.get(key)

    @property
    def bounded(self):
        """Explicitly bounded backfills never move incremental cursors."""
        return self.until is not None

    def configured(self):
        """Services with enough configuration to run, in a fixed order."""
        v = self.values
        ready = {
            "spotify": bool(
                v["spotify_access_token"]
                or (
                    v["spotify_client_id"]
                    and v["spotify_client_secret"]
                    and v["spotify_refresh_token"]
                )
                or (v["composio_api_key"] and v["spotify_composio_account"])
                or (v["composio_mcp_token"] and v["spotify_composio_mcp_account"])
            ),
            "spotify_backup": bool(v["spotify_backup_dir"]),
            "plex": bool(v["plex_url"] and v["plex_token"]),
            "audiobookshelf": bool(v["abs_url"] and v["abs_token"]),
            "youtube": bool(
                v["youtube_takeout"] or v["youtube_messages"] or v["youtube_watcher"]
            ),
        }
        chosen = [s for s in SERVICES if ready[s]]
        if self.services is not None:
            missing = sorted(self.services - set(chosen))
            if missing:
                raise ValueError(
                    "Requested services are not configured: " + ", ".join(missing)
                )
            chosen = [s for s in chosen if s in self.services]
        return chosen


def check_url(url, name):
    parts = urllib.parse.urlsplit(url)
    if (
        parts.scheme not in ("http", "https")
        or not parts.netloc
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise ValueError(f"{name} must be an HTTP(S) URL without credentials or query")


# ---------------------------------------------------------------- HTTP


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Credentials are sent in headers; never forward them to another URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HttpStatus(code, "redirect refused")


class Budget:
    def __init__(self, per_run, per_day, state, today):
        self.per_run = per_run
        self.per_day = per_day
        self.used = 0
        days = state.setdefault("requests_by_day", {})
        for day in list(days):
            if day < (today - timedelta(days=7)).isoformat():
                del days[day]
        self.days = days
        self.today = today.isoformat()

    def spend(self, label):
        if self.per_run and self.used >= self.per_run:
            raise LimitReached(f"per-run request limit reached at {label}")
        if self.per_day and self.days.get(self.today, 0) >= self.per_day:
            raise LimitReached(f"per-day request limit reached at {label}")
        self.used += 1
        self.days[self.today] = self.days.get(self.today, 0) + 1


class Http:
    def __init__(self, budget, delay_s=0.25, sleep=time.sleep, timeout=60, attempts=5):
        self.budget = budget
        self.delay_s = delay_s
        self.sleep = sleep
        self.timeout = timeout
        self.attempts = attempts
        self.last = None
        self.opener = urllib.request.build_opener(NoRedirect())

    def pace(self):
        if self.last is not None and self.delay_s:
            wait = self.delay_s - (time.monotonic() - self.last)
            if wait > 0:
                self.sleep(wait)
        self.last = time.monotonic()

    def request_bytes(
        self,
        method,
        url,
        label,
        headers=None,
        params=None,
        data=None,
        form=None,
        raw_body=None,
        accept="*/*",
    ):
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        body = raw_body
        headers = {
            "Accept": accept,
            "User-Agent": USER_AGENT,
            **(headers or {}),
        }
        if data is not None:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        elif form is not None:
            body = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        for attempt in range(self.attempts):
            self.budget.spend(label)
            self.pace()
            retry_after = None
            try:
                request = urllib.request.Request(
                    url, data=body, headers=headers, method=method
                )
                with self.opener.open(request, timeout=self.timeout) as response:
                    payload = response.read()
                    resp_headers = dict(response.headers.items())
            except urllib.error.HTTPError as error:
                status = error.code
                retry_after = (
                    error.headers.get("Retry-After") if error.headers else None
                )
                error.close()
                if status != 429 and status < 500:
                    raise HttpStatus(status, label) from None
                if attempt == self.attempts - 1:
                    raise HttpStatus(status, label) from None
            except HttpStatus:
                raise
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
                if attempt == self.attempts - 1:
                    raise FetchError(f"{label} failed (transport error)") from None
            else:
                return payload, resp_headers
            wait = min(60.0, 0.5 * 2**attempt)
            if retry_after:
                try:
                    wait = min(120.0, max(wait, float(retry_after)))
                except ValueError:
                    pass
            self.sleep(wait)
        raise FetchError(f"{label} retries exhausted")

    def request(
        self, method, url, label, headers=None, params=None, data=None, form=None, raw_body=None
    ):
        payload, _ = self.request_bytes(
            method,
            url,
            label,
            headers=headers,
            params=params,
            data=data,
            form=form,
            raw_body=raw_body,
            accept="application/json",
        )
        if not payload.strip():
            return {}
        try:
            return json.loads(payload.decode("utf-8-sig"))
        except ValueError:
            raise FetchError(f"{label} returned invalid JSON") from None


# ---------------------------------------------------------------- records


def record(kind, native_id, raw, context=None, observed_at=None):
    item = {
        "kind": kind,
        "id": str(native_id),
        "service": kind.split(".")[0],
        "raw": raw,
    }
    if context is not None:
        item["context"] = context
    if observed_at is not None:
        item["observed_at"] = observed_at
    return item


def strip_keys(value, keys):
    if isinstance(value, dict):
        return {k: strip_keys(v, keys) for k, v in value.items() if k not in keys}
    if isinstance(value, list):
        return [strip_keys(v, keys) for v in value]
    return value


def in_bounds(moment, config):
    if moment is None:
        return True
    if config.since and moment < config.since:
        return False
    if config.until and moment >= config.until:
        return False
    return True


# ---------------------------------------------------------------- Spotify


def has_more(page, count, limit, offset):
    """Offset paging: trust `next`, then `total`, then a full page.

    Spotify always sends `next` and `total`; a Composio envelope may drop
    them, and stopping after the first page would silently lose data.
    """
    if not count:
        return False
    if "next" in page:
        return bool(page["next"])
    if page.get("total") is not None:
        return offset < int(page["total"])
    return count >= limit


def spotify_entry_item(entry):
    return (entry or {}).get("item") or (entry or {}).get("track") or {}


def playlist_context(playlist):
    owner = playlist.get("owner") or {}
    return {
        "playlist_id": playlist.get("id"),
        "playlist_name": playlist.get("name"),
        "playlist_owner_id": owner.get("id"),
        "playlist_owner_name": owner.get("display_name"),
        "collaborative": playlist.get("collaborative"),
    }


def playlist_item_id(playlist_id, entry, occurrences):
    item = spotify_entry_item(entry)
    base = digest(
        [item.get("uri") or item.get("id") or item.get("name"), entry.get("added_at")],
        16,
    )
    occurrences[base] = occurrences.get(base, 0) + 1
    return f"{playlist_id}:{base}:{occurrences[base]}"


def _unwrap_composio(value):
    for _ in range(7):
        if not isinstance(value, dict) or value.get("successful") is False:
            raise ToolFailed("Spotify Composio response was unsuccessful")
        if isinstance(value.get("data"), dict):
            value = value["data"]
        else:
            return value
    raise ToolFailed("Spotify Composio response nesting is invalid")


def composio_mcp_recent(http, config):
    """Read profile + the newest 50 plays through the authenticated MCP workbench.

    The workbench is the already-live transport used by the retired standalone
    Spotify source.  The token stays in the Authorization header and the remote
    stdout contains only a compressed Spotify response.
    """
    import base64

    account = config.get("spotify_composio_mcp_account")
    specs = [
        ("profile", "SPOTIFY_GET_CURRENT_USER_S_PROFILE", {}),
        ("recent", "SPOTIFY_GET_RECENTLY_PLAYED_TRACKS", {"limit": 50}),
    ]
    code = (
        "import base64, json, zlib\n"
        f"_specs = json.loads({json.dumps(specs)!r})\n"
        "_out = {}\n"
        "for _label, _tool, _arguments in _specs:\n"
        f"    _result, _error = run_composio_tool(_tool, _arguments, account={account!r})\n"
        "    _out[_label] = {'result': _result, 'error': _error}\n"
        "_blob = zlib.compress(json.dumps(_out, ensure_ascii=False).encode())\n"
        f"print({COMPOSIO_PAYLOAD_PREFIX!r} + base64.b64encode(_blob).decode())\n"
    )
    body = {
        "jsonrpc": "2.0",
        "id": uuid.uuid4().hex,
        "method": "tools/call",
        "params": {
            "name": "COMPOSIO_REMOTE_WORKBENCH",
            "arguments": {
                "code_to_execute": code,
                "session_id": "throughline-consumption-" + uuid.uuid4().hex,
                "current_step": "SYNC_SPOTIFY",
                "thought": "Read Spotify identity and recently played data.",
            },
        },
    }
    payload, _headers = http.request_bytes(
        "POST",
        config.get("composio_mcp_url"),
        "spotify Composio MCP",
        headers={"Authorization": "Bearer " + config.get("composio_mcp_token")},
        data=body,
        accept="application/json, text/event-stream",
    )
    text = payload.decode("utf-8-sig")
    try:
        if text.lstrip().startswith("{"):
            envelope = json.loads(text)
        else:
            candidates = [
                json.loads(line[5:].strip())
                for line in text.splitlines()
                if line.startswith("data:")
                and line[5:].strip() not in ("", "[DONE]")
            ]
            envelope = next(
                item
                for item in candidates
                if isinstance(item, dict) and ("result" in item or "error" in item)
            )
        if envelope.get("error"):
            raise ValueError
        content = (envelope.get("result") or {}).get("content") or []
        workbench = json.loads(
            next(item["text"] for item in content if item.get("type") == "text")
        )
        data = workbench.get("data") or {}
        if workbench.get("successful") is not True or data.get("error") or data.get("stderr"):
            raise ValueError
        payloads = [
            line[len(COMPOSIO_PAYLOAD_PREFIX) :]
            for line in (data.get("stdout") or "").splitlines()
            if line.startswith(COMPOSIO_PAYLOAD_PREFIX)
        ]
        if len(payloads) != 1:
            raise ValueError
        decoded = json.loads(
            zlib.decompress(base64.b64decode(payloads[0], validate=True))
        )
        if any(not decoded.get(name) or decoded[name].get("error") for name in ("profile", "recent")):
            raise ValueError
        profile = _unwrap_composio(decoded["profile"]["result"])
        recent = _unwrap_composio(decoded["recent"]["result"])
    except (KeyError, StopIteration, ValueError, TypeError, zlib.error):
        raise ToolFailed("Spotify Composio MCP response was invalid") from None
    expected = config.get("spotify_expected_user")
    if expected and profile.get("id") != expected:
        raise ToolFailed("Spotify Composio identity does not match expected user")
    items = recent.get("items")
    if not isinstance(items, list) or len(items) > 50:
        raise ToolFailed("Spotify Composio recently-played response was invalid")
    return {"items": items, "cursors": recent.get("cursors") or {}}


class SpotifyClient:
    """Spotify Web API (token or refresh flow) or the Composio Spotify toolkit."""

    def __init__(self, http, config):
        self.http = http
        self.config = config
        v = config.values
        self.mcp = bool(
            v["composio_mcp_token"] and v["spotify_composio_mcp_account"]
        )
        self.composio = bool(
            not self.mcp
            and v["composio_api_key"]
            and v["spotify_composio_account"]
        )
        self._mcp_recent = None
        self.access = v["spotify_access_token"]
        self.refresh_token = v["spotify_refresh_token"]
        self.can_refresh = bool(
            v["spotify_client_id"]
            and v["spotify_client_secret"]
            and v["spotify_refresh_token"]
        )
        if not self.mcp and not self.composio and not self.access:
            self.refresh()

    def refresh(self):
        import base64

        v = self.config.values
        basic = base64.b64encode(
            f"{v['spotify_client_id']}:{v['spotify_client_secret']}".encode()
        ).decode()
        result = self.http.request(
            "POST",
            v["spotify_token_url"],
            "spotify token refresh",
            headers={"Authorization": "Basic " + basic},
            form={"grant_type": "refresh_token", "refresh_token": self.refresh_token},
        )
        if not result.get("access_token"):
            raise FetchError("spotify token refresh returned no access token")
        self.access = result["access_token"]
        # A rotated refresh token is kept in memory only; nothing is written.
        self.refresh_token = result.get("refresh_token") or self.refresh_token

    def get(self, tool, path, params, label):
        if self.mcp:
            if tool != "recent":
                raise ToolFailed("Spotify Composio MCP fast lane only supports recently played")
            # The workbench returns Spotify's complete newest-50 cap.  It has no
            # reliable backward cursor, so never repeat the same page as one.
            if "before" in params:
                return {"items": []}
            if self._mcp_recent is None:
                self._mcp_recent = composio_mcp_recent(self.http, self.config)
            return self._mcp_recent
        if self.composio:
            return self.execute(tool, params, label)
        url = self.config.values["spotify_api_base"].rstrip("/") + "/" + path
        for attempt in range(2):
            try:
                return self.http.request(
                    "GET",
                    url,
                    label,
                    headers={"Authorization": "Bearer " + self.access},
                    params=params,
                )
            except HttpStatus as error:
                if error.status == 401 and self.can_refresh and attempt == 0:
                    self.refresh()
                    continue
                raise

    def execute(self, tool, arguments, label):
        v = self.config.values
        body = {
            "connected_account_id": v["spotify_composio_account"],
            "arguments": arguments,
        }
        if v["composio_user_id"]:
            body["user_id"] = v["composio_user_id"]
        slug = self.config.composio_tools[tool]
        result = self.http.request(
            "POST",
            v["composio_base"].rstrip("/")
            + "/tools/execute/"
            + urllib.parse.quote(slug),
            label,
            headers={"x-api-key": v["composio_api_key"]},
            data=body,
        )
        if result.get("successful") is False:
            raise ToolFailed(f"{label} failed (Composio tool {slug} unsuccessful)")
        data = result.get("data", result)
        # Toolkits wrap the Spotify JSON in one or two envelopes.
        for _ in range(3):
            if isinstance(data, dict) and "items" not in data:
                inner = next(
                    (
                        data[k]
                        for k in ("response_data", "data", "body")
                        if isinstance(data.get(k), dict)
                    ),
                    None,
                )
                if inner is None:
                    break
                data = inner
        return data if isinstance(data, dict) else {}


def spotify_recent(client, state, config, notes=None):
    """Newest window, then backward with `before`; never combine before and after.

    Spotify keeps only the newest 50 plays. When a full window starts after
    the watermark, the plays in between are lost: noted in `notes` as
    spotify_recent_gap {from, to}.
    """
    watermark = state.get("recent_after_ms")
    lower = max(watermark or -1, ms(config.since) - 1 if config.since else -1)
    params = {"limit": 50}
    newest = watermark
    seen_before = set()
    full, reached = None, None
    for _ in range(40):
        page = client.get(
            "recent", "me/player/recently-played", params, "spotify recently-played"
        )
        items = page.get("items") or []
        if full is None:
            full = len(items) >= 50
        if not items:
            break
        times = []
        for entry in items:
            played = known_time(entry.get("played_at"))
            if played is None:
                continue
            at = ms(played)
            times.append(at)
            item = spotify_entry_item(entry)
            if at <= lower or not item or (config.until and played >= config.until):
                continue
            newest = max(newest or 0, at)
            yield record(
                "spotify.play",
                f"{at}:{item.get('uri') or item.get('id') or digest(item.get('name'))}",
                strip_keys(entry, VOLATILE_SPOTIFY),
            )
        if not times:
            break
        oldest = min(times)
        reached = oldest if reached is None else min(reached, oldest)
        if len(items) < 50 or oldest <= lower:
            break
        before = min(int((page.get("cursors") or {}).get("before") or oldest), oldest)
        if before in seen_before:
            break
        seen_before.add(before)
        params = {"limit": 50, "before": before}
    if (
        notes is not None
        and not config.bounded
        and watermark is not None
        and full
        and reached is not None
        and reached > watermark
    ):
        notes["spotify_recent_gap"] = {"from": ms_iso(watermark), "to": ms_iso(reached)}
    if not config.bounded and newest is not None:
        state["recent_after_ms"] = newest


def spotify_saved(client, state, config):
    watermark = known_time(state.get("saved_after"))
    newest = watermark
    offset = 0
    while True:
        page = client.get(
            "saved",
            "me/tracks",
            {"limit": 50, "offset": offset},
            "spotify saved tracks",
        )
        items = page.get("items") or []
        stop = False
        for entry in items:
            item = spotify_entry_item(entry)
            added = known_time(entry.get("added_at"))
            if not item or not (item.get("uri") or item.get("id")):
                continue
            if added is not None:
                newest = max(newest, added) if newest else added
                if (watermark and added <= watermark) or (
                    config.since and added < config.since
                ):
                    stop = True
                    continue
                if config.until and added >= config.until:
                    continue
            yield record(
                "spotify.saved_track",
                item.get("uri") or item.get("id"),
                strip_keys(entry, VOLATILE_SPOTIFY),
            )
        offset += len(items)
        if stop or not has_more(page, len(items), 50, offset):
            break
    if not config.bounded and newest is not None:
        state["saved_after"] = iso(newest)


def spotify_playlists(client, state, config, notes):
    snapshots = state.setdefault("playlist_snapshots", {})
    playlists = []
    offset = 0
    while True:
        page = client.get(
            "playlists",
            "me/playlists",
            {"limit": 50, "offset": offset},
            "spotify playlists",
        )
        items = page.get("items") or []
        playlists.extend(p for p in items if p and p.get("id"))
        offset += len(items)
        if not has_more(page, len(items), 50, offset):
            break
    for playlist in playlists:
        pid = playlist["id"]
        snapshot = playlist.get("snapshot_id")
        if snapshot and snapshots.get(pid) == snapshot:
            continue
        context = playlist_context(playlist)
        occurrences = {}
        offset = 0
        path = f"playlists/{pid}/items"
        try:
            while True:
                params = {"limit": 100, "offset": offset}
                if client.composio:
                    params["playlist_id"] = pid
                try:
                    page = client.get(
                        "playlist_items", path, params, "spotify playlist items"
                    )
                except HttpStatus as error:
                    # Before the 2026 rename the endpoint was /tracks.
                    if (
                        error.status == 404
                        and path.endswith("/items")
                        and not client.composio
                    ):
                        path = f"playlists/{pid}/tracks"
                        continue
                    raise
                entries = page.get("items") or []
                for entry in entries:
                    if not spotify_entry_item(entry):
                        continue
                    if not in_bounds(known_time(entry.get("added_at")), config):
                        continue
                    yield record(
                        "spotify.playlist_item",
                        playlist_item_id(pid, entry, occurrences),
                        strip_keys(entry, VOLATILE_SPOTIFY),
                        context,
                    )
                offset += len(entries)
                if not has_more(page, len(entries), 100, offset):
                    break
        except (HttpStatus, ToolFailed) as error:
            if isinstance(error, HttpStatus) and error.status not in (403, 404):
                raise
            notes["spotify_playlists_unreadable"] = (
                notes.get("spotify_playlists_unreadable", 0) + 1
            )
        if snapshot and not config.bounded and not config.since:
            snapshots[pid] = snapshot


def collect_spotify(fetcher, state):
    client = SpotifyClient(fetcher.http, fetcher.config)
    yield from spotify_recent(
        client, state.setdefault("recent", {}), fetcher.config, fetcher.notes
    )
    if client.mcp:
        return
    yield from spotify_saved(client, state.setdefault("saved", {}), fetcher.config)
    if fetcher.config.get("spotify_playlists") not in ("0", "false", "no"):
        yield from spotify_playlists(
            client, state.setdefault("playlists", {}), fetcher.config, fetcher.notes
        )


# ---------------------------------------------------------------- Spotify backup


def is_playlist(value):
    return (
        isinstance(value, dict)
        and value.get("id")
        and value.get("name") is not None
        and (
            "owner" in value
            or value.get("type") == "playlist"
            or "snapshot_id" in value
        )
    )


def playlist_entries(value):
    for key in ("items", "tracks", "entries"):
        found = value.get(key)
        if isinstance(found, dict):
            found = found.get("items")
        if isinstance(found, list):
            return found
    return None


def saved_entry(value):
    if not isinstance(value, dict):
        return None
    if isinstance(value.get("track"), dict) or isinstance(value.get("item"), dict):
        return value
    if str(value.get("uri", "")).startswith(("spotify:track:", "spotify:episode:")):
        return {"track": value, "added_at": None}
    return None


def walk_backup(value, hint):
    """Find playlists and liked songs in Spotify API shaped backup JSON."""
    if isinstance(value, dict):
        if is_playlist(value):
            entries = playlist_entries(value)
            if entries is not None:
                yield "playlist", value, entries
                return
        for key, inner in value.items():
            yield from walk_backup(inner, hint + "/" + str(key).lower())
    elif isinstance(value, list):
        liked = any(
            word in hint for word in ("liked", "saved", "favorite", "favourite")
        )
        if liked and value and all(saved_entry(v) for v in value):
            yield "saved", None, [saved_entry(v) for v in value]
            return
        for inner in value:
            yield from walk_backup(inner, hint)


def unwrap_page(value):
    """A paged API response, possibly inside `data` envelopes (Composio)."""
    for _ in range(3):
        if not isinstance(value, dict) or isinstance(value.get("items"), list):
            break
        value = value.get("data")
    if isinstance(value, dict) and isinstance(value.get("items"), list):
        return value
    return None


def backup_inventory(path, root, cache):
    """Playlists by ID from the nearest `inventory.json` at or above the file."""
    folder = path.parent
    while True:
        if folder not in cache:
            found = {}
            candidate = folder / "inventory.json"
            if candidate.is_file():
                try:
                    data = json.loads(candidate.read_text(encoding="utf-8-sig"))
                except ValueError:
                    data = None
                stack = [data]
                while stack:
                    value = stack.pop()
                    if isinstance(value, dict):
                        if is_playlist(value):
                            found.setdefault(value["id"], value)
                        stack.extend(value.values())
                    elif isinstance(value, list):
                        stack.extend(value)
            cache[folder] = found
        if cache[folder] or folder == root or root not in folder.parents:
            return cache[folder]
        folder = folder.parent


def backup_pages(data, path, root, inventories):
    """A page of one playlist's entries: `playlists/<playlist id>/<page>.json`.

    The page holds only entries; the playlist itself comes from an
    `inventory.json` of the same backup, else just its ID.
    """
    if path.parent.parent.name.lower() != "playlists" or is_playlist(data):
        return None
    page = unwrap_page(data)
    if page is None:
        return None
    pid = path.parent.name
    playlist = backup_inventory(path, root, inventories).get(pid) or {"id": pid}
    return playlist, page["items"]


def backup_observed(path, root):
    match = re.search(r"(\d{4}-\d{2}-\d{2})", str(path.relative_to(root)))
    if match:
        return match[1] + "T00:00:00+00:00"
    return iso(datetime.fromtimestamp(path.stat().st_mtime, timezone.utc))


def collect_spotify_backup(fetcher, state):
    root = Path(fetcher.config.get("spotify_backup_dir"))
    if not root.is_dir():
        raise FetchError("CONSUMPTION_SPOTIFY_BACKUP_DIR is not a directory")
    files = sorted(
        (p for p in root.rglob("*.json") if p.is_file()),
        # Snapshots first, the latest copy last: later files win on equal IDs.
        key=lambda p: (
            "latest" in [x.lower() for x in p.relative_to(root).parts],
            str(p.relative_to(root)),
        ),
    )
    done = state.setdefault("files", {})
    fresh = []
    for path in files:
        stat = path.stat()
        mark = f"{stat.st_size}:{stat.st_mtime_ns}"
        rel = path.relative_to(root).as_posix()
        if done.get(rel) != mark:
            fresh.append((path, rel, mark))
    collected = {}
    inventories = {}
    # Paged playlists count repeated entries across all pages of one playlist.
    page_occurrences = {}
    for path, rel, _mark in fresh:
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except ValueError:
            fetcher.notes["spotify_backup_unreadable_files"] = (
                fetcher.notes.get("spotify_backup_unreadable_files", 0) + 1
            )
            continue
        observed = backup_observed(path, root)
        paged = backup_pages(data, path, root, inventories)
        if paged is not None:
            groups = [("playlist", *paged)]
        else:
            # The hint is the path below the root, so `liked/page-0.json` counts.
            groups = walk_backup(data, rel.lower())
        for kind, playlist, entries in groups:
            if paged is not None:
                occurrences = page_occurrences.setdefault(path.parent, {})
            else:
                occurrences = {}
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                item = spotify_entry_item(entry)
                if not item:
                    continue
                if not in_bounds(known_time(entry.get("added_at")), fetcher.config):
                    continue
                entry = strip_keys(entry, VOLATILE_SPOTIFY)
                if kind == "saved":
                    ident = item.get("uri") or item.get("id")
                    if not ident:
                        continue
                    rec = record(
                        "spotify.saved_track", ident, entry, observed_at=observed
                    )
                else:
                    rec = record(
                        "spotify.playlist_item",
                        playlist_item_id(playlist["id"], entry, occurrences),
                        entry,
                        playlist_context(playlist),
                        observed_at=observed,
                    )
                key = (rec["kind"], rec["id"])
                # Keep the earliest observation time of an entry seen in several snapshots.
                if key in collected:
                    rec["observed_at"] = min(collected[key]["observed_at"], observed)
                collected[key] = rec
    for key in sorted(collected):
        yield collected[key]
    if not fetcher.config.bounded and not fetcher.config.since:
        for _path, rel, mark in fresh:
            done[rel] = mark


# ---------------------------------------------------------------- Plex


def collect_plex(fetcher, state):
    config = fetcher.config
    base = config.get("plex_url").rstrip("/") + "/"
    headers = {
        "X-Plex-Token": config.get("plex_token"),
        "X-Plex-Client-Identifier": "throughline-consumption",
        "X-Plex-Product": "Throughline",
    }

    def get(path, label, params=None, missing=False):
        try:
            result = fetcher.http.request(
                "GET", base + path.lstrip("/"), label, headers=headers, params=params
            )
        except HttpStatus as error:
            if missing and error.status in (401, 403, 404):
                return {}
            raise
        return result.get("MediaContainer") or {}

    server = get("/identity", "plex identity").get("machineIdentifier")
    if not server:
        raise FetchError("plex identity has no machineIdentifier")
    if state.get("server", server) != server:
        raise FetchError(
            "fetch state belongs to another Plex server; use a separate state"
        )
    accounts = {
        str(a.get("id")): a.get("name")
        for a in get("/accounts", "plex accounts", missing=True).get("Account") or []
    }
    devices = {
        str(d.get("id")): {"name": d.get("name"), "platform": d.get("platform")}
        for d in get("/devices", "plex devices", missing=True).get("Device") or []
    }
    wanted = {
        s.strip().casefold()
        for s in (config.get("plex_accounts") or "").split(",")
        if s.strip()
    }
    cache = {}
    lookups = 0

    def first(value):
        return value[0] if isinstance(value, list) and value else value or {}

    def account_info(row):
        user = first(row.get("User"))
        account_id = str(row.get("accountID") or user.get("id") or "unknown")
        account_name = accounts.get(account_id) or user.get("title")
        allowed = not wanted or bool(
            {account_id.casefold(), (account_name or "").casefold()} & wanted
        )
        return account_id, account_name, allowed

    def metadata(row):
        nonlocal lookups
        key = str(row.get("ratingKey") or "")
        if not key.isdigit():
            return {}
        if key not in cache:
            if lookups >= config.plex_metadata_lookups:
                raise LimitReached("plex metadata lookup limit reached")
            lookups += 1
            found = get(f"/library/metadata/{key}", "plex metadata", missing=True).get(
                "Metadata"
            ) or [{}]
            cache[key] = {k: found[0][k] for k in PLEX_METADATA_KEYS if k in found[0]}
        return cache[key]

    watermark = int(state.get("watermark") or 0)
    # One day of overlap re-reads late edits, except when resuming a run that
    # stopped at a limit: then the overlap could hold more than one run's quota.
    overlap = 0 if state.get("resume") else 86400
    lower = max(
        watermark - overlap if watermark else 0,
        int(config.since.timestamp()) if config.since else 0,
    )
    offset = 0
    exported = watermark
    finished_pass = False
    try:
        while True:
            page = get(
                "/status/sessions/history/all",
                "plex history",
                params={
                    "sort": "viewedAt:asc",
                    "viewedAt>": max(0, lower - 1),
                    "X-Plex-Container-Start": offset,
                    "X-Plex-Container-Size": config.plex_page_size,
                },
            )
            rows = page.get("Metadata") or []
            finished = False
            for row in rows:
                viewed = int(row.get("viewedAt") or 0)
                if config.until and viewed >= config.until.timestamp():
                    finished = True
                    break
                if viewed < lower or not row.get("historyKey"):
                    continue
                account = str(row.get("accountID", ""))
                name = accounts.get(account)
                if wanted and not (
                    {account.casefold(), (name or "").casefold()} & wanted
                ):
                    exported = max(exported, viewed)
                    continue
                context = {
                    "server": server,
                    "account": {"id": account, "name": name},
                    "device": devices.get(str(row.get("deviceID")), {}),
                    "metadata": metadata(row),
                }
                yield record(
                    "plex.history", f"{server}:{row['historyKey']}", row, context
                )
                exported = max(exported, viewed)
            offset += len(rows)
            total = page.get("totalSize")
            # Without totalSize (`size` is only this page), a full page means more.
            last = (
                offset >= int(total)
                if total is not None
                else len(rows) < config.plex_page_size
            )
            if finished or not rows or last:
                finished_pass = True
                break
    finally:
        state["server"] = server
        # Ascending order: everything up to `exported` was written, so a stopped
        # run can still move the watermark.
        if not config.bounded and exported:
            state["watermark"] = exported
            state["resume"] = not finished_pass

    # Active playback is a changing observation, separate from Plex's played
    # mark history.  It retains first/last poll times and playback position.
    now = int(fetcher.now.timestamp())
    observations = state.setdefault("sessions", {})
    current = set()
    live = get("/status/sessions", "plex active sessions", missing=True).get(
        "Metadata"
    ) or []
    for row in live:
        account, name, allowed = account_info(row)
        if not allowed:
            continue
        session = first(row.get("Session"))
        session_id = session.get("id") or row.get("sessionKey")
        if session_id is None:
            fetcher.notes["plex_sessions_without_id"] = (
                fetcher.notes.get("plex_sessions_without_id", 0) + 1
            )
            continue
        key = f"{session_id}:{row.get('ratingKey')}:{account}"
        current.add(key)
        position = max(0, int(row.get("viewOffset") or 0))
        old = observations.get(key)
        restarted = bool(
            old
            and position + 2000 < int(old.get("position_ms") or position)
            and position <= (now - int(old.get("seen") or now) + 2) * 1000
        )
        if old and (old.get("end") is not None or now - int(old["seen"]) > 120 or restarted):
            old["end"] = int(old.get("played") or old["seen"])
            old_raw = dict(old.get("raw") or {})
            old_raw.update(
                _sessionStart=old["start"],
                _sessionSeen=old["seen"],
                _sessionEnd=old["end"],
                _initialOffset=old.get("initial_offset_ms"),
            )
            yield record(
                "plex.session",
                old["external_id"],
                old_raw,
                old.get("context") or {},
            )
            old = None
        if old is None:
            old = {
                "start": now,
                "initial_offset_ms": position,
                "external_id": f"{server}:session:{key}:{now}",
            }
            observations[key] = old
        player = first(row.get("Player"))
        if player.get("state") != "paused" or position != old.get("position_ms"):
            old["played"] = now
        old.update(
            seen=now,
            position_ms=position,
            raw=copy.deepcopy(row),
            context={
                "server": server,
                "account": {"id": account, "name": name},
                "metadata": metadata(row),
            },
        )
        current_raw = dict(row)
        current_raw.update(
            _sessionStart=old["start"],
            _sessionSeen=now,
            _sessionEnd=None,
            _initialOffset=old.get("initial_offset_ms"),
        )
        yield record(
            "plex.session", old["external_id"], current_raw, old["context"]
        )

    for key, observation in list(observations.items()):
        if key in current or observation.get("end") is not None:
            continue
        observation["end"] = int(observation.get("played") or observation["seen"])
        closed_raw = dict(observation.get("raw") or {})
        closed_raw.update(
            _sessionStart=observation["start"],
            _sessionSeen=observation["seen"],
            _sessionEnd=observation["end"],
            _initialOffset=observation.get("initial_offset_ms"),
        )
        yield record(
            "plex.session",
            observation["external_id"],
            closed_raw,
            observation.get("context") or {},
        )
        observation.pop("raw", None)
        observation.pop("context", None)
    for key, observation in list(observations.items()):
        if observation.get("end") is not None and observation["seen"] < now - 86400:
            del observations[key]

    # Library additions use their own per-section cursor.  The first run walks
    # every movie, episode and album; later runs overlap one day, so equal
    # addedAt timestamps and late metadata edits cannot be missed.
    section_types = {"movie": 1, "show": 4, "artist": 9}
    addition_state = state.setdefault("additions", {})
    sections = get("/library/sections", "plex library sections").get("Directory") or []
    for section in sections:
        section_key = str(section.get("key") or "")
        plex_type = section_types.get(section.get("type"))
        if not section_key or plex_type is None:
            continue
        saved = addition_state.setdefault(section_key, {})
        watermark = int(saved.get("watermark") or 0)
        lower = int(
            saved.get("lower")
            or max(
                watermark - 86400 if watermark else 0,
                int(config.since.timestamp()) if config.since else 0,
            )
        )
        offset = int(saved.get("offset") or 0)
        exported = watermark
        while True:
            page = get(
                f"/library/sections/{section_key}/all",
                "plex library additions",
                params={
                    "type": plex_type,
                    "sort": "addedAt:asc",
                    "addedAt>": max(0, lower - 1),
                    "includeGuids": 1,
                    "X-Plex-Container-Start": offset,
                    "X-Plex-Container-Size": config.plex_page_size,
                },
            )
            rows = page.get("Metadata") or []
            bounded_done = False
            for item in rows:
                added = int(item.get("addedAt") or 0)
                if added < lower:
                    continue
                if config.until and added >= int(config.until.timestamp()):
                    bounded_done = True
                    break
                if item.get("type") not in ("movie", "episode", "album"):
                    continue
                exported = max(exported, added)
                yield record(
                    "plex.library_added",
                    f"{server}:{item.get('ratingKey')}",
                    item,
                    {
                        "server": server,
                        "section": {
                            "id": section_key,
                            "title": section.get("title"),
                            "type": section.get("type"),
                        },
                    },
                )
            offset += len(rows)
            if not config.bounded:
                saved.update(lower=lower, offset=offset)
            total = page.get("totalSize")
            last = (
                offset >= int(total)
                if total is not None
                else len(rows) < config.plex_page_size
            )
            if bounded_done or not rows or last:
                if not config.bounded:
                    saved["watermark"] = exported
                    saved.pop("lower", None)
                    saved.pop("offset", None)
                break


# ---------------------------------------------------------------- Audiobookshelf


def clean_session(session):
    session = {k: v for k, v in session.items() if k != "chapters"}
    if isinstance(session.get("deviceInfo"), dict):
        session["deviceInfo"] = {
            k: v for k, v in session["deviceInfo"].items() if k != "ipAddress"
        }
    return session


def names(value):
    if isinstance(value, list):
        return (
            ", ".join(
                str(v.get("name"))
                for v in value
                if isinstance(v, dict) and v.get("name")
            )
            or None
        )
    return value or None


def summarize_item(item, episode_id=None):
    media = item.get("media") or {}
    meta = media.get("metadata") or {}
    series = meta.get("series")
    summary = {
        "mediaType": item.get("mediaType"),
        "libraryId": item.get("libraryId"),
        "title": meta.get("title"),
        "author": meta.get("authorName")
        or names(meta.get("authors"))
        or meta.get("author"),
        "narrator": meta.get("narratorName")
        or (
            ", ".join(meta["narrators"])
            if isinstance(meta.get("narrators"), list)
            else None
        ),
        "series": meta.get("seriesName") or names(series),
        "duration": media.get("duration"),
    }
    if episode_id:
        episode = next(
            (e for e in media.get("episodes") or [] if e.get("id") == episode_id), {}
        )
        summary["episode"] = {
            "id": episode_id,
            "title": episode.get("title"),
            "season": episode.get("season"),
            "episode": episode.get("episode"),
            "duration": episode.get("duration")
            or (episode.get("audioFile") or {}).get("duration"),
        }
    return {k: v for k, v in summary.items() if v not in (None, "")}


def collect_audiobookshelf(fetcher, state):
    config = fetcher.config
    base = config.get("abs_url").rstrip("/")
    if base.endswith("/api"):
        base = base[: -len("/api")]
    headers = {"Authorization": "Bearer " + config.get("abs_token")}

    def get(path, label, params=None):
        return fetcher.http.request(
            "GET", f"{base}/api/{path}", label, headers=headers, params=params
        )

    me = get("me", "audiobookshelf me")
    user = {"id": me.get("id"), "username": me.get("username")}
    since_ms = ms(config.since) if config.since else 0
    until_ms = ms(config.until) if config.until else None

    sessions_state = state.setdefault("sessions", {})
    watermark = int(sessions_state.get("updated_ms") or 0)
    # Six hours of overlap: an open session keeps updating after it was read.
    lower = max(watermark - 6 * 3600 * 1000 if watermark else 0, since_ms)
    newest = watermark
    page_no = 0
    while True:
        page = get(
            "me/listening-sessions",
            "audiobookshelf listening sessions",
            {"itemsPerPage": config.abs_page_size, "page": page_no},
        )
        sessions = page.get("sessions") or []
        old_page = False
        for session in sessions:
            updated = int(session.get("updatedAt") or 0)
            started = int(session.get("startedAt") or updated)
            if updated < lower:
                old_page = True
                continue
            if not session.get("id") or (until_ms and started >= until_ms):
                continue
            newest = max(newest, updated)
            yield record(
                "audiobookshelf.session",
                session["id"],
                clean_session(session),
                {"user": user},
            )
        page_no += 1
        pages = page.get("numPages")
        last = (
            page_no >= int(pages)
            if pages is not None
            else len(sessions) < config.abs_page_size
        )
        if not sessions or old_page or last:
            break
    if not config.bounded:
        sessions_state["updated_ms"] = newest

    progress_state = state.setdefault("progress", {})
    progress_mark = int(progress_state.get("updated_ms") or 0)
    items = {}
    lookups = 0
    exported = progress_mark
    try:
        for progress in sorted(
            me.get("mediaProgress") or [],
            key=lambda p: (int(p.get("lastUpdate") or 0), str(p.get("id"))),
        ):
            updated = int(progress.get("lastUpdate") or 0)
            started = int(progress.get("startedAt") or updated)
            if not progress.get("id") or updated <= progress_mark or updated < since_ms:
                continue
            if until_ms and started >= until_ms:
                continue
            item_id = progress.get("libraryItemId")
            if item_id and item_id not in items:
                if lookups >= config.abs_item_lookups:
                    raise LimitReached("audiobookshelf item lookup limit reached")
                lookups += 1
                try:
                    items[item_id] = get(
                        f"items/{urllib.parse.quote(str(item_id))}",
                        "audiobookshelf item",
                    )
                except HttpStatus as error:
                    if error.status != 404:
                        raise
                    items[item_id] = {}
            context = {
                "user": user,
                "item": summarize_item(
                    items.get(item_id) or {}, progress.get("episodeId")
                ),
            }
            yield record("audiobookshelf.progress", progress["id"], progress, context)
            exported = max(exported, updated)
    finally:
        if not config.bounded:
            progress_state["updated_ms"] = exported

    if config.get("kavita_url") and config.get("kavita_api_key"):
        yield from collect_audiobook_texts(
            fetcher, get, me, user, items, state.setdefault("kavita_books", {})
        )


# ---------------------------------------------------------------- Kavita ebook text


BOOK_STOP_WORDS = frozenset(
    {"the", "a", "an", "of", "and", "in", "on", "to", "for", "with", "by", "at", "from"}
)
BOOK_NOISE_RE = re.compile(
    r"\((?:unabridged|abridged|new version|updated version|revised(?: and updated)?|"
    r"dramatized adaptation|\d+(?:st|nd|rd|th) anniversary edition|\d+ of \d+|"
    r"z-library[^)]*|1lib[^)]*|book\s*#?\d+[^)]*)\)|"
    r"\[(?:unabridged|abridged|dramatized adaptation|edition-[^\]]*|\d+)\]",
    re.IGNORECASE,
)
BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "blockquote",
        "tr",
        "section",
        "article",
        "pre",
        "dt",
        "dd",
        "hr",
        "header",
        "footer",
        "aside",
        "figcaption",
    }
)
SKIP_TAGS = frozenset({"script", "style", "head", "svg", "title", "noscript"})
HEADING_TAGS = frozenset({"h1", "h2", "h3"})


class KavitaClient:
    """Read-only Kavita HTTP API client. Exchanges API key for a memory-only JWT."""

    def __init__(self, http, base_url, api_key, runtime_secrets=None):
        self.http = http
        base = base_url.rstrip("/")
        if base.endswith("/api"):
            base = base[: -len("/api")]
        self.base = base
        self.api_key = api_key
        self.runtime_secrets = runtime_secrets
        self.token = None

    def authenticate(self):
        result = self.http.request(
            "POST",
            f"{self.base}/api/Plugin/authenticate",
            "kavita authenticate",
            params={"apiKey": self.api_key, "pluginName": "throughline-consumption"},
            raw_body=b"",
        )
        token = result.get("token") if isinstance(result, dict) else None
        if not token:
            raise FetchError("kavita authenticate returned no token")
        self.token = token
        if self.runtime_secrets is not None and token not in self.runtime_secrets:
            self.runtime_secrets.append(token)
        return token

    def _headers(self):
        if not self.token:
            self.authenticate()
        return {"Authorization": "Bearer " + self.token}

    def get_json(self, path, label, params=None, missing=False):
        url = f"{self.base}/api/{path.lstrip('/')}"
        for attempt in range(2):
            try:
                return self.http.request(
                    "GET", url, label, headers=self._headers(), params=params
                )
            except HttpStatus as error:
                if error.status == 401 and attempt == 0:
                    self.token = None
                    self.authenticate()
                    continue
                if missing and error.status == 404:
                    return None
                raise

    def get_bytes(self, path, label, params=None):
        url = f"{self.base}/api/{path.lstrip('/')}"
        for attempt in range(2):
            try:
                return self.http.request_bytes(
                    "GET", url, label, headers=self._headers(), params=params
                )
            except HttpStatus as error:
                if error.status == 401 and attempt == 0:
                    self.token = None
                    self.authenticate()
                    continue
                raise

    def search(self, query):
        return (
            self.get_json(
                "Search/search", "kavita search", params={"queryString": query}
            )
            or {}
        )

    def series_volumes(self, series_id):
        return (
            self.get_json(
                "Series/volumes",
                "kavita series volumes",
                params={"seriesId": int(series_id)},
                missing=True,
            )
            or []
        )

    def chapter(self, chapter_id):
        return self.get_json(
            "Chapter",
            "kavita chapter",
            params={"chapterId": int(chapter_id)},
            missing=True,
        )

    def download_chapter(self, chapter_id):
        return self.get_bytes(
            "Download/chapter",
            "kavita download chapter",
            params={"chapterId": int(chapter_id)},
        )


def clean_book_title(text):
    s = (text or "").strip()
    s = BOOK_NOISE_RE.sub(" ", s)
    s = re.sub(r"^\s*\d{1,2}\s+(?=[A-Za-z])", "", s)
    s = re.sub(r"\s+", " ", s).strip(" -–—:,")
    return s


def norm_book_text(text):
    s = clean_book_title(text).lower()
    s = s.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
    s = re.sub(r"'s\b", "s", s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return s


def _strip_leading_article(normed):
    return re.sub(r"^(?:the|a|an)\s+", "", normed).strip()


def _book_core_tokens(normed):
    words = [w for w in normed.split() if w]
    core = [w for w in words if w not in BOOK_STOP_WORDS]
    return core or words


def book_title_variants(title):
    raw = str(title or "")
    cleaned = clean_book_title(raw)
    out = []

    def add(value):
        v = (value or "").strip(" -–—:,")
        if v and v not in out:
            out.append(v)

    add(cleaned)
    no_paren = re.sub(r"\s*\([^)]*\)\s*", " ", cleaned).strip()
    add(no_paren)
    # Handle "Mistborn [01] The Final Empire [01]" -> "The Final Empire"
    m_bracket = re.search(r"\[\s*\d+\s*\]\s*(.+)", raw)
    if m_bracket:
        add(clean_book_title(m_bracket.group(1)))
    # Handle garbled separator in collection titles (e.g. "...Set¿Come As You Are: ...")
    if "¿" in raw:
        after_q = clean_book_title(raw.split("¿", 1)[1])
        add(after_q)
        add(re.split(r"\s*:\s*", after_q, maxsplit=1)[0])
    for sep in (r"\s*:\s*", r"\s+[–—-]\s+", r",\s*book\s+#?\d+.*$", r",\s+how\s+to\s+.*$"):
        parts = re.split(sep, no_paren, maxsplit=1, flags=re.IGNORECASE)
        if parts:
            add(parts[0])
        if len(parts) == 2 and sep in (r"\s*:\s*", r"\s+[–—-]\s+"):
            rhs = parts[1].strip()
            if len(rhs) >= 4 and not re.match(
                r"^(?:book\s*#?\d+|part\s*\d+|a\s+novel|how\s+|why\s+|the\s+quest|"
                r"the\s+art|a\s+practical|dreams\s+)",
                rhs,
                re.IGNORECASE,
            ):
                add(rhs)
    m = re.search(r"(?:book\s*#?\d+|#\d+)\s*[-–—:]\s*(.+)$", cleaned, re.IGNORECASE)
    if m:
        add(m.group(1))
    m2 = re.search(r"([A-Za-z][A-Za-z0-9 ']+?)\s+series\b", cleaned, re.IGNORECASE)
    if m2:
        add(re.sub(r"^(?:the\s+)?[A-Z][a-z]+\s+[A-Z][a-z]+\s+", "", m2.group(1)).strip())
    return out


def file_stem_title(file_path):
    base = posixpath.basename((file_path or "").replace("\\", "/"))
    stem = os.path.splitext(base)[0]
    for _ in range(2):
        stem = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]\s*$", "", stem)
    return clean_book_title(stem)


def author_surnames(author_list):
    out = set()
    skip = {
        "phd",
        "md",
        "jr",
        "sr",
        "ii",
        "iii",
        "graphicaudio",
        "inc",
        "press",
        "publishing",
        "pub",
        "with",
    }
    for name in author_list or ():
        for part in re.split(r"[,&/]|\bwith\b|\band\b", str(name or ""), flags=re.IGNORECASE):
            words = [w.lower() for w in re.findall(r"[A-Za-z]+", part) if w.lower() not in skip]
            if words:
                out.add(words[-1])
    return out


def pick_best_chapter_file(chapter):
    files = [
        f
        for f in (chapter.get("files") or [])
        if isinstance(f, dict)
        and (f.get("extension") or os.path.splitext(f.get("filePath") or "")[1]).lower()
        in EBOOK_EXTENSIONS
    ]
    if not files:
        return None
    rank = {ext: i for i, ext in enumerate(EBOOK_EXTENSIONS)}
    files.sort(
        key=lambda f: (
            rank.get(
                (f.get("extension") or os.path.splitext(f.get("filePath") or "")[1]).lower(),
                9,
            ),
            -(int(f.get("bytes") or 0)),
            str(f.get("filePath") or ""),
        )
    )
    return files[0]


def kavita_file_fingerprint(chapter, file_info=None):
    file_info = file_info or pick_best_chapter_file(chapter) or {}
    return digest(
        [
            chapter.get("id"),
            file_info.get("filePath"),
            file_info.get("bytes"),
            file_info.get("koreaderHash"),
            chapter.get("lastModifiedUtc"),
        ],
        24,
    )


def score_kavita_chapter(abs_title, abs_authors, abs_isbn, chapter):
    best_file = pick_best_chapter_file(chapter)
    if best_file is None:
        return 0.0, None, None

    a_isbn = re.sub(r"[^0-9Xx]", "", str(abs_isbn or ""))
    k_isbn = re.sub(r"[^0-9Xx]", "", str(chapter.get("isbn") or ""))
    if a_isbn and k_isbn and len(a_isbn) >= 10 and a_isbn.lower() == k_isbn.lower():
        return 1.0, "isbn", best_file

    k_titles = []
    for raw in (chapter.get("titleName"), chapter.get("title")):
        if raw and str(raw).strip() != "-100000":
            k_titles.extend(book_title_variants(str(raw)))
    fp = best_file.get("filePath") or ""
    if fp:
        k_titles.extend(book_title_variants(file_stem_title(fp)))
    k_norms = list(dict.fromkeys(n for t in k_titles if (n := norm_book_text(t))))
    a_norms = list(
        dict.fromkeys(n for t in book_title_variants(abs_title) if (n := norm_book_text(t)))
    )
    if not a_norms or not k_norms:
        return 0.0, None, best_file

    a_surnames = author_surnames(abs_authors)
    k_writers = [
        w.get("name")
        for w in (chapter.get("writers") or [])
        if isinstance(w, dict) and w.get("name")
    ]
    k_writer_norms = {norm_book_text(w) for w in k_writers if norm_book_text(w)}
    # Guard: if ABS has no authors and its title is actually just a writer's name (e.g. "Sara Cate"),
    # do not match an arbitrary book by that writer.
    if not a_surnames and a_norms and a_norms[0] in k_writer_norms:
        return 0.0, None, best_file

    k_surnames = author_surnames(k_writers)
    fp_base_lower = posixpath.basename(fp.replace("\\", "/")).lower()
    author_match = bool(
        a_surnames
        and (
            (a_surnames & k_surnames)
            or any(
                re.search(r"\b" + re.escape(s) + r"\b", fp_base_lower)
                for s in a_surnames
                if len(s) >= 3
            )
        )
    )
    author_conflict = bool(a_surnames and k_surnames and not author_match)
    is_graphic = bool(
        re.search(r"\b(?:graphic novel|comic|manga)\b", fp_base_lower + " " + str(chapter.get("titleName") or "").lower())
        and not re.search(r"\b(?:graphic novel|comic|manga)\b", (abs_title or "").lower())
    )

    best = 0.0
    method = None
    for an in a_norms:
        an_s = _strip_leading_article(an)
        a_toks = set(_book_core_tokens(an_s))
        if not a_toks:
            continue
        for kn in k_norms:
            kn_s = _strip_leading_article(kn)
            k_toks = set(_book_core_tokens(kn_s))
            if not k_toks:
                continue
            if an_s == kn_s:
                sc, m = 0.95, "title_exact"
            else:
                inter = a_toks & k_toks
                if not inter:
                    continue
                jacc = len(inter) / len(a_toks | k_toks)
                cov_a = len(inter) / len(a_toks)
                cov_k = len(inter) / len(k_toks)
                if len(a_toks) == 1:
                    if author_match and an_s in k_toks and len(an_s) >= 5:
                        sc, m = 0.78, "title_author"
                    else:
                        continue
                elif cov_a == 1.0 and len(a_toks) >= 2:
                    sc, m = 0.78 + 0.10 * cov_k, "title_subset"
                elif cov_k == 1.0 and len(k_toks) >= 2:
                    sc, m = 0.76 + 0.10 * cov_a, "title_superset"
                else:
                    sc, m = jacc * 0.85, "title_fuzzy"
            if author_match:
                sc = sc + 0.12
                m += "+author"
            elif author_conflict:
                sc -= 0.50 if len(a_toks) == 1 else 0.15
            if is_graphic:
                sc -= 0.25
            if sc > best:
                best = sc
                method = m
    if not a_surnames and best < 0.85:
        best -= 0.15
    return round(min(0.99, max(0.0, best)), 4), method, best_file


def find_kavita_ebook(kavita, title, authors, isbn=None):
    queries = []
    for variant in book_title_variants(title):
        normed = norm_book_text(variant)
        if len(normed) >= 2 and variant not in queries:
            queries.append(variant)
    queries = queries[:5]
    if not queries:
        return None

    candidates = {}
    series_for_chapter = {}
    for q in queries:
        res = kavita.search(q)
        for ch in res.get("chapters") or []:
            if isinstance(ch, dict) and ch.get("id") is not None:
                candidates[ch["id"]] = ch
        if not candidates:
            for s in (res.get("series") or [])[:2]:
                sid = s.get("seriesId") or s.get("id")
                if sid is None:
                    continue
                for vol in kavita.series_volumes(sid):
                    for ch in vol.get("chapters") or []:
                        if isinstance(ch, dict) and ch.get("id") is not None:
                            candidates[ch["id"]] = ch
                            series_for_chapter[ch["id"]] = sid
        else:
            # Record series id when search returned a single unambiguous series.
            series_hits = res.get("series") or []
            if len(series_hits) == 1:
                sid = series_hits[0].get("seriesId") or series_hits[0].get("id")
                for ch in res.get("chapters") or []:
                    if isinstance(ch, dict) and ch.get("id") is not None:
                        series_for_chapter.setdefault(ch["id"], sid)

        best_so_far = max(
            (score_kavita_chapter(title, authors, isbn, c)[0] for c in candidates.values()),
            default=0.0,
        )
        if best_so_far >= 0.95:
            break

    best_hit = None
    for cid, ch in candidates.items():
        sc, method, best_file = score_kavita_chapter(title, authors, isbn, ch)
        if best_file is None:
            continue
        if best_hit is None or sc > best_hit["score"]:
            best_hit = {
                "chapter": ch,
                "file": best_file,
                "score": sc,
                "method": method,
                "series_id": series_for_chapter.get(cid),
            }
    if best_hit is None or best_hit["score"] < 0.72:
        return None
    return best_hit


class EbookHTMLTextParser(HTMLParser):
    """Extract readable plain text and first heading from an XHTML/HTML spine document."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip_depth = 0
        self.heading_tag = None
        self.heading_parts = []
        self.first_heading = None

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t in SKIP_TAGS:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if t == "br":
            self.parts.append("\n")
        elif t in BLOCK_TAGS:
            self.parts.append("\n\n")
        if t in HEADING_TAGS and self.first_heading is None and self.heading_tag is None:
            self.heading_tag = t
            self.heading_parts = []

    def handle_endtag(self, tag):
        t = tag.lower()
        if t in SKIP_TAGS:
            if self.skip_depth:
                self.skip_depth -= 1
            return
        if self.skip_depth:
            return
        if t in BLOCK_TAGS:
            self.parts.append("\n\n")
        if t == self.heading_tag:
            heading = re.sub(r"\s+", " ", "".join(self.heading_parts)).strip()
            if heading:
                self.first_heading = heading[:200]
            self.heading_tag = None
            self.heading_parts = []

    def handle_data(self, data):
        if self.skip_depth:
            return
        self.parts.append(data)
        if self.heading_tag is not None:
            self.heading_parts.append(data)


def clean_extracted_text(text):
    if not text:
        return ""
    text = text.replace("\x00", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\x01-\x08\x0b\x0c\x0e-\x1f\x7f\ufeff]", "", text)
    lines = [re.sub(r"[ \t\u00a0]+", " ", line).strip() for line in text.split("\n")]
    out = []
    blank = False
    for line in lines:
        if not line:
            if out and not blank:
                out.append("")
            blank = True
        else:
            out.append(line)
            blank = False
    return "\n".join(out).strip()


def _html_to_text(raw_bytes):
    text = raw_bytes.decode("utf-8", errors="replace")
    parser = EbookHTMLTextParser()
    parser.feed(text)
    parser.close()
    return parser.first_heading, clean_extracted_text("".join(parser.parts))


def _local_tag(tag):
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _resolve_zip_path(base_dir, href):
    href = urllib.parse.unquote((href or "").split("#", 1)[0].strip())
    if not href:
        return ""
    joined = posixpath.normpath(posixpath.join(base_dir, href)) if base_dir else posixpath.normpath(href)
    return joined.lstrip("/")


def _parse_epub_toc(zf, opf_dir, manifest):
    """Map normalized zip member path -> chapter title from EPUB3 nav or EPUB2 ncx."""
    toc = {}
    nav_href = None
    ncx_href = None
    for _item_id, (href, media_type, props) in manifest.items():
        if "nav" in (props or "").split():
            nav_href = href
        if media_type == "application/x-dtbncx+xml" or href.lower().endswith(".ncx"):
            ncx_href = href
    names_lower = {n.lower(): n for n in zf.namelist()}

    def read_member(rel_href):
        full = _resolve_zip_path(opf_dir, rel_href)
        actual = full if full in zf.namelist() else names_lower.get(full.lower())
        if not actual:
            return None, ""
        try:
            return zf.read(actual), posixpath.dirname(actual)
        except KeyError:
            return None, ""

    if nav_href:
        raw, nav_dir = read_member(nav_href)
        if raw:
            try:
                root = ET.fromstring(raw)
                for el in root.iter():
                    if _local_tag(el.tag) == "a" and el.get("href"):
                        target = _resolve_zip_path(nav_dir, el.get("href"))
                        label = re.sub(r"\s+", " ", "".join(el.itertext())).strip()
                        if target and label:
                            toc.setdefault(target, label[:200])
            except ET.ParseError:
                pass

    if not toc and ncx_href:
        raw, ncx_dir = read_member(ncx_href)
        if raw:
            try:
                root = ET.fromstring(raw)
                for np in root.iter():
                    if _local_tag(np.tag) != "navpoint":
                        continue
                    label = ""
                    src = ""
                    for child in np:
                        lt = _local_tag(child.tag)
                        if lt == "navlabel":
                            label = re.sub(r"\s+", " ", "".join(child.itertext())).strip()
                        elif lt == "content":
                            src = child.get("src") or ""
                    target = _resolve_zip_path(ncx_dir, src)
                    if target and label:
                        toc.setdefault(target, label[:200])
            except ET.ParseError:
                pass
    return toc


def extract_epub_chapters(data):
    """Extract ordered (chapter_title, raw_text) pairs from EPUB bytes (stdlib only)."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        namelist = zf.namelist()
        names_lower = {n.lower(): n for n in namelist}
        spine_paths = []
        toc = {}

        container_name = names_lower.get("meta-inf/container.xml")
        opf_path = None
        if container_name:
            try:
                c_root = ET.fromstring(zf.read(container_name))
                for el in c_root.iter():
                    if _local_tag(el.tag) == "rootfile" and el.get("full-path"):
                        opf_path = el.get("full-path").lstrip("/")
                        break
            except ET.ParseError:
                opf_path = None

        if opf_path and (opf_path in namelist or opf_path.lower() in names_lower):
            actual_opf = opf_path if opf_path in namelist else names_lower[opf_path.lower()]
            opf_dir = posixpath.dirname(actual_opf)
            try:
                opf_root = ET.fromstring(zf.read(actual_opf))
                manifest = {}
                for el in opf_root.iter():
                    if _local_tag(el.tag) == "item" and el.get("id") and el.get("href"):
                        manifest[el.get("id")] = (
                            el.get("href"),
                            (el.get("media-type") or "").lower(),
                            el.get("properties") or "",
                        )
                toc = _parse_epub_toc(zf, opf_dir, manifest)
                for el in opf_root.iter():
                    if _local_tag(el.tag) == "itemref" and el.get("idref") in manifest:
                        href, mtype, _props = manifest[el.get("idref")]
                        if (
                            "html" in mtype
                            or "xml" in mtype
                            or href.lower().endswith((".xhtml", ".html", ".htm"))
                        ):
                            full = _resolve_zip_path(opf_dir, href)
                            actual = full if full in namelist else names_lower.get(full.lower())
                            if actual and actual not in spine_paths:
                                spine_paths.append(actual)
            except ET.ParseError:
                spine_paths = []

        if not spine_paths:
            spine_paths = sorted(
                n
                for n in namelist
                if n.lower().endswith((".xhtml", ".html", ".htm"))
                and not posixpath.basename(n).startswith(".")
            )

        chapters = []
        for path in spine_paths:
            try:
                raw = zf.read(path)
            except KeyError:
                continue
            heading, text = _html_to_text(raw)
            if not text:
                continue
            title = toc.get(path) or heading or posixpath.splitext(posixpath.basename(path))[0]
            chapters.append((title, text))
        return chapters


def _decode_pdf_literal(raw):
    out = []
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i : i + 1]
        if ch == b"\\" and i + 1 < n:
            nxt = raw[i + 1 : i + 2]
            escapes = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f", b"(": b"(", b")": b")", b"\\": b"\\"}
            if nxt in escapes:
                out.append(escapes[nxt])
                i += 2
                continue
            if b"0" <= nxt <= b"7":
                oct_digits = raw[i + 1 : i + 4]
                m = re.match(rb"[0-7]{1,3}", oct_digits)
                if m:
                    out.append(bytes([int(m.group(0), 8) & 0xFF]))
                    i += 1 + len(m.group(0))
                    continue
            i += 2
            continue
        out.append(ch)
        i += 1
    return b"".join(out).decode("latin-1", errors="replace")


def extract_pdf_text_stdlib(data):
    """Best-effort PDF text extraction: pdftotext if installed, else stdlib stream parser."""
    pdftotext = shutil.which("pdftotext")
    if pdftotext:
        try:
            proc = subprocess.run(
                [pdftotext, "-enc", "UTF-8", "-", "-"],
                input=data,
                capture_output=True,
                timeout=60,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                text = clean_extracted_text(proc.stdout.decode("utf-8", errors="replace"))
                if text:
                    return [("Full Text", text)]
        except (OSError, subprocess.SubprocessError):
            pass

    # Stdlib fallback: decompress FlateDecode streams and collect Tj/TJ literal strings.
    chunks = []
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, re.DOTALL):
        raw_stream = m.group(1)
        try:
            content = zlib.decompress(raw_stream)
        except zlib.error:
            content = raw_stream
        if b"BT" not in content:
            continue
        for bt in re.finditer(rb"BT(.*?)ET", content, re.DOTALL):
            block = bt.group(1)
            line_parts = []
            for tok in re.finditer(rb"\((?:\\.|[^\\()])*\)\s*Tj|\[(.*?)\]\s*TJ", block, re.DOTALL):
                if tok.group(1) is not None:
                    for lit in re.finditer(rb"\(((?:\\.|[^\\()])*)\)", tok.group(1)):
                        line_parts.append(_decode_pdf_literal(lit.group(1)))
                else:
                    lit = re.match(rb"\(((?:\\.|[^\\()])*)\)", tok.group(0))
                    if lit:
                        line_parts.append(_decode_pdf_literal(lit.group(1)))
            if line_parts:
                chunks.append("".join(line_parts))
    cleaned = clean_extracted_text("\n\n".join(chunks))
    return [("Full Text", cleaned)] if cleaned else []


def extract_ebook_chapters(data, filename_hint="", extension_hint=""):
    """Extract (book_format, [(chapter_title, text), ...]) from downloaded ebook bytes."""
    ext = (extension_hint or os.path.splitext(filename_hint or "")[1]).lower()
    if data[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            has_container = any(n.lower() == "meta-inf/container.xml" for n in names)
            if not has_container:
                # Multi-file Kavita chapter downloads wrap individual ebook files in a ZIP.
                nested = [
                    n
                    for n in names
                    if os.path.splitext(n)[1].lower() in EBOOK_EXTENSIONS
                    and not n.endswith("/")
                ]
                if nested:
                    hint_base = posixpath.basename((filename_hint or "").replace("\\", "/")).lower()
                    rank = {e: i for i, e in enumerate(EBOOK_EXTENSIONS)}
                    nested.sort(
                        key=lambda n: (
                            0 if hint_base and posixpath.basename(n).lower() == hint_base else 1,
                            rank.get(os.path.splitext(n)[1].lower(), 9),
                            -zf.getinfo(n).file_size,
                            n,
                        )
                    )
                    chosen = nested[0]
                    return extract_ebook_chapters(
                        zf.read(chosen),
                        filename_hint=chosen,
                        extension_hint=os.path.splitext(chosen)[1].lower(),
                    )
        return "epub", extract_epub_chapters(data)
    if ext == ".pdf" or data[:5] == b"%PDF-":
        return "pdf", extract_pdf_text_stdlib(data)
    if ext == ".txt":
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("latin-1", errors="replace")
        cleaned = clean_extracted_text(text)
        return "txt", ([("Full Text", cleaned)] if cleaned else [])
    raise ValueError(f"unsupported ebook format {ext or 'unknown'}")


def _split_long_block(text, max_chars):
    """Split one block > max_chars at sentence boundaries, else whitespace, else hard slice."""
    if len(text) <= max_chars:
        return [text]
    sentences = re.split(r"(?<=[.!?])\s+", text)
    if len(sentences) > 1:
        packed, buf = [], ""
        for sent in sentences:
            if not sent:
                continue
            if len(sent) > max_chars:
                if buf:
                    packed.append(buf)
                    buf = ""
                packed.extend(_split_words(sent, max_chars))
            elif not buf:
                buf = sent
            elif len(buf) + 1 + len(sent) <= max_chars:
                buf = buf + " " + sent
            else:
                packed.append(buf)
                buf = sent
        if buf:
            packed.append(buf)
        return packed
    return _split_words(text, max_chars)


def _split_words(text, max_chars):
    parts = []
    rest = text.strip()
    while len(rest) > max_chars:
        cut = rest.rfind(" ", 0, max_chars + 1)
        if cut < max_chars // 2:
            cut = max_chars
        piece = rest[:cut].strip()
        if piece:
            parts.append(piece)
        rest = rest[cut:].strip()
    if rest:
        parts.append(rest)
    return parts


def chunk_book_chapters(chapters, max_chars=8000):
    """Split [(chapter_title, text), ...] into ordered chunks <= max_chars."""
    max_chars = max(500, int(max_chars))
    raw_chunks = []
    for chapter_index, (chapter_title, text) in enumerate(chapters, 1):
        cleaned = clean_extracted_text(text)
        if not cleaned:
            continue
        paragraphs = [p.strip() for p in cleaned.split("\n\n") if p.strip()]
        chapter_parts = []
        buf = ""
        for para in paragraphs:
            if len(para) > max_chars:
                if buf:
                    chapter_parts.append(buf)
                    buf = ""
                chapter_parts.extend(_split_long_block(para, max_chars))
            elif not buf:
                buf = para
            elif len(buf) + 2 + len(para) <= max_chars:
                buf = buf + "\n\n" + para
            else:
                chapter_parts.append(buf)
                buf = para
        if buf:
            chapter_parts.append(buf)
        part_count = len(chapter_parts)
        for part_index, chunk_text in enumerate(chapter_parts, 1):
            raw_chunks.append(
                {
                    "chapter_index": chapter_index,
                    "chapter_title": chapter_title,
                    "part_index": part_index,
                    "part_count": part_count,
                    "text": chunk_text,
                    "char_count": len(chunk_text),
                    "word_count": len(chunk_text.split()),
                }
            )
    total = len(raw_chunks)
    for chunk_index, chunk in enumerate(raw_chunks, 1):
        chunk["chunk_index"] = chunk_index
        chunk["chunk_count"] = total
    return raw_chunks


def collect_audiobook_texts(fetcher, abs_get, me, user, items_cache, books_state):
    """Fetch and chunk matching Kavita ebooks for audiobooks > threshold (or finished)."""
    config = fetcher.config
    kavita = KavitaClient(
        fetcher.http,
        config.get("kavita_url"),
        config.get("kavita_api_key"),
        runtime_secrets=fetcher._runtime_secrets,
    )
    since_ms = ms(config.since) if config.since else 0
    until_ms = ms(config.until) if config.until else None
    downloads = 0
    lookups = 0

    candidates = sorted(
        (
            p
            for p in (me.get("mediaProgress") or [])
            if isinstance(p, dict)
            and p.get("libraryItemId")
            and not p.get("episodeId")
            and (
                float(p.get("progress") or 0) > config.book_min_progress
                or bool(p.get("isFinished"))
            )
        ),
        key=lambda p: (str(p.get("libraryItemId")), str(p.get("id") or "")),
    )

    for progress in candidates:
        item_id = str(progress["libraryItemId"])
        updated = int(progress.get("lastUpdate") or 0)
        started = int(progress.get("startedAt") or updated)
        if since_ms and updated and updated < since_ms:
            continue
        if until_ms and started and started >= until_ms:
            continue

        saved = books_state.get(item_id)
        if (
            isinstance(saved, dict)
            and saved.get("extractor_version") == BOOK_EXTRACTOR_VERSION
            and saved.get("chapter_id") is not None
            and saved.get("file_hash")
        ):
            existing_ch = kavita.chapter(saved["chapter_id"])
            if (
                isinstance(existing_ch, dict)
                and kavita_file_fingerprint(existing_ch) == saved["file_hash"]
            ):
                continue

        if item_id not in items_cache:
            if lookups >= config.abs_item_lookups:
                raise LimitReached("audiobookshelf item lookup limit reached")
            lookups += 1
            try:
                items_cache[item_id] = abs_get(
                    f"items/{urllib.parse.quote(item_id)}",
                    "audiobookshelf item",
                )
            except HttpStatus as error:
                if error.status != 404:
                    raise
                items_cache[item_id] = {}

        item = items_cache.get(item_id) or {}
        if item.get("mediaType") != "book":
            continue
        media = item.get("media") or {}
        meta = media.get("metadata") or {}
        title = meta.get("title") or ""
        if not title:
            continue
        authors = [
            a.get("name")
            for a in (meta.get("authors") or [])
            if isinstance(a, dict) and a.get("name")
        ]
        if not authors and meta.get("authorName"):
            authors = [meta["authorName"]]
        isbn = meta.get("isbn")

        match = find_kavita_ebook(kavita, title, authors, isbn=isbn)
        if match is None:
            fetcher.notes["kavita_unmatched_books"] = (
                fetcher.notes.get("kavita_unmatched_books", 0) + 1
            )
            continue

        if config.book_max_downloads and downloads >= config.book_max_downloads:
            raise LimitReached("kavita book download limit reached")

        ch = match["chapter"]
        best_file = match["file"]
        file_hash = kavita_file_fingerprint(ch, best_file)
        if (
            isinstance(saved, dict)
            and saved.get("extractor_version") == BOOK_EXTRACTOR_VERSION
            and saved.get("file_hash") == file_hash
        ):
            continue

        downloads += 1
        try:
            raw_bytes, _headers = kavita.download_chapter(ch["id"])
            book_format, chapters = extract_ebook_chapters(
                raw_bytes,
                filename_hint=best_file.get("filePath") or "",
                extension_hint=best_file.get("extension") or "",
            )
        except (HttpStatus, zipfile.BadZipFile, ET.ParseError, ValueError) as error:
            if isinstance(error, HttpStatus) and error.status not in (400, 404):
                raise
            fetcher.notes["kavita_extract_failed"] = (
                fetcher.notes.get("kavita_extract_failed", 0) + 1
            )
            continue

        chunks = chunk_book_chapters(chapters, max_chars=config.book_chunk_chars)
        if not chunks:
            fetcher.notes["kavita_extract_failed"] = (
                fetcher.notes.get("kavita_extract_failed", 0) + 1
            )
            continue

        series_name = meta.get("seriesName") or names(meta.get("series"))
        anchor_ms = int(
            progress.get("startedAt")
            or progress.get("finishedAt")
            or progress.get("lastUpdate")
            or 0
        )
        prog_val = float(progress.get("progress") or 0.0)
        is_fin = bool(progress.get("isFinished"))
        for chunk in chunks:
            raw_chunk = {
                "abs_item_id": item_id,
                "title": title,
                "authors": authors,
                "series": series_name,
                "book_format": book_format,
                "anchor_ms": anchor_ms or None,
                "progress": round(prog_val, 4),
                "is_finished": is_fin,
                "kavita_series_id": match.get("series_id"),
                "kavita_chapter_id": ch["id"],
                "kavita_file_path": best_file.get("filePath"),
                "kavita_file_hash": file_hash,
                "match_score": match["score"],
                "match_method": match["method"],
                **chunk,
            }
            yield record(
                "audiobook.text_chunk",
                f"{item_id}:chunk:{chunk['chunk_index']:04d}",
                raw_chunk,
                {"user": user},
            )
        if not config.bounded:
            books_state[item_id] = {
                "chapter_id": ch["id"],
                "file_hash": file_hash,
                "extractor_version": BOOK_EXTRACTOR_VERSION,
                "chunks": len(chunks),
                "title": title,
            }


# ---------------------------------------------------------------- YouTube


def youtube_video_id(url):
    parts = urllib.parse.urlsplit(url or "")
    if parts.hostname == "youtu.be":
        return parts.path.strip("/").split("/")[0] or None
    if parts.hostname not in {
        "youtube.com",
        "www.youtube.com",
        "music.youtube.com",
        "m.youtube.com",
    }:
        return None
    if parts.path.startswith(("/shorts/", "/embed/", "/live/")):
        return parts.path.split("/")[2] or None
    return urllib.parse.parse_qs(parts.query).get("v", [None])[0]


def youtube_channel_id(url):
    path = urllib.parse.urlsplit(url or "").path
    return path.split("/")[2] if path.startswith("/channel/") else None


MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "jan",
            "feb",
            "mar",
            "apr",
            "may",
            "jun",
            "jul",
            "aug",
            "sep",
            "oct",
            "nov",
            "dec",
        ],
        1,
    )
}


def takeout_time(value):
    """Takeout timestamps: ISO, or English month dates with UTC or a numeric offset."""
    text = (value or "").strip().replace(" ", " ").replace(" ", " ")
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        match = re.fullmatch(
            r"([A-Za-z]{3,9})\.? (\d{1,2}), (\d{4}),? (\d{1,2}):(\d{2})(?::(\d{2}))?\s*([AaPp][Mm])?\s*(UTC|GMT|[+-]\d{2}:?\d{2})",
            text,
        )
        if not match or match[1][:3].lower() not in MONTHS:
            raise ValueError(
                "Unsupported Takeout date; export history as JSON"
            ) from None
        hour = (
            int(match[4]) % 12 + (12 if match[7] and match[7].lower() == "pm" else 0)
            if match[7]
            else int(match[4])
        )
        zone = match[8]
        offset = timedelta(0)
        if zone not in ("UTC", "GMT"):
            sign = 1 if zone[0] == "+" else -1
            digits = zone[1:].replace(":", "")
            offset = sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
        moment = datetime(
            int(match[3]),
            MONTHS[match[1][:3].lower()],
            int(match[2]),
            hour,
            int(match[5]),
            int(match[6] or 0),
            tzinfo=timezone(offset),
        )
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("Takeout date has no timezone")
    return moment.isoformat()


def takeout_json_rows(text):
    rows = json.loads(text)
    if not isinstance(rows, list):
        raise FetchError("watch-history.json must contain an array")
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = row.get("titleUrl") or ""
        # Searches and other activity are not views.
        if "/results?" in url or str(row.get("header", "")).lower() == "search":
            continue
        title = row.get("title") or "Unavailable video"
        if not title.startswith("Watched ") and not url:
            continue
        if title.startswith("Watched "):
            title = title[8:]
        subtitle = (row.get("subtitles") or [{}])[0] or {}
        parsed = {
            "video_id": youtube_video_id(url),
            "title": title,
            "channel": subtitle.get("name"),
            "channel_id": youtube_channel_id(subtitle.get("url")),
            "url": url or None,
            "at": takeout_time(row.get("time")),
            "source": "takeout",
        }
        for flag in ("is_short", "is_music", "is_ad"):
            if isinstance(row.get(flag), bool):
                parsed[flag] = row[flag]
        if "/shorts/" in url:
            parsed["is_short"] = True
        if (
            urllib.parse.urlsplit(url).hostname == "music.youtube.com"
            or row.get("header") == "YouTube Music"
        ):
            parsed["is_music"] = True
        if any("google ads" in str(d).lower() for d in row.get("details") or []):
            parsed["is_ad"] = True
        yield row, parsed


class TakeoutCells(HTMLParser):
    """Collect each outer-cell's links and text lines from watch-history.html."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cells = []
        self.depth = 0
        self.links = []
        self.parts = []
        self.anchor = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "div" and (
            self.depth or "content-cell" in (attrs.get("class") or "").split()
        ):
            self.depth += 1
        if not self.depth:
            return
        if tag == "a":
            self.anchor = [attrs.get("href") or "", ""]
        elif tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if not self.depth:
            return
        if tag == "a" and self.anchor:
            self.links.append(self.anchor)
            self.anchor = None
        if tag == "div":
            self.depth -= 1
            if not self.depth:
                lines = [
                    x.strip() for x in "".join(self.parts).splitlines() if x.strip()
                ]
                self.cells.append((self.links, lines))
                self.links, self.parts = [], []

    def handle_data(self, data):
        if self.depth:
            self.parts.append(data)
            if self.anchor is not None:
                self.anchor[1] += data


def takeout_html_rows(text):
    parser = TakeoutCells()
    parser.feed(text)
    for links, lines in parser.cells:
        video = next((x for x in links if youtube_video_id(x[0])), None)
        if not video and not any(line.startswith("Watched") for line in lines):
            continue
        at = None
        for line in reversed(lines):
            try:
                at = takeout_time(line)
                break
            except ValueError:
                continue
        if at is None:
            raise FetchError(
                "Cannot parse a watch-history.html time; export history as JSON"
            )
        channel = next((x for x in links if youtube_channel_id(x[0])), [None, None])
        raw = {
            "links": [list(x) for x in links],
            "lines": [html.unescape(x) for x in lines],
        }
        yield (
            raw,
            {
                "video_id": youtube_video_id(video[0]) if video else None,
                "title": video[1] if video else "Unavailable video",
                "channel": channel[1],
                "channel_id": youtube_channel_id(channel[0]),
                "url": video[0] if video else None,
                "at": at,
                "source": "takeout",
            },
        )


def plugin_video_id(url):
    """The youtube plugin's video ID rule (no /live/ URLs), used only for IDs."""
    if urllib.parse.urlsplit(url or "").path.startswith("/live/"):
        return None
    return youtube_video_id(url)


def takeout_identity(parsed, raw, occurrences):
    """The Throughline youtube plugin's Takeout watch ID, so both plugins agree."""
    video = plugin_video_id(parsed["url"])
    base = digest([video, parsed["at"], raw.get("title") if not video else None])
    occurrences[base] = occurrences.get(base, 0) + 1
    return f"takeout:watch:{base}:{occurrences[base]}"


def file_marks(paths):
    return {str(p): f"{p.stat().st_size}:{p.stat().st_mtime_ns}" for p in paths}


def collect_youtube(fetcher, state):
    config = fetcher.config
    if config.get("youtube_takeout"):
        root = Path(config.get("youtube_takeout"))
        if root.is_file():
            history = [root]
        elif root.is_dir():
            history = sorted(
                p
                for p in root.rglob("watch-history.*")
                if p.suffix.lower() in (".json", ".html")
            )
        else:
            raise FetchError("CONSUMPTION_YOUTUBE_TAKEOUT does not exist")
        # Prefer JSON when both variants exist so views are not doubled.
        if any(p.suffix.lower() == ".json" for p in history):
            history = [p for p in history if p.suffix.lower() == ".json"]
        marks = file_marks(history)
        if state.get("takeout") != marks:
            occurrences = {}
            for path in history:
                text = path.read_text(encoding="utf-8-sig")
                rows = (
                    takeout_json_rows(text)
                    if path.suffix.lower() == ".json"
                    else takeout_html_rows(text)
                )
                for raw, parsed in rows:
                    ident = takeout_identity(parsed, raw, occurrences)
                    if in_bounds(known_time(parsed["at"]), config):
                        yield record("youtube.watch", ident, raw, {"parsed": parsed})
            if not config.bounded and not config.since:
                state["takeout"] = marks
    if config.get("youtube_messages"):
        root = Path(config.get("youtube_messages"))
        paths = (
            [root]
            if root.is_file()
            else sorted(
                p for p in root.rglob("*") if p.suffix.lower() in (".json", ".jsonl")
            )
        )
        marks = file_marks(paths)
        if state.get("messages") != marks:
            for path in paths:
                text = path.read_text(encoding="utf-8-sig")
                rows = (
                    [json.loads(line) for line in text.splitlines() if line.strip()]
                    if path.suffix.lower() == ".jsonl"
                    else json.loads(text)
                )
                for row in rows if isinstance(rows, list) else [rows]:
                    parsed = youtube_message(row)
                    if parsed and in_bounds(known_time(parsed["at"]), config):
                        yield record(
                            "youtube.watch",
                            parsed.pop("external_id"),
                            row,
                            {"parsed": parsed},
                        )
            if not config.bounded and not config.since:
                state["messages"] = marks
    if config.get("youtube_watcher"):
        yield from collect_youtube_watcher(config, state)


WATCHER_FORMAT = "throughline-youtube-watcher-export"


def read_watcher_export(path):
    """(header, records) of a youtube watcher export; refuses a partial file."""
    opener = gzip.open if path.name.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as handle:
        lines = [json.loads(line) for line in handle if line.strip()]
    if not lines or lines[0].get("format") != WATCHER_FORMAT:
        raise FetchError(f"{path.name} is not a YouTube watcher export")
    if lines[-1].get("kind") != "export.footer" or not lines[-1].get("complete"):
        raise FetchError(f"{path.name} is incomplete (no footer)")
    return lines[0], lines[1:-1]


def collect_youtube_watcher(config, state):
    """Viewing sessions and history days from the youtube watcher's exports.

    A session is one sitting with a video (progress before and after); a
    history day says the video was watched on that day (the backfill, and
    the summary of that day's sessions). Later exports repeat history days
    unchanged, so each id is yielded once per run, the last copy winning.
    """
    root = Path(config.get("youtube_watcher"))
    if root.is_file():
        paths = [root]
    elif root.is_dir():
        paths = sorted(
            p
            for p in root.rglob("*")
            if p.is_file() and p.name.endswith((".jsonl", ".jsonl.gz"))
        )
    else:
        raise FetchError("CONSUMPTION_YOUTUBE_WATCHER does not exist")
    marks = file_marks(paths)
    done = state.setdefault("watcher_files", {})
    changed = [path for path in paths if done.get(str(path)) != marks[str(path)]]
    if not changed:
        return
    latest = {}
    for path in changed:
        header, rows = read_watcher_export(path)
        for row in rows:
            if row.get("kind") not in ("session", "history_item"):
                continue
            raw = row["raw"]
            kind = (
                "youtube.session" if row["kind"] == "session" else "youtube.history_day"
            )
            at = raw.get("start_est") if kind == "youtube.session" else raw.get("day")
            if at and not in_bounds(known_time(at), config):
                continue
            latest[(kind, row["id"])] = record(
                kind, row["id"], raw, {"tz": header.get("tz") or "UTC"}
            )
    yield from latest.values()
    if not config.bounded and not config.since:
        done.update({str(path): marks[str(path)] for path in changed})
        state.pop("watcher", None)


def youtube_message(row):
    """A youtube.watch message (wire, API row or typed view row) as parsed fields."""
    if not isinstance(row, dict) or not row.get("external_id"):
        return None
    if row.get("channel") not in (None, "youtube.watch"):
        return None
    fields = row.get("fields") if isinstance(row.get("fields"), dict) else row
    if not (fields.get("video_id") or fields.get("video_title")) and not str(
        row["external_id"]
    ).startswith(("takeout:watch:", "ytdlp:watch:")):
        return None
    at = row.get("happened_at") or row.get("record_time")
    parsed = {
        "external_id": str(row["external_id"]),
        "video_id": fields.get("video_id"),
        "title": fields.get("video_title")
        or fields.get("title")
        or "Unavailable video",
        "channel": fields.get("channel_title"),
        "channel_id": fields.get("youtube_channel_id"),
        "url": fields.get("url"),
        "at": iso(parse_time(at)) if at else None,
        "source": "youtube_plugin",
        "time_precision": fields.get("time_precision"),
    }
    for flag in ("is_short", "is_music", "is_ad"):
        if isinstance(fields.get(flag), bool):
            parsed[flag] = fields[flag]
    return parsed


# ---------------------------------------------------------------- orchestration

COLLECTORS = {
    "spotify": collect_spotify,
    "spotify_backup": collect_spotify_backup,
    "plex": collect_plex,
    "audiobookshelf": collect_audiobookshelf,
    "youtube": collect_youtube,
}


class Fetcher:
    """Runs the configured collectors. `state` is updated only by work that completed."""

    def __init__(self, config, state=None, sleep=time.sleep, now=None):
        self.config = config
        self.state = copy.deepcopy(state or {})
        self.now = now or utc_now()
        self.budget = Budget(
            config.max_requests,
            config.max_requests_per_day,
            self.state,
            self.now.date(),
        )
        self.http = Http(self.budget, config.delay_s, sleep)
        self.counts = Counter()
        self.stopped = {}
        self.errors = {}
        self.notes = {}
        self._runtime_secrets = []
        self.fetched_at = iso(self.now)

    def services(self):
        return self.config.configured()

    def run(self):
        seen = set()
        for service in self.services():
            working = copy.deepcopy(self.state.get(service, {}))
            collector = COLLECTORS[service](self, working)
            completed = False
            exported = 0
            try:
                for item in collector:
                    key = (item["kind"], item["id"])
                    if key in seen:
                        continue
                    seen.add(key)
                    item["fetched_at"] = self.fetched_at
                    self.counts[item["kind"]] += 1
                    exported += 1
                    yield item
                    if self.config.max_items and exported >= self.config.max_items:
                        self.stopped[service] = "per-run item limit reached"
                        break
                else:
                    completed = True
            except LimitReached as error:
                self.stopped[service] = str(error)
            except (FetchError, ValueError, OSError) as error:
                self.errors[service] = redact(
                    str(error),
                    [*self.config.secrets.values(), *self._runtime_secrets],
                )
            except Exception as error:  # noqa: BLE001 - an unexpected shape fails one service, not the export
                self.errors[service] = redact(
                    f"unexpected {type(error).__name__}: {error}",
                    [*self.config.secrets.values(), *self._runtime_secrets],
                )
            finally:
                collector.close()
                # Collectors only write state for work that is safe to skip next
                # time, so it is kept whether or not the service finished.
                if completed or service in self.stopped or service in self.errors:
                    self.state[service] = working

    def footer(self):
        return {
            "kind": "export.footer",
            "complete": True,
            "counts": dict(sorted(self.counts.items())),
            "records": sum(self.counts.values()),
            "requests": self.budget.used,
            "stopped": self.stopped,
            "errors": self.errors,
            "notes": self.notes,
        }

    def header(self):
        return {
            "kind": "export.header",
            "format": FORMAT,
            "version": FORMAT_VERSION,
            "fetcher_version": FETCHER_VERSION,
            "fetched_at": self.fetched_at,
            "services": self.services(),
            "since": iso(self.config.since) if self.config.since else None,
            "until": iso(self.config.until) if self.config.until else None,
        }


def write_json_atomic(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".partial")
    temporary.write_text(json.dumps(value, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def export(fetcher, out, state_path=None, stream=None):
    """Write header, records and footer; the state file moves only after success."""

    def lines():
        yield fetcher.header()
        yield from fetcher.run()
        yield fetcher.footer()

    if out == "-":
        handle = stream or sys.stdout
        for item in lines():
            handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
    else:
        target = Path(out)
        temporary = target.with_name(target.name + ".partial")
        with open(temporary, "wb") as raw:
            # A fixed gzip header (name, mtime) keeps identical exports identical.
            packed = (
                gzip.GzipFile(
                    filename=target.name[:-3], mode="wb", fileobj=raw, mtime=0
                )
                if target.suffix == ".gz"
                else None
            )
            handle = io.TextIOWrapper(packed or raw, encoding="utf-8", newline="\n")
            for item in lines():
                handle.write(
                    json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n"
                )
            handle.flush()
            handle.detach()
            if packed:
                packed.close()
        os.replace(temporary, target)
    if state_path:
        write_json_atomic(state_path, fetcher.state)
    return fetcher.footer()


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="fetch.py",
        description="Export media consumption records as JSONL. Credentials come only from environment variables.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("export", help="fetch and write an export file")
    run.add_argument(
        "--out", required=True, help="output .jsonl or .jsonl.gz path, or - for stdout"
    )
    run.add_argument(
        "--state", help="JSON state file for incremental runs (no secrets are stored)"
    )
    run.add_argument(
        "--services", help="comma separated subset of " + ",".join(SERVICES)
    )
    run.add_argument("--since", help="lower bound, ISO date or timestamp (inclusive)")
    run.add_argument("--until", help="upper bound, ISO date or timestamp (exclusive)")
    run.add_argument("--max-requests", type=int)
    run.add_argument("--max-requests-per-day", type=int)
    run.add_argument("--max-items", type=int)
    run.add_argument("--delay", type=float, help="minimum seconds between requests")
    run.add_argument("--spotify-backup-dir")
    run.add_argument("--youtube-takeout")
    run.add_argument("--youtube-messages")
    commands.add_parser(
        "services", help="list the services configured by the environment"
    )
    args = parser.parse_args(argv)
    secrets = [os.environ.get(ENV[k][0]) for k in SECRET_KEYS]
    try:
        if args.command == "services":
            config = Config()
            print(json.dumps({"configured": config.configured()}))
            return 0
        overrides = {
            "services": args.services,
            "since": args.since,
            "until": args.until,
            "max_requests": None
            if args.max_requests is None
            else str(args.max_requests),
            "max_requests_per_day": None
            if args.max_requests_per_day is None
            else str(args.max_requests_per_day),
            "max_items": None if args.max_items is None else str(args.max_items),
            "delay_s": None if args.delay is None else str(args.delay),
            "spotify_backup_dir": args.spotify_backup_dir,
            "youtube_takeout": args.youtube_takeout,
            "youtube_messages": args.youtube_messages,
        }
        config = Config(**overrides)
        state = {}
        if args.state and Path(args.state).exists():
            state = json.loads(Path(args.state).read_text(encoding="utf-8"))
        fetcher = Fetcher(config, state)
        footer = export(fetcher, args.out, args.state)
    except Exception as error:  # noqa: BLE001 - report safely, never a traceback with headers
        print(
            "fetch failed: " + redact(f"{type(error).__name__}: {error}", secrets),
            file=sys.stderr,
        )
        return 1
    summary = {
        k: footer[k] for k in ("records", "counts", "requests", "stopped", "errors")
    }
    print(json.dumps(summary, sort_keys=True), file=sys.stderr)
    return 2 if footer["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
