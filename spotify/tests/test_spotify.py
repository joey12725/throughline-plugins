import copy
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from throughline_sdk import Checkpoint, Record
from throughline_sdk.contract import check_plugin

from throughline_spotify import Recording, Spotify, history, observe, playlist
from throughline_spotify.api import ComposioSpotifyAPI, SpotifyAPI
from throughline_spotify.auth import callback_values
from throughline_spotify.normalize import accept, api_play, export_play, relationships
from throughline_spotify.remote_snapshot import build_snapshot, playlist_id

ROOT = Path(__file__).parent / "recorded"
SESSION = json.loads((ROOT / "session.json").read_text())
TRACK = SESSION["recent"][0]["track"]


def test_plg_6_contract():
    try:
        check_plugin(
            "throughline_spotify:Spotify",
            {
                "SPOTIFY_RECORDING": str(ROOT / "session.json"),
                "SPOTIFY_EXPORT_PATH": str(ROOT),
                "SPOTIFY_CLIENT_SECRET": "invented-secret-canary-spotify",
            },
            secrets=["invented-secret-canary-spotify"],
        )
    except subprocess.CalledProcessError as error:
        # Avoid pytest's long traceback displaying inherited process environments.
        stderr = (error.stderr or b"").decode(errors="replace")
        pytest.fail("Contract subprocess failed: " + stderr[-2000:], pytrace=False)


def test_plg_4_cursor_50_item_windows(tmp_path):
    rows = []
    for n in range(125):
        row = copy.deepcopy(SESSION["recent"][0])
        row["played_at"] = (
            datetime(2020, 1, 1, tzinfo=UTC) + timedelta(minutes=n)
        ).isoformat()
        rows.append(row)
    recording = tmp_path / "windows.json"
    recording.write_text(json.dumps({"now": 1, "recent": rows}))

    class Checked(Recording):
        def get(self, path, params=None, **kwargs):
            assert not ("before" in params and "after" in params)
            assert params["limit"] == 50
            return super().get(path, params, **kwargs)

    api = Checked(recording)
    assert len(history(api)) == 125
    assert (
        len(
            history(
                api, int(datetime(2020, 1, 1, 0, 30, tzinfo=UTC).timestamp() * 1000)
            )
        )
        == 94
    )


def test_plg_4_repeated_cursor_terminates():
    class Stuck:
        def get(self, *args, **kwargs):
            return {"items": [SESSION["recent"][0]] * 50}

    assert len(history(Stuck())) == 1


def test_plg_2_refresh_rotation_and_401(tmp_path):
    calls, sleeps = [], []
    file = tmp_path / "token.json"

    def handler(request):
        calls.append(request)
        if request.url.host == "accounts.spotify.com":
            assert b"grant_type=refresh_token" in request.content
            assert request.headers["authorization"].startswith("Basic ")
            return httpx.Response(
                200,
                json={
                    "access_token": "invented-access",
                    "refresh_token": "invented-rotated",
                    "expires_in": 3600,
                },
            )
        if len(calls) == 2:
            return httpx.Response(401)
        return httpx.Response(200, json={"items": []})

    api = SpotifyAPI(
        "invented-id",
        "invented-secret",
        "invented-refresh",
        file,
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
    )
    try:
        assert api.get("me/player/recently-played") == {"items": []}
        assert len(calls) == 4
        assert json.loads(file.read_text()) == {"refresh_token": "invented-rotated"}
        assert b"invented-rotated" in calls[2].content
    finally:
        api.close()


def test_plg_6_expired_refresh_requires_auth(tmp_path):
    file = tmp_path / "token.json"
    file.write_text('{"refresh_token":"invented-expired"}')
    api = SpotifyAPI(
        "fake",
        "fake",
        "invented-expired",
        file,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(400, json={"error": "invalid_grant"})
        ),
    )
    try:
        with pytest.raises(RuntimeError, match="authorization expired"):
            api.refresh()
        assert json.loads(file.read_text()) == {}
    finally:
        api.close()


@pytest.mark.parametrize("status", [429, 503])
def test_plg_2_retry_after(status):
    sleeps, calls = [], []

    def handler(request):
        calls.append(1)
        return (
            httpx.Response(status, headers={"Retry-After": "7"})
            if len(calls) == 1
            else httpx.Response(200, json={})
        )

    api = SpotifyAPI(
        "fake",
        "fake",
        "fake",
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
    )
    try:
        assert api.request("GET", "https://api.spotify.com/v1/fake").status_code == 200
        assert sleeps == [7.0 if status == 429 else 1]
    finally:
        api.close()


def test_plg_2_retry_budget():
    sleeps = []
    api = SpotifyAPI(
        "fake",
        "fake",
        "fake",
        transport=httpx.MockTransport(lambda r: httpx.Response(429)),
        sleep=sleeps.append,
    )
    try:
        with pytest.raises(RuntimeError, match="budget"):
            api.request("GET", "https://api.spotify.com/v1/fake")
        assert len(sleeps) == 4
    finally:
        api.close()


@pytest.mark.parametrize("status", [403, 404])
def test_plg_5_context_resolution_failure_cached(status):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(status)

    api = SpotifyAPI("fake", "fake", "fake", transport=httpx.MockTransport(handler))
    api.access_token, api.expires = "fake", float("inf")
    cache = {}
    context = {"type": "playlist", "uri": "spotify:playlist:inventedprivate"}
    try:
        assert playlist(api, context, cache, 1) == (None, None)
        assert playlist(api, context, cache, 2) == (None, None)
        assert len(calls) == 1
        play = api_play(dict(SESSION["recent"][0], context=context))
        assert play[0]["fields"]["context_uri"] == context["uri"]
        assert any(r.data["type"] == "played_from" for r in relationships(play))
    finally:
        api.close()


def test_msg_1_msg_6_export_parsing():
    raw = json.loads((ROOT / "Streaming_History_Audio_0.json").read_text())[0]
    message = export_play(raw)[0]
    assert message["payload"] == raw
    assert message["happened_at"] == "2019-02-03T10:00:00+00:00"
    assert message["ended_at"] == "2019-02-03T10:03:00+00:00"
    assert message["fields"]["ms_played"] == 180000
    assert message["fields"]["context_uri"] is None
    assert message["fields"]["duration_ms"] is None
    assert message["fields"]["reason_end"] == "trackdone"


def test_msg_1_short_export():
    raw = json.loads((ROOT / "StreamingHistory0.json").read_text())[0]
    message = export_play(raw)[0]
    assert message["happened_at"] == "2018-01-02T10:00:00+00:00"
    assert message["fields"]["track_uri"] is None
    assert message["fields"]["timing_basis"] == "export_minute_precision"


def test_msg_1_podcasts():
    raw = json.loads((ROOT / "Streaming_History_Audio_0.json").read_text())[1]
    message = export_play(raw)[0]
    assert message["content"] == "Listened to Invented Episode from Invented Show"
    item = {
        "type": "episode",
        "name": "Invented Episode",
        "uri": "spotify:episode:inventedepisode",
        "show": {"name": "Invented Show"},
    }
    play = api_play({"played_at": "2020-01-01T10:00:00Z", "track": item})
    assert play[0]["content"] == message["content"]
    assert next(relationships(play)).data["to"]["kind"] == "work"


def test_med_1_work_shared_across_plays():
    one = api_play(SESSION["recent"][0])
    two = api_play(SESSION["recent"][1])
    assert one[0]["external_id"] != two[0]["external_id"]
    assert next(relationships(one)).data["to"] == next(relationships(two)).data["to"]
    assert {r.data["type"] for r in relationships(one)} == {
        "consumed",
        "part_of",
        "created_by",
        "played_from",
    }


@pytest.mark.parametrize("reverse", [False, True])
def test_msg_2_cross_source_deduplication(reverse):
    raw = json.loads((ROOT / "Streaming_History_Audio_0.json").read_text())[0]
    exported = export_play(raw)
    live = api_play(
        {"track": TRACK, "played_at": "2019-02-03T10:00:00Z"},
        end="2019-02-03T10:03:00Z",
        uncertainty=2000,
    )
    first, second = (exported, live) if reverse else (live, exported)
    ledger = {}
    assert accept(first, ledger)
    assert not accept(second, ledger)
    assert not accept(second, ledger)
    assert len(ledger) == 2


def test_msg_2_uncertain_plays_kept_separate():
    raw = json.loads((ROOT / "Streaming_History_Audio_0.json").read_text())[0]
    exported = export_play(raw)
    live = api_play(
        {"track": TRACK, "played_at": "2019-02-03T10:00:00Z"},
        end="2019-02-03T10:03:00Z",
        uncertainty=15000,
    )
    ledger = {}
    assert accept(live, ledger)
    assert accept(exported, ledger)


def test_plg_4_observation_checkpoint_and_real_end():
    raw = {
        "item": TRACK,
        "progress_ms": 0,
        "is_playing": True,
        "context": SESSION["recent"][0]["context"],
    }
    now = 1577959200000
    completed, pending = observe(raw, None, now)
    assert completed is None
    for progress in range(15000, 180000, 15000):
        completed, pending = observe(
            dict(raw, progress_ms=progress), pending, now + progress
        )
        assert completed is None
    next_item = dict(TRACK, uri="spotify:track:inventednext")
    completed, pending = observe(dict(raw, item=next_item), pending, now + 180000)
    assert completed[0]["ended_at"] == "2020-01-02T10:03:00+00:00"
    assert completed[0]["fields"]["end_uncertainty_ms"] == 2000
    assert pending["uri"] == next_item["uri"]


def test_msg_1_pause_seek_and_long_gap_not_exact():
    raw = {"item": TRACK, "progress_ms": 10000, "is_playing": True}
    _, pending = observe(raw, None, 100000)
    _, pending = observe(dict(raw, progress_ms=90000), pending, 115000)
    assert not pending["continuous"]
    completed, _ = observe(None, pending, 200000)
    assert completed[0]["ended_at"] is None


def test_plg_6_auth_state_validation():
    assert (
        callback_values("/callback?state=invented&code=fake-code", "invented")
        == "fake-code"
    )
    for path in (
        "/callback?state=wrong&code=x",
        "/other?state=invented&code=x",
        "/callback?state=invented&error=denied",
    ):
        with pytest.raises(ValueError):
            callback_values(path, "invented")


def test_plg_2_plg_4_shared_generator_replay(monkeypatch):
    monkeypatch.setenv("SPOTIFY_RECORDING", str(ROOT / "session.json"))
    monkeypatch.setenv("SPOTIFY_EXPORT_PATH", str(ROOT))
    plugin = Spotify()
    records = list(plugin.records({}, "backfill"))
    cursor = [r.cursor for r in records if isinstance(r, Checkpoint)][-1]
    assert len([r for r in records if isinstance(r, dict)]) == 19
    assert not [r for r in plugin.records(cursor, "sync") if isinstance(r, dict)]


def test_plg_4_live_restart_history_merge_then_export(monkeypatch, tmp_path):
    recording = tmp_path / "session.json"
    monkeypatch.setenv("SPOTIFY_RECORDING", str(recording))
    monkeypatch.delenv("SPOTIFY_EXPORT_PATH", raising=False)
    row = copy.deepcopy(SESSION["recent"][0])
    base = int(datetime.fromisoformat(row["played_at"]).timestamp() * 1000)
    cursor = {}
    messages = []
    for progress in range(15000, 195000, 15000):
        item = (
            TRACK
            if progress < 180000
            else dict(TRACK, uri="spotify:track:inventednext")
        )
        playback = {
            "item": item,
            "progress_ms": progress if progress < 180000 else progress - 180000,
            "is_playing": True,
            "context": row["context"],
            "device": {"name": "Invented Speaker"},
        }
        recording.write_text(
            json.dumps(
                {
                    "now": base + progress,
                    "playback": playback,
                    "recent": [row],
                    "playlists": SESSION["playlists"],
                }
            )
        )
        # Construct a fresh plugin each time to simulate process restart.
        emitted = list(Spotify().records(cursor, "sync"))
        messages.extend(r for r in emitted if isinstance(r, dict))
        cursor = [r.cursor for r in emitted if isinstance(r, Checkpoint)][-1]
    assert len(messages) == 1
    assert messages[0]["external_id"] == api_play(row)[0]["external_id"]
    assert messages[0]["fields"]["playlist_name"] == "Invented Playlist"
    assert messages[0]["fields"]["device"]["name"] == "Invented Speaker"
    raw_export = {
        "ts": (
            datetime.fromisoformat(row["played_at"]) + timedelta(minutes=3)
        ).isoformat(),
        "ms_played": 180000,
        "master_metadata_track_name": TRACK["name"],
        "spotify_track_uri": TRACK["uri"],
    }
    (tmp_path / "Streaming_History_Audio_0.json").write_text(json.dumps([raw_export]))
    monkeypatch.setenv("SPOTIFY_EXPORT_PATH", str(tmp_path))
    replay = list(Spotify().records(cursor, "backfill"))
    assert not [r for r in replay if isinstance(r, dict)]


def test_msg_2_ambiguous_cross_source_match_preserves_play():
    raw = json.loads((ROOT / "Streaming_History_Audio_0.json").read_text())[0]
    first = api_play(
        {"track": TRACK, "played_at": "2019-02-03T10:00:00Z"},
        end="2019-02-03T10:03:00Z",
        uncertainty=2000,
    )
    second = api_play(
        {"track": TRACK, "played_at": "2019-02-03T10:00:01Z"},
        end="2019-02-03T10:03:01Z",
        uncertainty=2000,
    )
    ledger = {}
    assert accept(first, ledger)
    assert accept(second, ledger)
    assert accept(export_play(raw), ledger)


def test_msg_1_history_null_context_retains_observed_context():
    raw = {
        "item": TRACK,
        "progress_ms": 1000,
        "is_playing": True,
        "context": {"type": "playlist", "uri": "spotify:playlist:inventedprivate"},
    }
    _, pending = observe(raw, None, 1577959201000)
    pending["history"] = {
        "track": TRACK,
        "played_at": "2020-01-02T10:00:00Z",
        "context": None,
    }
    completed, _ = observe(None, pending, 1577959216000)
    assert completed[0]["fields"]["context_uri"] == raw["context"]["uri"]
    assert completed[0]["payload"]["history"]["context"] is None


@pytest.mark.parametrize("delay,anchor", [(195000, 0), (180000, 180000), (30000, 6000)])
def test_msg_2_late_history_reconciles_observed_interval(
    monkeypatch, tmp_path, delay, anchor
):
    recording = tmp_path / "late.json"
    monkeypatch.setenv("SPOTIFY_RECORDING", str(recording))
    monkeypatch.delenv("SPOTIFY_EXPORT_PATH", raising=False)
    row = copy.deepcopy(SESSION["recent"][0])
    base = int(datetime.fromisoformat(row["played_at"]).timestamp() * 1000)
    row["played_at"] = datetime.fromtimestamp((base + anchor) / 1000, UTC).isoformat()
    cursor, emitted = {}, []
    for progress in range(0, 225000, 15000):
        item = (
            TRACK
            if progress < 180000
            else dict(TRACK, uri="spotify:track:inventednext")
        )
        recording.write_text(
            json.dumps(
                dict(
                    now=base + progress + 400,
                    playback=dict(
                        item=item, progress_ms=progress % 180000, is_playing=True
                    ),
                    recent=[row] if progress >= delay else [],
                )
            )
        )
        records = list(Spotify().records(cursor, "sync"))
        emitted.extend(
            r
            for r in records
            if isinstance(r, dict) and r["fields"]["track_uri"] == TRACK["uri"]
        )
        cursor = records[-1].cursor
    assert len({r["external_id"] for r in emitted}) == 1
    assert not [r for r in Spotify().records(cursor, "sync") if isinstance(r, dict)]
    recording.write_text(json.dumps(dict(now=base + 900000, recent=[row])))
    replay = list(Spotify().records(cursor, "sync"))
    assert not [
        r
        for r in replay
        if isinstance(r, dict) and r["fields"]["track_uri"] == TRACK["uri"]
    ]


def test_plg_4_full_window_past_the_cursor_reports_a_cap_gap(monkeypatch, tmp_path):
    """Spotify keeps only the newest 50 plays: after a long outage the window
    starts after the stored cursor and the plays in between are reported."""
    start = datetime(2020, 1, 1, tzinfo=UTC)

    def rows(first, count):
        out = []
        for n in range(first, first + count):
            row = copy.deepcopy(SESSION["recent"][0])
            row["played_at"] = (start + timedelta(minutes=5 * n)).isoformat()
            out.append(row)
        return out

    recording = tmp_path / "outage.json"
    monkeypatch.setenv("SPOTIFY_RECORDING", str(recording))
    monkeypatch.delenv("SPOTIFY_EXPORT_PATH", raising=False)

    def run_with(recent, cursor):
        recording.write_text(json.dumps({"now": 1, "recent": recent}))
        out = list(Spotify().records(cursor, "sync"))
        reports = [
            r.data for r in out if isinstance(r, Record) and r.kind == "coverage"
        ]
        return reports, [r.cursor for r in out if isinstance(r, Checkpoint)][-1]

    reports, cursor = run_with(rows(0, 50), {})
    assert reports == []  # first run: no cursor, nothing to compare with
    reports, _ = run_with(rows(100, 50), cursor)
    assert reports == [
        {
            "channel": "music.spotify",
            "scope_key": "",
            "status": "partial",
            "reasons": ["source_cap"],
            "gaps": [
                {
                    "from": "2020-01-01T04:05:00+00:00",
                    "to": "2020-01-01T08:20:00+00:00",
                    "reason": "source_cap",
                }
            ],
            "detail": {"history_rows": 50},
        }
    ]
    # A window that reaches back to the cursor lost nothing.
    reports, _ = run_with(rows(40, 50), cursor)
    assert reports == []


def _composio_wb_response(result, error="", sse=False):
    import base64
    import zlib

    blob = base64.b64encode(
        zlib.compress(json.dumps({"result": result, "error": error}).encode())
    ).decode()
    wb_json = json.dumps(
        {
            "successful": True,
            "data": {"stdout": "THROUGHLINE_SPOTIFY_B64=" + blob + "\n", "stderr": ""},
        }
    )
    envelope = {
        "jsonrpc": "2.0",
        "id": "1",
        "result": {"content": [{"type": "text", "text": wb_json}]},
    }
    if sse:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text="event: message\ndata: " + json.dumps(envelope) + "\n\n",
        )
    return httpx.Response(200, json=envelope)


def test_composio_api_reads_playback_recent_and_playlists():
    seen = []

    def handler(request):
        assert request.headers["authorization"] == "Bearer secret-composio-token"
        rpc = json.loads(request.content)
        code = rpc["params"]["arguments"]["code_to_execute"]
        seen.append(code)
        if "SPOTIFY_GET_PLAYBACK_STATE" in code:
            return _composio_wb_response(
                {"data": {"message": "No active playback", "status": "204"}}, sse=True
            )
        if "SPOTIFY_GET_RECENTLY_PLAYED_TRACKS" in code:
            return _composio_wb_response(
                {
                    "data": {
                        "items": [SESSION["recent"][0]],
                        "cursors": {"before": "1577959200000"},
                    },
                    "error": None,
                }
            )
        if "SPOTIFY_GET_PLAYLIST" in code:
            if "inventedprivate" in code:
                return _composio_wb_response(
                    {
                        "data": {"status_code": 404, "message": "Not found"},
                        "error": '{"error":{"status":404,"message":"Resource not found"}}',
                    },
                    error="404",
                )
            return _composio_wb_response(
                {
                    "data": {
                        "name": "Invented Playlist",
                        "owner": {"id": "owner1", "display_name": "Invented Owner"},
                    },
                    "error": None,
                }
            )
        raise AssertionError("Unexpected tool code: " + code)

    api = ComposioSpotifyAPI(
        "https://composio.example/mcp",
        "secret-composio-token",
        account="spotify_test",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert (
            api.get("me/player", params={"additional_types": "track,episode"}) is None
        )
        recent = api.get("me/player/recently-played", params={"limit": 50})
        assert len(recent["items"]) == 1
        assert (
            api.get(
                "playlists/inventedprivate",
                params={"fields": "name,owner(id,display_name)"},
                missing_ok=True,
            )
            is None
        )
        pl = api.get(
            "playlists/inventedpl",
            params={"fields": "name,owner(id,display_name)"},
            missing_ok=True,
        )
        assert pl["name"] == "Invented Playlist"
        assert all("spotify_test" in c for c in seen)
    finally:
        api.close()


def test_composio_retry_and_secret_redaction():
    sleeps, calls = [], []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return httpx.Response(401, text="secret-leak-body")

    api = ComposioSpotifyAPI(
        "https://secret-url.example/token-in-path",
        "super-secret-token-canary",
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
    )
    try:
        with pytest.raises(RuntimeError) as exc_info:
            api.get("me/player")
        msg = str(exc_info.value)
        assert "401" in msg
        assert "secret-url.example" not in msg
        assert "super-secret-token-canary" not in msg
        assert "secret-leak-body" not in msg
        assert sleeps == [3.0]
    finally:
        api.close()


def test_composio_plugin_source_and_cap_gap(monkeypatch):
    """End-to-end plugin run through Composio preserves source_cap gap reporting."""
    start = datetime(2020, 1, 1, tzinfo=UTC)

    def make_rows(first, count):
        out = []
        for n in range(first, first + count):
            row = copy.deepcopy(SESSION["recent"][0])
            row["context"] = None
            row["played_at"] = (start + timedelta(minutes=5 * n)).isoformat()
            out.append(row)
        return out

    window = {"rows": make_rows(0, 50)}

    def handler(request):
        code = json.loads(request.content)["params"]["arguments"]["code_to_execute"]
        if "SPOTIFY_GET_PLAYBACK_STATE" in code:
            return _composio_wb_response(
                {"data": {"message": "No active playback", "status": "204"}}
            )
        if "SPOTIFY_GET_RECENTLY_PLAYED_TRACKS" in code:
            if '"before":' in code:
                return _composio_wb_response({"data": {"items": [], "cursors": None}})
            rows = list(reversed(window["rows"]))
            return _composio_wb_response(
                {
                    "data": {
                        "items": rows,
                        "cursors": {
                            "before": str(
                                int(
                                    datetime.fromisoformat(
                                        rows[-1]["played_at"]
                                    ).timestamp()
                                    * 1000
                                )
                            )
                        },
                    }
                }
            )
        raise AssertionError("Unexpected tool call")

    monkeypatch.delenv("SPOTIFY_RECORDING", raising=False)
    monkeypatch.delenv("SPOTIFY_EXPORT_PATH", raising=False)
    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
    monkeypatch.setenv("SPOTIFY_COMPOSIO_MCP_URL", "https://composio.example/mcp")
    monkeypatch.setenv("SPOTIFY_COMPOSIO_MCP_TOKEN", "invented-composio-token")

    plugin = Spotify()
    monkeypatch.setattr(
        plugin,
        "source",
        lambda: ComposioSpotifyAPI(
            "https://composio.example/mcp",
            "invented-composio-token",
            transport=httpx.MockTransport(handler),
        ),
    )

    first_run = list(plugin.records({}, "sync"))
    assert len([r for r in first_run if isinstance(r, dict)]) == 50
    cursor = [r.cursor for r in first_run if isinstance(r, Checkpoint)][-1]

    window["rows"] = make_rows(100, 50)
    second_run = list(plugin.records(cursor, "sync"))
    reports = [
        r.data for r in second_run if isinstance(r, Record) and r.kind == "coverage"
    ]
    assert len(reports) == 1
    assert reports[0]["status"] == "partial"
    assert reports[0]["reasons"] == ["source_cap"]
    assert reports[0]["gaps"][0]["from"] == "2020-01-01T04:05:00+00:00"
    assert reports[0]["gaps"][0]["to"] == "2020-01-01T08:20:00+00:00"


def test_remote_snapshot_builds_recording_and_verifies_identity():
    class Client:
        def call_many(self, calls):
            if calls[0][0] == "profile":
                row = copy.deepcopy(SESSION["recent"][0])
                row["context"] = {
                    "type": "playlist",
                    "uri": "spotify:user:owner:playlist:inventedpl",
                }
                return {
                    "profile": {
                        "result": {"data": {"id": "invented-listener"}},
                        "error": None,
                    },
                    "recent": {
                        "result": {"data": {"items": [row]}},
                        "error": None,
                    },
                    "playback": {
                        "result": {
                            "data": {
                                "message": "No active playback",
                                "status": "204",
                            }
                        },
                        "error": None,
                    },
                }
            assert calls == [
                (
                    "playlist:inventedpl",
                    "SPOTIFY_GET_PLAYLIST",
                    {
                        "playlist_id": "inventedpl",
                        "fields": "name,owner(id,display_name)",
                    },
                )
            ]
            return {
                "playlist:inventedpl": {
                    "result": {
                        "data": {
                            "name": "Invented Playlist",
                            "owner": {"id": "owner"},
                        }
                    },
                    "error": None,
                }
            }

    snapshot = build_snapshot(Client(), expected_user="invented-listener", now_ms=1234)
    assert snapshot["now"] == 1234
    assert snapshot["playback"] is None
    assert len(snapshot["recent"]) == 1
    assert snapshot["playlists"]["inventedpl"]["name"] == "Invented Playlist"
    assert playlist_id({"type": "album", "uri": "spotify:album:abc"}) is None


def test_remote_snapshot_rejects_wrong_spotify_user():
    class Client:
        def call_many(self, calls):
            return {
                "profile": {
                    "result": {"data": {"id": "someone-else"}},
                    "error": None,
                },
                "recent": {
                    "result": {"data": {"items": []}},
                    "error": None,
                },
                "playback": {"result": {"data": {}}, "error": None},
            }

    with pytest.raises(RuntimeError, match="identity"):
        build_snapshot(Client(), expected_user="invented-listener")
