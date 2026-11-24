import json
from pathlib import Path

import httpx
import pytest
from throughline_sdk import Checkpoint, Record
from throughline_sdk.contract import check_plugin

from throughline_zoom import ZoomPlugin, vtt
from throughline_zoom.client import Zoom, ZoomError, fixture_transport, meeting_path_id

RECORDED = Path(__file__).parent / "recorded" / "api.json"
ACCOUNT, CLIENT, SECRET = "ACCOUNT_CANARY", "invented-client-canary", "invented-secret-canary"
ENV = {
    "ZOOM_ACCOUNT_ID": ACCOUNT,
    "ZOOM_CLIENT_ID": CLIENT,
    "ZOOM_CLIENT_SECRET": SECRET,
    "ZOOM_FIXTURE": str(RECORDED),
    "ZOOM_NOW": "2020-03-20T12:00:00Z",
    "ZOOM_EARLIEST": "2020-01-01",
    "ZOOM_PARTICIPANTS": "1",
    "ZOOM_BATCH_SIZE": "2",
}


@pytest.fixture
def env(monkeypatch):
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    for key in ("ZOOM_OWNER_NAMES", "ZOOM_ACCESS_LABEL"):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def messages(items):
    return [i for i in items if isinstance(i, dict)]


def by_id(items):
    return {m["external_id"]: m for m in messages(items)}


def test_vtt_cues_speakers_and_turns():
    text = (
        "﻿WEBVTT\r\n\r\nNOTE invented\r\n\r\n1\r\n00:00:01.000 --> 00:00:02.500\r\n"
        "Ann Example: Hello <b>there</b>\r\n\r\n2\r\n00:00:02.600 --> 00:00:04.000\r\n"
        "still Ann\r\n\r\n3\r\n01:02.000 --> 01:03.000\r\n<v Bo Example>Hi</v>\r\n\r\n"
        "4\r\n00:01:04.000 --> 00:01:05.000\r\nhttps://example.com: link\r\n"
    )
    cues = vtt.cues(text)
    assert [c["speaker"] for c in cues] == ["Ann Example", None, "Bo Example", None]
    assert cues[0]["text"] == "Hello there" and cues[2]["start_s"] == 62.0
    turns = vtt.turns(cues)
    assert [(t["speaker"], t["cue_count"]) for t in turns] == [("Ann Example", 2), ("Bo Example", 2)]
    assert turns[0]["text"] == "Hello there still Ann" and turns[0]["end_s"] == 4.0
    with pytest.raises(ValueError):
        vtt.cues("<html>login</html>")


def test_meeting_uuid_double_encoding():
    assert meeting_path_id("abcQ==") == "abcQ%3D%3D"
    assert meeting_path_id("/ab+c") == "%252Fab%252Bc"
    assert meeting_path_id("a//b") == "a%252F%252Fb"


def test_backfill_maps_turns_meetings_links(env):
    items = list(ZoomPlugin().records({}, "backfill"))
    rows = by_id(items)
    a = rows["inventedAAA1+Q=="]
    assert a["channel"] == "zoom.meetings"
    assert a["happened_at"] == "2020-01-14T10:00:00-05:00"
    assert a["ended_at"] == "2020-01-14T10:30:00-05:00"
    assert a["author"] == {"system": "email", "kind": "email", "value": "owner@example.com",
                           "display_name": "Invented Owner"}
    assert a["fields"]["transcript_status"] == "complete" and a["fields"]["turn_count"] == 5
    assert a["fields"]["participants"] == [
        {"name": "Invented Owner", "email": None},
        {"name": "Bea Example", "email": "bea@example.com"},
        {"name": "Cal Example", "email": "cal@example.com"},
    ]
    assert "password" not in json.dumps(a["payload"]) and ACCOUNT not in json.dumps(items, default=str)
    first = rows["inventedAAA1+Q==:1"]
    # recording_start 15:00:30Z + 1s cue offset, in the meeting time zone
    assert first["happened_at"] == "2020-01-14T10:00:31-05:00"
    assert first["content"] == "Welcome, this is the invented orchard call. We will plan the apple rows."
    assert first["fields"]["is_owner"] is True and first["fields"]["author_kind"] == "owner"
    assert first["thread"] == {"external_id": "inventedAAA1+Q==", "title": "Invented orchard planning"}
    bea = rows["inventedAAA1+Q==:2"]
    assert bea["author"]["system"] == "email" and bea["author"]["value"] == "bea@example.com"
    assert bea["fields"]["is_owner"] is False and bea["fields"]["author_kind"] == "other"
    # Participant report 404 (double-encoded uuid): labels stay per-meeting.
    dana = rows["/inventedB//2w==:2"]
    assert dana["author"] == {"system": "zoom", "kind": "speaker_label",
                              "value": "/inventedB//2w==:Dana Example", "display_name": "Dana Example"}
    assert dana["content"] == "Draft is done. and the figures too."
    assert dana["happened_at"] == "2020-02-05T18:01:03.500000+01:00"
    assert rows["/inventedB//2w=="]["fields"]["participants_status"] == "not_found"
    assert rows["inventedCCC3w=="]["fields"]["transcript_status"] == "none"
    # Two transcript files: numbering continues, each timed from its own start.
    assert rows["inventedDDD4w==:3"]["happened_at"] == "2020-03-18T11:12:01-04:00"
    assert rows["inventedDDD4w=="]["fields"]["participant_count"] == 2
    assert rows["inventedEEE5w=="]["fields"]["transcript_status"] == "processing"
    links = [i.data for i in items if isinstance(i, Record) and i.kind == "link"]
    turns = [m for m in messages(items) if m["channel"] == "zoom.transcripts"]
    assert len(links) == len(turns) == 12
    assert links[0] == {"type": "part_of", "method": "source",
                        "from": {"channel": "zoom.transcripts", "external_id": "inventedAAA1+Q==:1"},
                        "to": {"channel": "zoom.meetings", "external_id": "inventedAAA1+Q=="}}
    cov = [i.data for i in items if isinstance(i, Record) and i.kind == "coverage"]
    assert cov[0]["channel"] == "zoom.meetings" and cov[0]["status"] == "partial"
    final = items[-1]
    assert isinstance(final, Checkpoint) and final.cursor["scan"] is None
    assert final.cursor["complete"] is True
    assert final.cursor["pending"] == {"inventedEEE5w==": "2020-03-19T13:00:00Z"}
    assert final.cursor["watermark"] == "2020-03-19T09:00:00-04:00"


def test_resume_from_mid_page_checkpoint(env):
    items = list(ZoomPlugin().records({}, "backfill"))
    checkpoints = [i.cursor for i in items if isinstance(i, Checkpoint)]
    # A checkpoint on the second February page, before meeting C.
    mid = next(c for c in checkpoints if c["scan"] and c["scan"]["token"] == "invented-page-2")
    resumed = by_id(ZoomPlugin().records(mid, "backfill"))
    assert "/inventedB//2w==" not in resumed and "inventedCCC3w==" in resumed
    assert "inventedDDD4w==" in resumed


def test_expired_page_token_restarts_window(env):
    cursor = {"scan": {"start": "2020-01-31", "end": "2020-02-29", "token": "expired", "index": 0}}
    rows = by_id(ZoomPlugin().records(cursor, "sync"))
    assert {"/inventedB//2w==", "inventedCCC3w=="} <= set(rows)


def test_sync_window_uses_overlap_and_pending(env):
    calls = []
    base = fixture_transport(RECORDED)

    def spy(request):
        calls.append(request.url)
        return base.handle_request(request)

    plugin = ZoomPlugin()
    plugin.transport = httpx.MockTransport(spy)
    cursor = {"complete": True, "watermark": "2020-03-18T11:00:00-04:00",
              "pending": {"inventedEEE5w==": "2020-03-10T13:00:00Z"}, "scan": None}
    rows = by_id(plugin.records(cursor, "sync"))
    listed = [u for u in calls if u.path == "/v2/users/me/recordings"]
    assert [(u.params["from"], u.params["to"]) for u in listed] == [("2020-03-10", "2020-03-20")]
    assert "inventedDDD4w==" in rows and "inventedAAA1+Q==" not in rows


def test_participants_off_and_owner_names(env):
    env.setenv("ZOOM_PARTICIPANTS", "0")
    env.setenv("ZOOM_ACCESS_LABEL", "work")
    rows = by_id(ZoomPlugin().records({}, "backfill"))
    a = rows["inventedAAA1+Q=="]
    assert a["fields"]["participants_status"] == "disabled" and a["fields"]["participants"] is None
    assert a["access_label"] == "work"
    bea = rows["inventedAAA1+Q==:2"]
    assert bea["author"]["system"] == "zoom" and bea["fields"]["is_owner"] is False
    assert rows["inventedAAA1+Q==:1"]["fields"]["is_owner"] is True


def test_token_cached_refreshed_on_401_and_secrets_not_in_errors():
    seen = {"token": 0, "api": 0}

    def handler(request):
        if request.url.path == "/oauth/token":
            seen["token"] += 1
            assert request.url.params["account_id"] == "acct" and request.url.params["grant_type"] == "account_credentials"
            assert request.headers["Authorization"].startswith("Basic ")
            return httpx.Response(200, json={"access_token": f"tok{seen['token']}", "expires_in": 3599})
        seen["api"] += 1
        if request.headers["Authorization"] == "Bearer tok1" and seen["api"] == 2:
            return httpx.Response(401, json={"code": 124, "message": "Invalid access token."})
        return httpx.Response(200, json={"ok": request.headers["Authorization"]})

    zoom = Zoom("acct", "cid", "csecret", transport=httpx.MockTransport(handler))
    assert zoom.request("/users/me") == {"ok": "Bearer tok1"}
    assert zoom.request("/users/me") == {"ok": "Bearer tok2"}
    assert seen["token"] == 2

    def refuse(request):
        return httpx.Response(400, json={"reason": "Invalid client_id or client_secret csecret"})

    with pytest.raises(ZoomError) as raised:
        Zoom("acct", "cid", "csecret", transport=httpx.MockTransport(refuse)).request("/users/me")
    assert "csecret" not in str(raised.value)


def test_unreachable_source_raises(env):
    plugin = ZoomPlugin()

    def down(request):
        raise httpx.ConnectError("down")

    plugin.transport = httpx.MockTransport(down)
    with pytest.raises(httpx.ConnectError):
        list(plugin.records({}, "backfill"))


def test_zoom_contract_kit():
    result = check_plugin(
        "throughline_zoom:ZoomPlugin",
        ENV,
        secrets=[ACCOUNT, CLIENT, SECRET],
    )
    assert result["messages"] == 17
