import base64
import json
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from throughline_sdk import Checkpoint, Record
from throughline_sdk.contract import check_plugin

from throughline_gps_push import GpsPush, mapping
from throughline_gps_push.receiver import Receiver, make_server
from throughline_gps_push.spool import read_spool

RECORDED = Path(__file__).parent / "recorded" / "spool"
SECRET = "invented-receiver-secret-0123"


@pytest.fixture
def env(monkeypatch):
    for key in ("GPS_PUSH_SPOOL_DIR", "GPS_PUSH_RECORDER_URL", "GPS_PUSH_DEVICES", "GPS_PUSH_BATCH_LINES"):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def run_records(plugin, cursor=None):
    return list(plugin.records(cursor or {}, "backfill"))


def tracks(items):
    return [i.data for i in items if isinstance(i, Record) and i.kind == "track"]


def messages(items):
    return [i for i in items if isinstance(i, dict)]


def test_gps_spool_backfill_maps_every_format(env):
    env.setenv("GPS_PUSH_SPOOL_DIR", str(RECORDED))
    items = run_records(GpsPush())
    points = tracks(items)
    # 6 OwnTracks fixes (one 5 km fix dropped), 6 Overland, 2 GPSLogger
    assert len(points) == 14
    assert all(p["source"] == "gps_push" for p in points)
    assert all(datetime.fromisoformat(p["at"]).tzinfo for p in points)
    assert {"at": "2024-01-02T15:00:00+00:00", "lat": 51.4779, "lon": -0.0015, "accuracy_m": 15.0, "source": "gps_push"} in points
    assert any(p["at"] == "2024-01-02T19:05:00+00:00" for p in points)  # epoch GPSLogger
    by_id = {m["external_id"]: m for m in messages(items)}
    transitions = [m for m in by_id.values() if m["channel"] == mapping.TRANSITIONS]
    regions = [m for m in by_id.values() if m["channel"] == mapping.REGIONS]
    assert len(regions) == 3
    # 5 OwnTracks transitions (a retried push deduplicated) + 2 Overland visits
    assert len(transitions) == 7
    assert len(messages(items)) == len(by_id)
    leave = next(m for m in transitions if m["content"] == "Left Invented Home")
    assert leave["fields"]["event"] == "leave" and leave["fields"]["device"] == "invented/phone"
    assert leave["location"] == {"lat": 51.4779, "lon": -0.0015, "accuracy_m": 12.0}
    visit = by_id["overland:invented-ipad:visit:2024-01-02T16:00:00+00:00"]
    assert visit["ended_at"] == "2024-01-02T17:15:00+00:00"
    still = by_id["overland:invented-ipad:visit:2024-01-02T18:00:00+00:00"]
    assert still["ended_at"] is None and "still there" in still["content"]
    home = by_id["owntracks:invented/phone:a1b2c3"]
    assert home["fields"]["radius_m"] == 100 and home["fields"]["center_lat"] == 51.4779
    cursor = [i for i in items if isinstance(i, Checkpoint)][-1].cursor
    assert set(cursor["spool"]) == {"gps-2024-01-01.jsonl", "gps-2024-01-02.jsonl"}
    # Resuming at the cursor yields nothing new.
    assert not tracks(run_records(GpsPush(), cursor)) and not messages(run_records(GpsPush(), cursor))


def test_gps_small_batches_checkpoint_and_resume(env, tmp_path):
    spool = tmp_path / "spool"
    shutil.copytree(RECORDED, spool)
    env.setenv("GPS_PUSH_SPOOL_DIR", str(spool))
    env.setenv("GPS_PUSH_BATCH_LINES", "3")
    items = run_records(GpsPush())
    checkpoints = [i for i in items if isinstance(i, Checkpoint)]
    assert len(checkpoints) >= 5
    # Resume from a middle checkpoint gives the remainder only.
    middle = checkpoints[2].cursor
    rest = run_records(GpsPush(), middle)
    seen_after = {m["external_id"] for m in messages(rest)}
    total = {m["external_id"] for m in messages(items)}
    assert seen_after < total and seen_after


def test_gps_partial_line_waits_and_shrunk_file_rereads(tmp_path):
    spool = tmp_path / "s"
    spool.mkdir()
    path = spool / "gps-2024-01-01.jsonl"
    path.write_text('{"a":1}\n{"b":')
    rows = list(read_spool(spool, {}))
    assert [r[2] for r in rows] == [{"a": 1}]
    offset = rows[-1][1]
    with open(path, "a") as f:
        f.write('2}\nnot json\n')
    rows = list(read_spool(spool, {path.name: offset}))
    assert [r[2] for r in rows] == [{"b": 2}, None]
    path.write_text('{"c":3}\n')
    assert [r[2] for r in read_spool(spool, {path.name: 999})] == [{"c": 3}]


def test_gps_device_allowlist_and_accuracy(env):
    env.setenv("GPS_PUSH_SPOOL_DIR", str(RECORDED))
    env.setenv("GPS_PUSH_DEVICES", "invented-android")
    env.setenv("GPS_PUSH_MAX_ACCURACY_M", "7")
    items = run_records(GpsPush())
    assert [p["accuracy_m"] for p in tracks(items)] == [6.0]
    assert not messages(items)


def test_gps_mapping_edge_cases():
    record = {"format": "owntracks", "user": None, "device": None, "body": [
        {"_type": "location", "lat": 0, "lon": 0, "tst": 1704100000},
        {"_type": "location", "lat": 95, "lon": 1, "tst": 1704100000},
        {"_type": "location", "lat": 1.5, "lon": 2.5, "tst": 1704100000000, "topic": "owntracks/someone/tablet"},
        {"_type": "transition", "event": "bogus", "tst": 1704100000},
        {"_type": "card", "name": "Invented"},
    ]}
    out = list(mapping.map_record(record))
    assert out == [("someone/tablet", "track", {"at": "2024-01-01T09:06:40+00:00", "lat": 1.5, "lon": 2.5, "accuracy_m": None, "source": "gps_push"})]
    assert mapping.instant("2017-01-01T10:00:00-0700").isoformat() == "2017-01-01T17:00:00+00:00"
    assert mapping.instant("2024-03-01 07:30:00 -0800").isoformat() == "2024-03-01T15:30:00+00:00"
    unknown_arrival = {"format": "overland", "body": {"locations": [{"type": "Feature", "geometry": {"coordinates": [1, 1]},
        "properties": {"type": "visit", "arrival_date": "0001-01-01T00:00:00Z", "departure_date": "2024-01-02T10:00:00Z", "device_id": "d"}}]}}
    (_, kind, message), = mapping.map_record(unknown_arrival)
    assert message["content"].startswith("Left a place") and message["happened_at"] == "2024-01-02T10:00:00+00:00"


# ----------------------------------------------------------------- receiver


@pytest.fixture
def server(tmp_path):
    spool = tmp_path / "spool"
    clock = iter(datetime(2024, 1, 3, 8, m, tzinfo=timezone.utc) for m in range(60))
    receiver = Receiver(spool, SECRET, clock=lambda: next(clock))
    httpd = make_server(receiver, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    try:
        yield f"http://{host}:{port}", spool, receiver
    finally:
        httpd.shutdown()
        httpd.server_close()


def basic(user, password):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def test_gps_receiver_accepts_all_formats_then_plugin_reads(server, env):
    url, spool, receiver = server
    bearer = {"Authorization": f"Bearer {SECRET}"}
    with httpx.Client(base_url=url) as http:
        assert http.get("/healthz").text == "ok"
        r = http.post("/owntracks", json={"_type": "location", "lat": 51.5, "lon": -0.1, "tst": 1704260000, "acc": 5},
                      headers={"Authorization": basic("invented", SECRET), "X-Limit-D": "phone"})
        assert r.status_code == 200 and r.json() == []
        r = http.post("/overland", headers=bearer, json={"locations": [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [-0.1, 51.5]},
                      "properties": {"timestamp": "2024-01-03T08:00:00Z", "horizontal_accuracy": 10, "device_id": "invented-ipad"}}]})
        assert r.status_code == 200 and r.json() == {"result": "ok"}
        r = http.get("/gpslogger", headers=bearer, params={"lat": "51.51", "lon": "-0.11", "acc": "4", "time": "2024-01-03T08:05:00Z", "device": "invented-android"})
        assert r.status_code == 200 and r.text == "OK"
        r = http.post("/gpslogger", headers=bearer, data={"lat": "51.52", "lon": "-0.12", "timestamp": "1704269400"})
        assert r.status_code == 200
        r = http.post("/", headers=bearer, json={"_type": "transition", "event": "enter", "desc": "Invented Cafe", "tst": 1704261000, "lat": 51.5, "lon": -0.1})
        assert r.status_code == 200 and r.json() == []
        # Rejections
        assert http.post("/owntracks", json={"_type": "location"}).status_code == 401
        assert http.post("/owntracks", json={"_type": "location"}, headers={"Authorization": "Bearer nope"}).status_code == 401
        assert http.post("/owntracks", json={"_type": "location"}, headers={"Authorization": basic("x", "nope")}).status_code == 401
        assert http.get("/gpslogger", params={"lat": "1", "lon": "1", "token": SECRET}).status_code == 401
        assert http.post("/", headers=bearer, json={"hello": "world"}).status_code == 400
        assert http.post("/overland", headers=bearer, json={"locations": "no"}).status_code == 400
        assert http.post("/gpslogger", headers=bearer, data={"lat": "x"}).status_code == 400
        assert http.post("/owntracks", headers={**bearer, "Content-Type": "application/json"}, content=b"{not json").status_code == 400
    lines = [json.loads(line) for f in sorted(spool.iterdir()) for line in f.read_text().splitlines()]
    assert len(lines) == 5
    assert lines[0]["user"] == "invented" and lines[0]["device"] == "phone"
    assert all(SECRET not in json.dumps(line) for line in lines)
    env.setenv("GPS_PUSH_SPOOL_DIR", str(spool))
    items = run_records(GpsPush())
    assert len(tracks(items)) == 4
    (transition,) = messages(items)
    assert transition["content"] == "Entered Invented Cafe" and transition["fields"]["device"] == "unknown"


def test_gps_receiver_query_token_opt_in(tmp_path):
    receiver = Receiver(tmp_path, SECRET, allow_query_token=True)
    status, _, body = receiver.handle("GET", f"/gpslogger?lat=1.5&lon=2.5&token={SECRET}", {}, b"")
    assert status == 200 and body == b"OK"
    (line,) = (tmp_path).glob("gps-*.jsonl")
    assert SECRET not in line.read_text()
    assert receiver.handle("POST", "/owntracks", {"Authorization": f"Bearer {SECRET}"}, b"x" * (6 * 1024 * 1024))[0] == 413
    with pytest.raises(ValueError):
        Receiver(tmp_path, "")


# ----------------------------------------------------------------- recorder


def recorder_transport(calls):
    def handler(request):
        calls.append(request)
        assert request.headers["Authorization"] == basic("invented", "invented-recorder-pw")
        params = dict(request.url.params)
        if request.url.path == "/api/0/list":
            if "user" in params:
                return httpx.Response(200, json={"results": ["phone"] if params["user"] == "invented" else ["tablet"]})
            return httpx.Response(200, json={"results": ["invented", "relative"]})
        assert request.url.path == "/api/0/locations"
        start = datetime.fromisoformat(params["from"] + "+00:00").timestamp()
        end = datetime.fromisoformat(params["to"] + "+00:00").timestamp()
        rows = [
            {"_type": "location", "lat": 51.4 + i / 100, "lon": -0.1, "tst": 1704067200 + i * 86400, "acc": 20, "tid": "ip"}
            for i in range(10)
        ]
        rows.append({"_type": "location", "lat": 51.4, "lon": -0.1, "tst": 1704067200 + 3600, "acc": 4000})
        data = [r for r in rows if start <= r["tst"] <= end]
        return httpx.Response(200, json={"count": len(data), "data": data})

    return httpx.MockTransport(handler)


def test_gps_recorder_poll(env):
    env.setenv("GPS_PUSH_RECORDER_URL", "https://recorder.example.com")
    env.setenv("GPS_PUSH_RECORDER_USER", "invented")
    env.setenv("GPS_PUSH_RECORDER_PASSWORD", "invented-recorder-pw")
    env.setenv("GPS_PUSH_RECORDER_SINCE", "2024-01-01")
    env.setenv("GPS_PUSH_RECORDER_WINDOW_DAYS", "4")
    env.setenv("GPS_PUSH_DEVICES", "invented/phone")
    now = datetime(2024, 1, 12, tzinfo=timezone.utc)
    calls = []
    plugin = GpsPush(transport=recorder_transport(calls), now=lambda: now)
    items = run_records(plugin)
    points = tracks(items)
    assert len(points) == 10 and points[0]["at"] == "2024-01-01T00:00:00+00:00"
    assert not any("tablet" in str(c.url) for c in calls if c.url.path == "/api/0/locations")
    cursor = [i for i in items if isinstance(i, Checkpoint)][-1].cursor
    assert cursor["recorder"] == {"invented/phone": "2024-01-10T00:00:00+00:00"}
    # Sync re-reads only the overlap before the newest point.
    calls.clear()
    again = run_records(GpsPush(transport=recorder_transport(calls), now=lambda: now), cursor)
    assert [p["at"] for p in tracks(again)] == ["2024-01-09T00:00:00+00:00", "2024-01-10T00:00:00+00:00"]
    assert len([c for c in calls if c.url.path == "/api/0/locations"]) == 1


def test_gps_recorder_unreachable_raises(env):
    env.setenv("GPS_PUSH_RECORDER_URL", "https://recorder.example.com")

    def down(request):
        raise httpx.ConnectError("unreachable")

    with pytest.raises(httpx.ConnectError):
        run_records(GpsPush(transport=httpx.MockTransport(down)))


def test_gps_needs_a_source(env):
    with pytest.raises(ValueError):
        GpsPush()


def test_gps_contract_kit():
    result = check_plugin(
        "throughline_gps_push:GpsPush",
        {"GPS_PUSH_SPOOL_DIR": str(RECORDED), "GPS_PUSH_BATCH_LINES": "2"},
    )
    assert result["messages"] == 10
