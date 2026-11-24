"""Mapping, export/live sources, cursors, bounds and the SDK contract kit."""

import copy
import json

import pytest
from throughline_sdk import Checkpoint
from throughline_sdk.contract import check_plugin

from throughline_instagram_feed import InstagramFeed, fetch
from throughline_instagram_feed.mapping import (
    CHANNEL,
    MAPPING_REVISION,
    map_post,
    map_record,
    map_story,
    post_type,
)


def raw_records(server, accounts, **options):
    opts = {
        "base_url": server.url,
        "delay_s": "0",
        "prime": "0",
        **{k: str(v) for k, v in options.items()},
    }
    fetcher = fetch.Fetcher(
        [fetch._account(a) for a in accounts], {}, opts, sleep=lambda s: None
    )
    return [r for _, records in fetcher.run(select_all=True) for r in records]


def post_record(synth, raw, account="fake_venue", config=None):
    return {
        "kind": "post",
        "id": raw.get("shortcode") or raw.get("code"),
        "account": account,
        "fetched_at": "2021-06-01T00:00:00Z",
        "source": "web_profile_info",
        "account_config": config or {},
        "owner": {
            "id": "71001",
            "username": account,
            "full_name": "Invented",
            "is_verified": False,
        },
        "raw": raw,
    }


def write_jsonl(path, records):
    path.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in records), encoding="utf-8"
    )


def collect(plugin, cursor=None, mode="sync"):
    items = list(plugin.records(cursor or {}, mode))
    messages = [i for i in items if isinstance(i, dict)]
    checkpoints = [i.cursor for i in items if isinstance(i, Checkpoint)]
    return messages, checkpoints, items


# ------------------------------------------------------------------ mapping


def test_ig_7_post_types_urls_and_fields(synth):
    n = synth.node
    cases = {
        "image": n("FAKEI1", synth.base_ts),
        "video": n("FAKEV1", synth.base_ts, kind="video"),
        "reel": n("FAKER1", synth.base_ts, kind="reel"),
        "carousel": n("FAKEC1", synth.base_ts, kind="carousel", children=3),
    }
    for expected, raw in cases.items():
        assert post_type(raw) == expected
    reel = map_post(post_record(synth, cases["reel"]))
    assert reel["fields"]["url"] == "https://www.instagram.com/reel/FAKER1/"
    assert reel["fields"]["video_duration_s"] == 30.0
    carousel = map_post(post_record(synth, cases["carousel"]))
    assert carousel["fields"]["url"] == "https://www.instagram.com/p/FAKEC1/"
    assert carousel["fields"]["carousel_count"] == 3
    assert len(carousel["payload"]["node"]["children"]) == 3

    raw = n(
        "FAKEI2",
        synth.base_ts,
        caption="Show tonight #LiveMusic with @Fake_Band. #liveMusic",
        location=("5501", "Invented Hall"),
    )
    message = map_post(
        post_record(
            synth, raw, config={"label": "Invented Venue", "category": "venues/music"}
        )
    )
    assert message["channel"] == CHANNEL and message["external_id"] == "FAKEI2"
    assert message["happened_at"] == "2021-05-03T00:00:00+00:00"
    assert message["thread"] == {
        "external_id": "instagram:user:71001",
        "title": "@fake_venue",
    }
    assert message["author"] == {
        "system": "instagram",
        "kind": "user_id",
        "value": "71001",
        "display_name": "@fake_venue",
    }
    f = message["fields"]
    assert f["hashtags"] == ["livemusic"] and f["mentions"] == ["fake_band"]
    assert f["location_name"] == "Invented Hall" and f["location_id"] == "5501"
    assert (
        f["account_label"] == "Invented Venue"
        and f["account_category"] == "venues/music"
    )
    assert f["mapping_revision"] == MAPPING_REVISION and f["post_type"] == "image"
    assert message["content"].startswith(
        "@fake_venue (Invented Venue) posted an image at Invented Hall:\nShow tonight"
    )
    assert message["content"].endswith("https://www.instagram.com/p/FAKEI2/")
    declared = {d["name"] for d in InstagramFeed.manifest.channels[0].fields}
    assert set(f) == declared


def test_ig_7_v1_item_shape(synth):
    raw = {
        "pk": "3301",
        "code": "FAKEV1ITEM",
        "taken_at": synth.base_ts,
        "media_type": 8,
        "product_type": "carousel_container",
        "caption": {"text": "Item caption"},
        "carousel_media": [{"pk": "1", "media_type": 1}, {"pk": "2", "media_type": 2}],
        "user": {"pk": "71001", "username": "fake_venue", "profile_pic_url": "x"},
    }
    message = map_post(post_record(synth, raw))
    assert message["external_id"] == "FAKEV1ITEM"
    assert message["fields"]["post_type"] == "carousel"
    assert message["fields"]["caption"] == "Item caption"
    assert message["fields"]["carousel_count"] == 2
    assert message["fields"]["owner_id"] == "71001"
    assert message["payload"]["node"]["owner"] == {
        "pk": "71001",
        "username": "fake_venue",
    }


def test_ig_7_no_caption_and_malformed(synth):
    message = map_post(post_record(synth, synth.node("FAKENC", synth.base_ts)))
    assert message["content"] == (
        "@fake_venue posted an image (no caption)\nhttps://www.instagram.com/p/FAKENC/"
    )
    assert map_post({"kind": "profile"}) is None
    broken = synth.node("FAKEBAD", synth.base_ts)
    del broken["taken_at_timestamp"]
    with pytest.raises(ValueError):
        map_post(post_record(synth, broken))


def test_ig_8_volatile_fields_never_version(synth):
    a = synth.node(
        "FAKEVOL", synth.base_ts, kind="carousel", caption="Same", likes=5, sig="a"
    )
    b = synth.node(
        "FAKEVOL", synth.base_ts, kind="carousel", caption="Same", likes=900, sig="zzz"
    )
    b["pinned_for_users"] = [{"id": "1"}]
    rb = post_record(synth, b)
    rb["fetched_at"] = "2021-07-01T00:00:00Z"
    rb["owner"]["full_name"] = "Renamed display"
    assert map_post(post_record(synth, a)) == map_post(rb)
    edited = copy.deepcopy(a)
    edited["edge_media_to_caption"]["edges"][0]["node"]["text"] = "Edited caption"
    assert map_post(post_record(synth, edited)) != map_post(post_record(synth, a))


def test_ig_8_collab_post_maps_identically_through_both_accounts(fake_instagram, synth):
    records = raw_records(fake_instagram, synth.accounts)
    collab = [map_post(r) for r in records if r.get("id") == "FAKECOLLAB1"]
    assert len(collab) == 2 and collab[0] == collab[1]
    assert collab[0]["fields"]["account"] == "fake_venue"
    assert collab[0]["fields"]["coauthors"] == ["fake_records"]


# ------------------------------------------------------------------ export source


@pytest.fixture
def export_dir(tmp_path, fake_instagram, synth):
    directory = tmp_path / "export"
    directory.mkdir()
    records = raw_records(fake_instagram, synth.accounts)
    write_jsonl(directory / "2021-06-01.jsonl", records)
    # A second fetch a day later: same posts with changed counts and CDN URLs.
    again = copy.deepcopy(records)
    for r in again:
        if r["kind"] == "post":
            r["raw"]["edge_liked_by"]["count"] += 100
            r["raw"]["display_url"] += "-refetched"
            r["fetched_at"] = "2021-06-02T00:00:00Z"
    write_jsonl(directory / "2021-06-02.jsonl", again)
    (directory / "notes.txt").write_text("ignored", encoding="utf-8")
    return directory


def test_ig_9_export_directory_dedupes_and_checkpoints(monkeypatch, export_dir):
    monkeypatch.setenv("INSTAGRAM_FEED_SOURCE", f"export:{export_dir}")
    messages, checkpoints, items = collect(InstagramFeed())
    ids = [m["external_id"] for m in messages]
    assert len(ids) == len(set(ids)) == 8
    assert len(checkpoints) >= 4  # Per account and per file.
    final = checkpoints[-1]["export"]
    for name in ("2021-06-01.jsonl", "2021-06-02.jsonl"):
        assert final[name]["offset"] == (export_dir / name).stat().st_size
    assert "notes.txt" not in final
    # Resuming from the final cursor emits nothing.
    again, _, _ = collect(InstagramFeed(), checkpoints[-1])
    assert again == []
    # Resuming from every checkpoint never loses a post.
    for cursor in checkpoints:
        rest, _, _ = collect(InstagramFeed(), cursor)
        before = []
        for item in items:
            if isinstance(item, Checkpoint) and item.cursor == cursor:
                break
            if isinstance(item, dict):
                before.append(item["external_id"])
        assert set(before) | {m["external_id"] for m in rest} == set(ids)


def test_ig_9_export_append_partial_line_and_replacement(monkeypatch, tmp_path, synth):
    path = tmp_path / "feed.jsonl"
    first = post_record(synth, synth.node("FAKEAP1", synth.base_ts, caption="one"))
    second = post_record(
        synth, synth.node("FAKEAP2", synth.base_ts + 10, caption="two")
    )
    write_jsonl(path, [first])
    # A writer is mid-way through the next line: it must not be consumed.
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(second)[:20])
    monkeypatch.setenv("INSTAGRAM_FEED_SOURCE", f"export:{path}")
    messages, checkpoints, _ = collect(InstagramFeed())
    assert [m["external_id"] for m in messages] == ["FAKEAP1"]
    write_jsonl(path, [first, second])
    messages, checkpoints, _ = collect(InstagramFeed(), checkpoints[-1])
    assert [m["external_id"] for m in messages] == ["FAKEAP2"]
    # A replaced file (different first line) is read again from the start.
    write_jsonl(path, [second])
    messages, _, _ = collect(InstagramFeed(), checkpoints[-1])
    assert [m["external_id"] for m in messages] == ["FAKEAP2"]


def test_ig_9_unreadable_lines_and_other_kinds_skipped(monkeypatch, tmp_path, synth):
    path = tmp_path / "feed.jsonl"
    good = post_record(synth, synth.node("FAKEOK", synth.base_ts))
    broken = post_record(synth, synth.node("FAKEBROKEN", synth.base_ts))
    del broken["raw"]["taken_at_timestamp"]
    path.write_text(
        "\n".join(
            [
                "{not json",
                json.dumps({"kind": "status", "id": "x", "account": "fake_venue"}),
                json.dumps(broken),
                json.dumps(good),
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("INSTAGRAM_FEED_SOURCE", f"export:{path}")
    messages, _, _ = collect(InstagramFeed())
    assert [m["external_id"] for m in messages] == ["FAKEOK"]


def test_ig_10_since_until_bounds(monkeypatch, export_dir, synth):
    monkeypatch.setenv("INSTAGRAM_FEED_SOURCE", f"export:{export_dir}")
    monkeypatch.setenv("INSTAGRAM_FEED_SINCE", "2021-05-03T00:05:00Z")
    monkeypatch.setenv("INSTAGRAM_FEED_UNTIL", "2021-05-03T00:30:00+00:00")
    messages, _, _ = collect(InstagramFeed())
    assert sorted(m["external_id"] for m in messages) == [
        "FAKECOLLAB1",
        "FAKEREC01",
        "FAKEREC02",
        "FAKEVEN03",
    ]
    monkeypatch.setenv("INSTAGRAM_FEED_SINCE", "yesterday")
    with pytest.raises(ValueError):
        collect(InstagramFeed())


def test_ig_10_conflicting_versions_split_by_checkpoint(monkeypatch, tmp_path, synth):
    raw = synth.node("FAKEEDIT", synth.base_ts, caption="first")
    edited = copy.deepcopy(raw)
    edited["edge_media_to_caption"]["edges"][0]["node"]["text"] = "second"
    path = tmp_path / "feed.jsonl"
    write_jsonl(path, [post_record(synth, raw), post_record(synth, edited)])
    monkeypatch.setenv("INSTAGRAM_FEED_SOURCE", f"export:{path}")
    _, _, items = collect(InstagramFeed())
    kinds = ["m" if isinstance(i, dict) else "c" for i in items]
    assert kinds == ["m", "c", "m", "c"]
    assert items[2]["fields"]["caption"] == "second"


def test_ig_10_bad_source_setting(monkeypatch):
    monkeypatch.setenv("INSTAGRAM_FEED_SOURCE", "export:")
    with pytest.raises(ValueError):
        collect(InstagramFeed())
    monkeypatch.setenv("INSTAGRAM_FEED_SOURCE", "somewhere")
    with pytest.raises(ValueError):
        collect(InstagramFeed())


# ------------------------------------------------------------------ live source


def live_env(monkeypatch, server, **extra):
    env = {
        "INSTAGRAM_FEED_SOURCE": "live",
        "INSTAGRAM_FEED_BASE_URL": server.url,
        "INSTAGRAM_FEED_ACCOUNTS": "fake_venue,fake_records,fake_gallery",
        "INSTAGRAM_FEED_DELAY_S": "0",
        "INSTAGRAM_FEED_PRIME": "0",
        **extra,
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def test_ig_11_live_rotation_state_in_cursor(monkeypatch, fake_instagram):
    live_env(monkeypatch, fake_instagram, INSTAGRAM_FEED_BATCH="2")
    messages, checkpoints, _ = collect(InstagramFeed())
    assert {m["fields"]["account"] for m in messages} == {"fake_venue", "fake_records"}
    state = checkpoints[-1]["live"]
    assert set(state["accounts"]) == {"fake_venue", "fake_records"}
    assert state["requests"]["count"] == 2
    messages, checkpoints, _ = collect(InstagramFeed(), checkpoints[-1])
    assert {m["fields"]["account"] for m in messages} == {"fake_gallery", "fake_venue"}
    assert checkpoints[-1]["live"]["requests"]["count"] == 4
    assert fake_instagram.profile_calls() == [
        "fake_venue",
        "fake_records",
        "fake_gallery",
        "fake_venue",
    ]


def test_ig_11_live_blocked_persists_cooldown(monkeypatch, fake_instagram):
    fake_instagram.script["fake_venue"] = [(429, {}, {})]
    live_env(monkeypatch, fake_instagram)
    messages, checkpoints, _ = collect(InstagramFeed())
    assert messages == []
    state = checkpoints[-1]["live"]
    assert state["strikes"] == 1 and state["backoff_until"]
    calls = len(fake_instagram.log)
    messages, _, _ = collect(InstagramFeed(), checkpoints[-1])
    assert messages == [] and len(fake_instagram.log) == calls


def coverage_of(items):
    from throughline_sdk import Record

    return [i.data for i in items if isinstance(i, Record) and i.kind == "coverage"]


def test_ig_11_live_all_accounts_failing_fails_the_run(monkeypatch, fake_instagram):
    for handle in ("fake_venue", "fake_records", "fake_gallery"):
        fake_instagram.script[handle] = [(503, {}, {})]
    live_env(monkeypatch, fake_instagram, INSTAGRAM_FEED_RETRIES="1")
    items = []
    with pytest.raises(fetch.FetchError, match="every account fetch failed"):
        for item in InstagramFeed().records({}, "sync"):
            items.append(item)
    assert isinstance(items[-1], Checkpoint)  # state kept before failing
    # One failing account of three: the run stands, the account is reported.
    fake_instagram.script = {"fake_records": [(503, {}, {})]}
    messages, _, items = collect(InstagramFeed())
    assert messages
    assert coverage_of(items) == [
        {
            "channel": CHANNEL,
            "scope_key": "fake_records",
            "status": "partial",
            "reasons": ["source_error"],
            "detail": {"status": "error"},
        }
    ]


def test_ig_11_live_full_page_past_watermark_reports_cap_gap(
    monkeypatch, fake_instagram, synth
):
    ts = synth.base_ts
    old = [synth.node(f"FAKEOLD{i:02d}", ts + i) for i in range(12)]
    fake_instagram.profiles = {"fake_venue": synth.profile("fake_venue", "71001", old)}
    live_env(monkeypatch, fake_instagram, INSTAGRAM_FEED_ACCOUNTS="fake_venue")
    _, checkpoints, items = collect(InstagramFeed())
    assert coverage_of(items) == []  # first run: no watermark, no gap
    # Back after an outage: twelve newer posts, the oldest past the watermark.
    new = [synth.node(f"FAKENEW{i:02d}", ts + 1000 + i) for i in range(12)]
    fake_instagram.profiles = {"fake_venue": synth.profile("fake_venue", "71001", new)}
    _, _, items = collect(InstagramFeed(), checkpoints[-1])
    (report,) = coverage_of(items)
    assert report["scope_key"] == "fake_venue" and report["reasons"] == ["source_cap"]
    assert report["gaps"] == [
        {
            "from": "2021-05-03T00:00:11+00:00",
            "to": "2021-05-03T00:16:40+00:00",
            "reason": "source_cap",
        }
    ]
    # A page that overlaps the watermark lost nothing.
    fake_instagram.profiles = {
        "fake_venue": synth.profile("fake_venue", "71001", new[1:] + old[-1:])
    }
    _, _, items = collect(InstagramFeed(), checkpoints[-1])
    assert coverage_of(items) == []


def test_ig_11_live_matches_export_mapping(
    monkeypatch, fake_instagram, synth, tmp_path
):
    live_env(monkeypatch, fake_instagram, INSTAGRAM_FEED_ALL="1")
    live, _, _ = collect(InstagramFeed())
    path = tmp_path / "feed.jsonl"
    write_jsonl(path, raw_records(fake_instagram, synth.accounts))
    monkeypatch.setenv("INSTAGRAM_FEED_SOURCE", f"export:{path}")
    exported, _, _ = collect(InstagramFeed())
    key = lambda m: m["external_id"]  # noqa: E731
    # Accounts were configured without labels in live mode and with them in
    # the export; everything else is identical.
    for a, b in zip(sorted(live, key=key), sorted(exported, key=key)):
        a, b = copy.deepcopy(a), copy.deepcopy(b)
        for m in (a, b):
            m["payload"].pop("account_config")
            for k in ("account_label", "account_category"):
                m["fields"].pop(k)
            m["content"] = m["content"].split(":", 1)[-1]
        assert a == b


def test_ig_11_live_needs_accounts(monkeypatch, fake_instagram):
    live_env(monkeypatch, fake_instagram, INSTAGRAM_FEED_ACCOUNTS="")
    with pytest.raises(ValueError):
        collect(InstagramFeed())


# ------------------------------------------------------------------ contract kit


def test_ig_12_contract_export(export_dir):
    evidence = check_plugin(
        "throughline_instagram_feed:InstagramFeed",
        {
            "INSTAGRAM_FEED_SOURCE": f"export:{export_dir}",
            "INSTAGRAM_FEED_COOKIES_FILE": "fake-cookie-path-canary",
        },
        secrets=["fake-cookie-path-canary"],
    )
    assert evidence["messages"] == 8
    channel = evidence["manifest"]["channels"][0]
    assert (
        channel["name"] == CHANNEL
        and channel["is_outside"] is True
        and channel["location_tolerance_s"] == 0
    )


def test_ig_12_contract_live_against_fake_server(fake_instagram):
    evidence = check_plugin(
        "throughline_instagram_feed:InstagramFeed",
        {
            "INSTAGRAM_FEED_SOURCE": "live",
            "INSTAGRAM_FEED_BASE_URL": fake_instagram.url,
            "INSTAGRAM_FEED_ACCOUNTS": "fake_venue,fake_records,fake_gallery",
            "INSTAGRAM_FEED_DELAY_S": "0",
            "INSTAGRAM_FEED_PER_DAY": "100000",
            "NO_PROXY": "127.0.0.1",
        },
    )
    assert evidence["messages"] == 8


def test_ig_13_owner_id_without_username_is_not_misattributed(synth):
    raw = synth.node("FAKEIDONLY", synth.base_ts, caption="Invented collab")
    raw["owner"] = {"id": "79999"}
    msg = map_post(post_record(synth, raw, account="fake_venue"))
    assert msg["author"] == {
        "system": "instagram",
        "kind": "user_id",
        "value": "79999",
        "display_name": None,
    }
    assert msg["fields"]["account"] is None and msg["fields"]["owner_id"] == "79999"
    assert msg["thread"]["external_id"] == "instagram:user:79999"
    # The same id-only node fetched through its own owner's profile uses it.
    own = synth.node("FAKEOWNID", synth.base_ts)
    own["owner"] = {"id": "71001"}
    mine = map_post(post_record(synth, own, account="fake_venue"))
    assert mine["fields"]["account"] == "fake_venue"
    assert mine["author"]["value"] == "71001"


def test_ig_13_mapping_is_byte_stable_across_key_order(synth):
    raw = synth.node("FAKEORDER", synth.base_ts, kind="carousel", caption="#A b @c")
    shuffled = json.loads(json.dumps(raw, sort_keys=True))
    shuffled = dict(reversed(list(shuffled.items())))
    a = map_post(post_record(synth, raw))
    b = map_post(post_record(synth, shuffled))
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert a["happened_at"].endswith("+00:00")


def story_record(raw, account="fake_venue", config=None):
    return {
        "kind": "story",
        "id": str(raw.get("pk") or raw.get("id")).split("_", 1)[0],
        "account": account,
        "fetched_at": "2021-06-01T00:00:00Z",
        "source": "reels_media",
        "account_config": config or {},
        "owner": {
            "id": "71001",
            "username": account,
            "full_name": "Invented",
            "is_verified": False,
        },
        "raw": raw,
    }


def test_ig_14_story_mapping_fields_stability_and_export(monkeypatch, tmp_path, synth):
    raw_a = synth.story(
        "403500001234",
        synth.base_ts + 600,
        is_video=True,
        caption="Tonight's lineup #LiveMusic",
        links=[
            (
                "https://l.instagram.com/?u=https%3A%2F%2Ftickets.invalid%2Fshow&e=SIG1",
                "Tickets",
            )
        ],
        mentions=["Fake_Band"],
        hashtags=["SanAntonio"],
        location=("5501", "Invented Hall"),
        poll=("Coming tonight?", ["YES", "NO"]),
        views=10,
        sig="a",
    )
    raw_b = synth.story(
        "403500001234",
        synth.base_ts + 600,
        is_video=True,
        caption="Tonight's lineup #LiveMusic",
        links=[
            (
                "https://l.instagram.com/?u=https%3A%2F%2Ftickets.invalid%2Fshow&e=SIG2",
                "Tickets",
            )
        ],
        mentions=["Fake_Band"],
        hashtags=["SanAntonio"],
        location=("5501", "Invented Hall"),
        poll=("Coming tonight?", ["YES", "NO"]),
        views=999,
        sig="zzz",
    )
    cfg = {"label": "Invented Venue", "category": "venues/music"}
    msg_a = map_record(story_record(raw_a, config=cfg))
    msg_b = map_story(story_record(raw_b, config=cfg))
    assert msg_a == msg_b  # Volatile CDN URLs, l.instagram sig, and viewer counts ignored.
    assert msg_a["channel"] == CHANNEL
    assert msg_a["external_id"] == "story:403500001234"
    assert msg_a["thread"] == {
        "external_id": "instagram:user:71001",
        "title": "@fake_venue",
    }
    assert msg_a["author"] == {
        "system": "instagram",
        "kind": "user_id",
        "value": "71001",
        "display_name": "@fake_venue",
    }
    f = msg_a["fields"]
    declared = {d["name"] for d in InstagramFeed.manifest.channels[0].fields}
    assert set(f) == declared
    assert f["post_type"] == "story"
    assert f["story_id"] == "403500001234"
    assert f["story_media_type"] == "video"
    assert f["is_video"] is True
    assert f["video_duration_s"] == 14.5
    assert f["expires_at"] == "2021-05-04T00:10:00+00:00"
    assert f["url"] == "https://www.instagram.com/stories/fake_venue/403500001234/"
    assert f["story_links"] == ["https://tickets.invalid/show"]
    assert f["mentions"] == ["fake_band"]
    assert f["hashtags"] == ["livemusic", "sanantonio"]
    assert f["location_name"] == "Invented Hall" and f["location_id"] == "5501"
    assert "https://tickets.invalid/show" in msg_a["content"]
    assert "poll: Coming tonight?" in msg_a["content"]

    # Export file with both posts and repeated stories deduplicates cleanly.
    path = tmp_path / "mixed.jsonl"
    post = post_record(synth, synth.node("FAKEP1", synth.base_ts + 100, caption="Post"))
    write_jsonl(
        path,
        [post, story_record(raw_a, config=cfg), story_record(raw_b, config=cfg)],
    )
    monkeypatch.setenv("INSTAGRAM_FEED_SOURCE", f"export:{path}")
    messages, _, _ = collect(InstagramFeed())
    assert [m["external_id"] for m in messages] == ["FAKEP1", "story:403500001234"]


def test_ig_14_polaris_pk_preferred_over_scoped_id(synth):
    raw = {
        "pk": "4035543578819384682",
        "id": "POLARIS_4035543578819384682",
        "code": "DgBIYDzI1Vq",
        "taken_at": synth.base_ts + 50,
        "media_type": 1,
        "product_type": "feed",
        "caption": {"text": "Tonight at Paper Tiger"},
        "user": {
            "pk": "1751107304",
            "id": "17841400099147361",
            "username": "papertigersatx",
        },
        "location": {"pk": 651479295, "name": "Paper Tiger"},
    }
    rec = post_record(synth, raw, account="papertigersatx")
    rec["owner"] = {"id": "1751107304", "username": "papertigersatx"}
    msg = map_post(rec)
    assert msg["fields"]["owner_id"] == "1751107304"
    assert msg["fields"]["media_id"] == "4035543578819384682"
    assert msg["thread"]["external_id"] == "instagram:user:1751107304"
    assert msg["author"]["value"] == "1751107304"
    assert msg["fields"]["location_id"] == "651479295"

