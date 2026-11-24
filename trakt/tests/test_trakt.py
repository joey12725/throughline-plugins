import json
import logging
import os
import stat
import time
from pathlib import Path

import httpx
import pytest
from throughline_sdk import Checkpoint, Record
from throughline_sdk.contract import check_plugin

from trakt_fake import (
    ACCESS_PREFIX,
    CLIENT_ID,
    CLIENT_SECRET,
    EXPIRED_ACCESS,
    REFRESH_PREFIX,
    VALID_ACCESS,
    VALID_REFRESH,
    handler,
    load,
)
from throughline_trakt import Trakt, describe, map_history, map_rating, work_ref
from throughline_trakt.client import TokenStore, protect
from throughline_trakt.common import ApiError
from throughline_trakt.login import device_login

HERE = Path(__file__).resolve().parent
FAR = int(time.time()) + 30 * 86400


def write_token(path, access=VALID_ACCESS, refresh=VALID_REFRESH, expires_at=FAR):
    path.write_text(
        json.dumps({"access_token": access, "refresh_token": refresh, "expires_at": expires_at})
    )
    return path


@pytest.fixture
def env(monkeypatch, tmp_path):
    token = write_token(tmp_path / "trakt-token.json")
    monkeypatch.setenv("TRAKT_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("TRAKT_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setenv("TRAKT_TOKEN_FILE", str(token))
    monkeypatch.setenv("TRAKT_PAGE_SIZE", "3")
    monkeypatch.setenv("TRAKT_REQUEST_DELAY_S", "0")
    for name in ("TRAKT_USER", "TRAKT_TEST_TRANSPORT", "TRAKT_RATINGS", "TRAKT_WATCHLIST"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.token = token
    return monkeypatch


def plugin(**kwargs):
    p = Trakt()
    p.transport = httpx.MockTransport(handler(**kwargs))
    return p


def messages(items, channel=None):
    return [i for i in items if isinstance(i, dict) and (channel is None or i["channel"] == channel)]


def links(items):
    return [i.data for i in items if isinstance(i, Record) and i.kind == "link"]


def test_trakt_backfill_history_oldest_first(env):
    items = list(plugin().records({}, "backfill"))
    history = messages(items, "trakt.history")
    assert len(history) == len(load("trakt_history.json"))
    assert [m["happened_at"] for m in history] == sorted(m["happened_at"] for m in history)
    first = history[0]
    assert first["content"] == "Watched The Invented Expedition (2019)"
    assert first["external_id"] == "trakt:history:5550001000"
    assert first["happened_at"] == "2021-02-01T10:00:00+00:00"
    assert first["author"] == {
        "system": "trakt", "kind": "account", "value": "invented_viewer",
        "display_name": "Invented Viewer",
    }
    f = first["fields"]
    assert f["service"] == "trakt" and f["event_type"] == "play" and f["media_type"] == "movie"
    assert f["author_kind"] == "owner" and f["duration_s"] == 118 * 60 and f["year"] == 2019
    assert f["action"] == "scrobble" and f["native_id"] == "trakt:movie:900001"
    assert f["native_ids"]["imdb"] == "tt9990001"
    assert first["thread"]["title"] == "Trakt 2021-02-01"
    episode = history[1]
    assert episode["content"] == "Watched Invented Station S01E01 - Pilot of Inventions"
    assert episode["fields"]["show"] == "Invented Station"
    assert (episode["fields"]["season"], episode["fields"]["episode"]) == (1, 1)
    assert episode["fields"]["url"] == "https://trakt.tv/shows/invented-station/seasons/1/episodes/1"
    untitled = history[3]
    assert untitled["content"] == "Watched Invented Station S01E03 - Episode 3"
    checkpoints = [i.cursor for i in items if isinstance(i, Checkpoint)]
    stamps = [c["history_at"] for c in checkpoints]
    assert len(checkpoints) >= 5 and stamps == sorted(stamps)
    assert checkpoints[-1]["history_at"] == "2021-02-11T23:30:00.000Z"
    assert checkpoints[-1]["ratings_activity"] == "2021-02-11T23:59:00+00:00"
    assert "watchlist_activity" in checkpoints[-1]


def test_trakt_links_prefer_imdb_then_tmdb(env):
    items = list(plugin().records({}, "backfill"))
    refs = {link["to"]["external_ref"] for link in links(items) if link["type"] == "consumed"}
    assert "imdb:tt9990001" in refs            # movie with an IMDb id
    assert "tmdb:movie:9990002" in refs        # movie with only TMDB
    assert "trakt:movie:900003" in refs        # neither
    assert "tmdb:episode:9970004" in refs      # episode of a show without IMDb
    part_of = [(link["from"]["external_ref"], link["to"]["external_ref"]) for link in links(items)
               if link["type"] == "part_of"]
    assert ("imdb:tt9970001", "imdb:tt9980001:season:1") in part_of
    assert ("imdb:tt9980001:season:1", "imdb:tt9980001") in part_of
    # A rated season shares the season reference derived from episodes.
    season = [m for m in messages(items, "trakt.ratings") if m["fields"]["media_type"] == "season"][0]
    rated = [link for link in links(items) if link["type"] == "rated"
             and link["from"]["external_id"] == season["external_id"]]
    assert rated[0]["to"]["external_ref"] == "imdb:tt9980001:season:1"
    kinds = {link["type"] for link in links(items)}
    assert kinds <= set(Trakt.manifest.link_types)


def test_trakt_ratings_and_watchlist(env):
    items = list(plugin().records({}, "backfill"))
    ratings = messages(items, "trakt.ratings")
    assert len(ratings) == 5
    movie = ratings[0]
    assert movie["content"] == "Rated The Invented Expedition (2019) 8/10 on Trakt"
    assert movie["fields"]["rating"] == 8.0 and movie["fields"]["rating_raw"] == "8/10"
    assert movie["fields"]["event_type"] == "rated"
    assert movie["external_id"] == "trakt:invented_viewer:rating:movie:900001"
    labels = [m["content"] for m in ratings]
    assert "Rated Invented Station Season 1 7/10 on Trakt" in labels
    assert "Rated The Synthetic Office S01E04 - Synthetic Thursday 10/10 on Trakt" in labels
    watchlist = messages(items, "trakt.watchlist")
    assert len(watchlist) == 4
    assert watchlist[0]["fields"]["event_type"] == "watchlist_add"
    assert watchlist[0]["fields"]["rank"] == 1
    assert watchlist[0]["fields"]["notes"].startswith("Invented note")
    assert watchlist[0]["content"].startswith("Added Fictional Futures (2023) to the Trakt watchlist: ")
    assert {m["fields"]["media_type"] for m in watchlist} == {"movie", "show", "episode"}


def test_trakt_sync_resumes_from_cursor_and_skips_unchanged_lists(env):
    items = list(plugin().records({}, "backfill"))
    cursor = [i.cursor for i in items if isinstance(i, Checkpoint)][-1]
    requests = []
    again = list(plugin(requests=requests).records(cursor, "sync"))
    history = messages(again, "trakt.history")
    # Only the boundary instant is re-read; history IDs dedupe it.
    assert [m["external_id"] for m in history] == [messages(items, "trakt.history")[-1]["external_id"]]
    assert any(r.get("start_at") == cursor["history_at"] for r in requests)
    assert all(r.get("end_at") for r in requests if r["path"] == "/sync/history")
    assert not messages(again, "trakt.ratings") and not messages(again, "trakt.watchlist")
    assert not any(r["path"] in ("/sync/ratings", "/sync/watchlist") for r in requests)


def test_trakt_sync_rereads_changed_ratings(env):
    items = list(plugin().records({}, "backfill"))
    cursor = [i.cursor for i in items if isinstance(i, Checkpoint)][-1]
    ratings = load("trakt_ratings.json")
    ratings[0] = dict(ratings[0], rating=6, rated_at="2021-03-01T00:00:00.000Z")
    again = list(plugin(ratings=ratings).records(cursor, "sync"))
    rerated = [m for m in messages(again, "trakt.ratings") if m["external_id"].endswith("movie:900001")]
    assert rerated[0]["fields"]["rating"] == 6.0
    assert rerated[0]["external_id"] == messages(items, "trakt.ratings")[0]["external_id"]


def test_trakt_external_ids_stable_and_unique(env):
    a = messages(plugin().records({}, "backfill"))
    b = messages(plugin().records({}, "backfill"))
    assert [m["external_id"] for m in a] == [m["external_id"] for m in b]
    assert len({m["external_id"] for m in a}) == len(a)


def test_trakt_trakt_user_setting_skips_profile_lookup(env):
    env.setenv("TRAKT_USER", "Invented_Viewer")
    requests = []
    items = messages(plugin(requests=requests).records({}, "backfill"))
    assert not any(r["path"] == "/users/settings" for r in requests)
    assert items[0]["author"]["value"] == "Invented_Viewer"
    assert items[0]["thread"]["external_id"].startswith("trakt:invented_viewer:day:")


def test_trakt_lists_can_be_disabled(env):
    env.setenv("TRAKT_RATINGS", "false")
    env.setenv("TRAKT_WATCHLIST", "false")
    items = messages(plugin().records({}, "backfill"))
    assert {m["channel"] for m in items} == {"trakt.history"}


def test_trakt_refreshes_expired_token_and_rewrites_file(env, caplog):
    write_token(env.token, access=EXPIRED_ACCESS, expires_at=int(time.time()) - 10)
    caplog.set_level(logging.DEBUG)
    root = logging.getLogger()
    root.addHandler(caplog.handler)
    try:
        items = list(plugin().records({}, "backfill"))
        logging.getLogger("throughline_trakt").warning(
            "probe %s", json.loads(env.token.read_text())["access_token"]
        )
    finally:
        root.removeHandler(caplog.handler)
    assert messages(items)
    saved = json.loads(env.token.read_text())
    assert saved["access_token"].startswith(ACCESS_PREFIX + "refreshed-")
    assert saved["refresh_token"].startswith(REFRESH_PREFIX + "refreshed-")
    assert saved["expires_at"] > time.time() + 3600
    assert stat.S_IMODE(os.stat(env.token).st_mode) == 0o600
    assert not list(env.token.parent.glob(".*.tmp"))
    text = caplog.text + json.dumps(messages(items)) + json.dumps(links(items))
    assert "probe [REDACTED]" in caplog.text
    for secret in (ACCESS_PREFIX, REFRESH_PREFIX, CLIENT_ID, CLIENT_SECRET):
        assert secret not in text


def test_trakt_refreshes_on_401(env):
    write_token(env.token, access=ACCESS_PREFIX + "revoked-0000")
    requests = []
    items = list(plugin(requests=requests).records({}, "sync"))
    assert messages(items)
    assert sum(r["path"] == "/oauth/token" for r in requests) == 1
    assert json.loads(env.token.read_text())["access_token"].startswith(ACCESS_PREFIX + "refreshed-")


def test_trakt_rejected_refresh_asks_for_login(env):
    write_token(env.token, access=EXPIRED_ACCESS, refresh=REFRESH_PREFIX + "revoked",
                expires_at=1)
    with pytest.raises(ApiError, match="throughline-trakt-login") as error:
        list(plugin().records({}, "sync"))
    assert REFRESH_PREFIX not in str(error.value)


def test_trakt_missing_token_file_is_clear(env, tmp_path):
    env.setenv("TRAKT_TOKEN_FILE", str(tmp_path / "missing.json"))
    with pytest.raises(ApiError, match="No Trakt token file .*run throughline-trakt-login"):
        list(plugin().records({}, "sync"))


def test_trakt_bad_client_id_raises(env):
    env.setenv("TRAKT_CLIENT_ID", "not-the-client-id")
    with pytest.raises(ApiError, match="HTTP 403"):
        list(plugin().records({}, "sync"))


def test_trakt_unknown_items_skipped():
    assert describe({"type": "person", "person": {"name": "x"}}) is None
    assert map_history({"id": 1, "type": "movie", "movie": {"title": "x"}}, "u") is None
    assert map_rating({"type": "movie", "rated_at": "2021-01-01T00:00:00Z",
                       "movie": {"ids": {"trakt": 1}}}, "u") is None
    assert work_ref("movie", {"imdb": "", "tmdb": None, "trakt": 5}) == "trakt:movie:5"


def test_trakt_token_store_atomic_0600(tmp_path):
    store = TokenStore(str(tmp_path / "nested" / "token.json"))
    store.save({"access_token": ACCESS_PREFIX + "a", "refresh_token": REFRESH_PREFIX + "b",
                "expires_at": FAR})
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    assert store.load()["access_token"] == ACCESS_PREFIX + "a"


def test_trakt_device_login_polls_until_approved(tmp_path):
    store = TokenStore(str(tmp_path / "token.json"))
    printed, sleeps = [], []
    settings = device_login(
        CLIENT_ID, CLIENT_SECRET, store,
        transport=httpx.MockTransport(handler(device_replies=[400, 429, 400])),
        out=printed.append, sleep=sleeps.append, clock=lambda: 0,
    )
    assert settings["user"]["username"] == "invented_viewer"
    assert sleeps == [5, 5, 6, 6]  # slow_down adds a second
    assert "INV3NT3D" in printed[0] and "https://trakt.tv/activate" in printed[0]
    assert "invented_viewer" in printed[-1]
    assert not any(ACCESS_PREFIX in line or REFRESH_PREFIX in line for line in printed)
    assert store.load()["access_token"].startswith(ACCESS_PREFIX)
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600


@pytest.mark.parametrize(
    "status,message", [(404, "Invalid"), (409, "already used"), (410, "expired"), (418, "denied")]
)
def test_trakt_device_login_failures(tmp_path, status, message):
    store = TokenStore(str(tmp_path / "token.json"))
    with pytest.raises(ApiError, match=message):
        device_login(
            CLIENT_ID, CLIENT_SECRET, store,
            transport=httpx.MockTransport(handler(device_replies=[400, status])),
            out=lambda _: None, sleep=lambda _: None, clock=lambda: 0,
        )
    assert not store.path.exists()


def test_trakt_protect_redacts_root_handlers(caplog):
    root = logging.getLogger()
    root.addHandler(caplog.handler)
    try:
        protect("invented-protect-probe-value")
        logging.getLogger("x").error("token=%s", "invented-protect-probe-value")
    finally:
        root.removeHandler(caplog.handler)
    assert "invented-protect-probe-value" not in caplog.text


def test_trakt_contract_kit(tmp_path):
    token = write_token(tmp_path / "trakt-token.json", access=EXPIRED_ACCESS, expires_at=1)
    result = check_plugin(
        "throughline_trakt:Trakt",
        {
            "TRAKT_CLIENT_ID": CLIENT_ID,
            "TRAKT_CLIENT_SECRET": CLIENT_SECRET,
            "TRAKT_TOKEN_FILE": str(token),
            "TRAKT_PAGE_SIZE": "3",
            "TRAKT_REQUEST_DELAY_S": "0",
            "TRAKT_TEST_TRANSPORT": "trakt_fake:transport",
            "PYTHONPATH": os.pathsep.join(filter(None, [str(HERE), os.getenv("PYTHONPATH")])),
        },
        secrets=[CLIENT_ID, CLIENT_SECRET, ACCESS_PREFIX, REFRESH_PREFIX],
    )
    assert result["messages"] == 23
    assert json.loads(token.read_text())["access_token"].startswith(ACCESS_PREFIX + "refreshed-")
