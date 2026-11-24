import json
from pathlib import Path

import httpx
import pytest
from throughline_sdk import Checkpoint, Record
from throughline_sdk.contract import check_plugin

from throughline_mastodon import Mastodon
from throughline_mastodon.client import fixture_transport, page_params
from throughline_mastodon.text import html_to_text

RECORDED = Path(__file__).parent / "recorded" / "mastodon.json"
TOKEN = "invented-canary-token-0123456789"
HOST = "social.example"

ENV = {
    "MASTODON_BASE_URL": f"https://{HOST}",
    "MASTODON_ACCESS_TOKEN": TOKEN,
    "MASTODON_FIXTURE": str(RECORDED),
    "MASTODON_BATCH_SIZE": "3",
    "MASTODON_NOTIFICATIONS": "true",
    "MASTODON_NOTIFICATION_TYPES": "mention,favourite,follow,reblog",
}


def make(monkeypatch, **overrides):
    for key in (
        "MASTODON_ACCT_DOMAIN",
        "MASTODON_CAPTURE",
        "MASTODON_DIRECT_LABEL",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in {**ENV, **overrides}.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    return Mastodon()


def messages(items):
    return [i for i in items if isinstance(i, dict)]


def links(items):
    return [i.data for i in items if isinstance(i, Record) and i.kind == "link"]


def checkpoints(items):
    return [i.cursor for i in items if isinstance(i, Checkpoint)]


def by_key(items):
    return {(m["channel"], m["external_id"]): m for m in messages(items)}


def test_mastodon_contract_kit():
    result = check_plugin("throughline_mastodon:Mastodon", ENV, secrets=[TOKEN])
    assert result["messages"] == 21


def test_mastodon_statuses_mapping(monkeypatch):
    rows = by_key(make(monkeypatch).records({}, "backfill"))
    first = rows[("mastodon.statuses", f"{HOST}:111000000000000101")]
    assert first["happened_at"] == "2023-04-01T08:00:00+00:00"
    assert first["content"] == (
        "Hello invented world! Reading "
        "https://example.com/garden/article-about-owls #owls"
    )
    assert first["author"] == {
        "system": "mastodon",
        "kind": "acct",
        "value": f"inventedowl@{HOST}",
        "display_name": "Invented Owl",
    }
    assert first["fields"]["author_kind"] == "owner"
    assert first["fields"]["tags"] == ["owls"]
    assert first["fields"]["links"] == ["https://example.com/garden/article-about-owls"]
    assert first["thread"]["external_id"] == f"{HOST}:100:statuses"

    reply = rows[("mastodon.statuses", f"{HOST}:111000000000000102")]
    assert reply["content"] == "@fern I agree, moss gardens are invented & lovely."
    assert reply["fields"]["is_reply"] is True
    assert reply["fields"]["mentions"] == ["fern@other.example"]

    boost = rows[("mastodon.statuses", f"{HOST}:111000000000000103")]
    assert boost["fields"]["is_reblog"] is True
    assert boost["fields"]["reblog_of_author"] == f"moss@{HOST}"
    assert boost["fields"]["status_author"] == f"inventedowl@{HOST}"
    assert boost["content"].startswith(f"Boosted @moss@{HOST}:\n\nInvented moss")
    assert "watering schedule\nwater on Tuesdays" in boost["content"]

    cw = rows[("mastodon.statuses", f"{HOST}:111000000000000104")]
    assert cw["content"] == (
        "CW: invented plot twist\n\nInvented spoiler: the owl was a cat.\n\n"
        "[image: An invented drawing of an owl in a hat]\n[video]"
    )
    assert cw["fields"]["media_count"] == 2

    poll = rows[("mastodon.statuses", f"{HOST}:111000000000000105")]
    assert "Poll: Owl / Heron / Wren" in poll["content"]

    direct = rows[("mastodon.statuses", f"{HOST}:111000000000000106")]
    assert direct["access_label"] == "third_party_confidence"
    assert "access_label" not in first

    multi = rows[("mastodon.statuses", f"{HOST}:111000000000000107")]
    assert multi["content"] == (
        "First invented line\n\nSecond invented paragraph with a link "
        "(https://example.com/x)\n\n- one\n- two\n\n"
        "Link: Invented card title (https://example.com/card)"
    )
    assert multi["fields"]["edited_at"] == "2023-04-06T10:05:00+00:00"


def test_mastodon_links_and_placeholders(monkeypatch):
    items = list(make(monkeypatch).records({}, "backfill"))
    found = {
        (link["type"], link["from"]["channel"], link["from"]["external_id"]): link["to"]
        for link in links(items)
    }
    reply = found[("reply_to", "mastodon.statuses", f"{HOST}:111000000000000102")]
    assert reply["kind"] == "mastodon_status"
    assert reply["external_ref"] == f"{HOST}:111000000000000050"
    boost = found[("reposted", "mastodon.statuses", f"{HOST}:111000000000000103")]
    assert boost["external_ref"] == f"{HOST}:111000000000000060"
    assert boost["name"].startswith(f"@moss@{HOST}: Invented moss")
    liked = found[("liked", "mastodon.favourites", f"{HOST}:111000000000000050")]
    saved = found[("saved", "mastodon.bookmarks", f"{HOST}:111000000000000050")]
    assert liked["external_ref"] == saved["external_ref"] == reply["external_ref"]
    for link in links(items):
        assert link["method"] == "source"
        assert link["to"]["kind"] == "mastodon_status"


def test_mastodon_saved_time_basis_and_authors(monkeypatch):
    rows = by_key(make(monkeypatch).records({}, "backfill"))
    fav = rows[("mastodon.favourites", f"{HOST}:111000000000000050")]
    assert fav["happened_at"] == "2023-03-01T09:00:00+00:00"
    assert fav["fields"]["time_basis"] == "status_created_at"
    assert fav["fields"]["author_kind"] == "other"
    assert fav["author"]["value"] == "fern@other.example"
    own = rows[("mastodon.favourites", f"{HOST}:111000000000000101")]
    assert own["fields"]["author_kind"] == "owner"
    media_only = rows[("mastodon.bookmarks", f"{HOST}:111000000000000082")]
    assert media_only["content"] == "[image]"


def test_mastodon_notifications(monkeypatch):
    rows = by_key(make(monkeypatch).records({}, "backfill"))
    get = lambda n: rows[("mastodon.notifications", f"{HOST}:notification:{n}")]  # noqa: E731
    assert get(9001)["content"] == "@inventedowl invented reply to your post"
    assert get(9001)["fields"]["in_reply_to_id"] == "111000000000000101"
    assert get(9002)["content"].startswith(f"@moss@{HOST} favourited your post:")
    assert get(9003)["content"] == "@fern@other.example followed you"
    assert get(9004)["access_label"] == "third_party_confidence"
    assert get(9005)["fields"]["notification_type"] == "reblog"


def test_mastodon_notifications_off_by_default(monkeypatch):
    plugin = make(monkeypatch, MASTODON_NOTIFICATIONS=None)
    channels = {m["channel"] for m in messages(plugin.records({}, "backfill"))}
    assert channels == {
        "mastodon.statuses",
        "mastodon.favourites",
        "mastodon.bookmarks",
    }


def test_mastodon_checkpoints_resume_and_sync(monkeypatch):
    plugin = make(monkeypatch)
    items = list(plugin.records({}, "backfill"))
    marks = checkpoints(items)
    assert len(marks) >= 8
    final = marks[-1]
    assert final["streams"]["statuses"] == {"done": True, "newest": "111000000000000107"}
    assert final["streams"]["favourites"] == {"done": True, "newest": "5005"}
    # Resume from a mid-backfill checkpoint delivers the rest exactly once.
    middle = marks[1]
    assert middle["streams"]["statuses"]["older"] == "111000000000000102"
    rest = messages(make(monkeypatch).records(middle, "sync"))
    seen = {m["external_id"] for m in messages(items[: items.index(next(
        i for i in items if isinstance(i, Checkpoint) and i.cursor == middle))])}
    assert seen.isdisjoint(
        m["external_id"] for m in rest if m["channel"] == "mastodon.statuses"
    )
    # A finished cursor syncs nothing new.
    assert messages(make(monkeypatch).records(final, "sync")) == []


def test_mastodon_sync_pages_forward_with_link_ids(monkeypatch):
    calls = []
    newer = {
        "id": "111000000000000200",
        "created_at": "2023-08-01T10:00:00.000Z",
        "content": "<p>Invented later favourite</p>",
        "account": {"id": "200", "acct": "fern@other.example", "username": "fern"},
        "visibility": "public",
    }

    def handler(request):
        calls.append((request.url.path, dict(request.url.params)))
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        if request.url.path.endswith("verify_credentials"):
            return httpx.Response(200, json={"id": "100", "acct": "inventedowl"})
        if request.url.params.get("min_id") == "5005":
            return httpx.Response(
                200,
                json=[newer],
                headers={
                    "Link": f'<https://{HOST}/api/v1/favourites?min_id=6000>; rel="prev"'
                },
            )
        return httpx.Response(200, json=[])

    plugin = make(monkeypatch, MASTODON_CAPTURE="favourites", MASTODON_NOTIFICATIONS=None)
    plugin.transport = httpx.MockTransport(handler)
    cursor = {"account_id": "100", "streams": {"favourites": {"done": True, "newest": "5005"}}}
    items = list(plugin.records(cursor, "sync"))
    assert [m["fields"]["status_id"] for m in messages(items)] == ["111000000000000200"]
    assert checkpoints(items)[-1]["streams"]["favourites"]["newest"] == "6000"
    assert ("/api/v1/favourites", {"limit": "3", "min_id": "6000"}) in calls


def test_mastodon_unreachable_raises_and_partial_failure_reports(monkeypatch):
    def down(request):
        return httpx.Response(503)

    plugin = make(monkeypatch)
    plugin.transport = httpx.MockTransport(down)
    with pytest.raises(Exception, match="HTTP 503"):
        list(plugin.records({}, "sync"))

    def no_bookmarks(request):
        if request.url.path == "/api/v1/bookmarks":
            return httpx.Response(403, json={"error": "scope"})
        return fixture_transport(RECORDED).handle_request(request)

    plugin = make(monkeypatch)
    plugin.transport = httpx.MockTransport(no_bookmarks)
    items = list(plugin.records({}, "sync"))
    reports = [i.data for i in items if isinstance(i, Record) and i.kind == "coverage"]
    assert reports == [
        {
            "scope_key": "",
            "status": "partial",
            "channel": "mastodon.bookmarks",
            "reasons": ["source_error"],
            "detail": {"error": "/api/v1/bookmarks: HTTP 403"},
        }
    ]
    assert TOKEN not in json.dumps(reports)
    assert {m["channel"] for m in messages(items)} >= {"mastodon.statuses"}


def test_mastodon_acct_domain_override(monkeypatch):
    rows = by_key(
        make(monkeypatch, MASTODON_ACCT_DOMAIN="example.org").records({}, "backfill")
    )
    first = rows[("mastodon.statuses", f"{HOST}:111000000000000101")]
    assert first["author"]["value"] == "inventedowl@example.org"
    assert first["external_id"] == f"{HOST}:111000000000000101"


def test_html_to_text_edge_cases():
    assert html_to_text("") == ("", [])
    text, urls = html_to_text(
        '<p>a<br>b</p><blockquote><p>quoted</p></blockquote>'
        '<p><a href="https://example.com/" >example.com</a> &lt;3</p>'
    )
    assert text == "a\nb\n\n> quoted\n\nexample.com <3"
    assert urls == ["https://example.com/"]


def test_page_params():
    header = (
        '<https://h.example/api/v1/favourites?limit=3&max_id=12>; rel="next", '
        '<https://h.example/api/v1/favourites?limit=3&min_id=40>; rel="prev"'
    )
    assert page_params(header) == {"next": {"max_id": "12"}, "prev": {"min_id": "40"}}
    assert page_params(None) == {}


def test_mastodon_direct_text_stays_out_of_entity_names(monkeypatch):
    items = list(make(monkeypatch).records({}, "backfill"))
    for link in links(items):
        assert "invented direct message" not in link["to"]["name"]
        assert "private note" not in link["to"]["name"]
