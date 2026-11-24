"""Coalesce raw ActivityWatch events into sessions.

Raw watcher events are noisy: a window watcher emits a new event for every
title change, and quick alt-tabs leave one- or two-second blips. A session is
built in four steps, each a pure function so the result depends only on the
events from the window start onwards:

1. clip events to the window [lo, hi);
2. intersect with the host's not-afk time (when the host has an AFK bucket);
3. drop pieces shorter than ``min_event_s`` (blips);
4. merge consecutive pieces with the same key when the gap between them is
   at most ``merge_gap_s``.

A session is *finished* when nothing after the window end could still extend
it: it ends at least ``merge_gap_s`` before the window end. Unfinished
sessions are held back; the next window starts at the first of them, so a
session is only ever emitted once, complete, under a stable id (its start).
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


def parse(value):
    at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


@dataclass
class Piece:
    start: datetime
    end: datetime
    data: dict

    @property
    def seconds(self):
        return (self.end - self.start).total_seconds()


@dataclass
class Session:
    key: tuple
    start: datetime
    end: datetime
    data: dict
    active_s: float = 0.0
    events: int = 0
    pieces: list = field(default_factory=list)

    @property
    def duration_s(self):
        return (self.end - self.start).total_seconds()


def clip(events, lo, hi):
    out = []
    for event in events:
        try:
            start = parse(event["timestamp"])
            seconds = float(event.get("duration") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        end = start + timedelta(seconds=max(0.0, seconds))
        start, end = max(start, lo), min(end, hi)
        if end > start:
            out.append(Piece(start, end, event.get("data") or {}))
    out.sort(key=lambda p: (p.start, p.end))
    return out


def active_intervals(afk_events, lo, hi):
    """Merged [start, end) intervals the host was not AFK."""
    spans = [(p.start, p.end) for p in clip(afk_events, lo, hi) if p.data.get("status") == "not-afk"]
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def intersect(pieces, intervals):
    out = []
    j = 0
    for piece in pieces:
        while j < len(intervals) and intervals[j][1] <= piece.start:
            j += 1
        k = j
        while k < len(intervals) and intervals[k][0] < piece.end:
            start, end = max(piece.start, intervals[k][0]), min(piece.end, intervals[k][1])
            if end > start:
                out.append(Piece(start, end, piece.data))
            k += 1
    return out


def coalesce(pieces, key, *, min_event_s, merge_gap_s):
    sessions = []
    gap = timedelta(seconds=merge_gap_s)
    for piece in pieces:
        if piece.seconds < min_event_s:
            continue
        k = key(piece.data)
        current = sessions[-1] if sessions else None
        if current and current.key == k and piece.start - current.end <= gap:
            current.end = max(current.end, piece.end)
        else:
            current = Session(k, piece.start, piece.end, dict(piece.data))
            sessions.append(current)
        current.active_s += piece.seconds
        current.events += 1
        current.pieces.append(piece)
    return sessions


def window(pieces, key, hi, *, min_event_s, merge_gap_s, min_session_s):
    """(finished sessions to emit, start of the first unfinished one or None)."""
    gap = timedelta(seconds=merge_gap_s)
    sessions = coalesce(pieces, key, min_event_s=min_event_s, merge_gap_s=merge_gap_s)
    starts = [s.start for s in sessions if s.end + gap > hi]
    # A blip clipped at the window end may be longer than it looks.
    starts += [p.start for p in pieces if p.end >= hi]
    hold = min(starts) if starts else None
    done = [
        s
        for s in sessions
        if (hold is None or s.start < hold) and s.end + gap <= hi and s.active_s >= min_session_s
    ]
    return done, hold
