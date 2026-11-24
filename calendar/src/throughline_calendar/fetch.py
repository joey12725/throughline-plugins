#!/usr/bin/env python3
"""Google Calendar through Composio: a standard-library-only raw fetcher.

Copy this one file to any host with Python 3.12+ and run

    python fetch.py export --out calendar-export.jsonl [options]

It reads the calendars of one or more Composio connected accounts and writes
raw records as JSON lines. Credentials come only from environment variables
(COMPOSIO_API_KEY for the REST transport, COMPOSIO_MCP_URL and
COMPOSIO_MCP_TOKEN for the MCP transport); they are never accepted on the
command line and never printed, logged or written to the export.

Record kinds (one JSON object per line, every line carries `run`):

- run         run header: fetched_at, transport, accounts, window, mode
- calendar    one calendar list entry (`payload`), `id` = calendar id
- occurrence  one event occurrence from the windowed pass (singleEvents=true,
              showDeleted=true); `id` = the occurrence (instance) id
- window      a COMPLETE occurrence pass of one calendar over [time_min, time_max)
- event       one event resource from the change pass (singleEvents=false,
              showDeleted=true, updatedMin): masters, single events,
              exceptions and cancellation tombstones; `id` = event id
- changes     a COMPLETE change pass of one calendar (updated_min, full)
- run_end     complete flag, calls used, why it stopped, per-calendar errors

The plugin package imports this module for its live mode, so the mapping is
identical whether data arrives live or from an export file.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, date, datetime, timedelta

FETCHER_VERSION = 1
USER_AGENT = "throughline-calendar-fetch/1"
DEFAULT_BASE_URL = "https://backend.composio.dev/api/v3"
LIST_CALENDARS = "GOOGLECALENDAR_LIST_CALENDARS"
EVENTS_LIST = "GOOGLECALENDAR_EVENTS_LIST"
MULTI_EXECUTE = "COMPOSIO_MULTI_EXECUTE_TOOL"
# The Composio MCP proxy turns inline lists longer than two items into lossy
# previews, so every list call through it is paged at two.
MCP_PAGE_SIZE = 2
# Google's maximum page size for events.list.
MAX_PAGE_SIZE = 2500
# Re-read this much before the last seen modification time (idempotent).
CHANGES_OVERLAP = timedelta(seconds=60)

# (key, environment variable, default, description). Shared with the plugin
# manifest so one table documents both the fetcher and the plugin.
SETTINGS = [
    (
        "transport",
        "CALENDAR_TRANSPORT",
        "rest",
        "Composio transport: rest (preferred) or mcp",
    ),
    (
        "accounts",
        "CALENDAR_ACCOUNTS",
        None,
        "Comma-separated Composio connected account ids (REST) or MCP account names",
    ),
    (
        "calendars",
        "CALENDAR_CALENDARS",
        None,
        "Comma-separated calendar ids to include; unset means all",
    ),
    (
        "exclude_calendars",
        "CALENDAR_EXCLUDE_CALENDARS",
        None,
        "Comma-separated calendar ids to skip",
    ),
    (
        "past_days",
        "CALENDAR_PAST_DAYS",
        "30",
        "Rolling occurrence window: days before today",
    ),
    (
        "future_days",
        "CALENDAR_FUTURE_DAYS",
        "90",
        "Rolling occurrence window: days after today",
    ),
    (
        "backfill_from",
        "CALENDAR_BACKFILL_FROM",
        None,
        "Backfill occurrence window start date (YYYY-MM-DD)",
    ),
    (
        "backfill_to",
        "CALENDAR_BACKFILL_TO",
        None,
        "Backfill occurrence window end date (YYYY-MM-DD, exclusive)",
    ),
    (
        "changes",
        "CALENDAR_CHANGES",
        "true",
        "Run the change pass (event created/changed/cancelled)",
    ),
    (
        "changes_since",
        "CALENDAR_CHANGES_SINCE",
        None,
        "First change pass lower bound: a date, an ISO time, or 'all'; default is the window start",
    ),
    (
        "page_size",
        "CALENDAR_PAGE_SIZE",
        "250",
        "Items per call (REST); MCP always uses 2",
    ),
    (
        "max_calls_per_run",
        "CALENDAR_MAX_CALLS_PER_RUN",
        "400",
        "Composio calls allowed per run (retries count)",
    ),
    (
        "max_calls_per_day",
        "CALENDAR_MAX_CALLS_PER_DAY",
        "2000",
        "Composio calls allowed per UTC day (retries count)",
    ),
    (
        "min_interval_s",
        "CALENDAR_MIN_INTERVAL_S",
        "1.0",
        "Minimum seconds between Composio calls",
    ),
    ("retries", "CALENDAR_RETRIES", "4", "Retries on 429, 5xx and transport errors"),
    ("base_url", "COMPOSIO_BASE_URL", DEFAULT_BASE_URL, "Composio REST v3 base URL"),
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
        "Optional Composio toolkit version for REST executions",
    ),
]
SECRETS = [
    ("api_key", "COMPOSIO_API_KEY", "Composio API key (REST transport)"),
    (
        "mcp_url",
        "COMPOSIO_MCP_URL",
        "Composio MCP URL; secret because the token is embedded in it",
    ),
    ("mcp_token", "COMPOSIO_MCP_TOKEN", "Composio MCP bearer token"),
]


class FetchError(RuntimeError):
    """A call failed; the message never contains credentials or URLs."""


class LimitReached(Exception):
    """The per-run or per-day call budget is spent."""


class UpdatedMinTooOld(FetchError):
    """Google refused updatedMin because it lies too far in the past."""


def utcnow():
    return datetime.now(UTC).replace(microsecond=0)


def rfc3339(moment):
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_moment(value):
    """A date (YYYY-MM-DD) or an ISO time as an aware UTC datetime."""
    value = value.strip()
    if len(value) == 10:
        return datetime.combine(date.fromisoformat(value), datetime.min.time(), UTC)
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.utcoffset() is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def split_list(value):
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def truthy(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def config(values):
    """Normalize raw string settings (env or plugin settings) to typed config."""
    raw = {key: values.get(key, default) for key, _, default, _ in SETTINGS}
    cfg = dict(raw)
    cfg["transport"] = (raw["transport"] or "rest").strip().lower()
    if cfg["transport"] not in ("rest", "mcp"):
        raise ValueError("CALENDAR_TRANSPORT must be rest or mcp")
    cfg["accounts"] = split_list(raw["accounts"])
    cfg["calendars"] = split_list(raw["calendars"])
    cfg["exclude_calendars"] = split_list(raw["exclude_calendars"])
    for key in (
        "past_days",
        "future_days",
        "page_size",
        "max_calls_per_run",
        "max_calls_per_day",
        "retries",
    ):
        cfg[key] = int(raw[key])
        if cfg[key] < 0:
            raise ValueError(f"{key} must not be negative")
    cfg["min_interval_s"] = float(raw["min_interval_s"])
    cfg["changes"] = truthy(raw["changes"])
    cfg["page_size"] = max(1, min(cfg["page_size"], MAX_PAGE_SIZE))
    if cfg["transport"] == "mcp":
        cfg["page_size"] = MCP_PAGE_SIZE
    for key in ("backfill_from", "backfill_to"):
        cfg[key] = parse_moment(raw[key]) if raw[key] else None
    since = (raw["changes_since"] or "").strip()
    cfg["changes_since"] = (
        "all" if since.lower() == "all" else (parse_moment(since) if since else None)
    )
    cfg["base_url"] = (raw["base_url"] or DEFAULT_BASE_URL).rstrip("/")
    return cfg


def window(cfg, mode, now):
    """[time_min, time_max) of the occurrence pass, on whole UTC days."""
    today = datetime.combine(now.astimezone(UTC).date(), datetime.min.time(), UTC)
    start = today - timedelta(days=cfg["past_days"])
    end = today + timedelta(days=cfg["future_days"] + 1)
    if mode == "backfill":
        start = cfg["backfill_from"] or start
        end = cfg["backfill_to"] or end
    if end <= start:
        raise ValueError("the occurrence window end must be after its start")
    return start, end


# --- HTTP with pacing, budget and retries ---------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # Never forward credentials to another location.


class Budget:
    """Per-run and per-UTC-day call caps; the day counter lives in state."""

    def __init__(self, per_run, per_day, state, today):
        self.per_run = per_run
        self.per_day = per_day
        self.state = state
        self.calls = 0
        if state.get("day") != today:
            state["day"] = today
            state["day_calls"] = 0

    def take(self):
        if self.calls >= self.per_run:
            raise LimitReached("per_run_limit")
        if self.state.get("day_calls", 0) >= self.per_day:
            raise LimitReached("per_day_limit")
        self.calls += 1
        self.state["day_calls"] = self.state.get("day_calls", 0) + 1


class Caller:
    """POSTs JSON politely: minimum interval, retries with backoff on 429/5xx."""

    def __init__(
        self,
        budget,
        *,
        min_interval=1.0,
        retries=4,
        sleep=time.sleep,
        opener=None,
        clock=time.monotonic,
    ):
        self.budget = budget
        self.min_interval = min_interval
        self.retries = retries
        self.sleep = sleep
        self.clock = clock
        self.opener = opener or urllib.request.build_opener(_NoRedirect)
        self.last = None

    def _pace(self):
        if self.last is not None and self.min_interval > 0:
            wait = self.min_interval - (self.clock() - self.last)
            if wait > 0:
                self.sleep(wait)
        self.last = self.clock()

    def post(self, url, body, headers, what):
        """Returns (status, headers, text). Raises FetchError or LimitReached."""
        data = json.dumps(body).encode("utf-8")
        for attempt in range(self.retries + 1):
            self.budget.take()
            self._pace()
            try:
                request = urllib.request.Request(url, data=data, method="POST")
            except ValueError:
                # urllib names the URL in its message, and an MCP URL embeds
                # its token: never let that text out.
                raise FetchError(f"{what}: the endpoint URL is not valid") from None
            for key, value in headers.items():
                request.add_header(key, value)
            request.add_header("User-Agent", USER_AGENT)
            retry_after = None
            try:
                with self.opener.open(request, timeout=90) as response:
                    return (
                        response.status,
                        dict(response.headers),
                        response.read().decode("utf-8"),
                    )
            except urllib.error.HTTPError as error:
                status = error.code
                try:
                    text = error.read().decode("utf-8", "replace")
                except Exception:
                    text = ""
                error.close()
                if status != 429 and status < 500:
                    return status, dict(error.headers or {}), text
                retry_after = (error.headers or {}).get("Retry-After")
                reason = f"HTTP {status}"
            except ValueError:
                # http.client.InvalidURL and friends quote the URL as well.
                raise FetchError(f"{what}: the endpoint URL is not valid") from None
            except (
                urllib.error.URLError,
                TimeoutError,
                ConnectionError,
                OSError,
            ) as error:
                reason = type(error).__name__
            if attempt == self.retries:
                raise FetchError(
                    f"{what} failed after {self.retries + 1} attempts ({reason})"
                )
            delay = min(2.0 * 2**attempt, 60.0)
            if retry_after:
                try:
                    delay = min(max(float(retry_after), 0.0), 120.0)
                except ValueError:
                    pass
            self.sleep(delay)
        raise FetchError(f"{what} failed")  # pragma: no cover


def _error_text(value, limit=300):
    """A short, single-line error text from a tool or API response."""
    if isinstance(value, dict):
        value = (
            value.get("message")
            or value.get("error")
            or json.dumps(value, sort_keys=True)
        )
    text = " ".join(str(value).split())
    return text[:limit]


def _tool_failure(slug, message):
    text = _error_text(message)
    lowered = text.lower()
    if "updatedmintoolongago" in lowered or "too far in the past" in lowered:
        return UpdatedMinTooOld(f"{slug}: updatedMin too old")
    return FetchError(f"{slug} returned an error: {text}")


class RestTransport:
    """Composio REST v3: POST {base}/tools/execute/{slug} with x-api-key."""

    name = "rest"

    def __init__(self, caller, base_url, api_key, *, user_id=None, version=None):
        if not api_key:
            raise ValueError("COMPOSIO_API_KEY is required for the REST transport")
        self.caller = caller
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.user_id = user_id
        self.version = version

    def execute(self, slug, account, arguments):
        body = {"arguments": arguments}
        if account:
            body["connected_account_id"] = account
        if self.user_id:
            body["user_id"] = self.user_id
        if self.version:
            body["version"] = self.version
        status, _, text = self.caller.post(
            f"{self.base_url}/tools/execute/{slug}",
            body,
            {
                "x-api-key": self.api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            slug,
        )
        try:
            answer = json.loads(text) if text else {}
        except ValueError:
            raise FetchError(f"{slug}: HTTP {status} with a non-JSON body") from None
        if status >= 300:
            detail = answer.get("error") if isinstance(answer, dict) else None
            raise _tool_failure(
                slug, f"HTTP {status} {_error_text(detail or '')}".strip()
            )
        if not isinstance(answer, dict):
            raise FetchError(f"{slug}: unexpected response")
        if answer.get("successful") is False or answer.get("error"):
            raise _tool_failure(slug, answer.get("error") or "unsuccessful")
        return answer.get("data", answer)


def _sse_messages(text):
    """JSON-RPC messages carried in a text/event-stream body."""
    messages, data = [], []
    for line in text.splitlines() + [""]:
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
        elif not line.strip() and data:
            try:
                messages.append(json.loads("\n".join(data)))
            except ValueError:
                pass
            data = []
    return messages


class McpTransport:
    """Composio MCP JSON-RPC tools/call of COMPOSIO_MULTI_EXECUTE_TOOL."""

    name = "mcp"

    def __init__(self, caller, url, token):
        if not url:
            raise ValueError("COMPOSIO_MCP_URL is required for the MCP transport")
        self.caller = caller
        self.url = url
        self.token = token
        self.session = None
        self.initialized = False
        self.next_id = 0

    def _rpc(self, method, params, notify=False):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        body = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notify:
            self.next_id += 1
            body["id"] = self.next_id
        status, response_headers, text = self.caller.post(
            self.url, body, headers, "MCP " + method
        )
        lowered = {k.lower(): v for k, v in response_headers.items()}
        if lowered.get("mcp-session-id"):
            self.session = lowered["mcp-session-id"]
        if notify:
            return None
        if status >= 300:
            return {"_status": status}
        if "text/event-stream" in lowered.get("content-type", ""):
            messages = _sse_messages(text)
        else:
            try:
                parsed = json.loads(text) if text else None
            except ValueError:
                raise FetchError(f"MCP {method}: non-JSON response") from None
            messages = parsed if isinstance(parsed, list) else [parsed]
        for message in messages:
            if isinstance(message, dict) and message.get("id") == body["id"]:
                return message
        raise FetchError(f"MCP {method}: no response for the request")

    def _initialize(self):
        answer = self._rpc(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {
                    "name": "throughline-calendar-fetch",
                    "version": str(FETCHER_VERSION),
                },
            },
        )
        if "_status" in answer or answer.get("error"):
            raise FetchError(
                "MCP initialize failed; check COMPOSIO_MCP_URL and COMPOSIO_MCP_TOKEN"
            )
        self._rpc("notifications/initialized", {}, notify=True)
        self.initialized = True

    def execute(self, slug, account, arguments):
        tool = {"tool_slug": slug, "arguments": arguments}
        if account:
            tool["account"] = account
        params = {
            "name": MULTI_EXECUTE,
            "arguments": {"tools": [tool], "sync_response_to_workbench": False},
        }
        answer = self._rpc("tools/call", params)
        if not self.initialized and ("_status" in answer or answer.get("error")):
            # Stateful servers want the initialize handshake first.
            self._initialize()
            answer = self._rpc("tools/call", params)
        if "_status" in answer:
            raise FetchError(f"MCP tools/call {slug}: HTTP {answer['_status']}")
        if answer.get("error"):
            raise _tool_failure(slug, answer["error"])
        result = answer.get("result") or {}
        texts = [
            c.get("text")
            for c in result.get("content") or []
            if isinstance(c, dict) and c.get("text")
        ]
        if result.get("isError"):
            raise _tool_failure(slug, texts[0] if texts else "tool error")
        if not texts:
            raise FetchError(f"MCP tools/call {slug}: empty result")
        try:
            outer = json.loads(texts[0])
        except ValueError:
            raise _tool_failure(slug, texts[0]) from None
        data = outer.get("data", outer) if isinstance(outer, dict) else {}
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list) or not results:
            if isinstance(outer, dict) and (
                outer.get("successful") is False or outer.get("error")
            ):
                raise _tool_failure(slug, outer.get("error") or "unsuccessful")
            raise FetchError(f"MCP tools/call {slug}: unexpected result shape")
        first = results[0] if isinstance(results[0], dict) else {}
        if first.get("error") and not first.get("response"):
            raise _tool_failure(slug, first["error"])
        response = first.get("response", first)
        if isinstance(response, str):
            try:
                response = json.loads(response)
            except ValueError:
                raise _tool_failure(slug, response) from None
        if isinstance(response, dict):
            if response.get("successful") is False or response.get("error"):
                raise _tool_failure(slug, response.get("error") or "unsuccessful")
            return response.get("data", response)
        raise FetchError(f"MCP tools/call {slug}: unexpected response")


# --- Pages -----------------------------------------------------------------

LIST_KEYS = ("items", "calendars", "events")
WRAPPER_KEYS = ("response_data", "data", "result", "response", "body")


def page_of(data, depth=0, outer_token=None):
    """(items, next_page_token) from a Composio tool result, or None.

    A page token found on a wrapper level applies to a list nested below it,
    so a wrapper that lifts the token out of the list object cannot end the
    paging early.
    """
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return None
    if not isinstance(data, dict) or depth > 4:
        return None
    token = data.get("nextPageToken") or data.get("next_page_token") or outer_token
    for key in LIST_KEYS:
        if isinstance(data.get(key), list):
            return data[key], token
    for key in WRAPPER_KEYS:
        found = page_of(data.get(key), depth + 1, token)
        if found is not None:
            return found
    if token or str(data.get("kind", "")).startswith("calendar#"):
        return [], token  # Google omits an empty items list.
    return None


def check_page(slug, items):
    for item in items:
        if not isinstance(item, dict):
            # A literal "..." marks a lossy MCP preview: never trust the page.
            raise FetchError(
                f"{slug}: list was truncated or malformed; lower CALENDAR_PAGE_SIZE"
            )


# --- Fetcher ---------------------------------------------------------------


class Fetcher:
    """One fetch run. `run(state)` yields raw records and updates `state`.

    `state` holds the change pass cursors (last seen modification time per
    account and calendar) and the per-day call counter; keep it between runs.
    """

    def __init__(
        self,
        cfg,
        secrets,
        *,
        mode="sync",
        now=None,
        sleep=time.sleep,
        opener=None,
        log=None,
    ):
        self.cfg = cfg
        self.secrets = {k: v for k, v in (secrets or {}).items() if v}
        self.mode = mode
        self.now = now or utcnow()
        self.sleep = sleep
        self.opener = opener
        self.log = log or (lambda text: None)
        self.report = {}

    def transport(self, budget):
        caller = Caller(
            budget,
            min_interval=self.cfg["min_interval_s"],
            retries=self.cfg["retries"],
            sleep=self.sleep,
            opener=self.opener,
        )
        if self.cfg["transport"] == "mcp":
            return McpTransport(
                caller, self.secrets.get("mcp_url"), self.secrets.get("mcp_token")
            )
        return RestTransport(
            caller,
            self.cfg["base_url"],
            self.secrets.get("api_key"),
            user_id=self.cfg["user_id"],
            version=self.cfg["toolkit_version"],
        )

    def clean(self, text):
        for value in sorted(self.secrets.values(), key=len, reverse=True):
            text = text.replace(value, "[REDACTED]")
        return text

    def pages(self, transport, slug, account, arguments, token_key):
        """Every item of a paged list call; raises when a page is incomplete."""
        token, seen = None, set()
        while True:
            args = dict(arguments)
            if token:
                args[token_key] = token
            found = page_of(transport.execute(slug, account, args))
            if found is None:
                raise FetchError(f"{slug}: unexpected response shape")
            items, token = found
            check_page(slug, items)
            yield from items
            if not token:
                return
            if token in seen:
                raise FetchError(f"{slug}: repeated page token")
            seen.add(token)

    def run(self, state):
        if not self.cfg["accounts"]:
            raise ValueError("CALENDAR_ACCOUNTS is required for a live fetch")
        state.setdefault("version", FETCHER_VERSION)
        cursors = state.setdefault("changes", {})
        budget = Budget(
            self.cfg["max_calls_per_run"],
            self.cfg["max_calls_per_day"],
            state,
            self.now.date().isoformat(),
        )
        transport = self.transport(budget)
        fetched_at = self.now.isoformat()
        run = self.now.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        time_min, time_max = window(self.cfg, self.mode, self.now)
        size = self.cfg["page_size"]
        counts = {}
        errors = []
        stopped = None

        def record(kind, **values):
            counts[kind] = counts.get(kind, 0) + 1
            return dict(kind=kind, run=run, **values)

        yield record(
            "run",
            fetched_at=fetched_at,
            fetcher_version=FETCHER_VERSION,
            transport=transport.name,
            mode=self.mode,
            accounts=list(self.cfg["accounts"]),
            window=[time_min.isoformat(), time_max.isoformat()],
        )
        try:
            for account in self.cfg["accounts"]:
                try:
                    calendars = list(
                        self.pages(
                            transport,
                            LIST_CALENDARS,
                            account,
                            {"max_results": min(size, 250), "show_hidden": True},
                            "page_token",
                        )
                    )
                except FetchError as error:
                    errors.append(
                        dict(
                            account=account,
                            calendar_id=None,
                            step="calendars",
                            error=self.clean(str(error)),
                        )
                    )
                    self.log(
                        f"calendar list failed for an account: {self.clean(str(error))}"
                    )
                    continue
                for calendar in calendars:
                    if calendar.get("id"):
                        yield record(
                            "calendar",
                            account=account,
                            id=calendar["id"],
                            payload=calendar,
                        )
                for calendar in calendars:
                    cid = calendar.get("id")
                    if not cid or (
                        self.cfg["calendars"] and cid not in self.cfg["calendars"]
                    ):
                        continue
                    if cid in self.cfg["exclude_calendars"]:
                        continue
                    yield from self.occurrences(
                        transport, account, cid, time_min, time_max, record, errors
                    )
                    if self.cfg["changes"]:
                        yield from self.changes(
                            transport, account, cid, cursors, time_min, record, errors
                        )
        except LimitReached as limit:
            stopped = str(limit)
            self.log(f"calendar fetch stopped: {stopped}")
        self.report = dict(
            run=run,
            fetched_at=fetched_at,
            records=dict(sorted(counts.items())),
            calls=budget.calls,
            complete=stopped is None and not errors,
            stopped=stopped,
            errors=len(errors),
        )
        yield record(
            "run_end",
            complete=stopped is None and not errors,
            stopped=stopped,
            calls=budget.calls,
            errors=errors,
        )

    def occurrences(self, transport, account, cid, time_min, time_max, record, errors):
        arguments = {
            "calendarId": cid,
            "timeMin": rfc3339(time_min),
            "timeMax": rfc3339(time_max),
            "maxResults": self.cfg["page_size"],
            "singleEvents": True,
            "showDeleted": True,
            "orderBy": "startTime",
        }
        count = 0
        try:
            for item in self.pages(
                transport, EVENTS_LIST, account, arguments, "pageToken"
            ):
                if item.get("id"):
                    count += 1
                    yield record(
                        "occurrence",
                        account=account,
                        calendar_id=cid,
                        id=item["id"],
                        payload=item,
                    )
        except FetchError as error:
            errors.append(
                dict(
                    account=account,
                    calendar_id=cid,
                    step="occurrences",
                    error=self.clean(str(error)),
                )
            )
            self.log(f"occurrence pass failed: {self.clean(str(error))}")
            return
        yield record(
            "window",
            account=account,
            calendar_id=cid,
            time_min=time_min.isoformat(),
            time_max=time_max.isoformat(),
            count=count,
        )

    def changes(self, transport, account, cid, cursors, time_min, record, errors):
        key = account + "\t" + cid
        since = None
        if cursors.get(key):
            since = parse_moment(cursors[key]) - CHANGES_OVERLAP
        elif self.cfg["changes_since"] == "all":
            since = None
        else:
            since = self.cfg["changes_since"] or time_min
        arguments = {
            "calendarId": cid,
            "maxResults": self.cfg["page_size"],
            "singleEvents": False,
            "showDeleted": True,
        }
        full = since is None
        items = []
        try:
            try:
                if since is not None:
                    arguments["updatedMin"] = rfc3339(since)
                items = list(
                    self.pages(transport, EVENTS_LIST, account, arguments, "pageToken")
                )
            except UpdatedMinTooOld:
                # Read everything once and keep the bound on our side.
                arguments.pop("updatedMin", None)
                full = True
                items = [
                    item
                    for item in self.pages(
                        transport, EVENTS_LIST, account, arguments, "pageToken"
                    )
                    if since is None
                    or _updated(item) is None
                    or _updated(item) >= since
                ]
        except FetchError as error:
            errors.append(
                dict(
                    account=account,
                    calendar_id=cid,
                    step="changes",
                    error=self.clean(str(error)),
                )
            )
            self.log(f"change pass failed: {self.clean(str(error))}")
            return
        latest = parse_moment(cursors[key]) if cursors.get(key) else None
        count = 0
        for item in items:
            if not item.get("id"):
                continue
            count += 1
            moment = _updated(item)
            if moment and (latest is None or moment > latest):
                latest = moment
            yield record(
                "event", account=account, calendar_id=cid, id=item["id"], payload=item
            )
        yield record(
            "changes",
            account=account,
            calendar_id=cid,
            updated_min=since.isoformat() if since else None,
            full=full,
            count=count,
        )
        # Advance only after a complete pass; an interrupted one is re-read.
        # A first pass that saw nothing keeps its own bound for the next run.
        if latest is None and since is not None:
            latest = since
        if latest is not None:
            cursors[key] = latest.isoformat()


def _updated(item):
    try:
        return parse_moment(item["updated"]) if item.get("updated") else None
    except ValueError:
        return None


# --- Command line ------------------------------------------------------------


def env_values(environ):
    values = {key: environ.get(env) for key, env, default, _ in SETTINGS}
    return {k: v for k, v in values.items() if v not in (None, "")}


def secret_values(environ):
    return {key: environ.get(env) for key, env, _ in SECRETS if environ.get(env)}


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="fetch.py",
        description="Export Google Calendar data through Composio as raw JSONL. "
        "Credentials are read only from COMPOSIO_API_KEY, COMPOSIO_MCP_URL and COMPOSIO_MCP_TOKEN.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export", help="fetch one run and write it as JSONL")
    export.add_argument("--out", required=True, help="output JSONL file")
    export.add_argument(
        "--append",
        action="store_true",
        help="append the run to --out instead of replacing it",
    )
    export.add_argument(
        "--state", help="JSON state file: change cursors and the per-day call counter"
    )
    export.add_argument("--mode", choices=["sync", "backfill"], default="sync")
    export.add_argument(
        "--from",
        dest="backfill_from",
        help="backfill window start date (with --mode backfill)",
    )
    export.add_argument(
        "--to", dest="backfill_to", help="backfill window end date, exclusive"
    )
    export.add_argument("--past-days", type=int)
    export.add_argument("--future-days", type=int)
    export.add_argument(
        "--accounts", help="comma-separated connected account ids (not secret)"
    )
    export.add_argument("--calendars", help="comma-separated calendar ids to include")
    export.add_argument(
        "--exclude-calendars", help="comma-separated calendar ids to skip"
    )
    export.add_argument("--transport", choices=["rest", "mcp"])
    export.add_argument(
        "--changes-since", help="first change pass bound: date, ISO time or 'all'"
    )
    export.add_argument(
        "--no-changes", action="store_true", help="skip the change pass"
    )
    export.add_argument("--page-size", type=int)
    export.add_argument("--max-calls", dest="max_calls_per_run", type=int)
    export.add_argument("--max-calls-per-day", type=int)
    export.add_argument("--min-interval", dest="min_interval_s", type=float)
    return parser.parse_args(argv)


def export_main(args, environ, stdout, stderr):
    values = env_values(environ)
    for key in (
        "backfill_from",
        "backfill_to",
        "past_days",
        "future_days",
        "accounts",
        "calendars",
        "exclude_calendars",
        "transport",
        "changes_since",
        "page_size",
        "max_calls_per_run",
        "max_calls_per_day",
        "min_interval_s",
    ):
        value = getattr(args, key)
        if value is not None:
            values[key] = str(value)
    if args.no_changes:
        values["changes"] = "false"
    secrets = secret_values(environ)
    try:
        cfg = config(values)
    except ValueError as error:
        print(f"configuration error: {error}", file=stderr)
        return 2
    state = {}
    if args.state and os.path.exists(args.state):
        with open(args.state, encoding="utf-8") as handle:
            state = json.load(handle)
    fetcher = Fetcher(
        cfg, secrets, mode=args.mode, log=lambda text: print(text, file=stderr)
    )
    partial = args.out + ".part"
    status = 0
    try:
        with open(partial, "w", encoding="utf-8", newline="\n") as handle:
            for item in fetcher.run(state):
                handle.write(
                    json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n"
                )
    except (ValueError, FetchError) as error:
        print(f"export failed: {fetcher.clean(str(error))}", file=stderr)
        status = 1
    if status:
        try:
            os.remove(partial)
        except OSError:
            pass
        return status
    if args.append and os.path.exists(args.out):
        with (
            open(partial, encoding="utf-8") as source,
            open(args.out, "a", encoding="utf-8", newline="\n") as target,
        ):
            target.write(source.read())
        os.remove(partial)
    else:
        os.replace(partial, args.out)
    if args.state:
        # Written only after the export file is complete on disk.
        temporary = args.state + ".part"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True, indent=1)
        os.replace(temporary, args.state)
    print(json.dumps(dict(fetcher.report, out=args.out), sort_keys=True), file=stdout)
    return 0


def main(argv=None, environ=None, stdout=None, stderr=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    environ = os.environ if environ is None else environ
    if args.command == "export":
        return export_main(args, environ, stdout or sys.stdout, stderr or sys.stderr)
    return 2  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
