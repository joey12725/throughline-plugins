"""Invented calendar data and a fake Composio server. Nothing here is real."""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

ACCOUNT = "invented-account-1"
CAL_A = "invented.person@example.invalid"
CAL_B = "invented-team@group.calendar.google.com"
SERIES = "inventedseries1"
CANARY_KEY = "invented-calendar-api-key-canary"
CANARY_MCP = "invented-calendar-mcp-token-canary"


def when(value):
    return (
        {"date": value}
        if len(value) == 10
        else {"dateTime": value, "timeZone": "America/Winnipeg"}
    )


def event(eid, title, start, end, *, created, updated, **extra):
    item = {
        "kind": "calendar#event",
        "id": eid,
        "status": "confirmed",
        "summary": title,
        "start": when(start),
        "end": when(end),
        "created": created,
        "updated": updated,
        "iCalUID": eid + "@invented.invalid",
        "htmlLink": f"https://calendar.invalid/event?eid={eid}",
        "creator": {"email": CAL_A, "self": True},
        "organizer": {"email": CAL_A, "self": True},
        "etag": '"' + updated + '"',
    }
    item.update(extra)
    return item


def instance(day, *, updated="2024-01-01T09:00:05Z", **extra):
    item = event(
        f"{SERIES}_{day}T150000Z",
        "Invented standup",
        f"{day[:4]}-{day[4:6]}-{day[6:]}T15:00:00Z",
        f"{day[:4]}-{day[4:6]}-{day[6:]}T15:15:00Z",
        created="2024-01-01T09:00:00Z",
        updated=updated,
        recurringEventId=SERIES,
        originalStartTime={"dateTime": f"{day[:4]}-{day[4:6]}-{day[6:]}T15:00:00Z"},
        iCalUID=SERIES + "@invented.invalid",
        attendees=[
            {"email": CAL_A, "self": True, "responseStatus": "accepted"},
            {
                "email": "invented.colleague@example.invalid",
                "displayName": "Invented Colleague",
                "responseStatus": "needsAction",
            },
        ],
    )
    item.update(extra)
    return item


def master(updated="2024-01-01T09:00:05Z", rule="RRULE:FREQ=WEEKLY;BYDAY=MO"):
    item = instance("20240108", updated=updated)
    item["id"] = SERIES
    del item["recurringEventId"]
    del item["originalStartTime"]
    item["recurrence"] = [rule]
    return item


def tombstone(day, updated):
    return {
        "kind": "calendar#event",
        "id": f"{SERIES}_{day}T150000Z",
        "status": "cancelled",
        "recurringEventId": SERIES,
        "originalStartTime": {"dateTime": f"{day[:4]}-{day[4:6]}-{day[6:]}T15:00:00Z"},
        "updated": updated,
    }


DENTIST = event(
    "inventeddentist",
    "Invented dentist",
    "2024-02-10T15:00:00-06:00",
    "2024-02-10T16:00:00-06:00",
    created="2024-01-15T10:00:00Z",
    updated="2024-01-15T10:00:03Z",
    location="Invented Clinic, 1 Example Street",
)
LUNCH = event(
    "inventedlunch",
    "Invented lunch",
    "2024-03-05T12:00:00-06:00",
    "2024-03-05T13:00:00-06:00",
    created="2024-02-20T10:00:00Z",
    updated="2024-02-25T10:00:00Z",
)
LUNCH_MOVED = dict(
    LUNCH,
    start=when("2024-03-05T13:00:00-06:00"),
    end=when("2024-03-05T14:00:00-06:00"),
    updated="2024-03-07T08:00:00Z",
)
TRIP = event(
    "inventedtrip",
    "Invented trip",
    "2024-03-20",
    "2024-03-23",
    created="2024-02-01T10:00:00Z",
    updated="2024-02-01T10:00:01Z",
    transparency="transparent",
)
CALL = event(
    "inventedcall",
    "Invented call",
    "2024-03-25T09:00:00-05:00",
    "2024-03-25T09:30:00-05:00",
    created="2024-02-28T10:00:00Z",
    updated="2024-02-28T10:00:02Z",
)
DINNER = event(
    "inventeddinner",
    "Invented dinner",
    "2024-03-30T19:00:00-05:00",
    "2024-03-30T21:00:00-05:00",
    created="2024-03-06T18:00:00Z",
    updated="2024-03-06T18:00:01Z",
    organizer={
        "email": "invented.friend@example.invalid",
        "displayName": "Invented Friend",
    },
    attendees=[
        {
            "email": "invented.friend@example.invalid",
            "displayName": "Invented Friend",
            "organizer": True,
            "responseStatus": "accepted",
        },
        {"email": CAL_A, "self": True, "responseStatus": "tentative"},
    ],
)
DINNER_CANCELLED = dict(DINNER, status="cancelled", updated="2024-03-12T07:00:00Z")
REVIEW = event(
    "inventedreview",
    "Invented review",
    "2024-03-12T10:00:00-05:00",
    "2024-03-12T11:00:00-05:00",
    created="2024-02-15T10:00:00Z",
    updated="2024-02-15T10:00:00Z",
    description='<p>Agenda:<br>1. Invented item</p><p>See <a href="https://docs.invalid/x">the doc</a></p>',
    hangoutLink="https://meet.invalid/abc-defg-hij",
    organizer={"email": CAL_B, "displayName": "Invented Team"},
)
# Same `updated` as before: a change the API did not timestamp.
REVIEW_RETITLED = dict(REVIEW, summary="Invented quarterly review")

CALENDARS = [
    {
        "kind": "calendar#calendarListEntry",
        "id": CAL_A,
        "summary": CAL_A,
        "summaryOverride": "Invented Personal",
        "timeZone": "America/Winnipeg",
        "primary": True,
    },
    {
        "kind": "calendar#calendarListEntry",
        "id": CAL_B,
        "summary": "Invented Team",
        "timeZone": "America/Winnipeg",
    },
]


def run_records(run, fetched_at, window, a_occ, a_events, b_occ, b_events):
    rows = [
        dict(
            kind="run",
            run=run,
            fetched_at=fetched_at,
            fetcher_version=1,
            transport="rest",
            mode="sync",
            accounts=[ACCOUNT],
            window=window,
        )
    ]
    rows += [
        dict(kind="calendar", run=run, account=ACCOUNT, id=c["id"], payload=c)
        for c in CALENDARS
    ]
    for cid, occ, events in ((CAL_A, a_occ, a_events), (CAL_B, b_occ, b_events)):
        rows += [
            dict(
                kind="occurrence",
                run=run,
                account=ACCOUNT,
                calendar_id=cid,
                id=o["id"],
                payload=o,
            )
            for o in occ
        ]
        rows.append(
            dict(
                kind="window",
                run=run,
                account=ACCOUNT,
                calendar_id=cid,
                time_min=window[0],
                time_max=window[1],
                count=len(occ),
            )
        )
        rows += [
            dict(
                kind="event",
                run=run,
                account=ACCOUNT,
                calendar_id=cid,
                id=e["id"],
                payload=e,
            )
            for e in events
        ]
        rows.append(
            dict(
                kind="changes",
                run=run,
                account=ACCOUNT,
                calendar_id=cid,
                updated_min=window[0],
                full=False,
                count=len(events),
            )
        )
    rows.append(
        dict(kind="run_end", run=run, complete=True, stopped=None, calls=8, errors=[])
    )
    return rows


def synthetic_runs():
    """Three invented fetch runs a week apart (oldest first)."""
    one = run_records(
        "20240301T120000Z-aaaa0001",
        "2024-03-01T12:00:00+00:00",
        ["2024-02-01T00:00:00+00:00", "2024-04-01T00:00:00+00:00"],
        [
            DENTIST,
            LUNCH,
            instance("20240304"),
            instance("20240311"),
            instance("20240318"),
            instance("20240325"),
            TRIP,
            CALL,
        ],
        [LUNCH, master()],
        [REVIEW],
        [REVIEW],
    )
    two = run_records(
        "20240308T120000Z-aaaa0002",
        "2024-03-08T12:00:00+00:00",
        ["2024-02-07T00:00:00+00:00", "2024-04-07T00:00:00+00:00"],
        [
            DENTIST,
            instance("20240304"),
            LUNCH_MOVED,
            tombstone("20240311", "2024-03-07T09:00:00Z"),
            instance("20240318"),
            TRIP,
            CALL,
            instance("20240325"),
            DINNER,
            instance("20240401"),
        ],
        [LUNCH_MOVED, tombstone("20240311", "2024-03-07T09:00:00Z"), DINNER],
        [REVIEW],
        [],
    )
    ended = "2024-03-14T09:00:00Z"
    three = run_records(
        "20240315T120000Z-aaaa0003",
        "2024-03-15T12:00:00+00:00",
        ["2024-02-14T00:00:00+00:00", "2024-04-14T00:00:00+00:00"],
        [
            instance("20240304", updated=ended),
            LUNCH_MOVED,
            tombstone("20240311", "2024-03-07T09:00:00Z"),
            instance("20240318", updated=ended),
            TRIP,
            DINNER_CANCELLED,
        ],
        [
            master(ended, "RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20240320T000000Z"),
            DINNER_CANCELLED,
        ],
        [REVIEW_RETITLED],
        [REVIEW_RETITLED],
    )
    return [one, two, three]


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    return path


@pytest.fixture
def data():
    """This module: the invented records and helpers, for the test modules."""
    return sys.modules[__name__]


@pytest.fixture
def export_dir(tmp_path):
    folder = tmp_path / "export"
    folder.mkdir()
    for number, rows in enumerate(synthetic_runs(), 1):
        write_jsonl(folder / f"calendar-{number}.jsonl", rows)
    return folder


@pytest.fixture
def calendar_env(monkeypatch):
    """Clear every calendar variable so the developer's environment is ignored."""
    from throughline_calendar import Calendar

    for declaration in list(Calendar.manifest.settings.values()) + list(
        Calendar.manifest.secrets.values()
    ):
        monkeypatch.delenv(declaration.env, raising=False)
    monkeypatch.setenv("CALENDAR_MIN_INTERVAL_S", "0")
    return monkeypatch


# --- Fake Composio --------------------------------------------------------------


class FakeComposio:
    """Composio REST v3 and MCP over invented data; records every request."""

    def __init__(self):
        self.calendars = list(CALENDARS)
        self.occurrences = {
            CAL_A: [DENTIST, LUNCH, instance("20240304"), TRIP],
            CAL_B: [REVIEW],
        }
        self.events = {CAL_A: [DENTIST, LUNCH, master()], CAL_B: [REVIEW]}
        self.requests = []
        self.failures = []  # HTTP statuses to answer before the next success
        self.updated_min_too_old = False
        self.mcp_requires_init = False
        self.mcp_sse = False
        self.mcp_session = None
        self.preview_truncation = False
        self.redirect = False

    # Google semantics, simplified.
    def execute(self, slug, arguments):
        if slug == "GOOGLECALENDAR_LIST_CALENDARS":
            items, token_key, size = (
                self.calendars,
                "page_token",
                arguments.get("max_results", 250),
            )
        elif slug == "GOOGLECALENDAR_EVENTS_LIST":
            cid = arguments["calendarId"]
            if arguments.get("singleEvents"):
                items = self.occurrences.get(cid, [])
            else:
                items = self.events.get(cid, [])
                if arguments.get("updatedMin"):
                    if self.updated_min_too_old:
                        return {
                            "successful": False,
                            "error": "updatedMinTooLongAgo: The requested minimum modification time lies too far in the past.",
                            "data": {},
                        }
                    bound = arguments["updatedMin"]
                    items = [
                        i
                        for i in items
                        if i["updated"].replace("Z", "") >= bound.replace("Z", "")
                    ]
            token_key, size = "pageToken", arguments.get("maxResults", 250)
        else:
            return {"successful": False, "error": "unknown tool", "data": {}}
        start = int(arguments.get(token_key) or 0)
        page = items[start : start + size]
        data = {"items": page}
        if slug == "GOOGLECALENDAR_EVENTS_LIST":
            data["kind"] = "calendar#events"
        if start + size < len(items):
            data["nextPageToken"] = str(start + size)
        if self.preview_truncation and len(page) > 2:
            data["items"] = page[:2] + ["..."]
        return {"successful": True, "error": None, "data": {"response_data": data}}


def make_handler(fake):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, body, content_type="application/json", headers=None):
            raw = (
                body.encode("utf-8")
                if isinstance(body, str)
                else json.dumps(body).encode("utf-8")
            )
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            fake.requests.append(
                {
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": body,
                }
            )
            if fake.redirect:
                return self.reply(
                    302, {}, headers={"Location": "http://127.0.0.1:9/elsewhere"}
                )
            if fake.failures:
                status = fake.failures.pop(0)
                return self.reply(
                    status, {"error": "invented failure"}, headers={"Retry-After": "0"}
                )
            if self.path.startswith("/api/v3/tools/execute/"):
                if self.headers.get("x-api-key") != CANARY_KEY:
                    return self.reply(401, {"error": "invalid api key"})
                slug = self.path.rsplit("/", 1)[-1]
                return self.reply(200, fake.execute(slug, body.get("arguments") or {}))
            if self.path == "/mcp":
                return self.mcp(body)
            return self.reply(404, {"error": "not found"})

        def mcp(self, body):
            if self.headers.get("Authorization") != "Bearer " + CANARY_MCP:
                return self.reply(401, {"error": "unauthorized"})
            method = body.get("method")
            if method == "initialize":
                fake.mcp_session = "invented-session"
                return self.reply(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "result": {"protocolVersion": "2025-03-26", "capabilities": {}},
                    },
                    headers={"Mcp-Session-Id": fake.mcp_session},
                )
            if method == "notifications/initialized":
                return self.reply(202, "")
            if fake.mcp_requires_init and (
                not fake.mcp_session
                or self.headers.get("Mcp-Session-Id") != fake.mcp_session
            ):
                return self.reply(
                    400,
                    {
                        "jsonrpc": "2.0",
                        "id": body.get("id"),
                        "error": {"code": -32000, "message": "No session"},
                    },
                )
            tool = body["params"]["arguments"]["tools"][0]
            response = fake.execute(tool["tool_slug"], tool["arguments"])
            text = json.dumps(
                {
                    "successful": True,
                    "data": {
                        "results": [
                            {"response": response, "tool_slug": tool["tool_slug"]}
                        ]
                    },
                }
            )
            message = {
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {"content": [{"type": "text", "text": text}]},
            }
            if fake.mcp_sse:
                return self.reply(
                    200,
                    "event: message\ndata: " + json.dumps(message) + "\n\n",
                    "text/event-stream",
                )
            return self.reply(200, message)

    return Handler


@pytest.fixture
def composio():
    fake = FakeComposio()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(fake))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    fake.base_url = f"http://127.0.0.1:{server.server_address[1]}/api/v3"
    fake.mcp_url = f"http://127.0.0.1:{server.server_address[1]}/mcp"
    try:
        yield fake
    finally:
        server.shutdown()
        server.server_close()
