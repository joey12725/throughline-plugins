"""An offline AT Protocol PDS + AppView for tests and the contract kit.

`BLUESKY_FIXTURE` names a JSON file:

    {"did": ..., "handle": ..., "password": ..., "did_doc": {...},
     "session_did_doc": true,
     "records": {collection: [{"uri", "cid", "value"}, ...]},
     "posts": {uri: postView, ...},
     "notifications": [notification, ...]}

The transport answers createSession, refreshSession, the PLC directory,
listRecords (newest first, cursor = last rkey, like the reference PDS),
getPosts and listNotifications (newest first by indexedAt).
"""

import json
from pathlib import Path

import httpx


def rkey(uri):
    return uri.rsplit("/", 1)[-1]


class FakeNetwork:
    def __init__(self, data):
        self.data = data
        self.access = "invented-access-jwt"
        self.refresh = "invented-refresh-jwt"
        self.expire_next = 0  # Answer ExpiredToken to this many authed reads.
        self.requests = []

    def session(self):
        body = {
            "did": self.data["did"],
            "handle": self.data["handle"],
            "accessJwt": self.access,
            "refreshJwt": self.refresh,
        }
        if self.data.get("session_did_doc", True):
            body["didDoc"] = self.data["did_doc"]
        return body

    def handler(self, request):
        self.requests.append(request)
        path = request.url.path
        q = request.url.params
        if path == "/xrpc/com.atproto.server.createSession":
            body = json.loads(request.content or b"{}")
            if body.get("identifier") not in (self.data["handle"], self.data["did"]) or body.get(
                "password"
            ) != self.data.get("password"):
                return httpx.Response(
                    401, json={"error": "AuthenticationRequired", "message": "Invalid identifier or password"}
                )
            return httpx.Response(200, json=self.session())
        if path == "/xrpc/com.atproto.server.refreshSession":
            if request.headers.get("authorization") != "Bearer " + self.refresh:
                return httpx.Response(400, json={"error": "ExpiredToken"})
            self.access += "-r"
            return httpx.Response(200, json=self.session())
        if path == "/" + self.data["did"]:  # plc.directory/<did>
            return httpx.Response(200, json=self.data["did_doc"])
        if request.headers.get("authorization") != "Bearer " + self.access:
            return httpx.Response(400, json={"error": "ExpiredToken", "message": "Token has expired"})
        if self.expire_next:
            self.expire_next -= 1
            return httpx.Response(400, json={"error": "ExpiredToken", "message": "Token has expired"})
        limit = int(q.get("limit") or 50)
        if path == "/xrpc/com.atproto.repo.listRecords":
            if q.get("repo") not in (self.data["did"], self.data["handle"]):
                return httpx.Response(400, json={"error": "InvalidRequest"})
            rows = sorted(
                self.data.get("records", {}).get(q.get("collection"), []),
                key=lambda r: rkey(r["uri"]),
                reverse=True,
            )
            if q.get("cursor"):
                rows = [r for r in rows if rkey(r["uri"]) < q["cursor"]]
            page = rows[:limit]
            body = {"records": page}
            if len(rows) > limit:
                body["cursor"] = rkey(page[-1]["uri"])
            return httpx.Response(200, json=body)
        if path == "/xrpc/app.bsky.feed.getPosts":
            posts = self.data.get("posts", {})
            return httpx.Response(
                200, json={"posts": [posts[u] for u in q.get_list("uris") if u in posts]}
            )
        if path == "/xrpc/app.bsky.notification.listNotifications":
            rows = sorted(
                self.data.get("notifications", []),
                key=lambda n: (n["indexedAt"], n["uri"]),
                reverse=True,
            )
            start = int(q.get("cursor") or 0)
            page = rows[start : start + limit]
            body = {"notifications": page}
            if start + limit < len(rows):
                body["cursor"] = str(start + limit)
            return httpx.Response(200, json=body)
        return httpx.Response(404, json={"error": "MethodNotImplemented"})

    def transport(self):
        return httpx.MockTransport(self.handler)


def from_file(path):
    return FakeNetwork(json.loads(Path(path).read_text(encoding="utf-8")))
