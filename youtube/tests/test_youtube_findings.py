"""Regressions for the yt-dlp history baseline."""

import json
from pathlib import Path

import pytest
from throughline_sdk import Checkpoint, Record

from throughline_youtube import YouTube

REC = Path(__file__).parent / "recorded"


@pytest.fixture(autouse=True)
def clean_source_env(monkeypatch):
    for d in [*YouTube.manifest.settings.values(), *YouTube.manifest.secrets.values()]:
        monkeypatch.delenv(d.env, raising=False)


def collect(cursor=None):
    items = list(YouTube().records(cursor or {}, "sync"))
    return (
        [i for i in items if isinstance(i, dict) and i["channel"] == "youtube.history"],
        [i for i in items if isinstance(i, Record)],
        [i.cursor for i in items if isinstance(i, Checkpoint)][-1],
    )


def recording(monkeypatch, tmp_path, history):
    data = json.loads((REC / "live.json").read_text())
    data[":ythistory"] = history
    path = tmp_path / "recording.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("YOUTUBE_RECORDING", str(path))
    monkeypatch.setenv("YOUTUBE_YTDLP", "true")
    return path, data


def test_baselined_video_is_never_emitted_when_its_feed_entry_changes(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("YOUTUBE_TAKEOUT", str(REC / "takeout"))
    takeout, _, cursor = collect()
    assert takeout  # The export already supplied these views with real times.
    history = json.loads((REC / "live.json").read_text())[":ythistory"]
    path, data = recording(monkeypatch, tmp_path, history)
    views, _, cursor = collect(cursor)
    assert views == []
    # Title edit, new thumbnail and a counter: every payload field may drift.
    data[":ythistory"][0].update(
        title="Invented Orchard Tour (remastered)",
        thumbnail="https://example.invalid/new.jpg",
        view_count=12345,
    )
    data[":ythistory"][1]["channel"] = "Invented Lantern Renamed"
    path.write_text(json.dumps(data), encoding="utf-8")
    views, links, cursor = collect(cursor)
    assert views == []
    assert not [r for r in links if r.data["type"] == "consumed"]
    # A new ID is still reported, and an emitted view still versions normally.
    data[":ythistory"].insert(0, dict(id="FAKE_NEW", title="Invented new view"))
    path.write_text(json.dumps(data), encoding="utf-8")
    views, _, cursor = collect(cursor)
    assert [v["fields"]["video_id"] for v in views] == ["FAKE_NEW"]
    data[":ythistory"][0]["title"] = "Invented new view, retitled"
    path.write_text(json.dumps(data), encoding="utf-8")
    views, _, cursor = collect(cursor)
    assert [v["fields"]["video_title"] for v in views] == [
        "Invented new view, retitled"
    ]
    assert views[0]["external_id"] == "ytdlp:history:FAKE_NEW"
    assert collect(cursor)[0] == []


def test_cursor_from_before_the_baseline_marker_keeps_emitting(monkeypatch, tmp_path):
    history = json.loads((REC / "live.json").read_text())[":ythistory"]
    path, data = recording(monkeypatch, tmp_path, history)
    _, _, cursor = collect()
    # What the earlier code left behind: emitted observations, no marker.
    del cursor["history_baselined"]
    for entry in cursor["snapshots"]["ytdlp:history"].values():
        entry.pop("baseline")
    data[":ythistory"].insert(0, dict(id="FAKE_UPGRADE", title="Invented upgrade"))
    path.write_text(json.dumps(data), encoding="utf-8")
    views, links, cursor = collect(cursor)
    assert [v["fields"]["video_id"] for v in views] == ["FAKE_UPGRADE"]
    assert [r.data["type"] for r in links if r.data["type"] == "consumed"]
    assert collect(cursor)[0] == []


def test_baseline_resumed_from_a_mid_pass_checkpoint_stays_silent(
    monkeypatch, tmp_path
):
    history = [dict(id=f"FAKE_{n:03}", title=f"Invented {n}") for n in range(150)]
    recording(monkeypatch, tmp_path, history)
    events = list(YouTube().records({}, "sync"))
    partial = [
        e.cursor
        for e in events
        if isinstance(e, Checkpoint)
        and len(e.cursor["snapshots"].get("ytdlp:history", {})) == 100
        and not e.cursor.get("history_baselined")
    ]
    assert partial
    views, _, cursor = collect(partial[0])
    assert views == []
    assert len(cursor["snapshots"]["ytdlp:history"]) == 150
    assert cursor["history_baselined"] and "history_baselining" not in cursor
