import os
from pathlib import Path

import httpx
import pytest
from throughline_sdk import Checkpoint, Record
from throughline_sdk.contract import check_plugin

from goodreads_fake import KEY, PAGE, USER, handler, load
from throughline_goodreads import Goodreads

HERE = Path(__file__).resolve().parent


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("GOODREADS_USER_ID", USER)
    monkeypatch.setenv("GOODREADS_FEED_CAP", str(PAGE))
    monkeypatch.setenv("GOODREADS_BATCH_SIZE", "2")
    monkeypatch.setenv("GOODREADS_REQUEST_DELAY_S", "0")
    for key in ("GOODREADS_TEST_TRANSPORT", "GOODREADS_FEED_KEY", "GOODREADS_SHELVES", "GOODREADS_MAX_PAGES"):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def plugin(**kwargs):
    p = Goodreads()
    p.transport = httpx.MockTransport(handler(**kwargs))
    return p


def messages(items, channel=None):
    return [i for i in items if isinstance(i, dict) and channel in (None, i["channel"])]


def coverages(items):
    return [i.data for i in items if isinstance(i, Record) and i.kind == "coverage"]


def test_goodreads_books_and_reading_events(env):
    items = list(plugin().records({}, "backfill"))
    books = messages(items, "goodreads.books")
    events = {m["fields"]["title"]: m for m in messages(items, "goodreads.reading")}
    assert len(books) == len(load()) == len(events)
    novel = events["The Invented Novel"]
    assert novel["fields"]["event_type"] == "finished" and novel["fields"]["is_finished"] is True
    assert novel["happened_at"] == "2021-01-31T00:00:00-08:00"
    assert novel["fields"]["rating"] == 8 and novel["fields"]["rating_raw"] == "4/5"
    assert novel["content"] == (
        "Finished reading The Invented Novel by Ada Fictional, rated 4/5\n\n"
        "An invented review.\nSecond line & more."
    )
    assert novel["fields"]["shelves"] == ["read", "favorites"]
    assert novel["fields"]["author_kind"] == "owner" and novel["fields"]["media_type"] == "book"
    # No read date: falls back to the added date and says so.
    poems = events["Placeholder Poems"]
    assert poems["happened_at"] == "2021-01-04T10:00:00-08:00"
    assert "No read date" in poems["fields"]["timing_basis"]
    assert poems["fields"]["rating"] is None
    atlas = events["The Fake Atlas"]
    assert atlas["content"].startswith("Started reading") and atlas["happened_at"].startswith("2021-03-02")
    assert events["Someday Saga"]["fields"]["event_type"] == "watchlist_add"
    shelf_entry = next(b for b in books if b["fields"]["title"] == "Mock Memoir")
    assert shelf_entry["content"] == "Mock Memoir by Ada Fictional (2000) - on read, rated 3/5"
    links = [i.data for i in items if isinstance(i, Record) and i.kind == "link"]
    assert {link["type"] for link in links} == {"saved", "consumed", "created_by"}
    checkpoints = [i.cursor for i in items if isinstance(i, Checkpoint)]
    assert len(checkpoints) >= 5
    assert set(checkpoints[-1]["high_water"]) == {"read", "currently-reading", "to-read"}
    assert coverages(items) == []  # paged feed reached the start of every shelf


def test_goodreads_incremental_stops_at_high_water(env):
    items = list(plugin().records({}, "backfill"))
    cursor = [i.cursor for i in items if isinstance(i, Checkpoint)][-1]
    requests = []
    list(plugin(requests=requests).records(cursor, "sync"))
    read_pages = [r["page"] for r in requests if r["shelf"] == "read"]
    assert read_pages == ["1"]


def test_goodreads_unpaged_feed_reports_source_cap_gap(env):
    env.setenv("GOODREADS_SHELVES", "read")
    early = [b for b in load() if b["shelf"] == "read"][:2]
    first = list(plugin(books=early, paged=False).records({}, "sync"))
    cursor = [i.cursor for i in first if isinstance(i, Checkpoint)][-1]
    later = [b for b in load() if b["shelf"] == "read"][2:]
    cov = coverages(plugin(books=later, paged=False).records(cursor, "sync"))
    assert len(cov) == 1 and cov[0]["status"] == "partial" and cov[0]["scope_key"] == "read"
    gap = cov[0]["gaps"][0]
    assert gap["reason"] == "source_cap" and gap["from"] == cursor["high_water"]["read"]
    # First run against an unpaged full feed: best effort, nothing provable.
    cov = coverages(plugin(paged=False).records({}, "sync"))
    assert cov and cov[0]["status"] == "best_effort"


def test_goodreads_private_feed_key(env):
    env.setenv("GOODREADS_FEED_KEY", KEY)
    rows = messages(plugin(key=KEY).records({}, "sync"))
    assert rows and not any(KEY in str(r) for r in rows)


def test_goodreads_unknown_user_raises(env):
    env.setenv("GOODREADS_USER_ID", "999")
    with pytest.raises(RuntimeError, match="not found"):
        list(plugin().records({}, "sync"))


def test_goodreads_contract_kit():
    result = check_plugin(
        "throughline_goodreads:Goodreads",
        {
            "GOODREADS_USER_ID": USER,
            "GOODREADS_FEED_CAP": str(PAGE),
            "GOODREADS_BATCH_SIZE": "2",
            "GOODREADS_REQUEST_DELAY_S": "0",
            "GOODREADS_FEED_KEY": KEY,
            "GOODREADS_TEST_TRANSPORT": "goodreads_fake:transport",
            "PYTHONPATH": os.pathsep.join(filter(None, [str(HERE), os.getenv("PYTHONPATH")])),
        },
        secrets=[KEY],
    )
    assert result["messages"] == 20
