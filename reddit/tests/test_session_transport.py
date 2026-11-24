"""The signed-in browser transport reads account listings (mocked browser)."""

import json
from datetime import datetime, timezone

from throughline_reddit import browser as real_browser
from throughline_reddit import fetch

OWNER = "owner_account"


class FakeChrome:
    closed = False

    def close(self):
        self.closed = True


def child(id36, title, created):
    return {
        "kind": "t3",
        "data": {
            "name": f"t3_{id36}",
            "id": id36,
            "subreddit": "ExampleOne",
            "title": title,
            "author": "example_author",
            "permalink": f"/r/ExampleOne/comments/{id36}/x/",
            "created_utc": created,
        },
    }


class FakeBrowser:
    """Stands in for browser.py: one signed-in page and canned listings."""

    BrowserError = real_browser.BrowserError

    def __init__(self, signed_in=True):
        self.signed_in = signed_in
        self.urls = []
        self.logins = 0
        self.chromes = []
        self.listings = {
            "saved": [child("sv01", "Example saved", 1_790_000_000)],
            "submitted": [child("ps01", "Example own post", 1_790_000_100)],
        }

    def Chrome(self, profile):
        assert profile
        chrome = FakeChrome()
        self.chromes.append(chrome)
        return chrome

    def ensure_page(self, chrome):
        pass

    def whoami(self, chrome):
        return OWNER if self.signed_in else None

    def login(self, chrome, username, password):
        assert username == OWNER and password == "pw"
        self.logins += 1
        self.signed_in = True
        return OWNER

    def get(self, chrome, url):
        self.urls.append(url)
        path = url.split("?", 1)[0]
        prefix = f"/user/{OWNER}/"
        if path.startswith(prefix) and path.endswith(".json"):
            listing = path[len(prefix) : -len(".json")]
            children = self.listings.get(listing, [])
            body = {"kind": "Listing", "data": {"children": children, "after": None}}
            return {"status": 200, "body": json.dumps(body)}
        return {"status": 404, "body": "{}"}


def exporter(fake, environ_extra=None, state=None):
    environ = {
        "REDDIT_USER_AGENT": "test",
        "REDDIT_LISTINGS": "saved,submitted",
        "REDDIT_TRANSPORT": "session",
        "REDDIT_SESSION_PROFILE": "/nonexistent/profile",
        "REDDIT_MIN_INTERVAL": "0",
        **(environ_extra or {}),
    }
    config = fetch.Config.from_env(environ)
    credentials = fetch.Credentials.from_env(environ)
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    ex = fetch.Exporter(config, credentials, state, sleep=lambda s: None, now=lambda: now)
    ex.client._browser_module = fake
    return ex


def test_session_transport_reads_account_listings_without_oauth():
    fake = FakeBrowser()
    ex = exporter(fake)
    records = list(ex.records())
    items = [r for r in records if r["kind"] == "reddit.item"]
    assert [(r["listing"], r["id"]) for r in items] == [
        ("saved", "t3_sv01"),
        ("submitted", "t3_ps01"),
    ]
    assert all(r["source"] == "session" and r["account"] == OWNER for r in items)
    # The account name comes from the session; listings use same-origin .json.
    assert all(u.startswith(f"/user/{OWNER}/") and ".json" in u for u in fake.urls)
    assert records[-1]["account"] == OWNER
    assert fake.chromes and all(c.closed for c in fake.chromes)
    # A second run stops at the known heads and emits nothing new.
    again = exporter(fake, state=ex.state)
    assert [r for r in again.records() if r["kind"] == "reddit.item"] == []


def test_session_transport_signs_in_only_when_needed():
    fake = FakeBrowser(signed_in=False)
    ex = exporter(fake, {"REDDIT_USERNAME": OWNER, "REDDIT_PASSWORD": "pw"})
    list(ex.records())
    assert fake.logins == 1
    fake2 = FakeBrowser(signed_in=False)
    ex2 = exporter(fake2)
    try:
        list(ex2.records())
    except fetch.FetchError as error:
        assert "not signed in" in str(error)
    else:
        raise AssertionError("expected a sign-in error")
    assert fake2.logins == 0


def test_session_transport_refuses_another_signed_in_account():
    fake = FakeBrowser()
    ex = exporter(fake, {"REDDIT_USERNAME": "someone_else"})
    try:
        list(ex.records())
    except fetch.FetchError as error:
        assert "different account" in str(error)
    else:
        raise AssertionError("expected an account mismatch error")


def test_find_chrome_prefers_explicit_then_environment(monkeypatch):
    monkeypatch.setenv("REDDIT_CHROME", "/example/chrome")
    assert real_browser.find_chrome("/explicit/chrome") == "/explicit/chrome"
    assert real_browser.find_chrome() == "/example/chrome"
