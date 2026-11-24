"""Synthetic Oura and Weight Gurus data and a local fake of both APIs.

Everything here is invented. The fake server listens on 127.0.0.1 only, and
no test contacts a real service.
"""

import json
import threading
from types import SimpleNamespace
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

OURA_TOKEN = "fake-oura-access-canary"
WG_EMAIL = "fake-person@example.invalid"
WG_PASSWORD = "fake-weightgurus-password-canary"
WG_ACCESS = "fake-weightgurus-access-canary"

HEALTH_ENV = (
    "HEALTH_SOURCE",
    "HEALTH_PROVIDERS",
    "HEALTH_OURA_COLLECTIONS",
    "HEALTH_BACKFILL_START",
    "HEALTH_BACKFILL_END",
    "HEALTH_SYNC_OVERLAP_DAYS",
    "HEALTH_WINDOW_DAYS",
    "HEALTH_MAX_REQUESTS_PER_RUN",
    "HEALTH_MAX_REQUESTS_PER_DAY",
    "HEALTH_MIN_INTERVAL_S",
    "HEALTH_RETRIES",
    "HEALTH_BACKOFF_S",
    "HEALTH_WEIGHTGURUS_EVERY_H",
    "WEIGHTGURUS_UNIT",
    "WEIGHTGURUS_API_BASE",
    "WEIGHTGURUS_EMAIL",
    "WEIGHTGURUS_PASSWORD",
    "OURA_API_BASE",
    "OURA_TOKEN_FILE",
    "OURA_ALLOW_REFRESH",
    "OURA_TOKEN_URL",
    "OURA_ACCESS_TOKEN",
    "OURA_REFRESH_TOKEN",
    "OURA_CLIENT_ID",
    "OURA_CLIENT_SECRET",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """No owner credentials or settings leak into a test or its subprocesses."""
    for name in HEALTH_ENV:
        monkeypatch.delenv(name, raising=False)


def synthetic():
    days = [date(2024, 3, 29) + timedelta(days=i) for i in range(5)]
    data = {
        name: []
        for name in (
            "daily_sleep",
            "daily_readiness",
            "daily_activity",
            "daily_stress",
            "daily_spo2",
            "daily_resilience",
            "daily_cardiovascular_age",
            "vO2_max",
            "sleep",
            "session",
            "workout",
        )
    }
    for i, day in enumerate(days):
        d = day.isoformat()
        stamp = f"{d}T00:00:00-05:00"
        data["daily_sleep"].append(
            {
                "id": f"fake-daily-sleep-{i}",
                "day": d,
                "score": 70 + i,
                "timestamp": stamp,
                "contributors": {
                    "deep_sleep": 60 + i,
                    "efficiency": 90,
                    "latency": 80,
                    "rem_sleep": 75,
                    "restfulness": 70,
                    "timing": 85,
                    "total_sleep": 77,
                },
            }
        )
        data["daily_readiness"].append(
            {
                "id": f"fake-readiness-{i}",
                "day": d,
                "score": 65 + i,
                "timestamp": stamp,
                "temperature_deviation": round(-0.1 * (i + 1), 2),
                "temperature_trend_deviation": 0.05,
                "contributors": {
                    "activity_balance": 80,
                    "body_temperature": 90,
                    "hrv_balance": 70,
                    "previous_day_activity": 75,
                    "previous_night": 72,
                    "recovery_index": 68,
                    "resting_heart_rate": 88,
                    "sleep_balance": 79,
                },
            }
        )
        data["daily_activity"].append(
            {
                "id": f"fake-activity-{i}",
                "day": d,
                "score": 80 + i,
                "timestamp": f"{d}T04:00:00-05:00",
                "steps": 6000 + 1000 * i,
                "active_calories": 300 + i,
                "total_calories": 2300 + i,
                "target_calories": 450,
                "equivalent_walking_distance": 5000 + i,
                "high_activity_time": 600,
                "medium_activity_time": 1800,
                "low_activity_time": 7200,
                "sedentary_time": 30000,
                "resting_time": 28000,
                "non_wear_time": 0,
                "high_activity_met_minutes": 20,
                "medium_activity_met_minutes": 90,
                "low_activity_met_minutes": 150,
                "sedentary_met_minutes": 10,
                "average_met_minutes": 1.4375,
                "inactivity_alerts": 1,
                "meters_to_target": 1200,
                "target_meters": 9000,
                "class_5_min": "0011223344",
                "contributors": {
                    "meet_daily_targets": 60,
                    "move_every_hour": 95,
                    "recovery_time": 100,
                    "stay_active": 70,
                    "training_frequency": 80,
                    "training_volume": 90,
                },
            }
        )
        data["daily_stress"].append(
            {
                "id": f"fake-stress-{i}",
                "day": d,
                "stress_high": 900 * i,
                "recovery_high": 1800,
                "day_summary": "normal" if i % 2 else "restored",
            }
        )
        data["daily_spo2"].append(
            {
                "id": f"fake-spo2-{i}",
                "day": d,
                "breathing_disturbance_index": 3,
                "spo2_percentage": None if i == 4 else {"average": 96.5},
            }
        )
        data["daily_resilience"].append(
            {
                "id": f"fake-resilience-{i}",
                "day": d,
                "level": "solid",
                "contributors": {
                    "sleep_recovery": 70.5,
                    "daytime_recovery": 40.25,
                    "stress": 55.0,
                },
            }
        )
        # No id: the fetcher falls back to "<collection>:<day>".
        data["daily_cardiovascular_age"].append({"day": d, "vascular_age": 41})
        if i < 2:
            data["vO2_max"].append(
                {"id": f"fake-vo2-{i}", "day": d, "timestamp": stamp, "vo2_max": 42.5}
            )
        previous = (day - timedelta(days=1)).isoformat()
        data["sleep"].append(
            {
                "id": f"fake-sleep-{i}",
                "day": d,
                "type": "long_sleep",
                "period": 0,
                "bedtime_start": f"{previous}T23:10:00-05:00",
                "bedtime_end": f"{d}T06:40:00-05:00",
                "total_sleep_duration": 25200 + 60 * i,
                "time_in_bed": 27000,
                "deep_sleep_duration": 4800,
                "light_sleep_duration": 14100,
                "rem_sleep_duration": 6300,
                "awake_time": 1800,
                "latency": 600,
                "efficiency": 88,
                "average_heart_rate": 55.5,
                "lowest_heart_rate": 48,
                "average_hrv": 45,
                "average_breath": 14.5,
                "restless_periods": 200,
                "readiness": {
                    "score": 65 + i,
                    "temperature_deviation": -0.1,
                    "contributors": {"hrv_balance": 70},
                },
                "heart_rate": {
                    "interval": 300,
                    "items": [50, 52, None],
                    "timestamp": f"{previous}T23:10:00-05:00",
                },
                "movement_30_sec": "112233",
            }
        )
    data["sleep"].append(
        {
            "id": "fake-nap-1",
            "day": days[1].isoformat(),
            "type": "late_nap",
            "period": 1,
            "bedtime_start": f"{days[1]}T15:00:00-05:00",
            "bedtime_end": f"{days[1]}T15:25:00-05:00",
            "total_sleep_duration": 1200,
            "time_in_bed": 1500,
            "efficiency": 80,
        }
    )
    data["sleep"].append(
        {
            "id": "fake-deleted-sleep",
            "day": days[2].isoformat(),
            "type": "deleted",
            "period": 2,
            "bedtime_start": f"{days[2]}T13:00:00-05:00",
            "bedtime_end": f"{days[2]}T13:05:00-05:00",
        }
    )
    data["session"].append(
        {
            "id": "fake-session-1",
            "day": days[1].isoformat(),
            "type": "breathing",
            "mood": "good",
            "start_datetime": f"{days[1]}T12:00:00-05:00",
            "end_datetime": f"{days[1]}T12:10:00-05:00",
            "heart_rate": {
                "interval": 5,
                "items": [60, 62, None, 64],
                "timestamp": f"{days[1]}T12:00:00-05:00",
            },
            "heart_rate_variability": {
                "interval": 5,
                "items": [40, 42],
                "timestamp": f"{days[1]}T12:00:00-05:00",
            },
        }
    )
    data["workout"].append(
        {
            "id": "fake-workout-1",
            "day": days[0].isoformat(),
            "activity": "walking",
            "intensity": "moderate",
            "calories": 210.5,
            "distance": 3200.0,
            "source": "autodetected",
            "label": None,
            "start_datetime": f"{days[0]}T17:00:00-05:00",
            "end_datetime": f"{days[0]}T17:45:00-05:00",
        }
    )
    data["workout"].append(
        {
            "id": "fake-workout-2",
            "day": days[3].isoformat(),
            "activity": "cycling",
            "intensity": "hard",
            "calories": 480,
            "distance": 15000,
            "source": "manual",
            "label": "Invented ride",
            "start_datetime": f"{days[3]}T08:00:00-05:00",
            "end_datetime": f"{days[3]}T09:00:00-05:00",
        }
    )
    sources = ("sleep", "rest", "awake", "workout")
    data["heartrate"] = [
        {
            "bpm": 50 + h + 3 * k,
            "source": sources[(h // 2) % 4],
            "timestamp": f"{day}T{h:02d}:00:00+00:00",
        }
        for k, day in enumerate(("2024-03-30", "2024-03-31"))
        for h in range(0, 24, 2)
    ]
    entries = [
        f"2024-03-{29 + i:02d}T13:00:00.000Z"
        if i < 3
        else f"2024-04-0{i - 2}T13:00:00.000Z"
        for i in range(4)
    ]
    data["operations"] = [
        {
            "operationType": "create",
            "entryTimestamp": entry,
            "serverTimestamp": entry.replace("13:00:00", "13:00:05"),
            "weight": 1804 - 3 * i,
            "bodyFat": 0 if i == 3 else 243 - i,
            "muscleMass": 0 if i == 3 else 389,
            "water": 0 if i == 3 else 554,
            "bmi": 281,
            "source": "wifi scale",
        }
        for i, entry in enumerate(entries)
    ]
    data["operations"].append(
        {
            "operationType": "delete",
            "entryTimestamp": entries[1],
            "serverTimestamp": "2024-04-02T09:00:00.000Z",
            "weight": 1801,
            "source": "app",
        }
    )
    return data


class FakeServer:
    def __init__(self, data):
        self.data = data
        self.oura_tokens = {OURA_TOKEN}
        self.failures = {}
        self.requests = []
        self.page_size = 2
        self.refresh = None
        self.redirects = set()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                server.handle(self, "GET")

            def do_POST(self):
                server.handle(self, "POST")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def paths(self, prefix=""):
        return [r["path"] for r in self.requests if r["path"].startswith(prefix)]

    @staticmethod
    def reply(handler, status, body=None, headers=None):
        raw = json.dumps(body if body is not None else {}).encode()
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(raw)))
        for key, value in (headers or {}).items():
            handler.send_header(key, value)
        handler.end_headers()
        handler.wfile.write(raw)

    def handle(self, handler, method):
        parts = urlsplit(handler.path)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        length = int(handler.headers.get("Content-Length") or 0)
        body = handler.rfile.read(length) if length else b""
        auth = handler.headers.get("Authorization") or ""
        self.requests.append(
            {
                "method": method,
                "path": parts.path,
                "query": query,
                "auth": auth,
                "body": body,
            }
        )
        queue = self.failures.get(parts.path)
        if queue:
            status = queue.pop(0)
            return self.reply(
                handler,
                status,
                {"detail": "synthetic failure"},
                {"Retry-After": "0"} if status == 429 else None,
            )
        if parts.path in self.redirects:
            handler.send_response(302)
            handler.send_header("Location", "http://127.0.0.1:9/elsewhere")
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return
        if parts.path == "/v3/account/login" and method == "POST":
            answer = json.loads(body or b"{}")
            if (
                answer.get("email") == WG_EMAIL
                and answer.get("password") == WG_PASSWORD
            ):
                return self.reply(
                    handler,
                    200,
                    {"accessToken": WG_ACCESS, "expiresAt": "2099-01-01T00:00:00Z"},
                )
            return self.reply(handler, 401, {"message": "bad login"})
        if parts.path == "/v3/operation/" and method == "GET":
            if auth != "Bearer " + WG_ACCESS:
                return self.reply(handler, 401)
            return self.reply(handler, 200, {"operations": self.data["operations"]})
        if parts.path == "/oauth/token" and method == "POST":
            form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
            if (
                self.refresh
                and form.get("refresh_token") == self.refresh["refresh_token"]
            ):
                self.oura_tokens = {self.refresh["access_token"]}
                return self.reply(
                    handler,
                    200,
                    {
                        "access_token": self.refresh["access_token"],
                        "refresh_token": self.refresh["new_refresh_token"],
                        "expires_in": 86400,
                        "token_type": "bearer",
                    },
                )
            return self.reply(handler, 400, {"error": "invalid_grant"})
        prefix = "/v2/usercollection/"
        if parts.path.startswith(prefix) and method == "GET":
            if not auth.startswith("Bearer ") or auth[7:] not in self.oura_tokens:
                return self.reply(handler, 401, {"detail": "unauthorized"})
            name = parts.path[len(prefix) :]
            if name == "personal_info":
                return self.reply(handler, 200, {"id": "fake-user"})
            if name not in self.data:
                return self.reply(handler, 404, {"detail": "not found"})
            if name == "heartrate":
                low = datetime.fromisoformat(query["start_datetime"])
                high = datetime.fromisoformat(query["end_datetime"])
                if (high - low).days > 30:
                    return self.reply(handler, 400, {"detail": "range too long"})
                docs = [
                    d
                    for d in self.data[name]
                    if low <= datetime.fromisoformat(d["timestamp"]) < high
                ]
            else:
                # Exclusive end, the stricter reading of the API.
                docs = [
                    d
                    for d in self.data[name]
                    if query["start_date"] <= d["day"] < query["end_date"]
                ]
            offset = int(query.get("next_token") or 0)
            page = docs[offset : offset + self.page_size]
            following = offset + self.page_size
            return self.reply(
                handler,
                200,
                {
                    "data": page,
                    "next_token": str(following) if following < len(docs) else None,
                },
            )
        return self.reply(handler, 404, {"detail": "unknown route"})


@pytest.fixture
def data():
    return synthetic()


@pytest.fixture
def server(data):
    fake = FakeServer(data)
    try:
        yield fake
    finally:
        fake.close()


@pytest.fixture
def live_env(server):
    """Environment for live mode against the fake server."""
    return {
        "HEALTH_SOURCE": "live",
        "OURA_API_BASE": server.url,
        "WEIGHTGURUS_API_BASE": server.url,
        "OURA_TOKEN_URL": server.url + "/oauth/token",
        "OURA_ACCESS_TOKEN": OURA_TOKEN,
        "WEIGHTGURUS_EMAIL": WG_EMAIL,
        "WEIGHTGURUS_PASSWORD": WG_PASSWORD,
        "HEALTH_BACKFILL_START": "2024-03-25",
        "HEALTH_BACKFILL_END": "2024-04-05",
        "HEALTH_WINDOW_DAYS": "4",
        "HEALTH_MIN_INTERVAL_S": "0",
        "HEALTH_BACKOFF_S": "0",
    }


@pytest.fixture
def creds():
    return SimpleNamespace(
        oura=OURA_TOKEN, wg_email=WG_EMAIL, wg_password=WG_PASSWORD, wg_access=WG_ACCESS
    )


@pytest.fixture
def fake_env(server):
    """fetch.py environment (credentials and base URLs) for the fake server."""
    return {
        "OURA_ACCESS_TOKEN": OURA_TOKEN,
        "OURA_API_BASE": server.url,
        "WEIGHTGURUS_EMAIL": WG_EMAIL,
        "WEIGHTGURUS_PASSWORD": WG_PASSWORD,
        "WEIGHTGURUS_API_BASE": server.url,
    }
