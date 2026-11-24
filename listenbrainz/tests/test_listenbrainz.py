import os
from pathlib import Path

import httpx
import pytest
from throughline_sdk import Checkpoint, Record
from throughline_sdk.contract import check_plugin

from listenbrainz_fake import TOKEN, USER, handler, load
from throughline_listenbrainz import ListenBrainz, map_feedback, map_listen

HERE = Path(__file__).resolve().parent


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("LISTENBRAINZ_USER", USER)
    monkeypatch.setenv("LISTENBRAINZ_TOKEN", TOKEN)
    monkeypatch.setenv("LISTENBRAINZ_PAGE_SIZE", "3")
    monkeypatch.setenv("LISTENBRAINZ_REQUEST_DELAY_S", "0")
    monkeypatch.setenv("LISTENBRAINZ_LOOKBACK_S", "0")
    for name in ("LISTENBRAINZ_TEST_TRANSPORT", "LISTENBRAINZ_API_URL"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def plugin(**kwargs):
    p = ListenBrainz()
    p.transport = httpx.MockTransport(handler(**kwargs))
    return p


def messages(items, channel=None):
    return [i for i in items if isinstance(i, dict) and (channel is None or i["channel"] == channel)]


def links(items):
    return [i.data for i in items if isinstance(i, Record) and i.kind == "link"]


def test_backfill_full_history_oldest_first(env):
    requests = []
    items = list(plugin(requests=requests).records({}, "backfill"))
    listens = messages(items, "listenbrainz.listens")
    assert len(listens) == len(load("listenbrainz_listens.json"))
    assert len({m["external_id"] for m in listens}) == len(listens)
    assert [m["happened_at"] for m in listens] == sorted(m["happened_at"] for m in listens)
    first = listens[0]
    assert first["content"] == "Listened to Imaginary Song by Invented Band"
    assert first["happened_at"].endswith("+00:00")
    assert first["external_id"] == (
        f"listenbrainz:{USER}:listen:1612170000:bbbbbbbb-0000-4000-8000-000000000000"
    )
    assert first["thread"]["external_id"] == f"listenbrainz:{USER}:day:2021-02-01"
    f = first["fields"]
    assert f["event_type"] == "play" and f["media_type"] == "track"
    assert f["service"] == "listenbrainz" and f["author_kind"] == "owner"
    assert f["duration_s"] == 180.0 and f["client"] == "Example Player"
    assert f["submission_client"] == "Invented Scrobbler" and f["music_service"] == "example.com"
    assert f["url"] == "https://music.example.com/track/0"
    assert f["native_ids"]["recording_mbid"].startswith("aaaaaaaa")
    assert first["author"] == {
        "system": "listenbrainz", "kind": "account", "value": USER, "display_name": USER,
    }
    # Paged forward with min_ts only (the server rejects min_ts + max_ts).
    pages = [r for r in requests if r["path"].endswith("/listens")]
    assert all("max_ts" not in r and int(r["min_ts"]) >= 1 for r in pages)
    checkpoints = [i.cursor["listens_ts"] for i in items if isinstance(i, Checkpoint)]
    assert len(checkpoints) >= 6 and checkpoints == sorted(checkpoints)
    feedback = messages(items, "listenbrainz.feedback")
    assert [m["fields"]["event_type"] for m in feedback].count("liked") == 3
    assert any(m["content"].startswith("Hated Placeholder Anthem by") for m in feedback)


def test_page_boundary_inside_one_second_loses_nothing(env):
    data = load("listenbrainz_listens.json")
    assert data[2]["listened_at"] == data[3]["listened_at"]  # straddles page 1
    items = list(plugin().records({}, "backfill"))
    ids = [m["external_id"] for m in messages(items, "listenbrainz.listens")]
    assert len(ids) == len(set(ids)) == len(data)


def test_links_join_musicbrainz_refs(env):
    items = list(plugin().records({}, "backfill"))
    data = links(items)
    refs = {d["to"]["external_ref"] for d in data}
    assert "musicbrainz:recording:aaaaaaaa-0000-4000-8000-000000000001" in refs
    assert "musicbrainz:artist:11111111-0000-4000-8000-000000000001" in refs
    assert "musicbrainz:release:cccccccc-0000-4000-8000-000000000001" in refs
    assert "listenbrainz:artist:the fictional quartet" in refs
    assert {d["type"] for d in data} == {"consumed", "created_by", "part_of", "liked", "hated"}
    duet = [
        m for m in messages(items, "listenbrainz.listens") if m["fields"]["title"] == "Duet for Testing"
    ][0]
    assert duet["fields"]["creators"] == ["Synthetic Singer", "Invented Band"]
    work = "musicbrainz:recording:aaaaaaaa-0000-4000-8000-000000000004"
    artists = [d for d in data if d["type"] == "created_by" and d["from"]["external_ref"] == work]
    assert len({d["to"]["external_ref"] for d in artists}) == 2


def test_sync_resumes_from_cursor(env):
    items = list(plugin().records({}, "backfill"))
    cursor = [i.cursor for i in items if isinstance(i, Checkpoint)][-1]
    requests = []
    again = list(plugin(requests=requests).records(cursor, "sync"))
    assert messages(again, "listenbrainz.listens") == []
    assert any(r.get("min_ts") == str(cursor["listens_ts"]) for r in requests)


def test_sync_lookback_rereads_late_submissions(env):
    data = load("listenbrainz_listens.json")
    newest = max(i["listened_at"] for i in data)
    late = dict(data[-1], listened_at=newest - 100, recording_msid="bbbbbbbb-0000-4000-8000-00000000late")
    env.setenv("LISTENBRAINZ_LOOKBACK_S", "3600")
    cursor = {"listens_ts": newest, "feedback_created": 1612270000}
    again = list(plugin(listens=data + [late]).records(cursor, "sync"))
    ids = {m["external_id"] for m in messages(again, "listenbrainz.listens")}
    assert any(i.endswith("-00000000late") for i in ids)
    final = [i.cursor for i in again if isinstance(i, Checkpoint)][-1]
    assert final["listens_ts"] == newest


def test_external_ids_stable(env):
    a = messages(plugin().records({}, "backfill"))
    b = messages(plugin().records({}, "sync"))
    assert [m["external_id"] for m in a] == [m["external_id"] for m in b]


def test_map_edge_cases():
    assert map_listen({"track_metadata": {}}, USER) is None
    bare = map_listen({"listened_at": 1612170000, "track_metadata": {"track_name": "X"}}, USER)
    assert bare[0]["content"] == "Listened to X by an unknown artist"
    assert map_feedback({"created": 1612170000, "score": 0}, USER) is None
    loved = map_feedback(
        {"created": 1612170000, "score": 1, "recording_mbid": "aaaaaaaa-0000-4000-8000-000000000009"},
        USER,
    )
    message, data = loved
    assert message["external_id"].endswith(":feedback:mbid:aaaaaaaa-0000-4000-8000-000000000009")
    assert message["content"] == "Loved Unknown track by an unknown artist on ListenBrainz"
    assert data[0]["to"]["external_ref"] == "musicbrainz:recording:aaaaaaaa-0000-4000-8000-000000000009"


def test_bad_token_raises(env):
    env.setenv("LISTENBRAINZ_TOKEN", "wrong-token")
    with pytest.raises(RuntimeError, match="rejected the user token") as info:
        list(plugin().records({}, "sync"))
    assert "wrong-token" not in str(info.value)


def test_unknown_user_and_401_raise(env):
    env.setenv("LISTENBRAINZ_USER", "nobody_here")
    with pytest.raises(RuntimeError, match="HTTP 404"):
        list(plugin().records({}, "sync"))
    p = ListenBrainz()
    p.transport = httpx.MockTransport(lambda request: httpx.Response(401, json={"code": 401}))
    with pytest.raises(RuntimeError, match="HTTP 401") as info:
        list(p.records({}, "sync"))
    assert TOKEN not in str(info.value)


def test_unreachable_raises(env):
    p = ListenBrainz()

    def down(request):
        raise httpx.ConnectError("refused")

    p.transport = httpx.MockTransport(down)
    api = p.api()
    api.http.sleep = lambda s: None
    p.api = lambda: api
    with pytest.raises(RuntimeError, match="ConnectError"):
        list(p.records({}, "sync"))


def test_custom_api_url(env):
    env.setenv("LISTENBRAINZ_API_URL", "https://listens.example.com/")
    seen = []
    inner = handler()

    def handle(request):
        seen.append(request.url.host)
        return inner(request)

    p = ListenBrainz()
    p.transport = httpx.MockTransport(handle)
    list(p.records({}, "backfill"))
    assert set(seen) == {"listens.example.com"}


def test_contract_kit():
    result = check_plugin(
        "throughline_listenbrainz:ListenBrainz",
        {
            "LISTENBRAINZ_USER": USER,
            "LISTENBRAINZ_TOKEN": TOKEN,
            "LISTENBRAINZ_PAGE_SIZE": "3",
            "LISTENBRAINZ_REQUEST_DELAY_S": "0",
            "LISTENBRAINZ_TEST_TRANSPORT": "listenbrainz_fake:transport",
            "PYTHONPATH": os.pathsep.join(filter(None, [str(HERE), os.getenv("PYTHONPATH")])),
        },
        secrets=[TOKEN],
    )
    assert result["messages"] == 21
