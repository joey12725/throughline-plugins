"""fetch.py: stdlib-only fetcher, rotation, limits, backoff, cookies, CLI."""

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from throughline_instagram_feed import fetch

FETCH_PY = Path(fetch.__file__)
T0 = datetime(2021, 6, 1, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, at=T0):
        self.at = at

    def __call__(self):
        return self.at


def make(server, accounts, state=None, clock=None, **options):
    opts = {
        "base_url": server.url,
        "delay_s": "0",
        **{k: str(v) for k, v in options.items()},
    }
    sleeps = []
    fetcher = fetch.Fetcher(
        [fetch._account(a) for a in accounts],
        {} if state is None else state,
        opts,
        clock=clock or Clock(),
        sleep=sleeps.append,
    )
    return fetcher, sleeps


def drain(fetcher, **kw):
    return [r for _, records in fetcher.run(**kw) for r in records]


def test_ig_1_accounts_parsing_formats(tmp_path):
    text = tmp_path / "accounts.txt"
    text.write_text(
        "# comment\n@Fake_Venue Invented Venue\nfake_records\n\n", encoding="utf-8"
    )
    assert fetch.parse_accounts(path=str(text)) == [
        {"handle": "fake_venue", "label": "Invented Venue"},
        {"handle": "fake_records"},
    ]
    js = tmp_path / "accounts.json"
    js.write_text(
        json.dumps({"accounts": ["a.b", {"handle": "c_d", "category": "art"}]})
    )
    assert fetch.parse_accounts("a.b, e", str(js)) == [
        {"handle": "a.b"},
        {"handle": "e"},
        {"handle": "c_d", "category": "art"},
    ]
    with pytest.raises(ValueError):
        fetch.parse_accounts("bad/handle")


def test_ig_2_rotation_batches_least_recent_first(fake_instagram, synth):
    accounts = synth.accounts
    clock = Clock()
    state = {}
    seen = []
    for _ in range(3):
        fetcher, _ = make(fake_instagram, accounts, state, clock, batch=2, prime=0)
        seen.append([h for h, _ in fetcher.run()])
        clock.at += timedelta(hours=2)
    assert seen == [
        ["fake_venue", "fake_records"],
        ["fake_gallery", "fake_venue"],
        ["fake_records", "fake_venue"],  # Tie at the same time: config order.
    ]
    assert state["accounts"]["fake_venue"]["user_id"] == "71001"
    assert state["accounts"]["fake_venue"]["status"] == "ok"
    assert state["requests"] == {"day": "2021-06-01", "count": 6}


def test_ig_2_records_shape_and_priming(fake_instagram, synth):
    fetcher, _ = make(fake_instagram, synth.accounts[:1], posts=3)
    records = drain(fetcher)
    kinds = [r["kind"] for r in records]
    assert kinds == ["profile", "post", "post", "post"]
    post = records[1]
    assert post["id"] == "FAKEVEN01" and post["account"] == "fake_venue"
    assert post["account_config"] == {
        "label": "Invented Venue",
        "category": "venues/music",
    }
    assert post["raw"]["shortcode"] == "FAKEVEN01"
    assert "edge_owner_to_timeline_media" not in records[0]["raw"]
    # One anonymous priming request, then the profile with the CSRF cookie.
    assert [e["path"] for e in fake_instagram.log] == [
        "/",
        "/api/v1/users/web_profile_info/",
    ]
    headers = fake_instagram.log[1]["headers"]
    assert headers["x-ig-app-id"] == fetch.DEFAULT_APP_ID
    assert headers["x-csrftoken"] == "fakecsrf123"
    assert fetcher.summary["requests"] == 2


def test_ig_3_per_day_limit_across_runs(fake_instagram, synth):
    clock = Clock()
    state = {}
    fetcher, _ = make(fake_instagram, synth.accounts, state, clock, per_day=2, prime=0)
    assert [h for h, _ in fetcher.run()] == ["fake_venue", "fake_records"]
    assert fetcher.summary["stopped"] == "per_day_limit"
    fetcher, _ = make(fake_instagram, synth.accounts, state, clock, per_day=2, prime=0)
    assert list(fetcher.run()) == [] and fetcher.summary["stopped"] == "per_day_limit"
    assert fake_instagram.profile_calls() == ["fake_venue", "fake_records"]
    clock.at += timedelta(days=1)
    fetcher, _ = make(fake_instagram, synth.accounts, state, clock, per_day=2, prime=0)
    assert [h for h, _ in fetcher.run()] == ["fake_gallery", "fake_venue"]


def test_ig_3_429_cools_down_with_retry_after(fake_instagram, synth):
    fake_instagram.script["fake_records"] = [
        (429, {"status": "fail"}, {"Retry-After": "7200"})
    ]
    clock = Clock()
    state = {}
    fetcher, _ = make(
        fake_instagram, synth.accounts, state, clock, prime=0, cooldown_s=900
    )
    assert [h for h, _ in fetcher.run()] == ["fake_venue"]
    assert fetcher.summary["stopped"] == "blocked"
    assert state["strikes"] == 1
    assert state["backoff_until"] == "2021-06-01T14:00:00Z"  # Retry-After beats 900 s.
    calls = len(fake_instagram.log)
    clock.at += timedelta(hours=1)
    fetcher, _ = make(fake_instagram, synth.accounts, state, clock, prime=0)
    assert list(fetcher.run()) == [] and fetcher.summary["stopped"] == "cooling_down"
    assert len(fake_instagram.log) == calls  # No request at all while cooling down.
    # Still blocked after the window: the next cool-down doubles.
    clock.at += timedelta(hours=2)
    fake_instagram.script["fake_records"] = [(401, {"message": "Please wait"}, {})]
    fetcher, _ = make(
        fake_instagram, synth.accounts, state, clock, prime=0, cooldown_s=900
    )
    assert list(fetcher.run()) == []  # fake_records (never fetched) goes first.
    assert state["strikes"] == 2
    assert state["backoff_until"] == fetch.iso(clock.at + timedelta(seconds=1800))
    # Recovery resets the strikes.
    clock.at += timedelta(hours=1)
    fake_instagram.script.clear()
    fetcher, _ = make(fake_instagram, synth.accounts, state, clock, prime=0)
    assert [h for h, _ in fetcher.run()] == [
        "fake_records",
        "fake_gallery",
        "fake_venue",
    ]
    assert state["strikes"] == 0 and state["backoff_until"] is None


@pytest.mark.parametrize(
    "answer",
    [
        (
            200,
            {
                "status": "fail",
                "message": "Please wait a few minutes before you try again.",
            },
            {},
        ),
        (200, b"<html>login</html>", {}),
    ],
)
def test_ig_3_soft_blocks_stop_the_run(fake_instagram, synth, answer):
    fake_instagram.script["fake_venue"] = [answer]
    fetcher, _ = make(fake_instagram, synth.accounts, prime=0)
    assert list(fetcher.run()) == []
    assert fetcher.summary["stopped"] == "blocked"
    assert fake_instagram.profile_calls() == ["fake_venue"]


def test_ig_3_5xx_retries_with_backoff_then_moves_on(fake_instagram, synth):
    venue = fake_instagram.profiles["fake_venue"]
    fake_instagram.script["fake_venue"] = [
        (503, {}, {}),
        (502, {}, {}),
        (200, venue, {}),
    ]
    fake_instagram.script["fake_records"] = [(500, {}, {})]
    state = {}
    fetcher, sleeps = make(fake_instagram, synth.accounts, state, prime=0, retries=3)
    out = {h: records for h, records in fetcher.run()}
    assert [r["kind"] for r in out["fake_venue"]][:2] == ["profile", "post"]
    assert out["fake_records"][0]["kind"] == "status"
    assert out["fake_records"][0]["status"] == "error"
    assert state["accounts"]["fake_records"]["error"] == "HTTP 500"
    assert sleeps == [2.0, 4.0, 2.0, 4.0]
    assert fake_instagram.profile_calls().count("fake_records") == 3
    assert "fake_gallery" in out


def test_ig_3_deadline_stops_starting_accounts(fake_instagram, synth):
    ticks = iter([0, 1, 100, 200])
    fetcher = fetch.Fetcher(
        synth.accounts,
        {},
        {
            "base_url": fake_instagram.url,
            "delay_s": "0",
            "prime": "0",
            "deadline_s": "50",
        },
        clock=Clock(),
        monotonic=lambda: next(ticks),
        sleep=lambda s: None,
    )
    assert [h for h, _ in fetcher.run()] == ["fake_venue"]
    assert fetcher.summary["stopped"] == "deadline"


def test_ig_4_not_found_and_private(fake_instagram, synth):
    fake_instagram.profiles["fake_private"] = synth.profile(
        "fake_private", "71009", [], private=True
    )
    state = {}
    fetcher, _ = make(
        fake_instagram,
        [{"handle": "fake_missing"}, {"handle": "fake_private"}],
        state,
        prime=0,
    )
    out = {h: records for h, records in fetcher.run()}
    assert out["fake_missing"][0]["kind"] == "status"
    assert out["fake_missing"][0]["status"] == "not_found"
    assert [r["kind"] for r in out["fake_private"]] == ["profile"]
    assert state["accounts"]["fake_private"]["status"] == "private"


def test_ig_4b_html_404_for_every_handle_is_a_block(fake_instagram, synth):
    page = (404, b"<!DOCTYPE html><html>invented</html>", {})
    handles = ["fake_a", "fake_b", "fake_c", "fake_d"]
    for handle in handles:
        fake_instagram.script[handle] = [page]
    state = {}
    fetcher, _ = make(fake_instagram, [{"handle": h} for h in handles], state, prime=0)
    out = {h: records for h, records in fetcher.run()}
    assert list(out) == ["fake_a", "fake_b"]
    assert fetcher.summary["stopped"] == "blocked"
    assert state["strikes"] == 1 and state["backoff_until"]
    assert fake_instagram.profile_calls() == ["fake_a", "fake_b", "fake_c"]


def test_ig_4c_one_html_404_between_profiles_is_not_found(fake_instagram, synth):
    fake_instagram.profiles["fake_venue"] = synth.profile("fake_venue", "71001", [])
    fake_instagram.script["fake_gone"] = [(404, b"<html>invented</html>", {})]
    handles = ["fake_gone", "fake_venue", "fake_gone2", "fake_gone3"]
    for handle in ("fake_gone2", "fake_gone3"):
        fake_instagram.script[handle] = [(404, b"<html>invented</html>", {})]
    fetcher, _ = make(fake_instagram, [{"handle": h} for h in handles], {}, prime=0)
    out = {h: records for h, records in fetcher.run()}
    assert fetcher.summary["stopped"] is None
    assert [out[h][0]["status"] for h in ("fake_gone", "fake_gone2", "fake_gone3")] == [
        "not_found"
    ] * 3


def test_ig_4_delay_between_requests(fake_instagram, synth):
    sleeps = []

    class Rng:
        def random(self):
            return 0.25

    fetcher = fetch.Fetcher(
        synth.accounts,
        {},
        {"base_url": fake_instagram.url, "delay_s": "1.5", "prime": "1"},
        clock=Clock(),
        sleep=sleeps.append,
        rng=Rng(),
    )
    list(fetcher.run())
    assert sleeps == [1.75, 1.75, 1.75]  # Prime, then three profiles: three gaps.


def test_ig_5_collab_post_carries_owner_config(fake_instagram, synth):
    fetcher, _ = make(fake_instagram, synth.accounts, prime=0)
    posts = [
        r for r in drain(fetcher) if r["kind"] == "post" and r["id"] == "FAKECOLLAB1"
    ]
    assert [p["account"] for p in posts] == ["fake_venue", "fake_records"]
    assert posts[0]["account_config"] == posts[1]["account_config"]
    assert posts[1]["account_config"]["label"] == "Invented Venue"


def run_cli(script, args, env, cwd):
    return subprocess.run(
        [sys.executable, "-I", "-S", str(script), *args],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
    )


def base_env(tmp_path):
    keep = ("SYSTEMROOT", "PATH", "TEMP", "TMP")
    return {k: os.environ[k] for k in keep if k in os.environ}


def test_ig_6b_cli_export_creates_state_directory(fake_instagram, synth, tmp_path):
    accounts = tmp_path / "accounts.json"
    accounts.write_text(json.dumps(synth.accounts), encoding="utf-8")
    out = tmp_path / "feed.jsonl"
    state = tmp_path / "state" / "nested" / "feed-state.json"
    env = dict(
        base_env(tmp_path),
        INSTAGRAM_FEED_BASE_URL=fake_instagram.url,
        INSTAGRAM_FEED_ACCOUNTS_FILE=str(accounts),
        INSTAGRAM_FEED_DELAY_S="0",
    )
    result = run_cli(
        FETCH_PY,
        ["export", "--out", str(out), "--batch", "1", "--state", str(state)],
        env,
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(state.read_text(encoding="utf-8"))["requests"]["count"] >= 1


def test_ig_6_cli_export_standalone_stdlib_only(fake_instagram, synth, tmp_path):
    # A copy outside the package, run with -I -S (no site-packages): stdlib only.
    script_dir = tmp_path / "remote"
    script_dir.mkdir()
    script = script_dir / "fetch.py"
    shutil.copy(FETCH_PY, script)
    accounts = tmp_path / "accounts.json"
    accounts.write_text(json.dumps(synth.accounts), encoding="utf-8")
    out = tmp_path / "data" / "feed.jsonl"
    out.parent.mkdir()
    env = dict(
        base_env(tmp_path),
        INSTAGRAM_FEED_BASE_URL=fake_instagram.url,
        INSTAGRAM_FEED_ACCOUNTS_FILE=str(accounts),
        INSTAGRAM_FEED_DELAY_S="0",
    )
    first = run_cli(
        script, ["export", "--out", str(out), "--batch", "2"], env, script_dir
    )
    assert first.returncode == 0, first.stderr
    summary = json.loads(first.stderr.strip().splitlines()[-1])
    assert (
        summary["accounts"] == 2 and summary["requests"] == 3 and summary["written"] > 0
    )
    second = run_cli(
        script, ["export", "--out", str(out), "--batch", "2"], env, script_dir
    )
    assert second.returncode == 0, second.stderr
    lines = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert {r["kind"] for r in lines} == {"profile", "post"}
    assert [r["account"] for r in lines if r["kind"] == "profile"] == [
        "fake_venue",
        "fake_records",
        "fake_gallery",
        "fake_venue",
    ]
    state = json.loads((out.parent / "feed.jsonl.state.json").read_text())
    assert state["requests"]["count"] == 6
    listed = run_cli(script, ["accounts"], env, script_dir)
    assert [json.loads(x)["handle"] for x in listed.stdout.splitlines()] == [
        "fake_venue",
        "fake_records",
        "fake_gallery",
    ]


@pytest.mark.parametrize("style", ["netscape", "json"])
def test_ig_6_cookies_file_used_never_leaked(fake_instagram, synth, tmp_path, style):
    canary = "fake-session-canary-7"
    cookies = tmp_path / "cookies.txt"
    if style == "netscape":
        cookies.write_text(
            "# Netscape HTTP Cookie File\n"
            f"127.0.0.1\tFALSE\t/\tFALSE\t4102444800\tsessionid\t{canary}\n"
            "127.0.0.1\tFALSE\t/\tFALSE\t4102444800\tcsrftoken\tfake-csrf-file\n",
            encoding="utf-8",
        )
    else:
        cookies.write_text(
            json.dumps(
                [
                    {
                        "name": "sessionid",
                        "value": canary,
                        "domain": "127.0.0.1",
                        "path": "/",
                        "secure": False,
                    },
                    {
                        "name": "csrftoken",
                        "value": "fake-csrf-file",
                        "domain": "127.0.0.1",
                        "path": "/",
                        "secure": False,
                    },
                ]
            ),
            encoding="utf-8",
        )
    out = tmp_path / "feed.jsonl"
    env = dict(
        base_env(tmp_path),
        INSTAGRAM_FEED_BASE_URL=fake_instagram.url,
        INSTAGRAM_FEED_ACCOUNTS="fake_venue",
        INSTAGRAM_FEED_DELAY_S="0",
        INSTAGRAM_FEED_COOKIES_FILE=str(cookies),
    )
    result = run_cli(FETCH_PY, ["export", "--out", str(out)], env, tmp_path)
    assert result.returncode == 0, result.stderr
    # The cookie went to the (fake) site for profile and stories; priming was skipped.
    assert [e["path"] for e in fake_instagram.log] == [
        "/api/v1/users/web_profile_info/",
        "/api/v1/feed/reels_media/",
    ]
    for entry in fake_instagram.log:
        assert canary in entry["headers"].get("cookie", "")
        assert entry["headers"]["x-csrftoken"] == "fake-csrf-file"
    for text in (
        result.stdout,
        result.stderr,
        out.read_text(encoding="utf-8"),
        Path(str(out) + ".state.json").read_text(encoding="utf-8"),
    ):
        assert canary not in text


def test_ig_6_cli_needs_accounts(tmp_path):
    result = run_cli(
        FETCH_PY,
        ["export", "--out", str(tmp_path / "x.jsonl")],
        base_env(tmp_path),
        tmp_path,
    )
    assert result.returncode != 0
    assert "no accounts configured" in result.stderr


@pytest.mark.parametrize(
    "answer",
    [
        (403, {"status": "fail", "message": "Please wait a few minutes"}, {}),
        (400, {"status": "fail", "message": "checkpoint_required"}, {}),
        (200, {"status": "fail", "require_login": True}, {}),
        (400, {"status": "fail", "spam": True, "message": "feedback"}, {}),
    ],
)
def test_ig_13_json_soft_blocks_on_any_status_cool_down(fake_instagram, synth, answer):
    fake_instagram.script["fake_venue"] = [answer]
    state = {}
    fetcher, _ = make(fake_instagram, synth.accounts, state, prime=0)
    assert list(fetcher.run()) == []
    assert fetcher.summary["stopped"] == "blocked"
    assert state["strikes"] == 1 and state["backoff_until"]
    assert fake_instagram.profile_calls() == ["fake_venue"]


def test_ig_13_plain_4xx_is_an_account_error_not_a_block(fake_instagram, synth):
    fake_instagram.script["fake_venue"] = [(400, {"status": "fail"}, {})]
    state = {}
    fetcher, _ = make(fake_instagram, synth.accounts, state, prime=0)
    out = {h: records for h, records in fetcher.run()}
    assert out["fake_venue"][0]["status"] == "http_400"
    assert set(out) == {"fake_venue", "fake_records", "fake_gallery"}
    assert state["strikes"] == 0


def test_ig_13_retry_after_http_date(fake_instagram, synth):
    when = T0 + timedelta(hours=3)
    fake_instagram.script["fake_venue"] = [
        (429, {}, {"Retry-After": when.strftime("%a, %d %b %Y %H:%M:%S GMT")})
    ]
    state = {}
    fetcher, _ = make(fake_instagram, synth.accounts, state, prime=0, cooldown_s=60)
    assert list(fetcher.run()) == []
    assert state["backoff_until"] == fetch.iso(when)
    assert fetch._retry_after({"Retry-After": "soon"}) is None


def test_ig_13_incomplete_read_is_retried_not_fatal(synth):
    import http.client

    calls = []

    def transport(url, headers):
        calls.append(url)
        raise http.client.IncompleteRead(b"partial")

    fetcher = fetch.Fetcher(
        [fetch._account("fake_venue")],
        {},
        {"prime": "0", "delay_s": "0", "retries": "2"},
        transport=transport,
        clock=Clock(),
        sleep=lambda s: None,
    )
    out = list(fetcher.run())
    assert out[0][1][0]["status"] == "error"
    assert len(calls) == 2


def test_ig_13_collab_owner_without_username_keeps_fetched_config(synth):
    collab = synth.node("FAKEIDONLY", synth.base_ts, owner=("79999", "x"))
    collab["owner"] = {"id": "79999"}
    own = synth.node("FAKEOWN", synth.base_ts)
    own["owner"] = {"id": "71001"}
    user = synth.profile("fake_venue", "71001", [collab, own])["data"]["user"]
    fetcher = fetch.Fetcher(
        [fetch._account(a) for a in synth.accounts],
        {},
        {"prime": "0"},
        transport=lambda u, h: (200, {}, b"{}", u),
        clock=Clock(),
        sleep=lambda s: None,
    )
    records = fetcher.records(fetcher.accounts[0], "ok", user, "2021-06-01T00:00:00Z")
    posts = {r["id"]: r for r in records if r["kind"] == "post"}
    assert posts["FAKEIDONLY"]["account_config"] == {}
    assert posts["FAKEOWN"]["account_config"]["label"] == "Invented Venue"


def test_ig_13_cli_has_no_cookie_argument():
    with pytest.raises(SystemExit):
        fetch.main(["export", "--out", "x.jsonl", "--cookies-file", "c.txt"])


def test_ig_14_stories_fetched_when_authenticated_and_skipped_when_anonymous(
    fake_instagram, synth, tmp_path
):
    st1 = synth.story(
        "990001",
        synth.base_ts + 50,
        caption="Doors open now!",
        links=[("https://example.invalid/tickets", "Tickets")],
        mentions=["fake_records"],
    )
    fake_instagram.stories["71001"] = {
        "reels_media": [{"id": 71001, "items": [st1]}],
        "status": "ok",
    }
    # Anonymous: stories endpoint is not called; summary notes auth requirement.
    fetcher, _ = make(fake_instagram, synth.accounts[:1], prime=0)
    records = drain(fetcher)
    assert [r["kind"] for r in records if r["kind"] == "story"] == []
    assert fake_instagram.story_calls() == []
    assert fetcher.summary.get("stories_auth_required") is True
    # Authenticated via cookies file: stories are fetched alongside posts.
    cookies = tmp_path / "cookies.json"
    cookies.write_text(
        json.dumps(
            [
                {
                    "name": "sessionid",
                    "value": "fake-sess-1",
                    "domain": "127.0.0.1",
                    "path": "/",
                    "secure": False,
                },
                {
                    "name": "csrftoken",
                    "value": "fake-csrf-1",
                    "domain": "127.0.0.1",
                    "path": "/",
                    "secure": False,
                },
            ]
        ),
        encoding="utf-8",
    )
    state = {}
    auth_fetcher = fetch.Fetcher(
        [fetch._account(synth.accounts[0])],
        state,
        {"base_url": fake_instagram.url, "delay_s": "0", "prime": "0"},
        cookies_file=str(cookies),
        clock=Clock(),
        sleep=lambda s: None,
    )
    auth_records = drain(auth_fetcher)
    stories = [r for r in auth_records if r["kind"] == "story"]
    assert len(stories) == 1
    assert stories[0]["id"] == "990001"
    assert stories[0]["source"] == "reels_media"
    assert auth_fetcher.summary["stories"] == 1
    assert state["accounts"]["fake_venue"]["newest_story_at"] == synth.base_ts + 50
    assert fake_instagram.story_calls() == ["71001"]


def test_ig_14_html_fetch_mode_enriches_and_caches_known_posts(fake_instagram, synth):
    n1 = synth.node(
        "HTMLPOST1",
        synth.base_ts + 100,
        caption="Opening set #LiveMusic",
        location=("5501", "Invented Hall"),
    )
    n2 = synth.node(
        "HTMLREEL1",
        synth.base_ts + 200,
        kind="reel",
        caption="Reel clip",
    )
    fake_instagram.html_profiles["fake_venue"] = synth.profile_html(
        "fake_venue", "71001", [n1, n2], latest_reel_media=synth.base_ts + 300
    )
    fake_instagram.html_posts["HTMLPOST1"] = synth.post_detail_html(n1)
    fake_instagram.html_posts["HTMLREEL1"] = synth.post_detail_html(n2)
    state = {}
    fetcher, _ = make(
        fake_instagram,
        synth.accounts[:1],
        state,
        prime=0,
        fetch_mode="auto",
    )
    records = drain(fetcher)
    posts = [r for r in records if r["kind"] == "post"]
    assert [p["id"] for p in posts] == ["HTMLPOST1", "HTMLREEL1"]
    assert posts[0]["source"] == "web_profile_html"
    assert posts[0]["raw"]["taken_at"] == synth.base_ts + 100
    assert state["accounts"]["fake_venue"]["user_id"] == "71001"
    assert state["accounts"]["fake_venue"]["latest_reel_media"] == synth.base_ts + 300
    # Second run with unchanged grid captions skips redundant /p/ and /reel/ fetches.
    calls_before = len(fake_instagram.log)
    fetcher2, _ = make(
        fake_instagram,
        synth.accounts[:1],
        state,
        prime=0,
        fetch_mode="auto",
    )
    records2 = drain(fetcher2)
    assert [r for r in records2 if r["kind"] == "post"] == []
    new_paths = [e["path"] for e in fake_instagram.log[calls_before:]]
    assert new_paths == ["/fake_venue/"]

