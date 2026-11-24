"""fetch.py: the standard-library Composio fetcher, against a local fake."""

import ast
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from email_fakes import API_KEY, MCP_PATH, MCP_TOKEN, gmail_message, live_env
from throughline_email import fetch

FETCH = Path(fetch.__file__)


def config(fake, **extra):
    return fetch.Config.from_env(live_env(fake, **extra))


def drain(fetcher):
    records, checkpoints = [], []
    for event, item in fetcher.run():
        (records if event == "record" else checkpoints).append(item)
    return records, checkpoints


def ids(records, kind="gmail.message"):
    return [(r["account"], r["id"]) for r in records if r["kind"] == kind]


def test_fetch_is_standard_library_only():
    tree = ast.parse(FETCH.read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            modules.add((node.module or "").split(".")[0])
    assert modules <= set(sys.stdlib_module_names) | {"__future__"}


def test_backfill_pages_all_accounts_newest_first(composio):
    fetcher = fetch.Fetcher(config(composio))
    records, checkpoints = drain(fetcher)
    assert ids(records, "gmail.profile") == [
        ("alpha@example.invalid", "alpha@example.invalid"),
        ("beta@example.invalid", "beta@example.invalid"),
    ]
    # Spam is excluded by the query; newest first per account.
    assert ids(records) == [
        ("alpha@example.invalid", "a4"),
        ("alpha@example.invalid", "a3"),
        ("alpha@example.invalid", "a2"),
        ("alpha@example.invalid", "a1"),
        ("beta@example.invalid", "b3"),
        ("beta@example.invalid", "b2"),
        ("beta@example.invalid", "b1"),
    ]
    message = next(r for r in records if r["id"] == "a3")
    assert message["labels"] == {"Label_7": "Food"}
    assert message["account_ref"] == "ca_alpha" and message["thread_id"] == "tb"
    assert message["raw"]["payload"]["mimeType"] == "multipart/mixed"
    lists = [c for c in composio.calls if c[1] == "GMAIL_FETCH_EMAILS"]
    assert all(c[3]["max_results"] == 2 and c[3]["include_payload"] for c in lists)
    assert [c[3].get("page_token") for c in lists if c[2] == "ca_alpha"] == [None, "p2"]
    state = checkpoints[-1]
    alpha = state["accounts"]["ca_alpha"]
    assert alpha["backfilled"] and alpha["history_id"] == "500"
    assert alpha["max_ms"] == int(
        datetime(2021, 3, 3, 12, tzinfo=UTC).timestamp() * 1000
    )
    assert "listing" not in alpha
    assert state["calls"]["count"] == fetcher.run_calls == len(composio.calls)
    # A page is a checkpoint: 2 profile + 3 alpha pages... at least one per page.
    assert len(checkpoints) >= 6
    assert API_KEY not in json.dumps([records, checkpoints])


def test_backfill_bounds_and_cap(composio):
    cfg = config(
        composio,
        EMAIL_SINCE="2021-03-02",
        EMAIL_UNTIL="2021-03-03",
        EMAIL_QUERY="-category:promotions",
        EMAIL_ACCOUNTS="ca_alpha",
    )
    records, _ = drain(fetch.Fetcher(cfg))
    assert ids(records) == [
        ("alpha@example.invalid", "a4"),
        ("alpha@example.invalid", "a3"),
    ]
    query = next(c[3]["query"] for c in composio.calls if c[1] == "GMAIL_FETCH_EMAILS")
    since = int(datetime(2021, 3, 2, tzinfo=UTC).timestamp())
    until = int(datetime(2021, 3, 4, tzinfo=UTC).timestamp())
    assert query == f"-category:promotions after:{since} before:{until}"
    composio.calls.clear()
    records, _ = drain(
        fetch.Fetcher(
            config(composio, EMAIL_MAX_MESSAGES="3", EMAIL_ACCOUNTS="ca_alpha")
        )
    )
    assert ids(records) == [("alpha@example.invalid", x) for x in ("a4", "a3", "a2")]
    sizes = [
        c[3]["max_results"] for c in composio.calls if c[1] == "GMAIL_FETCH_EMAILS"
    ]
    assert sizes == [2, 1]


def test_incremental_history_new_message_label_change_and_deletion(composio):
    first = fetch.Fetcher(config(composio))
    _, checkpoints = drain(first)
    state = checkpoints[-1]
    composio.calls.clear()
    # No change: one history call per account, nothing else.
    records, checkpoints = drain(fetch.Fetcher(config(composio), state))
    assert records == []
    assert composio.tools() == ["GMAIL_LIST_HISTORY", "GMAIL_LIST_HISTORY"]
    state = checkpoints[-1]
    composio.add(
        "ca_alpha",
        gmail_message(
            "a6", "ta", "2021-03-06T09:00:00+00:00", "Robin Example <robin@example.invalid>",
            "alpha@example.invalid", "Re: Garden plans", plain="Bring gloves.",
        ),
    )  # fmt: skip
    composio.relabel("ca_alpha", "a1", ["INBOX", "STARRED"])
    composio.delete("ca_beta", "b3")
    composio.calls.clear()
    records, checkpoints = drain(fetch.Fetcher(config(composio), state))
    assert ids(records) == [
        ("alpha@example.invalid", "a6"),
        ("alpha@example.invalid", "a1"),
    ]
    assert next(r for r in records if r["id"] == "a1")["raw"]["labelIds"] == [
        "INBOX",
        "STARRED",
    ]
    assert ids(records, "gmail.deleted") == [("beta@example.invalid", "b3")]
    assert "GMAIL_FETCH_EMAILS" not in composio.tools()
    state = checkpoints[-1]
    assert state["accounts"]["ca_alpha"]["history_id"] == "502"
    assert state["accounts"]["ca_beta"]["history_id"] == "501"
    assert "pending" not in state["accounts"]["ca_alpha"]
    records, _ = drain(fetch.Fetcher(config(composio), state))
    assert records == []


def test_expired_history_falls_back_to_date_query(composio):
    _, checkpoints = drain(fetch.Fetcher(config(composio, EMAIL_ACCOUNTS="ca_alpha")))
    state = checkpoints[-1]
    composio.add(
        "ca_alpha",
        gmail_message(
            "a6", "te", "2021-03-03T18:00:00+00:00", "x@example.invalid",
            "alpha@example.invalid", "Late", plain="Late one.",
        ),
    )  # fmt: skip
    composio.expired.add("ca_alpha")
    composio.calls.clear()
    cfg = config(composio, EMAIL_ACCOUNTS="ca_alpha", EMAIL_OVERLAP_S="3600")
    records, checkpoints = drain(fetch.Fetcher(cfg, state))
    # Newest seen was a4 at 12:00; the overlap re-reads from 11:00.
    assert ids(records) == [
        ("alpha@example.invalid", "a6"),
        ("alpha@example.invalid", "a4"),
    ]
    query = next(c[3]["query"] for c in composio.calls if c[1] == "GMAIL_FETCH_EMAILS")
    assert query == f"after:{int(datetime(2021, 3, 3, 11, tzinfo=UTC).timestamp())}"
    alpha = checkpoints[-1]["accounts"]["ca_alpha"]
    assert alpha["history_id"] == "501" and "listing" not in alpha
    composio.expired.clear()
    composio.calls.clear()
    assert drain(fetch.Fetcher(cfg, checkpoints[-1]))[0] == []
    assert composio.tools() == ["GMAIL_LIST_HISTORY"]
    # An explicit history mode refuses to guess.
    composio.expired.add("ca_alpha")
    with pytest.raises(fetch.FetchError, match="expired"):
        drain(
            fetch.Fetcher(
                config(
                    composio, EMAIL_ACCOUNTS="ca_alpha", EMAIL_INCREMENTAL="history"
                ),
                checkpoints[-1],
            )
        )


def test_date_incremental_mode(composio):
    cfg = config(
        composio,
        EMAIL_ACCOUNTS="ca_beta",
        EMAIL_INCREMENTAL="date",
        EMAIL_OVERLAP_S="0",
    )
    _, checkpoints = drain(fetch.Fetcher(cfg))
    composio.calls.clear()
    records, _ = drain(fetch.Fetcher(cfg, checkpoints[-1]))
    assert ids(records) == [("beta@example.invalid", "b3")]  # the boundary message
    assert composio.tools() == ["GMAIL_FETCH_EMAILS", "GMAIL_LIST_LABELS"]


def test_date_fallback_refuses_a_different_mailbox(composio):
    cfg = config(composio, EMAIL_ACCOUNTS="ca_alpha")
    _, checkpoints = drain(fetch.Fetcher(cfg))
    state = checkpoints[-1]
    state["accounts"]["ca_alpha"].pop("history_id")
    state["accounts"]["ca_alpha"]["address"] = "other@example.invalid"
    with pytest.raises(fetch.FetchError, match="identity"):
        drain(fetch.Fetcher(cfg, state))


def test_per_run_limit_stops_and_resumes_without_loss(composio):
    cfg = config(composio, EMAIL_MAX_CALLS_PER_RUN="3")
    seen, state, runs = [], None, 0
    while True:
        fetcher = fetch.Fetcher(cfg, state)
        records, checkpoints = drain(fetcher)
        seen += ids(records)
        state = checkpoints[-1]
        runs += 1
        assert fetcher.run_calls <= 3
        if fetcher.stopped is None:
            break
        assert "per-run" in fetcher.stopped
        assert runs < 20
    expected = ids(drain(fetch.Fetcher(config(composio)))[0])
    assert sorted(set(seen)) == sorted(expected)
    assert all(state["accounts"][a]["backfilled"] for a in ("ca_alpha", "ca_beta"))


def test_per_day_limit_resets_next_day(composio):
    day = [datetime(2026, 1, 10, 12, tzinfo=UTC)]
    cfg = config(composio, EMAIL_MAX_CALLS_PER_DAY="4")
    fetcher = fetch.Fetcher(cfg, now=lambda: day[0])
    _, checkpoints = drain(fetcher)
    assert fetcher.run_calls == 4 and "per-day" in fetcher.stopped
    again = fetch.Fetcher(cfg, checkpoints[-1], now=lambda: day[0])
    assert drain(again)[0] == [] and again.run_calls == 0
    day[0] = datetime(2026, 1, 11, 0, 1, tzinfo=UTC)
    later = fetch.Fetcher(cfg, checkpoints[-1], now=lambda: day[0])
    drain(later)
    assert later.run_calls == 4
    assert later.state["calls"] == {"day": "2026-01-11", "count": 4}


def test_retries_429_and_5xx_then_fails_cleanly(composio):
    slept = []
    composio.failures += [
        ("GMAIL_GET_PROFILE", 429, [("Retry-After", "7")], {"error": "slow down"}),
        ("GMAIL_GET_PROFILE", 503, [], {"error": "unavailable"}),
    ]
    cfg = config(composio, EMAIL_ACCOUNTS="ca_beta", EMAIL_RETRY_BASE_S="0.5")
    records, _ = drain(fetch.Fetcher(cfg, sleep=slept.append))
    assert ids(records, "gmail.profile") == [
        ("beta@example.invalid", "beta@example.invalid")
    ]
    assert slept == [7, 1.0]  # Retry-After honoured, then doubled backoff
    composio.failures += [("*", 500, [], {"error": "boom"})] * 3
    cfg = config(composio, EMAIL_ACCOUNTS="ca_beta", EMAIL_MAX_RETRIES="2")
    with pytest.raises(fetch.FetchError, match=r"after retries \(HTTP 500\)"):
        drain(fetch.Fetcher(cfg, sleep=lambda s: None))


def test_client_errors_are_not_retried_and_never_leak_the_key(composio):
    cfg = fetch.Config.from_env(live_env(composio, COMPOSIO_API_KEY="wrong-key-canary"))
    with pytest.raises(fetch.FetchError) as error:
        drain(fetch.Fetcher(cfg, sleep=lambda s: pytest.fail("retried")))
    assert "HTTP 401" in str(error.value) and "invalid api key" in str(error.value)
    assert "wrong-key-canary" not in str(error.value)
    composio.failures.append(
        ("GMAIL_GET_PROFILE", 400, [], {"error": f"echo {API_KEY}"})
    )
    with pytest.raises(fetch.FetchError) as error:
        drain(fetch.Fetcher(config(composio)))
    assert API_KEY not in str(error.value) and "[REDACTED]" in str(error.value)


def test_redirects_are_refused(composio):
    composio.failures.append(
        ("GMAIL_GET_PROFILE", 302, [("Location", "http://127.0.0.1:9/steal")], {})
    )
    with pytest.raises(fetch.FetchError, match="HTTP 302"):
        drain(fetch.Fetcher(config(composio)))


def test_tool_failure_for_unknown_account(composio):
    with pytest.raises(fetch.ToolError, match="not found"):
        drain(fetch.Fetcher(config(composio, EMAIL_ACCOUNTS="ca_missing")))


def test_mcp_transport_pages_at_two_and_marks_truncation(composio):
    env = live_env(
        composio,
        EMAIL_TRANSPORT="mcp",
        COMPOSIO_MCP_URL=composio.url + MCP_PATH,
        COMPOSIO_MCP_TOKEN=MCP_TOKEN,
        EMAIL_PAGE_SIZE="25",
        EMAIL_ACCOUNTS="ca_alpha",
    )
    del env["COMPOSIO_API_KEY"]
    records, checkpoints = drain(fetch.Fetcher(fetch.Config.from_env(env)))
    assert ids(records) == [
        ("alpha@example.invalid", x) for x in ("a4", "a3", "a2", "a1")
    ]
    lists = [c[3] for c in composio.calls if c[1] == "GMAIL_FETCH_EMAILS"]
    assert all(c["max_results"] == 2 and not c["include_payload"] for c in lists)
    assert all(c[0] == "mcp" for c in composio.calls)
    a3 = next(r for r in records if r["id"] == "a3")
    # Three labels came back as a two-item preview: kept, and flagged.
    assert a3["truncated"] is True and a3["raw"]["labelIds"] == ["INBOX", "UNREAD"]
    assert a3["raw"]["sender"].endswith("<owner@cafe.example.invalid>")
    assert "truncated" not in next(r for r in records if r["id"] == "a2")
    serialized = json.dumps([records, checkpoints])
    assert MCP_TOKEN not in serialized and MCP_PATH not in serialized


def test_config_validation():
    with pytest.raises(ValueError, match="EMAIL_TRANSPORT"):
        fetch.Config.from_env({"EMAIL_TRANSPORT": "imap"})
    with pytest.raises(ValueError, match="offset"):
        fetch.Config.from_env({"EMAIL_SINCE": "2021-01-01T00:00:00"})
    with pytest.raises(ValueError, match="COMPOSIO_API_KEY"):
        fetch.Fetcher(fetch.Config.from_env({"EMAIL_ACCOUNTS": "ca_x"}))
    with pytest.raises(ValueError, match="EMAIL_ACCOUNTS"):
        fetch.Fetcher(fetch.Config.from_env({"COMPOSIO_API_KEY": "k"}))
    with pytest.raises(ValueError, match="COMPOSIO_BASE_URL"):
        fetch.Config.from_env({"COMPOSIO_BASE_URL": "https://user:pw@example.invalid"})
    assert (
        fetch.Config.from_env(
            {"EMAIL_TRANSPORT": "mcp", "EMAIL_PAGE_SIZE": "50"}
        ).page_size
        == 2
    )


def run_cli(args, env, cwd):
    return subprocess.run(
        [sys.executable, "-I", str(FETCH), *args],
        env={
            **{
                k: v
                for k, v in os.environ.items()
                if k in ("SYSTEMROOT", "PATH", "TEMP", "TMP")
            },
            **env,
        },
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_cli_export_writes_jsonl_and_resumes_with_state(composio, tmp_path):
    env = live_env(composio)
    out, state = tmp_path / "export-1.jsonl", tmp_path / "state.json"
    result = run_cli(
        ["export", "--out", str(out), "--state", str(state)], env, tmp_path
    )
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["counts"] == {"gmail.message": 7, "gmail.profile": 2}
    assert summary["stopped"] is None
    lines = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["kind"] == "export.meta"
    assert {line["kind"] for line in lines[1:]} == {"gmail.message", "gmail.profile"}
    for text in (
        out.read_text(encoding="utf-8"),
        state.read_text(encoding="utf-8"),
        result.stdout,
        result.stderr,
    ):
        assert API_KEY not in text
    # An existing output is never overwritten silently.
    again = run_cli(["export", "--out", str(out), "--state", str(state)], env, tmp_path)
    assert again.returncode == 1 and "--append" in again.stderr
    composio.add(
        "ca_beta",
        gmail_message(
            "b4", "tz", "2021-03-07T08:00:00+00:00", "sam@example.invalid",
            "beta@example.invalid", "Hello", plain="Invented hello.",
        ),
    )  # fmt: skip
    second = tmp_path / "export-2.jsonl"
    result = run_cli(
        ["export", "--out", str(second), "--state", str(state)], env, tmp_path
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["counts"] == {"gmail.message": 1}
    record = json.loads(second.read_text(encoding="utf-8").splitlines()[1])
    assert (record["account"], record["id"]) == ("beta@example.invalid", "b4")


def test_cli_reads_credentials_only_from_env(composio, tmp_path):
    env = live_env(composio)
    del env["COMPOSIO_API_KEY"]
    result = run_cli(["export", "--out", str(tmp_path / "x.jsonl")], env, tmp_path)
    assert result.returncode == 1 and "COMPOSIO_API_KEY" in result.stderr
    help_text = run_cli(["export", "--help"], {}, tmp_path).stdout
    assert "--api-key" not in help_text and "--account" in help_text


def test_cli_limit_stop_reports_and_keeps_state(composio, tmp_path):
    env = live_env(composio, EMAIL_MAX_CALLS_PER_RUN="3")  # profile, list, labels
    out, state = tmp_path / "e.jsonl", tmp_path / "s.json"
    result = run_cli(
        ["export", "--out", str(out), "--state", str(state)], env, tmp_path
    )
    assert result.returncode == 0
    assert "per-run" in json.loads(result.stdout)["stopped"]
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["accounts"]["ca_alpha"]["listing"]["page_token"] == "p2"
    result = run_cli(
        [
            "export",
            "--out",
            str(out),
            "--state",
            str(state),
            "--append",
            "--max-calls-per-run",
            "100",
        ],
        env,
        tmp_path,
    )
    assert result.returncode == 0 and json.loads(result.stdout)["stopped"] is None
    kinds = [
        json.loads(line)["kind"]
        for line in out.read_text(encoding="utf-8").splitlines()
    ]
    assert kinds.count("gmail.message") == 7


def test_not_found_never_matches_connection_errors():
    gone = [
        "Composio GMAIL_LIST_HISTORY failed: Requested entity was not found.",
        "Composio GMAIL_LIST_HISTORY failed (HTTP 404): startHistoryId too old",
        "Composio GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID failed (HTTP 410): gone",
    ]
    connection = [
        "Composio GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID failed: Connected account not found",
        "Composio GMAIL_LIST_HISTORY failed: OAuth access token expired",
        "Composio GMAIL_LIST_HISTORY failed (HTTP 404): Toolkit not found",
        "Composio GMAIL_LIST_HISTORY failed: unknown tool GMAIL_X (not found)",
    ]
    assert all(fetch._not_found(text) for text in gone)
    assert not any(fetch._not_found(text) for text in connection)


def test_connection_error_on_changed_message_fails_and_keeps_it_pending(composio):
    _, checkpoints = drain(fetch.Fetcher(config(composio, EMAIL_ACCOUNTS="ca_alpha")))
    composio.relabel("ca_alpha", "a1", ["INBOX", "STARRED"])
    composio.failures.append(
        (
            "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID",
            200,
            [],
            {"successful": False, "error": "OAuth access token expired"},
        )
    )
    cfg = config(composio, EMAIL_ACCOUNTS="ca_alpha")
    saved = []
    fetcher = fetch.Fetcher(cfg, checkpoints[-1])
    with pytest.raises(fetch.ToolError, match="expired"):
        for event, item in fetcher.run():
            if event == "checkpoint":
                saved.append(item)
    # Never read as "message gone": the change stays queued for the next run.
    assert saved[-1]["accounts"]["ca_alpha"]["pending"] == ["a1"]
    records, _ = drain(fetch.Fetcher(cfg, saved[-1]))
    assert ids(records) == [("alpha@example.invalid", "a1")]


def test_rest_http_404_with_detail_falls_back_and_bare_404_fails(composio):
    _, checkpoints = drain(fetch.Fetcher(config(composio, EMAIL_ACCOUNTS="ca_alpha")))
    state = checkpoints[-1]
    cfg = config(composio, EMAIL_ACCOUNTS="ca_alpha")
    composio.failures.append(
        (
            "GMAIL_LIST_HISTORY",
            404,
            [],
            {"error": {"message": "Requested entity was not found."}},
        )
    )
    composio.calls.clear()
    _, after = drain(fetch.Fetcher(cfg, state))
    assert "GMAIL_FETCH_EMAILS" in composio.tools()  # the date fallback ran
    alpha = after[-1]["accounts"]["ca_alpha"]
    assert alpha["history_id"] == "500" and "listing" not in alpha
    # A 404 without a cause (wrong base URL, say) is fatal, never "expired".
    composio.failures.append(("GMAIL_LIST_HISTORY", 404, [], "<html>nope</html>"))
    with pytest.raises(fetch.FetchError, match="HTTP 404") as error:
        drain(fetch.Fetcher(cfg, after[-1]))
    assert not isinstance(error.value, fetch.ToolError)


def test_retry_after_http_date(composio):
    now = datetime(2021, 3, 10, 12, 0, 0, tzinfo=UTC)
    composio.failures.append(
        (
            "GMAIL_GET_PROFILE",
            429,
            [("Retry-After", "Wed, 10 Mar 2021 12:00:09 GMT")],
            {"error": "slow down"},
        )
    )
    slept = []
    cfg = config(composio, EMAIL_ACCOUNTS="ca_beta")
    drain(fetch.Fetcher(cfg, sleep=slept.append, now=lambda: now))
    assert slept == [9.0]


def test_fetched_at_is_fixed_width():
    whole = fetch.iso(datetime(2021, 3, 10, 12, 0, 0, tzinfo=UTC))
    part = fetch.iso(datetime(2021, 3, 10, 12, 0, 0, 5, tzinfo=UTC))
    assert whole == "2021-03-10T12:00:00.000000Z" and whole < part
