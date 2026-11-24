"""A fake Readwise API over the invented recordings in recorded/.

It applies the real APIs' semantics as documented: token auth, opaque
pageCursor/nextPageCursor paging, updatedAfter filtering (export: only the
highlights updated after the time, books without such highlights dropped;
Reader: documents updated after it), ids= for whole books, and pages that are
not ordered by update time. The network is never touched."""

import json
from datetime import datetime
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
TOKEN = "invented-readwise-token-0000"
EXPORT_PAGE = 3
READER_PAGE = 4


def load(name):
    return json.loads((HERE / "recorded" / name).read_text())


def when(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def paged(items, params, size):
    start = int(params.get("pageCursor") or 0)
    chunk = items[start : start + size]
    following = start + size if start + size < len(items) else None
    return {"count": len(items), "nextPageCursor": str(following) if following else None, "results": chunk}


def handler(export=None, reader=None, requests=None, fail=None):
    export = load("readwise_export.json") if export is None else export
    reader = load("readwise_reader.json") if reader is None else reader

    def handle(request):
        params = dict(request.url.params)
        if requests is not None:
            requests.append((request.url.path, params))
        if request.headers.get("Authorization") != f"Token {TOKEN}":
            return httpx.Response(401, json={"detail": "Invalid token."})
        if fail and fail(request):
            return httpx.Response(503, json={"detail": "unavailable"})
        after = when(params["updatedAfter"]) if params.get("updatedAfter") else None
        if request.url.path == "/api/v2/export/":
            books = export
            if params.get("ids"):
                wanted = {int(i) for i in params["ids"].split(",")}
                books = [b for b in books if b["user_book_id"] in wanted]
            elif after:
                books = [
                    dict(b, highlights=kept)
                    for b in books
                    if (kept := [h for h in b["highlights"] if when(h["updated_at"]) > after])
                ]
            # Not in update order: the real export is ordered by book, not time.
            books = sorted(books, key=lambda b: -b["user_book_id"])
            return httpx.Response(200, json=paged(books, params, EXPORT_PAGE))
        if request.url.path == "/api/v3/list/":
            docs = [d for d in reader if after is None or when(d["updated_at"]) > after]
            docs = sorted(docs, key=lambda d: d["id"], reverse=True)
            return httpx.Response(200, json=paged(docs, params, READER_PAGE))
        return httpx.Response(404, json={"detail": "Not found."})

    return handle


def transport():
    return httpx.MockTransport(handler())


# The contract kit compares whole-store snapshots through /v1/sql, whose
# agent-view response is capped at about 45,000 characters, so it runs on a
# subset that still covers every channel, notes, discards and several batches.
CONTRACT_BOOKS = {101, 103, 106, 108}
CONTRACT_DOCS = {"01invented0000", "01invented0001", "01invented0002", "01invented0003", "01inventedchild0"}


def contract_transport():
    export = [b for b in load("readwise_export.json") if b["user_book_id"] in CONTRACT_BOOKS]
    reader = [d for d in load("readwise_reader.json") if d["id"] in CONTRACT_DOCS]
    return httpx.MockTransport(handler(export=export, reader=reader))
