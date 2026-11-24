import os
import re
from pathlib import Path

import httpx
import pytest
from throughline_sdk import Checkpoint, Record
from throughline_sdk.contract import check_plugin

from letterboxd_fake import FEED, handler
from throughline_letterboxd import Letterboxd, plain

HERE = Path(__file__).resolve().parent


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("LETTERBOXD_USER", "invented_viewer")
    monkeypatch.setenv("LETTERBOXD_BATCH_SIZE", "2")
    for key in ("LETTERBOXD_TEST_TRANSPORT", "LETTERBOXD_FEED_URL", "LETTERBOXD_FEED_CAP"):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def plugin(feed=FEED):
    p = Letterboxd()
    p.transport = httpx.MockTransport(handler(feed))
    return p


def messages(items):
    return [i for i in items if isinstance(i, dict)]


def coverages(items):
    return [i.data for i in items if isinstance(i, Record) and i.kind == "coverage"]


def drop_items(feed, titles):
    text = feed.decode()
    for title in titles:
        text = re.sub(r"<item>\s*<title>" + re.escape(title) + r".*?</item>", "", text, flags=re.S)
    return text.encode()


def test_letterboxd_maps_diary_entries(env):
    items = list(plugin().records({}, "backfill"))
    rows = messages(items)
    assert len(rows) == 7  # the list item is skipped
    by_title = {r["fields"]["title"]: r for r in rows}
    one = by_title["Invented Film One"]
    assert one["content"] == "Watched Invented Film One (2019) - rated ★★★½ (3.5/5), liked"
    assert one["happened_at"] == "2021-02-01T12:00:00+13:00"
    f = one["fields"]
    assert f["rating"] == 7.0 and f["rating_raw"] == "3.5/5" and f["time_precision"] == "day"
    assert f["event_type"] == "play" and f["media_type"] == "movie" and f["author_kind"] == "owner"
    assert f["review"] is None and f["native_id"] == "tmdb:movie:101"
    review = by_title["The Placeholder Picture"]
    assert review["content"].startswith("Rewatched The Placeholder Picture (1984)")
    assert "wonderful & strange" in review["content"]
    assert review["fields"]["is_rewatch"] is True
    assert by_title["Mock Documentary"]["fields"]["rating"] is None
    links = [i.data for i in items if isinstance(i, Record) and i.kind == "link"]
    assert {link["type"] for link in links} == {"consumed", "liked"}
    assert len([i for i in items if isinstance(i, Checkpoint)]) == 4
    # Feed smaller than the cap: complete history, no coverage claim.
    assert coverages(items) == []


def test_letterboxd_plain_drops_boilerplate():
    assert plain('<p><img src="x"/></p> <p>Watched on Monday February 1, 2021.</p>') is None
    assert plain("<p>One</p><p>Two &amp; three</p>") == "One\n\nTwo & three"


def test_letterboxd_full_feed_first_run_is_best_effort(env):
    env.setenv("LETTERBOXD_FEED_CAP", "8")
    cov = coverages(plugin().records({}, "sync"))
    assert cov and cov[0]["status"] == "best_effort" and cov[0]["reasons"] == ["source_cap"]


def test_letterboxd_reports_source_cap_gap(env):
    env.setenv("LETTERBOXD_FEED_CAP", "3")
    old = drop_items(FEED, ["Pretend Premiere", "Fictional Feature", "Synthetic Sequel", "Mock Documentary"])
    first = list(plugin(old).records({}, "sync"))
    cursor = [i.cursor for i in first if isinstance(i, Checkpoint)][-1]
    # Later the feed only holds the three newest entries; Mock Documentary
    # (logged in between) scrolled out.
    new = drop_items(FEED, ["Invented Film One", "The Placeholder Picture", "Example Movie", "Mock Documentary"])
    cov = coverages(plugin(new).records(cursor, "sync"))
    assert len(cov) == 1
    gap = cov[0]["gaps"][0]
    assert cov[0]["status"] == "partial" and gap["reason"] == "source_cap"
    assert gap["from"] == cursor["high_water"] and gap["to"] > gap["from"]
    # Overlapping feed: no gap.
    overlap = drop_items(FEED, ["Invented Film One", "The Placeholder Picture", "Mock Documentary"])
    assert coverages(plugin(overlap).records(cursor, "sync")) == []


def test_letterboxd_missing_user_raises(env):
    env.setenv("LETTERBOXD_USER", "nobody")
    with pytest.raises(RuntimeError, match="not found"):
        list(plugin().records({}, "sync"))


def test_letterboxd_contract_kit():
    result = check_plugin(
        "throughline_letterboxd:Letterboxd",
        {
            "LETTERBOXD_USER": "invented_viewer",
            "LETTERBOXD_BATCH_SIZE": "2",
            "LETTERBOXD_FEED_CAP": "6",
            "LETTERBOXD_TEST_TRANSPORT": "letterboxd_fake:transport",
            "PYTHONPATH": os.pathsep.join(filter(None, [str(HERE), os.getenv("PYTHONPATH")])),
        },
    )
    assert result["messages"] == 7
