"""Live Apple Health data through the Health Auto Export iOS app.

Health Auto Export's "REST API" automation POSTs JSON documents
(`{"data": {"metrics": [...], "workouts": [...]}}`) on a schedule.
`throughline-apple-health-receiver` accepts them and appends them to a spool
directory; this plugin reads the spool with a byte-offset cursor and maps
samples and workouts to messages shaped like the `health` connector's.
Workout GPS routes optionally become track points (source `apple_health`;
that needs a plugin token created with --location-source).
"""

import copy
import hashlib
import json
import logging

from throughline_sdk import Channel, Checkpoint, Declaration, Manifest, Plugin, Record, run

from . import mapping
from .mapping import MAPPING_REVISION
from .spool import read_spool

log = logging.getLogger(__name__)

DESCRIPTIONS = {
    mapping.METRICS: "Apple Health quantity samples (steps, energy, distance, HRV, resting heart rate, SpO2, respiratory rate, ...) as exported by Health Auto Export, one message per metric, time and source",
    mapping.HEART_RATE: "Apple Health heart rate samples (min/avg/max per export interval)",
    mapping.SLEEP: "Apple Health sleep: nightly summaries with stages, or individual stage segments when exported unaggregated",
    mapping.WEIGHT: "Apple Health body weight samples",
    mapping.WORKOUTS: "Apple Health workouts with energy, distance, heart rate and route summary",
}


def setting(env, default=None, description="", required=False):
    return Declaration(env=env, default=default, description=description, required=required)


class AppleHealth(Plugin):
    manifest = Manifest(
        name="apple_health",
        version="0.1.0",
        default_interval_s=3600,
        channels=[
            Channel(
                name=name,
                description=DESCRIPTIONS[name],
                fields=mapping.FIELDS[name],
                embed=name in (mapping.SLEEP, mapping.WORKOUTS),
                conversation_gap_s=3600 if name != mapping.WORKOUTS else 6 * 3600,
            )
            for name in mapping.CHANNELS
        ],
        settings={
            "spool_dir": setting("APPLE_HEALTH_SPOOL_DIR", None, "Spool directory written by throughline-apple-health-receiver", required=True),
            "metrics": setting("APPLE_HEALTH_METRICS", "", "Comma list of metric names to keep; empty keeps all"),
            "exclude_metrics": setting("APPLE_HEALTH_EXCLUDE_METRICS", "", "Comma list of metric names to drop"),
            "route_track": setting("APPLE_HEALTH_ROUTE_TRACK", "0", "1 sends workout GPS routes as track points (token needs --location-source)"),
            "max_accuracy_m": setting("APPLE_HEALTH_MAX_ACCURACY_M", "1000", "Route points less accurate than this are dropped"),
            "batch_lines": setting("APPLE_HEALTH_BATCH_LINES", "20", "Spool documents per checkpoint"),
        },
        run_mode="poll",
        link_types=[],
    )

    def __init__(self):
        super().__init__()
        keep = {m.strip() for m in (self.settings["metrics"] or "").split(",") if m.strip()}
        drop = {m.strip() for m in (self.settings["exclude_metrics"] or "").split(",") if m.strip()}
        self.metrics_filter = lambda name: (not keep or name in keep) and name not in drop
        self.route_track = self.settings["route_track"] == "1"
        self.limit = float(self.settings["max_accuracy_m"])

    def records(self, cursor, mode):
        state = copy.deepcopy(cursor) if cursor else {}
        offsets = state.setdefault("spool", {})
        batch = max(1, int(self.settings["batch_lines"]))
        pending = {}
        sent = {}  # id -> digest of the message already yielded this run: exports overlap
        lines = 0
        for name, offset, record in read_spool(self.settings["spool_dir"], offsets):
            messages, points = mapping.map_document(record or {}, metrics_filter=self.metrics_filter)
            for message in messages:
                key = (message["channel"], message["external_id"])
                if sent.get(key) != digest(message):
                    pending[key] = message
            if self.route_track:
                for point in points:
                    if point["accuracy_m"] is None or point["accuracy_m"] <= self.limit:
                        yield Record(kind="track", data=point)
            offsets[name] = offset
            lines += 1
            if lines % batch == 0:
                yield from self.flush(pending, sent)
                yield Checkpoint(cursor=copy.deepcopy(state))
        if pending or lines % batch:
            yield from self.flush(pending, sent)
            yield Checkpoint(cursor=copy.deepcopy(state))

    @staticmethod
    def flush(pending, sent):
        for key, message in pending.items():
            sent[key] = digest(message)
            yield message
        pending.clear()


def digest(message):
    return hashlib.sha256(json.dumps(message, sort_keys=True, default=str).encode()).digest()


__all__ = ["AppleHealth", "MAPPING_REVISION", "main"]


def main(argv=None):
    run(AppleHealth, argv)
