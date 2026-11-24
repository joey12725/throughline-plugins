"""Synthetic browser databases following each browser's documented schema.

Every URL, title and profile name is invented (example.com / example.org).
"""

import json
import sqlite3
from pathlib import Path

WEBKIT_OFFSET_S = 11_644_473_600  # seconds between 1601-01-01 and 1970-01-01
APPLE_OFFSET_S = 978_307_200
BASE = 1_735_689_600  # 2025-01-01T00:00:00Z


def webkit(unix):
    return (unix + WEBKIT_OFFSET_S) * 1_000_000


def chrome(folder, visits=None, bookmarks=True, name="Invented Person"):
    """A Chromium profile folder: History (+ Bookmarks). visits: list of
    (id, url, title, unix seconds, transition, duration_us, from_visit)."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(folder / "History")
    conn.executescript(
        """
        CREATE TABLE urls(id INTEGER PRIMARY KEY AUTOINCREMENT, url LONGVARCHAR, title LONGVARCHAR,
          visit_count INTEGER DEFAULT 0 NOT NULL, typed_count INTEGER DEFAULT 0 NOT NULL,
          last_visit_time INTEGER NOT NULL, hidden INTEGER DEFAULT 0 NOT NULL);
        CREATE TABLE visits(id INTEGER PRIMARY KEY, url INTEGER NOT NULL, visit_time INTEGER NOT NULL,
          from_visit INTEGER, transition INTEGER DEFAULT 0 NOT NULL, segment_id INTEGER,
          visit_duration INTEGER DEFAULT 0 NOT NULL, incremented_omnibox_typed_score BOOLEAN DEFAULT FALSE NOT NULL,
          opener_visit INTEGER, originator_cache_guid TEXT, originator_visit_id INTEGER);
        CREATE TABLE visit_source(id INTEGER PRIMARY KEY, source INTEGER NOT NULL);
        """
    )
    visits = visits if visits is not None else default_chrome_visits()
    add_chrome_visits(conn, visits)
    conn.close()
    if bookmarks:
        (folder / "Bookmarks").write_text(json.dumps({
            "version": 1,
            "roots": {
                "bookmark_bar": {"type": "folder", "name": "Bookmarks bar", "children": [
                    {"type": "url", "name": "Invented Docs", "url": "https://docs.example.com/start",
                     "guid": "00000000-0000-4000-8000-000000000001", "id": "5", "date_added": str(webkit(BASE - 86400))},
                    {"type": "folder", "name": "Invented Recipes", "children": [
                        {"type": "url", "name": "Invented Soup", "url": "https://recipes.example.org/soup?token=abc123",
                         "guid": "00000000-0000-4000-8000-000000000002", "id": "7", "date_added": str(webkit(BASE - 3600))},
                        {"type": "url", "name": "Bookmarklet", "url": "javascript:alert(1)",
                         "guid": "00000000-0000-4000-8000-000000000003", "id": "8", "date_added": str(webkit(BASE - 60))},
                    ]},
                ]},
                "other": {"type": "folder", "name": "Other bookmarks", "children": []},
            },
        }))
    parent = folder.parent
    (parent / "Local State").write_text(json.dumps({"profile": {"info_cache": {folder.name: {"name": name}}}}))
    return folder


def default_chrome_visits():
    link, typed, reload_, subframe = 0x30000000, 0x30000001, 0x30000008, 0x30000003
    return [
        (1, "https://www.example.com/", "Invented Home", BASE + 60, typed, 12_500_000, None),
        (2, "https://example.com/article?id=1&token=s3cret", "Invented Article", BASE + 120, link, 30_000_000, 1),
        (3, "https://ads.example.net/frame", "Invented Frame", BASE + 121, subframe, 0, 2),
        (4, "file:///home/invented/private.txt", "private.txt", BASE + 180, typed, 0, None),
        (5, "https://bank.example.org/login", "Invented Bank", BASE + 240, typed, 0, None),
        (6, "http://localhost:3000/", "Dev server", BASE + 300, typed, 0, None),
        (7, "https://t.example.com/r", None, BASE + 360, 0x10000000, 0, None),  # redirect source
        (8, "https://example.com/landing", "Invented Landing", BASE + 361, 0xA0000000, 0, 7),
        (9, "https://example.com/article?id=1&token=s3cret", "Invented Article", BASE + 3 * 86400, reload_, 1_000_000, None),
    ]


def add_chrome_visits(conn, visits, synced=()):
    for ident, url, title, unix, transition, duration, from_visit in visits:
        row = conn.execute("SELECT id FROM urls WHERE url=?", (url,)).fetchone()
        if row:
            url_id = row[0]
        else:
            url_id = conn.execute("INSERT INTO urls(url,title,last_visit_time) VALUES (?,?,?)", (url, title, webkit(unix))).lastrowid
        conn.execute(
            "INSERT INTO visits(id,url,visit_time,from_visit,transition,visit_duration,originator_cache_guid) VALUES (?,?,?,?,?,?,?)",
            (ident, url_id, webkit(unix), from_visit or 0, transition, duration, "remote-guid" if ident in synced else ""),
        )
    conn.commit()


def firefox(folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(folder / "places.sqlite")
    conn.executescript(
        """
        CREATE TABLE moz_places (id INTEGER PRIMARY KEY, url LONGVARCHAR, title LONGVARCHAR, rev_host LONGVARCHAR,
          visit_count INTEGER DEFAULT 0, hidden INTEGER DEFAULT 0 NOT NULL, typed INTEGER DEFAULT 0 NOT NULL,
          frecency INTEGER DEFAULT -1 NOT NULL, last_visit_date INTEGER, guid TEXT, url_hash INTEGER DEFAULT 0 NOT NULL);
        CREATE TABLE moz_historyvisits (id INTEGER PRIMARY KEY, from_visit INTEGER, place_id INTEGER,
          visit_date INTEGER, visit_type INTEGER, session INTEGER, source INTEGER DEFAULT 0 NOT NULL, triggeringPlaceId INTEGER);
        CREATE TABLE moz_bookmarks (id INTEGER PRIMARY KEY, type INTEGER, fk INTEGER DEFAULT NULL, parent INTEGER,
          position INTEGER, title LONGVARCHAR, keyword_id INTEGER, folder_type TEXT, dateAdded INTEGER,
          lastModified INTEGER, guid TEXT, syncStatus INTEGER NOT NULL DEFAULT 0, syncChangeCounter INTEGER NOT NULL DEFAULT 1);
        """
    )
    places = [
        (1, "https://invented.example.org/", "Invented Org"),
        (2, "https://invented.example.org/news", "Invented News"),
        (3, "about:config", None),
        (4, "https://short.example.net/x", None),
        (5, "https://invented.example.org/final", "Invented Final"),
        (6, "https://embed.example.net/widget", "Widget"),
        (7, "https://synced.example.org/", "Invented Synced"),
    ]
    conn.executemany("INSERT INTO moz_places(id,url,title) VALUES (?,?,?)", places)
    us = 1_000_000
    conn.executemany(
        "INSERT INTO moz_historyvisits(id,from_visit,place_id,visit_date,visit_type,source) VALUES (?,?,?,?,?,?)",
        [
            (1, 0, 1, (BASE + 86400) * us, 2, 0),
            (2, 1, 2, (BASE + 86400 + 30) * us, 1, 0),
            (3, 0, 3, (BASE + 86400 + 60) * us, 2, 0),
            (4, 2, 4, (BASE + 86400 + 90) * us, 1, 0),
            (5, 4, 5, (BASE + 86400 + 91) * us, 5, 0),
            (6, 5, 6, (BASE + 86400 + 92) * us, 4, 0),
            (7, 0, 7, (BASE + 86400 + 120) * us, 1, 1),
        ],
    )
    conn.executemany(
        "INSERT INTO moz_bookmarks(id,type,fk,parent,title,dateAdded,lastModified,guid) VALUES (?,?,?,?,?,?,?,?)",
        [
            (1, 2, None, 0, "", BASE * us, BASE * us, "root________"),
            (2, 2, None, 1, "menu", BASE * us, BASE * us, "menu________"),
            (3, 2, None, 1, "toolbar", BASE * us, BASE * us, "toolbar_____"),
            (4, 2, None, 3, "Invented Reading", BASE * us, BASE * us, "folderAAAAAA"),
            (5, 1, 2, 4, "Invented News", (BASE + 10) * us, (BASE + 10) * us, "bookmarkAAAA"),
            (6, 1, 1, 2, "Invented Org Home", (BASE + 20) * us, (BASE + 20) * us, "bookmarkBBBB"),
        ],
    )
    conn.commit()
    conn.close()
    return folder


def safari(folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(folder / "History.db")
    conn.executescript(
        """
        CREATE TABLE history_items (id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL UNIQUE,
          domain_expansion TEXT NULL, visit_count INTEGER NOT NULL, daily_visit_counts BLOB NOT NULL,
          weekly_visit_counts BLOB NULL, autocomplete_triggers BLOB NULL, should_recompute_derived_visit_counts INTEGER NOT NULL,
          visit_count_score INTEGER NOT NULL);
        CREATE TABLE history_visits (id INTEGER PRIMARY KEY AUTOINCREMENT, history_item INTEGER NOT NULL,
          visit_time REAL NOT NULL, title TEXT NULL, load_successful BOOLEAN NOT NULL DEFAULT 1,
          http_non_get BOOLEAN NOT NULL DEFAULT 0, synthesized BOOLEAN NOT NULL DEFAULT 0,
          redirect_source INTEGER NULL UNIQUE, redirect_destination INTEGER NULL UNIQUE, origin INTEGER NOT NULL DEFAULT 0,
          generation INTEGER NOT NULL DEFAULT 0, attributes INTEGER NOT NULL DEFAULT 0, score INTEGER NOT NULL DEFAULT 0);
        """
    )
    conn.executemany(
        "INSERT INTO history_items(id,url,visit_count,daily_visit_counts,should_recompute_derived_visit_counts,visit_count_score) VALUES (?,?,1,x'',0,0)",
        [(1, "https://safari.example.com/"), (2, "https://safari.example.com/page"), (3, "https://icloud.example.com/")],
    )
    apple = BASE - APPLE_OFFSET_S + 2 * 86400
    conn.executemany(
        "INSERT INTO history_visits(id,history_item,visit_time,title,origin) VALUES (?,?,?,?,?)",
        [(1, 1, apple + 0.25, "Invented Safari Home", 0), (2, 2, apple + 30.5, "Invented Safari Page", 0),
         (3, 3, apple + 40.0, "Invented Other Device", 1)],
    )
    conn.commit()
    conn.close()
    return folder


def linux_home(root):
    """A fake Linux home with a Chrome profile and a Firefox profile."""
    root = Path(root)
    chrome(root / ".config" / "google-chrome" / "Default")
    firefox(root / ".mozilla" / "firefox" / "abcd1234.default-release")
    (root / ".config" / "google-chrome" / "System Profile").mkdir()
    return root
