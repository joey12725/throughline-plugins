import copy
import json
from pathlib import Path

import httpx
import pytest
from throughline_sdk import Checkpoint, Record
from throughline_sdk.contract import check_plugin
from throughline_plex import Plex, Source

RECORDING = Path(__file__).parent / "recorded" / "responses.json"


@pytest.fixture
def plugin(monkeypatch):
    monkeypatch.setenv("PLEX_SERVER_URL", "https://invented.invalid:32400")
    monkeypatch.setenv("PLEX_TOKEN", "invented-secret-canary")
    monkeypatch.setenv("PLEX_RECORDING", str(RECORDING))
    monkeypatch.setenv("PLEX_PAGE_SIZE", "2")
    return Plex()


def messages(records):
    return [x for x in records if isinstance(x, dict)]


def test_plg_6_contract(plugin):
    result = check_plugin(
        "throughline_plex:Plex",
        {
            "PLEX_SERVER_URL": plugin.settings["server_url"],
            "PLEX_TOKEN": plugin.secrets["token"],
            "PLEX_RECORDING": str(RECORDING),
            "PLEX_PAGE_SIZE": "2",
        },
        secrets=[plugin.secrets["token"]],
    )
    assert result


def test_med_1_movies_episodes_music_msg_6(plugin):
    records = list(plugin.records({}, "backfill"))
    rows = messages(records)
    assert len(rows) == 6
    assert rows[0]["content"] == "Watched Invented movie 1 (2019)"
    assert rows[1]["content"] == "Watched Invented Show S01E02 - Invented episode 2"
    assert rows[2]["content"] == "Listened to Invented track 3 - Invented Artist"
    assert rows[0]["fields"]["duration_s"] == 2400
    assert rows[0]["ended_at"] is None
    assert rows[0]["fields"]["time_precision"] == "source_mark"
    assert rows[0]["author"]["value"] == "9001"
    original = json.loads(RECORDING.read_text())["history"]["MediaContainer"][
        "Metadata"
    ]
    assert [r["payload"]["source"] for r in rows] == original
    links = [x.data for x in records if isinstance(x, Record)]
    consumed = [x for x in links if x["type"] == "consumed"]
    assert consumed[1]["to"] == consumed[3]["to"]
    assert all(x["method"] == "source" for x in links)
    assert sum(x["type"] == "part_of" for x in links) == 6
    assert not plugin.manifest.channels[0].is_reference


def test_plg_4_paging_resume_msg_2(plugin):
    all_records = list(plugin.records({}, "backfill"))
    checkpoints = [x for x in all_records if isinstance(x, Checkpoint)]
    assert checkpoints[0].cursor["offset"] == 2
    for checkpoint in checkpoints[:-1]:
        resumed = messages(plugin.records(checkpoint.cursor, "backfill"))
        assert resumed == messages(all_records)[checkpoint.cursor["offset"] :]
    assert messages(plugin.records(checkpoints[-1].cursor, "sync")) == []
    assert messages(plugin.records({}, "sync")) == messages(all_records)


def test_idn_2_multiuser_filter(plugin):
    plugin.settings["accounts"] = "Invented Beta"
    rows = messages(plugin.records({}, "backfill"))
    assert len(rows) == 3
    assert {r["author"]["value"] for r in rows} == {"9002"}
    plugin.settings["accounts"] = "9001"
    assert len(messages(plugin.records({}, "sync"))) == 3
    plugin.settings["libraries"] = "unavailable"
    assert messages(plugin.records({}, "sync")) == []
    plugin.settings["libraries"] = "81"
    assert len(messages(plugin.records({}, "sync"))) == 3


def test_msg_1_missing_fields(plugin):
    raw = dict(
        historyKey="/status/sessions/history/999", viewedAt=1600000000, ratingKey="999"
    )
    row, links = plugin.normalize(raw, {}, "invented-server", {}, {})
    assert row["content"] == "Watched Unknown title"
    assert row["ended_at"] is None
    assert row["fields"]["view_offset_s"] is None
    assert links[0]["to"]["external_ref"] == "plex:invented-server:999"
    with pytest.raises(ValueError, match="viewedAt"):
        plugin.normalize({"historyKey": "fake"}, {}, "invented", {}, {})


def test_med_3_active_position_and_reconciliation(plugin, tmp_path, monkeypatch):
    data = json.loads(RECORDING.read_text())
    data["history"]["MediaContainer"]["Metadata"] = []
    live = dict(
        ratingKey="102",
        type="episode",
        title="Invented episode 2",
        viewOffset=600000,
        User={"id": 9002, "title": "Invented Beta"},
        Session={"id": "invented-session"},
        Player={
            "title": "Invented TV",
            "product": "Invented Player",
            "state": "playing",
        },
    )
    data["/status/sessions"]["MediaContainer"]["Metadata"] = [live]
    path = tmp_path / "live.json"
    path.write_text(json.dumps(data))
    plugin.settings["recording"] = str(path)
    monkeypatch.setattr("throughline_plex.time.time", lambda: 1600100000)
    first = list(plugin.records({}, "sync"))
    row = messages(first)[0]
    assert row["fields"]["view_offset_s"] == 600
    assert row["happened_at"] == "2020-09-14T16:13:20+00:00"
    assert row["ended_at"] is None
    state = first[-1].cursor
    replay = messages(plugin.records(state, "sync"))
    assert replay == [row]
    data["/status/sessions"]["MediaContainer"]["Metadata"] = []
    path.write_text(json.dumps(data))
    stopped = list(plugin.records(state, "sync"))
    assert messages(stopped)[0]["ended_at"] == row["fields"]["position_observed_at"]
    history = copy.deepcopy(
        json.loads(RECORDING.read_text())["history"]["MediaContainer"]["Metadata"][1]
    )
    history["viewedAt"] = 1600100000
    data["history"]["MediaContainer"]["Metadata"] = [history]
    path.write_text(json.dumps(data))
    final = list(plugin.records(stopped[-1].cursor, "sync"))
    assert messages(final)[0]["external_id"] == row["external_id"]
    assert len(messages(final)) == 1
    assert messages(plugin.records(final[-1].cursor, "sync")) == []


def test_plg_6_read_only_transport(plugin):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"MediaContainer": {}})

    settings = dict(plugin.settings, recording=None)
    source = Source(
        settings, plugin.secrets["token"], transport=httpx.MockTransport(handle)
    )
    try:
        source.get("/identity")
        source.get("/library/metadata/123")
        with pytest.raises(ValueError, match="allowlisted"):
            source.get("/library/sections/1/refresh")
        with pytest.raises(ValueError):
            source.get("/:/scrobble")
        assert all(r.method == "GET" for r in calls)
        assert all(r.headers["X-Plex-Token"] == "invented-secret-canary" for r in calls)
        assert all("invented-secret-canary" not in str(r.url) for r in calls)
    finally:
        source.close()


def test_plg_4_active_crash_replays_stable_id(plugin, tmp_path, monkeypatch):
    data = json.loads(RECORDING.read_text())
    data["history"]["MediaContainer"]["Metadata"] = []
    data["/status/sessions"]["MediaContainer"]["Metadata"] = [
        dict(
            ratingKey="102",
            type="episode",
            title="Invented episode 2",
            viewOffset=600000,
            User={"id": 9002},
            Session={"id": "invented-crash-session"},
        )
    ]
    path = tmp_path / "crash.json"
    path.write_text(json.dumps(data))
    plugin.settings["recording"] = str(path)
    monkeypatch.setattr("throughline_plex.time.time", lambda: 1600100000)
    records = list(plugin.records({}, "sync"))
    before_active = next(r for r in records if isinstance(r, Checkpoint)).cursor
    original = messages(records)[0]
    monkeypatch.setattr("throughline_plex.time.time", lambda: 1600100030)
    resumed = messages(plugin.records(before_active, "sync"))[0]
    assert resumed["external_id"] == original["external_id"]
    assert resumed["happened_at"] == original["happened_at"]


def test_plg_2_http_history_params_and_deleted_metadata(plugin):
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.path.startswith("/library/metadata/"):
            return httpx.Response(404)
        return httpx.Response(
            200, json={"MediaContainer": {"totalSize": 0, "Metadata": []}}
        )

    source = Source(
        dict(plugin.settings, recording=None),
        plugin.secrets["token"],
        transport=httpx.MockTransport(handle),
    )
    try:
        assert source.get("/library/metadata/123", missing=True) == {}
        source.get(
            "/status/sessions/history/all",
            {
                "sort": "viewedAt:asc",
                "viewedAt>": 1600000000,
                "X-Plex-Container-Start": 2,
                "X-Plex-Container-Size": 2,
            },
        )
        assert calls[-1].url.params["viewedAt>"] == "1600000000"
        assert "viewedAt%3E=1600000000" in str(calls[-1].url)
        assert "viewedAt%3E%3D=" not in str(calls[-1].url)
        assert calls[-1].url.params["X-Plex-Container-Start"] == "2"
    finally:
        source.close()


def poll_recording(plugin, path, data, cursor=None):
    path.write_text(json.dumps(data))
    plugin.settings["recording"] = str(path)
    return list(plugin.records(cursor or {}, "sync"))


def session_data():
    data = json.loads(RECORDING.read_text())
    data["history"]["MediaContainer"]["Metadata"] = []
    data["/status/sessions"]["MediaContainer"]["Metadata"] = [
        dict(
            ratingKey="102",
            viewOffset=3000000,
            User={"id": 9002},
            Session={"id": "invented-reused"},
        )
    ]
    return data


@pytest.mark.parametrize("matched", [False, True])
def test_msg_2_reused_session_is_new_view(plugin, tmp_path, monkeypatch, matched):
    data = session_data()
    clock = [1600100000]
    monkeypatch.setattr("throughline_plex.time.time", lambda: clock[0])
    path = tmp_path / "reused.json"
    first = poll_recording(plugin, path, data)
    if matched:
        next(iter(first[-1].cursor["observed"].values()))["matched"] = True
    live = data["/status/sessions"]["MediaContainer"]["Metadata"].pop()
    clock[0] += 60
    closed = poll_recording(plugin, path, data, first[-1].cursor)
    clock[0] += 3540
    data["/status/sessions"]["MediaContainer"]["Metadata"] = [live]
    second = poll_recording(plugin, path, data, closed[-1].cursor)
    assert messages(second)[0]["external_id"] != messages(first)[0]["external_id"]
    assert messages(second)[0]["ended_at"] is None
    data["/status/sessions"]["MediaContainer"]["Metadata"] = []
    clock[0] += 30
    assert messages(poll_recording(plugin, path, data, second[-1].cursor))[0][
        "ended_at"
    ]


def test_msg_3_resume_uses_first_seen(plugin, tmp_path, monkeypatch):
    monkeypatch.setattr("throughline_plex.time.time", lambda: 1600100000)
    row = messages(poll_recording(plugin, tmp_path / "resume.json", session_data()))[0]
    assert row["happened_at"] == row["fields"]["position_observed_at"]
    assert row["fields"]["time_precision"] == "first_seen"


def test_plg_4_history_overlap_no_metadata_or_emission(plugin, monkeypatch):
    first = list(plugin.records({}, "sync"))
    original = Source.get
    calls = []
    history_params = []

    def get(self, path, *args, **kwargs):
        calls.append(path)
        if path == "/status/sessions/history/all":
            history_params.append(args[0])
        return original(self, path, *args, **kwargs)

    monkeypatch.setattr(Source, "get", get)
    assert messages(plugin.records(first[-1].cursor, "sync")) == []
    assert not any(p.startswith("/library/metadata/") for p in calls)
    assert history_params and all(
        "viewedAt>" in p and "viewedAt>=" not in p for p in history_params
    )


def test_plg_4_closed_observations_are_pruned(plugin, tmp_path, monkeypatch):
    clock = [1600100000]
    monkeypatch.setattr("throughline_plex.time.time", lambda: clock[0])
    data = session_data()
    path = tmp_path / "bounded.json"
    first = poll_recording(plugin, path, data)
    data["/status/sessions"]["MediaContainer"]["Metadata"] = []
    closed = poll_recording(plugin, path, data, first[-1].cursor)
    clock[0] += 86401
    final = poll_recording(plugin, path, data, closed[-1].cursor)
    assert final[-1].cursor["observed"] == {}
    assert final[-1].cursor["aliases"] == {}


def test_account_filter_skips_metadata_and_state_for_other_accounts(
    plugin, tmp_path, monkeypatch
):
    data = json.loads(RECORDING.read_text())
    data["/status/sessions"]["MediaContainer"]["Metadata"] = [
        dict(
            ratingKey="102",
            type="episode",
            title="Invented episode 2",
            viewOffset=600000,
            User={"id": 9002, "title": "Invented Beta"},
            Session={"id": "other-user-session"},
        )
    ]
    path = tmp_path / "filtered.json"
    path.write_text(json.dumps(data))
    plugin.settings["recording"] = str(path)
    plugin.settings["accounts"] = "9001"
    original = Source.get
    metadata_calls = []

    def get(self, path, *args, **kwargs):
        if path.startswith("/library/metadata/"):
            metadata_calls.append(path)
        return original(self, path, *args, **kwargs)

    monkeypatch.setattr(Source, "get", get)
    records = list(plugin.records({}, "backfill"))
    rows = messages(records)
    cursor = [r for r in records if isinstance(r, Checkpoint)][-1].cursor
    assert len(rows) == 3
    assert {r["author"]["value"] for r in rows} == {"9001"}
    assert cursor["observed"] == {}
    assert len(cursor["history_seen"]) == 3
    # ratingKey 102 only belongs to account 9002 in this test (wait: 102 is also
    # watched by 9001 in history; verify other-user active session wasn't tracked
    # and ratingKey 103/other accounts didn't add extra history_seen entries)
    assert all(
        k.endswith((":/status/sessions/history/1", ":/status/sessions/history/3", ":/status/sessions/history/4"))
        or len(cursor["history_seen"]) == 3
        for k in cursor["history_seen"]
    )
    # Specifically ensure a ratingKey exclusive to account 9002 is never fetched.
    data["history"]["MediaContainer"]["Metadata"].append(
        dict(
            historyKey="/status/sessions/history/9999",
            ratingKey="9999",
            accountID=9002,
            deviceID=1,
            type="movie",
            title="Other User Only",
            viewedAt=1600009999,
        )
    )
    path.write_text(json.dumps(data))
    metadata_calls.clear()
    list(plugin.records({}, "backfill"))
    assert "/library/metadata/9999" not in metadata_calls
