"""Offline tests of the stdlib history/Watch Later watcher (invented data only)."""

import gzip
import json
from datetime import UTC, date, datetime, timedelta

import pytest

from throughline_youtube import fetch

T0 = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)  # a Tuesday


def vid(n):
    return f"vid{n:08d}"[:11].ljust(11, "x")


def video(n, progress=None, length="10:00", modern=False):
    """An invented history entry, in the classic or the lockup shape."""
    overlays = []
    if modern:
        overlays = [
            {
                "thumbnailOverlayBadgeViewModel": {
                    "thumbnailBadges": [{"thumbnailBadgeViewModel": {"text": length}}]
                }
            }
        ]
        if progress is not None:
            overlays.append(
                {
                    "thumbnailBottomOverlayViewModel": {
                        "progressBar": {
                            "thumbnailOverlayProgressBarViewModel": {
                                "startPercent": progress
                            }
                        }
                    }
                }
            )
        return {
            "lockupViewModel": {
                "contentId": vid(n),
                "contentType": "LOCKUP_CONTENT_TYPE_VIDEO",
                "contentImage": {"thumbnailViewModel": {"overlays": overlays}},
                "metadata": {
                    "lockupMetadataViewModel": {
                        "title": {"content": f"Invented video {n}"},
                        "metadata": {
                            "contentMetadataViewModel": {
                                "metadataRows": [
                                    {
                                        "metadataParts": [
                                            {"text": {"content": f"Channel {n % 3}"}}
                                        ]
                                    }
                                ]
                            }
                        },
                    }
                },
            }
        }
    if progress is not None:
        overlays.append(
            {
                "thumbnailOverlayResumePlaybackRenderer": {
                    "percentDurationWatched": progress
                }
            }
        )
    return {
        "videoRenderer": {
            "videoId": vid(n),
            "title": {"runs": [{"text": f"Invented video {n}"}]},
            "ownerText": {
                "runs": [
                    {
                        "text": f"Channel {n % 3}",
                        "navigationEndpoint": {
                            "browseEndpoint": {"browseId": f"UCchannel{n % 3:015d}"}
                        },
                    }
                ]
            },
            "lengthText": {"simpleText": length},
            "thumbnailOverlays": overlays,
        }
    }


def page(sections, token=None, continuation=False):
    contents = [
        {
            "itemSectionRenderer": {
                "header": {
                    "itemSectionHeaderRenderer": {"title": {"runs": [{"text": label}]}}
                },
                "contents": items,
            }
        }
        for label, items in sections
    ]
    if token:
        contents.append(
            {
                "continuationItemRenderer": {
                    "continuationEndpoint": {"continuationCommand": {"token": token}}
                }
            }
        )
    if continuation:
        return {
            "onResponseReceivedActions": [
                {"appendContinuationItemsAction": {"continuationItems": contents}}
            ]
        }
    return {
        "contents": {
            "twoColumnBrowseResultsRenderer": {
                "tabs": [
                    {
                        "tabRenderer": {
                            "content": {"sectionListRenderer": {"contents": contents}}
                        }
                    }
                ]
            }
        }
    }


def wl_page(entries):
    return {
        "contents": {
            "playlistVideoListRenderer": {
                "contents": [
                    {
                        "playlistVideoRenderer": {
                            "videoId": vid(n),
                            "setVideoId": f"SET{n}",
                            "index": {"simpleText": str(i + 1)},
                            "title": {"runs": [{"text": f"Saved {n}"}]},
                            "shortBylineText": {"runs": [{"text": "Somebody"}]},
                            "lengthSeconds": "600",
                            "thumbnailOverlays": [],
                        }
                    }
                    for i, n in enumerate(entries)
                ]
            }
        }
    }


def subs_page(entries):
    return {
        "contents": {
            "sectionListRenderer": {
                "contents": [
                    {
                        "channelRenderer": {
                            "channelId": f"UCsub{n:019d}",
                            "title": {"simpleText": f"Subscribed {n}"},
                        }
                    }
                    for n in entries
                ]
            }
        }
    }


class FakeClient:
    def __init__(self, history, wl=(), subs=()):
        self.pages = history if isinstance(history, list) else [history]
        self.wl = list(wl)
        self.subs = list(subs)
        self.requests = 0

    def browse(self, browse_id=None, continuation=None):
        self.requests += 1
        if browse_id == "VLWL":
            return wl_page(self.wl)
        if browse_id == "FEchannels":
            return subs_page(self.subs)
        if continuation:
            return self.pages[int(continuation.removeprefix("tok"))]
        return self.pages[0]


def run(state, history, wl=(), subs=(), now=T0, **kw):
    return fetch.poll(FakeClient(history, wl, subs), state, now=now, **kw)


def kinds(records, kind):
    return [r for r in records if r["kind"] == kind]


def test_day_headings():
    today = date(2026, 3, 10)  # Tuesday
    assert fetch.day_of("Today", today) == today
    assert fetch.day_of("Yesterday", today) == date(2026, 3, 9)
    assert fetch.day_of("Sunday", today) == date(2026, 3, 8)
    assert fetch.day_of("Tuesday", today) == date(2026, 3, 3)
    assert fetch.day_of("Feb 3", today) == date(2026, 2, 3)
    assert fetch.day_of("Dec 24", today) == date(2025, 12, 24)
    assert fetch.day_of("Nov 3, 2024", today) == date(2024, 11, 3)
    assert fetch.day_of("3 Nov 2024", today) == date(2024, 11, 3)
    assert fetch.day_of("Shorts", today) is None


def test_parse_both_shapes_shorts_and_continuation():
    first = page(
        [
            ("Today", [video(1, 40), video(2, modern=True, progress=75)]),
            (
                "Yesterday",
                [
                    {
                        "reelShelfRenderer": {
                            "items": [
                                {
                                    "reelItemRenderer": {
                                        "videoId": vid(3),
                                        "headline": {"simpleText": "S"},
                                    }
                                }
                            ]
                        }
                    }
                ],
            ),
        ],
        token="tok1",
    )
    second = page([("Monday", [video(4, 100)])], continuation=True)
    items, complete, pages = fetch.feed(FakeClient([first, second]), "FEhistory", 0)
    assert complete and pages == 2
    assert [i["video_id"] for i in items] == [vid(1), vid(2), vid(3), vid(4)]
    assert items[0]["progress"] == 40 and items[0]["length_s"] == 600
    assert items[0]["channel_id"].startswith("UC")
    assert items[1]["progress"] == 75 and items[1]["channel"] == "Channel 2"
    assert items[2]["is_short"] is True
    assert items[3]["section"] == "Monday"
    # Page limit stops early and reports an incomplete feed.
    items, complete, _ = fetch.feed(FakeClient([first, second]), "FEhistory", 1)
    assert not complete and len(items) == 3


def test_unrecognized_response_fails():
    with pytest.raises(fetch.FetchError):
        fetch.feed(FakeClient([{"responseContext": {}}]), "FEhistory", 1)


def test_baseline_then_sessions_in_parts():
    h1 = page([("Today", [video(1, 30), video(2, 100)]), ("Yesterday", [video(3, 50)])])
    out, state = run({}, h1, wl=[7, 8])
    assert not kinds(out, "session")
    assert {r["id"] for r in kinds(out, "history_item")} == {
        f"{vid(1)}:2026-03-10",
        f"{vid(2)}:2026-03-10",
        f"{vid(3)}:2026-03-09",
    }
    assert kinds(out, "poll")[0]["raw"]["baseline"] is True

    # 20 minutes later: video 1 continued from 30% to 55%, a new video 4 on top.
    t1 = T0 + timedelta(minutes=20)
    h2 = page(
        [
            ("Today", [video(4, 20), video(1, 55), video(2, 100)]),
            ("Yesterday", [video(3, 50)]),
        ]
    )
    out, state = run(state, h2, wl=[7, 8], now=t1)
    sessions = {r["raw"]["video_id"]: r["raw"] for r in kinds(out, "session")}
    assert set(sessions) == {vid(4), vid(1)}
    assert sessions[vid(1)]["session_kind"] == "resume"
    assert (
        sessions[vid(1)]["progress_before"] == 30
        and sessions[vid(1)]["progress_after"] == 55
    )
    assert (
        sessions[vid(1)]["watched_fraction"] == 0.25
        and sessions[vid(1)]["watched_s"] == 150
    )
    assert (
        sessions[vid(4)]["session_kind"] == "new"
        and sessions[vid(4)]["watched_s"] == 120
    )
    # Back to back from the poll: video 4 (top) ended last.
    assert sessions[vid(4)]["end_est"] == fetch.iso(t1)
    assert sessions[vid(1)]["end_est"] == sessions[vid(4)]["start_est"]
    assert sessions[vid(1)]["window_start"] == fetch.iso(T0)

    # Another part of video 1 later: a second, separate session.
    t2 = t1 + timedelta(minutes=30)
    h3 = page(
        [
            ("Today", [video(1, 90), video(4, 20), video(2, 100)]),
            ("Yesterday", [video(3, 50)]),
        ]
    )
    out, state = run(state, h3, wl=[7, 8], now=t2)
    (s,) = kinds(out, "session")
    assert s["raw"]["video_id"] == vid(1) and s["raw"]["session_number"] == 2
    assert s["raw"]["progress_before"] == 55 and s["raw"]["progress_after"] == 90
    assert s["id"] == f"{vid(1)}:{fetch.iso(t2)}"

    # Nothing changed: no sessions; history items keep their first progress.
    out, state = run(state, h3, wl=[7, 8], now=t2 + timedelta(minutes=15))
    assert not kinds(out, "session")
    item = next(r for r in kinds(out, "history_item") if r["id"].startswith(vid(1)))
    assert item["raw"]["progress"] == 30 and item["raw"]["first_seen"] == fetch.iso(T0)


def test_rewatch_moves_to_top_and_new_day():
    h1 = page([("Today", [video(1, 100), video(2, 100)])])
    _, state = run({}, h1)
    t1 = T0 + timedelta(days=1)
    h2 = page([("Today", [video(2, 35)]), ("Yesterday", [video(1, 100)])])
    out, state = run(state, h2, now=t1)
    (s,) = kinds(out, "session")
    assert s["raw"]["video_id"] == vid(2) and s["raw"]["session_kind"] == "rewatch"
    assert s["raw"]["watched_fraction"] == 0.35
    ids = {r["id"] for r in kinds(out, "history_item")}
    assert f"{vid(2)}:2026-03-11" in ids


def test_deeper_and_old_items_are_not_sessions():
    _, state = run({}, page([("Today", [video(1, 10)])]))
    t1 = T0 + timedelta(minutes=10)
    # Video 5 appears below everything previously seen: just deeper history.
    # Video 6 sits above video 1 but under a heading long before the window.
    h2 = page(
        [
            ("Feb 1", [video(6, 10)]),
            ("Today", [video(1, 10)]),
            ("Jan 2", [video(5, 10)]),
        ]
    )
    out, _ = run(state, h2, now=t1)
    assert not kinds(out, "session")


def test_watch_later_added_and_removed():
    _, state = run({}, page([("Today", [video(1)])]), wl=[7, 8])
    t1 = T0 + timedelta(hours=1)
    out, state = run(state, page([("Today", [video(1)])]), wl=[8, 9], now=t1)
    wl = {r["id"]: r["raw"] for r in kinds(out, "watch_later")}
    assert wl["SET9"]["first_seen"] == fetch.iso(t1) and not wl["SET9"]["removed"]
    assert wl["SET8"]["first_seen"] == fetch.iso(T0)
    assert wl["SET7"]["removed"] and wl["SET7"]["removed_seen"] == fetch.iso(t1)
    # Removal time stays fixed on later polls.
    out, _ = run(
        state, page([("Today", [video(1)])]), wl=[8, 9], now=t1 + timedelta(hours=1)
    )
    wl = {r["id"]: r["raw"] for r in kinds(out, "watch_later")}
    assert wl["SET7"]["removed_seen"] == fetch.iso(t1)


def test_export_roundtrip_and_footer(tmp_path):
    out, _ = run({}, page([("Today", [video(1, 5)])]), wl=[7])
    path = str(tmp_path / "w.jsonl.gz")
    counts = fetch.write_export(path, out, {"polled_at": fetch.iso(T0)})
    assert counts["history_item"] == 1 and counts["watch_later"] == 1
    header, records = fetch.read_export(path)
    assert header["format"] == fetch.FORMAT and records == json.loads(json.dumps(out))
    broken = tmp_path / "b.jsonl.gz"
    with gzip.open(broken, "wt", encoding="utf-8") as h:
        h.write(json.dumps(header) + "\n")
    with pytest.raises(ValueError):
        fetch.read_export(broken)


def test_cookies_and_authorization(tmp_path):
    jar = tmp_path / "cookies.txt"
    jar.write_text(
        "# Netscape HTTP Cookie File\n"
        ".youtube.com\tTRUE\t/\tTRUE\t2000000000\tSAPISID\tinvented\n"
        ".youtube.com\tTRUE\t/\tTRUE\t2000000000\t__Secure-3PAPISID\tinvented3\n"
        ".example.com\tTRUE\t/\tTRUE\t2000000000\tOTHER\tx\n",
        encoding="utf-8",
    )
    cookies = fetch.load_cookies({"YOUTUBE_COOKIES_FILE": str(jar)})
    assert set(cookies) == {"SAPISID", "__Secure-3PAPISID"}
    header = fetch.authorization(cookies, now=1700000000)
    assert header.startswith("SAPISIDHASH 1700000000_") and "SAPISID3PHASH" in header
    assert "invented" not in header
    with pytest.raises(fetch.FetchError):
        fetch.load_cookies({"YOUTUBE_COOKIE_HEADER": "PREF=1"})
    with pytest.raises(fetch.FetchError):
        fetch.load_cookies({})


# ---------------------------------------------------------------- plugin mapping


@pytest.fixture
def plugin_env(monkeypatch, tmp_path):
    from throughline_youtube import YouTube

    for d in [*YouTube.manifest.settings.values(), *YouTube.manifest.secrets.values()]:
        monkeypatch.delenv(d.env, raising=False)
    exports = tmp_path / "exports"
    exports.mkdir()
    monkeypatch.setenv("YOUTUBE_WATCHER", str(exports))
    return exports


def collect(cursor):
    from throughline_sdk import Checkpoint

    from throughline_youtube import YouTube

    items = list(YouTube().records(cursor, "sync"))
    messages = [i for i in items if isinstance(i, dict)]
    return messages, [i.cursor for i in items if isinstance(i, Checkpoint)][-1]


def test_plugin_maps_watcher_exports_idempotently(plugin_env):
    out1, state = run(
        {},
        page([("Today", [video(1, 30)]), ("Yesterday", [video(3, 50)])]),
        wl=[7, 8],
        subs=[1, 2],
        tz="America/Winnipeg",
    )
    fetch.write_export(
        str(plugin_env / "a-1.jsonl.gz"), out1, {"tz": "America/Winnipeg"}
    )
    t1 = T0 + timedelta(minutes=20)
    out2, state = run(
        state,
        page([("Today", [video(1, 55)]), ("Yesterday", [video(3, 50)])]),
        wl=[8],
        subs=[2, 3],
        now=t1,
        tz="America/Winnipeg",
    )
    fetch.write_export(
        str(plugin_env / "a-2.jsonl.gz"), out2, {"tz": "America/Winnipeg"}
    )

    messages, cursor = collect({})
    watch = [m for m in messages if m["channel"] == "youtube.history"]
    days = [m for m in watch if m["fields"]["record_type"] == "day"]
    (session,) = [m for m in watch if m["fields"]["record_type"] == "session"]
    assert len(days) == 2
    day = next(m for m in days if m["fields"]["video_id"] == vid(1))
    assert day["happened_at"] == "2026-03-10T00:00:00-05:00"
    assert day["fields"]["time_precision"] == "day" and day["fields"]["progress"] == 30
    assert session["fields"]["time_precision"] == "estimated"
    assert (
        session["fields"]["progress_before"] == 30
        and session["fields"]["progress_after"] == 55
    )
    assert session["ended_at"] == fetch.iso(t1)
    assert session["fields"]["source"] == "watcher"
    wl = [m for m in messages if m["channel"] == "youtube.watch_later"]
    removed = [m for m in wl if m["fields"]["removed"]]
    assert len(removed) == 1 and removed[0]["fields"]["video_id"] == vid(7)
    assert all(m["fields"]["time_precision"] == "first_seen" for m in wl)
    subs = [m for m in messages if m["channel"] == "youtube.subscriptions"]
    assert len(subs) == 3
    removed_subs = [m for m in subs if m["fields"]["removed"]]
    assert (
        len(removed_subs) == 1
        and removed_subs[0]["fields"]["youtube_channel_id"] == f"UCsub{1:019d}"
    )

    again, _ = collect(cursor)
    assert again == []


def test_subscriptions_added_and_removed():
    _, state = run({}, page([("Today", [video(1)])]), subs=[1, 2])
    t1 = T0 + timedelta(hours=1)
    out, state = run(state, page([("Today", [video(1)])]), subs=[2, 3], now=t1)
    subs = {r["id"]: r["raw"] for r in kinds(out, "subscription")}
    cid1, cid2, cid3 = f"UCsub{1:019d}", f"UCsub{2:019d}", f"UCsub{3:019d}"
    assert subs[cid3]["first_seen"] == fetch.iso(t1) and not subs[cid3]["removed"]
    assert subs[cid2]["first_seen"] == fetch.iso(T0)
    assert subs[cid1]["removed"] and subs[cid1]["removed_seen"] == fetch.iso(t1)


def test_chromium_cookies_read_in_place(tmp_path):
    import ctypes
    import ctypes.util
    import hashlib
    import sqlite3

    def encrypt_v10(host, plaintext):
        key = hashlib.pbkdf2_hmac("sha1", b"peanuts", b"saltysalt", 1, 16)
        data = hashlib.sha256(host.encode()).digest() + plaintext.encode()
        lib = ctypes.CDLL(ctypes.util.find_library("crypto") or "libcrypto.so.3")
        lib.EVP_CIPHER_CTX_new.restype = ctypes.c_void_p
        lib.EVP_aes_128_cbc.restype = ctypes.c_void_p
        lib.EVP_EncryptInit_ex.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
        ]
        lib.EVP_EncryptUpdate.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_int),
            ctypes.c_char_p,
            ctypes.c_int,
        ]
        lib.EVP_EncryptFinal_ex.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_int),
        ]
        lib.EVP_CIPHER_CTX_new.restype = ctypes.c_void_p
        lib.EVP_aes_128_cbc.restype = ctypes.c_void_p
        lib.EVP_CIPHER_CTX_free.argtypes = [ctypes.c_void_p]
        ctx = lib.EVP_CIPHER_CTX_new()
        try:
            assert lib.EVP_EncryptInit_ex(ctx, lib.EVP_aes_128_cbc(), None, key, b" " * 16) == 1
            out = ctypes.create_string_buffer(len(data) + 32)
            n1, n2 = ctypes.c_int(0), ctypes.c_int(0)
            assert lib.EVP_EncryptUpdate(ctx, out, ctypes.byref(n1), data, len(data)) == 1
            tail = ctypes.create_string_buffer(32)
            assert lib.EVP_EncryptFinal_ex(ctx, tail, ctypes.byref(n2)) == 1
            return b"v10" + out.raw[: n1.value] + tail.raw[: n2.value]
        finally:
            lib.EVP_CIPHER_CTX_free(ctx)

    profile = tmp_path / "chromium"
    profile.mkdir()
    con = sqlite3.connect(profile / "Cookies")
    con.execute(
        "create table cookies (host_key text, name text, encrypted_value blob, value text, expires_utc int)"
    )
    con.executemany(
        "insert into cookies values (?, ?, ?, ?, ?)",
        [
            (".youtube.com", "SAPISID", encrypt_v10(".youtube.com", "invented"), "", 1),
            (".example.com", "X", b"", "y", 1),
        ],
    )
    con.commit()
    con.close()
    before = sorted(p.name for p in profile.iterdir())
    assert fetch.load_cookies({"YOUTUBE_COOKIES_CHROMIUM": str(profile)}) == {
        "SAPISID": "invented"
    }
    assert sorted(p.name for p in profile.iterdir()) == before


def test_firefox_cookies_read_in_place(tmp_path):
    import sqlite3

    profile = tmp_path / "profile"
    profile.mkdir()
    con = sqlite3.connect(profile / "cookies.sqlite")
    con.execute(
        "create table moz_cookies (name text, value text, host text, expiry int)"
    )
    con.executemany(
        "insert into moz_cookies values (?, ?, ?, ?)",
        [("SAPISID", "invented", ".youtube.com", 1), ("X", "y", ".example.com", 1)],
    )
    con.commit()
    con.close()
    before = sorted(p.name for p in profile.iterdir())
    assert fetch.load_cookies({"YOUTUBE_COOKIES_FIREFOX": str(profile)}) == {
        "SAPISID": "invented"
    }
    assert sorted(p.name for p in profile.iterdir()) == before
