import json

from throughline_homeassistant import HomeAssistant
from throughline_homeassistant.remote_fetch import fixes
from throughline_sdk import Checkpoint, Record


def write(tmp_path, rows):
    path = tmp_path / "fixes.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def test_fixes_keep_gps_coordinates_only():
    series = [
        {"entity_id": "device_tracker.p", "state": "home", "last_updated": "2026-11-20T10:00:00+00:00",
         "attributes": {"latitude": 30.1, "longitude": -97.7, "gps_accuracy": 12, "source_type": "gps", "friendly_name": "P"}},
        {"entity_id": "device_tracker.p", "state": "unavailable", "last_updated": "2026-11-20T10:01:00+00:00", "attributes": {}},
        {"entity_id": "device_tracker.p", "state": "home", "last_updated": "2026-11-20T10:02:00+00:00",
         "attributes": {"latitude": 30.1, "longitude": -97.7, "source_type": "router"}},
    ]
    out = list(fixes(series))
    assert out == [dict(at="2026-11-20T10:00:00+00:00", lat=30.1, lon=-97.7, accuracy_m=12.0,
                        entity="device_tracker.p", source_type="gps")]


def test_records_are_track_points_with_checkpoint(tmp_path, monkeypatch):
    path = write(tmp_path, [
        dict(at="2026-11-20T10:00:00.5+00:00", lat=30.1, lon=-97.7, accuracy_m=12.0),
        dict(at="2026-11-20T10:05:00Z", lat=30.2, lon=-97.8, accuracy_m=5000.0),
        dict(at="2026-11-20T10:06:00Z", lat=0, lon=0, accuracy_m=1.0),
        dict(at="2026-11-20T10:07:00-06:00", lat=30.3, lon=-97.9, accuracy_m=None),
    ])
    monkeypatch.setenv("HOMEASSISTANT_RECORDING", str(path))
    items = list(HomeAssistant().records({}, "sync"))
    tracks = [i.data for i in items if isinstance(i, Record)]
    assert [t["at"] for t in tracks] == ["2026-11-20T10:00:00.500000+00:00", "2026-11-20T16:07:00+00:00"]
    assert {t["source"] for t in tracks} == {"homeassistant"}
    assert isinstance(items[-1], Checkpoint)
    assert items[-1].cursor == {"last_at": "2026-11-20T16:07:00+00:00"}


def test_batches_track_points_into_one_request(tmp_path, monkeypatch):
    path = write(tmp_path, [dict(at=f"2026-11-20T10:0{i}:00Z", lat=30.0, lon=-97.0, accuracy_m=10.0) for i in range(5)])
    monkeypatch.setenv("HOMEASSISTANT_RECORDING", str(path))
    calls = []

    class FakeClient:
        def register(self, manifest): pass
        def coverage_attempt(self, channels): pass
        def health(self): return {}
        def cursor(self): return {}
        def track(self, points): calls.append(len(points))
        def checkpoint(self, cursor, key): calls.append(("checkpoint", cursor))
        def heartbeat(self, **kw): pass
        def coverage(self, items): return {}

    HomeAssistant().execute(FakeClient(), "sync")
    assert calls[0] == 5
    assert calls[1][0] == "checkpoint"
