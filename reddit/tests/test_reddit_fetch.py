"""fetch.py against a loopback fake Reddit. No real network, no real data."""

import ast
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from throughline_reddit import fetch

FETCH_PY = Path(fetch.__file__)
UA = "test:throughline-reddit:0.1.0 (by /u/invented_owner)"


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def clock(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(round(seconds, 3))
        self.t += seconds


def make_config(fake, **values):
    base = dict(
        user_agent=UA,
        api_base=fake.url,
        auth_base=fake.url,
        min_interval="0",
        listings="saved",
    )
    base.update(values)
    return fetch.Config(base)


def creds(s, **overrides):
    values = dict(
        client_id=s.CLIENT_ID,
        client_secret=s.CLIENT_SECRET,
        refresh_token=s.REFRESH_TOKEN,
    )
    values.update(overrides)
    return fetch.Credentials(**values)


def export(config, credentials, state=None, clock=None, now=None):
    clock = clock or Clock()
    options = dict(sleep=clock.sleep, clock=clock.clock)
    if now:
        options["now"] = now
    exporter = fetch.Exporter(config, credentials, state, **options)
    records = list(exporter.records())
    return records, exporter.state, records[-1]


def ids(records, kind="reddit.item"):
    return [r["id"] for r in records if r["kind"] == kind]


def saved_posts(s, count):
    return [
        s.post(
            f"sv{i:02d}",
            s.GARDEN,
            f"Invented saved {i}",
            "invented_author_a",
            f"2025-01-{i + 1:02d}T10:00:00",
        )
        for i in range(count)
    ]


def test_rd_fetch_1_standard_library_only():
    tree = ast.parse(FETCH_PY.read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "fetch.py must not use relative imports"
            modules.add(node.module.split(".")[0])
    assert modules <= set(sys.stdlib_module_names), modules - set(
        sys.stdlib_module_names
    )


def test_rd_fetch_2_cli_standalone_export_and_incremental(
    tmp_path, fake_reddit, synthetic
):
    s = synthetic
    fake_reddit.listings["saved"] = saved_posts(s, 3)
    fake_reddit.listings["submitted"] = [
        s.post("ps01", s.GARDEN, "Invented own post", s.OWNER, "2025-01-20T10:00:00")
    ]
    standalone = tmp_path / "remote" / "fetch.py"
    standalone.parent.mkdir()
    shutil.copy(FETCH_PY, standalone)
    out, state = tmp_path / "out.jsonl", tmp_path / "state.json"
    env = {k: v for k, v in os.environ.items() if not k.startswith("REDDIT_")}
    env.update(
        REDDIT_USER_AGENT=UA,
        REDDIT_CLIENT_ID=s.CLIENT_ID,
        REDDIT_CLIENT_SECRET=s.CLIENT_SECRET,
        REDDIT_REFRESH_TOKEN=s.REFRESH_TOKEN,
        REDDIT_API_BASE=fake_reddit.url,
        REDDIT_AUTH_BASE=fake_reddit.url,
    )
    command = [
        sys.executable,
        "-I",
        str(standalone),
        "export",
        "--out",
        str(out),
        "--state",
        str(state),
        "--listings",
        "saved,submitted",
        "--min-interval",
        "0",
    ]
    first = subprocess.run(
        command, env=env, capture_output=True, text=True, timeout=60, cwd=tmp_path
    )
    assert first.returncode == 0, first.stderr
    records = [
        json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()
    ]
    assert ids(records) == ["t3_sv00", "t3_sv01", "t3_sv02", "t3_ps01"]
    assert records[-1]["kind"] == "reddit.export_summary"
    assert records[-1]["account"] == s.OWNER
    item = records[0]
    assert item["listing"] == "saved" and item["source"] == "oauth"
    assert item["raw"] == fake_reddit.listings["saved"][0]
    saved_state = json.loads(state.read_text())
    assert saved_state["listings"]["saved"]["head"] == ["t3_sv00", "t3_sv01", "t3_sv02"]
    secrets = [s.CLIENT_SECRET, s.REFRESH_TOKEN, "invented-access-token"]
    for text in (first.stdout, first.stderr, out.read_text(), state.read_text()):
        assert not any(secret in text for secret in secrets)
    assert all(r.headers.get("User-Agent") == UA for r in fake_reddit.requests)
    listed = [r for r in fake_reddit.requests if r.path.startswith("/user/")]
    assert listed and all(r.headers["Authorization"].startswith("Bearer ") for r in listed)

    fake_reddit.listings["saved"].insert(
        0,
        s.post(
            "sv99",
            s.GARDEN,
            "Invented newest save",
            "invented_author_a",
            "2025-01-30T10:00:00",
        ),
    )
    before = len(fake_reddit.requests)
    second = subprocess.run(
        command, env=env, capture_output=True, text=True, timeout=60, cwd=tmp_path
    )
    assert second.returncode == 0, second.stderr
    records = [
        json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()
    ]
    assert ids(records) == ["t3_sv99"]
    # token, me, one page per listing
    assert len(fake_reddit.requests) - before == 4


def test_rd_fetch_3_cli_requires_user_agent_and_never_takes_secrets_as_flags(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("REDDIT_")}
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(FETCH_PY),
            "export",
            "--out",
            str(tmp_path / "o.jsonl"),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 2 and "REDDIT_USER_AGENT" in result.stderr
    help_text = subprocess.run(
        [sys.executable, "-I", str(FETCH_PY), "export", "--help"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout
    for flag in (
        "--password",
        "--refresh-token",
        "--client-secret",
        "--token",
        "--client-id",
    ):
        assert flag not in help_text


def test_rd_fetch_4_pagination_heads_and_incremental(fake_reddit, synthetic):
    s = synthetic
    fake_reddit.listings["saved"] = saved_posts(s, 5)
    config = make_config(fake_reddit, page_size="2")
    records, state, summary = export(config, creds(s))
    assert ids(records) == [f"t3_sv{i:02d}" for i in range(5)]
    assert summary["listings"]["saved"] == {"items": 5, "pages": 3, "end": "end"}
    assert state["listings"]["saved"]["head"] == [f"t3_sv{i:02d}" for i in range(5)]
    pages = [r for r in fake_reddit.requests if r.path.endswith("/saved")]
    assert [r.query.get("after") for r in pages] == [None, "t3_sv01", "t3_sv03"]
    assert all(r.query["raw_json"] == "1" and r.query["limit"] == "2" for r in pages)

    fake_reddit.listings["saved"].insert(
        0, s.post("sv50", s.COOK, "Invented new", "x_author", "2025-02-01T00:00:00")
    )
    records, state, summary = export(config, creds(s), state)
    assert ids(records) == ["t3_sv50"]
    # The pass ends after three consecutive known ids (one more page).
    assert summary["listings"]["saved"] == {"items": 1, "pages": 2, "end": "known"}
    assert state["listings"]["saved"]["head"] == ["t3_sv50"] + [
        f"t3_sv{i:02d}" for i in range(5)
    ]

    # A full pass ignores the head and pages to the bounds.
    full = make_config(fake_reddit, page_size="2", max_pages="2", full=True)
    records, state, summary = export(full, creds(s), state)
    assert ids(records) == ["t3_sv50", "t3_sv00", "t3_sv01", "t3_sv02"]
    assert summary["listings"]["saved"]["end"] == "max_pages"


def test_rd_fetch_5_since_until_bounds(fake_reddit, synthetic):
    s = synthetic
    fake_reddit.listings["submitted"] = [
        s.post(
            f"sb{i}",
            s.GARDEN,
            f"Invented {i}",
            s.OWNER,
            f"2025-01-{20 - i:02d}T10:00:00",
        )
        for i in range(10)
    ]
    config = make_config(
        fake_reddit,
        listings="submitted",
        page_size="3",
        since="2025-01-15",
        until="2025-01-18T23:59:59",
    )
    records, _, summary = export(config, creds(s))
    assert ids(records) == ["t3_sb2", "t3_sb3", "t3_sb4", "t3_sb5"]
    assert summary["listings"]["submitted"]["end"] == "since"
    pages = [r for r in fake_reddit.requests if r.path.endswith("/submitted")]
    assert len(pages) == 3 and pages[0].query["sort"] == "new"


def test_rd_fetch_6_retries_429_and_5xx_with_backoff(fake_reddit, synthetic):
    s = synthetic
    fake_reddit.listings["saved"] = saved_posts(s, 1)
    path = f"/user/{s.OWNER}/saved"
    fake_reddit.failures[path] = [(429, {"Retry-After": "7"}), (503, {})]
    clock = Clock()
    records, _, _ = export(make_config(fake_reddit), creds(s), clock=clock)
    assert ids(records) == ["t3_sv00"]
    assert clock.sleeps == [7.0, 2.0]

    fake_reddit.failures[path] = [(502, {})] * 3
    with pytest.raises(fetch.FetchError, match=r"failed after retries \(502\)"):
        export(make_config(fake_reddit, retries="2"), creds(s))

    fake_reddit.failures[path] = [(429, {"Retry-After": "900"})]
    _, state, summary = export(make_config(fake_reddit, max_wait="60"), creds(s))
    assert "REDDIT_MAX_WAIT" in summary["truncated"]
    assert state["listings"]["saved"]["resume"]["after"] is None


def test_rd_fetch_7_rate_limit_headers_and_pacing(fake_reddit, synthetic):
    s = synthetic
    fake_reddit.listings["saved"] = saved_posts(s, 4)
    path = f"/user/{s.OWNER}/saved"
    fake_reddit.extra_headers[path] = {
        "X-Ratelimit-Remaining": "0",
        "X-Ratelimit-Used": "100",
        "X-Ratelimit-Reset": "30",
    }
    clock = Clock()
    export(
        make_config(fake_reddit, page_size="2", min_interval="1.5"),
        creds(s),
        clock=clock,
    )
    # Page 1 waits 1.5 s after the identity call; page 2 waits for the reset.
    assert clock.sleeps == [1.5, 31.0]


def test_rd_fetch_8_run_budget_truncates_and_resumes(fake_reddit, synthetic):
    s = synthetic
    fake_reddit.listings["saved"] = saved_posts(s, 6)
    config = make_config(fake_reddit, page_size="2", max_requests="4")
    records, state, summary = export(config, creds(s))
    assert ids(records) == ["t3_sv00", "t3_sv01", "t3_sv02", "t3_sv03"]
    assert summary["truncated"] == "per-run request limit reached"
    assert state["listings"]["saved"]["resume"] == {
        "after": "t3_sv03",
        "stop": [],
        "head": ["t3_sv00", "t3_sv01", "t3_sv02", "t3_sv03"],
    }
    assert "head" not in state["listings"]["saved"]

    records, state, summary = export(config, creds(s), state)
    assert ids(records) == ["t3_sv04", "t3_sv05"]
    assert summary["truncated"] is None
    assert state["listings"]["saved"]["head"] == [f"t3_sv{i:02d}" for i in range(6)]
    assert "resume" not in state["listings"]["saved"]


def test_rd_fetch_9_daily_budget_is_kept_in_state(fake_reddit, synthetic):
    s = synthetic
    fake_reddit.listings["saved"] = saved_posts(s, 1)
    day = [datetime(2026, 1, 5, 12, tzinfo=timezone.utc)]
    config = make_config(fake_reddit, max_requests_per_day="4")
    _, state, summary = export(config, creds(s), now=lambda: day[0])
    assert summary["requests"] == 3 and state["requests"] == {
        "day": "2026-01-05",
        "count": 3,
    }
    before = len(fake_reddit.requests)
    _, state, summary = export(config, creds(s), state, now=lambda: day[0])
    assert summary["truncated"] == "per-day request limit reached"
    assert len(fake_reddit.requests) - before == 1
    day[0] = datetime(2026, 1, 6, 0, 1, tzinfo=timezone.utc)
    _, state, summary = export(config, creds(s), state, now=lambda: day[0])
    assert summary["truncated"] is None and state["requests"]["day"] == "2026-01-06"


def test_rd_fetch_10_token_refresh_on_401_and_errors_without_secrets(
    fake_reddit, synthetic
):
    s = synthetic
    fake_reddit.listings["saved"] = saved_posts(s, 1)
    stale = "invented-stale-access-canary"
    records, _, _ = export(make_config(fake_reddit), creds(s, access_token=stale))
    assert ids(records) == ["t3_sv00"]
    statuses = [r.path for r in fake_reddit.requests]
    assert statuses[:2] == ["/api/v1/me", "/api/v1/access_token"]

    with pytest.raises(fetch.FetchError, match="access token is not valid") as caught:
        export(make_config(fake_reddit), fetch.Credentials(access_token=stale))
    assert stale not in str(caught.value)

    fake_reddit.token_error = "invalid_grant"
    with pytest.raises(fetch.FetchError) as caught:
        export(make_config(fake_reddit), creds(s))
    assert (
        str(caught.value)
        == "Reddit token endpoint returned no access token (invalid_grant)"
    )
    assert "redacted" in repr(creds(s))


def test_rd_fetch_11_password_grant_uses_configured_username(fake_reddit, synthetic):
    s = synthetic
    fake_reddit.listings["upvoted"] = saved_posts(s, 2)
    config = make_config(fake_reddit, listings="upvoted", username=s.OWNER)
    records, _, summary = export(
        config, creds(s, refresh_token=None, password=s.PASSWORD)
    )
    assert ids(records) == ["t3_sv00", "t3_sv01"]
    assert summary["account"] == s.OWNER
    assert "/api/v1/me" not in [r.path for r in fake_reddit.requests]
    token_call = next(
        r for r in fake_reddit.requests if r.path == "/api/v1/access_token"
    )
    assert "grant_type=password" in token_call.body


def test_rd_fetch_12_redirects_are_never_followed(fake_reddit, synthetic):
    s = synthetic
    fake_reddit.listings["saved"] = saved_posts(s, 1)
    fake_reddit.redirects.add(f"/user/{s.OWNER}/saved")
    exporter = fetch.Exporter(make_config(fake_reddit), creds(s), sleep=lambda _: None)
    with pytest.raises(fetch.HttpStatus) as caught:
        list(exporter.records())
    assert caught.value.status == 302
    assert not any("reddit.invalid" in r.path for r in fake_reddit.requests)


def test_rd_fetch_13_listings_without_credentials_are_skipped(fake_reddit, synthetic):
    config = make_config(fake_reddit)
    records, _, summary = export(config, fetch.Credentials())
    assert summary["skipped"] == [
        {"what": "listings", "reason": "no OAuth credentials"}
    ]
    assert fake_reddit.requests == []


@pytest.mark.parametrize(
    "values, message",
    [
        ({"api_base": "http://example.invalid"}, "REDDIT_API_BASE"),
        ({"auth_base": "https://user:pw@example.invalid"}, "REDDIT_AUTH_BASE"),
        ({"listings": "saved,history"}, "REDDIT_LISTINGS"),
        ({"page_size": "500"}, "REDDIT_PAGE_SIZE"),
        ({"transport": "rss"}, "REDDIT_TRANSPORT"),
        ({"transport": "session"}, "REDDIT_SESSION_PROFILE"),
        ({"since": "2025-02-01", "until": "2025-01-01"}, "precedes"),
        ({"username": "bad name"}, "REDDIT_USERNAME"),
    ],
)
def test_rd_fetch_14_configuration_is_validated(values, message):
    with pytest.raises(fetch.FetchError, match=message):
        fetch.Config(values)


def test_rd_fetch_15_resaved_item_on_top_does_not_end_the_pass(fake_reddit, synthetic):
    s = synthetic
    fake_reddit.listings["saved"] = saved_posts(s, 6)
    config = make_config(fake_reddit, page_size="3")
    _, state, _ = export(config, creds(s))
    # Saving sv03 again moves it to the top, above two items that are new.
    listing = fake_reddit.listings["saved"]
    moved = listing.pop(3)
    new = [
        s.post(f"nw{i}", s.COOK, f"Invented new {i}", "x_author", "2025-03-01T00:00")
        for i in range(2)
    ]
    fake_reddit.listings["saved"] = [moved] + new + listing
    records, state, summary = export(config, creds(s), state)
    assert ids(records) == ["t3_nw0", "t3_nw1"]
    assert summary["listings"]["saved"]["end"] == "known"
    assert state["listings"]["saved"]["head"][:3] == ["t3_sv03", "t3_nw0", "t3_nw1"]


def test_rd_fetch_16_until_bound_does_not_hide_newer_items(fake_reddit, synthetic):
    s = synthetic
    fake_reddit.listings["saved"] = [
        s.post("nw1", s.COOK, "Invented recent", "x_author", "2025-03-01T00:00:00")
    ] + saved_posts(s, 4)
    bounded = make_config(fake_reddit, until="2025-02-01")
    records, state, _ = export(bounded, creds(s))
    assert ids(records) == [f"t3_sv{i:02d}" for i in range(4)]
    assert "t3_nw1" not in state["listings"]["saved"]["head"]
    records, state, _ = export(make_config(fake_reddit), creds(s), state)
    assert ids(records) == ["t3_nw1"]


def test_rd_fetch_17_failed_cli_run_keeps_the_daily_request_count(
    tmp_path, fake_reddit, synthetic
):
    s = synthetic
    fake_reddit.listings["saved"] = saved_posts(s, 1)
    fake_reddit.failures[f"/user/{s.OWNER}/saved"] = [(502, {})] * 5
    state_file = tmp_path / "state.json"
    before = {"version": 1, "listings": {"saved": {"head": ["t3_old"]}}}
    state_file.write_text(json.dumps(before), encoding="utf-8")
    environ = {
        "REDDIT_USER_AGENT": UA,
        "REDDIT_CLIENT_ID": s.CLIENT_ID,
        "REDDIT_CLIENT_SECRET": s.CLIENT_SECRET,
        "REDDIT_REFRESH_TOKEN": s.REFRESH_TOKEN,
        "REDDIT_API_BASE": fake_reddit.url,
        "REDDIT_AUTH_BASE": fake_reddit.url,
        "REDDIT_MIN_INTERVAL": "0",
        "REDDIT_LISTINGS": "saved",
        "REDDIT_RETRIES": "0",
    }
    out = tmp_path / "out.jsonl"
    argv = ["export", "--out", str(out), "--state", str(state_file)]
    assert fetch.main(argv, environ) == 1
    after = json.loads(state_file.read_text(encoding="utf-8"))
    # Token, identity and the failed listing request were spent and counted;
    # nothing else in the state moved.
    assert after["requests"]["count"] == 3
    assert after["listings"] == before["listings"]
    text = state_file.read_text(encoding="utf-8") + out.read_text(encoding="utf-8")
    assert s.REFRESH_TOKEN not in text and s.CLIENT_SECRET not in text


def test_rd_fetch_18_capped_listing_windows_record_gaps(
    fake_reddit, synthetic
):
    s = synthetic

    # A listing pass that runs out of pages before reaching the head.
    day = [datetime(2025, 3, 1, tzinfo=timezone.utc)]
    fake_reddit.listings["saved"] = saved_posts(s, 2)
    config = make_config(fake_reddit, page_size="2", max_pages="1")
    _, state, summary = export(config, creds(s), now=lambda: day[0])
    assert summary["listings"]["saved"]["end"] == "end"
    day[0] = datetime(2025, 3, 5, tzinfo=timezone.utc)
    fake_reddit.listings["saved"] = [
        s.post(f"nw{i}", s.COOK, "Invented new", "x_author", "2025-03-02T00:00:00")
        for i in range(3)
    ] + saved_posts(s, 2)
    _, state, summary = export(config, creds(s), state, now=lambda: day[0])
    assert summary["listings"]["saved"]["end"] == "max_pages"
    assert summary["listings"]["saved"]["gap"] == {
        "from": "2025-03-01T00:00:00+00:00",
        "to": "2025-03-05T00:00:00+00:00",
    }
