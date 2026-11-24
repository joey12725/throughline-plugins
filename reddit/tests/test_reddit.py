"""Plugin mapping, cursors and the SDK contract kit, on synthetic data only."""

import gzip
import json
from collections import Counter

import pytest
from throughline_sdk import Checkpoint, Record
from throughline_sdk.contract import check_plugin
from throughline_reddit import Reddit, mapping

CANARY = "invented-reddit-secret-canary"
RESERVED = {
    "id",
    "channel_id",
    "channel",
    "external_id",
    "version",
    "thread_id",
    "happened_at",
    "content",
    "fields",
    "lat",
    "lon",
    "person_id",
    "score",
    "kind",
    "record_kind",
    "images",
    "author_handle",
    "display_name",
    "is_reference",
    "is_outside",
}


def make(monkeypatch, **env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Reddit()


def collect(plugin, cursor=None, mode="sync"):
    out = list(plugin.records(cursor or {}, mode))
    messages = [x for x in out if isinstance(x, dict)]
    links = [x.data for x in out if isinstance(x, Record)]
    checkpoints = [x for x in out if isinstance(x, Checkpoint)]
    return messages, links, checkpoints


def by_id(messages):
    return {(m["channel"], m["external_id"]): m for m in messages}


def test_rd_plg_1_contract(export_dir, monkeypatch):
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", CANARY)
    result = check_plugin(
        "throughline_reddit:Reddit",
        {
            "REDDIT_SOURCE": f"export:{export_dir}",
            "REDDIT_BATCH_SIZE": "3",
            "REDDIT_CLIENT_SECRET": CANARY,
        },
        secrets=[CANARY],
    )
    assert result["messages"] == 8


def test_rd_plg_2_manifest():
    manifest = json.loads(Reddit.manifest.model_dump_json())
    names = [c["name"] for c in manifest["channels"]]
    assert names == [
        "reddit.saved",
        "reddit.votes",
        "reddit.posts",
        "reddit.comments",
    ]
    for channel in manifest["channels"]:
        fields = {f["name"] for f in channel["fields"]}
        assert not fields & RESERVED
        assert all(f["description"] for f in channel["fields"])
        assert not channel["is_outside"]
    assert all(d["default"] is None for d in manifest["secrets"].values())
    assert all(not d["required"] for d in manifest["secrets"].values())


def test_rd_plg_3_mapping(export_dir, monkeypatch, synthetic):
    s = synthetic
    plugin = make(monkeypatch, REDDIT_SOURCE=f"export:{export_dir}")
    messages, links, checkpoints = collect(plugin)
    assert Counter(m["channel"] for m in messages) == {
        "reddit.saved": 3,
        "reddit.votes": 1,
        "reddit.posts": 1,
        "reddit.comments": 3,
    }
    times = [m["happened_at"] for m in messages]
    assert times == sorted(times)
    rows = by_id(messages)

    saved = rows[("reddit.saved", "t3_aa01")]
    assert saved["happened_at"] == "2025-03-01T12:00:00+00:00"
    assert saved["subject"] == {"start": "2025-02-10T10:00:00+00:00"}
    assert saved["content"] == (
        "Saved post in r/InventedGardening by u/invented_author_a: Invented raised bed question"
        "\n\nWhich invented soil mix?"
    )
    assert "author" not in saved
    assert saved["thread"] == {"external_id": "saved", "title": "Saved on Reddit"}
    fields = saved["fields"]
    assert (
        fields["reddit_score"] == 10
    )  # as first captured; the later score-only change is ignored
    assert fields["time_precision"] == "first_seen"
    assert (
        fields["permalink"]
        == "https://www.reddit.com/r/InventedGardening/comments/aa01/invented_slug/"
    )
    assert fields["created_at"] == "2025-02-10T10:00:00+00:00"
    assert fields["mapping_revision"] == mapping.MAPPING_REVISION
    assert fields["account"] == s.OWNER and fields["source"] == "oauth"
    assert set(fields) == set(mapping.FIELDS)
    assert saved["payload"]["item"]["data"]["score"] == 10

    saved_comment = rows[("reddit.saved", "t1_cc01")]
    assert saved_comment["content"] == (
        'Saved comment by u/invented_author_b in r/InventedCooking on "Invented bread thread":'
        "\n\nUse an invented starter."
    )
    assert saved_comment["fields"]["item_type"] == "comment"
    assert saved_comment["fields"]["post_fullname"] == "t3_aa02"

    vote = rows[("reddit.votes", "t3_aa03")]
    assert vote["fields"]["vote"] == -1 and vote["fields"]["listing"] == "downvoted"
    assert (
        vote["happened_at"] == "2025-03-01T12:00:00+00:00"
    )  # first seen, as an upvote
    assert vote["content"] == (
        "Downvoted post in r/InventedCooking by u/invented_author_c: Invented recipe link"
        "\n\nhttps://example.invalid/invented-recipe"
    )
    assert vote["fields"]["link_url"] == "https://example.invalid/invented-recipe"

    own = rows[("reddit.posts", "t3_aa04")]
    assert own["happened_at"] == "2025-02-25T15:00:00+00:00"
    assert (
        own["content"]
        == "Posted in r/InventedGardening: Invented compost update\n\nFirst invented paragraph, edited."
    )
    assert own["fields"]["edited_at"] == "2025-03-01T08:00:00+00:00"
    assert own["author"] == {
        "system": "reddit",
        "kind": "username",
        "value": s.OWNER,
        "display_name": f"u/{s.OWNER}",
    }
    assert "subject" not in own
    assert own["thread"] == {
        "external_id": "r/inventedgardening",
        "title": "r/InventedGardening",
    }

    reply = rows[("reddit.comments", "t1_cc02")]
    assert (
        reply["content"]
        == 'Commented in r/InventedGardening on "Invented raised bed question":\n\nInvented reply one.'
    )
    assert reply["thread"] == {
        "external_id": "post:t3_aa01",
        "title": "Invented raised bed question",
    }
    assert reply["fields"]["time_precision"] == "source"

    assert all(link["method"] == "source" for link in links)
    saved_links = [
        x
        for x in links
        if x["from"] == {"channel": "reddit.saved", "external_id": "t1_cc01"}
    ]
    assert saved_links == [
        {
            "type": "saved",
            "from": {"channel": "reddit.saved", "external_id": "t1_cc01"},
            "to": {
                "kind": "comment",
                "external_ref": "reddit:t1_cc01",
                "name": "Comment by u/invented_author_b on Invented bread thread",
            },
            "method": "source",
        }
    ]
    entity = saved_links[0]["to"]
    assert {
        "type": "reply_to",
        "from": entity,
        "method": "source",
        "to": {
            "kind": "post",
            "external_ref": "reddit:t3_aa02",
            "name": "Invented bread thread",
        },
    } in links
    assert {
        "type": "posted_in",
        "from": entity,
        "method": "source",
        "to": {
            "kind": "community",
            "external_ref": "reddit:r/inventedcooking",
            "name": "r/InventedCooking",
        },
    } in links
    represents = {
        x["type"]
        for x in links
        if x["from"].get("channel") == "reddit.posts"
    }
    assert represents == {"represents"}
    assert checkpoints[-1].cursor["mapping_revision"] == mapping.MAPPING_REVISION


def test_rd_plg_4_incremental_and_idempotent(export_dir, monkeypatch):
    first_file = sorted(export_dir.glob("*.jsonl"))[0]
    second_file = sorted(export_dir.glob("*.jsonl"))[1]
    plugin = make(monkeypatch, REDDIT_SOURCE=f"export:{first_file}")
    messages, _, checkpoints = collect(plugin)
    assert len(messages) == 6
    cursor = checkpoints[-1].cursor

    # Only the second export, with the first one's cursor: first-seen times come from the cursor.
    plugin.settings["source"] = f"export:{second_file}"
    later, _, checkpoints = collect(plugin, cursor)
    assert sorted((m["channel"], m["external_id"]) for m in later) == [
        ("reddit.comments", "t1_cc04"),
        ("reddit.posts", "t3_aa04"),
        ("reddit.saved", "t3_aa05"),
        ("reddit.votes", "t3_aa03"),
    ]
    assert (
        by_id(later)[("reddit.votes", "t3_aa03")]["happened_at"]
        == "2025-03-01T12:00:00+00:00"
    )
    cursor = checkpoints[-1].cursor

    plugin.settings["source"] = f"export:{export_dir}"
    assert collect(plugin, cursor)[0] == []
    full, _, _ = collect(plugin, {})
    assert full == collect(plugin, {})[0]
    assert (
        by_id(full)[("reddit.votes", "t3_aa03")]
        == by_id(later)[("reddit.votes", "t3_aa03")]
    )
    assert collect(plugin, cursor, "backfill")[0] == []


def test_rd_plg_5_mapping_revision_reemits_keeping_first_seen(export_dir, monkeypatch):
    plugin = make(monkeypatch, REDDIT_SOURCE=f"export:{export_dir}")
    before, _, checkpoints = collect(plugin)
    monkeypatch.setattr(mapping, "MAPPING_REVISION", mapping.MAPPING_REVISION + 1)
    after, _, _ = collect(plugin, checkpoints[-1].cursor)
    assert len(after) == len(before)
    for old, new in zip(before, after):
        assert new["happened_at"] == old["happened_at"]
        assert (
            new["fields"]["mapping_revision"] == old["fields"]["mapping_revision"] + 1
        )


def test_rd_plg_6_checkpoints_resume_after_crash(export_dir, monkeypatch):
    plugin = make(
        monkeypatch, REDDIT_SOURCE=f"export:{export_dir}", REDDIT_BATCH_SIZE="5"
    )
    out = list(plugin.records({}, "backfill"))
    messages = [x for x in out if isinstance(x, dict)]
    checkpoints = [i for i, x in enumerate(out) if isinstance(x, Checkpoint)]
    assert len(checkpoints) == 2
    for index in checkpoints[:-1]:
        done = sum(isinstance(x, dict) for x in out[:index])
        resumed, _, _ = collect(plugin, out[index].cursor, "backfill")
        assert resumed == messages[done:]


def test_rd_plg_7_live_mode_with_fake_reddit(
    tmp_path, monkeypatch, fake_reddit, synthetic
):
    s = synthetic
    fake_reddit.listings["saved"] = [
        s.post(
            "lv01",
            s.GARDEN,
            "Invented live save",
            "invented_author_a",
            "2025-01-01T10:00:00",
        )
    ]
    fake_reddit.listings["submitted"] = [
        s.post("lv02", s.GARDEN, "Invented live post", s.OWNER, "2025-01-02T10:00:00")
    ]
    archive = tmp_path / "archive"
    plugin = make(
        monkeypatch,
        REDDIT_SOURCE="live",
        REDDIT_USER_AGENT="test:throughline-reddit:0.1.0 (by /u/invented_owner)",
        REDDIT_LISTINGS="saved,submitted",
        REDDIT_API_BASE=fake_reddit.url,
        REDDIT_AUTH_BASE=fake_reddit.url,
        REDDIT_MIN_INTERVAL="0",
        REDDIT_CLIENT_ID=s.CLIENT_ID,
        REDDIT_CLIENT_SECRET=s.CLIENT_SECRET,
        REDDIT_REFRESH_TOKEN=s.REFRESH_TOKEN,
        REDDIT_BATCH_SIZE="1",
        REDDIT_ARCHIVE_DIR=str(archive),
    )
    messages, _, checkpoints = collect(plugin)
    assert [(m["channel"], m["external_id"]) for m in messages] == [
        ("reddit.posts", "t3_lv02"),
        ("reddit.saved", "t3_lv01"),
    ]
    saved = messages[-1]
    assert saved["fields"]["time_precision"] == "first_seen"
    # The fetch cursor advances only in the final checkpoint; earlier ones
    # carry just the per-day request count.
    assert all(set(c.cursor["fetch"]) == {"requests"} for c in checkpoints[:-1])
    assert checkpoints[0].cursor["fetch"]["requests"]["count"] > 0
    final = checkpoints[-1].cursor
    assert final["fetch"]["listings"]["saved"]["head"] == ["t3_lv01"]
    text = json.dumps(final) + "".join(p.read_text() for p in archive.glob("*.jsonl"))
    assert (
        s.CLIENT_SECRET not in text
        and s.REFRESH_TOKEN not in text
        and "invented-access-token" not in text
    )
    assert len(list(archive.glob("*.jsonl"))) == 1

    again, _, checkpoints = collect(plugin, final)
    assert again == []
    assert checkpoints[-1].cursor["items"] == final["items"]
    assert len(list(archive.glob("*.jsonl"))) == 2


def test_rd_plg_8_configuration_errors(tmp_path, monkeypatch):
    plugin = make(monkeypatch, REDDIT_SOURCE="somewhere")
    with pytest.raises(ValueError, match="REDDIT_SOURCE"):
        collect(plugin)
    plugin.settings["source"] = f"export:{tmp_path / 'missing'}"
    with pytest.raises(ValueError, match="does not exist"):
        collect(plugin)
    broken = tmp_path / "broken.jsonl"
    broken.write_text(
        '{"kind": "reddit.item"}\n{"invented secret-looking text\n', encoding="utf-8"
    )
    plugin.settings["source"] = f"export:{broken}"
    with pytest.raises(ValueError) as caught:
        collect(plugin)
    assert str(caught.value) == "invalid JSON in export file broken.jsonl line 2"
    plugin.settings.update(source="live")
    with pytest.raises(RuntimeError, match="REDDIT_USER_AGENT"):
        collect(plugin)
    plugin.settings["batch_size"] = "0"
    with pytest.raises(ValueError, match="REDDIT_BATCH_SIZE"):
        collect(plugin)


def test_rd_plg_9_unusable_records_are_skipped(tmp_path, monkeypatch, synthetic):
    s = synthetic
    good = s.export_one()[0]
    rows = [
        {"kind": "reddit.export_summary"},
        {
            "kind": "reddit.item",
            "listing": "history",
            "id": "t3_zz01",
            "observed_at": "2025-01-01T00:00:00Z",
            "raw": {},
        },
        dict(good, id="not-a-fullname"),
        dict(good, observed_at=None),
        dict(good, raw={"kind": "t5", "data": {}}),
        good,
    ]
    s.write_jsonl(tmp_path / "x.jsonl", rows)
    plugin = make(monkeypatch, REDDIT_SOURCE=f"export:{tmp_path / 'x.jsonl'}")
    messages, _, _ = collect(plugin)
    assert [m["external_id"] for m in messages] == ["t3_aa01"]


def test_rd_plg_10_failed_live_fetch_keeps_request_count(
    monkeypatch, fake_reddit, synthetic
):
    s = synthetic
    fake_reddit.listings["saved"] = []
    fake_reddit.failures[f"/user/{s.OWNER}/saved"] = [(503, {})] * 5
    plugin = make(
        monkeypatch,
        REDDIT_SOURCE="live",
        REDDIT_USER_AGENT="test:throughline-reddit:0.1.0 (by /u/invented_owner)",
        REDDIT_LISTINGS="saved",
        REDDIT_API_BASE=fake_reddit.url,
        REDDIT_AUTH_BASE=fake_reddit.url,
        REDDIT_MIN_INTERVAL="0",
        REDDIT_RETRIES="0",
        REDDIT_CLIENT_ID=s.CLIENT_ID,
        REDDIT_CLIENT_SECRET=s.CLIENT_SECRET,
        REDDIT_REFRESH_TOKEN=s.REFRESH_TOKEN,
    )
    cursor = {"items": {}, "fetch": {"listings": {"saved": {"head": ["t3_old"]}}}}
    out = []
    with pytest.raises(RuntimeError, match="Reddit fetch failed"):
        for item in plugin.records(cursor, "sync"):
            out.append(item)
    assert len(out) == 1 and isinstance(out[0], Checkpoint)
    kept = out[0].cursor["fetch"]
    assert kept["listings"] == cursor["fetch"]["listings"]
    assert kept["requests"]["count"] == 3
    assert s.REFRESH_TOKEN not in json.dumps(out[0].cursor)


def test_rd_plg_11_capped_windows_become_source_cap_gaps():
    window = {"from": "2025-03-01T00:00:00+00:00", "to": "2025-03-05T00:00:00+00:00"}
    reports = Reddit.coverage_reports(
        {
            "listings": {
                "saved": {"end": "max_pages", "gap": window},
                "comments": {"end": "end"},
            },
        }
    )
    assert [(r.data["channel"], r.data["scope_key"], r.data["gaps"]) for r in reports] == [
        ("reddit.saved", "saved", [dict(window, reason="source_cap")]),
    ]
    assert all(r.data["reasons"] == ["source_cap"] for r in reports)


def test_rd_plg_12_gzip_exports_report_remote_fetch_gaps(
    tmp_path, monkeypatch, synthetic
):
    window = {
        "from": "2025-03-01T00:00:00+00:00",
        "to": "2025-03-02T00:00:00+00:00",
    }
    rows = synthetic.export_one()[:1] + [
        {
            "kind": "reddit.export_summary",
            "listings": {"comments": {"end": "max_pages", "gap": window}},
        }
    ]
    target = tmp_path / "remote.jsonl.gz"
    with gzip.open(target, "wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    messages, records, _ = collect(make(monkeypatch, REDDIT_SOURCE=f"export:{target}"))
    assert len(messages) == 1
    reports = [row for row in records if row.get("scope_key")]
    assert reports[0]["gaps"] == [dict(window, reason="source_cap")]
