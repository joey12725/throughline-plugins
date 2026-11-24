"""Google Timeline (on-device export, Timeline.json) as a location track.

Observed fixes come from rawSignals positions and semanticSegments
timelinePath points. The export only samples sparsely, so the plugin also
fills a fixed grid (STEP seconds) wherever no observed point falls:

- inside a visit, at the visit's place (accuracy VISIT_ACCURACY_M); level-0
  visits win over their level-1 parents;
- inside an activity (a trip), linearly between the nearest known points or
  the trip's own start and end, with accuracy growing toward the middle.

Time outside every visit and trip stays empty, so no event is placed where
the export says nothing. All points are delivered under one source so stop
detection sees one continuous stream.
"""

import bisect
import json
import math
from datetime import datetime, timezone

from throughline_sdk import Checkpoint, Declaration, Manifest, Plugin, Record, run

SOURCE = "google_timeline"
STEP = 300
VISIT_ACCURACY_M = 100.0
TRIP_MIN_ACCURACY_M = 200.0


def _at(value):
    when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def _latlng(value):
    lat, lon = (float(part.strip().rstrip("°")) for part in value.split(","))
    return lat, lon


def _usable(lat, lon):
    return -90 <= lat <= 90 and -180 <= lon <= 180 and not (lat == 0 and lon == 0)


def _distance(a, b):
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    x = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(math.radians(b[1] - a[1]) / 2) ** 2
    return 6371000 * 2 * math.asin(min(1, math.sqrt(x)))


def points(export, max_accuracy_m=1000.0, step=STEP):
    return list(_points(export, max_accuracy_m, step))


def _points(export, max_accuracy_m, step):
    """(epoch_s, lat, lon, accuracy_m) sorted by time, one per instant."""
    observed = {}
    for signal in export.get("rawSignals") or []:
        p = signal.get("position")
        if not p or "LatLng" not in p or "timestamp" not in p:
            continue
        acc = p.get("accuracyMeters")
        if acc is not None and acc > max_accuracy_m:
            continue
        lat, lon = _latlng(p["LatLng"])
        if _usable(lat, lon):
            t = _at(p["timestamp"]).timestamp()
            old = observed.get(t)
            if old is None or (acc is not None and (old[2] is None or acc < old[2])):
                observed[t] = (lat, lon, float(acc) if acc is not None else None)
    visits, trips = [], []
    for seg in export.get("semanticSegments") or []:
        for p in seg.get("timelinePath") or []:
            lat, lon = _latlng(p["point"])
            if _usable(lat, lon):
                observed.setdefault(_at(p["time"]).timestamp(), (lat, lon, None))
        start, end = _at(seg["startTime"]).timestamp(), _at(seg["endTime"]).timestamp()
        if end < start:
            continue
        visit = seg.get("visit")
        if visit:
            place = (visit.get("topCandidate") or {}).get("placeLocation", {}).get("latLng")
            if place:
                lat, lon = _latlng(place)
                if _usable(lat, lon):
                    visits.append((visit.get("hierarchyLevel") or 0, start, end, lat, lon))
        activity = seg.get("activity")
        if activity:
            ends = [activity.get(k, {}).get("latLng") for k in ("start", "end")]
            if all(ends):
                a, b = _latlng(ends[0]), _latlng(ends[1])
                if _usable(*a) and _usable(*b):
                    trips.append((start, end, a, b))

    slots = {round(t / step) for t in observed}
    filled = {}
    for _, start, end, lat, lon in sorted(visits):
        for slot in range(math.ceil(start / step), math.floor(end / step) + 1):
            if slot not in slots:
                slots.add(slot)
                filled[slot * step] = (lat, lon, VISIT_ACCURACY_M)

    known = sorted({**filled, **observed}.items())
    times = [t for t, _ in known]
    for start, end, a, b in trips:
        lo = bisect.bisect_left(times, start)
        hi = bisect.bisect_right(times, end)
        inside = [(start, a)] + [(times[i], known[i][1][:2]) for i in range(lo, hi)] + [(end, b)]
        stamps = [t for t, _ in inside]
        for slot in range(math.ceil(start / step), math.floor(end / step) + 1):
            if slot in slots:
                continue
            t = slot * step
            j = bisect.bisect_right(stamps, t)
            (t0, p0), (t1, p1) = inside[max(j - 1, 0)], inside[min(j, len(inside) - 1)]
            f = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
            lat, lon = p0[0] + (p1[0] - p0[0]) * f, p0[1] + (p1[1] - p0[1]) * f
            acc = max(TRIP_MIN_ACCURACY_M, _distance(p0, p1) * min(f, 1 - f))
            slots.add(slot)
            filled[t] = (lat, lon, round(acc, 1))

    merged = {**filled, **observed}
    for t in sorted(merged):
        yield (t, *merged[t])


class GoogleTimeline(Plugin):
    manifest = Manifest(
        name="google_timeline",
        version="0.1.0",
        channels=[],
        settings={
            "export": Declaration(
                env="GOOGLE_TIMELINE_EXPORT", required=True,
                description="Path to the Timeline.json exported from the phone",
            ),
            "max_accuracy_m": Declaration(
                env="GOOGLE_TIMELINE_MAX_ACCURACY_M", required=False, default="1000",
                description="Raw fixes reported less accurate than this are dropped",
            ),
        },
        run_mode="poll",
        default_interval_s=86400,
    )

    def records(self, cursor, mode):
        with open(self.settings["export"]) as f:
            export = json.load(f)
        latest = None
        fixes = points(export, float(self.settings["max_accuracy_m"]))
        total = len(fixes)
        for i, (t, lat, lon, acc) in enumerate(fixes, 1):
            at = datetime.fromtimestamp(t, timezone.utc).isoformat()
            yield Record(kind="track", data=dict(at=at, lat=lat, lon=lon, accuracy_m=acc, source=SOURCE))
            latest = at
            if i % 2000 == 0 or i == total:
                self.progress(done=i, total=total, unit="points")
        if latest:
            yield Checkpoint(cursor={"last_at": latest})


def main(argv=None):
    run(GoogleTimeline, argv)
