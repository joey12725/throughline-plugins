"""fetch.py: the standard-library exporter, against an invented local server."""

import gzip
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest
from consumption_fakes import (
    SRC,
    TAKEOUT,
    TOKENS,
    epoch,
    live_env,
    write_backup,
    write_paged_backup,
    write_takeout,
)

from throughline_consumption import fetch

FETCH = SRC / "throughline_consumption" / "fetch.py"
CANARIES = [*TOKENS.values(), "fake-access-canary-1", "fake-rotated-canary"]
NOW = datetime(2020, 2, 1, 12, 0, tzinfo=timezone.utc)


def clean_env(extra):
    names = {name for name, *_ in fetch.ENV.values()}
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in names and not k.startswith("PYTHON")
    }
    env.update(extra)
    return env


def cli(fake, tmp_path, name="export.jsonl", extra=None, state=None, args=()):
    """Run fetch.py the way an operator would: a fresh, isolated interpreter."""
    out = tmp_path / name
    command = [sys.executable, "-I", str(FETCH), "export", "--out", str(out), *args]
    if state:
        command += ["--state", str(state)]
    result = subprocess.run(
        command,
        env=clean_env({**live_env(fake), **(extra or {})}),
        capture_output=True,
        text=True,
        timeout=120,
    )
    lines = []
    if out.exists():
        data = out.read_bytes()
        data = gzip.decompress(data) if out.suffix == ".gz" else data
        lines = [json.loads(x) for x in data.decode("utf-8").splitlines()]
    return result, lines


def in_process(fake, extra=None, state=None, sleep=None):
    config = fetch.Config({**live_env(fake), **(extra or {})})
    fetcher = fetch.Fetcher(config, state, sleep=sleep or (lambda s: None), now=NOW)
    return fetcher, list(fetcher.run())


def test_con_1_cli_exports_every_service_without_secrets(fake, tmp_path):
    write_backup(tmp_path / "backup")
    write_takeout(tmp_path / "takeout")
    state = tmp_path / "state.json"
    result, lines = cli(
        fake,
        tmp_path,
        extra={
            "CONSUMPTION_SPOTIFY_BACKUP_DIR": str(tmp_path / "backup"),
            "CONSUMPTION_YOUTUBE_TAKEOUT": str(tmp_path / "takeout"),
        },
        state=state,
    )
    assert result.returncode == 0, result.stderr
    header, records, footer = lines[0], lines[1:-1], lines[-1]
    assert header["format"] == fetch.FORMAT and header["version"] == 1
    assert header["services"] == [
        "spotify",
        "spotify_backup",
        "plex",
        "audiobookshelf",
        "youtube",
    ]
    assert footer["complete"] and not footer["errors"]
    assert footer["counts"] == {
        "audiobookshelf.progress": 2,
        "audiobookshelf.session": 3,
        "plex.history": 5,
        "spotify.play": 55,
        "spotify.playlist_item": 4,
        "spotify.saved_track": 5,
        "youtube.watch": 5,
    }
    assert footer["notes"] == {
        "spotify_playlists_unreadable": 1,
        "spotify_backup_unreadable_files": 1,
    }
    assert all(
        {"kind", "id", "service", "raw", "fetched_at"} <= set(r) for r in records
    )
    assert len({(r["kind"], r["id"]) for r in records}) == len(records)
    exported = (tmp_path / "export.jsonl").read_text(encoding="utf-8")
    everything = (
        exported + result.stdout + result.stderr + state.read_text(encoding="utf-8")
    )
    for canary in CANARIES:
        assert canary not in everything
    # Volatile and sensitive keys never reach the export.
    for key in (
        '"popularity"',
        '"available_markets"',
        '"ipAddress"',
        '"chapters"',
        '"viewCount"',
    ):
        assert key not in exported
    assert not (tmp_path / "export.jsonl.partial").exists()


def test_con_2_state_makes_runs_incremental(fake, world, tmp_path):
    state = tmp_path / "state.json"
    first, _ = cli(fake, tmp_path, "one.jsonl", state=state)
    assert first.returncode == 0, first.stderr
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["plex"]["server"] == "fake-server" and saved["plex"]["resume"] is False
    assert saved["spotify"]["playlists"]["playlist_snapshots"] == {
        "PLFAKE1": "snap-1",
        "PLFAKE2": "snap-x",
    }
    before = len(fake.log)
    _, lines = cli(fake, tmp_path, "two.jsonl", state=state)
    counts = lines[-1]["counts"]
    # Only the overlap windows (one day of Plex, six hours of sessions) repeat.
    assert counts == {"plex.history": 2, "audiobookshelf.session": 1}
    assert not any("/items" in e["path"] for e in fake.log[before:])
    world.plex_history.append(
        dict(
            world.plex_history[0],
            historyKey="/status/sessions/history/6",
            viewedAt=world.plex_history[-1]["viewedAt"] + 3600,
        )
    )
    world.playlists[0]["snapshot_id"] = "snap-2"
    _, lines = cli(fake, tmp_path, "three.jsonl", state=state)
    ids = {r["id"] for r in lines[1:-1]}
    assert "fake-server:/status/sessions/history/6" in ids
    assert lines[-1]["counts"]["spotify.playlist_item"] == 3


def test_con_3_bounded_backfill_never_moves_cursors(fake, tmp_path):
    state = tmp_path / "state.json"
    result, lines = cli(
        fake,
        tmp_path,
        state=state,
        args=(
            "--since",
            "2020-01-04",
            "--until",
            "2020-01-05",
            "--services",
            "plex,audiobookshelf",
        ),
    )
    assert result.returncode == 0, result.stderr
    assert {r["id"] for r in lines[1:-1]} == {
        "fake-server:/status/sessions/history/2",
        "fake-server:/status/sessions/history/3",
    }
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert "watermark" not in saved["plex"]
    assert "updated_ms" not in saved["audiobookshelf"]["sessions"]


def test_con_4_retries_429_and_5xx_with_retry_after(fake):
    fake.faults["/plex/identity"] = [(429, {"Retry-After": "3"}), (503, {})]
    sleeps = []
    fetcher, records = in_process(
        fake, {"CONSUMPTION_SERVICES": "plex"}, sleep=sleeps.append
    )
    assert not fetcher.errors and len(records) == 5
    assert sleeps[0] >= 3 and sleeps[1] > 0


def test_con_5_persistent_failure_is_reported_per_service(fake, tmp_path):
    fake.faults["/abs/api/me"] = [(500, {})] * 5
    result, lines = cli(
        fake, tmp_path, extra={"CONSUMPTION_SERVICES": "plex,audiobookshelf"}
    )
    assert result.returncode == 2
    footer = lines[-1]
    assert footer["errors"] == {"audiobookshelf": "audiobookshelf me failed (HTTP 500)"}
    assert footer["counts"] == {"plex.history": 5}
    unauthorized, lines = cli(
        fake,
        tmp_path,
        "bad.jsonl",
        extra={"PLEX_TOKEN": "fake-wrong-canary", "CONSUMPTION_SERVICES": "plex"},
    )
    assert lines[-1]["errors"] == {"plex": "plex identity failed (HTTP 401)"}
    assert "fake-wrong-canary" not in unauthorized.stderr + unauthorized.stdout


def test_con_6_request_limits_stop_and_resume(fake):
    state, ids, runs = {}, set(), 0
    while True:
        runs += 1
        fetcher, records = in_process(
            fake,
            {"CONSUMPTION_SERVICES": "plex", "CONSUMPTION_MAX_REQUESTS": "12"},
            state,
        )
        ids |= {r["id"] for r in records}
        state = fetcher.state
        if not fetcher.stopped:
            break
        assert "per-run request limit" in fetcher.stopped["plex"]
        # A later Plex phase (active sessions/additions) may hit the request
        # cap after history itself completed; only an interrupted history pass
        # keeps its no-overlap resume flag.
        if state["plex"]["resume"] is False:
            assert state["plex"]["watermark"] == max(
                row["viewedAt"] for row in fake.world.plex_history
            )
        assert runs < 12
    assert runs > 1 and len(ids) == 5
    assert state["plex"]["resume"] is False
    day = NOW.date().isoformat()
    fetcher, records = in_process(
        fake,
        {
            "CONSUMPTION_SERVICES": "plex",
            "CONSUMPTION_MAX_REQUESTS_PER_DAY": str(state["requests_by_day"][day]),
        },
        state,
    )
    assert records == [] and "per-day request limit" in fetcher.stopped["plex"]


def test_con_7_item_limit_and_metadata_limit(fake):
    fetcher, records = in_process(
        fake, {"CONSUMPTION_SERVICES": "plex", "CONSUMPTION_MAX_ITEMS": "2"}
    )
    assert len(records) == 2 and fetcher.stopped == {
        "plex": "per-run item limit reached"
    }
    fetcher, records = in_process(
        fake, {"CONSUMPTION_SERVICES": "plex", "CONSUMPTION_PLEX_METADATA_LOOKUPS": "1"}
    )
    assert len(records) == 1 and "metadata lookup limit" in fetcher.stopped["plex"]


def test_con_8_redirects_are_refused(fake):
    fake.redirect.add("/plex/identity")
    fetcher, records = in_process(fake, {"CONSUMPTION_SERVICES": "plex"})
    assert records == [] and fetcher.errors == {
        "plex": "redirect refused failed (HTTP 302)"
    }


def test_con_9_spotify_refreshes_once_on_401_and_keeps_tokens_in_memory(fake):
    fetcher, records = in_process(
        fake,
        {
            "CONSUMPTION_SERVICES": "spotify",
            "SPOTIFY_ACCESS_TOKEN": "fake-stale-canary",
        },
    )
    assert not fetcher.errors and len(records) == 61
    paths = [e["path"] for e in fake.log]
    assert paths[:3] == [
        "/spotify/v1/me/player/recently-played",
        "/spotify/token",
        "/spotify/v1/me/player/recently-played",
    ]
    assert paths.count("/spotify/token") == 1
    serialized = json.dumps([fetcher.state, records])
    for canary in [*CANARIES, "fake-stale-canary"]:
        assert canary not in serialized
    plays = sorted(r["id"] for r in records if r["kind"] == "spotify.play")
    assert len(plays) == 55 and "before" in fake.log[3]["query"]


def test_con_10_composio_transport_matches_web_api(fake, tmp_path):
    env = live_env(fake, spotify="composio")
    config = fetch.Config({**env, "CONSUMPTION_SERVICES": "spotify"})
    fetcher = fetch.Fetcher(config, sleep=lambda s: None, now=NOW)
    via_composio = list(fetcher.run())
    assert not fetcher.errors and fetcher.notes == {"spotify_playlists_unreadable": 1}
    calls = [e for e in fake.log if e["path"].startswith("/composio/")]
    assert calls and all(
        e["method"] == "POST"
        and e["headers"].get("x-api-key") == TOKENS["COMPOSIO_API_KEY"]
        for e in calls
    )
    _, via_api = in_process(fake, {"CONSUMPTION_SERVICES": "spotify"})
    assert [(r["kind"], r["id"], r["raw"]) for r in via_composio] == [
        (r["kind"], r["id"], r["raw"]) for r in via_api
    ]


def test_con_11_spotify_backup_snapshots(tmp_path):
    write_backup(tmp_path / "backup")
    config = fetch.Config({"CONSUMPTION_SPOTIFY_BACKUP_DIR": str(tmp_path / "backup")})
    fetcher = fetch.Fetcher(config, now=NOW)
    records = list(fetcher.run())
    by_id = {(r["kind"], r["id"]): r for r in records}
    assert set(by_id) == {
        ("spotify.saved_track", "spotify:track:FAKEOLD1"),
        ("spotify.saved_track", "spotify:track:FAKENOTIME"),
        (
            "spotify.playlist_item",
            next(i for k, i in by_id if k == "spotify.playlist_item"),
        ),
    }
    # A like without added_at keeps the earliest snapshot that showed it.
    assert (
        by_id[("spotify.saved_track", "spotify:track:FAKENOTIME")]["observed_at"]
        == "2019-12-15T00:00:00+00:00"
    )
    assert "popularity" not in json.dumps(records)
    again = fetch.Fetcher(config, fetcher.state, now=NOW)
    assert list(again.run()) == []


def test_con_11b_spotify_backup_raw_pages(tmp_path):
    write_paged_backup(tmp_path / "backup")
    config = fetch.Config({"CONSUMPTION_SPOTIFY_BACKUP_DIR": str(tmp_path / "backup")})
    fetcher = fetch.Fetcher(config, now=NOW)
    records = list(fetcher.run())
    liked = [r for r in records if r["kind"] == "spotify.saved_track"]
    entries = [r for r in records if r["kind"] == "spotify.playlist_item"]
    assert [r["id"] for r in liked] == ["spotify:track:FAKEPG3"]
    # Three real entries across two pages; the null entry is skipped, and the
    # same entry repeated on two pages stays two occurrences.
    assert len(entries) == 3
    assert sorted(r["id"].rsplit(":", 1)[1] for r in entries) == ["1", "1", "2"]
    assert all(r["id"].startswith("PLPAGED:") for r in entries)
    assert {r["context"]["playlist_name"] for r in entries} == {"Invented Paged List"}
    assert {r["context"]["playlist_owner_id"] for r in entries} == {"fake-owner"}
    assert {r["observed_at"] for r in records} == {"2020-01-12T00:00:00+00:00"}
    assert "available_markets" not in json.dumps(records)
    again = fetch.Fetcher(config, fetcher.state, now=NOW)
    assert list(again.run()) == []


def youtube_plugin_identity(row, occurrences):
    """The youtube plugin's Takeout watch ID, re-derived independently."""
    url = row.get("titleUrl", "")
    video = (
        url.split("v=")[1]
        if "v=" in url
        else (url.split("/shorts/")[1] if "/shorts/" in url else None)
    )
    at = datetime.fromisoformat(row["time"].replace("Z", "+00:00")).isoformat()
    base = hashlib.sha256(
        json.dumps(
            [video, at, row["title"] if not video else None],
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()[:24]
    occurrences[base] = occurrences.get(base, 0) + 1
    return f"takeout:watch:{base}:{occurrences[base]}"


def test_con_12_youtube_takeout_ids_match_the_youtube_plugin(tmp_path):
    write_takeout(tmp_path)
    fetcher = fetch.Fetcher(
        fetch.Config({"CONSUMPTION_YOUTUBE_TAKEOUT": str(tmp_path)}), now=NOW
    )
    records = list(fetcher.run())
    occurrences = {}
    expected = [
        youtube_plugin_identity(r, occurrences)
        for r in TAKEOUT
        if "/results?" not in r.get("titleUrl", "")
    ]
    assert [r["id"] for r in records] == expected
    parsed = [r["context"]["parsed"] for r in records]
    assert parsed[2]["is_short"] is True and parsed[3]["is_music"] is True
    assert (
        parsed[4]["video_id"] is None
        and parsed[4]["title"] == "a video that has been removed"
    )


def test_con_13_youtube_html_and_plugin_message_exports(tmp_path):
    page = (
        '<html><body><div class="outer-cell"><div class="content-cell">Watched '
        '<a href="https://www.youtube.com/watch?v=FAKE_HTML">Invented &amp; Rare</a><br>'
        '<a href="https://www.youtube.com/channel/UC_FAKE_HTML">Invented Channel</a><br>'
        "Mar 2, 2019, 1:05:09 PM UTC<br></div></div></body></html>"
    )
    (tmp_path / "watch-history.html").write_text(page, encoding="utf-8")
    fetcher = fetch.Fetcher(
        fetch.Config(
            {"CONSUMPTION_YOUTUBE_TAKEOUT": str(tmp_path / "watch-history.html")}
        ),
        now=NOW,
    )
    (record,) = list(fetcher.run())
    assert record["context"]["parsed"] == {
        "video_id": "FAKE_HTML",
        "title": "Invented & Rare",
        "channel": "Invented Channel",
        "channel_id": "UC_FAKE_HTML",
        "url": "https://www.youtube.com/watch?v=FAKE_HTML",
        "at": "2019-03-02T13:05:09+00:00",
        "source": "takeout",
    }
    (tmp_path / "bad.html").write_text(page.replace("UTC", "EST"), encoding="utf-8")
    fetcher = fetch.Fetcher(
        fetch.Config({"CONSUMPTION_YOUTUBE_TAKEOUT": str(tmp_path / "bad.html")}),
        now=NOW,
    )
    assert (
        list(fetcher.run()) == []
        and "export history as JSON" in fetcher.errors["youtube"]
    )
    messages = tmp_path / "youtube-watch.jsonl"
    rows = [
        {
            "channel": "youtube.watch",
            "external_id": "takeout:watch:abc:1",
            "happened_at": "2019-04-01T10:00:00+02:00",
            "fields": {
                "video_id": "FAKE_X",
                "video_title": "Invented X",
                "channel_title": "Invented Y",
                "youtube_channel_id": "UC_FAKE_Y",
                "url": "https://www.youtube.com/watch?v=FAKE_X",
                "time_precision": "source",
                "source": "takeout",
                "is_short": False,
            },
        },
        {
            "channel": "youtube.watch_later",
            "external_id": "playlist:WL:FAKE_X:1",
            "happened_at": "2019-04-01T10:00:00Z",
        },
        {
            "external_id": "ytdlp:watch:FAKE_Z",
            "record_time": "2019-05-01T00:00:00Z",
            "video_id": "FAKE_Z",
            "video_title": "Invented Z",
            "time_precision": "first_seen",
        },
    ]
    messages.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    fetcher = fetch.Fetcher(
        fetch.Config({"CONSUMPTION_YOUTUBE_MESSAGES": str(messages)}), now=NOW
    )
    records = list(fetcher.run())
    assert [r["id"] for r in records] == ["takeout:watch:abc:1", "ytdlp:watch:FAKE_Z"]
    assert records[0]["context"]["parsed"]["at"] == "2019-04-01T08:00:00+00:00"
    assert records[1]["context"]["parsed"]["time_precision"] == "first_seen"


def test_con_14_configuration_errors():
    with pytest.raises(ValueError, match="Unknown CONSUMPTION_SERVICES"):
        fetch.Config({"CONSUMPTION_SERVICES": "netflix"})
    with pytest.raises(ValueError, match="not configured"):
        fetch.Config({"CONSUMPTION_SERVICES": "plex"}).configured()
    with pytest.raises(ValueError, match="without credentials"):
        fetch.Config({"PLEX_SERVER_URL": "http://user:pw@invented.invalid"})
    with pytest.raises(ValueError, match="after CONSUMPTION_SINCE"):
        fetch.Config(
            {"CONSUMPTION_SINCE": "2020-02-01", "CONSUMPTION_UNTIL": "2020-01-01"}
        )
    assert fetch.Config({}).configured() == []


def test_con_15_cli_stdout_gzip_and_services(fake, tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(FETCH),
            "export",
            "--out",
            "-",
            "--services",
            "audiobookshelf",
        ],
        env=clean_env(live_env(fake)),
        capture_output=True,
        text=True,
        timeout=60,
    )
    lines = [json.loads(x) for x in result.stdout.splitlines()]
    assert result.returncode == 0 and lines[-1]["counts"] == {
        "audiobookshelf.progress": 2,
        "audiobookshelf.session": 3,
    }
    result, _ = cli(
        fake, tmp_path, "packed.jsonl.gz", extra={"CONSUMPTION_SERVICES": "plex"}
    )
    packed = gzip.decompress((tmp_path / "packed.jsonl.gz").read_bytes()).decode()
    assert result.returncode == 0 and json.loads(packed.splitlines()[-1])["counts"] == {
        "plex.history": 5
    }
    listed = subprocess.run(
        [sys.executable, "-I", str(FETCH), "services"],
        env=clean_env(live_env(fake)),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert json.loads(listed.stdout) == {
        "configured": ["spotify", "plex", "audiobookshelf"]
    }
    # Secrets are never options.
    refused = subprocess.run(
        [sys.executable, "-I", str(FETCH), "export", "--out", "-", "--plex-token", "x"],
        env=clean_env({}),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert refused.returncode != 0


def test_con_16_fetch_imports_only_the_standard_library():
    source = FETCH.read_text(encoding="utf-8")
    imported = set()
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("import "):
            imported |= {
                p.strip().split(" ")[0].split(".")[0] for p in stripped[7:].split(",")
            }
        elif stripped.startswith("from ") and " import " in stripped:
            imported.add(stripped[5:].split(" ")[0].split(".")[0])
    assert imported <= set(sys.stdlib_module_names), imported - set(
        sys.stdlib_module_names
    )


def test_con_17_pages_without_totals_are_read_to_the_end(fake, world):
    """A missing totalSize, numPages or next must not stop paging after one page."""
    small = {"CONSUMPTION_PLEX_PAGE_SIZE": "2", "CONSUMPTION_ABS_PAGE_SIZE": "1"}
    _, full = in_process(fake, {**small, "CONSUMPTION_SERVICES": "plex,audiobookshelf"})
    world.lean = True
    _, lean = in_process(fake, {**small, "CONSUMPTION_SERVICES": "plex,audiobookshelf"})
    assert sorted((r["kind"], r["id"]) for r in lean) == sorted(
        (r["kind"], r["id"]) for r in full
    )
    assert sum(r["kind"] == "plex.history" for r in lean) == 5
    assert sum(r["kind"] == "audiobookshelf.session" for r in lean) == 3
    # Composio envelopes without next or total: saved tracks and playlist items.
    world.saved.extend(
        dict(
            world.saved[0],
            track=dict(
                world.saved[0]["track"],
                id=f"FAKEMORE{n:02}",
                uri=f"spotify:track:FAKEMORE{n:02}",
            ),
            added_at=f"2018-0{1 + n // 28}-{1 + n % 28:02}T00:00:00Z",
        )
        for n in range(60)
    )
    env = live_env(fake, spotify="composio")
    fetcher = fetch.Fetcher(
        fetch.Config({**env, "CONSUMPTION_SERVICES": "spotify"}),
        sleep=lambda s: None,
        now=NOW,
    )
    records = list(fetcher.run())
    assert sum(r["kind"] == "spotify.saved_track" for r in records) == len(world.saved)


def test_con_18_live_urls_keep_the_youtube_plugin_identity(tmp_path):
    rows = [
        {
            "header": "YouTube",
            "title": "Watched Invented Stream",
            "titleUrl": "https://www.youtube.com/live/FAKE_LIVE",
            "time": "2019-04-01T10:00:00Z",
        }
    ]
    (tmp_path / "watch-history.json").write_text(json.dumps(rows), encoding="utf-8")
    fetcher = fetch.Fetcher(
        fetch.Config(
            {"CONSUMPTION_YOUTUBE_TAKEOUT": str(tmp_path / "watch-history.json")}
        ),
        now=NOW,
    )
    (record,) = list(fetcher.run())
    # The youtube plugin has no /live/ rule, so its ID falls back to the title.
    at = "2019-04-01T10:00:00+00:00"
    base = hashlib.sha256(
        json.dumps([None, at, rows[0]["title"]]).encode()
    ).hexdigest()[:24]
    assert record["id"] == f"takeout:watch:{base}:1"
    # The work itself is still recognised for links.
    assert record["context"]["parsed"]["video_id"] == "FAKE_LIVE"


def test_con_19_unexpected_error_fails_one_service_only(fake, monkeypatch):
    def broken(fetcher, state):
        yield from ()
        raise TypeError("unexpected payload shape")

    monkeypatch.setitem(fetch.COLLECTORS, "plex", broken)
    fetcher, records = in_process(fake, {"CONSUMPTION_SERVICES": "plex,audiobookshelf"})
    assert fetcher.errors == {"plex": "unexpected TypeError: unexpected payload shape"}
    assert {r["kind"] for r in records} == {
        "audiobookshelf.session",
        "audiobookshelf.progress",
    }


def test_con_spotify_full_window_past_the_watermark_notes_a_gap(fake, world):
    from datetime import timedelta

    fetcher, _ = in_process(fake, {"CONSUMPTION_SERVICES": "spotify"})
    assert "spotify_recent_gap" not in fetcher.notes  # first run: no watermark
    watermark = fetcher.state["spotify"]["recent"]["recent_after_ms"]
    # Back after an outage: Spotify holds only fifty newer plays.
    newest = datetime.fromtimestamp(watermark / 1000, timezone.utc)
    for n, play in enumerate(world.plays[:50]):
        play["played_at"] = (newest + timedelta(days=3, minutes=4 * n)).isoformat()
    world.plays[:] = world.plays[:50]
    again, records = in_process(
        fake, {"CONSUMPTION_SERVICES": "spotify"}, state=fetcher.state
    )
    assert len([r for r in records if r["kind"] == "spotify.play"]) == 50
    assert again.notes["spotify_recent_gap"] == {
        "from": newest.isoformat(),
        "to": (newest + timedelta(days=3)).isoformat(),
    }
    # The next run reaches back to the new watermark: nothing lost.
    third, _ = in_process(fake, {"CONSUMPTION_SERVICES": "spotify"}, state=again.state)
    assert "spotify_recent_gap" not in third.notes


def test_con_spotify_composio_mcp_fast_lane(fake):
    extra = {
        "CONSUMPTION_SERVICES": "spotify",
        "SPOTIFY_CLIENT_ID": "",
        "SPOTIFY_CLIENT_SECRET": "",
        "SPOTIFY_REFRESH_TOKEN": "",
        "SPOTIFY_COMPOSIO_MCP_URL": fake.base + "/composio-mcp",
        "SPOTIFY_COMPOSIO_MCP_TOKEN": TOKENS["SPOTIFY_COMPOSIO_MCP_TOKEN"],
        "SPOTIFY_COMPOSIO_MCP_ACCOUNT": "fake-account",
        "SPOTIFY_EXPECTED_USER": "fake-owner",
    }
    fetcher, records = in_process(fake, extra)
    assert not fetcher.errors
    assert len(records) == 50
    assert {record["kind"] for record in records} == {"spotify.play"}
    assert TOKENS["SPOTIFY_COMPOSIO_MCP_TOKEN"] not in json.dumps(fetcher.state)
    assert sum(1 for entry in fake.log if entry["path"] == "/composio-mcp") == 1


def test_con_plex_active_sessions_and_library_additions(fake, world):
    movie_added = epoch("2020-01-20T12:00:00+00:00")
    episode_added = epoch("2020-01-21T12:00:00+00:00")
    album_added = epoch("2020-01-22T12:00:00+00:00")
    world.plex_sections = [
        {"key": "1", "type": "movie", "title": "Films"},
        {"key": "2", "type": "show", "title": "TV"},
        {"key": "3", "type": "artist", "title": "Music"},
    ]
    world.plex_additions = {
        "1": [
            {
                **world.plex_metadata["101"],
                "addedAt": movie_added,
                "typeCode": 1,
                "Guid": [{"id": "tmdb://101"}],
            }
        ],
        "2": [
            {
                **world.plex_metadata["201"],
                "addedAt": episode_added,
                "typeCode": 4,
                "Guid": [{"id": "tvdb://201"}],
            }
        ],
        "3": [
            {
                "ratingKey": "401",
                "guid": "plex://album/fake401",
                "type": "album",
                "typeCode": 9,
                "title": "Invented Album",
                "parentTitle": "Invented Artist",
                "addedAt": album_added,
                "Guid": [{"id": "mbid://fake401"}],
            }
        ],
    }
    world.plex_active_sessions = [
        {
            "ratingKey": "201",
            "type": "episode",
            "title": "Pilot",
            "viewOffset": 120000,
            "User": {"id": 1, "title": "Fake Owner"},
            "Session": {"id": "active-1"},
            "Player": {"title": "Fake TV", "product": "Plex Web", "state": "playing"},
        }
    ]
    fetcher, records = in_process(fake, {"CONSUMPTION_SERVICES": "plex"})
    assert not fetcher.errors
    additions = [record for record in records if record["kind"] == "plex.library_added"]
    sessions = [record for record in records if record["kind"] == "plex.session"]
    assert {record["raw"]["type"] for record in additions} == {
        "movie",
        "episode",
        "album",
    }
    assert {record["raw"]["addedAt"] for record in additions} == {
        movie_added,
        episode_added,
        album_added,
    }
    assert len(sessions) == 1 and sessions[0]["raw"]["_sessionEnd"] is None
    assert fetcher.state["plex"]["additions"] == {
        "1": {"watermark": movie_added},
        "2": {"watermark": episode_added},
        "3": {"watermark": album_added},
    }

    world.plex_active_sessions = []
    later = fetch.Fetcher(
        fetch.Config({**live_env(fake), "CONSUMPTION_SERVICES": "plex"}),
        fetcher.state,
        sleep=lambda _seconds: None,
        now=NOW + timedelta(minutes=5),
    )
    second = list(later.run())
    closed = [record for record in second if record["kind"] == "plex.session"]
    assert len(closed) == 1 and closed[0]["raw"]["_sessionEnd"] is not None


def test_con_kavita_threshold_matching_extraction_and_idempotency(fake, world):
    import io
    import zipfile
    from consumption_fakes import KAVITA_JWT, TOKENS, build_test_epub

    base_ms = world.progress[0]["startedAt"]
    # Exact 25% boundary -> must NOT be fetched.
    world.progress.append(
        {
            "id": "prog-25",
            "userId": "fake-user",
            "libraryItemId": "li-book-25",
            "episodeId": None,
            "duration": 10000,
            "progress": 0.25,
            "currentTime": 2500,
            "isFinished": False,
            "lastUpdate": base_ms + 1000,
            "startedAt": base_ms,
        }
    )
    world.items["li-book-25"] = {
        "id": "li-book-25",
        "libraryId": "lib-books",
        "mediaType": "book",
        "media": {
            "metadata": {"title": "Quarter Book", "authors": [{"name": "Jane Quarter"}]}
        },
    }
    world.add_kavita_book(
        chapter_id=501,
        title="Quarter Book",
        authors=["Jane Quarter"],
        file_path="/books/Quarter Book (Jane Quarter).epub",
        data=build_test_epub([("Ch 1", "<p>Quarter text.</p>")]),
        series_id=50,
    )

    # >25% (0.42) -> MUST match real novel EPUB (NOT the Graphic Novel!) and split long chapter.
    world.progress.append(
        {
            "id": "prog-read",
            "userId": "fake-user",
            "libraryItemId": "li-book-read",
            "episodeId": None,
            "duration": 20000,
            "progress": 0.42,
            "currentTime": 8400,
            "isFinished": False,
            "lastUpdate": base_ms + 2000,
            "startedAt": base_ms + 500,
        }
    )
    world.items["li-book-read"] = {
        "id": "li-book-read",
        "libraryId": "lib-books",
        "mediaType": "book",
        "media": {
            "metadata": {
                "title": "Dungeon Crawler Test (Unabridged)",
                "authors": [{"name": "Matt Tester"}],
                "seriesName": "Crawler Series #1",
            }
        },
    }
    long_para_a = "Alpha paragraph sentence. " * 25
    long_para_b = "Beta paragraph sentence. " * 25
    epub_bytes = build_test_epub(
        [
            ("Chapter One", f"<p>{long_para_a}</p><p>{long_para_b}</p>"),
            ("Chapter Two", "<p>Final &amp; clean ending.</p>"),
        ]
    )
    # Add a Graphic Novel decoy and the true novel EPUB (wrapped in multi-file ZIP!).
    world.add_kavita_book(
        chapter_id=502,
        title="Dungeon Crawler Test, Vol. 1 (Graphic Novel)",
        authors=["Matt Tester"],
        file_path="/books/Dungeon Crawler Test, Vol. 1 (Graphic Novel).epub",
        data=build_test_epub([("Panel", "<p>Comic text</p>")]),
        series_id=51,
    )
    nested_zip = io.BytesIO()
    with zipfile.ZipFile(nested_zip, "w") as zf:
        zf.writestr("Dungeon Crawler Test (Matt Tester).epub", epub_bytes)
    world.add_kavita_book(
        chapter_id=503,
        title="Dungeon Crawler Test",
        authors=["Matt Tester"],
        file_path="/books/Dungeon Crawler Test (Matt Tester).epub",
        data=nested_zip.getvalue(),
        series_id=52,
    )

    # isFinished=True even with low progress -> MUST match (TXT format test!).
    world.progress.append(
        {
            "id": "prog-fin",
            "userId": "fake-user",
            "libraryItemId": "li-book-fin",
            "episodeId": None,
            "duration": 5000,
            "progress": 0.1,
            "currentTime": 500,
            "isFinished": True,
            "lastUpdate": base_ms + 3000,
            "startedAt": base_ms + 1000,
            "finishedAt": base_ms + 3000,
        }
    )
    world.items["li-book-fin"] = {
        "id": "li-book-fin",
        "libraryId": "lib-books",
        "mediaType": "book",
        "media": {
            "metadata": {
                "title": "Plain Text Tale",
                "authors": [{"name": "Alice Plain"}],
                "isbn": "9781234567890",
            }
        },
    }
    world.add_kavita_book(
        chapter_id=504,
        title="Completely Different Edition Title",
        authors=["Alice Plain"],
        file_path="/books/Plain Text Tale.txt",
        data=b"Line one of plain text.\r\n\r\nLine two after blank.",
        isbn="9781234567890",
        extension=".txt",
        series_id=53,
    )

    # >25% with NO matching Kavita book -> skipped gracefully, no error.
    world.progress.append(
        {
            "id": "prog-miss",
            "userId": "fake-user",
            "libraryItemId": "li-book-miss",
            "episodeId": None,
            "duration": 9000,
            "progress": 0.85,
            "currentTime": 7650,
            "isFinished": False,
            "lastUpdate": base_ms + 4000,
            "startedAt": base_ms + 1500,
        }
    )
    world.items["li-book-miss"] = {
        "id": "li-book-miss",
        "libraryId": "lib-books",
        "mediaType": "book",
        "media": {
            "metadata": {
                "title": "Totally Missing Audiobook",
                "authors": [{"name": "Nobody Home"}],
            }
        },
    }

    extra = {
        "CONSUMPTION_SERVICES": "audiobookshelf",
        "KAVITA_URL": fake.base + "/kavita",
        "KAVITA_API_KEY": TOKENS["KAVITA_API_KEY"],
        "CONSUMPTION_BOOK_CHUNK_CHARS": "700",
    }
    fetcher, records = in_process(fake, extra)
    assert not fetcher.errors
    assert fetcher.notes.get("kavita_unmatched_books") == 1

    chunks = [r for r in records if r["kind"] == "audiobook.text_chunk"]
    by_book = {}
    for c in chunks:
        by_book.setdefault(c["raw"]["abs_item_id"], []).append(c)

    # li-book (0.15), li-book-25 (0.25), li-pod (podcast), li-book-miss (unmatched) excluded.
    assert set(by_book) == {"li-book-read", "li-book-fin"}

    read_chunks = by_book["li-book-read"]
    assert len(read_chunks) == 3  # 2 parts for Chapter One + 1 part for Chapter Two
    assert all(c["raw"]["kavita_chapter_id"] == 503 for c in read_chunks)
    assert all(c["raw"]["book_format"] == "epub" for c in read_chunks)
    assert all(len(c["raw"]["text"]) <= 700 for c in read_chunks)
    assert [c["raw"]["chunk_index"] for c in read_chunks] == [1, 2, 3]
    assert [c["raw"]["chapter_title"] for c in read_chunks] == [
        "Chapter One",
        "Chapter One",
        "Chapter Two",
    ]
    assert "Final & clean ending." in read_chunks[-1]["raw"]["text"]
    assert "ignore()" not in json.dumps(read_chunks)
    assert "color:red" not in json.dumps(read_chunks)

    fin_chunks = by_book["li-book-fin"]
    assert len(fin_chunks) == 1
    assert fin_chunks[0]["raw"]["book_format"] == "txt"
    assert fin_chunks[0]["raw"]["match_method"] == "isbn"
    assert "Line one of plain text." in fin_chunks[0]["raw"]["text"]

    # Secrets (API key and in-memory JWT) never leak into records or state.
    blob = json.dumps([records, fetcher.state])
    assert TOKENS["KAVITA_API_KEY"] not in blob
    assert KAVITA_JWT not in blob

    # Second run: unchanged books MUST NOT be re-downloaded or re-emitted.
    downloads_before = sum(
        1 for e in fake.log if e["path"] == "/kavita/api/Download/chapter"
    )
    assert downloads_before == 2
    again, records2 = in_process(fake, extra, state=fetcher.state)
    assert not again.errors
    assert [r for r in records2 if r["kind"] == "audiobook.text_chunk"] == []
    downloads_after = sum(
        1 for e in fake.log if e["path"] == "/kavita/api/Download/chapter"
    )
    assert downloads_after == downloads_before

    # If the Kavita file hash changes, it IS re-downloaded on the next run.
    world.kavita_chapters[504]["files"][0]["koreaderHash"] = "FAKEHASH2"
    third, records3 = in_process(fake, extra, state=again.state)
    chunks3 = [r for r in records3 if r["kind"] == "audiobook.text_chunk"]
    assert len(chunks3) == 1 and chunks3[0]["raw"]["abs_item_id"] == "li-book-fin"


def test_con_ebook_pdf_and_author_guard_unit():
    # Minimal PDF stream text extraction (stdlib fallback).
    pdf = (
        b"%PDF-1.4\n1 0 obj\n<< /Length 44 >>\nstream\n"
        b"BT /F1 12 Tf (Hello PDF World) Tj ET\nendstream\nendobj\n%%EOF"
    )
    fmt, chapters = fetch.extract_ebook_chapters(pdf, filename_hint="sample.pdf")
    assert fmt == "pdf"
    assert chapters and "Hello PDF World" in chapters[0][1]

    # Guard: author-only ABS title with empty authors does not match arbitrary book by that author.
    ch = {
        "id": 900,
        "titleName": "Burn for Me",
        "writers": [{"name": "Sara Cate"}],
        "files": [{"filePath": "/books/Burn for Me (Sara Cate).epub", "extension": ".epub", "bytes": 1000}],
    }
    score, _method, _bf = fetch.score_kavita_chapter("Sara Cate", [], None, ch)
    assert score == 0.0
