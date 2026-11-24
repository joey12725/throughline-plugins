"""A fake Last.fm API over the invented recordings in recorded/.

It applies the real API's semantics (from/to filtering, newest-first paging,
a now-playing item on page 1, error objects) so the plugin's paging logic is
exercised, never the network."""

import json
import math
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
API_KEY = "invented-lastfm-key-0000"
NOW_PLAYING = {
    "artist": {"name": "Invented Band", "mbid": ""},
    "name": "Currently Playing Invention",
    "album": {"#text": "", "mbid": ""},
    "@attr": {"nowplaying": "true"},
    "url": "https://www.last.fm/music/Invented+Band/_/Currently+Playing+Invention",
}


def load(name):
    return json.loads((HERE / "recorded" / name).read_text())


def page(items, params, key):
    limit = int(params.get("limit", 50))
    number = int(params.get("page", 1))
    total = len(items)
    pages = max(1, math.ceil(total / limit)) if total else 0
    chunk = items[(number - 1) * limit : number * limit]
    return {
        key: {
            "track": chunk,
            "@attr": {
                "user": params["user"],
                "page": str(number),
                "perPage": str(limit),
                "totalPages": str(pages),
                "total": str(total),
            },
        }
    }


def handler(recent=None, loved=None, requests=None):
    recent = load("lastfm_recent.json") if recent is None else recent
    loved = load("lastfm_loved.json") if loved is None else loved

    def handle(request):
        params = dict(request.url.params)
        if requests is not None:
            requests.append(params)
        if params.get("api_key") != API_KEY:
            return httpx.Response(403, json={"error": 10, "message": "Invalid API key"})
        if params.get("user") != "invented_listener":
            return httpx.Response(404, json={"error": 6, "message": "User not found"})
        method = params["method"].lower()
        if method == "user.getrecenttracks":
            low = int(params.get("from", 0))
            high = int(params.get("to", 2**40))
            items = sorted(
                (t for t in recent if low <= int(t["date"]["uts"]) <= high),
                key=lambda t: -int(t["date"]["uts"]),
            )
            body = page(items, params, "recenttracks")
            if params.get("page", "1") == "1":
                body["recenttracks"]["track"] = [NOW_PLAYING] + body["recenttracks"]["track"]
            return httpx.Response(200, json=body)
        if method == "user.getlovedtracks":
            items = sorted(loved, key=lambda t: -int(t["date"]["uts"]))
            return httpx.Response(200, json=page(items, params, "lovedtracks"))
        return httpx.Response(400, json={"error": 3, "message": "Invalid method"})

    return handle


def transport():
    return httpx.MockTransport(handler())
