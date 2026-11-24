import json
import sqlite3
import sys
from pathlib import Path

import pytest
from throughline_sdk import Checkpoint, Record
from throughline_sdk.contract import check_plugin

sys.path.insert(0, str(Path(__file__).parent))

from browser_fake import BASE, add_chrome_visits, chrome, firefox, linux_home, safari  # noqa: E402

from throughline_browser_history import BOOKMARKS, VISITS, BrowserHistory  # noqa: E402
from throughline_browser_history.privacy import Privacy  # noqa: E402
from throughline_browser_history.profiles import discover  # noqa: E402

ENV_KEYS = ("BROWSER_HISTORY_PROFILES", "BROWSER_HISTORY_HOME", "BROWSER_HISTORY_PLATFORM", "BROWSER_HISTORY_BROWSERS")


@pytest.fixture
def env(monkeypatch, tmp_path):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BROWSER_HISTORY_DEVICE", "invented-laptop")
    monkeypatch.setenv("BROWSER_HISTORY_TZ", "UTC")
    monkeypatch.setenv("BROWSER_HISTORY_EXCLUDE_DOMAINS", "bank.example.org")
    monkeypatch.setenv("BROWSER_HISTORY_CHECKPOINT_EVERY", "2")
    return monkeypatch


@pytest.fixture
def home(env, tmp_path):
    root = linux_home(tmp_path / "home")
    env.setenv("BROWSER_HISTORY_HOME", str(root))
    env.setenv("BROWSER_HISTORY_PLATFORM", "linux")
    return root


def run(cursor=None, now=None):
    plugin = BrowserHistory()
    plugin.now = now
    items = list(plugin.records(cursor or {}, "sync"))
    rows = [i for i in items if isinstance(i, dict)]
    records = [i.data for i in items if isinstance(i, Record)]
    checkpoints = [i.cursor for i in items if isinstance(i, Checkpoint)]
    return rows, records, checkpoints


def by_channel(rows, channel):
    return {r["external_id"]: r for r in rows if r["channel"] == channel}


def test_discovery_linux_and_macos(tmp_path):
    root = linux_home(tmp_path / "home")
    keys = [p.key for p in discover(root, "linux", {})]
    assert keys == ["chrome/Default", "firefox/abcd1234.default-release"]
    assert discover(root, "linux", {})[0].name == "Invented Person"
    mac = tmp_path / "mac"
    chrome(mac / "Library" / "Application Support" / "BraveSoftware" / "Brave-Browser" / "Profile 1")
    safari(mac / "Library" / "Safari")
    assert [p.key for p in discover(mac, "darwin", {})] == ["brave/Profile 1", "safari/default"]
    win = tmp_path / "win"
    chrome(win / "Local" / "Microsoft" / "Edge" / "User Data" / "Default")
    firefox(win / "Roaming" / "Mozilla" / "Firefox" / "Profiles" / "x.default")
    found = discover(win, "windows", {"LOCALAPPDATA": str(win / "Local"), "APPDATA": str(win / "Roaming")})
    assert [p.key for p in found] == ["edge/Default", "firefox/x.default"]


def test_visits_mapping_and_filters(home):
    rows, records, checkpoints = run()
    visits = by_channel(rows, VISITS)
    chrome_ids = sorted(v["payload"]["visit"]["id"] for v in visits.values() if v["fields"]["browser"] == "chrome")
    firefox_ids = sorted(v["payload"]["visit"]["id"] for v in visits.values() if v["fields"]["browser"] == "firefox")
    assert chrome_ids == [1, 2, 8, 9]  # subframe, file:, excluded bank, localhost, redirect source dropped
    assert firefox_ids == [1, 2, 5]  # about:, redirect source, embed, synced dropped
    article = next(v for v in visits.values() if v["payload"]["visit"]["id"] == 2 and v["fields"]["browser"] == "chrome")
    assert article["fields"]["url"] == "https://example.com/article?id=1&token=REDACTED"
    assert "s3cret" not in json.dumps(rows)
    assert article["content"] == "Invented Article\nhttps://example.com/article?id=1&token=REDACTED"
    assert article["fields"]["domain"] == "example.com"
    assert article["fields"]["duration_s"] == 30.0
    assert article["fields"]["referrer_url"] == "https://www.example.com/"
    assert article["fields"]["from_visit"].startswith("invented-laptop:chrome/Default:1:")
    assert article["happened_at"] == "2025-01-01T00:02:00+00:00"
    assert article["thread"] == {"external_id": "invented-laptop:chrome/Default:2025-01-01",
                                 "title": "Chrome Invented Person · 2025-01-01"}
    home_visit = next(v for v in visits.values() if v["payload"]["visit"]["id"] == 1 and v["fields"]["browser"] == "chrome")
    assert home_visit["fields"]["typed"] is True and home_visit["fields"]["domain"] == "example.com"
    landing = next(v for v in visits.values() if v["payload"]["visit"]["id"] == 8)
    assert landing["fields"]["redirect"] is True
    ff_final = next(v for v in visits.values() if v["fields"]["browser"] == "firefox" and v["payload"]["visit"]["id"] == 5)
    assert ff_final["fields"]["transition"] == "redirect_permanent"
    assert ff_final["fields"]["referrer_url"] == "https://short.example.net/x"

    marks = by_channel(rows, BOOKMARKS)
    assert set(marks) == {
        "chrome:00000000-0000-4000-8000-000000000001",
        "chrome:00000000-0000-4000-8000-000000000002",
        "firefox:bookmarkAAAA",
        "firefox:bookmarkBBBB",
    }
    soup = marks["chrome:00000000-0000-4000-8000-000000000002"]
    assert soup["fields"]["folder"] == "Bookmarks bar/Invented Recipes"
    assert soup["fields"]["url"] == "https://recipes.example.org/soup?token=REDACTED"
    assert marks["firefox:bookmarkAAAA"]["fields"]["folder"] == "Bookmarks Toolbar/Invented Reading"
    assert len(checkpoints) >= 4
    assert all(r["status"] == "complete" for r in records if "status" in r)


def test_incremental_cleared_history_and_gap(home):
    _, _, checkpoints = run()
    cursor = checkpoints[-1]
    assert run(cursor)[0] == []
    db = home / ".config" / "google-chrome" / "Default" / "History"
    conn = sqlite3.connect(db)
    add_chrome_visits(conn, [(10, "https://example.com/new", "Invented New", BASE + 4 * 86400, 0x30000000, 0, None)])
    add_chrome_visits(conn, [(11, "https://example.com/other", "Invented Synced", BASE + 4 * 86400 + 5, 0x30000000, 0, None)],
                      synced=(11,))
    conn.close()
    rows, _, checkpoints = run(cursor)
    assert [r["payload"]["visit"]["id"] for r in rows] == [10]
    cursor = checkpoints[-1]
    # The owner clears recent history; SQLite reuses the freed ids.
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM visits WHERE id >= 8")
    conn.commit()
    add_chrome_visits(conn, [(8, "https://example.com/after-clear", "Invented After", BASE + 5 * 86400, 0x30000000, 0, None)])
    conn.close()
    rows, _, checkpoints = run(cursor)
    assert [r["fields"]["url"] for r in rows] == ["https://example.com/after-clear"]
    cursor = checkpoints[-1]
    # Expiry: everything older than our high-water mark disappears.
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM visits")
    add_chrome_visits(conn, [(20, "https://example.com/much-later", "Invented Later", BASE + 200 * 86400, 0x30000000, 0, None)])
    conn.close()
    rows, records, _ = run(cursor)
    assert [r["payload"]["visit"]["id"] for r in rows] == [20]
    gap = next(r for r in records if r.get("scope_key") == "chrome/Default" and r.get("channel") == VISITS)
    assert gap["status"] == "partial" and gap["reasons"] == ["source_cap"]


def test_bookmark_removed(home):
    _, _, checkpoints = run()
    path = home / ".config" / "google-chrome" / "Default" / "Bookmarks"
    data = json.loads(path.read_text())
    data["roots"]["bookmark_bar"]["children"] = data["roots"]["bookmark_bar"]["children"][1:]
    path.write_text(json.dumps(data))
    rows, _, _ = run(checkpoints[-1], now="2025-02-01T00:00:00+00:00")
    removed = by_channel(rows, BOOKMARKS)["chrome:00000000-0000-4000-8000-000000000001"]
    assert removed["fields"]["removed"] is True and removed["fields"]["title"] == "Invented Docs"
    assert removed["happened_at"] == "2025-02-01T00:00:00+00:00"


def test_safari_explicit_profile(env, tmp_path):
    folder = safari(tmp_path / "Safari")
    env.setenv("BROWSER_HISTORY_PROFILES", f"safari:{folder}")
    rows, _, _ = run()
    assert [r["fields"]["title"] for r in rows] == ["Invented Safari Home", "Invented Safari Page"]
    assert rows[0]["happened_at"] == "2025-01-03T00:00:00.250000+00:00"


def test_no_profiles_raises(env, tmp_path):
    env.setenv("BROWSER_HISTORY_HOME", str(tmp_path / "empty"))
    with pytest.raises(RuntimeError):
        list(BrowserHistory().records({}, "sync"))


def test_privacy_rules():
    privacy = Privacy(exclude=["example.net", "*.corp.example.com"])
    assert not privacy.allowed("https://sub.example.net/a")
    assert not privacy.allowed("https://x.corp.example.com/")
    assert privacy.allowed("https://notexample.net/")
    assert not privacy.allowed("chrome://settings")
    assert privacy.clean("https://user:pw@example.com/cb#access_token=abc&x=1") == "https://example.com/cb#access_token=REDACTED&x=1"
    assert Privacy(strip_query=True).clean("https://example.com/a?b=c#d") == "https://example.com/a"


def test_contract_kit(tmp_path):
    chrome_dir = chrome(tmp_path / "chrome" / "Default")
    firefox_dir = firefox(tmp_path / "firefox" / "abcd.default")
    safari_dir = safari(tmp_path / "Safari")
    result = check_plugin(
        "throughline_browser_history:BrowserHistory",
        {
            "BROWSER_HISTORY_PROFILES": f"chrome:{chrome_dir},firefox:{firefox_dir},safari:{safari_dir}",
            "BROWSER_HISTORY_DEVICE": "invented-laptop",
            "BROWSER_HISTORY_TZ": "UTC",
            "BROWSER_HISTORY_EXCLUDE_DOMAINS": "bank.example.org",
            "BROWSER_HISTORY_CHECKPOINT_EVERY": "2",
        },
    )
    assert result["messages"] == 4 + 3 + 2 + 4
