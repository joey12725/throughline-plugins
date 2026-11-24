import base64
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from throughline_sdk import Checkpoint, Record
from throughline_sdk.contract import check_plugin

from throughline_apple_health import AppleHealth, mapping
from throughline_apple_health.receiver import Receiver, make_server

RECORDED = Path(__file__).parent / "recorded" / "spool"
SECRET = "invented-health-secret-4567"


@pytest.fixture
def env(monkeypatch):
    for key in ("APPLE_HEALTH_METRICS", "APPLE_HEALTH_EXCLUDE_METRICS", "APPLE_HEALTH_ROUTE_TRACK", "APPLE_HEALTH_BATCH_LINES"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("APPLE_HEALTH_SPOOL_DIR", str(RECORDED))
    return monkeypatch


def collect(cursor=None):
    return list(AppleHealth().records(cursor or {}, "backfill"))


def messages(items):
    return [i for i in items if isinstance(i, dict)]


def by_id(items):
    return {m["external_id"]: m for m in messages(items)}


def test_ah_maps_every_channel_like_health(env):
    items = collect()
    rows = by_id(items)
    assert len(messages(items)) == len(rows) == 23  # one overlapping step sample deduplicated
    assert {m["channel"] for m in rows.values()} == set(mapping.CHANNELS)
    steps = rows["step_count:2024-03-01T16:00:00Z:Invented Phone"]
    assert steps["happened_at"] == "2024-03-01T08:00:00-08:00"
    assert steps["fields"] == {
        "provider": "health_auto_export", "mapping_revision": 1, "day": "2024-03-01",
        "source": "Invented Phone", "metric": "step_count", "value": 1430.0, "units": "count",
    }
    assert steps["thread"] == {"external_id": "step_count:2024-03", "title": "Step count 2024-03"}
    assert steps["content"] == "Step count: 1,430 count [Invented Phone]"
    assert "author" not in steps
    hr = rows["heart_rate:2024-03-01T16:00:00Z:Invented Watch"]
    assert hr["channel"] == mapping.HEART_RATE
    assert (hr["fields"]["bpm_min"], hr["fields"]["bpm_mean"], hr["fields"]["bpm_max"]) == (61, 78.2, 131)
    assert rows["heart_rate:2024-03-03T06:00:00Z:Invented Watch"]["content"] == "Heart rate 60 bpm [Invented Watch]"
    sleep = rows["sleep_analysis:2024-03-01T08:00:00Z:Invented Watch"]
    assert sleep["happened_at"] == "2024-02-29T23:10:00-08:00" and sleep["ended_at"] == "2024-03-01T06:40:00-08:00"
    f = sleep["fields"]
    assert (f["total_sleep_s"], f["deep_sleep_s"], f["rem_sleep_s"], f["core_sleep_s"], f["time_in_bed_s"]) == (26100, 4500, 7200, 14400, 28080)
    assert f["day"] == "2024-03-01" and f["aggregated"] is True and "asleep_s" in f
    assert sleep["content"].startswith("Slept 7h 15m (23:10-06:40): deep 1h 15m, REM 2h 00m, core 4h 00m")
    segment = rows["sleep_analysis:2024-03-02T09:00:00Z:Invented Watch:Deep"]
    assert segment["fields"]["stage"] == "Deep" and segment["fields"]["duration_s"] == 3600
    weight = rows["weight_body_mass:2024-03-01T14:55:00Z:Invented Scale"]
    assert weight["fields"]["weight_kg"] == 78.2 and weight["fields"]["weight_unit"] == "lb"
    assert rows["weight_body_mass:2024-03-03T06:05:00Z:Invented Scale"]["fields"]["weight_kg"] == 78.1
    bp = rows["blood_pressure:2024-03-01T17:30:00Z:Invented Cuff"]
    assert bp["content"] == "Blood pressure: 118/76 mmHg [Invented Cuff]"
    run = rows["workout:INVENTED-WORKOUT-0001"]
    assert run["ended_at"] == "2024-03-01T10:32:00-08:00"
    wf = run["fields"]
    assert (wf["duration_s"], wf["calories"], wf["distance_m"], wf["heart_rate_mean"], wf["route_points"]) == (1920, 341.2, 5020.0, 152, 6)
    assert wf["environment"] == "Outdoor" and wf["elevation_gain_m"] == 24
    assert "route" not in run["payload"] and run["payload"]["heartRateData"]
    assert run["location"] == {"lat": 51.4779, "lon": -0.0015, "accuracy_m": 5.0}
    assert run["content"] == "Workout: Outdoor Run, 32m, 341 kcal, 5.02 km, avg HR 152"
    lift = rows["workout:Traditional Strength Training:2024-03-03T17:00:00Z"]
    assert lift["fields"]["calories"] == 215.1 and lift["fields"]["environment"] == "Indoor"
    assert lift["fields"]["heart_rate_mean"] == 118 and lift["fields"]["duration_s"] == 2700
    # No track points unless asked for.
    assert not [i for i in items if isinstance(i, Record)]
    cursor = [i for i in items if isinstance(i, Checkpoint)][-1].cursor
    assert not messages(collect(cursor))


def test_ah_route_track_filters_and_batches(env):
    env.setenv("APPLE_HEALTH_ROUTE_TRACK", "1")
    env.setenv("APPLE_HEALTH_MAX_ACCURACY_M", "8")
    env.setenv("APPLE_HEALTH_BATCH_LINES", "1")
    items = collect()
    points = [i.data for i in items if isinstance(i, Record) and i.kind == "track"]
    assert [p["accuracy_m"] for p in points] == [5, 6, 7, 8]
    assert points[0] == {"at": "2024-03-01T18:00:00+00:00", "lat": 51.4779, "lon": -0.0015, "accuracy_m": 5, "source": "apple_health"}
    assert len([i for i in items if isinstance(i, Checkpoint)]) == 5


def test_ah_metric_filters(env):
    env.setenv("APPLE_HEALTH_METRICS", "step_count,heart_rate")
    env.setenv("APPLE_HEALTH_EXCLUDE_METRICS", "heart_rate")
    assert {m["fields"].get("metric") for m in messages(collect()) if m["channel"] != mapping.WORKOUTS} == {"step_count"}


def test_ah_revised_sample_is_a_new_version_in_a_later_batch(tmp_path, env):
    spool = tmp_path / "s"
    spool.mkdir()
    def sample(qty):
        return {"v": 1, "format": "health_auto_export", "body": {"data": {"metrics": [
            {"name": "step_count", "units": "count", "data": [{"date": "2024-03-01 00:00:00 -0800", "qty": qty, "source": "Invented Phone"}]}]}}}

    (spool / "health-2024-03-01.jsonl").write_text("\n".join(json.dumps(sample(q)) for q in (100, 100, 250)) + "\n")
    env.setenv("APPLE_HEALTH_SPOOL_DIR", str(spool))
    env.setenv("APPLE_HEALTH_BATCH_LINES", "1")
    values = [m["fields"]["value"] for m in messages(collect())]
    assert values == [100, 250]


def test_ah_mapping_edge_cases():
    assert mapping.instant("2024-03-01 07:30:00 -0800").isoformat() == "2024-03-01T07:30:00-08:00"
    assert mapping.instant("2024-03-01T07:30:00Z").isoformat() == "2024-03-01T07:30:00+00:00"
    assert mapping.instant("garbage") is None
    assert mapping.map_document({"body": {"data": {"metrics": [{"name": "step_count", "data": [{"date": "bad", "qty": 1}, {"date": "2024-03-01 00:00:00 +0000"}]}]}}}) == ([], [])
    assert mapping.map_document({"body": "nope"}) == ([], [])
    v1 = {"name": "Walk", "start": "2024-03-01 10:00:00 +0000", "end": "2024-03-01 10:30:00 +0000",
          "distance": {"qty": 2, "units": "mi"}, "route": [{"lat": 1.0, "lon": 2.0, "timestamp": "2024-03-01 10:01:00 +0000"}, {"lat": 0, "lon": 0, "timestamp": "2024-03-01 10:02:00 +0000"}]}
    message, points = mapping.workout_message(v1)
    assert message["fields"]["distance_m"] == 3218.7 and message["fields"]["duration_s"] == 1800
    assert len(points) == 1 and message["fields"]["route_points"] == 2


# ----------------------------------------------------------------- receiver


@pytest.fixture
def server(tmp_path):
    spool = tmp_path / "spool"
    receiver = Receiver(spool, SECRET, clock=lambda: datetime(2024, 3, 5, 8, 0, tzinfo=timezone.utc))
    httpd = make_server(receiver, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    host, port = httpd.server_address[:2]
    try:
        yield f"http://{host}:{port}", spool
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_ah_receiver_round_trip(server, env):
    url, spool = server
    document = json.loads((RECORDED / "health-2024-03-02.jsonl").read_text())["body"]
    with httpx.Client(base_url=url) as http:
        assert http.get("/healthz").text == "ok"
        r = http.post("/", json=document, headers={"Authorization": f"Bearer {SECRET}", "automation-name": "Invented hourly"})
        assert r.status_code == 200 and r.json() == {"status": "ok"}
        basic = "Basic " + base64.b64encode(f"anyone:{SECRET}".encode()).decode()
        assert http.post("/hae", json=document, headers={"Authorization": basic}).status_code == 200
        assert http.post("/", json=document).status_code == 401
        assert http.post("/", json=document, headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert http.post("/", json=document, params={"token": SECRET}).status_code == 401
        auth = {"Authorization": f"Bearer {SECRET}"}
        assert http.post("/", json={"data": {}}, headers=auth).status_code == 400
        assert http.post("/", json={"data": {"metrics": [1]}}, headers=auth).status_code == 400
        assert http.post("/", content=b"{", headers=auth).status_code == 400
        assert http.get("/", headers=auth).status_code == 405
    (path,) = spool.glob("health-*.jsonl")
    assert path.name == "health-2024-03-05.jsonl"
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(lines) == 2 and lines[0]["automation"] == "Invented hourly"
    assert SECRET not in path.read_text()
    env.setenv("APPLE_HEALTH_SPOOL_DIR", str(spool))
    assert len(messages(collect())) == 5  # the second post repeats the first


def test_ah_receiver_query_token_and_size(tmp_path):
    receiver = Receiver(tmp_path, SECRET, allow_query_token=True, max_body=50)
    body = json.dumps({"data": {"workouts": []}}).encode()
    assert receiver.handle("POST", f"/?token={SECRET}", {}, body)[0] == 200
    assert receiver.handle("POST", f"/?token={SECRET}", {}, b" " * 60)[0] == 413
    with pytest.raises(ValueError):
        Receiver(tmp_path, None)


def test_ah_missing_spool_raises(env, tmp_path):
    env.setenv("APPLE_HEALTH_SPOOL_DIR", str(tmp_path / "absent"))
    with pytest.raises(FileNotFoundError):
        collect()


def test_ah_contract_kit():
    result = check_plugin(
        "throughline_apple_health:AppleHealth",
        {"APPLE_HEALTH_SPOOL_DIR": str(RECORDED), "APPLE_HEALTH_BATCH_LINES": "1"},
    )
    assert result["messages"] == 23
