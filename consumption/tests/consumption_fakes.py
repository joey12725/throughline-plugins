"""Invented services on a local fake HTTP server; no test reaches a real host.

Every name, ID, token and timestamp below is synthetic. Tokens are canaries:
tests assert they never reach records, exports, state files or output.
"""

import io
import base64
import json
import sys
import threading
import zipfile
import zlib
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


SRC = Path(__file__).resolve().parents[1] / "src"
for _path in (SRC, Path(__file__).resolve().parent):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

TOKENS = {
    "PLEX_TOKEN": "fake-plex-canary-7f3a",
    "AUDIOBOOKSHELF_TOKEN": "fake-abs-canary-91c2",
    "KAVITA_API_KEY": "fake-kavita-key-canary-44b1",
    "SPOTIFY_CLIENT_ID": "fake-client-id-canary",
    "SPOTIFY_CLIENT_SECRET": "fake-client-secret-canary-55",
    "SPOTIFY_REFRESH_TOKEN": "fake-refresh-canary-13",
    "COMPOSIO_API_KEY": "fake-composio-canary-08",
    "SPOTIFY_COMPOSIO_MCP_TOKEN": "fake-composio-mcp-canary-09",
}
ACCESS = "fake-access-canary-1"
KAVITA_JWT = "fake-kavita-jwt-canary-88"


def build_test_epub(chapters):
    """Build a minimal valid EPUB3/EPUB2 archive in memory for tests."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?>'
            '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>'
            "</container>",
        )
        manifest_items = [
            '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
        ]
        spine_items = []
        nav_points = []
        for idx, (title, body_html) in enumerate(chapters, 1):
            href = f"ch{idx}.xhtml"
            manifest_items.append(
                f'<item id="ch{idx}" href="{href}" media-type="application/xhtml+xml"/>'
            )
            spine_items.append(f'<itemref idref="ch{idx}"/>')
            nav_points.append(
                f'<navPoint id="np{idx}" playOrder="{idx}">'
                f"<navLabel><text>{title}</text></navLabel>"
                f'<content src="{href}"/></navPoint>'
            )
            zf.writestr(
                f"OEBPS/{href}",
                f"<html><head><title>{title}</title><style>p{{color:red}}</style></head>"
                f"<body><h1>{title}</h1>{body_html}<script>ignore()</script></body></html>",
            )
        zf.writestr(
            "OEBPS/toc.ncx",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
            f"<navMap>{''.join(nav_points)}</navMap></ncx>",
        )
        zf.writestr(
            "OEBPS/content.opf",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
            f"<manifest>{''.join(manifest_items)}</manifest>"
            f'<spine toc="ncx">{"".join(spine_items)}</spine>'
            "</package>",
        )
    return buf.getvalue()


def epoch(text):
    return int(datetime.fromisoformat(text).timestamp())


def iso_z(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class World:
    """Synthetic source state. Tests may mutate it between runs."""

    def __init__(self, null_added=True):
        # lean: page envelopes without totals (no Plex totalSize, no
        # Audiobookshelf numPages, Composio pages without next or total).
        self.lean = False
        start = datetime(2020, 1, 10, 8, 0, tzinfo=timezone.utc)
        self.plays = []
        for n in range(55):
            self.plays.append(
                {
                    "track": {
                        "id": f"FAKETRACK{n:02}",
                        "uri": f"spotify:track:FAKETRACK{n:02}",
                        "type": "track",
                        "name": f"Invented Song {n}",
                        "duration_ms": 180000 + n,
                        "popularity": 50 + n,
                        "available_markets": ["XX"],
                        "artists": [
                            {
                                "name": "Invented Band",
                                "uri": "spotify:artist:FAKEARTIST",
                                "id": "FAKEARTIST",
                            }
                        ],
                        "album": {
                            "name": "Invented Album",
                            "uri": "spotify:album:FAKEALBUM",
                            "release_date": "2019-05-01",
                        },
                        "external_urls": {
                            "spotify": f"https://open.spotify.com/track/FAKETRACK{n:02}"
                        },
                    },
                    "played_at": iso_z(start + timedelta(minutes=4 * n)),
                    "context": {"type": "playlist", "uri": "spotify:playlist:PLFAKE1"}
                    if n % 2
                    else None,
                }
            )
        self.saved = [
            {
                "added_at": f"2020-01-0{d}T12:00:00Z",
                "track": {
                    "id": f"FAKESAVED{d}",
                    "uri": f"spotify:track:FAKESAVED{d}",
                    "type": "track",
                    "name": f"Invented Favourite {d}",
                    "duration_ms": 200000,
                    "artists": [
                        {"name": "Invented Singer", "uri": "spotify:artist:FAKESINGER"}
                    ],
                    "album": {
                        "name": "Invented Record",
                        "uri": "spotify:album:FAKERECORD",
                    },
                },
            }
            for d in (5, 4, 3)
        ]
        entry = {
            "added_at": "2020-01-02T09:00:00Z",
            "added_by": {"id": "fake-owner"},
            "track": {
                "id": "FAKETRACK01",
                "uri": "spotify:track:FAKETRACK01",
                "type": "track",
                "name": "Invented Song 1",
                "duration_ms": 180001,
                "artists": [
                    {"name": "Invented Band", "uri": "spotify:artist:FAKEARTIST"}
                ],
                "album": {"name": "Invented Album", "uri": "spotify:album:FAKEALBUM"},
            },
        }
        episode = {
            "added_at": None if null_added else "2020-01-02T10:00:00Z",
            "added_by": {"id": "fake-friend"},
            "item": {
                "id": "FAKEEPISODE",
                "uri": "spotify:episode:FAKEEPISODE",
                "type": "episode",
                "name": "Invented Episode",
                "duration_ms": 1800000,
                "show": {
                    "name": "Invented Show",
                    "uri": "spotify:show:FAKESHOW",
                    "publisher": "Fake Studio",
                },
            },
        }
        self.playlists = [
            {
                "id": "PLFAKE1",
                "name": "Invented Mix",
                "snapshot_id": "snap-1",
                "collaborative": True,
                "owner": {"id": "fake-owner", "display_name": "Fake Owner"},
            },
            {
                "id": "PLFAKE2",
                "name": "Not Mine",
                "snapshot_id": "snap-x",
                "owner": {"id": "someone-else"},
            },
        ]
        # The same track added twice at the same instant stays two entries.
        self.playlist_items = {"PLFAKE1": [entry, dict(entry), episode]}
        self.plex_history = [
            {
                "historyKey": "/status/sessions/history/1",
                "ratingKey": "101",
                "title": "Invented Film",
                "type": "movie",
                "viewedAt": epoch("2020-01-03T20:00:00+00:00"),
                "accountID": 1,
                "deviceID": 5,
                "librarySectionID": "1",
            },
            {
                "historyKey": "/status/sessions/history/2",
                "ratingKey": "201",
                "title": "Pilot",
                "type": "episode",
                "grandparentTitle": "Invented Series",
                "parentIndex": 1,
                "index": 1,
                "viewedAt": epoch("2020-01-04T21:00:00+00:00"),
                "accountID": 1,
                "deviceID": 5,
                "librarySectionID": "2",
            },
            {
                "historyKey": "/status/sessions/history/3",
                "ratingKey": "202",
                "title": "Second",
                "type": "episode",
                "grandparentTitle": "Invented Series",
                "parentIndex": 1,
                "index": 2,
                "viewedAt": epoch("2020-01-04T22:00:00+00:00"),
                "accountID": 2,
                "deviceID": 9,
                "librarySectionID": "2",
            },
            {
                "historyKey": "/status/sessions/history/4",
                "ratingKey": "301",
                "title": "Invented Tune",
                "type": "track",
                "grandparentTitle": "Invented Artist",
                "parentTitle": "Invented LP",
                "viewedAt": epoch("2020-01-05T07:00:00+00:00"),
                "accountID": 1,
                "deviceID": 5,
                "librarySectionID": "3",
            },
            {
                "historyKey": "/status/sessions/history/5",
                "ratingKey": "999",
                "title": "Deleted Film",
                "type": "movie",
                "viewedAt": epoch("2020-01-06T07:00:00+00:00"),
                "accountID": 1,
                "deviceID": 5,
                "librarySectionID": "1",
            },
        ]
        self.plex_metadata = {
            "101": {
                "ratingKey": "101",
                "guid": "plex://movie/fake101",
                "type": "movie",
                "title": "Invented Film",
                "year": 2001,
                "duration": 5400000,
                "librarySectionTitle": "Films",
                "viewCount": 3,
            },
            "201": {
                "ratingKey": "201",
                "guid": "plex://episode/fake201",
                "type": "episode",
                "title": "Pilot",
                "grandparentTitle": "Invented Series",
                "grandparentGuid": "plex://show/fakeshow",
                "grandparentRatingKey": "200",
                "parentIndex": 1,
                "index": 1,
                "duration": 1500000,
                "librarySectionTitle": "TV",
            },
            "202": {
                "ratingKey": "202",
                "guid": "plex://episode/fake202",
                "type": "episode",
                "title": "Second",
                "grandparentTitle": "Invented Series",
                "grandparentGuid": "plex://show/fakeshow",
                "grandparentRatingKey": "200",
                "parentIndex": 1,
                "index": 2,
                "duration": 1500000,
                "librarySectionTitle": "TV",
            },
            "301": {
                "ratingKey": "301",
                "guid": "plex://track/fake301",
                "type": "track",
                "title": "Invented Tune",
                "grandparentTitle": "Invented Artist",
                "parentTitle": "Invented LP",
                "duration": 200000,
                "librarySectionTitle": "Music",
            },
        }
        self.plex_active_sessions = []
        self.plex_sections = []
        self.plex_additions = {}
        base = epoch("2020-01-07T19:00:00+00:00") * 1000
        self.sessions = [
            {
                "id": "sess-1",
                "userId": "fake-user",
                "libraryId": "lib-books",
                "libraryItemId": "li-book",
                "mediaType": "book",
                "displayTitle": "Invented Saga",
                "displayAuthor": "Fake Author",
                "mediaMetadata": {
                    "title": "Invented Saga",
                    "authors": [{"id": "a1", "name": "Fake Author"}],
                    "narrators": ["Fake Narrator"],
                    "series": [{"name": "Invented Cycle", "sequence": "1"}],
                },
                "chapters": [{"id": 0, "title": "One"}],
                "duration": 36000,
                "playMethod": 0,
                "mediaPlayer": "html5",
                "deviceInfo": {
                    "deviceName": "Fake Phone",
                    "clientName": "Fake Client",
                    "clientVersion": "1.0",
                    "ipAddress": "192.0.2.10",
                },
                "date": "2020-01-07",
                "timeListening": 1800,
                "startTime": 0,
                "currentTime": 1800,
                "startedAt": base,
                "updatedAt": base + 1800 * 1000,
            },
            {
                "id": "sess-2",
                "userId": "fake-user",
                "libraryId": "lib-books",
                "libraryItemId": "li-book",
                "mediaType": "book",
                "displayTitle": "Invented Saga",
                "displayAuthor": "Fake Author",
                "mediaMetadata": {
                    "title": "Invented Saga",
                    "authors": [{"id": "a1", "name": "Fake Author"}],
                },
                "duration": 36000,
                "playMethod": 1,
                "deviceInfo": {"manufacturer": "Fake", "model": "Tablet"},
                "timeListening": 3600,
                "startTime": 1800,
                "currentTime": 5400,
                "startedAt": base + 86400000,
                "updatedAt": base + 86400000 + 3600 * 1000,
            },
            {
                "id": "sess-3",
                "userId": "fake-user",
                "libraryId": "lib-pods",
                "libraryItemId": "li-pod",
                "episodeId": "ep-1",
                "mediaType": "podcast",
                "displayTitle": "Invented Episode One",
                "displayAuthor": "Fake Host",
                "mediaMetadata": {"title": "Invented Podcast", "author": "Fake Host"},
                "duration": 2400,
                "playMethod": 0,
                "timeListening": 2400,
                "startTime": 0,
                "currentTime": 2400,
                "startedAt": base + 2 * 86400000,
                "updatedAt": base + 2 * 86400000 + 2400 * 1000,
            },
        ]
        self.progress = [
            {
                "id": "prog-book",
                "userId": "fake-user",
                "libraryItemId": "li-book",
                "episodeId": None,
                "duration": 36000,
                "progress": 0.15,
                "currentTime": 5400,
                "isFinished": False,
                "hideFromContinueListening": False,
                "lastUpdate": base + 86400000 + 3600 * 1000,
                "startedAt": base,
                "finishedAt": None,
            },
            {
                "id": "prog-pod",
                "userId": "fake-user",
                "libraryItemId": "li-pod",
                "episodeId": "ep-1",
                "duration": 2400,
                "progress": 1,
                "currentTime": 2400,
                "isFinished": True,
                "lastUpdate": base + 2 * 86400000 + 2400 * 1000,
                "startedAt": base + 2 * 86400000,
                "finishedAt": base + 2 * 86400000 + 2400 * 1000,
            },
        ]
        self.items = {
            "li-book": {
                "id": "li-book",
                "libraryId": "lib-books",
                "mediaType": "book",
                "media": {
                    "duration": 36000,
                    "metadata": {
                        "title": "Invented Saga",
                        "authorName": "Fake Author",
                        "narratorName": "Fake Narrator",
                        "seriesName": "Invented Cycle #1",
                    },
                },
            },
            "li-pod": {
                "id": "li-pod",
                "libraryId": "lib-pods",
                "mediaType": "podcast",
                "media": {
                    "metadata": {"title": "Invented Podcast", "author": "Fake Host"},
                    "episodes": [
                        {
                            "id": "ep-1",
                            "title": "Invented Episode One",
                            "season": "1",
                            "episode": "1",
                            "audioFile": {"duration": 2400},
                        }
                    ],
                },
            },
        }
        # Kavita synthetic catalog (chapters + downloadable ebook bytes).
        self.kavita_chapters = {}
        self.kavita_files = {}
        self.kavita_series = []
        self.kavita_volumes = {}

    def add_kavita_book(
        self,
        *,
        chapter_id,
        title,
        authors,
        file_path,
        data,
        isbn="",
        series_id=101,
        extension=".epub",
        koreader_hash="FAKEHASH1",
        last_modified="2020-01-01T00:00:00Z",
    ):
        chapter = {
            "id": chapter_id,
            "title": "-100000",
            "titleName": title,
            "isbn": isbn,
            "lastModifiedUtc": last_modified,
            "writers": [{"name": a} for a in authors],
            "files": [
                {
                    "id": chapter_id + 1000,
                    "filePath": file_path,
                    "bytes": len(data),
                    "extension": extension,
                    "koreaderHash": koreader_hash,
                }
            ],
        }
        self.kavita_chapters[chapter_id] = chapter
        self.kavita_files[chapter_id] = data
        self.kavita_series.append(
            {"seriesId": series_id, "name": title, "libraryId": 1}
        )
        self.kavita_volumes.setdefault(series_id, []).append(
            {"id": series_id * 10, "chapters": [chapter]}
        )
        return chapter


class Fake:
    def __init__(self, world):
        self.world = world
        self.log = []
        self.faults = {}  # path -> list of (status, headers) returned before success
        self.lock = threading.Lock()
        self.redirect = set()

    def take_fault(self, path):
        with self.lock:
            queue = self.faults.get(path)
            return queue.pop(0) if queue else None


def make_handler(fake):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, body=None, headers=None):
            if isinstance(body, (bytes, bytearray)):
                data = bytes(body)
                ct = "application/octet-stream"
            else:
                data = json.dumps(body).encode() if body is not None else b""
                ct = "application/json"
            self.send_response(status)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(data)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.route("GET")

        def do_POST(self):
            self.route("POST")

        def route(self, method):
            parts = urlsplit(self.path)
            path = parts.path
            query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            with fake.lock:
                fake.log.append(
                    {
                        "method": method,
                        "path": path,
                        "query": query,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                    }
                )
            if path in fake.redirect:
                return self.send(
                    302, None, {"Location": "http://127.0.0.1:9/elsewhere"}
                )
            fault = fake.take_fault(path)
            if fault:
                return self.send(fault[0], {"error": "injected"}, fault[1])
            try:
                status, result = dispatch(
                    fake.world, method, path, query, self.headers, body
                )
            except KeyError:
                status, result = 404, {"error": "not found"}
            self.send(status, result)

    return Handler


def page_list(items, query, default=50):
    limit = int(query.get("limit", default))
    offset = int(query.get("offset", 0))
    chunk = items[offset : offset + limit]
    more = offset + limit < len(items)
    return {
        "items": chunk,
        "total": len(items),
        "offset": offset,
        "limit": limit,
        "next": "more" if more else None,
    }


def spotify(world, path, query):
    if path == "me/player/recently-played":
        rows = sorted(world.plays, key=lambda r: r["played_at"], reverse=True)
        if "before" in query:
            rows = [
                r
                for r in rows
                if epoch(r["played_at"].replace("Z", "+00:00")) * 1000
                < int(query["before"])
            ]
        rows = rows[: int(query.get("limit", 50))]
        before = (
            epoch(rows[-1]["played_at"].replace("Z", "+00:00")) * 1000 if rows else None
        )
        return {"items": rows, "cursors": {"before": str(before) if before else None}}
    if path == "me/tracks":
        return page_list(world.saved, query)
    if path == "me/playlists":
        return page_list(world.playlists, query)
    if path.startswith("playlists/") and path.endswith("/items"):
        pid = path.split("/")[1]
        if pid not in world.playlist_items:
            return 403
        return page_list(world.playlist_items[pid], query, 100)
    raise KeyError(path)


def dispatch(world, method, path, query, headers, body):
    if path == "/spotify/token":
        if not headers.get("Authorization", "").startswith("Basic "):
            return 401, {"error": "invalid_client"}
        form = parse_qs(body.decode())
        if form.get("refresh_token") != [TOKENS["SPOTIFY_REFRESH_TOKEN"]]:
            return 400, {"error": "invalid_grant"}
        return 200, {
            "access_token": ACCESS,
            "refresh_token": "fake-rotated-canary",
            "expires_in": 3600,
        }
    if path.startswith("/spotify/v1/"):
        if headers.get("Authorization") != "Bearer " + ACCESS:
            return 401, {"error": "expired"}
        result = spotify(world, path[len("/spotify/v1/") :], query)
        return (
            (result, {"error": "forbidden"})
            if isinstance(result, int)
            else (200, result)
        )
    if path.startswith("/composio/tools/execute/"):
        if headers.get("x-api-key") != TOKENS["COMPOSIO_API_KEY"]:
            return 401, {"error": "bad key"}
        request = json.loads(body)
        slug = path.rsplit("/", 1)[1]
        args = {k: str(v) for k, v in request["arguments"].items()}
        target = {
            "SPOTIFY_GET_RECENTLY_PLAYED_TRACKS": "me/player/recently-played",
            "SPOTIFY_GET_USER_S_SAVED_TRACKS": "me/tracks",
            "SPOTIFY_GET_CURRENT_USER_S_PLAYLISTS": "me/playlists",
            "SPOTIFY_GET_PLAYLIST_ITEMS": f"playlists/{args.get('playlist_id')}/items",
        }[slug]
        result = spotify(world, target, args)
        if isinstance(result, int):
            return 200, {"successful": False, "error": "forbidden", "data": {}}
        if world.lean:
            result = {k: v for k, v in result.items() if k not in ("next", "total")}
        return 200, {"successful": True, "data": {"response_data": result}}
    if path == "/composio-mcp":
        if headers.get("Authorization") != "Bearer " + TOKENS["SPOTIFY_COMPOSIO_MCP_TOKEN"]:
            return 401, {"error": "bad token"}
        recent = spotify(world, "me/player/recently-played", {"limit": "50"})
        result = {
            "profile": {"result": {"id": "fake-owner"}, "error": None},
            "recent": {"result": recent, "error": None},
        }
        packed = base64.b64encode(
            zlib.compress(json.dumps(result).encode())
        ).decode()
        workbench = {
            "successful": True,
            "data": {
                "stdout": "THROUGHLINE_SPOTIFY_SNAPSHOT_B64=" + packed + "\n",
                "stderr": "",
            },
        }
        return 200, {
            "jsonrpc": "2.0",
            "id": "fake",
            "result": {
                "content": [{"type": "text", "text": json.dumps(workbench)}]
            },
        }
    if path.startswith("/plex/"):
        if headers.get("X-Plex-Token") != TOKENS["PLEX_TOKEN"]:
            return 401, {"error": "unauthorized"}
        sub = path[len("/plex") :]
        if sub == "/identity":
            return 200, {"MediaContainer": {"machineIdentifier": "fake-server"}}
        if sub == "/accounts":
            return 200, {
                "MediaContainer": {
                    "Account": [
                        {"id": 1, "name": "Fake Owner"},
                        {"id": 2, "name": "Fake Guest"},
                    ]
                }
            }
        if sub == "/devices":
            return 200, {
                "MediaContainer": {
                    "Device": [{"id": 5, "name": "Fake TV", "platform": "FakeOS"}]
                }
            }
        if sub == "/status/sessions":
            return 200, {
                "MediaContainer": {
                    "size": len(world.plex_active_sessions),
                    "Metadata": world.plex_active_sessions,
                }
            }
        if sub == "/library/sections":
            return 200, {"MediaContainer": {"Directory": world.plex_sections}}
        if sub.startswith("/library/sections/") and sub.endswith("/all"):
            section = sub.split("/")[3]
            rows = sorted(
                (
                    row
                    for row in world.plex_additions.get(section, [])
                    if int(row.get("addedAt") or 0) > int(query.get("addedAt>", -1))
                    and int(row.get("typeCode") or query.get("type", 0))
                    == int(query.get("type", 0))
                ),
                key=lambda row: (int(row.get("addedAt") or 0), str(row.get("ratingKey"))),
            )
            start = int(query["X-Plex-Container-Start"])
            size = int(query["X-Plex-Container-Size"])
            chunk = rows[start : start + size]
            return 200, {
                "MediaContainer": {"totalSize": len(rows), "Metadata": chunk}
            }
        if sub == "/status/sessions/history/all":
            lower = int(query.get("viewedAt>", -1))
            rows = sorted(
                (r for r in world.plex_history if r["viewedAt"] > lower),
                key=lambda r: r["viewedAt"],
            )
            start = int(query["X-Plex-Container-Start"])
            size = int(query["X-Plex-Container-Size"])
            chunk = rows[start : start + size]
            if world.lean:
                return 200, {"MediaContainer": {"size": len(chunk), "Metadata": chunk}}
            return 200, {"MediaContainer": {"totalSize": len(rows), "Metadata": chunk}}
        if sub.startswith("/library/metadata/"):
            key = sub.rsplit("/", 1)[1]
            return 200, {"MediaContainer": {"Metadata": [world.plex_metadata[key]]}}
        raise KeyError(sub)
    if path.startswith("/abs/api/"):
        if headers.get("Authorization") != "Bearer " + TOKENS["AUDIOBOOKSHELF_TOKEN"]:
            return 401, {"error": "unauthorized"}
        sub = path[len("/abs/api/") :]
        if sub == "me":
            return 200, {
                "id": "fake-user",
                "username": "fakeowner",
                "mediaProgress": world.progress,
            }
        if sub == "me/listening-sessions":
            rows = sorted(world.sessions, key=lambda s: s["updatedAt"], reverse=True)
            size = int(query["itemsPerPage"])
            page = int(query["page"])
            pages = max(1, -(-len(rows) // size))
            body = {
                "total": len(rows),
                "numPages": pages,
                "page": page,
                "itemsPerPage": size,
                "sessions": rows[page * size : (page + 1) * size],
            }
            if world.lean:
                body = {"sessions": body["sessions"]}
            return 200, body
        if sub.startswith("items/"):
            return 200, world.items[sub.split("/", 1)[1]]
        raise KeyError(sub)
    if path.startswith("/kavita/api/"):
        sub = path[len("/kavita/api/") :]
        if sub == "Plugin/authenticate" and method == "POST":
            if query.get("apiKey") != TOKENS["KAVITA_API_KEY"]:
                return 401, {"error": "bad api key"}
            return 200, {"token": KAVITA_JWT}
        if headers.get("Authorization") != "Bearer " + KAVITA_JWT:
            return 401, {"error": "unauthorized"}
        if sub == "Search/search":
            q = (query.get("queryString") or "").lower()
            tokens = [t for t in q.split() if len(t) >= 2]
            ch_hits = []
            for ch in world.kavita_chapters.values():
                hay = " ".join(
                    [
                        str(ch.get("titleName") or ""),
                        str(ch.get("title") or ""),
                        *(f.get("filePath") or "" for f in ch.get("files") or []),
                        *(w.get("name") or "" for w in ch.get("writers") or []),
                    ]
                ).lower()
                if q and (q in hay or (tokens and all(t in hay for t in tokens))):
                    ch_hits.append(ch)
            s_hits = [
                s
                for s in world.kavita_series
                if q and q in (s.get("name") or "").lower()
            ]
            return 200, {"series": s_hits, "chapters": ch_hits, "files": []}
        if sub == "Series/volumes":
            sid = int(query["seriesId"])
            return 200, world.kavita_volumes.get(sid, [])
        if sub == "Chapter":
            cid = int(query["chapterId"])
            if cid not in world.kavita_chapters:
                return 404, {"error": "not found"}
            return 200, world.kavita_chapters[cid]
        if sub == "Download/chapter":
            cid = int(query["chapterId"])
            if cid not in world.kavita_files:
                return 404, {"error": "not found"}
            return 200, world.kavita_files[cid]
        raise KeyError(sub)
    raise KeyError(path)


def live_env(fake, *, spotify="refresh"):
    env = {
        "PLEX_SERVER_URL": fake.base + "/plex",
        "PLEX_TOKEN": TOKENS["PLEX_TOKEN"],
        "AUDIOBOOKSHELF_URL": fake.base + "/abs/api",
        "AUDIOBOOKSHELF_TOKEN": TOKENS["AUDIOBOOKSHELF_TOKEN"],
        "CONSUMPTION_SPOTIFY_API_BASE": fake.base + "/spotify/v1/",
        "CONSUMPTION_SPOTIFY_TOKEN_URL": fake.base + "/spotify/token",
        "COMPOSIO_BASE_URL": fake.base + "/composio/",
        "CONSUMPTION_REQUEST_DELAY_S": "0",
        "CONSUMPTION_PLEX_PAGE_SIZE": "2",
        "CONSUMPTION_ABS_PAGE_SIZE": "2",
    }
    if spotify == "refresh":
        for key in (
            "SPOTIFY_CLIENT_ID",
            "SPOTIFY_CLIENT_SECRET",
            "SPOTIFY_REFRESH_TOKEN",
        ):
            env[key] = TOKENS[key]
    elif spotify == "composio":
        env["COMPOSIO_API_KEY"] = TOKENS["COMPOSIO_API_KEY"]
        env["SPOTIFY_COMPOSIO_ACCOUNT_ID"] = "fake-connected-account"
    return env


def write_backup(root):
    """An invented weekly backup tree: snapshots plus a latest copy."""
    liked = [
        {
            "added_at": "2019-12-01T10:00:00Z",
            "track": {
                "id": "FAKEOLD1",
                "uri": "spotify:track:FAKEOLD1",
                "name": "Invented Oldie",
                "artists": [{"name": "Invented Elder"}],
                "popularity": 3,
            },
        },
        {
            "id": "FAKENOTIME",
            "uri": "spotify:track:FAKENOTIME",
            "name": "Invented Timeless",
            "artists": [{"name": "Invented Elder"}],
        },
    ]
    playlist = {
        "id": "PLBACKUP",
        "name": "Invented Archive",
        "owner": {"id": "fake-owner"},
        "snapshot_id": "b1",
        "tracks": {
            "items": [
                {
                    "added_at": "2019-11-01T00:00:00Z",
                    "track": {
                        "id": "FAKEOLD1",
                        "uri": "spotify:track:FAKEOLD1",
                        "name": "Invented Oldie",
                        "artists": [{"name": "Invented Elder"}],
                    },
                }
            ]
        },
    }
    for folder in ("snapshots/2019-12-15", "snapshots/2019-12-22", "latest"):
        target = root / folder
        target.mkdir(parents=True)
        (target / "liked_songs.json").write_text(
            json.dumps({"items": liked}), encoding="utf-8"
        )
        (target / "playlists.json").write_text(
            json.dumps({"playlists": [playlist]}), encoding="utf-8"
        )
    (root / "latest" / "notes.json").write_text("not json", encoding="utf-8")


def write_paged_backup(root):
    """An invented backup kept as raw API pages, as a Composio backup job writes.

    Liked songs are pages under `raw/liked-*/`, playlist entries are pages under
    `raw/playlists/<playlist id>/`, and the playlists themselves are listed in
    `inventory.json`. Every page sits inside Composio `data` envelopes.
    """

    def track(n):
        return {
            "id": f"FAKEPG{n}",
            "uri": f"spotify:track:FAKEPG{n}",
            "name": f"Invented Paged {n}",
            "artists": [{"name": "Invented Pager"}],
            "available_markets": ["XX"],
        }

    playlist = {
        "id": "PLPAGED",
        "name": "Invented Paged List",
        "owner": {"id": "fake-owner", "display_name": "Fake Owner"},
        "snapshot_id": "p1",
        "items": {"href": "https://invalid.example/items", "total": 3},
        "tracks": {"href": "https://invalid.example/tracks", "total": 3},
        "type": "playlist",
    }
    repeated = {"added_at": "2020-01-05T00:00:00Z", "track": track(1)}
    pages = {
        "page-0000000.json": [repeated, {"added_at": None, "track": None}],
        "page-0000002.json": [
            repeated,
            {"added_at": "2020-01-06T00:00:00Z", "item": track(2)},
        ],
    }
    for name in ("2020-01-12T000000Z", "2020-01-19T000000Z"):
        snap = root / "snapshots" / name
        (snap / "raw" / "liked-before").mkdir(parents=True)
        (snap / "raw" / "playlists" / "PLPAGED").mkdir(parents=True)
        (snap / "inventory.json").write_text(json.dumps([playlist]), encoding="utf-8")
        (snap / "raw" / "liked-before" / "page-0000000.json").write_text(
            json.dumps(
                {
                    "data": {
                        "items": [
                            {"added_at": "2020-01-02T00:00:00Z", "track": track(3)}
                        ],
                        "next": None,
                    },
                    "error": None,
                }
            ),
            encoding="utf-8",
        )
        for page, items in pages.items():
            (snap / "raw" / "playlists" / "PLPAGED" / page).write_text(
                json.dumps({"data": {"items": items, "next": None}, "error": None}),
                encoding="utf-8",
            )


TAKEOUT = [
    {
        "header": "YouTube",
        "title": "Watched Invented Orchard Tour",
        "titleUrl": "https://www.youtube.com/watch?v=FAKE_VIDEO_A",
        "time": "2019-03-02T12:00:00.250Z",
        "subtitles": [
            {
                "name": "Invented Orchard",
                "url": "https://www.youtube.com/channel/UC_FAKE_ORCHARD",
            }
        ],
    },
    {
        "header": "YouTube",
        "title": "Watched Invented Orchard Tour",
        "titleUrl": "https://www.youtube.com/watch?v=FAKE_VIDEO_A",
        "time": "2019-03-02T12:00:00.250Z",
        "subtitles": [
            {
                "name": "Invented Orchard",
                "url": "https://www.youtube.com/channel/UC_FAKE_ORCHARD",
            }
        ],
    },
    {
        "header": "YouTube",
        "title": "Watched Invented Short",
        "titleUrl": "https://www.youtube.com/shorts/FAKE_SHORT",
        "time": "2019-03-03T08:00:00-05:00",
    },
    {
        "header": "YouTube Music",
        "title": "Watched Invented Song Video",
        "titleUrl": "https://music.youtube.com/watch?v=FAKE_MUSIC",
        "time": "2019-03-04T08:00:00Z",
    },
    {
        "header": "YouTube",
        "title": "Searched for invented things",
        "titleUrl": "https://www.youtube.com/results?search_query=x",
        "time": "2019-03-04T09:00:00Z",
    },
    {
        "header": "YouTube",
        "title": "Watched a video that has been removed",
        "time": "2018-01-01T01:00:00Z",
    },
]


def write_takeout(root):
    folder = root / "Takeout" / "YouTube and YouTube Music" / "history"
    folder.mkdir(parents=True)
    (folder / "watch-history.json").write_text(json.dumps(TAKEOUT), encoding="utf-8")
    return root
