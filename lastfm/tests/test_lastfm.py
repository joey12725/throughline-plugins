import os
from pathlib import Path

import httpx
import pytest
from throughline_sdk import Checkpoint, Record
from throughline_sdk.contract import check_plugin

from lastfm_fake import API_KEY, handler, load
from throughline_lastfm import LastFm, map_scrobble

HERE = Path(__file__).resolve().parent


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("LASTFM_USER", "invented_listener")
    monkeypatch.setenv("LASTFM_API_KEY", API_KEY)
    monkeypatch.setenv("LASTFM_PAGE_SIZE", "3")
    monkeypatch.setenv("LASTFM_REQUEST_DELAY_S", "0")
    monkeypatch.delenv("LASTFM_TEST_TRANSPORT", raising=False)
    return monkeypatch


def plugin(**kwargs):
    p = LastFm()
    p.transport = httpx.MockTransport(handler(**kwargs))
    return p


def messages(items, channel=None):
    return [i for i in items if isinstance(i, dict) and (channel is None or i["channel"] == channel)]


def test_lastfm_backfill_full_history_oldest_first(env):
    items = list(plugin().records({}, "backfill"))
    scrobbles = messages(items, "lastfm.scrobbles")
    assert len(scrobbles) == len(load("lastfm_recent.json"))
    assert [m["happened_at"] for m in scrobbles] == sorted(m["happened_at"] for m in scrobbles)
    assert not any("Currently Playing" in m["content"] for m in scrobbles)
    first = scrobbles[0]
    assert first["content"] == "Listened to Imaginary Song by Invented Band"
    assert first["happened_at"].endswith("+00:00")
    f = first["fields"]
    assert f["event_type"] == "play" and f["media_type"] == "track" and f["service"] == "lastfm"
    assert f["author_kind"] == "owner" and f["creator"] == "Invented Band"
    assert f["native_ids"]["recording_mbid"].startswith("aaaaaaaa")
    assert first["author"]["system"] == "lastfm"
    links = [i.data for i in items if isinstance(i, Record) and i.kind == "link"]
    assert any(link["to"]["external_ref"].startswith("musicbrainz:artist:") for link in links)
    checkpoints = [i.cursor for i in items if isinstance(i, Checkpoint)]
    assert len(checkpoints) >= 5
    uts = [c["scrobbles_uts"] for c in checkpoints if "scrobbles_uts" in c]
    assert uts == sorted(uts)
    loved = messages(items, "lastfm.loved")
    assert len(loved) == 4 and all(m["fields"]["event_type"] == "liked" for m in loved)


def test_lastfm_sync_resumes_from_cursor(env):
    items = list(plugin().records({}, "backfill"))
    cursor = [i.cursor for i in items if isinstance(i, Checkpoint)][-1]
    requests = []
    again = list(plugin(requests=requests).records(cursor, "sync"))
    scrobbles = messages(again, "lastfm.scrobbles")
    # Only the boundary second is re-read; IDs dedupe it.
    assert len(scrobbles) == 1
    assert scrobbles[0]["external_id"] == messages(items, "lastfm.scrobbles")[-1]["external_id"]
    assert any(r.get("from") == str(cursor["scrobbles_uts"]) for r in requests)


def test_lastfm_external_ids_stable_and_unique(env):
    a = messages(plugin().records({}, "backfill"))
    b = messages(plugin().records({}, "sync"))
    assert [m["external_id"] for m in a] == [m["external_id"] for m in b]
    assert len({m["external_id"] for m in a}) == len(a)


def test_lastfm_now_playing_ignored():
    assert map_scrobble({"name": "x", "@attr": {"nowplaying": "true"}}, "u") is None


def test_lastfm_api_error_raises(env):
    env.setenv("LASTFM_USER", "nobody_here")
    with pytest.raises(RuntimeError, match="User not found|HTTP 404"):
        list(plugin().records({}, "sync"))


def test_lastfm_contract_kit():
    result = check_plugin(
        "throughline_lastfm:LastFm",
        {
            "LASTFM_USER": "invented_listener",
            "LASTFM_API_KEY": API_KEY,
            "LASTFM_PAGE_SIZE": "3",
            "LASTFM_REQUEST_DELAY_S": "0",
            "LASTFM_TEST_TRANSPORT": "lastfm_fake:transport",
            "PYTHONPATH": os.pathsep.join(filter(None, [str(HERE), os.getenv("PYTHONPATH")])),
        },
        secrets=[API_KEY],
    )
    assert result["messages"] == 18
