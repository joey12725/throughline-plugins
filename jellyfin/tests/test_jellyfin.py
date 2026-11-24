import os
from pathlib import Path

import httpx
import pytest
from throughline_sdk import Checkpoint, Record
from throughline_sdk.contract import check_plugin

from jellyfin_fake import API_KEY, handler
from throughline_jellyfin import Jellyfin

HERE = Path(__file__).resolve().parent


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("JELLYFIN_URL", "http://jellyfin.example.com:8096")
    monkeypatch.setenv("JELLYFIN_USER", "invented owner")
    monkeypatch.setenv("JELLYFIN_API_KEY", API_KEY)
    monkeypatch.setenv("JELLYFIN_PAGE_SIZE", "2")
    monkeypatch.setenv("JELLYFIN_TIMEZONE", "Pacific/Auckland")
    for key in ("JELLYFIN_TEST_TRANSPORT", "JELLYFIN_HISTORY"):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def plugin(**kwargs):
    p = Jellyfin()
    p.transport = httpx.MockTransport(handler(**kwargs))
    return p


def messages(items, channel=None):
    return [i for i in items if isinstance(i, dict) and channel in (None, i["channel"])]


def coverages(items):
    return [i.data for i in items if isinstance(i, Record) and i.kind == "coverage"]


def test_jellyfin_playback_reporting_every_play(env):
    items = list(plugin().records({}, "backfill"))
    plays = messages(items, "jellyfin.plays")
    # 8 owner rows; the guest's row is not the owner's.
    assert len(plays) == 8
    assert all(p["fields"]["source_kind"] == "playback_reporting" for p in plays)
    assert not any("Someone Else" in str(p["fields"]["device"]) for p in plays)
    first = plays[0]
    assert first["content"] == "Watched Fictional Detectives S01E01 - The Case of the Invented Clue 1 (45 min)"
    # Server-local 19:00 in Auckland (UTC+13 in February).
    assert first["happened_at"] == "2021-02-01T06:00:00.100000+00:00"
    f = first["fields"]
    assert f["media_type"] == "episode" and f["show"] == "Fictional Detectives"
    assert f["season"] == 1 and f["episode"] == 1 and f["listened_s"] == 2700
    assert f["client"] == "Jellyfin Web" and f["author_kind"] == "owner"
    heist = [p for p in plays if p["fields"]["title"] == "The Invented Heist"]
    assert len(heist) == 2  # both plays, not just the last one
    deleted = next(p for p in plays if "Since-Deleted" in p["content"])
    assert deleted["fields"]["detail"]["item_missing"] is True
    track = next(p for p in plays if p["fields"]["media_type"] == "track")
    assert track["content"].startswith("Listened to Imaginary Track 1 by Invented Band")
    links = [i.data for i in items if isinstance(i, Record) and i.kind == "link"]
    assert {"kind": "work", "external_ref": "imdb:tt0000001", "name": "The Invented Heist"} in [
        link["to"] for link in links
    ]
    assert coverages(items) == []


def test_jellyfin_library_additions(env):
    items = list(plugin().records({}, "backfill"))
    added = messages(items, "jellyfin.library")
    assert len(added) == 8  # movies, series, episodes, album; tracks excluded by default
    assert all(m["fields"]["event_type"] == "library_added" for m in added)
    heist = next(m for m in added if m["fields"]["title"] == "The Invented Heist")
    assert heist["content"] == "Added The Invented Heist (2019) to Invented Media Server (Movies)"
    assert heist["fields"]["library"] == "Movies" and heist["fields"]["added_at"] == heist["happened_at"]
    cursor = [i.cursor for i in items if isinstance(i, Checkpoint)][-1]
    assert set(cursor["added_at"]) == {"libmovies", "libshows", "libmusic"}


def test_jellyfin_sync_is_incremental(env):
    items = list(plugin().records({}, "backfill"))
    cursor = [i.cursor for i in items if isinstance(i, Checkpoint)][-1]
    again = list(plugin().records(cursor, "sync"))
    assert messages(again, "jellyfin.plays") == []
    # Additions re-read only the boundary item of each library.
    assert len(messages(again, "jellyfin.library")) == 3


def test_jellyfin_without_reporting_uses_last_played(env):
    items = list(plugin(reporting=False).records({}, "backfill"))
    plays = messages(items, "jellyfin.plays")
    assert len(plays) == 6  # one per played item: core keeps only the last play
    heist = next(p for p in plays if p["fields"]["title"] == "The Invented Heist")
    assert heist["fields"]["play_count"] == 2 and heist["fields"]["source_kind"] == "last_played"
    planet = next(p for p in plays if p["fields"]["title"] == "Placeholder Planet")
    assert planet["fields"]["event_type"] == "progress" and planet["fields"]["progress"] == 0.5
    assert planet["content"] == "Watched Placeholder Planet (2001) (50% so far)"
    cov = coverages(items)
    assert cov and cov[0]["status"] == "best_effort" and cov[0]["reasons"] == ["source_cap"]
    # A later play moves LastPlayedDate: a new message for that play.
    cursor = [i.cursor for i in items if isinstance(i, Checkpoint)][-1]
    from jellyfin_fake import load

    data = load()
    data["userdata"]["m0000000000000000000000000000001"].update(
        PlayCount=3, LastPlayedDate="2021-03-01T21:00:00.0000000Z"
    )
    later = messages(plugin(reporting=False, data=data).records(cursor, "sync"), "jellyfin.plays")
    new = [p for p in later if p["happened_at"].startswith("2021-03-01")]
    assert len(new) == 1 and new[0]["fields"]["play_count"] == 3


def test_jellyfin_bad_key_and_user(env):
    env.setenv("JELLYFIN_API_KEY", "wrong")
    with pytest.raises(RuntimeError, match="refused"):
        list(plugin().records({}, "sync"))
    env.setenv("JELLYFIN_API_KEY", API_KEY)
    env.setenv("JELLYFIN_USER", "nobody")
    with pytest.raises(RuntimeError, match="not found"):
        list(plugin().records({}, "sync"))


def kit_env(transport):
    return {
        "JELLYFIN_URL": "http://jellyfin.example.com:8096",
        "JELLYFIN_USER": "Invented Owner",
        "JELLYFIN_API_KEY": API_KEY,
        "JELLYFIN_PAGE_SIZE": "2",
        "JELLYFIN_TIMEZONE": "Pacific/Auckland",
        "JELLYFIN_TEST_TRANSPORT": transport,
        "PYTHONPATH": os.pathsep.join(filter(None, [str(HERE), os.getenv("PYTHONPATH")])),
    }


def test_jellyfin_contract_kit_playback_reporting():
    result = check_plugin("throughline_jellyfin:Jellyfin", kit_env("jellyfin_fake:transport"), secrets=[API_KEY])
    assert result["messages"] == 16


def test_jellyfin_contract_kit_core_only():
    result = check_plugin("throughline_jellyfin:Jellyfin", kit_env("jellyfin_fake:transport_core"), secrets=[API_KEY])
    assert result["messages"] == 14
