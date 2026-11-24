"""Synthetic Reddit data and a loopback fake Reddit server. No real data."""

import base64
import json
import os
import threading
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

OWNER = "invented_owner"
CLIENT_ID = "invented-client-id"
CLIENT_SECRET = "invented-client-secret-canary"
REFRESH_TOKEN = "invented-refresh-token-canary"
PASSWORD = "invented-password-canary"


@pytest.fixture(autouse=True)
def clean_reddit_env(monkeypatch):
    """Never inherit real REDDIT_* settings; subprocesses inherit this env."""
    for name in list(os.environ):
        if name.startswith("REDDIT_"):
            monkeypatch.delenv(name, raising=False)


def ts(text):
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()


def post(
    id36, sub, title, author, created, selftext="", url=None, score=1, edited=False
):
    permalink = f"/r/{sub}/comments/{id36}/invented_slug/"
    return {
        "kind": "t3",
        "data": {
            "name": f"t3_{id36}",
            "id": id36,
            "subreddit": sub,
            "title": title,
            "selftext": selftext,
            "author": author,
            "permalink": permalink,
            "url": url or "https://www.reddit.com" + permalink,
            "is_self": url is None,
            "domain": f"self.{sub}" if url is None else "example.invalid",
            "score": score,
            "num_comments": 2,
            "upvote_ratio": 0.9,
            "over_18": False,
            "link_flair_text": None,
            "created_utc": ts(created),
            "edited": edited,
        },
    }


def comment(id36, sub, link_id, link_title, author, body, created, score=1):
    return {
        "kind": "t1",
        "data": {
            "name": f"t1_{id36}",
            "id": id36,
            "subreddit": sub,
            "link_id": link_id,
            "link_title": link_title,
            "parent_id": link_id,
            "author": author,
            "body": body,
            "permalink": f"/r/{sub}/comments/{link_id[3:]}/invented_slug/{id36}/",
            "score": score,
            "created_utc": ts(created),
            "edited": False,
        },
    }


def item(listing, child, observed):
    return {
        "kind": "reddit.item",
        "listing": listing,
        "id": child["data"]["name"],
        "account": OWNER,
        "source": "oauth",
        "observed_at": observed,
        "raw": child,
    }


GARDEN, COOK = "InventedGardening", "InventedCooking"


def export_one():
    at = "2025-03-01T12:00:00+00:00"
    return [
        item(
            "saved",
            post(
                "aa01",
                GARDEN,
                "Invented raised bed question",
                "invented_author_a",
                "2025-02-10T10:00:00",
                "Which invented soil mix?",
                score=10,
            ),
            at,
        ),
        item(
            "saved",
            comment(
                "cc01",
                COOK,
                "t3_aa02",
                "Invented bread thread",
                "invented_author_b",
                "Use an invented starter.",
                "2025-02-12T09:30:00",
            ),
            at,
        ),
        item(
            "upvoted",
            post(
                "aa03",
                COOK,
                "Invented recipe link",
                "invented_author_c",
                "2025-02-20T18:00:00",
                url="https://example.invalid/invented-recipe",
            ),
            at,
        ),
        item(
            "submitted",
            post(
                "aa04",
                GARDEN,
                "Invented compost update",
                OWNER,
                "2025-02-25T15:00:00",
                "First invented paragraph.",
            ),
            at,
        ),
        item(
            "comments",
            comment(
                "cc02",
                GARDEN,
                "t3_aa01",
                "Invented raised bed question",
                OWNER,
                "Invented reply one.",
                "2025-02-26T11:00:00",
            ),
            at,
        ),
        item(
            "comments",
            comment(
                "cc03",
                GARDEN,
                "t3_aa01",
                "Invented raised bed question",
                OWNER,
                "Invented reply two.",
                "2025-02-27T11:00:00",
            ),
            at,
        ),
        {"kind": "reddit.export_summary", "fetcher_version": "1", "requests": 6},
    ]


def export_two():
    at = "2025-03-02T12:00:00+00:00"
    return [
        # Only the score changed: no new version.
        item(
            "saved",
            post(
                "aa01",
                GARDEN,
                "Invented raised bed question",
                "invented_author_a",
                "2025-02-10T10:00:00",
                "Which invented soil mix?",
                score=25,
            ),
            at,
        ),
        item(
            "saved",
            post(
                "aa05",
                COOK,
                "Invented knife care",
                "invented_author_g",
                "2025-03-01T20:00:00",
                "Invented whetstone notes.",
            ),
            at,
        ),
        # The vote flipped from up to down.
        item(
            "downvoted",
            post(
                "aa03",
                COOK,
                "Invented recipe link",
                "invented_author_c",
                "2025-02-20T18:00:00",
                url="https://example.invalid/invented-recipe",
            ),
            at,
        ),
        # The owner edited the post.
        item(
            "submitted",
            post(
                "aa04",
                GARDEN,
                "Invented compost update",
                OWNER,
                "2025-02-25T15:00:00",
                "First invented paragraph, edited.",
                edited=ts("2025-03-01T08:00:00"),
            ),
            at,
        ),
        item(
            "comments",
            comment(
                "cc04",
                GARDEN,
                "t3_aa04",
                "Invented compost update",
                OWNER,
                "Invented follow-up.",
                "2025-03-02T08:00:00",
            ),
            at,
        ),
        {"kind": "reddit.export_summary", "fetcher_version": "1", "requests": 5},
    ]


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    return path


@pytest.fixture
def synthetic():
    return SimpleNamespace(
        post=post,
        comment=comment,
        ts=ts,
        export_one=export_one,
        export_two=export_two,
        write_jsonl=write_jsonl,
        OWNER=OWNER,
        CLIENT_ID=CLIENT_ID,
        CLIENT_SECRET=CLIENT_SECRET,
        REFRESH_TOKEN=REFRESH_TOKEN,
        PASSWORD=PASSWORD,
        GARDEN=GARDEN,
        COOK=COOK,
    )


@pytest.fixture
def export_dir(tmp_path):
    folder = tmp_path / "exports"
    write_jsonl(folder / "reddit-20250301T120000Z.jsonl", export_one())
    write_jsonl(folder / "reddit-20250302T120000Z.jsonl", export_two())
    return folder


class FakeReddit:
    """Loopback stand-in for www.reddit.com and oauth.reddit.com."""

    def __init__(self):
        self.listings = {
            name: []
            for name in ("saved", "upvoted", "downvoted", "submitted", "comments")
        }
        self.redirects = set()
        self.failures = {}  # path -> [(status, headers)] answered before success
        self.extra_headers = {}  # path -> headers added to successful answers
        self.requests = []
        self.tokens = set()
        self.issued = 0
        self.token_error = None
        self.lock = threading.Lock()

    def issue(self):
        with self.lock:
            self.issued += 1
            token = f"invented-access-token-{self.issued}"
            self.tokens.add(token)
            return token

    def handle(self, handler, method):
        parts = urllib.parse.urlsplit(handler.path)
        query = dict(urllib.parse.parse_qsl(parts.query))
        length = int(handler.headers.get("Content-Length") or 0)
        body = handler.rfile.read(length).decode() if length else ""
        self.requests.append(
            SimpleNamespace(
                method=method,
                path=parts.path,
                query=query,
                headers=dict(handler.headers.items()),
                body=body,
            )
        )
        queued = self.failures.get(parts.path)
        if queued:
            status, headers = queued.pop(0)
            return status, headers, b"{}"
        if parts.path in self.redirects:
            return 302, {"Location": "https://www.reddit.invalid/quarantine"}, b""
        if method == "POST" and parts.path == "/api/v1/access_token":
            return self.token(handler, body)
        auth = handler.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] not in self.tokens:
            return 401, {}, b'{"message": "Unauthorized", "error": 401}'
        if parts.path == "/api/v1/me":
            return 200, {}, json.dumps({"name": OWNER}).encode()
        segments = parts.path.strip("/").split("/")
        if len(segments) == 3 and segments[0] == "user" and segments[1] == OWNER:
            return self.page(self.listings.get(segments[2]), query, parts.path)
        return 404, {}, b"{}"

    def page(self, children, query, path):
        if children is None:
            return 404, {}, b"{}"
        limit = int(query.get("limit", 25))
        start = 0
        if query.get("after"):
            names = [c["data"]["name"] for c in children]
            start = names.index(query["after"]) + 1
        chunk = children[start : start + limit]
        after = (
            chunk[-1]["data"]["name"]
            if start + limit < len(children) and chunk
            else None
        )
        payload = {
            "kind": "Listing",
            "data": {"after": after, "before": None, "children": chunk},
        }
        return 200, dict(self.extra_headers.get(path, {})), json.dumps(payload).encode()

    def token(self, handler, body):
        expected = (
            "Basic "
            + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        )
        if handler.headers.get("Authorization") != expected:
            return 401, {}, b'{"error": 401}'
        form = dict(urllib.parse.parse_qsl(body))
        ok = (
            form.get("grant_type") == "refresh_token"
            and form.get("refresh_token") == REFRESH_TOKEN
        ) or (
            form.get("grant_type") == "password"
            and form.get("username") == OWNER
            and form.get("password") == PASSWORD
        )
        if self.token_error or not ok:
            return (
                200,
                {},
                json.dumps({"error": self.token_error or "invalid_grant"}).encode(),
            )
        payload = {
            "access_token": self.issue(),
            "token_type": "bearer",
            "expires_in": 3600,
            "scope": "*",
        }
        return 200, {}, json.dumps(payload).encode()


@pytest.fixture
def fake_reddit():
    fake = FakeReddit()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def answer(self, method):
            status, headers, body = fake.handle(self, method)
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.answer("GET")

        def do_POST(self):
            self.answer("POST")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    fake.url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield fake
    finally:
        server.shutdown()
        server.server_close()
