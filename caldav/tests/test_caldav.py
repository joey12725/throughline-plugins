import copy
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from xml.sax.saxutils import escape

import httpx
import pytest
from throughline_sdk import Checkpoint, Record
from throughline_sdk.contract import check_plugin

from throughline_caldav import CalDav, FetchFailed
from throughline_caldav.fixture import handler_for

FIXTURE = Path(__file__).parent / "recorded" / "server.json"
PASSWORD = "invented-canary-caldav-password"
FEED = "https://feeds.example.org/invented/private-token-abc123/club.ics"
ACCOUNT = "invented@dav.example.com"
PERSONAL = ACCOUNT + ":/dav/calendars/invented/personal/:"
WORK = ACCOUNT + ":/dav/calendars/invented/work/:"
D = 'xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" xmlns:cs="http://calendarserver.org/ns/"'
NOW = datetime(2025, 3, 15, 12, 0, tzinfo=UTC)

BASE_ENV = {
    "CALDAV_FIXTURE": str(FIXTURE),
    "CALDAV_URL": "https://dav.example.com/dav/",
    "CALDAV_USERNAME": "invented",
    "CALDAV_PASSWORD": PASSWORD,
    "CALDAV_ICS_URLS": FEED,
    "CALDAV_WINDOW_START": "2025-01-01",
    "CALDAV_WINDOW_END": "2025-03-01",
    "CALDAV_OWNER_ADDRESSES": "owner@example.com",
    "CALDAV_BATCH_SIZE": "4",
}


def routes():
    return json.loads(FIXTURE.read_text())["routes"]


@pytest.fixture
def make(monkeypatch):
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)

    def build(route_list=None, log=None, **settings):
        for key, value in settings.items():
            monkeypatch.setenv(key, value)
        plugin = CalDav()
        plugin.clock = lambda: NOW
        plugin.transport = httpx.MockTransport(handler_for(route_list if route_list is not None else routes(), log))
        return plugin

    return build


def run(plugin, cursor=None):
    items = list(plugin.records(cursor or {}, "sync"))
    messages = {m["external_id"]: m for m in items if isinstance(m, dict)}
    checkpoints = [i for i in items if isinstance(i, Checkpoint)]
    return messages, checkpoints, items


def ms(*responses):
    return f'<?xml version="1.0"?><d:multistatus {D}>' + "".join(responses) + "</d:multistatus>"


def resp(href, props):
    return f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>{props}</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"


def obj(href, etag, text):
    return resp(href, f'<d:getetag>"{etag}"</d:getetag><c:calendar-data>{escape(text)}</c:calendar-data>')


def ics(*lines):
    return "\r\n".join(["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Invented//EN", *lines, "END:VCALENDAR"]) + "\r\n"


def fixture_object(name):
    """The calendar text of one object in the recorded personal calendar."""
    from throughline_caldav.dav import parse_multistatus

    report = next(r for r in routes() if r["path"] == "/dav/calendars/invented/personal/")
    for href, _, props in parse_multistatus(report["body"]):
        if href.endswith(name):
            return props["calendar-data"].text
    raise KeyError(name)


def with_ctag(route_list, ctag):
    result = copy.deepcopy(route_list)
    home = next(r for r in result if r["path"] == "/dav/calendars/invented/")
    home["body"] = home["body"].replace("ctag-p-1", ctag)
    return result


def test_backfill_occurrences_lifecycle_and_fields(make):
    messages, checkpoints, items = run(make())
    standup = [m for k, m in messages.items() if k.startswith(PERSONAL + "standup-1@example.com/")]
    assert sorted(m["happened_at"] for m in standup) == [
        "2025-01-06T09:00:00+01:00",
        "2025-01-20T10:00:00+01:00",
        "2025-01-27T09:00:00+01:00",
    ]  # The 13th is an EXDATE.
    moved = messages[PERSONAL + "standup-1@example.com/20250120T080000Z"]
    assert moved["fields"]["status"] == "cancelled"
    assert moved["fields"]["original_start"] == "2025-01-20T09:00:00+01:00"
    first = messages[PERSONAL + "standup-1@example.com/20250106T080000Z"]
    assert first["fields"]["series_id"] == "standup-1@example.com"
    assert first["fields"]["calendar_name"] == "Invented personal"
    assert first["fields"]["start_time_zone"] == "Europe/Berlin"
    assert first["fields"]["my_response"] == "accepted"
    assert first["fields"]["attendees"][1] == {
        "email": "colleague@example.com",
        "name": "Invented Colleague",
        "response": "needsAction",
        "optional": True,
    }
    assert first["author"] == {"system": "email", "kind": "email", "value": "lead@example.com", "display_name": "Invented Lead"}
    assert first["subject"] == {
        "start": "2025-01-06T09:00:00+01:00",
        "start_precision": "exact",
        "end": "2025-01-06T09:30:00+01:00",
        "end_precision": "exact",
    }
    trip = messages[PERSONAL + "trip-1@example.com"]
    assert trip["fields"]["all_day"] is True and trip["happened_at"] == "2025-01-10T00:00:00+00:00"
    assert trip["subject"]["end"] == "2025-01-12T00:00:00+00:00" and trip["subject"]["end_precision"] == "day"
    assert trip["fields"]["transparency"] == "transparent"
    assert "Pack the invented tent.\nBring snacks." in trip["content"]
    review = messages[WORK + "review-1@example.com"]
    assert review["happened_at"] == "2025-02-05T11:00:00-05:00"
    assert review["fields"]["conference_url"] == "https://meet.example.com/invented-review"
    assert review["fields"]["attendees"][0]["resource"] is True
    assert messages[WORK + "floating-1@example.com"]["happened_at"] == "2025-02-10T08:00:00+00:00"

    changes = {k: m for k, m in messages.items() if m["channel"] == "caldav.changes"}
    assert PERSONAL + "standup-1@example.com:created" in changes
    assert PERSONAL + "trip-1@example.com:changed:first-seen:2024-11-25T12:00:00+00:00" in changes
    cancelled = changes[PERSONAL + "dinner-1@example.com:cancelled:2025-01-20T09:00:00+00:00"]
    assert cancelled["fields"]["time_precision"] == "source"
    assert not any(k.startswith(WORK + "floating-1") for k in changes)  # No CREATED: no record.

    feed = [m for m in messages.values() if m["fields"]["account"] == "ics"]
    assert {m["fields"]["calendar_name"] for m in feed} == {"Invented club feed"}
    assert len([m for m in feed if m["channel"] == "caldav.events"]) == 4
    assert not any("private-token" in json.dumps(m) for m in messages.values())

    links = [i.data for i in items if isinstance(i, Record) and i.kind == "link"]
    assert {"type": "part_of", "from": {"channel": "caldav.events", "external_id": first["external_id"]},
            "to": {"kind": "calendar_event", "external_ref": "ical:standup-1@example.com", "name": "Invented standup"},
            "method": "source"} in links  # fmt: skip
    assert len([c for c in checkpoints if "resume" in c.cursor]) >= 3
    assert "resume" not in checkpoints[-1].cursor


def test_unchanged_ctag_and_sync_token_skip_the_reports(make):
    _, checkpoints, _ = run(make())
    log = []
    messages, _, _ = run(make(log=log), checkpoints[-1].cursor)
    assert messages == {}
    assert not [r for r in log if r.method == "REPORT"]
    feed = [r for r in log if r.method == "GET"]
    assert feed[0].headers["If-None-Match"] == '"feed-1"'


def test_incremental_etags_changed_deleted_moved_and_exdate(make):
    _, checkpoints, _ = run(make())
    standup = fixture_object("standup.ics").replace(
        "EXDATE;TZID=Europe/Berlin:20250113T090000", "EXDATE;TZID=Europe/Berlin:20250113T090000,20250127T090000"
    ).replace("LAST-MODIFIED:20241201T100030Z", "LAST-MODIFIED:20250201T100000Z")
    dentist = fixture_object("dentist.ics").replace("DTSTART:20250115T140000Z", "DTSTART:20250116T140000Z").replace(
        "DTEND:20250115T143000Z", "DTEND:20250116T143000Z"
    ).replace("CREATED:20241210T090000Z", "CREATED:20241210T090000Z\r\nLAST-MODIFIED:20250105T090000Z")
    dinner = fixture_object("dinner.ics")
    base = "/dav/calendars/invented/personal/"
    later = with_ctag(routes(), "ctag-p-2")
    later[0:0] = [
        {"method": "REPORT", "path": base, "body_contains": "trip.ics", "status": 207,
         "body": ms(f"<d:response><d:href>{base}trip.ics</d:href><d:status>HTTP/1.1 404 Not Found</d:status></d:response>")},
        {"method": "REPORT", "path": base, "body_contains": "calendar-multiget", "status": 207,
         "body": ms(obj(base + "standup.ics", "e-standup-2", standup), obj(base + "dentist.ics", "e-dentist-2", dentist))},
        {"method": "REPORT", "path": base, "body_lacks": "calendar-data", "status": 207,
         "body": ms(*(resp(base + n, f'<d:getetag>"{e}"</d:getetag>') for n, e in
                      (("standup.ics", "e-standup-2"), ("dentist.ics", "e-dentist-2"), ("dinner.ics", "e-dinner-1"))))},
    ]  # fmt: skip
    assert dinner  # Unchanged etag: not downloaded again.
    messages, final, _ = run(make(later), checkpoints[-1].cursor)

    # Trip deleted on the server: occurrence removed, lifecycle removed.
    trip = messages[PERSONAL + "trip-1@example.com"]
    assert trip["fields"]["status"] == "removed" and trip["fields"]["observed_at"] == NOW.isoformat()
    assert messages[PERSONAL + "trip-1@example.com:removed:observed:" + NOW.isoformat()]["fields"]["change"] == "removed"
    # Dentist moved a day: same record, new version; changed record at LAST-MODIFIED.
    assert messages[PERSONAL + "dentist-1@example.com"]["happened_at"] == "2025-01-16T14:00:00+00:00"
    changed = messages[PERSONAL + "dentist-1@example.com:changed:2025-01-05T09:00:00+00:00"]
    assert changed["fields"]["changed_fields"] == ["event_start", "event_end"]
    # New EXDATE: that occurrence removed; series changed (recurrence) and a removed record.
    assert messages[PERSONAL + "standup-1@example.com/20250127T080000Z"]["fields"]["status"] == "removed"
    assert messages[PERSONAL + "standup-1@example.com:changed:2025-02-01T10:00:00+00:00"]["fields"]["changed_fields"] == ["recurrence"]
    series = messages[PERSONAL + "standup-1@example.com:removed:observed:" + NOW.isoformat()]
    assert series["fields"]["occurrence_count"] == 1 and "2025-01-27" in series["content"]
    assert not any("dinner" in k for k in messages)
    etags = final[-1].cursor["sources"][ACCOUNT + "\t/dav/calendars/invented/personal/"]["etags"]
    assert sorted(etags) == [base + "dentist.ics", base + "dinner.ics", base + "standup.ics"]


def test_event_moved_out_of_window_is_updated_not_removed(make):
    _, checkpoints, _ = run(make())
    review = next(r for r in routes() if r["path"] == "/dav/calendars/invented/work/")["body"]
    from throughline_caldav.dav import parse_multistatus

    text = next(p["calendar-data"].text for h, _, p in parse_multistatus(review) if h.endswith("review.ics"))
    text = text.replace("20250205T110000", "20250405T110000").replace("20250205T120000", "20250405T120000")
    base = "/dav/calendars/invented/work/"
    later = copy.deepcopy(routes())
    home = next(r for r in later if r["path"] == "/dav/calendars/invented/")
    home["body"] = home["body"].replace("sync/w-1", "sync/w-2")
    later[0:0] = [
        {"method": "REPORT", "path": base, "body_contains": "calendar-multiget", "status": 207, "body": ms(obj(base + "review.ics", "e-review-2", text))},
        {"method": "REPORT", "path": base, "body_lacks": "calendar-data", "status": 207,
         "body": ms(resp(base + "floating.ics", '<d:getetag>"e-floating-1"</d:getetag>'))},
    ]  # fmt: skip
    messages, _, _ = run(make(later), checkpoints[-1].cursor)
    review = messages[WORK + "review-1@example.com"]
    assert review["fields"]["status"] == "confirmed"
    assert review["happened_at"] == "2025-04-05T11:00:00-04:00"


def test_feed_removed_event_and_rolling_window(make):
    _, checkpoints, _ = run(make())
    later = copy.deepcopy(routes())
    feed = later[-1]
    start = feed["body"].index("BEGIN:VEVENT\r\nUID:club-party")
    feed["body"] = feed["body"][:start] + "END:VCALENDAR\r\n"
    messages, _, _ = run(make(later), checkpoints[-1].cursor)
    party = [m for k, m in messages.items() if "club-party" in k]
    assert {m["channel"] for m in party} == {"caldav.events", "caldav.changes"}
    assert all(m["fields"]["status"] == "removed" or m["fields"].get("change") == "removed" for m in party)


def test_empty_answer_removes_nothing(make):
    _, checkpoints, _ = run(make())
    later = copy.deepcopy(routes())
    for route in later:
        if route["path"] == "/dav/calendars/invented/personal/":
            route["body"] = ms()
    cursor = copy.deepcopy(checkpoints[-1].cursor)
    cursor["sources"][ACCOUNT + "\t/dav/calendars/invented/personal/"]["window"] = ["2024-01-01", "x"]
    cursor["sources"][ACCOUNT + "\t/dav/calendars/invented/personal/"]["etags"] = {}
    messages, _, _ = run(make(later), cursor)
    assert not [m for m in messages.values() if m["fields"].get("status") == "removed"]


def test_partial_failure_reports_coverage_and_total_failure_raises(make, caplog):
    caplog.set_level(logging.WARNING)
    broken = [r for r in routes() if r["method"] == "GET"] + [
        {"method": "PROPFIND", "path": "/dav/", "status": 401, "body": "no"}
    ]
    messages, _, items = run(make(broken))
    assert messages and all(m["fields"]["account"] == "ics" for m in messages.values())
    reports = [i.data for i in items if isinstance(i, Record) and i.kind == "coverage"]
    assert {r["scope_key"] for r in reports} == {ACCOUNT}
    assert all(r["status"] == "partial" and r["reasons"] == ["source_error"] for r in reports)
    with pytest.raises(FetchFailed):
        run(make([{"method": "PROPFIND", "path": "/dav/", "status": 401, "body": "no"}]))
    assert "private-token" not in caplog.text and PASSWORD not in caplog.text


def test_contract_kit():
    result = check_plugin("throughline_caldav:CalDav", dict(BASE_ENV), secrets=[PASSWORD, FEED])
    assert result["messages"] >= 20


@pytest.mark.parametrize("body", ["<html><body>Temporarily unavailable</body></html>", ics()])
def test_unreadable_or_empty_feed_removes_nothing(make, body):
    _, checkpoints, _ = run(make())
    later = copy.deepcopy(routes())
    later[-1]["body"] = body
    later[-1]["response_headers"] = {}
    messages, final, _ = run(make(later), checkpoints[-1].cursor)
    assert not [m for m in messages.values() if m["fields"].get("status") == "removed" or m["fields"].get("change") == "removed"]


def test_unparseable_object_in_full_read_removes_nothing(make):
    _, checkpoints, _ = run(make())
    cursor = copy.deepcopy(checkpoints[-1].cursor)
    cursor["sources"][ACCOUNT + "\t/dav/calendars/invented/personal/"]["window"] = None  # Force a full read.
    later = copy.deepcopy(routes())
    report = next(r for r in later if r["path"] == "/dav/calendars/invented/personal/")
    report["body"] = report["body"].replace("BEGIN:VEVENT\r\nUID:trip-1", "BEGIN:VEVENT\r\nUID:trip-1\r\nDTSTART:garbage")
    messages, final, _ = run(make(later), cursor)
    assert not [m for m in messages.values() if m["fields"].get("status") == "removed"]


def personal_incremental(*extra):
    later = with_ctag(routes(), "ctag-p-2")
    base = "/dav/calendars/invented/personal/"
    later[0:0] = list(extra) + [
        {"method": "REPORT", "path": base, "body_lacks": "calendar-data", "status": 207,
         "body": ms(*(resp(base + n, f'<d:getetag>"{e}"</d:getetag>') for n, e in
                      (("standup.ics", "e-standup-1"), ("dentist.ics", "e-dentist-1"), ("dinner.ics", "e-dinner-1"))))},
    ]  # fmt: skip
    return later


def test_href_missing_from_multiget_is_not_deleted(make):
    _, checkpoints, _ = run(make())
    base = "/dav/calendars/invented/personal/"
    later = personal_incremental({"method": "REPORT", "path": base, "body_contains": "trip.ics", "status": 207, "body": ms()})
    messages, final, _ = run(make(later), checkpoints[-1].cursor)
    assert not any("trip" in k for k in messages)
    assert base + "trip.ics" in final[-1].cursor["sources"][ACCOUNT + "\t" + base]["etags"]


def test_truncated_listing_judges_nothing(make):
    _, checkpoints, _ = run(make())
    base = "/dav/calendars/invented/personal/"
    later = with_ctag(routes(), "ctag-p-2")
    later[0:0] = [{"method": "REPORT", "path": base, "body_lacks": "calendar-data", "status": 207, "body": ms(
        resp(base + "standup.ics", '<d:getetag>"e-standup-1"</d:getetag>'),
        f"<d:response><d:href>{base}</d:href><d:status>HTTP/1.1 507 Insufficient Storage</d:status></d:response>")}]  # fmt: skip
    log = []
    messages, final, _ = run(make(later, log=log), checkpoints[-1].cursor)
    assert messages == {}
    assert not [r for r in log if b"calendar-multiget" in r.content]
    assert final[-1].cursor["sources"][ACCOUNT + "\t" + base]["tag"] is None  # Read again next time.


def test_every_calendar_failing_raises_and_query_is_padded(make):
    failing = [r for r in routes() if r["method"] == "PROPFIND"]
    with pytest.raises(FetchFailed):
        run(make(failing, CALDAV_ICS_URLS=""))
    log = []
    run(make(log=log))
    report = next(r for r in log if r.method == "REPORT")
    assert b'start="20241231T000000Z" end="20250302T000000Z"' in report.content


def test_each_feed_url_is_a_secret(make):
    plugin = make(CALDAV_ICS_URLS=FEED + ",webcal://feeds.example.org/other/tok.ics")
    assert {FEED, "webcal://feeds.example.org/other/tok.ics", "https://feeds.example.org/other/tok.ics"} <= set(plugin.secrets.values())
