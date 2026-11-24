"""The plugin: deterministic mapping, export and live sources, contract kit."""

import copy
import json
import os
from datetime import datetime, timedelta, timezone

import pytest
from consumption_fakes import SRC, TOKENS, World, live_env, write_backup, write_takeout

from throughline_consumption import Consumption, fetch, mapping
from throughline_sdk import Checkpoint, Record
from throughline_sdk.contract import check_plugin

NOW = datetime(2020, 2, 1, 12, 0, tzinfo=timezone.utc)
TYPES = {
    "text": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "timestamp": str,
    "json": (dict, list, str, int, float, bool),
}


@pytest.fixture
def plugin_env(monkeypatch):
    for name, *_ in fetch.ENV.values():
        monkeypatch.delenv(name, raising=False)

    def apply(values):
        for key, value in values.items():
            monkeypatch.setenv(key, value)
        return Consumption()

    return apply


def export_file(path, fake, extra=None, state=None, now=NOW):
    config = fetch.Config({**live_env(fake), **(extra or {})})
    fetcher = fetch.Fetcher(config, state, sleep=lambda s: None, now=now)
    fetch.export(fetcher, str(path))
    return fetcher


def split(items):
    messages = [i for i in items if isinstance(i, dict)]
    links = [i.data for i in items if isinstance(i, Record) and i.kind == "link"]
    checkpoints = [i.cursor for i in items if isinstance(i, Checkpoint)]
    return messages, links, checkpoints


def full_records(fake, tmp_path):
    write_backup(tmp_path / "backup")
    write_takeout(tmp_path / "takeout")
    config = fetch.Config(
        {
            **live_env(fake),
            "CONSUMPTION_SPOTIFY_BACKUP_DIR": str(tmp_path / "backup"),
            "CONSUMPTION_YOUTUBE_TAKEOUT": str(tmp_path / "takeout"),
        }
    )
    fetcher = fetch.Fetcher(config, sleep=lambda s: None, now=NOW)
    return [json.loads(json.dumps(r)) for r in fetcher.run()]


def test_con_20_mapping_is_deterministic_typed_and_complete(fake, tmp_path):
    records = full_records(fake, tmp_path)
    first = [mapping.map_record(copy.deepcopy(r)) for r in records]
    second = [mapping.map_record(copy.deepcopy(r)) for r in records]
    assert all(m is not None for m in first)
    assert [(m.message, m.links) for m in first] == [
        (m.message, m.links) for m in second
    ]
    ids = [m.message["external_id"] for m in first]
    assert len(ids) == len(set(ids))
    for mapped in first:
        message = mapped.message
        assert set(message["fields"]) == set(mapping.FIELDS)
        assert message["fields"]["mapping_revision"] == mapping.MAPPING_REVISION
        assert message["content"].strip() and message["channel"] == "media.consumption"
        assert datetime.fromisoformat(message["happened_at"]).utcoffset() is not None
        assert (
            "fetched_at" not in message["payload"]
            and "observed_at" not in message["payload"]
        )
        for name, value in message["fields"].items():
            if value is not None:
                assert isinstance(value, TYPES[mapping.FIELDS[name][0]]), (name, value)
                if mapping.FIELDS[name][0] in ("integer", "number"):
                    assert not isinstance(value, bool)
        assert message["fields"]["media_type"] in (
            "track",
            "episode",
            "movie",
            "video",
            "audiobook",
            "podcast",
        )
    services = {m.message["fields"]["service"] for m in first}
    assert services == {"spotify", "plex", "audiobookshelf", "youtube"}
    serialized = json.dumps([(m.message, m.links) for m in first])
    for canary in TOKENS.values():
        assert canary not in serialized


def test_con_21_service_mappings(fake, tmp_path):
    records = full_records(fake, tmp_path)
    by_id = {}
    for record in records:
        mapped = mapping.map_record(record)
        by_id[mapped.message["external_id"]] = mapped
    film = by_id["plex:fake-server:/status/sessions/history/1"]
    assert film.message["content"] == "Watched Invented Film (2001)"
    assert film.message["author"] == {
        "system": "plex",
        "kind": "account_id",
        "value": "1",
        "display_name": "Fake Owner",
    }
    assert (
        film.message["fields"]["duration_s"] == 5400
        and film.message["fields"]["device"] == "Fake TV"
    )
    assert film.message["happened_at"] == "2020-01-03T20:00:00+00:00"
    pilot = by_id["plex:fake-server:/status/sessions/history/2"]
    assert pilot.message["content"] == "Watched Invented Series S01E01 - Pilot"
    assert {link["type"] for link in pilot.links} == {"consumed", "part_of"}
    session = by_id["audiobookshelf:session:sess-1"]
    fields = session.message["fields"]
    assert (
        session.message["content"]
        == "Listened to Invented Saga by Fake Author for 30 min"
    )
    assert (
        fields["media_type"],
        fields["listened_s"],
        fields["start_position_s"],
        fields["position_s"],
    ) == ("audiobook", 1800, 0, 1800)
    assert (
        fields["series"] == "Invented Cycle #1"
        and fields["play_method"] == "direct play"
    )
    assert fields["device"] == "Fake Phone" and fields["client"] == "Fake Client 1.0"
    assert session.message["happened_at"] == "2020-01-07T19:00:00+00:00"
    assert session.message["ended_at"] == "2020-01-07T19:30:00+00:00"
    assert session.message["thread"]["external_id"] == "audiobookshelf:item:li-book"
    podcast = by_id["audiobookshelf:progress:prog-pod"]
    assert (
        podcast.message["content"]
        == "Finished Invented Episode One from Invented Podcast"
    )
    assert (
        podcast.message["fields"]["event_type"] == "finished"
        and podcast.message["fields"]["is_finished"] is True
    )
    assert (
        podcast.message["thread"]["external_id"]
        == "audiobookshelf:item:li-pod:episode:ep-1"
    )
    book = by_id["audiobookshelf:progress:prog-book"]
    assert (
        book.message["content"] == "15% through Invented Saga"
        and book.message["ended_at"] is None
    )
    play = next(
        m
        for k, m in by_id.items()
        if k.startswith("spotify:play:") and k.endswith("FAKETRACK01")
    )
    assert play.message["content"] == "Listened to Invented Song 1 by Invented Band"
    assert play.message["fields"]["context_uri"] == "spotify:playlist:PLFAKE1"
    assert {(link["type"], link["to"].get("external_ref")) for link in play.links} == {
        ("consumed", "spotify:track:FAKETRACK01"),
        ("part_of", "spotify:album:FAKEALBUM"),
        ("created_by", "spotify:artist:FAKEARTIST"),
    }
    entries = [
        m for k, m in by_id.items() if k.startswith("spotify:playlist_item:PLFAKE1:")
    ]
    assert len(entries) == 3
    episode = next(m for m in entries if m.message["fields"]["media_type"] == "podcast")
    assert (
        episode.pinned and episode.message["fields"]["time_precision"] == "first_seen"
    )
    assert episode.message["author"] == {
        "system": "spotify",
        "kind": "user_id",
        "value": "fake-friend",
    }
    assert (
        episode.message["content"]
        == "Added Invented Episode from Invented Show to playlist Invented Mix"
    )
    timeless = by_id["spotify:liked:spotify:track:FAKENOTIME"]
    assert (
        timeless.pinned
        and timeless.message["happened_at"] == "2019-12-15T00:00:00+00:00"
    )
    assert timeless.message["fields"]["source"] == "backup"
    videos = [m for k, m in by_id.items() if k.startswith("youtube:takeout:watch:")]
    assert len(videos) == 5
    orchard = videos[0]
    assert (
        orchard.message["content"]
        == "Watched Invented Orchard Tour by Invented Orchard"
    )
    assert {link["to"]["external_ref"] for link in orchard.links} == {
        "youtube:FAKE_VIDEO_A",
        "youtube:UC_FAKE_ORCHARD",
    }
    assert (
        sum(1 for m in videos if m.message["fields"]["native_id"] == "FAKE_VIDEO_A")
        == 2
    )


def write_exports(fake, tmp_path, world):
    folder = tmp_path / "exports"
    folder.mkdir()
    write_backup(tmp_path / "backup")
    write_takeout(tmp_path / "takeout")
    export_file(
        folder / "20200201T120000-media.jsonl",
        fake,
        {"CONSUMPTION_SERVICES": "plex,audiobookshelf"},
    )
    export_file(
        folder / "20200201T120100-spotify.jsonl",
        fake,
        {"CONSUMPTION_SERVICES": "spotify"},
    )
    export_file(
        folder / "20200201T120200-files.jsonl.gz",
        fake,
        {
            "CONSUMPTION_SERVICES": "spotify_backup,youtube",
            "CONSUMPTION_SPOTIFY_BACKUP_DIR": str(tmp_path / "backup"),
            "CONSUMPTION_YOUTUBE_TAKEOUT": str(tmp_path / "takeout"),
        },
    )
    return folder


def test_con_22_export_source_resumes_and_replays_nothing(
    fake, world, tmp_path, plugin_env
):
    folder = write_exports(fake, tmp_path, world)
    plugin = plugin_env(
        {"CONSUMPTION_SOURCE": f"export:{folder}", "CONSUMPTION_CHECKPOINT_EVERY": "10"}
    )
    messages, links, checkpoints = split(list(plugin.records({}, "backfill")))
    assert len(messages) == 79 and links
    assert len(checkpoints) > 5
    final = checkpoints[-1]
    assert set(final["exports"].values()) == {"done"}
    assert split(list(plugin.records(final, "sync")))[0] == []
    # Resuming from a mid-file checkpoint emits only what came after it.
    middle = checkpoints[3]
    resumed = split(list(plugin.records(copy.deepcopy(middle), "backfill")))[0]
    assert resumed == messages[-len(resumed) :] and 0 < len(resumed) < len(messages)
    # A reset cursor replays identical messages.
    assert split(list(plugin.records({}, "backfill")))[0] == messages


def test_con_23_older_export_of_a_changed_session_is_ignored(
    fake, world, tmp_path, plugin_env
):
    folder = tmp_path / "exports"
    folder.mkdir()
    extra = {"CONSUMPTION_SERVICES": "audiobookshelf"}
    # Sorted by name, the newer export is read first here.
    world.sessions[0] = dict(
        world.sessions[0],
        updatedAt=world.sessions[0]["updatedAt"] + 600000,
        timeListening=2400,
    )
    export_file(folder / "a-newer.jsonl", fake, extra)
    world.sessions[0] = dict(
        world.sessions[0],
        updatedAt=world.sessions[0]["updatedAt"] - 600000,
        timeListening=1800,
    )
    export_file(folder / "b-older.jsonl", fake, extra)
    plugin = plugin_env({"CONSUMPTION_SOURCE": f"export:{folder}"})
    messages, _, checkpoints = split(list(plugin.records({}, "backfill")))
    sessions = [
        m for m in messages if m["external_id"] == "audiobookshelf:session:sess-1"
    ]
    assert len(sessions) == 1 and sessions[0]["fields"]["listened_s"] == 2400
    assert (
        checkpoints[-1]["latest"]["audiobookshelf:session:sess-1"]
        == world.sessions[0]["updatedAt"] + 600000
    )


def test_con_24_first_seen_time_is_pinned_to_the_earliest_export(
    fake, tmp_path, plugin_env
):
    folder = tmp_path / "exports"
    folder.mkdir()
    extra = {"CONSUMPTION_SERVICES": "spotify"}
    export_file(folder / "a.jsonl", fake, extra, now=NOW + timedelta(days=7))
    export_file(folder / "b.jsonl", fake, extra, now=NOW)
    plugin = plugin_env({"CONSUMPTION_SOURCE": f"export:{folder}"})
    messages, _, checkpoints = split(list(plugin.records({}, "backfill")))
    pinned = [m for m in messages if m["fields"]["time_precision"] == "first_seen"]
    # An export fetched earlier moves the pin back once; a later one repeats it.
    assert [m["happened_at"] for m in pinned] == [
        (NOW + timedelta(days=7)).isoformat(),
        NOW.isoformat(),
    ]
    key = pinned[0]["external_id"]
    assert checkpoints[-1]["first_seen"][key] == NOW.isoformat()
    later = tmp_path / "later"
    later.mkdir()
    export_file(later / "c.jsonl", fake, extra, now=NOW + timedelta(days=30))
    plugin = plugin_env({"CONSUMPTION_SOURCE": f"export:{later}"})
    messages, _, _ = split(list(plugin.records(checkpoints[-1], "sync")))
    assert (
        next(m for m in messages if m["external_id"] == key)["happened_at"]
        == NOW.isoformat()
    )


def test_con_25_refuses_incomplete_or_foreign_exports(fake, tmp_path, plugin_env):
    path = tmp_path / "cut.jsonl"
    export_file(path, fake, {"CONSUMPTION_SERVICES": "plex"})
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    plugin = plugin_env({"CONSUMPTION_SOURCE": f"export:{path}"})
    with pytest.raises(ValueError, match="incomplete"):
        list(plugin.records({}, "backfill"))
    path.write_text('{"kind": "something.else"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="not a consumption export"):
        list(plugin.records({}, "backfill"))
    plugin = plugin_env({"CONSUMPTION_SOURCE": "elsewhere"})
    with pytest.raises(ValueError, match="live or export"):
        list(plugin.records({}, "backfill"))


def test_con_26_live_source_runs_the_fetcher_in_process(fake, tmp_path, plugin_env):
    plugin = plugin_env(
        {
            **live_env(fake),
            "CONSUMPTION_SOURCE": "live",
            "CONSUMPTION_SERVICES": "plex,audiobookshelf",
        }
    )
    messages, _, checkpoints = split(list(plugin.records({}, "sync")))
    assert len(messages) == 10
    cursor = checkpoints[-1]
    assert cursor["fetch_state"]["plex"]["server"] == "fake-server"
    for canary in TOKENS.values():
        assert canary not in json.dumps(cursor)
    again, _, _ = split(list(plugin.records(cursor, "sync")))
    previous = {m["external_id"]: m for m in messages}
    assert again and all(previous[m["external_id"]] == m for m in again)
    fake.faults["/abs/api/me"] = [(500, {})] * 5
    with pytest.raises(RuntimeError, match="audiobookshelf me failed"):
        list(plugin.records(cursor, "sync"))


def test_con_27_manifest_declares_every_env_name():
    manifest = Consumption.manifest
    declared = {d.env for d in manifest.settings.values()} | {
        d.env for d in manifest.secrets.values()
    }
    assert declared == {name for name, *_ in fetch.ENV.values()}
    assert {d.env for d in manifest.secrets.values()} == {
        "PLEX_TOKEN",
        "AUDIOBOOKSHELF_TOKEN",
        "KAVITA_API_KEY",
        "SPOTIFY_ACCESS_TOKEN",
        "SPOTIFY_CLIENT_ID",
        "SPOTIFY_CLIENT_SECRET",
        "SPOTIFY_REFRESH_TOKEN",
        "COMPOSIO_API_KEY",
        "SPOTIFY_COMPOSIO_MCP_TOKEN",
    }
    assert [c.name for c in manifest.channels] == [
        "media.consumption",
        "audiobooks.text",
    ]
    assert not manifest.channels[0].is_reference
    assert manifest.channels[1].is_reference is True
    assert {f["name"] for f in manifest.channels[0].fields} == set(mapping.FIELDS)
    assert {f["name"] for f in manifest.channels[1].fields} == set(
        mapping.BOOK_TEXT_FIELDS
    )


def kit_env(extra):
    path = os.pathsep.join(filter(None, [str(SRC), os.environ.get("PYTHONPATH")]))
    base = {name: "" for name, *_ in fetch.ENV.values()}
    return {**base, "PYTHONPATH": path, **extra}


def _shrink_world_with_kavita(world):
    """Trim synthetic world to fit core /v1/sql agent-view char budget while covering every service and channel."""
    from consumption_fakes import build_test_epub

    world.plays = world.plays[:1]
    world.saved = world.saved[:1]
    world.playlist_items["PLFAKE1"] = world.playlist_items["PLFAKE1"][:1]
    world.plex_history = world.plex_history[:2]
    world.sessions = world.sessions[:1]
    world.progress[0]["progress"] = 0.65
    world.add_kavita_book(
        chapter_id=701,
        title="Invented Saga",
        authors=["Fake Author"],
        file_path="/books/Invented Saga (Fake Author).epub",
        data=build_test_epub(
            [
                ("Chapter 1", "<p>Opening chapter of the invented saga.</p>"),
                ("Chapter 2", "<p>Second chapter of the invented saga.</p>"),
            ]
        ),
        series_id=70,
    )


def test_con_28_contract_export_source(fake, world, tmp_path):
    _shrink_world_with_kavita(world)
    folder = tmp_path / "exports"
    folder.mkdir()
    export_file(
        folder / "20200201T120000-media.jsonl",
        fake,
        {
            "CONSUMPTION_SERVICES": "plex,audiobookshelf",
            "KAVITA_URL": fake.base + "/kavita",
            "KAVITA_API_KEY": TOKENS["KAVITA_API_KEY"],
        },
    )
    export_file(
        folder / "20200201T120100-spotify.jsonl",
        fake,
        {"CONSUMPTION_SERVICES": "spotify"},
    )
    result = check_plugin(
        "throughline_consumption:Consumption",
        kit_env(
            {
                "CONSUMPTION_SOURCE": f"export:{folder}",
                "CONSUMPTION_CHECKPOINT_EVERY": "3",
                "PLEX_TOKEN": TOKENS["PLEX_TOKEN"],
                "AUDIOBOOKSHELF_TOKEN": TOKENS["AUDIOBOOKSHELF_TOKEN"],
                "KAVITA_API_KEY": TOKENS["KAVITA_API_KEY"],
            }
        ),
        secrets=list(TOKENS.values()),
    )
    assert result["messages"] == 10


@pytest.fixture
def steady_world():
    # Live contract runs fetch again and again: no record may depend on the clock.
    return World(null_added=False)


def test_con_29_contract_live_source(steady_world, tmp_path):
    from http.server import ThreadingHTTPServer
    import threading

    from consumption_fakes import Fake, make_handler

    _shrink_world_with_kavita(steady_world)
    state = Fake(steady_world)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state.base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        result = check_plugin(
            "throughline_consumption:Consumption",
            kit_env(
                {
                    **live_env(state),
                    "KAVITA_URL": state.base + "/kavita",
                    "KAVITA_API_KEY": TOKENS["KAVITA_API_KEY"],
                    "CONSUMPTION_SOURCE": "live",
                    "CONSUMPTION_CHECKPOINT_EVERY": "3",
                }
            ),
            secrets=list(TOKENS.values()),
        )
    finally:
        server.shutdown()
        server.server_close()
    assert result["messages"] == 10


def test_con_30_pinned_view_stays_in_its_first_day_thread(plugin_env):
    plugin = plugin_env({"CONSUMPTION_SOURCE": "export:unused"})
    parsed = {
        "video_id": "FAKE_UNDATED",
        "title": "Invented Clip",
        "at": None,
        "source": "youtube_plugin",
    }
    row = {
        "external_id": "ytdlp:watch:FAKE_UNDATED",
        "channel": "youtube.watch",
        "fields": {"video_id": "FAKE_UNDATED"},
    }

    def record(fetched):
        return {
            "kind": "youtube.watch",
            "id": "ytdlp:watch:FAKE_UNDATED",
            "service": "youtube",
            "raw": row,
            "context": {"parsed": dict(parsed)},
            "fetched_at": fetched.isoformat(),
        }

    state = {}
    (first, *_) = list(plugin.emit(state, record(NOW)))
    (again, *_) = list(plugin.emit(state, record(NOW + timedelta(days=3))))
    assert first["happened_at"] == again["happened_at"] == NOW.isoformat()
    assert (
        first["thread"]
        == again["thread"]
        == {"external_id": "youtube:day:2020-02-01", "title": "YouTube 2020-02-01"}
    )
    assert first == again


def write_watcher(path, records, tz="America/Winnipeg"):
    lines = [
        {
            "kind": "export.header",
            "format": fetch.WATCHER_FORMAT,
            "version": 1,
            "tz": tz,
        },
        *records,
        {"kind": "export.footer", "complete": True, "counts": {}},
    ]
    path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")


def test_con_31_youtube_watcher_sessions_and_days(tmp_path, plugin_env):
    """Invented watcher export: a video watched in two parts, plus history days."""
    base = {
        "video_id": "FAKEVIDEO01",
        "title": "Invented Lecture",
        "channel": "Invented Hall",
        "channel_id": "UC_FAKE_HALL",
        "length_s": 1000,
    }

    def session(end, before, after, n, start):
        return {
            "kind": "session",
            "id": f"FAKEVIDEO01:{end}",
            "fetched_at": end,
            "raw": base
            | {
                "session_kind": "resume" if before else "new",
                "progress_before": before,
                "progress_after": after,
                "watched_fraction": (after - (before or 0)) / 100,
                "watched_s": (after - (before or 0)) * 10,
                "window_start": start,
                "window_end": end,
                "start_est": start,
                "end_est": end,
                "session_number": n,
                "day": "2020-02-01",
            },
        }

    folder = tmp_path / "watcher"
    folder.mkdir()
    day = {
        "kind": "history_item",
        "id": "FAKEVIDEO01:2020-02-01",
        "fetched_at": "2020-02-01T18:00:00Z",
        "raw": base
        | {"day": "2020-02-01", "progress": 30, "first_seen": "2020-02-01T18:00:00Z"},
    }
    write_watcher(
        folder / "a.jsonl",
        [day, session("2020-02-01T18:00:00Z", None, 30, 1, "2020-02-01T17:30:00Z")],
    )
    write_watcher(
        folder / "b.jsonl",
        [day, session("2020-02-01T19:00:00Z", 30, 80, 2, "2020-02-01T18:30:00Z")],
    )
    plugin = plugin_env(
        {"CONSUMPTION_SOURCE": "live", "CONSUMPTION_YOUTUBE_WATCHER": str(folder)}
    )
    messages, links, checkpoints = split(list(plugin.records({}, "backfill")))
    by_id = {m["external_id"]: m for m in messages}
    assert set(by_id) == {
        "youtube:history_day:FAKEVIDEO01:2020-02-01",
        "youtube:session:FAKEVIDEO01:2020-02-01T18:00:00Z",
        "youtube:session:FAKEVIDEO01:2020-02-01T19:00:00Z",
    }
    second = by_id["youtube:session:FAKEVIDEO01:2020-02-01T19:00:00Z"]
    f = second["fields"]
    assert f["event_type"] == "session" and f["time_precision"] == "estimated"
    assert (
        f["start_position_s"] == 300
        and f["position_s"] == 800
        and f["listened_s"] == 500
    )
    assert f["progress"] == 0.8 and f["duration_s"] == 1000
    assert (
        second["happened_at"] == "2020-02-01T18:30:00Z"
        and second["ended_at"] == "2020-02-01T19:00:00Z"
    )
    dayed = by_id["youtube:history_day:FAKEVIDEO01:2020-02-01"]
    assert dayed["happened_at"] == "2020-02-01T00:00:00-06:00"
    assert (
        dayed["fields"]["time_precision"] == "day"
        and dayed["fields"]["progress"] == 0.3
    )
    assert {(link["type"], link["to"].get("external_ref")) for link in links} >= {
        ("consumed", "youtube:FAKEVIDEO01"),
        ("published_by", "youtube:UC_FAKE_HALL"),
    }
    # Unchanged files: nothing again.
    assert split(list(plugin.records(checkpoints[-1], "sync")))[0] == []


def test_spotify_cap_note_becomes_a_coverage_gap():
    from throughline_consumption import capped

    window = {"from": "2020-01-10T12:00:00+00:00", "to": "2020-01-13T12:00:00+00:00"}
    (report,) = capped({"spotify_recent_gap": window})
    assert report.kind == "coverage" and report.data == {
        "channel": "media.consumption",
        "scope_key": "spotify",
        "status": "partial",
        "reasons": ["source_cap"],
        "gaps": [dict(window, reason="source_cap")],
        "detail": {"source": "recently-played"},
    }
    assert list(capped({})) == []


def test_con_32_audiobook_text_channel_mapping_and_idempotency(
    fake, world, plugin_env
):
    from consumption_fakes import build_test_epub

    world.progress[0]["progress"] = 0.75
    world.add_kavita_book(
        chapter_id=801,
        title="Invented Saga",
        authors=["Fake Author"],
        file_path="/books/Invented Saga (Fake Author).epub",
        data=build_test_epub(
            [
                ("Prologue", "<p>First chapter words.</p>"),
                ("Epilogue", "<p>Last chapter words.</p>"),
            ]
        ),
        series_id=80,
    )
    plugin = plugin_env(
        {
            **live_env(fake),
            "CONSUMPTION_SOURCE": "live",
            "CONSUMPTION_SERVICES": "audiobookshelf",
            "KAVITA_URL": fake.base + "/kavita",
            "KAVITA_API_KEY": TOKENS["KAVITA_API_KEY"],
        }
    )
    messages, links, checkpoints = split(list(plugin.records({}, "sync")))
    book_msgs = [m for m in messages if m["channel"] == "audiobooks.text"]
    assert len(book_msgs) == 2
    assert [m["external_id"] for m in book_msgs] == [
        "audiobookshelf:book:li-book:chunk:0001",
        "audiobookshelf:book:li-book:chunk:0002",
    ]
    assert all(
        m["thread"] == {"external_id": "audiobookshelf:book:li-book", "title": "Invented Saga"}
        for m in book_msgs
    )
    assert book_msgs[0]["happened_at"] < book_msgs[1]["happened_at"]
    assert "First chapter words." in book_msgs[0]["content"]
    assert "Last chapter words." in book_msgs[1]["content"]
    assert set(book_msgs[0]["fields"]) == set(mapping.BOOK_TEXT_FIELDS)
    assert book_msgs[0]["fields"]["chapter_title"] == "Prologue"
    assert book_msgs[1]["fields"]["chapter_title"] == "Epilogue"
    assert any(
        lk["type"] == "part_of"
        and lk["from"] == {"channel": "audiobooks.text", "external_id": "audiobookshelf:book:li-book:chunk:0001"}
        and lk["to"]["external_ref"] == "audiobookshelf:item:li-book"
        for lk in links
    )
    # Second run with saved cursor skips re-downloading and emits 0 book chunks.
    downloads_before = sum(
        1 for e in fake.log if e["path"] == "/kavita/api/Download/chapter"
    )
    messages2, _, _ = split(list(plugin.records(checkpoints[-1], "sync")))
    assert [m for m in messages2 if m["channel"] == "audiobooks.text"] == []
    downloads_after = sum(
        1 for e in fake.log if e["path"] == "/kavita/api/Download/chapter"
    )
    assert downloads_after == downloads_before


def test_con_33_plex_additions_sessions_and_history_join_the_same_work():
    metadata = {
        "ratingKey": "201",
        "guid": "plex://episode/fake201",
        "Guid": [{"id": "tvdb://201"}],
        "type": "episode",
        "title": "Pilot",
        "grandparentTitle": "Invented Series",
        "grandparentGuid": "plex://show/fakeshow",
        "grandparentRatingKey": "200",
        "parentTitle": "Season 1",
        "parentGuid": "plex://season/fakeseason1",
        "parentRatingKey": "210",
        "parentIndex": 1,
        "index": 1,
        "librarySectionTitle": "TV",
    }
    history = mapping.map_record(
        {
            "kind": "plex.history",
            "id": "fake-server:/status/sessions/history/2",
            "service": "plex",
            "raw": {
                "historyKey": "/status/sessions/history/2",
                "ratingKey": "201",
                "viewedAt": 1578168000,
                "accountID": 1,
            },
            "context": {
                "server": "fake-server",
                "account": {"id": "1", "name": "Fake Owner"},
                "metadata": metadata,
            },
        }
    )
    session = mapping.map_record(
        {
            "kind": "plex.session",
            "id": "fake-server:session:active-1:1578167900",
            "service": "plex",
            "raw": {
                **metadata,
                "viewOffset": 120000,
                "_sessionStart": 1578167900,
                "_sessionSeen": 1578168000,
                "_sessionEnd": None,
                "_initialOffset": 0,
                "Player": {"title": "Fake TV", "product": "Plex Web", "state": "playing"},
            },
            "context": {
                "server": "fake-server",
                "account": {"id": "1", "name": "Fake Owner"},
                "metadata": metadata,
            },
        }
    )
    addition = mapping.map_record(
        {
            "kind": "plex.library_added",
            "id": "fake-server:201",
            "service": "plex",
            "raw": {**metadata, "addedAt": 1578000000},
            "context": {
                "server": "fake-server",
                "section": {"id": "2", "title": "TV", "type": "show"},
            },
        }
    )
    assert history and session and addition

    def joined_work(mapped):
        return next(
            link["to"]["external_ref"]
            for link in mapped.links
            if link["type"] in ("consumed", "saved")
        )

    assert {joined_work(history), joined_work(session), joined_work(addition)} == {
        "plex://episode/fake201"
    }
    assert addition.message["fields"] | {
        "source_kind": "library_added",
        "added_at": "2020-01-02T21:20:00+00:00",
        "plex_rating_key": "201",
    } == addition.message["fields"]
    assert addition.message["fields"]["guids"] == [
        "plex://episode/fake201",
        "tvdb://201",
    ]
    assert session.message["fields"]["source_kind"] == "active_session"
    assert session.message["fields"]["position_s"] == 120
    hierarchy = {
        (link["from"].get("external_ref"), link["to"].get("external_ref"))
        for link in addition.links
        if link["type"] == "part_of"
    }
    assert hierarchy == {
        ("plex://episode/fake201", "plex://season/fakeseason1"),
        ("plex://episode/fake201", "plex://show/fakeshow"),
        ("plex://season/fakeseason1", "plex://show/fakeshow"),
    }
