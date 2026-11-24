"""Home Assistant location plugin: the owner's GPS track.

Reads a JSON-lines export written by remote_fetch.py (run where the Home
Assistant credentials live) and delivers the fixes as track points under the
source `homeassistant`. The core places every life event within its
channel's location tolerance at the nearest point, including events that
arrived before the point did (late attachment), so records are tied to the
owner's location as both streams come in.
"""

import json
from datetime import datetime, timezone

from throughline_sdk import Checkpoint, Declaration, Manifest, Plugin, Record, run

SOURCE = "homeassistant"


def _at(value):
    when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


class HomeAssistant(Plugin):
    manifest = Manifest(
        name="homeassistant",
        version="0.1.0",
        channels=[],
        settings={
            "recording": Declaration(env="HOMEASSISTANT_RECORDING", description="JSONL export from remote_fetch.py"),
            "max_accuracy_m": Declaration(
                env="HOMEASSISTANT_MAX_ACCURACY_M", required=False, default="1000",
                description="Fixes reported less accurate than this are dropped",
            ),
        },
        run_mode="poll",
        default_interval_s=300,
    )

    def records(self, cursor, mode):
        limit = float(self.settings["max_accuracy_m"])
        last = cursor.get("last_at")
        latest = last
        with open(self.settings["recording"]) as f:
            for line in f:
                if not line.strip():
                    continue
                fix = json.loads(line)
                accuracy = fix.get("accuracy_m")
                if accuracy is not None and accuracy > limit:
                    continue
                if not (-90 <= fix["lat"] <= 90 and -180 <= fix["lon"] <= 180):
                    continue
                if fix["lat"] == 0 and fix["lon"] == 0:
                    continue
                at = _at(fix["at"]).isoformat()
                yield Record(kind="track", data=dict(at=at, lat=fix["lat"], lon=fix["lon"], accuracy_m=accuracy, source=SOURCE))
                latest = max(latest, at) if latest else at
        if latest:
            yield Checkpoint(cursor={"last_at": latest})


def main(argv=None):
    run(HomeAssistant, argv)
