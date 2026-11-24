"""Phone GPS trackers as a Throughline location source.

Push mode: `throughline-gps-push-receiver` accepts OwnTracks (HTTP mode),
Overland and GPSLogger for Android requests and appends them to a spool
directory; this plugin reads the spool with a byte-offset cursor. Location
fixes become track points (source `gps_push`); OwnTracks region transitions
and Overland visits become messages in `gps_push.transitions`, OwnTracks
region definitions (waypoints) become reference records in `gps_push.regions`.

Poll mode: with GPS_PUSH_RECORDER_URL the plugin also reads location history
from an OwnTracks Recorder's HTTP API.

Writing track points needs a plugin token created with --location-source.
"""

import copy
import logging
from datetime import datetime, timedelta, timezone

import httpx
from throughline_sdk import Channel, Checkpoint, Declaration, Manifest, Plugin, Record, run

from . import mapping
from .mapping import REGIONS, SOURCE, TRANSITIONS
from .spool import read_spool

log = logging.getLogger(__name__)


def fields(**values):
    return [dict(name=k, type=v[0], description=v[1]) for k, v in values.items()]


COMMON = dict(
    device=("text", "Device key: OwnTracks user/device, Overland device_id, or GPSLogger device"),
    app=("text", "Tracker app: owntracks or overland"),
    mapping_revision=("integer", "Mapping revision that produced this record"),
)


def setting(env, default=None, description="", required=False):
    return Declaration(env=env, default=default, description=description, required=required)


class GpsPush(Plugin):
    manifest = Manifest(
        name="gps_push",
        version="0.1.0",
        channels=[
            Channel(
                name=TRANSITIONS,
                description="Region enter/leave events from OwnTracks and place visits from Overland (the owner's own phone)",
                fields=fields(
                    event=("text", "enter, leave or visit"),
                    region=("text", "OwnTracks region (waypoint) name"),
                    region_id=("text", "OwnTracks region id (rid)"),
                    trigger=("text", "OwnTracks trigger code (c circular region, b beacon, ...)"),
                    accuracy_m=("number", "Reported accuracy of the position, metres"),
                    arrived_at=("timestamp", "Overland visit arrival"),
                    departed_at=("timestamp", "Overland visit departure (absent while still there)"),
                    **COMMON,
                ),
                conversation_gap_s=6 * 3600,
                embed=False,
            ),
            Channel(
                name=REGIONS,
                description="OwnTracks regions (waypoints) defined on the owner's devices",
                fields=fields(
                    region=("text", "Region name"),
                    region_id=("text", "OwnTracks region id (rid)"),
                    radius_m=("number", "Geofence radius, metres"),
                    center_lat=("number", "Geofence centre latitude"),
                    center_lon=("number", "Geofence centre longitude"),
                    **COMMON,
                ),
                is_reference=True,
                embed=False,
            ),
        ],
        settings={
            "spool_dir": setting("GPS_PUSH_SPOOL_DIR", None, "Spool directory written by throughline-gps-push-receiver"),
            "recorder_url": setting("GPS_PUSH_RECORDER_URL", None, "OwnTracks Recorder base URL (e.g. https://recorder.example.com) to poll instead of / as well as the spool"),
            "recorder_user": setting("GPS_PUSH_RECORDER_USER", None, "HTTP Basic user for the Recorder (when behind an authenticating proxy)"),
            "recorder_since": setting("GPS_PUSH_RECORDER_SINCE", "2015-01-01", "First day read from the Recorder on an empty cursor"),
            "recorder_window_days": setting("GPS_PUSH_RECORDER_WINDOW_DAYS", "30", "Days per Recorder request; one checkpoint per window"),
            "recorder_overlap_h": setting("GPS_PUSH_RECORDER_OVERLAP_H", "24", "Hours re-read before the newest Recorder point, for late uploads"),
            "devices": setting("GPS_PUSH_DEVICES", "", "Comma list of device keys (user/device or device id) to keep; empty keeps all. Use it when other people's phones share the receiver or Recorder"),
            "max_accuracy_m": setting("GPS_PUSH_MAX_ACCURACY_M", "1000", "Fixes reported less accurate than this are dropped"),
            "batch_lines": setting("GPS_PUSH_BATCH_LINES", "500", "Spool lines per checkpoint"),
        },
        secrets={
            "recorder_password": setting("GPS_PUSH_RECORDER_PASSWORD", None, "HTTP Basic password for the Recorder"),
        },
        run_mode="poll",
        default_interval_s=300,
        link_types=[],
    )

    def __init__(self, transport=None, now=None):
        super().__init__()
        if not self.settings["spool_dir"] and not self.settings["recorder_url"]:
            raise ValueError("set GPS_PUSH_SPOOL_DIR and/or GPS_PUSH_RECORDER_URL")
        self.transport = transport
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.limit = float(self.settings["max_accuracy_m"])
        self.allowed = {d.strip() for d in (self.settings["devices"] or "").split(",") if d.strip()}

    def keep(self, device, kind, data):
        if self.allowed and device not in self.allowed and device.rsplit("/", 1)[-1] not in self.allowed:
            return False
        if kind == "track":
            accuracy = data.get("accuracy_m")
            return accuracy is None or accuracy <= self.limit
        return True

    def records(self, cursor, mode):
        state = copy.deepcopy(cursor) if cursor else {}
        if self.settings["spool_dir"]:
            yield from self.spool_records(state)
        if self.settings["recorder_url"]:
            yield from self.recorder_records(state)

    # ------------------------------------------------------------------ spool

    def spool_records(self, state):
        offsets = state.setdefault("spool", {})
        batch = max(1, int(self.settings["batch_lines"]))
        lines = 0
        messages = {}
        sent = {}  # id -> payload already yielded this run (retried pushes repeat)
        for name, offset, record in read_spool(self.settings["spool_dir"], offsets):
            for device, kind, data in mapping.map_record(record or {}):
                if not self.keep(device, kind, data):
                    continue
                if kind == "track":
                    yield Record(kind="track", data=data)
                else:
                    # A retried push repeats a message: keep one per id.
                    key = (data["channel"], data["external_id"])
                    if sent.get(key) != data:
                        messages[key] = data
            offsets[name] = offset
            lines += 1
            if lines % batch == 0:
                sent.update(messages)
                yield from messages.values()
                messages.clear()
                yield Checkpoint(cursor=copy.deepcopy(state))
        if messages or lines % batch:
            yield from messages.values()
            yield Checkpoint(cursor=copy.deepcopy(state))

    # --------------------------------------------------------------- recorder

    def client(self):
        auth = None
        if self.settings["recorder_user"]:
            auth = httpx.BasicAuth(self.settings["recorder_user"], self.secrets["recorder_password"] or "")
        return httpx.Client(
            base_url=self.settings["recorder_url"].rstrip("/"),
            auth=auth,
            transport=self.transport,
            timeout=60,
            follow_redirects=False,
        )

    def recorder_records(self, state):
        recorder = state.setdefault("recorder", {})
        window = timedelta(days=max(1, int(self.settings["recorder_window_days"])))
        overlap = timedelta(hours=float(self.settings["recorder_overlap_h"]))
        since = datetime.fromisoformat(self.settings["recorder_since"])
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        now = self.now()
        with self.client() as http:
            for user in _results(http.get("/api/0/list")):
                for device in _results(http.get("/api/0/list", params={"user": user})):
                    key = mapping.device_key(user, device)
                    if self.allowed and key not in self.allowed and device not in self.allowed:
                        continue
                    last = recorder.get(key)
                    start = mapping.instant(last) - overlap if last else since
                    emitted = None  # windows share their edges: never repeat a point
                    while start < now:
                        end = min(start + window, now)
                        response = http.get(
                            "/api/0/locations",
                            params={
                                "user": user,
                                "device": device,
                                "from": start.strftime("%Y-%m-%dT%H:%M:%S"),
                                "to": end.strftime("%Y-%m-%dT%H:%M:%S"),
                                "format": "json",
                            },
                        )
                        response.raise_for_status()
                        body = response.json()
                        rows = body.get("data") if isinstance(body, dict) else body
                        newest = None
                        for row in sorted(rows or [], key=lambda r: _tst(r)):
                            if not isinstance(row, dict) or row.get("_type", "location") != "location":
                                continue
                            fix = mapping.owntracks_location(row)
                            if fix and self.keep(key, "track", fix) and (emitted is None or fix["at"] > emitted):
                                emitted = fix["at"]
                                yield Record(kind="track", data=fix)
                                newest = max(newest, fix["at"]) if newest else fix["at"]
                        if newest and (not recorder.get(key) or newest > recorder[key]):
                            recorder[key] = newest
                            yield Checkpoint(cursor=copy.deepcopy(state))
                        start = end


def _tst(row):
    value = mapping._float(row.get("tst")) if isinstance(row, dict) else None
    return value if value is not None else 0


def _results(response):
    response.raise_for_status()
    body = response.json()
    results = body.get("results") if isinstance(body, dict) else body
    return [r for r in results or [] if isinstance(r, str)]


__all__ = ["GpsPush", "SOURCE", "main"]


def main(argv=None):
    run(GpsPush, argv)
