from throughline_google_timeline import STEP, VISIT_ACCURACY_M, points


def seg(start, end, **kw):
    return dict(startTime=start, endTime=end, **kw)


def test_observed_points_win_and_visits_fill_the_grid():
    export = {
        "semanticSegments": [
            seg("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z",
                timelinePath=[{"point": "30.0°, -97.0°", "time": "2026-01-01T00:10:00Z"}]),
            seg("2026-01-01T00:00:00Z", "2026-01-01T00:30:00Z",
                visit={"hierarchyLevel": 0, "topCandidate": {"placeLocation": {"latLng": "30.1°, -97.1°"}}}),
            seg("2026-01-01T00:00:00Z", "2026-01-01T00:30:00Z",
                visit={"hierarchyLevel": 1, "topCandidate": {"placeLocation": {"latLng": "31.0°, -98.0°"}}}),
        ],
        "rawSignals": [{"position": {"LatLng": "30.2°, -97.2°", "accuracyMeters": 9,
                                     "timestamp": "2026-01-01T00:20:00.000-00:00"}}],
    }
    out = points(export)
    times = [p[0] for p in out]
    assert times == sorted(times) and len(set(times)) == len(times)
    base = out[0][0]
    assert all(b - a <= STEP for a, b in zip(times, times[1:]))
    assert (base + 600, 30.0, -97.0, None) in out
    assert (base + 1200, 30.2, -97.2, 9.0) in out
    assert all(p[1:3] == (30.1, -97.1) for p in out if p[3] == VISIT_ACCURACY_M)


def test_trips_interpolate_and_gaps_stay_empty():
    export = {"semanticSegments": [
        seg("2026-01-01T00:00:00Z", "2026-01-01T00:20:00Z",
            activity={"start": {"latLng": "30.0°, -97.0°"}, "end": {"latLng": "30.4°, -97.0°"}}),
    ]}
    out = points(export)
    assert [round(p[1], 3) for p in out] == [30.0, 30.1, 30.2, 30.3, 30.4]
    assert out[2][3] > out[1][3] >= 200
    assert list(points({"semanticSegments": [], "rawSignals": []})) == []
