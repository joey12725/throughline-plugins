import json
import os
from pathlib import Path

import httpx
import pytest
from throughline_sdk import Checkpoint, Record
from throughline_sdk.contract import check_plugin

from readwise_fake import TOKEN, handler, load
from throughline_readwise import Readwise
from throughline_readwise.mapping import map_book, map_document

HERE = Path(__file__).resolve().parent
SETTINGS = {"owner_identifier": "readwise:account:owner", "owner_name": "Invented Owner"}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("READWISE_TOKEN", TOKEN)
    monkeypatch.setenv("READWISE_PAGE_SIZE", "2")
    monkeypatch.setenv("READWISE_REQUEST_DELAY_S", "0")
    monkeypatch.setenv("READWISE_OWNER_NAME", "Invented Owner")
    for name in ("READWISE_TEST_TRANSPORT", "READWISE_READER", "READWISE_READER_FEED"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def plugin(**kwargs):
    p = Readwise()
    p.transport = httpx.MockTransport(handler(**kwargs))
    return p


def messages(items, channel=None):
    return [i for i in items if isinstance(i, dict) and (channel is None or i["channel"] == channel)]


def links(items):
    return [i.data for i in items if isinstance(i, Record) and i.kind == "link"]


def by_id(items):
    return {m["external_id"]: m for m in messages(items)}


def test_readwise_backfill_channels_and_authorship(env):
    items = list(plugin().records({}, "backfill"))
    highlights = messages(items, "readwise.highlights")
    passages = [m for m in highlights if m["fields"]["entry_type"] == "highlight"]
    notes = [m for m in highlights if m["fields"]["entry_type"] == "note"]
    assert len(passages) == 21 and len(notes) == 10
    first = by_id(items)["readwise:highlight:9001"]
    assert first["content"] == "Invented passage 1 from The Invented Garden."
    assert first["author"] == {
        "system": "readwise", "kind": "author", "value": "Ada Example", "display_name": "Ada Example",
    }
    f = first["fields"]
    assert f["author_kind"] == "other" and f["selected_by"] == "owner"
    assert f["event_type"] == "highlight" and f["media_type"] == "book" and f["service"] == "readwise"
    assert f["title"] == "The Invented Garden" and f["creator"] == "Ada Example"
    assert f["tags"] == ["invented-tag"] and f["is_favorite"] is True
    assert first["thread"]["external_id"] == "readwise:book:101"
    assert first["happened_at"] == "2022-03-01T10:00:00+00:00"
    note = by_id(items)["readwise:highlight:9002:note"]
    assert note["fields"]["author_kind"] == "owner" and note["fields"]["selected_by"] is None
    assert note["author"]["value"] == "owner" and note["author"]["display_name"] == "Invented Owner"
    assert note["thread"] == first["thread"]
    assert {"type": "annotates", "method": "source",
            "from": {"channel": "readwise.highlights", "external_id": "readwise:highlight:9002:note"},
            "to": {"channel": "readwise.highlights", "external_id": "readwise:highlight:9002"}} in links(items)
    document_note = by_id(items)["readwise:book:101:note"]
    assert document_note["content"] == "Invented note: a book about patience."
    assert document_note["fields"]["author_kind"] == "owner"
    assert document_note["happened_at"] == first["happened_at"]
    for m in messages(items):
        assert m["fields"]["author_kind"] in ("owner", "other")


def test_readwise_books_reference(env):
    items = list(plugin().records({}, "backfill"))
    books = {m["external_id"]: m for m in messages(items, "readwise.books")}
    assert "readwise:book:108" not in books  # no highlights, nothing to date it by
    assert len(books) == 8
    media = {k: m["fields"]["media_type"] for k, m in books.items()}
    assert media["readwise:book:102"] == "article" and media["readwise:book:103"] == "tweet"
    assert media["readwise:book:104"] == "podcast" and media["readwise:book:105"] == "other"
    garden = books["readwise:book:101"]
    assert garden["fields"]["highlight_count"] == 4 and garden["fields"]["tags"] == ["invented-shelf"]
    assert garden["happened_at"] == "2022-03-01T10:00:00+00:00"
    assert garden["content"] == "The Invented Garden by Ada Example (book, 4 highlights in Readwise)"
    assert books["readwise:book:102"]["fields"]["url"] == "https://example.com/source/102"


def test_readwise_discard_and_missing_highlight_time():
    book = next(b for b in load("readwise_export.json") if b["user_book_id"] == 106)
    out, _ = map_book(book, SETTINGS)
    ids = {m["external_id"]: m for m in out}
    discarded = ids[f"readwise:highlight:{book['highlights'][2]['id']}"]
    assert discarded["fields"]["is_discard"] is True
    undated = ids[f"readwise:highlight:{book['highlights'][1]['id']}"]
    assert undated["happened_at"] == book["highlights"][1]["created_at"].replace("Z", "+00:00")


def test_readwise_reader_documents(env):
    items = list(plugin().records({}, "backfill"))
    docs = {m["external_id"]: m for m in messages(items, "readwise.reader")}
    assert len(docs) == 10  # two feed items and two child records skipped
    assert not any("child" in k for k in docs)
    zero = docs["readwise:reader:01invented0000"]
    assert zero["fields"]["event_type"] == "saved" and zero["fields"]["author_kind"] == "owner"
    assert "summary" not in zero["content"].lower()
    assert zero["fields"]["summary"] == "Invented AI-style summary of document 0."
    assert zero["fields"]["summary_author_kind"] == "agent"
    assert zero["fields"]["tags"] == ["invented"]
    archived = docs["readwise:reader:01invented0003"]
    assert archived["fields"]["is_finished"] is True and archived["fields"]["media_type"] == "document"
    assert docs["readwise:reader:01invented0004"]["fields"]["is_finished"] is True  # 99.5%
    assert docs["readwise:reader:01invented0001"]["fields"]["is_finished"] is False
    assert docs["readwise:reader:01invented0001"]["fields"]["progress"] == 0.35
    no_saved_at = docs["readwise:reader:01invented0005"]
    assert no_saved_at["happened_at"] == "2022-04-25T09:00:00+00:00"  # created_at fallback
    assert map_document({"id": "x", "parent_id": "y", "saved_at": "2022-01-01"}, SETTINGS) is None


def test_readwise_reader_feed_and_disable(env):
    env.setenv("READWISE_READER_FEED", "true")
    assert len(messages(plugin().records({}, "backfill"), "readwise.reader")) == 12
    env.setenv("READWISE_READER", "false")
    requests = []
    items = list(plugin(requests=requests).records({}, "backfill"))
    assert not messages(items, "readwise.reader")
    assert all(path == "/api/v2/export/" for path, _ in requests)


def test_readwise_checkpoints_and_high_water(env):
    items = list(plugin().records({}, "backfill"))
    checkpoints = [i.cursor for i in items if isinstance(i, Checkpoint)]
    assert len(checkpoints) >= 10
    final = checkpoints[-1]
    assert set(final) == {"export", "reader"}
    assert final["export"] == {"since": "2023-06-14T12:00:00+00:00"}
    assert final["reader"] == {"since": "2023-07-15T09:00:00+00:00"}
    # Mid-pass checkpoints never advance the completed-pass high-water mark.
    mid = [c["export"] for c in checkpoints if "page" in c.get("export", {})]
    assert mid and all(c["since"] is None for c in mid)


def test_readwise_resume_mid_pass_matches_clean_run(env):
    clean = list(plugin().records({}, "backfill"))
    checkpoints = [i for i in clean if isinstance(i, Checkpoint)]
    mid = next(c.cursor for c in checkpoints if (c.cursor.get("export") or {}).get("page"))
    resumed = list(plugin().records(mid, "backfill"))
    seen = {}
    for item in clean[: clean.index(next(c for c in checkpoints if c.cursor == mid)) + 1]:
        if isinstance(item, dict):
            seen[item["external_id"]] = item
    seen.update(by_id(resumed))
    assert seen == by_id(clean)
    assert [i.cursor for i in resumed if isinstance(i, Checkpoint)][-1] == checkpoints[-1].cursor


def test_readwise_sync_rereads_changed_books_whole(env):
    clean = list(plugin().records({}, "backfill"))
    cursor = [i.cursor for i in clean if isinstance(i, Checkpoint)][-1]
    export = load("readwise_export.json")
    changed = next(b for b in export if b["user_book_id"] == 107)
    changed["highlights"][0].update(text="Invented edited passage.", updated_at="2024-02-01T00:00:00Z")
    requests = []
    again = list(plugin(export=export, requests=requests).records(cursor, "sync"))
    got = by_id(again)
    old = by_id(clean)
    assert got["readwise:highlight:9017"]["content"] == "Invented edited passage."
    # The whole book is re-read, so the book entry and untouched highlights match.
    assert got["readwise:book:107"] == old["readwise:book:107"]
    assert got["readwise:highlight:9018"] == old["readwise:highlight:9018"]
    assert any(p.get("ids") for _, p in requests)
    export_params = [p for path, p in requests if path == "/api/v2/export/" and not p.get("ids")]
    assert export_params[0]["updatedAfter"] == "2023-06-14T11:50:00+00:00"  # 10 min overlap
    final = [i.cursor for i in again if isinstance(i, Checkpoint)][-1]
    assert final["export"]["since"] == "2024-02-01T00:00:00+00:00"


def test_readwise_ids_stable(env):
    a = messages(plugin().records({}, "backfill"))
    b = messages(plugin().records({}, "sync"))
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert len({(m["channel"], m["external_id"]) for m in a}) == len(a)


def test_readwise_bad_token_raises(env):
    env.setenv("READWISE_TOKEN", "invented-wrong-token")
    with pytest.raises(RuntimeError, match="rejected the token"):
        list(plugin().records({}, "sync"))


def test_readwise_contract_kit():
    result = check_plugin(
        "throughline_readwise:Readwise",
        {
            "READWISE_TOKEN": TOKEN,
            "READWISE_PAGE_SIZE": "2",
            "READWISE_REQUEST_DELAY_S": "0",
            "READWISE_TEST_TRANSPORT": "readwise_fake:contract_transport",
            "PYTHONPATH": os.pathsep.join(filter(None, [str(HERE), os.getenv("PYTHONPATH")])),
        },
        secrets=[TOKEN],
    )
    assert result["messages"] == 19
