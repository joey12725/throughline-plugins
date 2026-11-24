"""Export the owner's Home Assistant location history as JSON lines.

Stdlib only: copy it to the host where the Home Assistant credentials live
and run it there, so no secret leaves that host. The URL and token come from
HOMEASSISTANT_URL / HOMEASSISTANT_TOKEN, or, when HOMEASSISTANT_SECRET_COMMAND
is set, from that command run with the variable name as its last argument
(for a secret manager on the host). Writes one line per GPS fix:
{"at","lat","lon","accuracy_m","entity","source_type"}. Coordinates only;
no token, URL or other attribute is written.

The history API answers one day per request when no end time is given, so
the window is walked day by day. The person entity names the device tracker
that feeds it (attribute `source`); its history is far denser than the
person's own.
"""

import argparse
import json
import os
import shlex
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone


def secret(name):
    value = os.environ.get(name)
    if value:
        return value
    command = os.environ.get("HOMEASSISTANT_SECRET_COMMAND")
    if not command:
        raise SystemExit(f"set {name} (or HOMEASSISTANT_SECRET_COMMAND)")
    return subprocess.run(
        shlex.split(command) + [name],
        capture_output=True, text=True, timeout=30, check=True,
    ).stdout.strip()


def get(base, token, path):
    request = urllib.request.Request(base + path, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(request, timeout=90) as response:
        return json.load(response)


def stamp(value):
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fixes(series):
    for state in series:
        a = state.get("attributes") or {}
        if a.get("latitude") is None or a.get("longitude") is None:
            continue
        if a.get("source_type") not in (None, "gps"):
            continue
        yield dict(
            at=state.get("last_updated") or state.get("last_changed"),
            lat=float(a["latitude"]),
            lon=float(a["longitude"]),
            accuracy_m=float(a["gps_accuracy"]) if a.get("gps_accuracy") is not None else None,
            entity=state["entity_id"],
            source_type=a.get("source_type"),
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", required=True, help="ISO time; start of the window")
    parser.add_argument(
        "--person", required=True, help="person entity to export, e.g. person.alex"
    )
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    base = secret("HOMEASSISTANT_URL").rstrip("/")
    base = base if base.endswith("/api") else base + "/api"
    token = secret("HOMEASSISTANT_TOKEN")
    person = get(base, token, "/states/" + args.person)
    attributes = person.get("attributes") or {}
    entities = list(dict.fromkeys(
        [e for e in [attributes.get("source")] if e] + list(attributes.get("device_trackers") or [])
    )) or [args.person]
    since = datetime.fromisoformat(args.since.replace("Z", "+00:00"))
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    seen = set()
    count = 0
    with open(args.out, "w") as out:
        start = since
        while start < now:
            end = min(start + timedelta(days=1), now)
            path = "/history/period/{}?{}".format(
                urllib.parse.quote(stamp(start)),
                urllib.parse.urlencode(dict(filter_entity_id=",".join(entities), end_time=stamp(end))),
            )
            for series in get(base, token, path):
                for fix in fixes(series):
                    key = (fix["entity"], fix["at"])
                    if fix["at"] and key not in seen:
                        seen.add(key)
                        out.write(json.dumps(fix) + "\n")
                        count += 1
            start = end
    print(json.dumps(dict(entities=entities, points=count)))


if __name__ == "__main__":
    main()
