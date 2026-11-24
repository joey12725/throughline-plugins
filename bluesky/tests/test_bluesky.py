import json
import os
from pathlib import Path

import pytest
from throughline_sdk import Checkpoint, Record
from throughline_sdk.contract import check_plugin

from throughline_bluesky import Bluesky, post_text, tid_time
from throughline_bluesky.fake import FakeNetwork

RECORDED = Path(__file__).parent / "recorded" / "network.json"
PASSWORD = "invented-app-password-canary"
DID = "did:plc:inventedowner000000000001"


@pytest.fixture
def env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("BLUESKY_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("BLUESKY_IDENTIFIER", "invented-owner.example.com")
    monkeypatch.setenv("BLUESKY_APP_PASSWORD", PASSWORD)
    monkeypatch.setenv("BLUESKY_NOTIFICATIONS", "true")
    monkeypatch.setenv("BLUESKY_PAGE_SIZE", "3")
    return monkeypatch


@pytest.fixture
def network():
    return FakeNetwork(json.loads(RECORDED.read_text()))


def plugin(network):
    p = Bluesky(transport=network.transport())
    p.sleep = lambda s: None
    return p


def messages(items):
    return [i for i in items if isinstance(i, dict)]


def last_cursor(items):
    return [i for i in items if isinstance(i, Checkpoint)][-1].cursor


def test_tid_time_and_post_text():
    assert tid_time("3jzfcijpj2z2a").year == 2023
    assert tid_time("not-a-tid") is None
    value = {
        "text": "",
        "embed": {
            "$type": "app.bsky.embed.recordWithMedia",
            "record": {"record": {"uri": "at://did:plc:x/app.bsky.feed.post/1"}},
            "media": {"$type": "app.bsky.embed.images", "images": [{"alt": "Invented cat"}]},
        },
    }
    assert post_text(value) == "[image: Invented cat]\n[quoting at://did:plc:x/app.bsky.feed.post/1]"
    assert post_text({"text": " "}) == "[empty post]"


def test_session_resolves_pds_from_plc_and_reads_every_collection(env, network):
    items = list(plugin(network).records({}, "backfill"))
    hosts = {r.url.host for r in network.requests}
    assert {"bsky.social", "plc.directory", "pds.example.com"} <= hosts
    listed = [r for r in network.requests if r.url.path.endswith("listRecords")]
    assert all(r.url.host == "pds.example.com" for r in listed)
    out = messages(items)
    counts = {}
    for m in out:
        counts[m["channel"]] = counts.get(m["channel"], 0) + 1
    assert counts == {
        "bluesky.posts": 8,
        "bluesky.reposts": 2,
        "bluesky.likes": 4,
        "bluesky.notifications": 4,
    }
    assert PASSWORD not in json.dumps(out)


def test_post_mapping_threads_and_links(env, network):
    items = list(plugin(network).records({}, "backfill"))
    posts = {m["content"].split("\n")[0]: m for m in messages(items) if m["channel"] == "bluesky.posts"}
    reply = posts["Totally agree with this invented take"]
    parent = reply["fields"]["reply_parent_uri"]
    assert reply["thread"]["external_id"] == parent and reply["fields"]["is_reply"]
    assert reply["author"] == {
        "system": "bluesky",
        "kind": "account",
        "value": DID,
        "display_name": "invented-owner.example.com",
    }
    assert reply["fields"]["author_kind"] == "owner"
    links = [i.data for i in items if isinstance(i, Record) and i.kind == "link"]
    assert {
        "type": "reply_to",
        "from": {"channel": "bluesky.posts", "external_id": reply["external_id"]},
        "to": {"kind": "bluesky_post", "external_ref": parent},
        "method": "source",
    } in links
    assert {link["type"] for link in links} == {"reply_to", "quotes", "liked", "reposted"}
    photo = posts["Invented photo of a pretend garden"]
    assert "[image: Invented tomatoes on a vine]" in photo["content"]
    assert photo["fields"]["image_count"] == 1
    assert photo["thread"]["external_id"] == photo["external_id"]
    quote = posts["Quoting this one @invented-friend.example.com"]
    assert quote["fields"]["quote_uri"].startswith("at://did:plc:inventedstranger")
    assert quote["fields"]["mention_dids"] == ["did:plc:inventedfriend00000000002"]
    assert posts["An invented morning post with an offset"]["happened_at"] == "2025-03-01T07:15:00-05:00"
    intro = posts["Hello from an invented account #intro"]
    assert intro["fields"]["tags"] == ["intro"]
    assert intro["happened_at"] == "2025-01-05T10:00:00+00:00"


def test_likes_are_hydrated_and_missing_subjects_are_honest(env, network):
    out = messages(plugin(network).records({}, "backfill"))
    likes = sorted((m for m in out if m["channel"] == "bluesky.likes"), key=lambda m: m["happened_at"])
    assert likes[0]["content"] == (
        "Liked @invented-friend.example.com's post: Invented friend post about orchards"
    )
    assert likes[0]["fields"]["subject_available"] is True
    assert likes[0]["payload"]["subject_post"]["cid"] == "bafyfriend1"
    assert likes[2]["fields"]["subject_available"] is False
    assert "text not available" in likes[2]["content"]
    assert likes[3]["fields"]["subject_author_did"] == "did:plc:inventedfeed"


def test_hydration_off(env, network):
    env.setenv("BLUESKY_HYDRATE", "false")
    out = messages(plugin(network).records({}, "backfill"))
    assert not any(r.url.path.endswith("getPosts") for r in network.requests)
    assert all(not m["fields"]["subject_available"] for m in out if m["channel"] == "bluesky.likes")


def test_notifications_filtered_by_reason(env, network):
    out = [m for m in messages(plugin(network).records({}, "backfill")) if m["channel"] == "bluesky.notifications"]
    assert {m["fields"]["reason"] for m in out} == {"reply", "mention", "quote"}
    welcome = next(m for m in out if "Welcome" in m["content"])
    assert welcome["author"]["value"] == "did:plc:inventedfriend00000000002"
    assert welcome["fields"]["author_kind"] == "other"
    assert welcome["thread"]["external_id"] == welcome["fields"]["reply_root_uri"]


def test_sync_only_new_records_and_resume_mid_pass(env, network):
    first = list(plugin(network).records({}, "backfill"))
    cursor = last_cursor(first)
    again = messages(plugin(network).records(cursor, "sync"))
    # The newest notification is re-read (same indexedAt): replay safe.
    assert [m["channel"] for m in again] == ["bluesky.notifications"]
    posts = network.data["records"]["app.bsky.feed.post"]
    posts.append(
        {
            "uri": f"at://{DID}/app.bsky.feed.post/3zzzzzzzzzzzz",
            "cid": "bafynew",
            "value": {"text": "A brand new invented post", "createdAt": "2025-04-01T00:00:00Z"},
        }
    )
    new = messages(plugin(network).records(cursor, "sync"))
    assert [m["content"] for m in new if m["channel"] == "bluesky.posts"] == ["A brand new invented post"]
    # A crash after the first page resumes from the stored page cursor.
    items = plugin(network).records({}, "backfill")
    seen = []
    for item in items:
        if isinstance(item, Checkpoint):
            mid = item.cursor
            break
        seen.append(item)
    assert mid["app.bsky.feed.post"]["page"]
    rest = messages(plugin(network).records(json.loads(json.dumps(mid)), "backfill"))
    got = {m["external_id"] for m in messages(seen)} | {m["external_id"] for m in rest}
    assert len([g for g in got if "/app.bsky.feed.post/" in g and DID in g]) == 9


def test_expired_access_token_is_refreshed(env, network):
    network.expire_next = 1
    out = messages(plugin(network).records({}, "backfill"))
    assert any(r.url.path.endswith("refreshSession") for r in network.requests)
    assert len(out) == 18


def test_bad_password_and_unreachable(env, network):
    env.setenv("BLUESKY_APP_PASSWORD", "wrong-invented-password")
    with pytest.raises(RuntimeError, match="refused the sign-in"):
        list(plugin(network).records({}, "backfill"))
    import httpx

    def down(request):
        raise httpx.ConnectError("down")

    p = Bluesky(transport=httpx.MockTransport(down))
    p.sleep = lambda s: None
    with pytest.raises(RuntimeError, match="unreachable"):
        list(p.records({}, "backfill"))


def test_notification_gap_is_reported_as_source_cap(env, network):
    cursor = {"notifications": {"high": "2024-12-01T00:00:00+00:00"}}
    items = list(plugin(network).records(cursor, "sync"))
    gaps = [i.data for i in items if isinstance(i, Record) and i.kind == "coverage"]
    assert gaps and gaps[0]["reasons"] == ["source_cap"]
    assert gaps[0]["gaps"][0]["from"] == "2024-12-01T00:00:00+00:00"


def test_contract_kit():
    result = check_plugin(
        "throughline_bluesky:Bluesky",
        {
            "BLUESKY_FIXTURE": str(RECORDED),
            "BLUESKY_IDENTIFIER": "invented-owner.example.com",
            "BLUESKY_APP_PASSWORD": PASSWORD,
            "BLUESKY_NOTIFICATIONS": "true",
            "BLUESKY_PAGE_SIZE": "3",
        },
        secrets=[PASSWORD],
    )
    assert result["messages"] == 18
