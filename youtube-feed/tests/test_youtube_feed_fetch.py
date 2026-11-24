import ast
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs

import pytest

from throughline_youtube_feed import fetch

FETCH = Path(fetch.__file__)


def feeds(server, yt, pages):
    """Serve RSS per channel_id query from a dict channel_id -> xml (mutable)."""

    def handler(request):
        cid = parse_qs(request.query).get("channel_id", [""])[0]
        if cid not in pages:
            return 404, {}, "no such channel"
        value = pages[cid]
        if isinstance(value, list):
            value = value.pop(0) if len(value) > 1 else value[0]
        if isinstance(value, tuple):
            return value
        return 200, {"Content-Type": "application/atom+xml"}, value

    server.routes[("GET", "/feeds/videos.xml")] = handler


def test_fetch_is_standard_library_only():
    tree = ast.parse(FETCH.read_text("utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "fetch.py must not use relative imports"
            modules.add(node.module.split(".")[0])
    assert modules <= set(sys.stdlib_module_names), modules - set(
        sys.stdlib_module_names
    )


def test_channel_lists_opml_json_csv_text(tmp_path, yt):
    opml = tmp_path / "subs.opml"
    opml.write_text(
        f"""<opml version="1.1"><body><outline text="YouTube Subscriptions">
<outline text="Invented One" title="Invented One" type="rss" xmlUrl="https://www.youtube.com/feeds/videos.xml?channel_id={yt.CH1}"/>
</outline></body></opml>""",
        "utf-8",
    )
    api = tmp_path / "subs.json"
    api.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "snippet": {
                            "title": "Invented Two",
                            "resourceId": {
                                "kind": "youtube#channel",
                                "channelId": yt.CH2,
                            },
                        }
                    },
                    {
                        "snippet": {
                            "title": "Dup One",
                            "resourceId": {"channelId": yt.CH1},
                        }
                    },
                ]
            }
        ),
        "utf-8",
    )
    notify = tmp_path / "state.json"
    notify.write_text(
        json.dumps(
            {
                "version": 2,
                "channels": [{"channel_id": yt.CH3, "channel_name": "Invented Three"}],
                "pending": [{"channel_id": "UC" + "z" * 22}],
            }
        ),
        "utf-8",
    )
    takeout = tmp_path / "subscriptions.csv"
    takeout.write_text(
        f"Channel Id,Channel Url,Channel Title\n{yt.CH2},http://www.youtube.com/channel/{yt.CH2},Invented Two CSV\n",
        "utf-8",
    )
    text = tmp_path / "channels.txt"
    text.write_text(
        f"# invented\nhttps://www.youtube.com/channel/{yt.CH3} Invented Three Text\n{yt.CH1}\nnot a channel\n",
        "utf-8",
    )

    assert fetch.parse_channel_list(opml.read_text(), opml.name) == [
        (yt.CH1, "Invented One")
    ]
    assert fetch.parse_channel_list(api.read_text(), api.name) == [
        (yt.CH2, "Invented Two"),
        (yt.CH1, "Dup One"),
    ]
    # Only the channels list of a youtube-notify state file counts.
    assert fetch.parse_channel_list(notify.read_text(), notify.name) == [
        (yt.CH3, "Invented Three")
    ]
    assert fetch.parse_channel_list(takeout.read_text(), takeout.name) == [
        (yt.CH2, "Invented Two CSV")
    ]
    assert fetch.parse_channel_list(text.read_text(), text.name) == [
        (yt.CH3, "Invented Three Text"),
        (yt.CH1, None),
    ]
    cfg = fetch.config(
        {},
        channels=os.pathsep.join(map(str, [opml, api, text])),
        channel_ids=f"{yt.CH2}, junk",
    )
    assert [(c["channel_id"], c["title"]) for c in fetch.load_channels(cfg)] == [
        (yt.CH1, "Invented One"),
        (yt.CH2, "Invented Two"),
        (yt.CH3, "Invented Three Text"),
    ]
    with pytest.raises(ValueError):
        fetch.parse_channel_list('<!DOCTYPE x [<!ENTITY a "b">]><opml/>', "evil.opml")


def test_parse_feed_separates_volatile_fields_and_shorts(yt):
    xml = yt.feed_xml(
        yt.CH1,
        "Invented One",
        [
            {
                "n": 1,
                "title": "First &amp; best",
                "published": "2026-09-30T10:00:00+00:00",
                "views": 5,
            },
            {
                "n": 2,
                "title": "A short",
                "published": "2026-09-30T11:00:00+00:00",
                "link": f"https://www.youtube.com/shorts/{yt.vid(2)}",
            },
        ],
    )
    info, entries = fetch.parse_feed(xml.encode())
    assert info["channel_id"] == yt.CH1 and info["title"] == "Invented One"
    raw, observed = entries[0]
    assert (
        raw["title"] == "First & best"
        and raw["media"]["description"] == "Invented description 1"
    )
    assert observed == {
        "updated": "2026-09-30T10:00:00+00:00",
        "views": 5,
        "rating": {"count": "5", "average": "5.00", "min": "1", "max": "5"},
    }
    record = {
        "kind": "upload",
        "id": yt.vid(2),
        "transport": "rss",
        "raw": entries[1][0],
    }
    video = fetch.normalize_upload(record)
    assert video["is_short"] is True and video["url"].endswith("/shorts/" + yt.vid(2))
    assert (
        fetch.normalize_upload(
            {"kind": "upload", "id": yt.vid(1), "transport": "rss", "raw": raw}
        )["is_short"]
        is None
    )


def test_collect_incremental_volatile_and_edits(server, yt, cfg):
    pages = {
        yt.CH1: yt.feed_xml(
            yt.CH1,
            "Invented One",
            [{"n": 1, "title": "One", "published": "2026-09-30T10:00:00+00:00"}],
        ),
        yt.CH2: yt.feed_xml(
            yt.CH2,
            "Invented Two",
            [{"n": 2, "title": "Two", "published": "2026-09-29T10:00:00+00:00"}],
        ),
    }
    feeds(server, yt, pages)
    config = cfg(channel_ids=f"{yt.CH1},{yt.CH2}")
    state = {}
    records, stats = fetch.collect(config, state)
    assert sorted(r["id"] for r in records) == [yt.vid(1), yt.vid(2)]
    assert stats["channels_read"] == 2 and stats["uploads"] == 2
    # Same feed with new view counts: nothing new.
    pages[yt.CH1] = yt.feed_xml(
        yt.CH1,
        "Invented One",
        [
            {
                "n": 1,
                "title": "One",
                "published": "2026-09-30T10:00:00+00:00",
                "views": 999,
                "updated": "2026-10-01T00:00:00+00:00",
            }
        ],
    )
    assert fetch.collect(config, state)[0] == []
    # An edited title is a changed record.
    pages[yt.CH1] = yt.feed_xml(
        yt.CH1,
        "Invented One",
        [{"n": 1, "title": "One (edited)", "published": "2026-09-30T10:00:00+00:00"}],
    )
    again, _ = fetch.collect(config, state)
    assert [r["raw"]["title"] for r in again] == ["One (edited)"]
    assert state["days"][yt.NOW.date().isoformat()]["requests"] == 6


def test_retries_backoff_errors_and_daily_limit(server, yt, cfg):
    sleeps = []
    ok = yt.feed_xml(
        yt.CH1,
        "Invented One",
        [{"n": 1, "title": "One", "published": "2026-09-30T10:00:00+00:00"}],
    )
    feeds(
        server,
        yt,
        {yt.CH1: [(429, {"Retry-After": "7"}, "slow down"), (503, {}, "busy"), ok]},
    )
    state = {}
    config = cfg(channel_ids=f"{yt.CH1},{yt.CH3}")
    http = fetch.Http(config, state, sleep=sleeps.append)
    records, stats = fetch.collect(config, state, http)
    assert sleeps == [
        7,
        0,
    ]  # Retry-After honoured, then exponential backoff (0 base in tests).
    assert [r["kind"] for r in records] == ["upload", "feed_error"]
    assert records[1]["status"] == 404 and state["channels"][yt.CH3]["errors"] == 1
    assert stats["retryable_errors"] == 2
    # Daily request cap stops the run cleanly.
    capped = cfg(channel_ids=f"{yt.CH1},{yt.CH3}", requests_per_day="1")
    fresh = {}
    records, stats = fetch.collect(
        capped, fresh, fetch.Http(capped, fresh, sleep=lambda s: None)
    )
    assert stats.get("limit") == "requests_per_day"
    # Persistent 5xx gives up after the retry budget.
    feeds(server, yt, {yt.CH1: (500, {}, "down")})
    config = cfg(channel_ids=yt.CH1, retries="2")
    records, _ = fetch.collect(config, {}, fetch.Http(config, {}, sleep=lambda s: None))
    assert records[0]["kind"] == "feed_error" and records[0]["status"] == 500


def test_rotation_reads_a_batch_per_run(server, yt, cfg):
    feeds(
        server,
        yt,
        {
            c: yt.feed_xml(
                c,
                "Invented",
                [{"n": i, "title": "T", "published": "2026-09-30T10:00:00+00:00"}],
            )
            for i, c in enumerate([yt.CH1, yt.CH2, yt.CH3], 1)
        },
    )
    config = cfg(channel_ids=f"{yt.CH1},{yt.CH2},{yt.CH3}", channels_per_run="2")
    state = {}
    first, _ = fetch.collect(config, state)
    second, _ = fetch.collect(config, state)
    assert [r["channel_id"] for r in first] == [yt.CH1, yt.CH2]
    assert [r["channel_id"] for r in second] == [yt.CH3]  # CH1 again but already seen
    assert state["rotation"] == 1


def test_page_transport_details_and_shorts(server, yt, cfg):
    server.route(
        "GET",
        f"/channel/{yt.CH1}/videos",
        (
            200,
            {},
            yt.channel_page(
                "Invented One",
                [
                    {"n": 1, "title": "Paged", "ago": "3 hours ago", "length": "12:34"},
                    {"n": 2, "title": "Tiny", "short": True},
                ],
            ),
        ),
    )
    server.route("GET", "/watch", (200, {}, yt.watch_page(1)))
    config = cfg(
        channel_ids=yt.CH1,
        transport="page",
        details="1",
        details_per_run="1",
        check_shorts="1",
    )
    state = {}
    records, stats = fetch.collect(config, state)
    paged = next(r for r in records if r["id"] == yt.vid(1))
    assert paged["estimated_published_at"] == "2026-10-01T09:00:00+00:00"
    assert "short_check" not in paged and stats["details"] == 1
    video = fetch.normalize_upload(paged)
    assert video["duration_s"] == 754 and video["time_precision"] == "exact"
    assert video["published_at"] == "2026-09-30T08:00:00-07:00"
    assert video["channel_title"] == "Invented One"
    tiny = fetch.normalize_upload(next(r for r in records if r["id"] == yt.vid(2)))
    assert tiny["is_short"] is True and tiny["time_precision"] == "approximate"
    assert server.hits("HEAD") == []  # the page says which items are Shorts
    # Next run: relative text moved on, but the estimate is fixed at first sight.
    server.route(
        "GET",
        f"/channel/{yt.CH1}/videos",
        (
            200,
            {},
            yt.channel_page(
                "Invented One",
                [
                    {"n": 1, "title": "Paged", "ago": "5 hours ago", "length": "12:34"},
                    {"n": 2, "title": "Tiny", "short": True},
                ],
            ),
        ),
    )
    assert fetch.collect(config, state)[0] == []


def test_auto_transport_falls_back_to_page(server, yt, cfg):
    feeds(server, yt, {})
    server.route(
        "GET",
        f"/channel/{yt.CH1}/videos",
        (200, {}, yt.channel_page("Invented One", [{"n": 4, "title": "Fallback"}])),
    )
    records, _ = fetch.collect(cfg(channel_ids=yt.CH1, transport="auto"), {})
    assert [(r["transport"], r["id"]) for r in records] == [("page", yt.vid(4))]


def test_cli_export_runs_standalone_and_never_prints_secrets(server, yt, tmp_path):
    feeds(
        server,
        yt,
        {
            yt.CH1: yt.feed_xml(
                yt.CH1,
                "Invented One",
                [{"n": 1, "title": "One", "published": "2026-09-30T10:00:00+00:00"}],
            )
        },
    )
    channels = tmp_path / "channels.txt"
    channels.write_text(yt.CH1 + " Invented One\n", "utf-8")
    predictions = tmp_path / "predictions.jsonl"
    predictions.write_text(json.dumps(yt.prediction(7)) + "\n", "utf-8")
    out, state = tmp_path / "out" / "feed.jsonl", tmp_path / "state.json"
    canary = "invented-secret-canary-router"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "YOUTUBE_FEED_RSS_URL": server.url
        + "/feeds/videos.xml?channel_id={channel_id}",
        "YOUTUBE_FEED_DELAY_S": "0",
        "YOUTUBE_FEED_ROUTER_API_KEY": canary,
        "YOUTUBE_FEED_PLAYLIST": "PLinventedinventedinvented",
    }
    command = [
        sys.executable,
        "-I",
        str(FETCH),
        "export",
        "--out",
        str(out),
        "--state",
        str(state),
        "--channels",
        str(channels),
        "--import-predictions",
        str(predictions),
        "--scorer",
        "jev",
    ]
    result = subprocess.run(
        command, env=env, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    lines = [json.loads(x) for x in out.read_text("utf-8").splitlines()]
    assert [r["kind"] for r in lines] == ["upload", "pick", "run"]
    assert lines[1]["id"] == yt.vid(7) + ":jev"
    assert json.loads(state.read_text())["seen"][yt.vid(1)]
    assert (
        canary
        not in result.stdout + result.stderr + out.read_text() + state.read_text()
    )
    # Incremental: a second run writes only the run record.
    result = subprocess.run(
        command[:-4], env=env, capture_output=True, text=True, timeout=60
    )
    assert [json.loads(x)["kind"] for x in out.read_text("utf-8").splitlines()] == [
        "run"
    ]
    # No credentials on the command line.
    help_text = subprocess.run(
        [sys.executable, "-I", str(FETCH), "export", "--help"],
        capture_output=True,
        text=True,
    ).stdout
    assert "key" not in help_text.lower() and "token" not in help_text.lower()


def test_rss_shorts_check_uses_one_head_without_redirects(server, yt, cfg):
    feeds(
        server,
        yt,
        {
            yt.CH1: yt.feed_xml(
                yt.CH1,
                "Invented One",
                [
                    {"n": 1, "title": "Long", "published": "2026-09-30T10:00:00+00:00"},
                    {
                        "n": 2,
                        "title": "Short",
                        "published": "2026-09-30T11:00:00+00:00",
                    },
                    {
                        "n": 3,
                        "title": "Linked short",
                        "published": "2026-09-30T12:00:00+00:00",
                        "link": "https://www.youtube.com/shorts/" + yt.vid(3),
                    },
                ],
            )
        },
    )
    server.route(
        "HEAD", "/shorts/" + yt.vid(1), (303, {"Location": "/watch?v=" + yt.vid(1)}, "")
    )
    server.route("HEAD", "/shorts/" + yt.vid(2), (200, {}, ""))
    records, _ = fetch.collect(cfg(channel_ids=yt.CH1, check_shorts="1"), {})
    shorts = {r["id"]: fetch.normalize_upload(r)["is_short"] for r in records}
    assert shorts == {yt.vid(1): False, yt.vid(2): True, yt.vid(3): True}
    assert sorted(r.path for r in server.hits("HEAD")) == [
        "/shorts/" + yt.vid(1),
        "/shorts/" + yt.vid(2),
    ]
    assert server.hits("GET", "/watch") == []  # the redirect was not followed


def test_full_feed_past_the_newest_seen_upload_records_a_gap(server, yt, cfg):
    def page(first):
        return yt.feed_xml(
            yt.CH1,
            "Invented One",
            [
                {"n": n, "title": f"Upload {n}", "published": f"2026-09-{n:02d}T10:00:00+00:00"}
                for n in range(first + 14, first - 1, -1)
            ],
        )

    pages = {yt.CH1: page(1)}  # uploads of 1..15 September
    feeds(server, yt, pages)
    state, config = {}, cfg(channel_ids=yt.CH1)
    records, _ = fetch.collect(config, state, fetch.Http(config, state))
    assert "feed_gap" not in {r["kind"] for r in records}  # first run: no watermark
    # Back after an outage: the feed now starts on the 20th, past the 15th.
    pages[yt.CH1] = page(20)
    records, _ = fetch.collect(config, state, fetch.Http(config, state))
    (gap,) = [r for r in records if r["kind"] == "feed_gap"]
    assert (gap["channel_id"], gap["from"], gap["to"], gap["reason"]) == (
        yt.CH1,
        "2026-09-15T10:00:00+00:00",
        "2026-09-20T10:00:00+00:00",
        "source_cap",
    )
    # A feed that reaches back to the newest seen upload lost nothing.
    pages[yt.CH1] = page(15)
    records, _ = fetch.collect(config, state, fetch.Http(config, state))
    assert "feed_gap" not in {r["kind"] for r in records}
