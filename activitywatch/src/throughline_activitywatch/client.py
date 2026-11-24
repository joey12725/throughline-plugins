"""ActivityWatch REST client (aw-server / aw-server-rust, /api/0).

`fixture_transport(path)` serves a recorded JSON document
(`{"buckets": {id: bucket}, "events": {id: [event, ...]}}`) with the same
query semantics as aw-server, for tests and offline runs.
"""

import json
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from .sessions import parse


def _iso(at):
    return at.astimezone(timezone.utc).isoformat()


class ActivityWatch:
    def __init__(self, base_url, *, transport=None, timeout=60):
        self.http = httpx.Client(
            base_url=base_url.rstrip("/"),
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
            headers={"Accept": "application/json"},
        )

    def close(self):
        self.http.close()

    def buckets(self):
        response = self.http.get("/buckets/")
        response.raise_for_status()
        body = response.json()
        return body if isinstance(body, dict) else {b["id"]: b for b in body}

    def events(self, bucket_id, start, end):
        response = self.http.get(
            f"/buckets/{quote(bucket_id, safe='')}/events",
            params={"start": _iso(start), "end": _iso(end), "limit": "-1"},
        )
        response.raise_for_status()
        return response.json()


def fixture_transport(path):
    with open(path) as f:
        recorded = json.load(f)
    buckets = recorded.get("buckets") or {}
    events = recorded.get("events") or {}

    def handler(request):
        parts = request.url.path.rstrip("/").split("/")
        if parts[-1] == "buckets":
            return httpx.Response(200, json=buckets)
        if parts[-1] == "events" and parts[-3] == "buckets":
            from urllib.parse import unquote

            bucket = unquote(parts[-2])
            if bucket not in buckets:
                return httpx.Response(404, json={"message": "no such bucket"})
            params = request.url.params
            start = parse(params["start"]) if "start" in params else datetime.min.replace(tzinfo=timezone.utc)
            end = parse(params["end"]) if "end" in params else datetime.max.replace(tzinfo=timezone.utc)
            out = []
            for event in events.get(bucket, []):
                begin = parse(event["timestamp"])
                finish = begin.timestamp() + float(event.get("duration") or 0)
                # aw-server returns events that intersect [start, end].
                if begin <= end and finish >= start.timestamp():
                    out.append(event)
            out.sort(key=lambda e: parse(e["timestamp"]), reverse=True)
            limit = int(params.get("limit", "-1"))
            return httpx.Response(200, json=out if limit < 0 else out[:limit])
        return httpx.Response(404, json={"message": "not found"})

    return httpx.MockTransport(handler)
