"""Read visits and bookmarks from private copies of browser databases."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path

WEBKIT_EPOCH = dt.datetime(1601, 1, 1, tzinfo=dt.UTC)
APPLE_EPOCH = dt.datetime(2001, 1, 1, tzinfo=dt.UTC)
UNIX_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)

CHROME_CORE = {
    0: "link", 1: "typed", 2: "auto_bookmark", 3: "auto_subframe", 4: "manual_subframe",
    5: "generated", 6: "auto_toplevel", 7: "form_submit", 8: "reload", 9: "keyword", 10: "keyword_generated",
}
CHROME_SUBFRAMES = {3, 4}
CHAIN_START, CHAIN_END = 0x10000000, 0x20000000
CLIENT_REDIRECT, SERVER_REDIRECT = 0x40000000, 0x80000000
FORWARD_BACK, FROM_ADDRESS_BAR = 0x01000000, 0x02000000
CHROME_SYNCED_SOURCE = 0  # visit_source.source: SOURCE_SYNCED

FIREFOX_TYPES = {
    1: "link", 2: "typed", 3: "bookmark", 4: "embed", 5: "redirect_permanent",
    6: "redirect_temporary", 7: "download", 8: "framed_link", 9: "reload",
}
FIREFOX_SUBFRAMES = {4, 8}
FIREFOX_SYNCED_SOURCE = 1  # moz_historyvisits.source: SOURCE_SYNCED
FIREFOX_ROOTS = {"menu": "Bookmarks Menu", "toolbar": "Bookmarks Toolbar", "unfiled": "Other Bookmarks", "mobile": "Mobile Bookmarks"}


def webkit_time(us):
    return WEBKIT_EPOCH + dt.timedelta(microseconds=int(us))


def unix_us_time(us):
    return UNIX_EPOCH + dt.timedelta(microseconds=int(us))


def apple_time(seconds):
    return APPLE_EPOCH + dt.timedelta(seconds=float(seconds))


@contextmanager
def snapshot(path):
    """Copy a database with its -wal/-shm/-journal to a temporary folder;
    the live file is never opened (browsers hold locks on it)."""
    path = Path(path)
    with tempfile.TemporaryDirectory(prefix="tl-browser-") as folder:
        target = Path(folder) / path.name
        shutil.copy2(path, target)
        for suffix in ("-wal", "-shm", "-journal"):
            side = path.with_name(path.name + suffix)
            if side.is_file():
                shutil.copy2(side, target.with_name(target.name + suffix))
        conn = sqlite3.connect(target)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()


def columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


# --- visits -------------------------------------------------------------------
# Each reader yields dicts: id, at (aware datetime), url, title, transition,
# typed, duration_s, from_id, from_at, referrer, synced, redirect_source,
# raw (source values for the payload), raw_time.


def chromium_visits(conn, after_id, since_raw=None, limit=1000):
    visit_cols = columns(conn, "visits")
    has_source = "visit_source" in tables(conn)
    origin = "v.originator_cache_guid" if "originator_cache_guid" in visit_cols else "NULL"
    source = "vs.source" if has_source else "NULL"
    join = "LEFT JOIN visit_source vs ON vs.id = v.id" if has_source else ""
    where = "v.visit_time > ?" if since_raw is not None else "v.id > ?"
    start = since_raw if since_raw is not None else after_id
    if since_raw is not None:
        limit = -1
    while True:
        rows = conn.execute(
            f"SELECT v.id, v.visit_time, v.transition, v.visit_duration, v.from_visit, u.url, u.title, "
            f"{origin} AS origin, {source} AS source, fv.visit_time AS from_time, fu.url AS referrer "
            f"FROM visits v JOIN urls u ON u.id = v.url "
            f"LEFT JOIN visits fv ON fv.id = v.from_visit LEFT JOIN urls fu ON fu.id = fv.url {join} "
            f"WHERE {where} ORDER BY {'v.visit_time, v.id' if since_raw is not None else 'v.id'} LIMIT ?",
            (start, limit),
        ).fetchall()
        if not rows:
            return
        for row in rows:
            transition = int(row["transition"] or 0)
            core = transition & 0xFF
            chained = transition & (CHAIN_START | CHAIN_END)
            yield {
                "id": row["id"],
                "raw_time": row["visit_time"],
                "at": webkit_time(row["visit_time"]),
                "url": row["url"],
                "title": row["title"],
                "transition": CHROME_CORE.get(core, f"type_{core}"),
                "subframe": core in CHROME_SUBFRAMES,
                "typed": core == 1 or bool(transition & FROM_ADDRESS_BAR),
                "forward_back": bool(transition & FORWARD_BACK),
                "redirect": bool(transition & (CLIENT_REDIRECT | SERVER_REDIRECT)),
                "redirect_source": bool(chained) and not transition & CHAIN_END,
                "duration_s": round(row["visit_duration"] / 1e6, 3) if row["visit_duration"] else None,
                "from_id": row["from_visit"] or None,
                "from_raw_time": row["from_time"],
                "referrer": row["referrer"],
                "synced": bool(row["origin"]) or row["source"] == CHROME_SYNCED_SOURCE,
                "raw": {"transition": transition, "visit_duration_us": row["visit_duration"]},
            }
        if since_raw is not None:
            return  # recovery mode reads everything newer in one query
        start = rows[-1]["id"]


def firefox_visits(conn, after_id, since_raw=None, limit=1000):
    has_source = "source" in columns(conn, "moz_historyvisits")
    source = "v.source" if has_source else "NULL"
    where = "v.visit_date > ?" if since_raw is not None else "v.id > ?"
    start = since_raw if since_raw is not None else after_id
    if since_raw is not None:
        limit = -1
    while True:
        rows = conn.execute(
            f"SELECT v.id, v.visit_date, v.visit_type, v.from_visit, p.url, p.title, {source} AS source, "
            f"fv.visit_date AS from_time, fp.url AS referrer, "
            f"EXISTS (SELECT 1 FROM moz_historyvisits r WHERE r.from_visit = v.id AND r.visit_type IN (5, 6)) AS redirects "
            f"FROM moz_historyvisits v JOIN moz_places p ON p.id = v.place_id "
            f"LEFT JOIN moz_historyvisits fv ON fv.id = v.from_visit LEFT JOIN moz_places fp ON fp.id = fv.place_id "
            f"WHERE {where} ORDER BY {'v.visit_date, v.id' if since_raw is not None else 'v.id'} LIMIT ?",
            (start, limit),
        ).fetchall()
        if not rows:
            return
        for row in rows:
            kind = int(row["visit_type"] or 0)
            yield {
                "id": row["id"],
                "raw_time": row["visit_date"],
                "at": unix_us_time(row["visit_date"]),
                "url": row["url"],
                "title": row["title"],
                "transition": FIREFOX_TYPES.get(kind, f"type_{kind}"),
                "subframe": kind in FIREFOX_SUBFRAMES,
                "typed": kind == 2,
                "forward_back": False,
                "redirect": kind in (5, 6),
                "redirect_source": bool(row["redirects"]),
                "duration_s": None,
                "from_id": row["from_visit"] or None,
                "from_raw_time": row["from_time"],
                "referrer": row["referrer"],
                "synced": row["source"] == FIREFOX_SYNCED_SOURCE,
                "raw": {"visit_type": kind},
            }
        if since_raw is not None:
            return  # recovery mode reads everything newer in one query
        start = rows[-1]["id"]


def safari_visits(conn, after_id, since_raw=None, limit=1000):
    visit_cols = columns(conn, "history_visits")
    origin = "v.origin" if "origin" in visit_cols else "0"
    where = "v.visit_time > ?" if since_raw is not None else "v.id > ?"
    start = since_raw if since_raw is not None else after_id
    if since_raw is not None:
        limit = -1
    while True:
        rows = conn.execute(
            f"SELECT v.id, v.visit_time, v.title, v.load_successful, v.redirect_source, v.redirect_destination, "
            f"{origin} AS origin, i.url, fv.visit_time AS from_time, fi.url AS referrer "
            f"FROM history_visits v JOIN history_items i ON i.id = v.history_item "
            f"LEFT JOIN history_visits fv ON fv.id = v.redirect_source LEFT JOIN history_items fi ON fi.id = fv.history_item "
            f"WHERE {where} ORDER BY {'v.visit_time, v.id' if since_raw is not None else 'v.id'} LIMIT ?",
            (start, limit),
        ).fetchall()
        if not rows:
            return
        for row in rows:
            yield {
                "id": row["id"],
                "raw_time": row["visit_time"],
                "at": apple_time(row["visit_time"]),
                "url": row["url"],
                "title": row["title"],
                "transition": "redirect" if row["redirect_source"] else "visit",
                "subframe": False,
                "typed": False,
                "forward_back": False,
                "redirect": bool(row["redirect_source"]),
                "redirect_source": row["redirect_destination"] is not None,
                "duration_s": None,
                "from_id": row["redirect_source"] or None,
                "from_raw_time": row["from_time"],
                "referrer": row["referrer"],
                "synced": bool(row["origin"]),
                "raw": {"load_successful": row["load_successful"], "origin": row["origin"]},
            }
        if since_raw is not None:
            return  # recovery mode reads everything newer in one query
        start = rows[-1]["id"]


VISIT_TABLES = {
    "chromium": ("visits", "id", "visit_time", chromium_visits),
    "firefox": ("moz_historyvisits", "id", "visit_date", firefox_visits),
    "safari": ("history_visits", "id", "visit_time", safari_visits),
}


def visit_bounds(conn, family):
    """(max id, min raw time, max raw time) of the visits table."""
    table, ident, when, _ = VISIT_TABLES[family]
    row = conn.execute(f"SELECT max({ident}), min({when}), max({when}) FROM {table}").fetchone()
    return row[0], row[1], row[2]


def to_time(family, raw):
    if raw is None:
        return None
    return {"chromium": webkit_time, "firefox": unix_us_time, "safari": apple_time}[family](raw)


# --- bookmarks ------------------------------------------------------------------
# Each yields dicts: guid, url, title, folder, at (aware datetime or None).


def chromium_bookmarks(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))

    def walk(node, trail):
        if node.get("type") == "url":
            added = node.get("date_added")
            yield {
                "guid": node.get("guid") or node.get("id"),
                "url": node.get("url"),
                "title": node.get("name"),
                "folder": "/".join(trail),
                "at": webkit_time(int(added)) if added and str(added).isdigit() and int(added) > 0 else None,
            }
        for child in node.get("children") or []:
            yield from walk(child, trail + ([node.get("name")] if node.get("type") == "folder" and node.get("name") else []))

    for key, root in sorted((data.get("roots") or {}).items()):
        if isinstance(root, dict):
            yield from walk(root, [])


def chromium_signature(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]


def firefox_bookmarks(conn):
    folders = {
        r["id"]: (r["parent"], r["title"], r["guid"])
        for r in conn.execute("SELECT id, parent, title, guid FROM moz_bookmarks WHERE type = 2")
    }

    def trail(parent):
        names, seen = [], set()
        while parent in folders and parent not in seen:
            seen.add(parent)
            up, title, guid = folders[parent]
            root = (guid or "").rstrip("_")
            if root == "root":
                break
            names.append(FIREFOX_ROOTS.get(root) or title or "")
            parent = up
        return "/".join(n for n in reversed(names) if n)

    for row in conn.execute(
        "SELECT b.guid, b.title, b.dateAdded, b.parent, p.url FROM moz_bookmarks b "
        "JOIN moz_places p ON p.id = b.fk WHERE b.type = 1 ORDER BY b.id"
    ):
        yield {
            "guid": row["guid"],
            "url": row["url"],
            "title": row["title"],
            "folder": trail(row["parent"]),
            "at": unix_us_time(row["dateAdded"]) if row["dateAdded"] else None,
        }


def firefox_signature(conn):
    row = conn.execute("SELECT count(*), max(lastModified), sum(id) FROM moz_bookmarks").fetchone()
    return f"{row[0]}:{row[1]}:{row[2]}"
