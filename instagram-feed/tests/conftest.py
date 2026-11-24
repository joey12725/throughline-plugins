"""Invented Instagram-shaped payloads and a loopback fake server.

Everything here is synthetic: handles, ids, captions and URLs are made up and
no test contacts Instagram or any other outside host.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from throughline_instagram_feed import InstagramFeed

BASE_TS = 1_620_000_000  # 2021-05-03, historical so ingest time follows it.


def node(
    code,
    ts,
    *,
    owner=("71001", "fake_venue"),
    kind="image",
    caption=None,
    children=0,
    location=None,
    likes=5,
    views=50,
    sig="a",
):
    out = {
        "id": None,
        "shortcode": code,
        "taken_at_timestamp": ts,
        "owner": {"id": owner[0], "username": owner[1]},
        "display_url": f"https://cdn.invalid/{code}.jpg?oe=FAKE{sig}",
        "thumbnail_src": f"https://cdn.invalid/{code}_t.jpg?oe=FAKE{sig}",
        "edge_liked_by": {"count": likes},
        "edge_media_preview_like": {"count": likes},
        "edge_media_to_comment": {"count": likes // 2},
        "dimensions": {"height": 1080, "width": 1080},
        "accessibility_caption": "Invented description of a synthetic picture.",
        "edge_media_to_caption": {
            "edges": [{"node": {"text": caption}}] if caption is not None else []
        },
        "pinned_for_users": [],
    }
    out["id"] = "90" + "".join(str(ord(c) % 10) for c in code)
    if kind == "image":
        out.update(__typename="GraphImage", is_video=False, product_type="feed")
    elif kind == "video":
        out.update(
            __typename="GraphVideo",
            is_video=True,
            product_type="feed",
            video_view_count=views,
            video_duration=12.5,
        )
    elif kind == "reel":
        out.update(
            __typename="GraphVideo",
            is_video=True,
            product_type="clips",
            video_view_count=views,
            video_duration=30.0,
        )
    elif kind == "carousel":
        out.update(
            __typename="GraphSidecar", is_video=False, product_type="carousel_container"
        )
        out["edge_sidecar_to_children"] = {
            "edges": [
                {
                    "node": {
                        "__typename": "GraphImage",
                        "id": f"{out['id']}{i}",
                        "shortcode": f"{code}c{i}",
                        "is_video": False,
                        "display_url": f"https://cdn.invalid/{code}{i}.jpg?oe=FAKE{sig}",
                    }
                }
                for i in range(children or 2)
            ]
        }
    if location:
        out["location"] = {
            "id": location[0],
            "name": location[1],
            "slug": "fake-place",
            "has_public_page": True,
        }
    return out


def profile(handle, uid, nodes, *, private=False, followers=10):
    return {
        "data": {
            "user": {
                "id": uid,
                "username": handle,
                "full_name": f"Invented {handle}",
                "biography": "Synthetic biography.",
                "is_private": private,
                "is_verified": False,
                "profile_pic_url": f"https://cdn.invalid/{handle}.jpg?oe=FAKE",
                "edge_followed_by": {"count": followers},
                "edge_owner_to_timeline_media": {
                    "count": len(nodes),
                    "page_info": {"has_next_page": False, "end_cursor": None},
                    "edges": [{"node": n} for n in nodes],
                },
            }
        },
        "status": "ok",
    }


def world():
    """Three invented accounts, one collaboration post shared by two."""
    collab = node(
        "FAKECOLLAB1",
        BASE_TS + 500,
        owner=("71001", "fake_venue"),
        caption="Joint show with @fake_records #LiveMusic",
        kind="image",
    )
    collab["coauthor_producers"] = [{"id": "71002", "username": "fake_records"}]
    venue = [
        node(
            "FAKEVEN01",
            BASE_TS + 100,
            caption="Doors at 8. #friday #LiveMusic",
            location=("5501", "Invented Hall"),
        ),
        node("FAKEVEN02", BASE_TS + 200, kind="reel", caption="Soundcheck clip"),
        node(
            "FAKEVEN03",
            BASE_TS + 300,
            kind="carousel",
            children=3,
            caption="Photo dump from the invented night",
        ),
        collab,
    ]
    records = [
        node(
            "FAKEREC01",
            BASE_TS + 1100,
            owner=("71002", "fake_records"),
            kind="video",
            caption="New crate arrivals",
        ),
        node(
            "FAKEREC02", BASE_TS + 1200, owner=("71002", "fake_records"), caption=None
        ),
        collab,
    ]
    art = [
        node(
            "FAKEART01",
            BASE_TS + 2100,
            owner=("71003", "fake_gallery"),
            caption="Opening night, invented artist @fake_painter",
        ),
        node(
            "FAKEART02",
            BASE_TS + 2200,
            owner=("71003", "fake_gallery"),
            kind="carousel",
            caption="Install views",
        ),
    ]
    return {
        "fake_venue": profile("fake_venue", "71001", venue),
        "fake_records": profile("fake_records", "71002", records),
        "fake_gallery": profile("fake_gallery", "71003", art),
    }


ACCOUNTS = [
    {"handle": "fake_venue", "label": "Invented Venue", "category": "venues/music"},
    {"handle": "fake_records", "label": "Invented Records", "category": "records"},
    {"handle": "fake_gallery", "category": "art"},
]


def story_item(
    sid,
    ts,
    *,
    owner=("71001", "fake_venue"),
    is_video=False,
    caption=None,
    expiring_at=None,
    links=None,
    mentions=None,
    hashtags=None,
    location=None,
    poll=None,
    views=42,
    sig="a",
):
    out = {
        "pk": str(sid),
        "id": f"{sid}_{owner[0]}",
        "code": f"STORY{sid[-4:]}",
        "taken_at": ts,
        "expiring_at": expiring_at if expiring_at is not None else ts + 86400,
        "media_type": 2 if is_video else 1,
        "product_type": "story",
        "user": {"pk": owner[0], "username": owner[1]},
        "image_versions2": {
            "candidates": [{"url": f"https://cdn.invalid/s_{sid}.jpg?oe=FAKE{sig}"}]
        },
        "total_viewer_count": views,
        "accessibility_caption": "Invented story image description.",
    }
    if is_video:
        out["video_duration"] = 14.5
        out["video_versions"] = [{"url": f"https://cdn.invalid/s_{sid}.mp4?oe=FAKE{sig}"}]
    if caption is not None:
        out["caption"] = {"text": caption}
    if links:
        out["story_link_stickers"] = [
            {"story_link": {"url": u, "link_title": t}} for u, t in links
        ]
    if mentions:
        out["reel_mentions"] = [{"user": {"username": m}} for m in mentions]
    if hashtags:
        out["story_hashtags"] = [{"hashtag": {"name": h}} for h in hashtags]
    if location:
        out["story_locations"] = [
            {"location": {"pk": location[0], "name": location[1]}}
        ]
    if poll:
        out["story_polls"] = [
            {
                "poll_sticker": {
                    "question": poll[0],
                    "tallies": [{"text": opt} for opt in poll[1]],
                }
            }
        ]
    return out


def profile_html(handle, uid, nodes, *, latest_reel_media=0):
    grid_edges = []
    for n in nodes:
        code = n.get("shortcode") or n.get("code")
        cap_edges = ((n.get("edge_media_to_caption") or {}).get("edges")) or []
        cap_text = (
            ((cap_edges[0] or {}).get("node") or {}).get("text") if cap_edges else None
        )
        grid_edges.append(
            {
                "node": {
                    "__typename": (
                        "XIGPolarisCarouselMedia"
                        if n.get("product_type") == "carousel_container"
                        else (
                            "XIGPolarisVideoMedia"
                            if n.get("is_video")
                            else "XIGPolarisImageMedia"
                        )
                    ),
                    "pk": n.get("id") or n.get("pk"),
                    "id": f"POLARIS_{n.get('id') or n.get('pk')}",
                    "code": code,
                    "accessibility_caption": n.get("accessibility_caption"),
                    "caption": {"text": cap_text} if cap_text is not None else None,
                    "media_type": 8
                    if n.get("product_type") == "carousel_container"
                    else (2 if n.get("is_video") else 1),
                    "product_type": n.get("product_type") or "feed",
                    "user": {
                        "username": (n.get("owner") or {}).get("username") or handle,
                        "id": "178414000000" + str(uid),
                    },
                }
            }
        )
    s1 = json.dumps(
        {
            "require": [
                {
                    "xig_user_by_username": {
                        "pk": str(uid),
                        "id": "178414000000" + str(uid),
                        "username": handle,
                        "full_name": f"Invented {handle}",
                        "biography": "Synthetic HTML bio.",
                        "is_private": False,
                        "is_verified": False,
                        "latest_reel_media": latest_reel_media,
                    }
                }
            ]
        }
    )
    s2 = json.dumps(
        {
            "require": [
                {
                    "xig_user_by_username": {
                        "pk": str(uid),
                        "polaris_ordered_timeline_connection": {
                            "count": len(grid_edges),
                            "edges": grid_edges,
                        },
                    }
                }
            ]
        }
    )
    return (
        "<!DOCTYPE html><html><head></head><body>"
        f'<script type="application/json">{s1}</script>'
        f'<script type="application/json">{s2}</script>'
        "</body></html>"
    ).encode("utf-8")


def post_detail_html(n):
    owner = n.get("owner") or {}
    uid = owner.get("pk") or owner.get("id") or "71001"
    cap_edges = ((n.get("edge_media_to_caption") or {}).get("edges")) or []
    cap_text = (
        ((cap_edges[0] or {}).get("node") or {}).get("text") if cap_edges else None
    )
    children = ((n.get("edge_sidecar_to_children") or {}).get("edges")) or []
    inner = {
        "pk": n.get("id") or n.get("pk"),
        "id": f"POLARIS_{n.get('id') or n.get('pk')}",
        "code": n.get("shortcode") or n.get("code"),
        "taken_at": n.get("taken_at_timestamp") or n.get("taken_at"),
        "media_type": 8
        if n.get("product_type") == "carousel_container"
        else (2 if n.get("is_video") else 1),
        "product_type": n.get("product_type") or "feed",
        "caption": {"text": cap_text} if cap_text is not None else None,
        "accessibility_caption": n.get("accessibility_caption"),
        "location": n.get("location"),
        "coauthor_producers": n.get("coauthor_producers") or [],
        "video_duration": n.get("video_duration"),
        "user": {
            "pk": str(uid),
            "id": "178414000000" + str(uid),
            "username": owner.get("username"),
        },
    }
    if children:
        inner["carousel_media"] = [
            {"pk": c["node"]["id"], "media_type": 2 if c["node"].get("is_video") else 1}
            for c in children
        ]
    script = json.dumps({"xig_polaris_media": {"if_not_gated_logged_out": inner}})
    return (
        "<!DOCTYPE html><html><body>"
        f'<script type="application/json">{script}</script>'
        "</body></html>"
    ).encode("utf-8")


class FakeInstagram:
    """Loopback server; ``script[handle]`` is a list of (status, body, headers)."""

    def __init__(self, profiles=None, stories=None):
        self.profiles = dict(profiles or {})
        self.stories = dict(stories or {})
        self.html_profiles = {}
        self.html_posts = {}
        self.script = {}
        self.stories_script = {}
        self.log = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                parts = urlsplit(self.path)
                outer.log.append(
                    {
                        "path": parts.path,
                        "query": parts.query,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                    }
                )
                if parts.path == "/":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Set-Cookie", "csrftoken=fakecsrf123; Path=/")
                    self.end_headers()
                    self.wfile.write(b"<html>invented</html>")
                    return
                if parts.path == "/api/v1/users/web_profile_info/":
                    handle = parse_qs(parts.query).get("username", [""])[0]
                    queue = outer.script.get(handle)
                    if queue:
                        status, body, headers = (
                            queue.pop(0) if len(queue) > 1 else queue[0]
                        )
                    elif handle in outer.profiles:
                        status, body, headers = 200, outer.profiles[handle], {}
                    else:
                        status, body, headers = 404, {"status": "fail"}, {}
                    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    for k, v in headers.items():
                        self.send_header(k, v)
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                if parts.path == "/api/v1/feed/reels_media/":
                    uid = parse_qs(parts.query).get("reel_ids", [""])[0]
                    queue = outer.stories_script.get(uid)
                    if queue:
                        status, body, headers = (
                            queue.pop(0) if len(queue) > 1 else queue[0]
                        )
                    elif uid in outer.stories:
                        status, body, headers = 200, outer.stories[uid], {}
                    else:
                        status, body, headers = (
                            200,
                            {"reels_media": [], "status": "ok"},
                            {},
                        )
                    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    for k, v in headers.items():
                        self.send_header(k, v)
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                segs = [s for s in parts.path.split("/") if s]
                if len(segs) == 1 and segs[0] in outer.html_profiles:
                    raw = outer.html_profiles[segs[0]]
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                if len(segs) == 2 and segs[0] in ("p", "reel") and segs[1] in outer.html_posts:
                    raw = outer.html_posts[segs[1]]
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                self.send_response(404)
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def profile_calls(self):
        return [
            parse_qs(e["query"])["username"][0]
            for e in self.log
            if e["path"] == "/api/v1/users/web_profile_info/"
        ]

    def story_calls(self):
        return [
            parse_qs(e["query"])["reel_ids"][0]
            for e in self.log
            if e["path"] == "/api/v1/feed/reels_media/"
        ]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Synth:
    """Builders handed to tests as a fixture (test modules never import conftest)."""

    node = staticmethod(node)
    story = staticmethod(story_item)
    profile = staticmethod(profile)
    profile_html = staticmethod(profile_html)
    post_detail_html = staticmethod(post_detail_html)
    world = staticmethod(world)
    accounts = ACCOUNTS
    base_ts = BASE_TS
    Server = FakeInstagram


@pytest.fixture
def synth():
    return Synth


@pytest.fixture
def fake_instagram():
    server = FakeInstagram(world())
    try:
        yield server
    finally:
        server.close()


@pytest.fixture(autouse=True)
def clean_source_env(monkeypatch):
    for d in [
        *InstagramFeed.manifest.settings.values(),
        *InstagramFeed.manifest.secrets.values(),
    ]:
        monkeypatch.delenv(d.env, raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
