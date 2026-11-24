"""Regressions for polled Plex viewings: one message, observed start and end."""

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from throughline_sdk import Checkpoint
from throughline_plex import Plex

RECORDING = Path(__file__).parent / "recorded" / "responses.json"
T0 = 1600100000


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


class Server:
    """An invented Plex server whose sessions and history the test edits."""

    def __init__(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PLEX_SERVER_URL", "https://invented.invalid:32400")
        monkeypatch.setenv("PLEX_TOKEN", "invented-secret-canary")
        monkeypatch.setenv("PLEX_RECORDING", str(tmp_path / "server.json"))
        self.path = tmp_path / "server.json"
        self.data = json.loads(RECORDING.read_text())
        self.row = copy.deepcopy(self.data["history"]["MediaContainer"]["Metadata"][1])
        self.data["history"]["MediaContainer"]["Metadata"] = []
        self.clock = T0
        self.cursor = {}
        self.seen = []
        monkeypatch.setattr("throughline_plex.time.time", lambda: self.clock)

    def poll(self, at, offset=None, state="playing", history=None):
        self.clock = T0 + at
        self.data["/status/sessions"]["MediaContainer"]["Metadata"] = (
            []
            if offset is None
            else [
                dict(
                    ratingKey="102",
                    type="episode",
                    title="Invented episode 2",
                    viewOffset=offset * 1000,
                    User={"id": 9002, "title": "Invented Beta"},
                    Session={"id": "invented-session"},
                    Player={"title": "Invented TV", "state": state},
                )
            ]
        )
        if history is not None:
            self.data["history"]["MediaContainer"]["Metadata"] = [
                dict(self.row, viewedAt=T0 + history)
            ]
        self.path.write_text(json.dumps(self.data))
        records = list(Plex().records(self.cursor, "sync"))
        self.cursor = [r for r in records if isinstance(r, Checkpoint)][-1].cursor
        rows = [r for r in records if isinstance(r, dict)]
        self.seen.extend(rows)
        return rows


@pytest.fixture
def server(monkeypatch, tmp_path):
    return Server(monkeypatch, tmp_path)


def test_backward_seek_keeps_one_viewing(server):
    first = server.poll(0, 600)[0]
    # Skip back twice (20 s) during the next 30 s: 600 + 30 - 20.
    server.poll(30, 610)
    # A long rewind to rewatch a scene, far from the start.
    server.poll(60, 200)
    server.poll(90, 230)
    final = server.poll(120)
    assert {r["external_id"] for r in server.seen} == {first["external_id"]}
    assert final[0]["happened_at"] == iso(T0)
    assert final[0]["ended_at"] == iso(T0 + 90)
    assert final[0]["fields"]["view_offset_s"] == 230


def test_restart_from_the_beginning_is_a_new_viewing(server):
    first = server.poll(0, 2390)[0]
    # The item ended and began again inside one poll interval (repeat).
    second = server.poll(30, 20)
    assert [r["external_id"] for r in second][0] == first["external_id"]
    assert second[0]["ended_at"] == iso(T0)
    assert second[1]["external_id"] != first["external_id"]
    assert second[1]["happened_at"] == iso(T0 + 30) and second[1]["ended_at"] is None


def test_history_matched_while_live_keeps_observed_start_and_end(server):
    first = server.poll(0, 600)[0]
    # The played mark lands mid-viewing; playback goes on for another minute.
    matched = server.poll(30, 630, history=20)
    assert len(matched) == 1
    assert matched[0]["external_id"] == first["external_id"]
    assert matched[0]["happened_at"] == iso(T0)
    assert matched[0]["ended_at"] is None
    assert matched[0]["payload"]["history"]["viewedAt"] == T0 + 20
    assert matched[0]["payload"]["source"]["Session"] == {"id": "invented-session"}
    assert "played mark" in matched[0]["fields"]["timing_basis"]
    assert server.poll(60, 660)[0]["fields"]["view_offset_s"] == 660
    closed = server.poll(90)
    assert len(closed) == 1
    assert closed[0]["external_id"] == first["external_id"]
    assert closed[0]["happened_at"] == iso(T0)
    assert closed[0]["ended_at"] == iso(T0 + 60)
    assert closed[0]["payload"]["history"]["viewedAt"] == T0 + 20
    assert server.poll(120) == []
    assert {r["external_id"] for r in server.seen} == {first["external_id"]}


def test_history_matched_after_close_keeps_observed_start_and_end(server):
    first = server.poll(0, 600)[0]
    server.poll(30, 630)
    server.poll(60)
    late = server.poll(90, history=25)
    assert len(late) == 1
    assert late[0]["external_id"] == first["external_id"]
    assert late[0]["happened_at"] == iso(T0)
    assert late[0]["ended_at"] == iso(T0 + 30)
    assert late[0]["fields"]["view_offset_s"] == 630
    assert late[0]["fields"]["time_precision"] == "first_seen"
    assert late[0]["payload"]["source"]["viewedAt"] == T0 + 25
    assert server.poll(120) == []
    # A revised history row still reports the interval, even a day later.
    server.row["viewCount"] = 2
    revised = server.poll(90000, history=25)
    assert len(revised) == 1
    assert revised[0]["external_id"] == first["external_id"]
    assert (revised[0]["happened_at"], revised[0]["ended_at"]) == (
        iso(T0),
        iso(T0 + 30),
    )


def test_paused_session_ends_at_last_playing_poll(server):
    server.poll(0, 600)
    server.poll(30, 630)
    # Paused between polls, then left listed while paused.
    assert server.poll(60, 640, state="paused")[0]["ended_at"] is None
    server.poll(90, 640, state="paused")
    server.poll(120, 640, state="paused")
    closed = server.poll(150)
    assert closed[0]["happened_at"] == iso(T0)
    assert closed[0]["ended_at"] == iso(T0 + 60)
    assert len({r["external_id"] for r in server.seen}) == 1


def test_viewing_left_open_across_long_downtime_gets_its_end(server):
    first = server.poll(0, 600)[0]
    server.poll(30, 630)
    # The plugin is down for 25 h; the session is long gone when it returns.
    closed = server.poll(30 + 25 * 3600)
    assert len(closed) == 1
    assert closed[0]["external_id"] == first["external_id"]
    assert closed[0]["happened_at"] == iso(T0)
    assert closed[0]["ended_at"] == iso(T0 + 30)
    assert closed[0]["fields"]["view_offset_s"] == 630
    # Closed and past the horizon, it is pruned on the next run, silently.
    assert server.poll(60 + 25 * 3600) == []
    assert server.cursor["observed"] == {}


def test_session_still_listed_after_long_downtime_ends_the_old_viewing(server):
    first = server.poll(0, 600)[0]
    server.poll(30, 630)
    # Same session and item still listed 25 h later: a new viewing, and the
    # old one ends at its last playing poll rather than being dropped.
    rows = server.poll(30 + 25 * 3600, 900)
    assert [r["external_id"] for r in rows][0] == first["external_id"]
    assert rows[0]["ended_at"] == iso(T0 + 30)
    assert rows[1]["external_id"] != first["external_id"]
    assert rows[1]["happened_at"] == iso(T0 + 30 + 25 * 3600)
    assert rows[1]["ended_at"] is None
