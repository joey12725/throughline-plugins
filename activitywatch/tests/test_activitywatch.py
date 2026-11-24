import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from throughline_sdk import Checkpoint, Record
from throughline_sdk.contract import check_plugin

from throughline_activitywatch import AFK, EDITOR, WEB, WINDOWS, ActivityWatchPlugin
from throughline_activitywatch import sessions as S
from throughline_activitywatch.client import fixture_transport

FIXTURE = Path(__file__).parent / "recorded" / "activitywatch.json"
LAPTOP = "invented-laptop"


@pytest.fixture
def env(monkeypatch):
    for key in ("ACTIVITYWATCH_SINCE", "ACTIVITYWATCH_HOSTS", "ACTIVITYWATCH_WINDOW_H", "ACTIVITYWATCH_EXCLUDE_APPS", "ACTIVITYWATCH_INCLUDE_INCOGNITO"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ACTIVITYWATCH_FIXTURE", str(FIXTURE))
    return monkeypatch


def messages(items):
    return [i for i in items if isinstance(i, dict)]


def collect(cursor=None, **kw):
    return list(ActivityWatchPlugin(**kw).records(cursor or {}, "backfill"))


def by_content(items, channel):
    return {m["content"]: m for m in messages(items) if m["channel"] == channel}


def test_aw_coalesces_windows_with_afk_and_blips(env):
    items = collect()
    windows = [m for m in messages(items) if m["channel"] == WINDOWS and m["fields"]["host"] == LAPTOP]
    day1 = [m for m in windows if m["happened_at"].startswith("2024-02-05")]
    assert [m["fields"]["app"] for m in day1] == [
        "Invented Editor", "Invented Browser", "Invented Terminal", "Invented Terminal", "Invented Browser",
    ]
    editor = day1[0]
    # 120 s + a 2 s chat blip (dropped) + 300 s merge into one session
    assert editor["happened_at"] == "2024-02-05T09:00:00+00:00" and editor["ended_at"] == "2024-02-05T09:07:02+00:00"
    assert editor["fields"]["active_s"] == 420 and editor["fields"]["event_count"] == 2
    assert editor["fields"]["afk_filtered"] is True
    assert editor["external_id"] == f"{LAPTOP}/aw-watcher-window_{LAPTOP}/2024-02-05T09:00:00+00:00"
    assert editor["thread"] == {"external_id": f"{LAPTOP}:2024-02-05", "title": f"{LAPTOP} 2024-02-05"}
    # The terminal session is cut by 10 minutes AFK into two sessions.
    term1, term2 = day1[2], day1[3]
    assert term1["ended_at"] == "2024-02-05T09:33:20+00:00" and term2["happened_at"] == "2024-02-05T09:43:20+00:00"
    # The 20 s music session is below ACTIVITYWATCH_MIN_SESSION_S.
    assert not any(m["fields"]["app"] == "Invented Music" for m in windows)
    # The newest event is still in progress: held back.
    assert not any("draft.md" in m["content"] for m in windows)
    desktop = [m for m in messages(items) if m["fields"]["host"] == "invented-desktop"]
    assert len(desktop) == 5 and all(m["fields"]["afk_filtered"] is False for m in desktop)
    channels = {m["channel"] for m in messages(items)}
    assert channels == {WINDOWS, WEB, EDITOR, AFK}


def test_aw_web_editor_afk_fields(env):
    items = collect()
    web = by_content(items, WEB)
    docs = web["Invented Docs (docs.example.org) (20m)"]
    assert docs["fields"]["domain"] == "docs.example.org" and docs["fields"]["browser"] == "chrome"
    assert docs["fields"]["host"] == LAPTOP  # from the bucket id: the web bucket says hostname unknown
    video = web["Invented Video (example.org) (19m 50s)"]
    assert video["fields"]["audible"] is True
    assert not any("Private" in c for c in web)  # incognito dropped
    assert not any("Tail" in c for c in web)  # in progress
    editor = by_content(items, EDITOR)["Editing notes.txt in /home/invented/project (2m 40s)"]
    assert editor["fields"]["language"] == "plaintext" and editor["fields"]["editor"] == "aw-watcher-vscode"
    afk = [m for m in messages(items) if m["channel"] == AFK]
    assert [m["fields"]["status"] for m in afk] == ["active", "afk", "active", "afk"]
    assert afk[3]["content"] == "Away from the computer for 22h 13m"


def test_aw_incognito_and_exclusions(env):
    env.setenv("ACTIVITYWATCH_INCLUDE_INCOGNITO", "1")
    env.setenv("ACTIVITYWATCH_MIN_SESSION_S", "5")
    env.setenv("ACTIVITYWATCH_EXCLUDE_APPS", "invented terminal")
    env.setenv("ACTIVITYWATCH_HOSTS", LAPTOP)
    items = collect()
    assert any("Private" in m["content"] for m in messages(items))
    assert not any(m["fields"].get("app") == "Invented Terminal" for m in messages(items))
    assert any(m["fields"].get("app") == "Invented Music" for m in messages(items))
    assert {m["fields"]["host"] for m in messages(items)} == {LAPTOP}


def test_aw_resume_and_small_windows_give_the_same_sessions(env):
    full = {m["external_id"]: m for m in messages(collect())}
    env.setenv("ACTIVITYWATCH_WINDOW_H", "1")
    items = collect()
    small = {m["external_id"]: m for m in messages(items)}
    assert small == full  # the window grid does not change sessions (long AFK widens the window)
    checkpoints = [i.cursor for i in items if isinstance(i, Checkpoint)]
    for cursor in checkpoints[::7]:
        before = []
        for item in items:
            if isinstance(item, Checkpoint) and item.cursor == cursor:
                break
            if isinstance(item, dict):
                before.append(item["external_id"])
        resumed = {m["external_id"]: m for m in messages(collect(cursor))}
        assert set(before) | set(resumed) == set(full)
        assert all(resumed[k] == full[k] for k in resumed)
    # Nothing new at the final cursor.
    assert not messages(collect(checkpoints[-1]))


def test_aw_in_progress_event_is_emitted_once_when_stable(env, tmp_path):
    recorded = json.loads(FIXTURE.read_text())
    bucket = f"aw-watcher-window_{LAPTOP}"
    path = tmp_path / "aw.json"
    path.write_text(json.dumps(recorded))
    env.setenv("ACTIVITYWATCH_FIXTURE", str(path))
    env.setenv("ACTIVITYWATCH_HOSTS", LAPTOP)
    first = collect()
    cursor = [i.cursor for i in first if isinstance(i, Checkpoint)][-1]
    assert not any("draft.md" in m["content"] for m in messages(first))
    # The watcher keeps heartbeating: the last event grows and a new one starts.
    later = copy.deepcopy(recorded)
    draft = later["events"][bucket][-1]
    draft["duration"] = 900
    end = S.parse(draft["timestamp"]) + timedelta(seconds=900)
    later["events"][bucket].append({"id": 999, "timestamp": end.isoformat(), "duration": 600, "data": {"app": "Invented Mail", "title": "Inbox"}})
    for b in (bucket, f"aw-watcher-afk_{LAPTOP}"):
        later["buckets"][b]["last_updated"] = (end + timedelta(seconds=600)).isoformat()
    later["events"][f"aw-watcher-afk_{LAPTOP}"][-1]["duration"] += 2000
    path.write_text(json.dumps(later))
    second = messages(collect(cursor))
    drafts = [m for m in second if "draft.md" in m["content"]]
    assert len(drafts) == 1 and drafts[0]["fields"]["active_s"] == 900
    ids = {m["external_id"] for m in messages(first)}
    assert not ids & {m["external_id"] for m in second}


def test_aw_settle_holds_recent_data(env):
    now = datetime(2024, 2, 5, 9, 30, tzinfo=timezone.utc)
    items = collect(now=lambda: now)
    assert all(m["ended_at"] <= "2024-02-05T09:20:00+00:00" for m in messages(items))


def test_aw_session_functions():
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)

    def iso(s):
        return (t + timedelta(seconds=s)).isoformat()

    events = [
        {"timestamp": iso(0), "duration": 50, "data": {"k": "a"}},
        {"timestamp": iso(50), "duration": 1, "data": {"k": "b"}},
        {"timestamp": iso(51), "duration": 30, "data": {"k": "a"}},
        {"timestamp": iso(200), "duration": 100, "data": {"k": "a"}},
        {"timestamp": "bad", "duration": 1, "data": {}},
    ]
    pieces = S.clip(events, t, t + timedelta(seconds=250))
    assert pieces[-1].end == t + timedelta(seconds=250)
    done, hold = S.window(pieces, lambda d: (d["k"],), t + timedelta(seconds=250), min_event_s=5, merge_gap_s=60, min_session_s=0)
    assert [(s.start, s.end, s.events) for s in done] == [(t, t + timedelta(seconds=81), 2)]
    assert hold == t + timedelta(seconds=200)
    active = S.active_intervals([
        {"timestamp": iso(0), "duration": 10, "data": {"status": "not-afk"}},
        {"timestamp": iso(5), "duration": 10, "data": {"status": "not-afk"}},
        {"timestamp": iso(15), "duration": 10, "data": {"status": "afk"}},
        {"timestamp": iso(25), "duration": 10, "data": {"status": "not-afk"}},
    ], t, t + timedelta(seconds=100))
    assert active == [(t, t + timedelta(seconds=15)), (t + timedelta(seconds=25), t + timedelta(seconds=35))]
    cut = S.intersect(S.clip([{"timestamp": iso(0), "duration": 40, "data": {}}], t, t + timedelta(seconds=100)), active)
    assert [(p.start, p.end) for p in cut] == list(active)


def test_aw_unreachable_raises_and_bucket_failure_is_partial(env):
    env.delenv("ACTIVITYWATCH_FIXTURE")

    def down(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(httpx.ConnectError):
        collect(transport=httpx.MockTransport(down))

    good = fixture_transport(FIXTURE)

    def flaky(request):
        if "aw-watcher-vscode" in request.url.path:
            return httpx.Response(500)
        return good.handle_request(request)

    items = collect(transport=httpx.MockTransport(flaky))
    partial = [i for i in items if isinstance(i, Record) and i.kind == "coverage"]
    assert len(partial) == 1 and partial[0].data["channel"] == EDITOR and partial[0].data["reasons"] == ["source_error"]
    assert not any(m["channel"] == EDITOR for m in messages(items))

    def broken(request):
        if "/events" in request.url.path:
            return httpx.Response(500)
        return good.handle_request(request)

    with pytest.raises(RuntimeError):
        collect(transport=httpx.MockTransport(broken))


def test_aw_contract_kit():
    result = check_plugin(
        "throughline_activitywatch:ActivityWatchPlugin",
        {"ACTIVITYWATCH_FIXTURE": str(FIXTURE), "ACTIVITYWATCH_WINDOW_H": "12"},
    )
    assert result["messages"] >= 20
