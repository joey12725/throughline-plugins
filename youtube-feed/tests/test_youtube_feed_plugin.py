import json
import subprocess
from urllib.parse import parse_qs

import pytest
from throughline_sdk import Checkpoint, Record
from throughline_sdk.contract import check_plugin

from throughline_youtube_feed import YouTubeFeed, fetch, mapping

CANARY = "invented-secret-canary-youtube-feed"


def rss(yt, n, channel, published, fetched, title=None, details=None):
    record = {
        "kind": "upload",
        "id": yt.vid(n),
        "channel_id": channel,
        "transport": "rss",
        "fetched_at": fetched,
        "raw": {
            "entry_id": f"yt:video:{yt.vid(n)}",
            "video_id": yt.vid(n),
            "channel_id": channel,
            "title": title or f"Invented upload {n}",
            "link": f"https://www.youtube.com/watch?v={yt.vid(n)}",
            "author": {
                "name": "Invented One" if channel == yt.CH1 else "Invented Two",
                "uri": None,
            },
            "published": published,
            "media": {
                "title": title or f"Invented upload {n}",
                "description": f"Invented description {n}",
            },
        },
        "observed": {"views": n * 10, "updated": fetched},
    }
    if details:
        record["details"] = details
    return record


def router_pick(yt, n, verdict, scored, published):
    return {
        "kind": "pick",
        "id": f"{yt.vid(n)}:invented-model",
        "video_id": yt.vid(n),
        "scorer": "invented-model",
        "origin": "router",
        "raw": {"content": "{}", "usage": {"total_tokens": 10}},
        "decision": {
            "watch_probability": 0.9 if verdict == "pick" else 0.2,
            "progress_if_watched": 80.0,
            "junk_food_probability": 0.1,
            "watch_later_probability": 0.5,
            "is_junk_food": False,
            "verdict": verdict,
            "reason": "Invented reason",
            "scored_at": scored,
            "thresholds": {"min_watch": 0.7, "min_progress": 50.0, "max_junk": 0.5},
            "video": {
                "video_id": yt.vid(n),
                "channel_id": yt.CH1,
                "channel_title": "Invented One",
                "title": f"Invented upload {n}",
                "url": f"https://www.youtube.com/watch?v={yt.vid(n)}",
                "published_at": published,
                "duration_s": 754,
            },
            "model": "invented-model",
            "prompt_sha": "0" * 16,
        },
    }


def write_jsonl(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records), "utf-8")


def export_dir(yt, root):
    """Invented export directory: two fetch runs plus youtube-notify files."""
    root.mkdir(parents=True, exist_ok=True)
    first = "2026-09-25T12:00:00+00:00"
    second = "2026-09-26T12:00:00+00:00"
    write_jsonl(
        root / "feed-1.jsonl",
        [
            rss(yt, 1, yt.CH1, "2026-09-24T10:00:00+00:00", first),
            rss(yt, 2, yt.CH1, "2026-09-23T10:00:00+00:00", first),
            rss(yt, 3, yt.CH1, "2026-09-22T10:00:00+00:00", first),
            rss(yt, 4, yt.CH2, "2026-09-24T15:00:00+00:00", first),
            rss(yt, 5, yt.CH2, "2026-09-21T15:00:00+00:00", first),
            {
                "kind": "feed_error",
                "id": yt.CH3,
                "channel_id": yt.CH3,
                "error": "HTTP 404",
                "status": 404,
            },
            {"kind": "run", "id": first, "stats": {}},
        ],
    )
    write_jsonl(
        root / "feed-2.jsonl",
        [
            rss(
                yt,
                1,
                yt.CH1,
                "2026-09-24T10:00:00+00:00",
                second,
                details={
                    "length_seconds": 754,
                    "description": "Long invented",
                    "publish_date": "2026-09-24",
                },
            ),
            rss(yt, 2, yt.CH1, "2026-09-23T10:00:00+00:00", second),
            {
                "kind": "upload",
                "id": yt.vid(6),
                "channel_id": yt.CH2,
                "transport": "page",
                "fetched_at": second,
                "estimated_published_at": "2026-09-26T09:00:00+00:00",
                "raw": {
                    "video_id": yt.vid(6),
                    "title": "Invented paged",
                    "length_text": "3:05",
                    "short": False,
                    "live": False,
                    "channel_title": "Invented Two",
                },
                "observed": {"published_text": "3 hours ago"},
            },
            router_pick(
                yt, 1, "pick", "2026-09-26T12:05:00+00:00", "2026-09-24T10:00:00+00:00"
            ),
            router_pick(
                yt, 4, "skip", "2026-09-26T12:06:00+00:00", "2026-09-24T15:00:00+00:00"
            ),
            {
                "kind": "playlist_add",
                "id": f"{yt.vid(1)}:PLinventedpicks0001",
                "video_id": yt.vid(1),
                "pick_id": f"{yt.vid(1)}:invented-model",
                "playlist_id": "PLinventedpicks0001",
                "playlist": "Feed Picks",
                "added_at": "2026-09-26T12:07:00+00:00",
                "item_id": "invented-item",
            },
        ],
    )
    notify = root / "notify"
    notify.mkdir(exist_ok=True)
    write_jsonl(
        notify / "predictions.jsonl",
        [
            yt.prediction(
                7,
                scored="2026-09-19T10:00:00+00:00",
                published="2026-09-19T08:00:00+00:00",
            ),
            yt.prediction(
                2,
                scored="2026-09-23T11:00:00+00:00",
                published="2026-09-23T10:00:00+00:00",
                watch=0.3,
            ),
        ],
    )
    (notify / "flagged.json").write_text(
        json.dumps(
            [
                yt.prediction(
                    7,
                    scored="2026-09-19T10:00:00+00:00",
                    published="2026-09-19T08:00:00+00:00",
                )
            ]
        ),
        "utf-8",
    )
    return root


def run_records(plugin, cursor=None):
    messages, links, checkpoints = [], [], []
    for item in plugin.records(cursor or {}, "backfill"):
        if isinstance(item, Checkpoint):
            checkpoints.append(item.cursor)
        elif isinstance(item, Record):
            links.append(item.data)
        else:
            messages.append(item)
    return messages, links, checkpoints[-1]


@pytest.fixture
def exported(yt, tmp_path, monkeypatch):
    root = export_dir(yt, tmp_path / "export")
    monkeypatch.setenv("YOUTUBE_FEED_SOURCE", f"export:{root}")
    monkeypatch.setenv("YOUTUBE_FEED_IMPORT_SCORER", "jev")
    return root


def test_plg_6_contract(yt, exported):
    try:
        result = check_plugin(
            "throughline_youtube_feed:YouTubeFeed",
            {
                "YOUTUBE_FEED_SOURCE": f"export:{exported}",
                "YOUTUBE_FEED_IMPORT_SCORER": "jev",
                "YOUTUBE_FEED_BATCH": "2",
                "YOUTUBE_FEED_ROUTER_API_KEY": CANARY,
            },
            secrets=[CANARY],
        )
    except subprocess.CalledProcessError as error:
        stderr = (error.stderr or b"").decode(errors="replace")
        pytest.fail("Contract subprocess failed: " + stderr[-2000:], pytrace=False)
    assert result["messages"] == 11


def test_msg_1_export_mapping(yt, exported):
    messages, links, cursor = run_records(YouTubeFeed())
    by_id = {(m["channel"], m["external_id"]): m for m in messages}
    assert len(messages) == 11
    one = by_id[(mapping.UPLOADS, yt.vid(1))]
    # The detailed record wins; the RSS timestamp stays exact.
    assert one["happened_at"] == "2026-09-24T10:00:00+00:00"
    assert (
        one["fields"]["duration_s"] == 754
        and one["fields"]["time_precision"] == "exact"
    )
    assert one["content"].startswith("Invented One uploaded Invented upload 1 (12:34)")
    assert one["thread"] == {
        "external_id": "channel:" + yt.CH1,
        "title": "Invented One",
    }
    assert one["author"] == {
        "system": "youtube",
        "kind": "channel_id",
        "value": yt.CH1,
        "display_name": "Invented One",
    }
    assert one["fields"]["mapping_revision"] == mapping.MAPPING_REVISION
    assert "observed" not in one["payload"] and "fetched_at" not in one["payload"]
    paged = by_id[(mapping.UPLOADS, yt.vid(6))]
    assert (
        paged["fields"]["time_precision"] == "approximate"
        and paged["fields"]["duration_s"] == 185
    )
    synth = by_id[(mapping.UPLOADS, yt.vid(7))]
    assert (
        synth["fields"]["transport"] == "prediction"
        and synth["fields"]["duration_s"] == 607
    )
    assert synth["happened_at"] == "2026-09-19T08:00:00+00:00"
    pick = by_id[(mapping.PICKS, yt.vid(1) + ":invented-model")]
    assert pick["happened_at"] == "2026-09-26T12:05:00+00:00"
    assert pick["subject"] == {"start": "2026-09-24T10:00:00+00:00"}
    assert pick["fields"]["verdict"] == "pick" and pick["fields"]["picked"] is True
    assert pick["fields"]["playlist_id"] == "PLinventedpicks0001"
    assert pick["content"].startswith(
        "Picked Invented upload 1 by Invented One: watch 90%, progress 80%, junk 10%. Invented reason"
    )
    assert pick["thread"]["external_id"] == "scorer:invented-model"
    jev7 = by_id[(mapping.PICKS, yt.vid(7) + ":jev")]
    assert (
        jev7["fields"]["verdict"] == "pick"
        and jev7["fields"]["origin"] == "predictions"
    )
    assert len(jev7["payload"]["sources"]) == 2  # predictions line + flagged entry
    jev2 = by_id[(mapping.PICKS, yt.vid(2) + ":jev")]
    assert jev2["fields"]["verdict"] == "skip"
    pick_links = [x for x in links if x["type"] == "pick_of"]
    assert len(pick_links) == 4
    assert {
        "type": "pick_of",
        "from": {
            "channel": mapping.PICKS,
            "external_id": yt.vid(4) + ":invented-model",
        },
        "to": {"channel": mapping.UPLOADS, "external_id": yt.vid(4)},
        "method": "source",
    } in links
    assert sum(x["type"] == "upload_of" for x in links) == 7
    assert sum(x["type"] == "published_by" for x in links) == 7
    # Uploads precede picks so every pick_of target already exists.
    channels = [m["channel"] for m in messages]
    assert channels == sorted(channels, key=lambda c: c != mapping.UPLOADS)
    assert set(cursor["uploads"]) == {yt.vid(n) for n in range(1, 8)}


def test_msg_2_idempotent_rerun_and_reset(yt, exported):
    first, _, cursor = run_records(YouTubeFeed())
    again, links, _ = run_records(YouTubeFeed(), cursor)
    assert again == [] and links == []
    reset, _, _ = run_records(YouTubeFeed(), {})
    assert reset == first


def test_msg_3_richer_records_replace_poorer_never_the_reverse(
    yt, tmp_path, monkeypatch
):
    root = tmp_path / "inc"
    root.mkdir()
    monkeypatch.setenv("YOUTUBE_FEED_SOURCE", f"export:{root}")
    write_jsonl(
        root / "a.jsonl",
        [rss(yt, 1, yt.CH1, "2026-09-24T10:00:00+00:00", "2026-09-25T00:00:00+00:00")],
    )
    first, _, cursor = run_records(YouTubeFeed())
    assert first[0]["fields"]["duration_s"] is None
    write_jsonl(
        root / "b.jsonl",
        [
            rss(
                yt,
                1,
                yt.CH1,
                "2026-09-24T10:00:00+00:00",
                "2026-09-26T00:00:00+00:00",
                details={"length_seconds": 90},
            )
        ],
    )
    second, _, cursor = run_records(YouTubeFeed(), cursor)
    assert [m["fields"]["duration_s"] for m in second] == [90]
    # A later, poorer record (no details) does not undo it.
    write_jsonl(
        root / "c.jsonl",
        [rss(yt, 1, yt.CH1, "2026-09-24T10:00:00+00:00", "2026-09-27T00:00:00+00:00")],
    )
    assert run_records(YouTubeFeed(), cursor)[0] == []
    # A real edit at the same completeness is a new version.
    write_jsonl(
        root / "d.jsonl",
        [
            rss(
                yt,
                1,
                yt.CH1,
                "2026-09-24T10:00:00+00:00",
                "2026-09-28T00:00:00+00:00",
                title="Edited",
                details={"length_seconds": 90},
            )
        ],
    )
    edited, _, _ = run_records(YouTubeFeed(), cursor)
    assert [m["fields"]["title"] for m in edited] == ["Edited"]


def test_plg_4_bounds(yt, exported, monkeypatch):
    monkeypatch.setenv("YOUTUBE_FEED_SINCE", "2026-09-23")
    monkeypatch.setenv("YOUTUBE_FEED_UNTIL", "2026-09-25T00:00:00+00:00")
    messages, _, _ = run_records(YouTubeFeed())
    got = sorted((m["channel"], m["external_id"]) for m in messages)
    assert got == sorted(
        [
            (mapping.UPLOADS, yt.vid(1)),
            (mapping.UPLOADS, yt.vid(2)),
            (mapping.UPLOADS, yt.vid(4)),
            (mapping.PICKS, yt.vid(2) + ":jev"),
        ]
    )
    monkeypatch.setenv("YOUTUBE_FEED_SINCE", "yesterday")
    with pytest.raises(ValueError):
        YouTubeFeed()


def test_plg_2_live_mode_fetches_routes_and_checkpoints(
    yt, server, monkeypatch, tmp_path
):
    feed = yt.feed_xml(
        yt.CH1,
        "Invented One",
        [
            {"n": 1, "title": "Fresh", "published": "2026-10-01T10:00:00+00:00"},
            {"n": 2, "title": "Older", "published": "2026-09-01T10:00:00+00:00"},
        ],
    )

    def feeds(request):
        assert parse_qs(request.query)["channel_id"] == [yt.CH1]
        return 200, {}, feed

    server.routes[("GET", "/feeds/videos.xml")] = feeds

    def chat(request):
        body = json.loads(request.body)
        assert request.headers["Authorization"] == "Bearer " + CANARY
        return (
            200,
            {},
            {
                "choices": [
                    {
                        "message": {
                            "content": '{"watch_probability": 0.8, "progress_if_watched": 90, "junk_food_probability": 0.05, "reason": "Invented"}'
                        }
                    }
                ],
                "model": body["model"],
            },
        )

    server.routes[("POST", "/v1/chat/completions")] = chat
    channels = tmp_path / "channels.txt"
    channels.write_text(yt.CH1 + "\n", "utf-8")
    archive = tmp_path / "archive.jsonl"
    for name, value in {
        "SOURCE": "live",
        "CHANNELS": str(channels),
        "RSS_URL": server.url + "/feeds/videos.xml?channel_id={channel_id}",
        "DELAY_S": "0",
        "ROUTER": "1",
        "ROUTER_URL": server.url + "/v1",
        "ROUTER_MODEL": "invented-model",
        "ROUTER_API_KEY": CANARY,
        "ARCHIVE": str(archive),
    }.items():
        monkeypatch.setenv("YOUTUBE_FEED_" + name, value)
    plugin = YouTubeFeed()
    plugin.dry_run = True
    messages, _, _ = run_records(plugin)
    assert [m["channel"] for m in messages] == [mapping.UPLOADS, mapping.UPLOADS]
    assert server.hits("POST") == [] and not archive.exists()  # dry run never pays
    plugin = YouTubeFeed()
    messages, links, cursor = run_records(plugin)
    assert [(m["channel"], m["external_id"]) for m in messages] == [
        (mapping.UPLOADS, yt.vid(2)),
        (mapping.UPLOADS, yt.vid(1)),
        (mapping.PICKS, yt.vid(1) + ":invented-model"),
    ]
    assert messages[2]["fields"]["verdict"] == "pick"
    assert {
        "type": "pick_of",
        "from": {
            "channel": mapping.PICKS,
            "external_id": yt.vid(1) + ":invented-model",
        },
        "to": {"channel": mapping.UPLOADS, "external_id": yt.vid(1)},
        "method": "source",
    } in links
    assert len(server.hits("POST")) == 1  # the old upload is not scored
    assert cursor["fetch"]["scored"][yt.vid(1)]["verdict"] == "pick"
    assert plugin.stats["scored"] == 1
    assert CANARY not in json.dumps([messages, links, cursor]) + archive.read_text()
    assert [json.loads(x)["kind"] for x in archive.read_text().splitlines()] == [
        "upload",
        "upload",
        "pick",
    ]
    # Next poll: nothing new, nothing paid.
    again, _, _ = run_records(YouTubeFeed(), cursor)
    assert again == [] and len(server.hits("POST")) == 1


def test_mid_run_checkpoints_keep_previous_fetch_state(yt, exported, monkeypatch):
    monkeypatch.setenv("YOUTUBE_FEED_BATCH", "3")
    checkpoints = [
        i.cursor
        for i in YouTubeFeed().records({"fetch": {"seen": {"x": {}}}}, "sync")
        if isinstance(i, Checkpoint)
    ]
    assert len(checkpoints) == 4
    assert all(c["fetch"] == {"seen": {"x": {}}} for c in checkpoints)
    assert len(checkpoints[0]["uploads"]) == 3


def test_source_setting_is_validated(monkeypatch):
    monkeypatch.setenv("YOUTUBE_FEED_SOURCE", "somewhere")
    with pytest.raises(ValueError):
        YouTubeFeed()
    monkeypatch.setenv("YOUTUBE_FEED_SOURCE", "live")
    with pytest.raises(ValueError, match="CHANNELS"):
        run_records(YouTubeFeed())
    monkeypatch.setenv("YOUTUBE_FEED_SOURCE", "export:does-not-exist")
    with pytest.raises(ValueError, match="does not exist"):
        run_records(YouTubeFeed())


def test_fetch_and_plugin_share_setting_names():
    declared = {d.env for d in YouTubeFeed.manifest.settings.values()} | {
        d.env for d in YouTubeFeed.manifest.secrets.values()
    }
    assert {
        fetch.PREFIX + k.upper() for k in [*fetch.DEFAULTS, *fetch.SECRETS]
    } <= declared
    assert all(d.default is None for d in YouTubeFeed.manifest.secrets.values())


def test_live_feed_errors_fail_the_run_or_report_partial(
    yt, server, monkeypatch, tmp_path
):
    ok = yt.feed_xml(
        yt.CH1,
        "Invented One",
        [{"n": 1, "title": "Fresh", "published": "2026-10-01T10:00:00+00:00"}],
    )
    pages = {yt.CH1: ok}

    def feeds(request):
        cid = parse_qs(request.query)["channel_id"][0]
        return (200, {}, pages[cid]) if cid in pages else (404, {}, "gone")

    server.routes[("GET", "/feeds/videos.xml")] = feeds
    for name, value in {
        "SOURCE": "live",
        "CHANNEL_IDS": f"{yt.CH1},{yt.CH2}",
        "RSS_URL": server.url + "/feeds/videos.xml?channel_id={channel_id}",
        "DELAY_S": "0",
        "BACKOFF_S": "0",
    }.items():
        monkeypatch.setenv("YOUTUBE_FEED_" + name, value)
    messages, records, _ = run_records(YouTubeFeed())
    assert [m["external_id"] for m in messages] == [yt.vid(1)]
    assert [r for r in records if "scope_key" in r] == [
        {
            "channel": mapping.UPLOADS,
            "scope_key": yt.CH2,
            "status": "partial",
            "reasons": ["source_error"],
            "detail": {"status": 404},
        }
    ]
    pages.clear()  # every channel unreachable
    seen = []
    with pytest.raises(fetch.FetchError, match="every channel feed failed"):
        for item in YouTubeFeed().records({}, "backfill"):
            seen.append(item)
    assert isinstance(seen[-1], Checkpoint)
