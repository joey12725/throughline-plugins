#!/usr/bin/env python3
"""Gmail through Composio, standard library only (Python 3.12+).

    python fetch.py export --out FILE [--account ID ...] [--state FILE] [options]

Reads one or more Gmail accounts connected in Composio and writes raw records
as JSON Lines, one object per line:

    {"kind": "gmail.message", "account": "<mailbox address>",
     "account_ref": "<Composio account>", "id": "<Gmail message id>",
     "thread_id": "...", "fetched_at": "...", "transport": "rest",
     "labels": {"Label_1": "Receipts"}, "raw": {<message as Composio returned it>}}

Other kinds: "gmail.profile" (once per account at backfill start),
"gmail.deleted" (a deletion reported by Gmail history) and "export.meta"
(one per export run). The Throughline email plugin maps these records; it can
also run this module in-process (EMAIL_SOURCE=live).

Transports: "rest" (default) calls Composio v3 tools execute
(POST {COMPOSIO_BASE_URL}/api/v3/tools/execute/{TOOL}) with COMPOSIO_API_KEY
and a connected account id. "mcp" calls COMPOSIO_MULTI_EXECUTE_TOOL through
the Composio MCP URL (COMPOSIO_MCP_URL, optional COMPOSIO_MCP_TOKEN); that
proxy truncates lists longer than two items, so it pages at two messages and
marks records whose nested lists were cut ("truncated": true).

Credentials are read only from environment variables, never from argv, and
are never printed or logged. Redirects are refused so a key header is never
re-sent to another host.

Sync model, per account, kept in a state dict (--state FILE for exports, the
plugin cursor in live mode):
  1. backfill: newest first, bounded by --since/--until and --max-messages,
     one checkpoint per page; the profile history id is taken first;
  2. incremental: Gmail history from that id (new messages, label changes,
     deletions), each changed message fetched by id; when the history id has
     expired (or --incremental date) a date query after the newest message
     seen, minus an overlap window.
Every Composio execution counts against per-run and per-day call limits; a
run that reaches a limit stops at its last checkpoint and the next run
resumes there. Calls are paced (--min-interval) and 429/5xx/transport errors
are retried with exponential backoff honouring Retry-After.
"""

from __future__ import annotations

import argparse
import copy
import http.client
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

VERSION = 1
LOG = logging.getLogger("throughline_email.fetch")
DEFAULT_BASE_URL = "https://backend.composio.dev"
MCP_PAGE_SIZE = 2
TOOLS = {
    "profile": "GMAIL_GET_PROFILE",
    "labels": "GMAIL_LIST_LABELS",
    "list": "GMAIL_FETCH_EMAILS",
    "get": "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID",
    "history": "GMAIL_LIST_HISTORY",
}
MCP_TOOL = "COMPOSIO_MULTI_EXECUTE_TOOL"

# (key, environment variable, default, description). Shared with the plugin
# manifest so the CLI and the plugin read the same names.
SETTINGS = [
    (
        "accounts",
        "EMAIL_ACCOUNTS",
        None,
        "Comma-separated Composio connected account ids (REST) or account names (MCP)",
    ),
    ("transport", "EMAIL_TRANSPORT", "rest", "rest (Composio v3 API) or mcp"),
    ("base_url", "COMPOSIO_BASE_URL", DEFAULT_BASE_URL, "Composio REST base URL"),
    (
        "user_id",
        "COMPOSIO_USER_ID",
        None,
        "Optional Composio user id sent with REST executions",
    ),
    (
        "toolkit_version",
        "COMPOSIO_TOOLKIT_VERSION",
        None,
        "Optional Gmail toolkit version sent with REST executions",
    ),
    (
        "since",
        "EMAIL_SINCE",
        None,
        "Backfill lower bound: YYYY-MM-DD (UTC day) or ISO timestamp with offset",
    ),
    (
        "until",
        "EMAIL_UNTIL",
        None,
        "Backfill upper bound: YYYY-MM-DD (inclusive UTC day) or ISO timestamp",
    ),
    ("query", "EMAIL_QUERY", "", "Extra Gmail search filter added to every listing"),
    (
        "max_messages",
        "EMAIL_MAX_MESSAGES",
        "1000",
        "Backfill cap per account, newest first; 0 means no cap",
    ),
    (
        "page_size",
        "EMAIL_PAGE_SIZE",
        "25",
        "Messages per list call, 1..500 (MCP always uses 2)",
    ),
    (
        "incremental",
        "EMAIL_INCREMENTAL",
        "auto",
        "auto (history id, date fallback), history, or date",
    ),
    (
        "overlap_s",
        "EMAIL_OVERLAP_S",
        "86400",
        "Date-mode re-read window before the newest message seen, seconds",
    ),
    (
        "max_calls_per_run",
        "EMAIL_MAX_CALLS_PER_RUN",
        "500",
        "Composio executions per run; 0 means unlimited",
    ),
    (
        "max_calls_per_day",
        "EMAIL_MAX_CALLS_PER_DAY",
        "5000",
        "Composio executions per UTC day for this state; 0 means unlimited",
    ),
    (
        "min_interval_s",
        "EMAIL_MIN_INTERVAL_S",
        "1.0",
        "Minimum seconds between Composio requests",
    ),
    (
        "retry_base_s",
        "EMAIL_RETRY_BASE_S",
        "2.0",
        "First retry delay in seconds, doubled per attempt (capped at 60)",
    ),
    (
        "max_retries",
        "EMAIL_MAX_RETRIES",
        "5",
        "Retries on 429, 5xx and transport errors",
    ),
    ("timeout_s", "EMAIL_TIMEOUT_S", "60", "HTTP timeout per request, seconds"),
    (
        "include_spam_trash",
        "EMAIL_INCLUDE_SPAM_TRASH",
        "false",
        "Include spam and trash in listings (spam is skipped otherwise)",
    ),
]
SECRETS = [
    ("api_key", "COMPOSIO_API_KEY", "Composio API key (REST transport)"),
    (
        "mcp_url",
        "COMPOSIO_MCP_URL",
        "Composio MCP URL (MCP transport; it embeds a token, treat as secret)",
    ),
    ("mcp_token", "COMPOSIO_MCP_TOKEN", "Optional bearer token for the MCP URL"),
]


class FetchError(RuntimeError):
    """A source failure, described without credentials or response bodies."""


class ToolError(FetchError):
    """Composio executed the tool and reported a failure."""


class LimitReached(Exception):
    """A per-run or per-day call limit; the run stops at its last checkpoint."""


def utc_now():
    return datetime.now(UTC)


def iso(at):
    # Fixed width (always microseconds), so stamps also order correctly as text.
    return at.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_bound(value, end=False):
    """Epoch seconds for a bound: a UTC day (end: the following midnight) or
    an ISO timestamp with offset. None when unset."""
    if value is None or not str(value).strip():
        return None
    value = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        day = date.fromisoformat(value)
        at = datetime(day.year, day.month, day.day, tzinfo=UTC)
        if end:
            at += timedelta(days=1)
    else:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if at.utcoffset() is None:
            raise ValueError("backfill bounds need a UTC offset or a YYYY-MM-DD day")
    return int(at.timestamp())


def message_ms(raw):
    """Gmail internal date in epoch milliseconds, or None."""
    value = raw.get("internalDate")
    if value not in (None, ""):
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
    value = raw.get("messageTimestamp")
    if isinstance(value, str) and value:
        try:
            at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if at.utcoffset() is None:
            at = at.replace(tzinfo=UTC)
        return int(at.timestamp() * 1000)
    return None


def message_id(raw):
    value = raw.get("id") or raw.get("messageId")
    return str(value) if value not in (None, "") else None


def label_ids(raw):
    value = raw.get("labelIds")
    return [str(v) for v in value] if isinstance(value, list) else []


def _flag(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _int(value, name, low=0, high=None):
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer") from None
    if number < low or (high is not None and number > high):
        raise ValueError(f"{name} is out of range")
    return number


def _float(value, name):
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None
    if number < 0:
        raise ValueError(f"{name} must not be negative")
    return number


class Config:
    """Validated fetch settings. Built from env (CLI) or plugin settings."""

    def __init__(self, values):
        def get(key):
            env, default = next((e, d) for k, e, d, _ in SETTINGS if k == key)
            value = values.get(key)
            return default if value is None or value == "" else value

        self.accounts = [
            a.strip() for a in str(get("accounts") or "").split(",") if a.strip()
        ]
        self.transport = str(get("transport")).strip().lower()
        if self.transport not in ("rest", "mcp"):
            raise ValueError("EMAIL_TRANSPORT must be rest or mcp")
        self.base_url = str(get("base_url")).rstrip("/")
        if not re.match(r"https?://[^/@?#]+", self.base_url) or "@" in self.base_url:
            raise ValueError(
                "COMPOSIO_BASE_URL must be an http(s) URL without credentials"
            )
        self.user_id = get("user_id")
        self.toolkit_version = get("toolkit_version")
        self.since = parse_bound(get("since"))
        self.until = parse_bound(get("until"), end=True)
        self.query = str(get("query") or "").strip()
        self.max_messages = _int(get("max_messages"), "EMAIL_MAX_MESSAGES")
        self.page_size = _int(get("page_size"), "EMAIL_PAGE_SIZE", 1, 500)
        if self.transport == "mcp":
            self.page_size = MCP_PAGE_SIZE
        self.incremental = str(get("incremental")).strip().lower()
        if self.incremental not in ("auto", "history", "date"):
            raise ValueError("EMAIL_INCREMENTAL must be auto, history or date")
        self.overlap_s = _int(get("overlap_s"), "EMAIL_OVERLAP_S")
        self.max_calls_per_run = _int(
            get("max_calls_per_run"), "EMAIL_MAX_CALLS_PER_RUN"
        )
        self.max_calls_per_day = _int(
            get("max_calls_per_day"), "EMAIL_MAX_CALLS_PER_DAY"
        )
        self.min_interval_s = _float(get("min_interval_s"), "EMAIL_MIN_INTERVAL_S")
        self.retry_base_s = _float(get("retry_base_s"), "EMAIL_RETRY_BASE_S")
        self.max_retries = _int(get("max_retries"), "EMAIL_MAX_RETRIES", 0, 20)
        self.timeout_s = _float(get("timeout_s"), "EMAIL_TIMEOUT_S") or 60.0
        self.include_spam_trash = _flag(get("include_spam_trash"))
        self.api_key = values.get("api_key") or None
        self.mcp_url = values.get("mcp_url") or None
        self.mcp_token = values.get("mcp_token") or None

    @classmethod
    def from_env(cls, env=None, **overrides):
        env = os.environ if env is None else env
        values = {key: env.get(name) for key, name, _, _ in SETTINGS}
        values.update({key: env.get(name) for key, name, _ in SECRETS})
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(values)

    def check(self):
        """Fail clearly before any call when the transport cannot run."""
        if not self.accounts:
            raise ValueError("EMAIL_ACCOUNTS (or --account) names no account")
        if self.transport == "rest" and not self.api_key:
            raise ValueError("COMPOSIO_API_KEY is required for the rest transport")
        if self.transport == "mcp":
            if not self.mcp_url or not re.match(r"https?://", self.mcp_url):
                raise ValueError(
                    "COMPOSIO_MCP_URL must be an http(s) URL for the mcp transport"
                )

    def secrets(self):
        return [s for s in (self.api_key, self.mcp_url, self.mcp_token) if s]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # The 3xx surfaces as an HTTPError: never re-send a key.


def _preview(value):
    return isinstance(value, str) and (value.startswith("...") or value.startswith("…"))


def strip_previews(value):
    """(value without MCP preview markers, whether any list was truncated)."""
    if isinstance(value, list):
        cut = any(_preview(v) for v in value)
        items = []
        for v in value:
            if _preview(v):
                continue
            clean, inner = strip_previews(v)
            cut = cut or inner
            items.append(clean)
        return items, cut
    if isinstance(value, dict):
        cut = False
        result = {}
        for k, v in value.items():
            result[k], inner = strip_previews(v)
            cut = cut or inner
        return result, cut
    return value, False


def _unwrap(data):
    while isinstance(data, dict) and set(data) <= {"response_data"} and data:
        data = data["response_data"]
    return data if isinstance(data, dict) else {"value": data}


_MISSING = re.compile(r"\b404\b|\b410\b|not ?found|too old|expired", re.I)
# Errors about the connection itself (unknown account, revoked or expired
# OAuth, bad key, unknown tool) must fail the run, never read as "this message
# or history id is gone": that would silently drop messages.
_CONNECTION = re.compile(
    r"account|connect|toolkit|oauth|auth|token|api.?key|credential|permission"
    r"|scope|slug|\btool\b|\baction\b",
    re.I,
)


def _not_found(error):
    text = str(error)
    return bool(_MISSING.search(text)) and not _CONNECTION.search(text)


class Fetcher:
    """Composio Gmail reader. run() yields ("record", dict) and
    ("checkpoint", state) events; a checkpoint covers every record before it."""

    def __init__(
        self,
        config,
        state=None,
        *,
        now=utc_now,
        sleep=time.sleep,
        clock=time.monotonic,
    ):
        config.check()
        self.config = config
        self.state = copy.deepcopy(state) if state else {}
        self.state["version"] = VERSION
        self.state.setdefault("accounts", {})
        self.now = now
        self.sleep = sleep
        self.clock = clock
        self.run_calls = 0
        self.stopped = None
        self._last = None
        self._session = None
        self._mcp_ready = False
        self._rpc = 0
        self._labels = {}
        self._opener = urllib.request.build_opener(_NoRedirect)

    # ---- transport -------------------------------------------------------

    def _scrub(self, text):
        for secret in sorted(self.config.secrets(), key=len, reverse=True):
            text = text.replace(secret, "[REDACTED]")
        return text

    def _pace(self):
        if self._last is not None and self.config.min_interval_s:
            wait = self.config.min_interval_s - (self.clock() - self._last)
            if wait > 0:
                self.sleep(wait)
        self._last = self.clock()

    def _detail(self, text):
        try:
            body = json.loads(text)
        except ValueError:
            return ""
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            error = error.get("message") or error.get("detail")
        if not isinstance(error, str) or not error:
            return ""
        return ": " + self._scrub(error)[:200]

    def _post(self, url, body, headers, what):
        data = json.dumps(body).encode()
        delay = self.config.retry_base_s
        for attempt in range(self.config.max_retries + 1):
            self._pace()
            request = urllib.request.Request(
                url, data=data, headers=headers, method="POST"
            )
            wait = None
            try:
                with self._opener.open(
                    request, timeout=self.config.timeout_s
                ) as response:
                    return (
                        response.status,
                        response.headers,
                        response.read().decode("utf-8", "replace"),
                    )
            except urllib.error.HTTPError as error:
                status = error.code
                text = error.read().decode("utf-8", "replace") if error.fp else ""
                if status != 429 and status < 500:
                    detail = self._detail(text)
                    # A 404/410 that names its cause is a tool answer (the
                    # message or history id is gone); a bare one stays fatal.
                    kind = ToolError if status in (404, 410) and detail else FetchError
                    raise kind(f"{what} failed (HTTP {status}){detail}") from None
                wait = self._retry_after(
                    error.headers.get("Retry-After") if error.headers else None
                )
                reason = f"HTTP {status}"
            except (
                urllib.error.URLError,
                TimeoutError,
                ConnectionError,
                http.client.HTTPException,
                OSError,
            ) as error:
                reason = type(error).__name__
            if attempt == self.config.max_retries:
                raise FetchError(f"{what} failed after retries ({reason})") from None
            LOG.warning("%s: %s, retrying", what, reason)
            self.sleep(wait if wait is not None else min(delay, 60))
            delay *= 2
        raise FetchError(f"{what} failed")  # pragma: no cover

    def _retry_after(self, value):
        """Seconds from a Retry-After header (seconds or HTTP date), capped."""
        value = (value or "").strip()
        if not value:
            return None
        if value.isdigit():
            return min(int(value), 60)
        try:
            at = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
        if at is None or at.utcoffset() is None:
            return None
        return min(max(0.0, (at - self.now()).total_seconds()), 60)

    def _count(self):
        calls = self.state.get("calls") or {}
        day = self.now().astimezone(UTC).date().isoformat()
        if calls.get("day") != day:
            calls = {"day": day, "count": 0}
        cfg = self.config
        if cfg.max_calls_per_run and self.run_calls >= cfg.max_calls_per_run:
            raise LimitReached(f"per-run call limit {cfg.max_calls_per_run} reached")
        if cfg.max_calls_per_day and calls["count"] >= cfg.max_calls_per_day:
            raise LimitReached(f"per-day call limit {cfg.max_calls_per_day} reached")
        self.run_calls += 1
        calls["count"] += 1
        self.state["calls"] = calls

    def execute(self, account, tool, arguments):
        """Run one Composio tool for one account; returns its data object."""
        self._count()
        if self.config.transport == "mcp":
            return self._mcp(account, tool, arguments)
        return self._rest(account, tool, arguments)

    def _rest(self, account, tool, arguments):
        body = {"connected_account_id": account, "arguments": arguments}
        if self.config.user_id:
            body["user_id"] = self.config.user_id
        if self.config.toolkit_version:
            body["version"] = self.config.toolkit_version
        _, _, text = self._post(
            f"{self.config.base_url}/api/v3/tools/execute/{tool}",
            body,
            {
                "x-api-key": self.config.api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "throughline-email-fetch/1",
            },
            f"Composio {tool}",
        )
        try:
            answer = json.loads(text)
        except ValueError:
            raise FetchError(f"Composio {tool} returned invalid JSON") from None
        if not isinstance(answer, dict):
            raise FetchError(f"Composio {tool} returned an unexpected shape")
        if answer.get("successful") is False or answer.get("error"):
            message = answer.get("error")
            if isinstance(message, dict):
                message = message.get("message")
            raise ToolError(
                f"Composio {tool} failed: {self._scrub(str(message or 'unsuccessful'))[:200]}"
            )
        return _unwrap(answer.get("data") or {})

    def _mcp_headers(self):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "throughline-email-fetch/1",
        }
        if self.config.mcp_token:
            headers["Authorization"] = "Bearer " + self.config.mcp_token
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        return headers

    @staticmethod
    def _rpc_message(text, content_type, rpc_id):
        if "text/event-stream" in (content_type or ""):
            found = None
            for block in re.split(r"\r?\n\r?\n", text):
                data = "\n".join(
                    line[5:].lstrip()
                    for line in block.splitlines()
                    if line.startswith("data:")
                )
                if not data:
                    continue
                try:
                    message = json.loads(data)
                except ValueError:
                    continue
                if isinstance(message, dict) and message.get("id") == rpc_id:
                    found = message
            if found is None:
                raise FetchError("Composio MCP stream had no response")
            return found
        try:
            return json.loads(text)
        except ValueError:
            raise FetchError("Composio MCP returned invalid JSON") from None

    def _mcp_init(self):
        self._rpc += 1
        _, headers, text = self._post(
            self.config.mcp_url,
            {
                "jsonrpc": "2.0",
                "id": self._rpc,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {
                        "name": "throughline-email-fetch",
                        "version": str(VERSION),
                    },
                },
            },
            self._mcp_headers(),
            "Composio MCP initialize",
        )
        message = self._rpc_message(text, headers.get("Content-Type"), self._rpc)
        if "error" in message:
            raise FetchError("Composio MCP initialize was refused")
        self._session = headers.get("Mcp-Session-Id") or None
        self._post(
            self.config.mcp_url,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            self._mcp_headers(),
            "Composio MCP initialize",
        )
        self._mcp_ready = True

    def _mcp(self, account, tool, arguments):
        if not self._mcp_ready:
            self._mcp_init()
        self._rpc += 1
        _, headers, text = self._post(
            self.config.mcp_url,
            {
                "jsonrpc": "2.0",
                "id": self._rpc,
                "method": "tools/call",
                "params": {
                    "name": MCP_TOOL,
                    "arguments": {
                        "tools": [
                            {
                                "tool_slug": tool,
                                "arguments": arguments,
                                "account": account,
                            }
                        ],
                        "sync_response_to_workbench": False,
                    },
                },
            },
            self._mcp_headers(),
            f"Composio MCP {tool}",
        )
        message = self._rpc_message(text, headers.get("Content-Type"), self._rpc)
        if "error" in message:
            raise FetchError(f"Composio MCP {tool} returned a JSON-RPC error")
        result = message.get("result") or {}
        try:
            payload = json.loads(result["content"][0]["text"])
        except (KeyError, IndexError, TypeError, ValueError):
            raise FetchError(
                f"Composio MCP {tool} returned an unexpected shape"
            ) from None
        if result.get("isError"):
            raise ToolError(
                f"Composio MCP {tool} failed: {self._scrub(str(payload))[:200]}"
            )
        outer = payload.get("data", payload) if isinstance(payload, dict) else {}
        results = outer.get("results") if isinstance(outer, dict) else None
        if not results:
            raise FetchError(f"Composio MCP {tool} returned no result")
        first = results[0] if isinstance(results[0], dict) else {}
        response = first.get("response", first)
        if not isinstance(response, dict):
            raise FetchError(f"Composio MCP {tool} returned an unexpected shape")
        if response.get("successful") is False or response.get("error"):
            raise ToolError(
                f"Composio MCP {tool} failed: {self._scrub(str(response.get('error') or 'unsuccessful'))[:200]}"
            )
        return _unwrap(response.get("data", response))

    # ---- Gmail operations -------------------------------------------------

    def profile(self, account):
        return self.execute(account, TOOLS["profile"], {"user_id": "me"})

    def label_names(self, account):
        """User label id -> name, one call per account and run."""
        if account not in self._labels:
            names = {}
            try:
                data = self.execute(account, TOOLS["labels"], {"user_id": "me"})
            except ToolError as error:
                LOG.warning("Gmail labels unavailable for one account: %s", error)
                data = {}
            labels, _ = strip_previews(data.get("labels") or [])
            for label in labels:
                if isinstance(label, dict) and label.get("id") and label.get("name"):
                    if str(label["name"]) != str(label["id"]):
                        names[str(label["id"])] = str(label["name"])
            self._labels[account] = names
        return self._labels[account]

    def list_page(self, account, query, page_token, size):
        rest = self.config.transport == "rest"
        arguments = {
            "user_id": "me",
            "query": query,
            "max_results": size,
            "include_payload": rest,
            "verbose": rest,
            "include_spam_trash": self.config.include_spam_trash,
        }
        if page_token:
            arguments["page_token"] = page_token
        data = self.execute(account, TOOLS["list"], arguments)
        messages = data.get("messages") or []
        if not isinstance(messages, list) or any(_preview(m) for m in messages):
            raise FetchError(
                "Composio returned a truncated message list; use the rest transport"
            )
        token = data.get("nextPageToken") or data.get("next_page_token")
        return [m for m in messages if isinstance(m, dict)], token

    def get_message(self, account, ident):
        try:
            return self.execute(
                account,
                TOOLS["get"],
                {"user_id": "me", "message_id": ident, "format": "full"},
            )
        except ToolError as error:
            if _not_found(error):
                return None
            raise

    def history_page(self, account, start, page_token):
        arguments = {
            "user_id": "me",
            "start_history_id": str(start),
            "max_results": 100 if self.config.transport == "rest" else MCP_PAGE_SIZE,
        }
        if page_token:
            arguments["page_token"] = page_token
        data = self.execute(account, TOOLS["history"], arguments)
        history = data.get("history") or []
        if any(_preview(h) for h in history):
            raise FetchError(
                "Composio returned a truncated history list; use the rest transport"
            )
        token = data.get("nextPageToken") or data.get("next_page_token")
        return [h for h in history if isinstance(h, dict)], token, data.get("historyId")

    # ---- records ---------------------------------------------------------

    def _record(self, kind, ref, acct, ident, raw, **extra):
        record = {
            "kind": kind,
            "account": acct.get("address") or ref,
            "account_ref": ref,
            "id": str(ident),
            "fetched_at": iso(self.now()),
            "transport": self.config.transport,
        }
        record.update(extra)
        record["raw"] = raw
        return "record", record

    def _message(self, ref, acct, raw, ident=None):
        raw, cut = (
            strip_previews(raw) if self.config.transport == "mcp" else (raw, False)
        )
        labels = label_ids(raw)
        if "SPAM" in labels and not self.config.include_spam_trash:
            return None
        names = self.label_names(ref)
        extra = dict(
            thread_id=str(raw.get("threadId") or ""),
            labels={i: names[i] for i in labels if i in names},
        )
        if cut:
            extra["truncated"] = True
        return self._record(
            "gmail.message", ref, acct, message_id(raw) or ident, raw, **extra
        )

    def _checkpoint(self):
        return "checkpoint", copy.deepcopy(self.state)

    def _query(self, after=None, before=None):
        parts = [self.config.query] if self.config.query else []
        if after is not None:
            parts.append(f"after:{int(after)}")
        if before is not None:
            parts.append(f"before:{int(before)}")
        return " ".join(parts)

    # ---- sync ------------------------------------------------------------

    def run(self):
        try:
            for ref in self.config.accounts:
                yield from self._account(ref)
        except LimitReached as reason:
            self.stopped = str(reason)
            LOG.warning("Email fetch stopped: %s; the next run resumes", reason)
        yield self._checkpoint()

    def _account(self, ref):
        acct = self.state["accounts"].setdefault(ref, {})
        if acct.get("backfilled"):
            yield from self._incremental(ref, acct)
            return
        if "listing" not in acct:
            profile = self.profile(ref)
            address = str(profile.get("emailAddress") or "").strip().lower()
            acct["address"] = address or ref
            acct["history_id"] = (
                str(profile["historyId"]) if profile.get("historyId") else None
            )
            acct["started"] = int(self.now().timestamp())
            acct["listing"] = {
                "purpose": "backfill",
                "query": self._query(self.config.since, self.config.until),
                "page_token": None,
                "count": 0,
                "limit": self.config.max_messages,
            }
            yield self._record("gmail.profile", ref, acct, acct["address"], profile)
            yield self._checkpoint()
        yield from self._listing(ref, acct)
        acct["backfilled"] = True
        yield self._checkpoint()

    def _listing(self, ref, acct):
        """Page a listing; each page is applied to the state only when done."""
        listing = acct["listing"]
        while True:
            limit = listing.get("limit") or 0
            remaining = limit - listing["count"] if limit else None
            if remaining is not None and remaining <= 0:
                break
            size = (
                min(self.config.page_size, remaining)
                if remaining
                else self.config.page_size
            )
            messages, token = self.list_page(
                ref, listing["query"], listing["page_token"], size
            )
            newest = acct.get("max_ms") or 0
            for raw in messages[:size]:
                ident = message_id(raw)
                if not ident:
                    continue
                if (
                    self.config.transport == "rest"
                    and "payload" not in raw
                    and "messageText" not in raw
                ):
                    raw = self.get_message(ref, ident)
                    if raw is None:
                        continue
                event = self._message(ref, acct, raw, ident)
                if event:
                    yield event
                newest = max(newest, message_ms(raw) or 0)
            listing["count"] += len(messages)
            listing["page_token"] = token
            if newest:
                acct["max_ms"] = newest
            yield self._checkpoint()
            if not token or not messages:
                break
        del acct["listing"]
        if acct.get("next_history_id"):
            acct["history_id"] = acct.pop("next_history_id")

    def _incremental(self, ref, acct):
        if acct.get("pending"):
            yield from self._pending(ref, acct)
        if "listing" in acct:
            yield from self._listing(ref, acct)
            yield self._checkpoint()
            return
        mode = self.config.incremental
        if mode != "date" and acct.get("history_id"):
            try:
                yield from self._history(ref, acct)
                return
            except ToolError as error:
                if not _not_found(error):
                    raise
                if mode == "history":
                    raise FetchError(
                        "Gmail history id expired; rerun with EMAIL_INCREMENTAL=auto or date"
                    ) from None
                LOG.warning(
                    "Gmail history id expired for one account; using a date query"
                )
                for key in ("history_id", "history_page", "history_latest"):
                    acct.pop(key, None)
        if mode != "date":
            profile = self.profile(ref)
            address = str(profile.get("emailAddress") or "").strip().lower()
            known = str(acct.get("address") or "").strip().lower()
            if known and address and address != known:
                raise FetchError(
                    "Gmail connected account identity no longer matches its cursor"
                )
            if address:
                acct["address"] = address
            if profile.get("historyId"):
                acct["next_history_id"] = str(profile["historyId"])
        if acct.get("max_ms"):
            after = acct["max_ms"] // 1000 - self.config.overlap_s
        else:
            after = (
                acct.get("started") or int(self.now().timestamp())
            ) - self.config.overlap_s
        acct["listing"] = {
            "purpose": "date",
            "query": self._query(max(0, after), None),
            "page_token": None,
            "count": 0,
            "limit": 0,
        }
        yield from self._listing(ref, acct)
        yield self._checkpoint()

    def _history(self, ref, acct):
        while True:
            entries, token, latest = self.history_page(
                ref, acct["history_id"], acct.get("history_page")
            )
            pending = acct.setdefault("pending", [])
            queued = set(pending)
            for entry in entries:
                for item in entry.get("messagesDeleted") or []:
                    message = (item or {}).get("message") or {}
                    ident = message_id(message)
                    if not ident:
                        continue
                    if ident in queued:
                        pending.remove(ident)
                        queued.discard(ident)
                    yield self._record(
                        "gmail.deleted",
                        ref,
                        acct,
                        ident,
                        message,
                        thread_id=str(message.get("threadId") or ""),
                        history_id=str(entry.get("id") or ""),
                    )
                changed = []
                for key in ("messagesAdded", "labelsAdded", "labelsRemoved"):
                    changed += [
                        ((i or {}).get("message") or {}) for i in entry.get(key) or []
                    ]
                if not any(
                    entry.get(k)
                    for k in (
                        "messagesAdded",
                        "labelsAdded",
                        "labelsRemoved",
                        "messagesDeleted",
                    )
                ):
                    changed += [
                        m for m in entry.get("messages") or [] if isinstance(m, dict)
                    ]
                for message in changed:
                    ident = message_id(message)
                    if ident and ident not in queued:
                        pending.append(ident)
                        queued.add(ident)
            if latest:
                acct["history_latest"] = str(latest)
            acct["history_page"] = token
            yield self._checkpoint()
            if not token:
                break
        acct.pop("history_page", None)
        if acct.get("history_latest"):
            acct["history_id"] = acct.pop("history_latest")
        yield self._checkpoint()
        yield from self._pending(ref, acct)

    def _pending(self, ref, acct):
        pending = acct.get("pending") or []
        done = 0
        while pending:
            ident = pending[0]
            raw = self.get_message(ref, ident)
            if raw is not None:
                event = self._message(ref, acct, raw, ident)
                if event:
                    yield event
                acct["max_ms"] = (
                    max(acct.get("max_ms") or 0, message_ms(raw) or 0) or None
                )
            pending.pop(0)
            done += 1
            if done % self.config.page_size == 0:
                yield self._checkpoint()
        acct.pop("pending", None)
        yield self._checkpoint()


# ---- command line ---------------------------------------------------------


def _write_state(path, state):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(state, sort_keys=True, indent=1), encoding="utf-8")
    os.replace(temporary, path)


def export(args, env=None):
    overrides = dict(
        accounts=",".join(args.account) if args.account else None,
        transport=args.transport,
        since=args.since,
        until=args.until,
        query=args.query,
        max_messages=args.max_messages,
        page_size=args.page_size,
        incremental=args.incremental,
        max_calls_per_run=args.max_calls_per_run,
        max_calls_per_day=args.max_calls_per_day,
        min_interval_s=args.min_interval,
        include_spam_trash="true" if args.include_spam_trash else None,
    )
    config = Config.from_env(env, **overrides)
    out = Path(args.out)
    if out.exists() and not args.append:
        raise ValueError("output file exists; pass --append or choose a new file")
    state = {}
    if args.state and Path(args.state).exists():
        state = json.loads(Path(args.state).read_text(encoding="utf-8"))
    fetcher = Fetcher(config, state)
    counts = Counter()
    started = utc_now()
    with out.open("a", encoding="utf-8", newline="\n") as handle:
        meta = {
            "kind": "export.meta",
            "id": iso(started),
            "fetched_at": iso(started),
            "transport": config.transport,
            "raw": {"fetcher_version": VERSION, "accounts": config.accounts},
        }
        handle.write(json.dumps(meta, sort_keys=True) + "\n")
        for event, item in fetcher.run():
            if event == "record":
                handle.write(
                    json.dumps(item, sort_keys=True, ensure_ascii=False) + "\n"
                )
                counts[item["kind"]] += 1
            else:
                handle.flush()
                os.fsync(handle.fileno())
                if args.state:
                    _write_state(args.state, item)
    return {
        "out": str(out),
        "counts": dict(sorted(counts.items())),
        "calls": fetcher.run_calls,
        "stopped": fetcher.stopped,
    }


def main(argv=None, env=None):
    parser = argparse.ArgumentParser(
        prog="fetch.py",
        description="Export Gmail through Composio as JSON Lines (credentials from env only)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("export", help="write raw records to a JSONL file")
    command.add_argument("--out", required=True, help="output JSONL file")
    command.add_argument(
        "--state", help="sync state file, read and updated (incremental exports)"
    )
    command.add_argument(
        "--append", action="store_true", help="append to an existing output file"
    )
    command.add_argument(
        "--account",
        action="append",
        help="connected account id or name (repeatable; default EMAIL_ACCOUNTS)",
    )
    command.add_argument("--transport", choices=["rest", "mcp"])
    command.add_argument(
        "--since", help="backfill lower bound (YYYY-MM-DD or ISO timestamp)"
    )
    command.add_argument(
        "--until", help="backfill upper bound (YYYY-MM-DD inclusive or ISO timestamp)"
    )
    command.add_argument("--query", help="extra Gmail search filter")
    command.add_argument("--max-messages", help="backfill cap per account; 0 = no cap")
    command.add_argument("--page-size")
    command.add_argument("--incremental", choices=["auto", "history", "date"])
    command.add_argument("--max-calls-per-run")
    command.add_argument("--max-calls-per-day")
    command.add_argument("--min-interval", help="seconds between requests")
    command.add_argument("--include-spam-trash", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    try:
        summary = export(args, env)
    except (FetchError, ValueError, OSError) as error:
        print(f"error: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
