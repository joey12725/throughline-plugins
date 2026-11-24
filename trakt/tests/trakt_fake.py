"""A fake Trakt API over the invented recordings in recorded/.

It applies the real API's semantics: trakt-api-key/version headers, Bearer
auth with 401 for unknown or expired tokens, refresh-token rotation, the
device-code flow, newest-first history with start_at/end_at (inclusive) and
page/limit paging reported in X-Pagination-* headers, optional paging on
ratings and the watchlist, and /sync/last_activities. Token validity is
stateless (by prefix) so tokens refreshed by one plugin process stay valid
in the next one."""

import json
import math
import time
from datetime import datetime
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
CLIENT_ID = "invented-trakt-client-id-0000"
CLIENT_SECRET = "invented-trakt-client-secret-0000"
ACCESS_PREFIX = "invented-access-"
REFRESH_PREFIX = "invented-refresh-"
EXPIRED_ACCESS = ACCESS_PREFIX + "expired-0000"
VALID_ACCESS = ACCESS_PREFIX + "valid-0000"
VALID_REFRESH = REFRESH_PREFIX + "valid-0000"
USER = {"username": "invented_viewer", "name": "Invented Viewer", "ids": {"slug": "invented_viewer"}}
DEVICE_CODE = "invented-device-code-0000"


def load(name):
    return json.loads((HERE / "recorded" / name).read_text())


def stamp(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def paged(items, params):
    if "page" not in params and "limit" not in params:
        return httpx.Response(200, json=items)
    limit = int(params.get("limit", 10))
    number = int(params.get("page", 1))
    pages = math.ceil(len(items) / limit) if items else 0
    return httpx.Response(
        200,
        json=items[(number - 1) * limit : number * limit],
        headers={
            "X-Pagination-Page": str(number),
            "X-Pagination-Limit": str(limit),
            "X-Pagination-Page-Count": str(pages),
            "X-Pagination-Item-Count": str(len(items)),
        },
    )


def handler(history=None, ratings=None, watchlist=None, requests=None, device_replies=None):
    history = load("trakt_history.json") if history is None else history
    ratings = load("trakt_ratings.json") if ratings is None else ratings
    watchlist = load("trakt_watchlist.json") if watchlist is None else watchlist
    state = {"issued": 0, "device": list(device_replies or [])}

    def token():
        state["issued"] += 1
        n = state["issued"]
        return {
            "access_token": f"{ACCESS_PREFIX}refreshed-{n:04}",
            "token_type": "bearer",
            "expires_in": 86400,
            "refresh_token": f"{REFRESH_PREFIX}refreshed-{n:04}",
            "scope": "public",
            "created_at": int(time.time()),
        }

    def handle(request):
        path = request.url.path
        params = dict(request.url.params)
        body = json.loads(request.content) if request.content else {}
        if requests is not None:
            requests.append({"method": request.method, "path": path, **params})
        if request.headers.get("trakt-api-version") != "2":
            return httpx.Response(400, json={"error": "missing api version"})
        if path.startswith("/oauth/"):
            if body.get("client_id") != CLIENT_ID:
                return httpx.Response(401, json={"error": "invalid_client"})
            if path == "/oauth/device/code":
                return httpx.Response(
                    200,
                    json={
                        "device_code": DEVICE_CODE,
                        "user_code": "INV3NT3D",
                        "verification_url": "https://trakt.tv/activate",
                        "expires_in": 600,
                        "interval": 5,
                    },
                )
            if body.get("client_secret") != CLIENT_SECRET:
                return httpx.Response(401, json={"error": "invalid_client"})
            if path == "/oauth/device/token":
                if body.get("code") != DEVICE_CODE:
                    return httpx.Response(404)
                status = state["device"].pop(0) if state["device"] else 200
                return httpx.Response(status, json=token() if status == 200 else {})
            if path == "/oauth/token":
                if body.get("grant_type") != "refresh_token" or not str(
                    body.get("refresh_token", "")
                ).startswith(REFRESH_PREFIX) or "revoked" in body["refresh_token"]:
                    return httpx.Response(401, json={"error": "invalid_grant"})
                return httpx.Response(200, json=token())
            return httpx.Response(404)
        if request.headers.get("trakt-api-key") != CLIENT_ID:
            return httpx.Response(403, json={"error": "invalid api key"})
        auth = request.headers.get("Authorization", "")
        bearer = auth.removeprefix("Bearer ")
        if (
            not auth.startswith("Bearer ")
            or not bearer.startswith(ACCESS_PREFIX)
            or "expired" in bearer
            or "revoked" in bearer
        ):
            return httpx.Response(401, json={"error": "invalid_token"})
        if path == "/users/settings":
            return httpx.Response(200, json={"user": USER, "account": {"timezone": "UTC"}})
        if path.startswith("/sync/history"):
            kind = path.removeprefix("/sync/history").strip("/").rstrip("s")
            low = stamp(params["start_at"]) if "start_at" in params else None
            high = stamp(params["end_at"]) if "end_at" in params else None
            items = sorted(
                (
                    h
                    for h in history
                    if (not kind or h["type"] == kind)
                    and (low is None or stamp(h["watched_at"]) >= low)
                    and (high is None or stamp(h["watched_at"]) <= high)
                ),
                key=lambda h: (stamp(h["watched_at"]), h["id"]),
                reverse=True,
            )
            return paged(items, params)
        if path == "/sync/ratings":
            return paged(sorted(ratings, key=lambda r: r["rated_at"], reverse=True), params)
        if path == "/sync/watchlist":
            return paged(sorted(watchlist, key=lambda w: w["rank"]), params)
        if path == "/sync/last_activities":
            def newest(items, key, kind):
                values = [i[key] for i in items if i["type"] == kind]
                return max(values) if values else "2020-01-01T00:00:00.000Z"

            return httpx.Response(
                200,
                json={
                    "all": max(h["watched_at"] for h in history) if history else None,
                    **{
                        kind + "s": {
                            "watched_at": newest(history, "watched_at", kind),
                            "rated_at": newest(ratings, "rated_at", kind),
                            "watchlisted_at": newest(watchlist, "listed_at", kind),
                        }
                        for kind in ("movie", "show", "season", "episode")
                    },
                },
            )
        return httpx.Response(404)

    return handle


def transport():
    return httpx.MockTransport(handler())
