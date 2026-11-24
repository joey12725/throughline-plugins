import ast
import copy
import json
import os
import subprocess
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import pytest
from throughline_sdk import Checkpoint

from throughline_calendar import Calendar, fetch

FETCH_PY = Path(fetch.__file__)
NOW = datetime(2024, 3, 1, 12, 0, tzinfo=UTC)


def direct():
    # Tests talk to 127.0.0.1 only: never through a proxy from the environment.
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), fetch._NoRedirect
    )


def fetcher(composio, data, **overrides):
    values = {
        "accounts": data.ACCOUNT,
        "base_url": composio.base_url,
        "min_interval_s": "0",
        "page_size": "2",
    }
    values.update({k: str(v) for k, v in overrides.items()})
    secrets = {
        "api_key": data.CANARY_KEY,
        "mcp_url": composio.mcp_url,
        "mcp_token": data.CANARY_MCP,
    }
    sleeps = []
    return fetch.Fetcher(
        fetch.config(values), secrets, now=NOW, sleep=sleeps.append, opener=direct()
    ), sleeps


def kinds(records):
    counts = {}
    for record in records:
        counts[record["kind"]] = counts.get(record["kind"], 0) + 1
    return counts


def test_cal_fetch_uses_only_the_standard_library():
    tree = ast.parse(FETCH_PY.read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "fetch.py must stay a standalone file"
            modules.add(node.module.split(".")[0])
    assert modules <= set(sys.stdlib_module_names)


def test_cal_rest_pages_retries_and_cursor(composio, data):
    composio.failures = [429, 503]
    run, sleeps = fetcher(composio, data)
    state = {}
    records = list(run.run(state))
    assert sleeps == [0.0, 0.0]  # Retry-After honoured on 429 and 5xx.
    assert kinds(records) == {
        "run": 1,
        "calendar": 2,
        "occurrence": 5,
        "window": 2,
        "event": 2,
        "changes": 2,
        "run_end": 1,
    }
    assert records[0]["fetched_at"] == NOW.isoformat()
    assert records[0]["window"] == [
        "2024-01-31T00:00:00+00:00",
        "2024-05-31T00:00:00+00:00",
    ]
    assert records[-1]["complete"] is True and records[-1]["errors"] == []
    assert {r["run"] for r in records} == {records[0]["run"]}
    calls = [r for r in composio.requests if r["path"].endswith("EVENTS_LIST")]
    occurrence_args = [
        c["body"]["arguments"] for c in calls if c["body"]["arguments"]["singleEvents"]
    ]
    assert occurrence_args[0] == {
        "calendarId": data.CAL_A,
        "timeMin": "2024-01-31T00:00:00Z",
        "timeMax": "2024-05-31T00:00:00Z",
        "maxResults": 2,
        "singleEvents": True,
        "showDeleted": True,
        "orderBy": "startTime",
    }
    assert occurrence_args[1]["pageToken"] == "2"
    for request in composio.requests:
        assert request["headers"]["x-api-key"] == data.CANARY_KEY
        assert request["body"]["connected_account_id"] == data.ACCOUNT
        assert request["headers"]["user-agent"].startswith("throughline-calendar-fetch")
    change_args = [
        c["body"]["arguments"]
        for c in calls
        if not c["body"]["arguments"]["singleEvents"]
    ]
    assert change_args[0]["updatedMin"] == "2024-01-31T00:00:00Z"
    assert change_args[0]["showDeleted"] is True
    # The cursor is the newest modification time seen; the counter is per day.
    assert (
        state["changes"][data.ACCOUNT + "\t" + data.CAL_A]
        == "2024-02-25T10:00:00+00:00"
    )
    assert state["day"] == "2024-03-01" and state["day_calls"] == run.report[
        "calls"
    ] == len(composio.requests)
    assert data.CANARY_KEY not in json.dumps(records)


def test_cal_changes_are_incremental(composio, data):
    run, _ = fetcher(composio, data)
    state = {}
    list(run.run(state))
    composio.requests.clear()
    run, _ = fetcher(composio, data)
    records = list(run.run(state))
    changes = [
        r["body"]["arguments"]
        for r in composio.requests
        if r["path"].endswith("EVENTS_LIST")
        and not r["body"]["arguments"]["singleEvents"]
    ]
    assert changes[0]["updatedMin"] == "2024-02-25T09:59:00Z"  # One minute of overlap.
    assert [
        r["id"]
        for r in records
        if r["kind"] == "event" and r["calendar_id"] == data.CAL_A
    ] == ["inventedlunch"]


def test_cal_updated_min_too_old_falls_back_to_full_listing(composio, data):
    composio.updated_min_too_old = True
    run, _ = fetcher(composio, data, changes_since="2024-02-01")
    records = list(run.run({}))
    events = [
        r["id"]
        for r in records
        if r["kind"] == "event" and r["calendar_id"] == data.CAL_A
    ]
    # Read in full, filtered on our side by the same bound.
    assert events == ["inventedlunch"]
    marker = next(
        r for r in records if r["kind"] == "changes" and r["calendar_id"] == data.CAL_A
    )
    assert (
        marker["full"] is True and marker["updated_min"] == "2024-02-01T00:00:00+00:00"
    )
    run, _ = fetcher(composio, data, changes_since="all")
    records = list(run.run({}))
    assert (
        len(
            [
                r
                for r in records
                if r["kind"] == "event" and r["calendar_id"] == data.CAL_A
            ]
        )
        == 3
    )


def test_cal_per_run_and_per_day_limits(composio, data):
    run, _ = fetcher(composio, data, max_calls_per_run=3)
    state = {}
    records = list(run.run(state))
    assert len(composio.requests) == 3
    end = records[-1]
    assert (
        end["kind"] == "run_end"
        and end["complete"] is False
        and end["stopped"] == "per_run_limit"
    )
    assert "window" in kinds(records) and "changes" not in kinds(records)
    assert (
        state["changes"] == {}
    )  # An interrupted change pass does not move its cursor.
    composio.requests.clear()
    run, _ = fetcher(composio, data, max_calls_per_day=3)
    records = list(run.run(state))
    assert composio.requests == []
    assert records[-1]["stopped"] == "per_day_limit"
    state["day"] = "2024-02-29"  # A new UTC day resets the counter.
    run, _ = fetcher(composio, data, max_calls_per_day=3)
    list(run.run(state))
    assert len(composio.requests) == 3


def test_cal_mcp_transport_handshake_sse_and_page_size(composio, data):
    composio.mcp_requires_init = True
    composio.mcp_sse = True
    run, _ = fetcher(composio, data, transport="mcp", page_size=250)
    records = list(run.run({}))
    assert records[-1]["complete"] is True
    assert kinds(records)["occurrence"] == 5
    calls = [r for r in composio.requests if r["body"].get("method") == "tools/call"]
    assert [r["body"]["method"] for r in composio.requests[:3]] == [
        "tools/call",
        "initialize",
        "notifications/initialized",
    ]
    for call in calls:
        tool = call["body"]["params"]["arguments"]["tools"][0]
        assert call["body"]["params"]["name"] == "COMPOSIO_MULTI_EXECUTE_TOOL"
        assert tool["account"] == data.ACCOUNT
        assert (
            tool["arguments"].get("maxResults", tool["arguments"].get("max_results"))
            == 2
        )
        assert call["headers"]["authorization"] == "Bearer " + data.CANARY_MCP
    assert all(
        r["headers"].get("mcp-session-id") == "invented-session" for r in calls[1:]
    )


def test_cal_truncated_preview_is_never_trusted(composio, data):
    composio.preview_truncation = True
    run, _ = fetcher(composio, data, page_size=4)
    records = list(run.run({}))
    end = records[-1]
    assert end["complete"] is False
    assert any("truncated" in e["error"] for e in end["errors"])
    # The damaged calendar has no complete-window marker, so nothing is "removed".
    assert not [
        r for r in records if r["kind"] == "window" and r["calendar_id"] == data.CAL_A
    ]


def test_cal_errors_and_redirects_never_leak_credentials(composio, data):
    composio.redirect = True
    run, _ = fetcher(composio, data)
    records = list(run.run({}))
    assert records[-1]["errors"][0]["step"] == "calendars"
    assert len(composio.requests) == 1  # Not followed.
    composio.redirect = False
    values = {
        "accounts": data.ACCOUNT,
        "base_url": composio.base_url,
        "min_interval_s": "0",
    }
    wrong = "invented-wrong-key-canary"
    run = fetch.Fetcher(
        fetch.config(values), {"api_key": wrong}, now=NOW, opener=direct()
    )
    records = list(run.run({}))
    text = json.dumps(records)
    assert "HTTP 401" in text and wrong not in text


def test_cal_fetch_cli_export_and_state(composio, data, tmp_path):
    out, state = tmp_path / "export.jsonl", tmp_path / "state.json"
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("CALENDAR_", "COMPOSIO_"))
    }
    env.update(
        COMPOSIO_API_KEY=data.CANARY_KEY,
        COMPOSIO_BASE_URL=composio.base_url,
        CALENDAR_MIN_INTERVAL_S="0",
        NO_PROXY="127.0.0.1,localhost",
        no_proxy="127.0.0.1,localhost",
    )
    command = [
        sys.executable,
        "-I",
        str(FETCH_PY),
        "export",
        "--out",
        str(out),
        "--state",
        str(state),
        "--accounts",
        data.ACCOUNT,
    ]
    result = subprocess.run(
        command, env=env, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert data.CANARY_KEY not in result.stdout + result.stderr + out.read_text(
        encoding="utf-8"
    )
    summary = json.loads(result.stdout)
    assert summary["complete"] is True and summary["records"]["occurrence"] == 5
    first = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert first[0]["kind"] == "run" and first[-1]["kind"] == "run_end"
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["changes"] and data.CANARY_KEY not in json.dumps(saved)
    result = subprocess.run(
        command + ["--append"], env=env, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    lines = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert len({line["run"] for line in lines}) == 2
    # Credentials are never accepted as arguments.
    refused = subprocess.run(
        command + ["--api-key", "x"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert refused.returncode == 2
    missing = subprocess.run(
        command[:-2], env=env, capture_output=True, text=True, timeout=60
    )
    assert missing.returncode == 1 and "CALENDAR_ACCOUNTS" in missing.stderr


def test_cal_live_mode_matches_its_archived_export(
    calendar_env, composio, data, tmp_path
):
    archive = tmp_path / "archive"
    calendar_env.setenv("CALENDAR_SOURCE", "live")
    calendar_env.setenv("CALENDAR_ACCOUNTS", data.ACCOUNT)
    calendar_env.setenv("COMPOSIO_BASE_URL", composio.base_url)
    calendar_env.setenv("COMPOSIO_API_KEY", data.CANARY_KEY)
    calendar_env.setenv("CALENDAR_ARCHIVE_DIR", str(archive))
    live = Calendar()
    live.fetch_options = {"opener": direct(), "now": NOW}
    items = list(live.records({}, "sync"))
    rows = [i for i in items if isinstance(i, dict)]
    assert {m["channel"] for m in rows} == {"calendar.events", "calendar.changes"}
    cursor = [i for i in items if isinstance(i, Checkpoint)][-1].cursor
    assert (
        cursor["fetch"]["changes"]
        and cursor["fetch"]["day_calls"] == live.stats["fetch"]["calls"]
    )
    assert data.CANARY_KEY not in json.dumps([rows, cursor])
    assert len(list(archive.iterdir())) == 1
    calendar_env.setenv("CALENDAR_SOURCE", "export:" + str(archive))
    replay = [i for i in Calendar().records({}, "sync") if isinstance(i, dict)]
    assert replay == rows
    # A second live run against unchanged data emits nothing new.
    calendar_env.setenv("CALENDAR_SOURCE", "live")
    again = Calendar()
    again.fetch_options = {"opener": direct(), "now": NOW.replace(minute=30)}
    assert [
        i for i in again.records(copy.deepcopy(cursor), "sync") if isinstance(i, dict)
    ] == []


def test_cal_config_validation():
    with pytest.raises(ValueError, match="rest or mcp"):
        fetch.config({"transport": "smtp"})
    assert fetch.config({"transport": "mcp", "page_size": "250"})["page_size"] == 2
    assert fetch.config({"changes_since": "all"})["changes_since"] == "all"
    cfg = fetch.config({"backfill_from": "2015-01-01", "backfill_to": "2016-01-01"})
    start, end = fetch.window(cfg, "backfill", NOW)
    assert (start.isoformat(), end.isoformat()) == (
        "2015-01-01T00:00:00+00:00",
        "2016-01-01T00:00:00+00:00",
    )
    assert fetch.window(cfg, "sync", NOW)[0].isoformat() == "2024-01-31T00:00:00+00:00"


def test_cal_live_mode_fails_loudly_when_nothing_could_be_read(
    calendar_env, composio, data
):
    calendar_env.setenv("CALENDAR_SOURCE", "live")
    calendar_env.setenv("CALENDAR_ACCOUNTS", data.ACCOUNT)
    calendar_env.setenv("COMPOSIO_BASE_URL", composio.base_url)
    calendar_env.setenv("COMPOSIO_API_KEY", "invented-wrong-key-canary")
    live = Calendar()
    live.fetch_options = {"opener": direct(), "now": NOW}
    with pytest.raises(RuntimeError, match="every calendar"):
        list(live.records({}, "sync"))


def test_cal_invalid_endpoint_url_never_leaks_the_embedded_token(composio, data):
    # urllib quotes a malformed URL in its error; an MCP URL embeds its token.
    run, _ = fetcher(composio, data, transport="mcp")
    run.secrets["mcp_url"] = "not-a-url-" + data.CANARY_MCP
    records = list(run.run({}))
    end = records[-1]
    assert end["complete"] is False and end["errors"][0]["step"] == "calendars"
    assert "not valid" in end["errors"][0]["error"]
    assert data.CANARY_MCP not in json.dumps(records)
    caller = fetch.Caller(fetch.Budget(5, 5, {}, "2024-03-01"), min_interval=0)
    with pytest.raises(fetch.FetchError) as error:
        caller.post("bad url " + data.CANARY_MCP, {}, {}, "MCP tools/call")
    assert data.CANARY_MCP not in str(error.value)


def test_cal_page_token_on_a_wrapper_level_keeps_paging():
    nested = {
        "data": {"response_data": {"items": [{"id": "a"}]}, "nextPageToken": "t2"}
    }
    assert fetch.page_of(nested) == ([{"id": "a"}], "t2")
    inner = {"data": {"items": [{"id": "a"}], "nextPageToken": "inner"}}
    assert fetch.page_of(inner) == ([{"id": "a"}], "inner")


def test_cal_failed_live_run_still_counts_its_calls(calendar_env, composio, data):
    calendar_env.setenv("CALENDAR_SOURCE", "live")
    calendar_env.setenv("CALENDAR_ACCOUNTS", data.ACCOUNT)
    calendar_env.setenv("COMPOSIO_BASE_URL", composio.base_url)
    calendar_env.setenv("COMPOSIO_API_KEY", "invented-wrong-key-canary")
    live = Calendar()
    live.fetch_options = {"opener": direct(), "now": NOW}
    seen = []
    with pytest.raises(RuntimeError, match="every calendar"):
        for item in live.records(
            {"fetch": {"day": "2024-03-01", "day_calls": 7}}, "sync"
        ):
            seen.append(item)
    checkpoints = [i for i in seen if isinstance(i, Checkpoint)]
    assert len(checkpoints) == 1
    spent = checkpoints[0].cursor["fetch"]
    assert spent["day"] == "2024-03-01" and spent["day_calls"] == 8
    assert "changes" not in spent and "last_run" not in checkpoints[0].cursor
