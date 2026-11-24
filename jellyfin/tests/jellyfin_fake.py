"""A fake Jellyfin server over recorded/jellyfin_server.json (all invented).

Implements the endpoints the plugin uses with Jellyfin's semantics: API-key
auth, /Users, /Users/{id}/Views, /Users/{id}/Items (Ids, ParentId,
IncludeItemTypes, SortBy DatePlayed/DateCreated descending, StartIndex,
Limit, UserData), /Plugins and the Playback Reporting custom query."""

import json
import re
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
API_KEY = "invented0jellyfin0api0key0000000"
COLUMNS = [
    "DateCreated", "UserId", "ItemId", "ItemType", "ItemName",
    "PlaybackMethod", "ClientName", "DeviceName", "PlayDuration",
]


def load():
    return json.loads((HERE / "recorded" / "jellyfin_server.json").read_text())


def handler(reporting=True, data=None, requests=None):
    data = load() if data is None else data
    users = [
        {"Id": data["owner_id"].replace("-", ""), "Name": "Invented Owner"},
        {"Id": data["other_id"].replace("-", ""), "Name": "Invented Guest"},
    ]
    views = [
        {"Id": "lib-movies", "Name": "Movies", "CollectionType": "movies"},
        {"Id": "lib-shows", "Name": "Shows", "CollectionType": "tvshows"},
        {"Id": "lib-music", "Name": "Music", "CollectionType": "music"},
        {"Id": "lib-lists", "Name": "Playlists", "CollectionType": "playlists"},
    ]

    def public(item, with_userdata):
        out = {k: v for k, v in item.items() if not k.startswith("_")}
        if with_userdata:
            out["UserData"] = dict(
                {"PlayCount": 0, "Played": False, "PlaybackPositionTicks": 0},
                **data["userdata"].get(item["Id"], {}),
            )
        return out

    def handle(request):
        path = request.url.path
        params = dict(request.url.params)
        if requests is not None:
            requests.append((request.method, path, params))
        if f'Token="{API_KEY}"' not in request.headers.get("Authorization", ""):
            return httpx.Response(401, text="Unauthorized")
        if path == "/System/Info":
            return httpx.Response(200, json={"Id": "f00dfeed0000400080000000000000aa", "ServerName": "Invented Media Server", "Version": "10.9.0"})
        if path == "/Users":
            return httpx.Response(200, json=users)
        if path == "/Plugins":
            plugins = [{"Name": "Invented Other Plugin", "Version": "1.0"}]
            if reporting:
                plugins.append({"Name": "Playback Reporting", "Version": "16.0.0.0"})
            return httpx.Response(200, json=plugins)
        if path == "/user_usage_stats/submit_custom_query":
            if not reporting:
                return httpx.Response(404)
            sql = json.loads(request.content)["CustomQueryString"]
            after = re.search(r"DateCreated > '([^']*)'", sql).group(1)
            limit = int(re.search(r"LIMIT (\d+)", sql).group(1))
            rows = sorted(r for r in data["reporting"] if r[0] > after)[:limit]
            return httpx.Response(200, json={"colums": COLUMNS, "results": rows, "message": ""})
        match = re.fullmatch(r"/Users/([0-9a-f]+)/(Views|Items)", path)
        if match:
            if match.group(1) not in {u["Id"] for u in users}:
                return httpx.Response(404)
            if match.group(2) == "Views":
                return httpx.Response(200, json={"Items": views, "TotalRecordCount": len(views)})
            rows = data["items"]
            if params.get("Ids"):
                wanted = set(params["Ids"].split(","))
                rows = [i for i in rows if i["Id"] in wanted]
            if params.get("ParentId"):
                rows = [i for i in rows if i["_library"] == params["ParentId"]]
            if params.get("IncludeItemTypes"):
                types = set(params["IncludeItemTypes"].split(","))
                rows = [i for i in rows if i["Type"] in types]
            if params.get("SortBy") == "DatePlayed":
                played = lambda i: data["userdata"].get(i["Id"], {}).get("LastPlayedDate") or ""  # noqa: E731
                rows = sorted(rows, key=played, reverse=True)
            elif params.get("SortBy") == "DateCreated":
                rows = sorted(rows, key=lambda i: i["DateCreated"], reverse=True)
            start = int(params.get("StartIndex", 0))
            limit = int(params.get("Limit", 10**6))
            page = rows[start : start + limit]
            with_userdata = params.get("EnableUserData") == "true"
            return httpx.Response(
                200,
                json={"Items": [public(i, with_userdata) for i in page], "TotalRecordCount": len(rows)},
            )
        return httpx.Response(404)

    return handle


def transport():
    return httpx.MockTransport(handler())


def transport_core():
    """A server without the Playback Reporting plugin."""
    return httpx.MockTransport(handler(reporting=False))
