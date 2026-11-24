"""Fake HTTP server and invented fixture builders (no real data, no network)."""

import json
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from throughline_youtube_feed import fetch

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
CH1 = "UC" + "a" * 22
CH2 = "UC" + "b" * 22
CH3 = "UC" + "c" * 22


def vid(n):
    return f"vid{n:08d}"


def feed_xml(channel_id, title, entries):
    """An invented Atom channel feed. entries: dicts with n, title, published."""
    items = []
    for e in entries:
        v = vid(e["n"])
        link = e.get("link") or f"https://www.youtube.com/watch?v={v}"
        items.append(
            f"""<entry>
  <id>yt:video:{v}</id>
  <yt:videoId>{v}</yt:videoId>
  <yt:channelId>{channel_id}</yt:channelId>
  <title>{e["title"]}</title>
  <link rel="alternate" href="{link}"/>
  <author><name>{title}</name><uri>https://www.youtube.com/channel/{channel_id}</uri></author>
  <published>{e["published"]}</published>
  <updated>{e.get("updated", e["published"])}</updated>
  <media:group>
    <media:title>{e["title"]}</media:title>
    <media:content url="https://www.youtube.com/v/{v}?version=3" type="application/x-shockwave-flash" width="640" height="390"/>
    <media:thumbnail url="https://i1.ytimg.com/vi/{v}/hqdefault.jpg" width="480" height="360"/>
    <media:description>{e.get("description", "Invented description " + str(e["n"]))}</media:description>
    <media:community>
      <media:starRating count="{e.get("likes", 5)}" average="5.00" min="1" max="5"/>
      <media:statistics views="{e.get("views", 100)}"/>
    </media:community>
  </media:group>
</entry>"""
        )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/" xmlns="http://www.w3.org/2005/Atom">
 <link rel="self" href="http://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"/>
 <id>yt:channel:{channel_id[2:]}</id>
 <yt:channelId>{channel_id}</yt:channelId>
 <title>{title}</title>
 <author><name>{title}</name><uri>https://www.youtube.com/channel/{channel_id}</uri></author>
 <published>2020-01-01T00:00:00+00:00</published>
 {"".join(items)}
</feed>"""


def channel_page(title, videos):
    """An invented channel videos page. videos: dicts with n, title, ago, length."""
    contents = []
    for v in videos:
        if v.get("short"):
            contents.append(
                {
                    "richItemRenderer": {
                        "content": {
                            "reelItemRenderer": {
                                "videoId": vid(v["n"]),
                                "headline": {"simpleText": v["title"]},
                            }
                        }
                    }
                }
            )
        else:
            contents.append(
                {
                    "richItemRenderer": {
                        "content": {
                            "videoRenderer": {
                                "videoId": vid(v["n"]),
                                "title": {"runs": [{"text": v["title"]}]},
                                "lengthText": {"simpleText": v.get("length", "10:00")},
                                "publishedTimeText": {
                                    "simpleText": v.get("ago", "2 hours ago")
                                },
                                "viewCountText": {"simpleText": "1,234 views"},
                            }
                        }
                    }
                }
            )
    data = {
        "metadata": {"channelMetadataRenderer": {"title": title}},
        "contents": {
            "twoColumnBrowseResultsRenderer": {
                "tabs": [
                    {
                        "tabRenderer": {
                            "content": {"richGridRenderer": {"contents": contents}}
                        }
                    }
                ]
            }
        },
    }
    return f"<html><script>var ytInitialData = {json.dumps(data)};</script></html>"


def watch_page(
    n,
    length=754,
    description="Invented long description",
    publish="2026-09-30T08:00:00-07:00",
):
    player = {
        "videoDetails": {
            "videoId": vid(n),
            "title": f"Invented video {n}",
            "lengthSeconds": str(length),
            "channelId": CH1,
            "author": "Invented Channel One",
            "shortDescription": description,
            "isLiveContent": False,
        },
        "microformat": {
            "playerMicroformatRenderer": {
                "publishDate": publish,
                "uploadDate": publish,
                "category": "Education",
            }
        },
    }
    return f"<html><script>var ytInitialPlayerResponse = {json.dumps(player)};var meta = 1;</script></html>"


def prediction(
    n,
    *,
    channel=CH1,
    scored="2026-09-20T10:00:00+00:00",
    published="2026-09-20T08:00:00+00:00",
    watch=0.8,
    progress=70,
    junk=0.1,
    flagged=None,
):
    line = {
        "channel_id": channel,
        "channel_name": "Invented Channel One"
        if channel == CH1
        else "Invented Channel Two",
        "video_id": vid(n),
        "url": f"https://www.youtube.com/watch?v={vid(n)}",
        "title": f"Invented scored video {n}",
        "duration_seconds": 600 + n,
        "published_at": published,
        "scored_at": scored,
        "watch_probability": watch,
        "progress_if_watched": progress,
        "junk_food_probability": junk,
        "is_junk_food": junk >= 0.5,
        "watch_later_probability": 0.3,
    }
    line["flagged"] = (
        (watch >= 0.7 and progress >= 50 and junk < 0.5) if flagged is None else flagged
    )
    return line


class Server:
    """Routes: (METHOD, path) -> list of responses (last one repeats) or callable."""

    def __init__(self):
        self.routes = {}
        self.requests = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def handle_any(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                parts = urlsplit(self.path)
                request = SimpleNamespace(
                    method=self.command,
                    path=parts.path,
                    query=parts.query,
                    headers=dict(self.headers),
                    body=body,
                )
                server.requests.append(request)
                route = server.routes.get((self.command, parts.path))
                if route is None:
                    status, headers, payload = 404, {}, b"not found"
                elif callable(route):
                    status, headers, payload = route(request)
                else:
                    status, headers, payload = (
                        route[0] if len(route) == 1 else route.pop(0)
                    )
                if isinstance(payload, (dict, list)):
                    payload = json.dumps(payload).encode()
                    headers = {"Content-Type": "application/json", **headers}
                elif isinstance(payload, str):
                    payload = payload.encode()
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(payload)

            do_GET = do_POST = do_HEAD = handle_any

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def route(self, method, path, *responses):
        self.routes[(method, path)] = list(responses)

    def hits(self, method=None, path=None):
        return [
            r
            for r in self.requests
            if (method is None or r.method == method)
            and (path is None or r.path == path)
        ]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    s = Server()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    """No inherited YOUTUBE_FEED_* settings; a fixed clock for fetch.py."""
    import os

    for name in list(os.environ):
        if name.startswith("YOUTUBE_FEED_"):
            monkeypatch.delenv(name)
    monkeypatch.setattr(fetch, "utcnow", lambda: NOW)


@pytest.fixture
def yt():
    return SimpleNamespace(
        NOW=NOW,
        CH1=CH1,
        CH2=CH2,
        CH3=CH3,
        vid=vid,
        feed_xml=feed_xml,
        channel_page=channel_page,
        watch_page=watch_page,
        prediction=prediction,
    )


@pytest.fixture
def cfg(server, tmp_path):
    """A fetch config pointed at the fake server, without delays."""

    def make(**overrides):
        base = dict(
            rss_url=server.url + "/feeds/videos.xml?channel_id={channel_id}",
            web_url=server.url,
            api_url=server.url + "/youtube/v3",
            token_url=server.url + "/token",
            delay_s="0",
            backoff_s="0",
        )
        base.update(overrides)
        return fetch.config({}, **base)

    return make
