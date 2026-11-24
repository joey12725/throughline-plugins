import json
from pathlib import Path

import psycopg
import pytest
from throughline_sdk import Checkpoint
from throughline_sdk.contract import check_plugin

from throughline_wave import Wave, recording_message, turn_message

RECORDED = Path(__file__).parent / "recorded"


def plugin(monkeypatch, source, **env):
    monkeypatch.setenv("WAVE_SOURCE", source)
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    return Wave()


def messages(records):
    return [r for r in records if isinstance(r, dict)]


def test_msg_1_msg_3_turn_timing_manual_and_location():
    recording = {
        "id": "fake-r",
        "title": "Invented",
        "lat": 12,
        "lon": 34,
        "place": "Orchard",
    }
    turn = {
        "turn_no": 1,
        "speaker": "A",
        "text": "Invented words",
        "start_s": 1.25,
        "end_s": 3.75,
        "said_at": "2020-01-01T10:00:01.250+02:00",
    }
    normal = turn_message(recording, turn)
    assert normal["happened_at"] == "2020-01-01T10:00:01.250000+02:00"
    assert normal["ended_at"] == "2020-01-01T10:00:03.750000+02:00"
    turn.update(said_at_manual="2020-01-02T12:30:00-04:00", lat=13, lon=35)
    manual = turn_message(recording, turn)
    assert manual["ended_at"] == "2020-01-02T12:30:02.500000-04:00"
    assert manual["location"] == {"lat": 13, "lon": 35}
    assert manual["fields"]["place"] == "Orchard"
    turn["end_s"] = None
    assert turn_message(recording, turn)["ended_at"] is None


def test_idn_2_idn_8_speaker_mapping_and_idn_6_ignored():
    r = {"id": "fake-r"}
    t = {
        "turn_no": 2,
        "speaker": "A",
        "text": "Invented",
        "said_at": "2020-01-01Z",
        "ignore_reason": "synthetic",
    }
    # Use an explicit aware instant, never infer a timezone.
    t["said_at"] = "2020-01-01T00:00:00Z"
    profile = {
        "system": "wave",
        "kind": "voice_profile",
        "value": "fake-profile",
        "provenance": "user_rename",
    }
    m = turn_message(r, t, profile)
    assert m["author"] == {
        "system": "wave",
        "kind": "voice_profile",
        "value": "fake-profile",
    }
    assert m["fields"]["ignore_reason"] == "synthetic"
    assert m["fields"]["speaker_provenance"] == "user_rename"
    assert turn_message(r, t)["author"]["value"] == "fake-r:A"
    assert turn_message({"id": "fake-other"}, t)["author"]["value"] == "fake-other:A"


def test_msg_1_recording_interval_summary_empty_text():
    r = {
        "id": "fake-r",
        "started_at": "2020-01-01T00:00:00Z",
        "duration_s": 60,
        "metadata": {"summary": "Invented summary", "type": "meeting"},
    }
    m = recording_message(r, 2)
    assert m["ended_at"] == "2020-01-01T00:01:00+00:00"
    assert m["content"] == "Untitled Wave recording\n\nInvented summary"
    assert m["fields"]["turn_count"] == 2
    t = {"turn_no": 1, "text": " ", "said_at": r["started_at"]}
    assert turn_message(r, t)["content"].strip()


def test_plg_4_archive_keyset_resume_and_late_edits(monkeypatch, archive_dsn):
    p = plugin(monkeypatch, "archive", WAVE_ARCHIVE_DSN=archive_dsn)
    records = list(p.records({}, "backfill"))
    assert len(messages(records)) == 15
    assert list(p.records({}, "sync")) == records
    for index, record in enumerate(records):
        if isinstance(record, Checkpoint) and record.cursor:
            resumed = messages(p.records(record.cursor, "backfill"))
            expected = messages(records[index + 1 :])
            # Mid-recording resume replays its parent, which is idempotent.
            assert {m["external_id"] for m in expected} <= {
                m["external_id"] for m in resumed
            }
    with psycopg.connect(archive_dsn) as conn:
        conn.execute(
            "UPDATE transcripts.turn SET text='Invented corrected words' WHERE recording_id='fake-recording-0' AND turn_no=1"
        )
    refreshed = messages(p.records({}, "sync"))
    assert (
        next(m for m in refreshed if m["external_id"] == "fake-recording-0:1")[
            "content"
        ]
        == "Invented corrected words"
    )


def test_plg_6_archive_contract(archive_dsn):
    check_plugin(
        "throughline_wave:Wave",
        {"WAVE_SOURCE": "archive", "WAVE_ARCHIVE_DSN": archive_dsn},
    )


def test_plg_6_api_contract():
    check_plugin(
        "throughline_wave:Wave",
        {
            "WAVE_SOURCE": "api",
            "WAVE_API_FIXTURE": str(RECORDED / "api.json"),
            "WAVE_API_TOKEN": "fake-wave-secret-canary",
        },
        secrets=["fake-wave-secret-canary"],
    )


def test_plg_4_api_resume(monkeypatch):
    p = plugin(monkeypatch, "api", WAVE_API_FIXTURE=RECORDED / "api.json")
    records = list(p.records({}, "backfill"))
    assert len(messages(records)) == 9
    for index, record in enumerate(records):
        if isinstance(record, Checkpoint) and record.cursor:
            assert messages(p.records(record.cursor, "sync")) == messages(
                records[index + 1 :]
            )


def test_msg_6_api_raw_and_no_network(monkeypatch):
    import httpx

    monkeypatch.setattr(
        httpx.Client, "get", lambda *a, **k: pytest.fail("source network called")
    )
    p = plugin(monkeypatch, "api", WAVE_API_FIXTURE=RECORDED / "api.json")
    records = messages(p.records({}, "sync"))
    fixture = json.loads((RECORDED / "api.json").read_text())
    assert records[0]["payload"] == fixture["sessions/fake-session-3"]
    assert (
        records[1]["payload"]["segment"]
        == fixture["sessions/fake-session-3/transcript"]["segments"][0]
    )


def test_plg_2_api_http_paths_auth_and_retry(monkeypatch):
    import httpx
    import throughline_wave

    fixture = json.loads((RECORDED / "api.json").read_text())
    calls = []
    failures = [429, 503]
    original = httpx.Client

    def respond(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer fake-http-token"
        assert request.url.host == "api.wave.co"
        assert request.url.path.startswith("/v1/sessions")
        if failures:
            return httpx.Response(failures.pop(0), headers={"Retry-After": "0"})
        path = request.url.path.removeprefix("/v1/")
        if request.url.params.get("cursor"):
            path += "?cursor=" + request.url.params["cursor"]
        return httpx.Response(200, json=fixture[path])

    monkeypatch.delenv("WAVE_API_FIXTURE", raising=False)
    monkeypatch.setattr(throughline_wave.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: original(**kw, transport=httpx.MockTransport(respond)),
    )
    p = plugin(monkeypatch, "api", WAVE_API_TOKEN="fake-http-token")
    assert len(messages(p.records({}, "sync"))) == 9
    assert len(calls) == 10
    assert calls[0].url.params["limit"] == "100"


def test_plg_6_api_http_error_omits_secret_body(monkeypatch):
    import httpx
    import throughline_wave

    original = httpx.Client
    monkeypatch.delenv("WAVE_API_FIXTURE", raising=False)
    monkeypatch.setattr(throughline_wave.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: original(
            **kw,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(401, text="fake-secret-in-upstream-body")
            ),
        ),
    )
    p = plugin(monkeypatch, "api", WAVE_API_TOKEN="fake-token")
    with pytest.raises(ValueError, match="HTTP 401") as error:
        list(p.records({}, "backfill"))
    assert "fake-secret" not in str(error.value)


def test_plg_4_api_resume_uses_saved_page_when_new_sessions_shift_it(
    monkeypatch, tmp_path
):
    fixture = json.loads((RECORDED / "api.json").read_text())
    path = tmp_path / "shifted.json"
    path.write_text(json.dumps(fixture), encoding="utf-8")
    p = plugin(monkeypatch, "api", WAVE_API_FIXTURE=path)
    records = list(p.records({}, "backfill"))
    checkpoint = next(
        r for r in records if isinstance(r, Checkpoint) and r.cursor.get("index") == 1
    )
    fixture["sessions"]["sessions"] = [{"id": "fake-new-session"}]
    path.write_text(json.dumps(fixture), encoding="utf-8")
    resumed = messages(p.records(checkpoint.cursor, "sync"))
    assert {r["external_id"] for r in resumed if r["channel"] == "recordings.wave"} == {
        "fake-session-2",
        "fake-session-1",
    }


def test_wave_whole_thread_manifest():
    from throughline.models import Channel

    channel = next(c for c in Wave.manifest.channels if c.name == "transcripts.wave")
    assert channel.model_dump()["conversation_whole_thread"] is True
    assert Channel.model_validate(
        dict(channel.model_dump(), plugin="wave")
    ).conversation_whole_thread


def test_plg_4_null_start_and_bad_turn_do_not_block(archive_dsn, monkeypatch, caplog):
    with psycopg.connect(archive_dsn) as conn:
        conn.execute(
            "UPDATE transcripts.recording SET started_at=NULL WHERE id='fake-recording-1'"
        )
        conn.execute(
            "UPDATE transcripts.turn SET start_s=12,end_s=10 WHERE recording_id='fake-recording-1' AND turn_no=2"
        )
    p = plugin(monkeypatch, "archive", WAVE_ARCHIVE_DSN=archive_dsn)
    rows = messages(p.records({}, "backfill"))
    assert len(rows) == 15
    parent = next(r for r in rows if r["external_id"] == "fake-recording-1")
    turns = [r for r in rows if r["external_id"].startswith("fake-recording-1:")]
    assert parent["happened_at"] == min(r["happened_at"] for r in turns)
    assert parent["fields"]["time_precision"] == "first_turn"
    assert parent["payload"]["started_at"] is None
    assert turns[1]["ended_at"] is None
    with psycopg.connect(archive_dsn) as conn:
        conn.execute(
            "DELETE FROM transcripts.turn WHERE recording_id='fake-recording-1'"
        )
    records = list(p.records({}, "backfill"))
    assert "fake-recording-2" in {r["external_id"] for r in messages(records)}
    assert p.quarantined == 1
    assert "fake-recording-1" in caplog.text
    assert "Invented orchard planning" not in caplog.text


def test_plg_2_api_poll_uses_persisted_since_and_daily_budget(monkeypatch):
    import throughline_wave
    import httpx

    fixture = json.loads((Path(__file__).parent / "recorded" / "api.json").read_text())
    calls = []
    original = httpx.Client

    def respond(request):
        calls.append(request)
        path = request.url.path.removeprefix("/v1/")
        if request.url.params.get("since"):
            return httpx.Response(200, json={"sessions": [], "has_more": False})
        if request.url.params.get("cursor"):
            path += "?cursor=" + request.url.params["cursor"]
        return httpx.Response(200, json=fixture[path])

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: original(**kw, transport=httpx.MockTransport(respond)),
    )
    monkeypatch.setattr(throughline_wave.time, "sleep", lambda _: None)
    monkeypatch.delenv("WAVE_API_FIXTURE", raising=False)
    p = plugin(monkeypatch, "api", WAVE_API_TOKEN="fake-token")
    first = list(p.records({}, "backfill"))
    cursor = first[-1].cursor
    assert cursor["since"]
    calls.clear()
    second = list(p.records(cursor, "sync"))
    assert not messages(second)
    assert len(calls) == 1
    # The list is asked from the watermark minus the 168-hour overlap, in UTC,
    # so sessions that finish processing late are still seen.
    assert cursor["since"] == "2020-01-03T10:00:00+02:00"
    assert calls[0].url.params["since"] == "2019-12-27T08:00:00Z"
    assert second[-1].cursor["budget"]["requests"] == cursor["budget"]["requests"] + 5
    cursor["budget"]["requests"] = 9000
    calls.clear()
    assert not messages(p.records(cursor, "sync"))
    assert not calls


def test_archive_overflow_and_attribute_errors_do_not_block(archive_dsn, monkeypatch):
    """Every mapping error is quarantined in the archive path, as in the API path."""
    with psycopg.connect(archive_dsn) as conn:
        # timedelta overflows on the interval: OverflowError from a turn.
        conn.execute(
            "UPDATE transcripts.turn SET end_s=1e20 WHERE recording_id='fake-recording-0' AND turn_no=2"
        )
        # A JSON string where an object is expected: AttributeError from a recording.
        conn.execute(
            "UPDATE transcripts.recording SET metadata='\"invented\"' WHERE id='fake-recording-1'"
        )
    p = plugin(monkeypatch, "archive", WAVE_ARCHIVE_DSN=archive_dsn)
    records = list(p.records({}, "backfill"))
    ids = {r["external_id"] for r in messages(records)}
    assert "fake-recording-0:2" not in ids and "fake-recording-1" not in ids
    assert {"fake-recording-0:3", "fake-recording-0:4", "fake-recording-2"} <= ids
    assert {f"fake-recording-2:{n}" for n in range(1, 5)} <= ids
    assert p.quarantined == 2
    assert records[-1].cursor == {}


def late_fixture(tmp_path, sessions):
    """Invented API whose list shows only the named sessions, newest first."""
    fixture = json.loads((RECORDED / "api.json").read_text())
    fixture["sessions"] = {
        "sessions": [{"id": s} for s in sessions],
        "has_more": False,
        "next_cursor": None,
    }
    path = tmp_path / "late.json"
    path.write_text(json.dumps(fixture), encoding="utf-8")
    return fixture, path


def recordings(records):
    return [
        r["external_id"] for r in messages(records) if r["channel"] == "recordings.wave"
    ]


def test_api_sync_imports_session_listed_after_a_newer_one(monkeypatch, tmp_path):
    """A session that finishes processing late is behind the watermark when listed."""
    fixture, path = late_fixture(tmp_path, ["fake-session-3"])
    p = plugin(monkeypatch, "api", WAVE_API_FIXTURE=path)
    first = list(p.records({}, "sync"))
    assert recordings(first) == ["fake-session-3"]
    cursor = first[-1].cursor
    assert instant_of(cursor["since"]) == instant_of(
        fixture["sessions/fake-session-3"]["timestamp"]
    )
    # Session 2 was recorded a day earlier and only now appears in the list.
    late_fixture(tmp_path, ["fake-session-3", "fake-session-2"])
    second = list(p.records(cursor, "sync"))
    assert recordings(second) == ["fake-session-2"]
    assert len(messages(second)) == 3
    # The watermark does not move backwards and nothing is fetched twice.
    assert second[-1].cursor["since"] == cursor["since"]
    assert set(second[-1].cursor["seen"]) == {"fake-session-3", "fake-session-2"}
    third = list(p.records(second[-1].cursor, "sync"))
    assert not messages(third)
    # Beyond the overlap the list is not consulted; documented, needs backfill.
    monkeypatch.setenv("WAVE_API_OVERLAP_HOURS", "1")
    assert not messages(Wave().records(cursor, "sync"))


def test_api_quarantined_session_is_retried_by_later_polls(monkeypatch, tmp_path):
    fixture, path = late_fixture(tmp_path, ["fake-session-3", "fake-session-2"])
    transcript = fixture["sessions/fake-session-2/transcript"]
    fixture["sessions/fake-session-2/transcript"] = {"id": "fake-session-2"}
    path.write_text(json.dumps(fixture), encoding="utf-8")
    p = plugin(monkeypatch, "api", WAVE_API_FIXTURE=path)
    first = list(p.records({}, "sync"))
    assert recordings(first) == ["fake-session-3"]
    assert p.quarantined == 1
    assert set(first[-1].cursor["seen"]) == {"fake-session-3"}
    cursor = first[-1].cursor
    assert cursor["failed"]["fake-session-2"][1] == 1
    # Still unprocessed when the retry is due: asked again, still not imported.
    again = list(p.records(due(cursor), "sync"))
    assert not messages(again) and p.quarantined == 2
    assert again[-1].cursor["failed"]["fake-session-2"][1] == 2
    fixture["sessions/fake-session-2/transcript"] = transcript
    path.write_text(json.dumps(fixture), encoding="utf-8")
    later = list(p.records(due(again[-1].cursor), "sync"))
    assert recordings(later) == ["fake-session-2"]
    assert later[-1].cursor["failed"] == {}


def due(cursor):
    """The same cursor once the retry time of every waiting session has passed."""
    from datetime import datetime, timedelta, timezone

    cursor = json.loads(json.dumps(cursor))
    for entry in cursor["failed"].values():
        entry[2] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    return cursor


def test_api_quarantined_session_backs_off_and_reserves_no_budget(
    monkeypatch, tmp_path
):
    from datetime import datetime, timezone

    fixture, path = late_fixture(tmp_path, ["fake-session-3", "fake-session-2"])
    transcript = fixture["sessions/fake-session-2/transcript"]
    fixture["sessions/fake-session-2/transcript"] = {"id": "fake-session-2"}
    path.write_text(json.dumps(fixture), encoding="utf-8")
    p = plugin(monkeypatch, "api", WAVE_API_FIXTURE=path)
    cursor = list(p.records({}, "sync"))[-1].cursor
    assert p.quarantined == 1
    spent = cursor["budget"]["requests"]
    # Polls before the retry time ask only for the list: no detail or
    # transcript request, no reserved budget, no repeated quarantine.
    for _ in range(3):
        cursor = list(p.records(cursor, "sync"))[-1].cursor
        spent += 5
        assert cursor["budget"]["requests"] == spent
    assert p.quarantined == 1
    assert cursor["failed"]["fake-session-2"][1] == 1
    # The wait doubles with each failed attempt and stops at a day.
    waits = []
    for _ in range(7):
        cursor = list(p.records(due(cursor), "sync"))[-1].cursor
        left = instant_of(cursor["failed"]["fake-session-2"][2]) - datetime.now(
            timezone.utc
        )
        waits.append(round(left.total_seconds() / 3600))
    assert waits == [2, 4, 8, 16, 24, 24, 24]
    # A changed list row is retried at once, whatever the wait.
    fixture["sessions"]["sessions"][1]["title"] = "Processed"
    fixture["sessions/fake-session-2/transcript"] = transcript
    path.write_text(json.dumps(fixture), encoding="utf-8")
    done = list(p.records(cursor, "sync"))
    assert recordings(done) == ["fake-session-2"]
    assert done[-1].cursor["failed"] == {}
    # An entry whose session is no longer listed does not stay forever.
    cursor["failed"]["fake-gone"] = ["x", 3, "2000-01-01T00:00:00+00:00"]
    assert "fake-gone" not in list(p.records(cursor, "sync"))[-1].cursor["failed"]


def test_api_seen_map_stays_bounded_and_reads_old_cursor(monkeypatch):
    p = plugin(
        monkeypatch,
        "api",
        WAVE_API_FIXTURE=RECORDED / "api.json",
        WAVE_API_OVERLAP_HOURS="30",
    )
    done = list(p.records({}, "backfill"))[-1].cursor
    # Sessions are a day apart: only those within 30 hours of the newest stay.
    assert set(done["seen"]) == {"fake-session-3", "fake-session-2"}
    # A cursor from before the overlap carried {id: signature} at the watermark.
    old = {k: v for k, v in done.items() if k != "seen"}
    old["boundary"] = {"fake-session-3": done["seen"]["fake-session-3"][0]}
    resumed = list(p.records(old, "sync"))
    assert recordings(resumed) == ["fake-session-2"]
    assert resumed[-1].cursor["seen"] == done["seen"]


def instant_of(value):
    from throughline_wave import instant

    return instant(value)
