import copy
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from throughline_sdk import Checkpoint, Record
from throughline_sdk.contract import check_plugin

from throughline_msgraph import MsGraph, graph_time
from throughline_msgraph import login as login_cli
from throughline_msgraph.auth import AuthError, Tokens, load, save
from throughline_msgraph.fixture import handler_for
from throughline_msgraph.graph import GraphClient, GraphError

FIXTURE = Path(__file__).parent / "recorded" / "graph.json"
TOKEN = "invented-canary-graph-token"
ME = "invented.owner@example.com"
G = "https://graph.microsoft.com/v1.0"
NOW = datetime(2025, 3, 15, 12, 0, tzinfo=UTC)
BASE_ENV = {
    "MSGRAPH_FIXTURE": str(FIXTURE),
    "MSGRAPH_ACCESS_TOKEN": TOKEN,
    "MSGRAPH_WINDOW_START": "2025-01-01",
    "MSGRAPH_WINDOW_END": "2025-03-01",
    "MSGRAPH_BATCH_SIZE": "4",
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
        plugin = MsGraph()
        plugin.clock = lambda: NOW
        plugin.transport = httpx.MockTransport(handler_for(route_list if route_list is not None else routes(), log))
        return plugin

    return build


def run(plugin, cursor=None):
    items = list(plugin.records(cursor or {}, "sync"))
    messages = {m["external_id"].split(":", 1)[1]: m for m in items if isinstance(m, dict)}
    return messages, [i for i in items if isinstance(i, Checkpoint)], items


def replace_route(route_list, path, query, value):
    for route in route_list:
        if route["path"] == "/v1.0" + path and route.get("query") == query:
            route["json"] = value
            return route_list
    raise KeyError(path)


def test_graph_time_parsing():
    assert graph_time({"dateTime": "2025-01-07T08:00:00.0000000", "timeZone": "UTC"}).isoformat() == "2025-01-07T08:00:00+00:00"
    assert graph_time({"dateTime": "2025-07-07T08:00:00", "timeZone": "Europe/Berlin"}).isoformat() == "2025-07-07T08:00:00+02:00"
    assert graph_time({"dateTime": "2025-07-07T08:00:00", "timeZone": "W. Europe Standard Time"}).utcoffset().total_seconds() == 0
    assert graph_time("2024-12-01T10:00:00.1234567Z").isoformat() == "2024-12-01T10:00:00.123456+00:00"


def test_mail_mapping_dedupe_threads_and_fields(make):
    messages, checkpoints, _ = run(make())
    mail = {k: m for k, m in messages.items() if m["channel"] == "msgraph.message"}
    assert set(mail) == {"book-1@example.com", "book-2@example.com", "book-3@example.com", "invoice-9@example.com", "self-7@example.com"}
    note = mail["self-7@example.com"]
    assert note["fields"]["folders"] == ["Inbox", "Sent Items"]
    assert note["fields"]["direction"] == "sent" and note["fields"]["author_kind"] == "owner"
    book = mail["book-1@example.com"]
    assert {mail[k]["thread"]["external_id"] for k in ("book-1@example.com", "book-2@example.com", "book-3@example.com")} == {ME + ":conv:conv-book"}
    assert book["thread"]["title"] == "Invented book club"
    assert book["fields"]["thread_source"] == "provider"
    assert book["fields"]["is_unread"] is True
    assert book["author"] == {"system": "email", "kind": "email", "value": "friend@example.com", "display_name": "Invented Friend"}
    assert mail["book-2@example.com"]["fields"]["direction"] == "sent"
    invoice = mail["invoice-9@example.com"]
    assert invoice["fields"]["body_format"] == "html" and "Your invented invoice is attached." in invoice["content"]
    assert invoice["fields"]["is_starred"] is True and invoice["fields"]["is_important"] is True
    assert invoice["fields"]["labels"] == ["Inbox", "Invented finance"]
    assert invoice["fields"]["attachments"] == [
        {"filename": "invented-invoice.pdf", "mime_type": "application/pdf", "size": 2048, "content_id": None, "inline": False}
    ]
    assert invoice["fields"]["graph_message_id"] == "m2"
    state = checkpoints[-1].cursor
    assert state["mail"]["inbox"]["link"].endswith("$deltatoken=in-1")
    assert TOKEN not in json.dumps([messages, state])


def test_calendar_occurrences_and_lifecycle(make):
    messages, _, items = run(make())
    sync = messages["primary:occ-1"]
    assert sync["channel"] == "msgraph.events" and sync["fields"]["series_id"] == "series-1"
    assert sync["fields"]["ical_uid"] is None  # Graph's per-occurrence iCalUId is not the series UID.
    moved = messages["primary:occ-2"]
    assert moved["fields"]["original_start"] == "2025-01-14T08:00:00+00:00"
    assert moved["fields"]["event_type"] == "exception"
    assert messages["primary:occ-3"]["fields"]["status"] == "cancelled"
    assert messages["primary:series-1:created"]["fields"]["change"] == "created"
    assert "primary:occ-3:cancelled:2025-01-15T12:00:00+00:00" in messages
    assert "primary:occ-2:created" not in messages  # Exceptions carry the series' creation.
    kickoff = messages["primary:single-1"]
    assert kickoff["fields"]["my_response"] == "tentative"
    assert kickoff["fields"]["conference_url"] == "https://teams.example.com/l/invented-kickoff"
    assert kickoff["fields"]["place"] == "Invented Room 4"
    assert kickoff["fields"]["description"] == "Agenda:\n\nInvented goals" or "Invented goals" in kickoff["fields"]["description"]
    assert [a.get("optional") for a in kickoff["fields"]["attendees"]] == [None, True, None]
    assert kickoff["fields"]["attendees"][2]["resource"] is True
    assert kickoff["author"]["value"] == "lead@example.com"
    holiday = messages["primary:allday-1"]
    assert holiday["fields"]["all_day"] is True and holiday["fields"]["transparency"] == "transparent"
    assert holiday["subject"] == {"start": "2025-02-10T00:00:00+00:00", "start_precision": "day", "end": "2025-02-11T00:00:00+00:00", "end_precision": "day"}
    links = [i.data for i in items if isinstance(i, Record) and i.kind == "link"]
    assert any(link["to"]["external_ref"] == "ical:ical-single-1" for link in links)


def test_second_run_uses_delta_links_and_is_quiet(make):
    _, checkpoints, _ = run(make())
    log = []
    messages, _, _ = run(make(log=log), checkpoints[-1].cursor)
    assert messages == {}
    deltas = [str(r.url) for r in log if r.url.path.endswith("/delta")]
    assert all("deltatoken" in url for url in deltas) and len(deltas) == 3


def test_incremental_changes_and_removals(make):
    _, checkpoints, _ = run(make())
    original = {e["id"]: e for r in routes() if r["path"] == "/v1.0/me/calendarView/delta" for e in r["json"]["value"]}
    kickoff = dict(original["single-1"], subject="Invented project kickoff (moved)", lastModifiedDateTime="2025-01-12T09:00:00Z")
    later = replace_route(routes(), "/me/calendarView/delta", {"$deltatoken": "cal-1"}, {
        "value": [kickoff, {"id": "occ-1", "@removed": {"reason": "deleted"}}, {"id": "allday-1", "@removed": {"reason": "deleted"}}],
        "@odata.deltaLink": G + "/me/calendarView/delta?$deltatoken=cal-2",
    })  # fmt: skip
    new_mail = {"id": "m9", "conversationId": "conv-book", "internetMessageId": "<book-9@example.com>", "subject": "RE: Invented book club",
                "from": {"emailAddress": {"address": "friend@example.com", "name": "Invented Friend"}}, "toRecipients": [],
                "receivedDateTime": "2025-01-09T09:00:00Z", "isRead": True, "body": {"contentType": "text", "content": "One more thing."}}  # fmt: skip
    replace_route(later, "/me/mailFolders/inbox-id/messages/delta", {"$deltatoken": "in-1"},
                  {"value": [new_mail], "@odata.deltaLink": G + "/me/mailFolders/inbox-id/messages/delta?$deltatoken=in-3"})  # fmt: skip
    later.append({"method": "GET", "host": "graph.microsoft.com", "path": "/v1.0/me/messages",
                  "query": {"$filter": "internetMessageId eq '<book-9@example.com>'"}, "json": {"value": [new_mail]}})  # fmt: skip
    messages, checkpoints2, items = run(make(later), checkpoints[-1].cursor)
    assert not [i for i in items if isinstance(i, Record) and i.kind == "coverage"]
    stamp = NOW.isoformat()
    assert set(messages) == {
        "book-9@example.com",
        "primary:single-1",
        "primary:single-1:changed:2025-01-12T09:00:00+00:00",
        "primary:occ-1",
        "primary:series-1:removed:observed:" + stamp,
        "primary:allday-1",
        "primary:allday-1:removed:observed:" + stamp,
    }
    assert messages["primary:single-1:changed:2025-01-12T09:00:00+00:00"]["fields"]["changed_fields"] == ["title"]
    assert messages["primary:occ-1"]["fields"]["status"] == "removed"
    assert messages["primary:series-1:removed:observed:" + stamp]["fields"]["occurrence_count"] == 1
    assert checkpoints2[-1].cursor["calendar"]["primary"]["link"].endswith("cal-2")


def test_new_window_rereads_and_detects_vanished(make):
    _, checkpoints, _ = run(make())
    later = routes()
    later.append({"method": "GET", "host": "graph.microsoft.com", "path": "/v1.0/me/calendarView/delta", "query": {"startDateTime": "2025-01-02T00:00:00+00:00"},
                  "json": {"value": [e for r in routes() if r["path"] == "/v1.0/me/calendarView/delta" and r.get("query", {}).get("startDateTime")
                                     for e in r["json"]["value"]], "@odata.deltaLink": G + "/me/calendarView/delta?$deltatoken=cal-9"}})  # fmt: skip
    messages, _, _ = run(make(later, MSGRAPH_WINDOW_START="2025-01-02"), checkpoints[-1].cursor)
    # allday-1 and single-2 were on the second page, which this read does not return.
    assert messages["primary:allday-1"]["fields"]["status"] == "removed"
    assert "primary:single-2" not in messages  # Already cancelled: not removed again.


def test_expired_delta_link_starts_over(make):
    _, checkpoints, _ = run(make())
    later = routes()
    for route in later:
        if route.get("query") == {"$deltatoken": "in-1"}:
            route.pop("json")
            route["status"], route["body"] = 410, '{"error": {"code": "syncStateNotFound"}}'
    first, _, _ = run(make())
    messages, checkpoints2, _ = run(make(later), checkpoints[-1].cursor)
    # The inbox is read again from the start; the core keeps unchanged mail as is.
    assert set(messages) == {"book-1@example.com", "book-3@example.com", "invoice-9@example.com", "self-7@example.com"}
    assert messages["book-1@example.com"] == first["book-1@example.com"]
    assert checkpoints2[-1].cursor["mail"]["inbox"]["link"].endswith("in-1")


def test_one_failing_folder_is_partial_all_failing_raises(make):
    broken = [r for r in routes() if r["path"] != "/v1.0/me/mailFolders/sentitems"]
    messages, _, items = run(make(broken))
    assert "book-2@example.com" not in messages
    reports = [i.data for i in items if isinstance(i, Record) and i.kind == "coverage"]
    assert reports == [{"scope_key": ME + ":sentitems", "status": "partial", "channel": "msgraph.message", "reasons": ["source_error"]}]
    with pytest.raises(GraphError):
        run(make([r for r in routes() if r["path"] == "/v1.0/me"]))
    with pytest.raises(GraphError):
        run(make([]))


def test_paging_links_never_leave_graph():
    tokens = Tokens(static=TOKEN)

    def handler(request):
        return httpx.Response(200, json={"value": [], "@odata.nextLink": "https://evil.example.com/steal"})

    client = GraphClient(tokens, transport=httpx.MockTransport(handler))
    with pytest.raises(GraphError, match="outside graph"):
        client.delta("/me/calendarView/delta")


def write_cache(path, **values):
    data = {"client_id": "invented-client", "tenant": "consumers", "scope": "offline_access Mail.Read", "token_type": "Bearer",
            "access_token": "old-access", "refresh_token": "old-refresh", "expires_at": 0}  # fmt: skip
    data.update(values)
    save(path, data)


def test_refresh_rotates_tokens_and_keeps_0600(tmp_path):
    cache = tmp_path / "nested" / "token.json"
    write_cache(cache)
    assert stat.S_IMODE(os.stat(cache).st_mode) == 0o600
    seen = []

    def login(request):
        seen.append(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600, "scope": "Mail.Read"})

    tokens = Tokens(cache, http=httpx.Client(transport=httpx.MockTransport(login)), clock=lambda: 1_000_000)
    assert tokens.access() == "new-access"
    assert seen[0]["grant_type"] == "refresh_token" and seen[0]["refresh_token"] == "old-refresh"
    assert "client_secret" not in seen[0]
    stored = load(cache)
    assert stored["refresh_token"] == "new-refresh" and stored["expires_at"] == 1_003_600
    assert stat.S_IMODE(os.stat(cache).st_mode) == 0o600
    assert tokens.access() == "new-access" and len(seen) == 1  # Still valid: no refresh.


def test_401_forces_one_refresh_and_failures_are_clear(tmp_path):
    cache = tmp_path / "token.json"
    write_cache(cache, access_token="stale", expires_at=9_999_999_999)
    calls = []

    def graph(request):
        calls.append(request.headers["Authorization"])
        if request.headers["Authorization"] == "Bearer stale":
            return httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken"}})
        return httpx.Response(200, json={"mail": ME})

    def login(request):
        return httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600})

    tokens = Tokens(cache, http=httpx.Client(transport=httpx.MockTransport(login)))
    client = GraphClient(tokens, transport=httpx.MockTransport(graph))
    assert client.get("/me") == {"mail": ME}
    assert calls == ["Bearer stale", "Bearer fresh"]
    assert load(cache)["refresh_token"] == "old-refresh"  # Not rotated: the old one is kept.

    def refused(request):
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": "AADSTS70000: invented expiry\nTrace"})

    write_cache(cache)
    with pytest.raises(AuthError, match="invalid_grant") as error:
        Tokens(cache, http=httpx.Client(transport=httpx.MockTransport(refused))).access()
    assert "old-refresh" not in str(error.value)
    with pytest.raises(AuthError, match="throughline-msgraph-login"):
        Tokens(tmp_path / "missing.json").access()


def test_device_code_login_cli(tmp_path, capsys):
    answers = iter(
        [
            httpx.Response(200, json={"device_code": "dev-1", "user_code": "INVENTED", "verification_uri": "https://microsoft.com/devicelogin",
                                      "interval": 1, "expires_in": 600, "message": "Open https://microsoft.com/devicelogin and enter INVENTED"}),
            httpx.Response(400, json={"error": "authorization_pending"}),
            httpx.Response(400, json={"error": "slow_down"}),
            httpx.Response(200, json={"access_token": "dev-access", "refresh_token": "dev-refresh", "expires_in": 3600, "scope": "Mail.Read"}),
        ]
    )  # fmt: skip
    requests = []

    def handler(request):
        requests.append((request.url.path, dict(httpx.QueryParams(request.content.decode()))))
        return next(answers)

    sleeps = []
    cache = tmp_path / "cfg" / "msgraph-token.json"
    code = login_cli.main(
        ["--client-id", "invented-client", "--tenant", "consumers", "--cache", str(cache)],
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=sleeps.append,
    )
    assert code == 0
    assert requests[0][0] == "/consumers/oauth2/v2.0/devicecode"
    assert requests[1][1]["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code"
    assert sleeps == [1, 1, 6]
    stored = load(cache)
    assert stored["refresh_token"] == "dev-refresh" and stored["client_id"] == "invented-client"
    assert stat.S_IMODE(os.stat(cache).st_mode) == 0o600
    err = capsys.readouterr().err
    assert "INVENTED" in err and "dev-access" not in err and "dev-refresh" not in err


def test_plugin_with_token_cache(make, tmp_path, monkeypatch):
    cache = tmp_path / "token.json"
    write_cache(cache, access_token=TOKEN, expires_at=9_999_999_999)
    monkeypatch.delenv("MSGRAPH_ACCESS_TOKEN")
    messages, _, _ = run(make(MSGRAPH_TOKEN_CACHE=str(cache), MSGRAPH_CALENDAR="false"))
    assert {m["channel"] for m in messages.values()} == {"msgraph.message"}


def test_contract_kit():
    result = check_plugin("throughline_msgraph:MsGraph", copy.deepcopy(BASE_ENV), secrets=[TOKEN])
    assert result["messages"] >= 15


def test_copy_in_another_folder_keeps_earlier_folders(make):
    _, checkpoints, _ = run(make())
    inbox_copy = next(e for r in routes() if r["path"].endswith("inbox-id/messages/delta") for e in r["json"]["value"] if e["id"] == "m1")
    sent_copy = dict(inbox_copy, id="s9", parentFolderId="sent-id")
    later = replace_route(routes(), "/me/mailFolders/sent-id/messages/delta", {"$deltatoken": "sent-1"},
                          {"value": [sent_copy], "@odata.deltaLink": G + "/me/mailFolders/sent-id/messages/delta?$deltatoken=sent-2"})  # fmt: skip
    later.append({"method": "GET", "host": "graph.microsoft.com", "path": "/v1.0/me/messages",
                  "query": {"$filter": "internetMessageId eq '<book-1@example.com>'"}, "json": {"value": [inbox_copy, sent_copy]}})  # fmt: skip
    messages, _, _ = run(make(later), checkpoints[-1].cursor)
    book = messages["book-1@example.com"]
    assert book["fields"]["folders"] == ["Inbox", "Sent Items"]
    assert book["happened_at"] == "2025-01-06T09:00:00+00:00"
    assert book["payload"]["message"]["graph_ids"] == {"Inbox": "m1", "Sent Items": "s9"}


@pytest.mark.parametrize("order", ["removed_last", "present_last"])
def test_last_state_in_a_delta_round_wins(make, order):
    _, checkpoints, _ = run(make())
    original = {e["id"]: e for r in routes() if r["path"] == "/v1.0/me/calendarView/delta" for e in r["json"]["value"]}
    present = dict(original["single-1"], subject="Invented kickoff again", lastModifiedDateTime="2025-01-12T09:00:00Z")
    gone = {"id": "single-1", "@removed": {"reason": "deleted"}}
    value = [present, gone] if order == "removed_last" else [gone, present]
    later = replace_route(routes(), "/me/calendarView/delta", {"$deltatoken": "cal-1"},
                          {"value": value, "@odata.deltaLink": G + "/me/calendarView/delta?$deltatoken=cal-2"})  # fmt: skip
    messages, _, _ = run(make(later), checkpoints[-1].cursor)
    status = messages["primary:single-1"]["fields"]["status"]
    assert status == ("removed" if order == "removed_last" else "confirmed")


def test_all_day_events_round_to_the_nearest_midnight():
    event = {"id": "a", "type": "singleInstance", "isAllDay": True, "start": {"dateTime": "2025-02-09T23:00:00", "timeZone": "UTC"},
             "end": {"dateTime": "2025-02-10T23:00:00", "timeZone": "UTC"}}  # fmt: skip
    occ = MsGraph.occurrence(event, set())
    assert occ["start"].isoformat() == "2025-02-10T00:00:00+00:00" and occ["end"].isoformat() == "2025-02-11T00:00:00+00:00"


def test_refresh_keeps_offline_access_and_tokens_are_guarded(tmp_path, make):
    cache = tmp_path / "token.json"
    write_cache(cache, scope="Mail.Read Calendars.Read")
    seen = []

    def login(request):
        seen.append(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={"access_token": TOKEN, "refresh_token": "rotated-invented", "expires_in": 3600, "scope": "Mail.Read"})

    plugin = make(MSGRAPH_TOKEN_CACHE=str(cache), MSGRAPH_ACCESS_TOKEN="")
    plugin.auth_http = httpx.Client(transport=httpx.MockTransport(login))
    run(plugin)
    assert seen[0]["scope"].split()[0] == "offline_access"
    assert load(cache)["scope"] == "offline_access Mail.Read Calendars.Read"
    assert {TOKEN, "rotated-invented"} <= set(plugin.secrets.values())
