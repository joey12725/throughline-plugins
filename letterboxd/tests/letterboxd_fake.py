"""A fake letterboxd.com serving the invented RSS recording."""

from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
FEED = (HERE / "recorded" / "letterboxd_rss.xml").read_bytes()


def handler(feed=FEED, requests=None):
    def handle(request):
        if requests is not None:
            requests.append(str(request.url))
        if request.url.path != "/invented_viewer/rss/":
            return httpx.Response(404, text="Not found")
        return httpx.Response(200, content=feed, headers={"Content-Type": "application/rss+xml"})

    return handle


def transport():
    return httpx.MockTransport(handler())
