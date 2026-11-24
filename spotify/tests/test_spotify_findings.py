"""Regressions for reconciling recently-played rows with observed plays."""

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from throughline_sdk import Checkpoint, Record

from throughline_spotify import Spotify

ROOT = Path(__file__).parent / "recorded"
SESSION = json.loads((ROOT / "session.json").read_text())
TRACK = SESSION["recent"][0]["track"]
LENGTH = TRACK["duration_ms"]


@pytest.mark.parametrize("semantics", ["end", "start"])
def test_repeated_track_history_rows_never_become_extra_plays(
    monkeypatch, tmp_path, semantics
):
    """The same track twice in a row, then another track.

    A row whose played_at is the boundary between the two plays fits both the
    completed observation and the pending one. Spotify does not say whether
    played_at is a start or an end, so both readings must yield two plays.
    """
    recording = tmp_path / "repeat.json"
    monkeypatch.setenv("SPOTIFY_RECORDING", str(recording))
    monkeypatch.delenv("SPOTIFY_EXPORT_PATH", raising=False)
    base = int(datetime.fromisoformat(SESSION["recent"][0]["played_at"]).timestamp())
    base *= 1000
    shift = LENGTH if semantics == "end" else 0
    rows = []
    for play in range(2):
        row = copy.deepcopy(SESSION["recent"][0])
        row["played_at"] = datetime.fromtimestamp(
            (base + play * LENGTH + shift) / 1000, UTC
        ).isoformat()
        rows.append(row)
    cursor, emitted, links = {}, [], []
    for elapsed in range(0, 2 * LENGTH + 45000, 15000):
        item = (
            TRACK
            if elapsed < 2 * LENGTH
            else dict(TRACK, uri="spotify:track:inventednext")
        )
        # Each row is published just after its played_at.
        published = [
            r
            for n, r in enumerate(rows)
            if elapsed >= n * LENGTH + shift + (0 if semantics == "end" else 15000)
        ]
        recording.write_text(
            json.dumps(
                dict(
                    now=base + elapsed + 400,
                    playback=dict(
                        item=item,
                        progress_ms=elapsed % LENGTH,
                        is_playing=True,
                        context=rows[0]["context"],
                    ),
                    recent=published,
                    playlists=SESSION["playlists"],
                )
            )
        )
        records = list(Spotify().records(cursor, "sync"))
        emitted.extend(
            r
            for r in records
            if isinstance(r, dict) and r["fields"]["track_uri"] == TRACK["uri"]
        )
        links.extend(r.data for r in records if isinstance(r, Record))
        cursor = [r.cursor for r in records if isinstance(r, Checkpoint)][-1]
    assert len(emitted) == 2
    assert len({r["external_id"] for r in emitted}) == 2
    starts = sorted(
        int(datetime.fromisoformat(r["happened_at"]).timestamp() * 1000)
        for r in emitted
    )
    assert abs(starts[0] - base) <= 5000
    assert abs(starts[1] - (base + LENGTH)) <= 5000
    # Both plays carry when they ended and the playlist they were played from.
    for row in emitted:
        end = int(datetime.fromisoformat(row["ended_at"]).timestamp() * 1000)
        start = int(datetime.fromisoformat(row["happened_at"]).timestamp() * 1000)
        assert abs(end - start - LENGTH) <= 5000
        assert row["fields"]["context_type"] == "playlist"
        assert row["fields"]["playlist_name"] == "Invented Playlist"
    played_from = [x for x in links if x["type"] == "played_from"]
    assert {x["from"]["external_id"] for x in played_from} >= {
        r["external_id"] for r in emitted
    }
    assert all(
        x["to"]["external_ref"] == rows[0]["context"]["uri"] for x in played_from
    )
    # The overlap window re-reads both rows on later polls: still nothing new.
    recording.write_text(json.dumps(dict(now=base + 2 * LENGTH + 120000, recent=rows)))
    replay = list(Spotify().records(cursor, "sync"))
    assert not [
        r
        for r in replay
        if isinstance(r, dict) and r["fields"]["track_uri"] == TRACK["uri"]
    ]
