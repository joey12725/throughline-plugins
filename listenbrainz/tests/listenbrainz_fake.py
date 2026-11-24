"""A fake ListenBrainz API over the invented recordings in recorded/.

It applies the real server's semantics (verified against the docs and the
server's listen store): min_ts and max_ts are exclusive and cannot be
combined; with only min_ts the `count` listens immediately after it are
returned; without min_ts the newest listens before max_ts (default now) are
returned; every page is newest first; count is capped at 1000; min_ts=0 counts
as unset. Feedback is newest-created first, paged by offset/count. A wrong
token is a 401, and /1/validate-token reports valid=false."""

import json
import re
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
TOKEN = "invented-listenbrainz-token-0000"
USER = "invented_listener"
NOW = 2**40


def load(name):
    return json.loads((HERE / "recorded" / name).read_text())


def error(code, text):
    return httpx.Response(code, json={"code": code, "error": text})


def handler(listens=None, feedback=None, requests=None, token=TOKEN):
    listens = load("listenbrainz_listens.json") if listens is None else listens
    feedback = load("listenbrainz_feedback.json") if feedback is None else feedback

    def handle(request):
        params = dict(request.url.params)
        path = request.url.path
        if requests is not None:
            requests.append(dict(params, path=path))
        authorised = request.headers.get("Authorization") == f"Token {token}"
        if path == "/1/validate-token":
            if not authorised:
                return httpx.Response(
                    200, json={"code": 200, "message": "Token invalid.", "valid": False}
                )
            return httpx.Response(
                200,
                json={"code": 200, "message": "Token valid.", "valid": True, "user_name": USER},
            )
        if not authorised:
            return error(401, "Invalid authorization token.")
        match = re.fullmatch(r"/1/user/([^/]+)/listens", path)
        if match:
            if match.group(1) != USER:
                return error(404, "Cannot find user")
            min_ts = int(params.get("min_ts") or 0)
            max_ts = int(params.get("max_ts") or 0)
            if min_ts and max_ts:
                return error(400, "You may only specify min_ts or max_ts, but not both.")
            count = min(1000, int(params.get("count") or 25))
            if min_ts:
                chosen = sorted(
                    (item for item in listens if item["listened_at"] > min_ts),
                    key=lambda item: item["listened_at"],
                )[:count]
                chosen.reverse()
            else:
                high = max_ts or NOW
                chosen = sorted(
                    (item for item in listens if item["listened_at"] < high),
                    key=lambda item: -item["listened_at"],
                )[:count]
            return httpx.Response(
                200,
                json={"payload": {"count": len(chosen), "user_id": USER, "listens": chosen}},
            )
        match = re.fullmatch(r"/1/feedback/user/([^/]+)/get-feedback", path)
        if match:
            if match.group(1) != USER:
                return error(404, "Cannot find user")
            score = params.get("score")
            items = [f for f in feedback if score is None or str(f["score"]) == score]
            items = sorted(items, key=lambda f: -f["created"])
            offset = int(params.get("offset") or 0)
            count = min(1000, int(params.get("count") or 25))
            chunk = items[offset : offset + count]
            if params.get("metadata") != "true":
                chunk = [{k: v for k, v in f.items() if k != "track_metadata"} for f in chunk]
            return httpx.Response(
                200,
                json={
                    "count": len(chunk),
                    "feedback": chunk,
                    "offset": offset,
                    "total_count": len(items),
                },
            )
        return error(404, "Not found")

    return handle


def transport():
    return httpx.MockTransport(handler())
