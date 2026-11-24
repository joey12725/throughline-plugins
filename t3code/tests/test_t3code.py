import json
from datetime import UTC, datetime

from throughline_sdk import Checkpoint
from throughline_sdk.contract import check_plugin

from throughline_t3code import T3Code, fetch

from t3code_fake import EMAIL_CANARY, Home

WEEK_END = datetime(2026, 1, 5, 5, 0, tzinfo=UTC)  # A Monday.
RESET_1 = int(WEEK_END.timestamp())
RESET_2 = RESET_1 + 7 * 86400


def run(plugin, cursor=None, now="2026-01-03T12:00:00Z"):
    plugin.now = now
    items = list(plugin.records(cursor or {}, "backfill"))
    rows = [i for i in items if isinstance(i, dict)]
    cursor = [i for i in items if isinstance(i, Checkpoint)][-1].cursor
    return rows, cursor


def channel(rows, name):
    return [r for r in rows if r["channel"] == name]


def test_contract_kit(env, home):
    home.cache("claudeAgent", label="Claude Max Subscription",
               windows=[{"id": "seven_day", "label": "Weekly", "windowDurationMins": 10080,
                         "usedPercent": 30, "resetsAt": WEEK_END.isoformat()}],
               credits={"availableCount": 1, "nextExpiresAt": "2026-06-01T00:00:00Z"})
    result = check_plugin(
        "throughline_t3code:T3Code",
        {"T3CODE_HOMES": f"test={home.root}", "T3CODE_CODEX_HOMES": "none"},
        secrets=[EMAIL_CANARY],
    )
    assert result["messages"] > 0


def test_work_records_authors_and_threads(env, home):
    rows, _ = run(T3Code())
    messages = {m["external_id"]: m for m in channel(rows, "t3code.messages")}
    assert set(messages) == {"m1", "m2", "m3", "m4", "m5"}  # m6 is still streaming
    assert messages["m1"]["fields"]["author_kind"] == "owner"
    assert messages["m1"]["author"]["kind"] == "owner"
    assert messages["m2"]["fields"]["author_kind"] == "agent"
    assert messages["m2"]["fields"]["model"] == "claude-fable-5"  # from the run
    assert messages["m3"]["fields"]["author_kind"] == "agent_prompt"
    assert messages["m3"]["fields"]["is_subagent"] is True
    assert messages["m3"]["fields"]["root_thread"] == "t1"
    assert messages["m5"]["fields"]["creation_source"] == "mobile"
    assert messages["m1"]["thread"]["title"] == "Orchard · Plan the orchard"
    assert messages["m1"]["fields"]["project"] == "Orchard"
    assert messages["m1"]["fields"]["environment_id"] == "env-0000-test"
    threads = {t["external_id"]: t for t in channel(rows, "t3code.threads")}
    assert threads["t1-sub"]["fields"]["parent_thread"] == "t1"
    project = channel(rows, "t3code.projects")[0]
    assert project["fields"]["top_level_threads"] == 2
    assert project["fields"]["all_threads"] == 3


def test_incremental_and_streaming_finished_later(env, home):
    plugin = T3Code()
    rows, cursor = run(plugin)
    home.add_message("m6", "t2", "assistant", "Fence fixed.", "2026-01-02T09:01:00Z", "agent", "provider")
    home.add_message("m7", "t2", "user", "Thanks!", "2026-01-04T09:00:00Z", "user", "web")
    rows, cursor = run(T3Code(), cursor)
    ids = {m["external_id"] for m in channel(rows, "t3code.messages")}
    assert {"m6", "m7"} <= ids
    assert "m1" not in ids  # older than the watermark minus the overlap


def test_two_homes_first_wins_no_duplicates(env, home, tmp_path):
    other = Home(tmp_path / "copy")
    env.setenv("T3CODE_HOMES", f"old={other.root},new={home.root}")
    rows, _ = run(T3Code())
    messages = channel(rows, "t3code.messages")
    assert len(messages) == len({m["external_id"] for m in messages}) == 5
    assert {m["fields"]["environment"] for m in messages} == {"old"}


def test_usage_status_dedup_burn_and_reset_at_100(env, home):
    home.claude_event("2026-01-01T00:00:00.000Z", 0.10, RESET_1)
    home.claude_event("2026-01-01T01:00:00.000Z", 0.10, RESET_1)  # unchanged
    home.claude_event("2026-01-02T00:00:00.000Z", 0.40, RESET_1)
    home.cache("claudeAgent", label="Claude Max Subscription",
               windows=[{"id": "seven_day", "label": "Weekly", "windowDurationMins": 10080,
                         "usedPercent": 55, "resetsAt": WEEK_END.isoformat()}],
               checked="2026-01-03T00:00:00Z")
    rows, cursor = run(T3Code(), now="2026-01-03T12:00:00Z")
    usage = channel(rows, "t3code.usage")
    assert [u["fields"]["used_percent"] for u in usage] == [10, 40, 55]
    last = usage[-1]["fields"]
    assert last["left_percent"] == 45
    assert last["burn_since_previous"] == 15
    assert last["subscription"] == "Claude Max"
    assert last["time_left"] == "2d 5h"
    assert "45% left" in usage[-1]["content"] and "2d 5h left" in usage[-1]["content"]
    assert not channel(rows, "t3code.resets")
    # No snapshot after the reset: the run after the reset time still records 100%.
    rows, cursor = run(T3Code(), cursor, now="2026-01-05T06:00:00Z")
    usage = channel(rows, "t3code.usage")
    assert len(usage) == 1
    reset = usage[0]
    assert reset["fields"]["event"] == "reset"
    assert reset["fields"]["left_percent"] == 100
    assert reset["happened_at"] == "2026-01-05T05:00:00Z"
    assert reset["fields"]["resets_at"] == "2026-01-12T05:00:00Z"
    resets = [r for r in channel(rows, "t3code.resets") if r["fields"]["event"] == "reset"]
    assert len(resets) == 1 and resets[0]["fields"]["used_before_percent"] == 55
    # The first snapshot after it continues the new cycle; the reset is not doubled.
    home.claude_event("2026-01-05T08:00:00.000Z", 0.05, RESET_2)
    rows, cursor = run(T3Code(), cursor, now="2026-01-05T09:00:00Z")
    usage = channel(rows, "t3code.usage")
    assert [(u["fields"]["event"], u["fields"]["used_percent"]) for u in usage] == [("observed", 5)]
    assert usage[0]["fields"]["burn_since_previous"] == 5


def test_early_reset_and_credits(env, home):
    home.cache("codex", label="ChatGPT Pro Subscription",
               windows=[{"id": "primary", "label": "Weekly", "windowDurationMins": 10080,
                         "usedPercent": 90, "resetsAt": WEEK_END.isoformat()}],
               credits={"availableCount": 1, "nextExpiresAt": "2026-03-01T00:00:00Z"},
               checked="2026-01-02T00:00:00Z")
    rows, cursor = run(T3Code(), now="2026-01-02T00:10:00Z")
    events = [r["fields"]["event"] for r in channel(rows, "t3code.resets")]
    assert events == ["credit_balance"]
    # A credit spent: the window restarts two days early, one credit fewer.
    home.cache("codex", label="ChatGPT Pro Subscription",
               windows=[{"id": "primary", "label": "Weekly", "windowDurationMins": 10080,
                         "usedPercent": 0, "resetsAt": "2026-01-10T00:00:00Z"}],
               credits={"availableCount": 0}, checked="2026-01-03T00:00:00Z")
    rows, cursor = run(T3Code(), cursor, now="2026-01-03T00:10:00Z")
    resets = {r["fields"]["event"]: r for r in channel(rows, "t3code.resets")}
    assert set(resets) == {"early_reset", "credit_used"}
    assert resets["credit_used"]["fields"]["credit_change"] == -1
    usage = channel(rows, "t3code.usage")
    assert [u["fields"]["event"] for u in usage] == ["early_reset"]
    assert usage[0]["fields"]["left_percent"] == 100
    # New credits granted.
    home.cache("codex", label="ChatGPT Pro Subscription",
               windows=[{"id": "primary", "label": "Weekly", "windowDurationMins": 10080,
                         "usedPercent": 3, "resetsAt": "2026-01-10T00:00:00Z"}],
               credits={"availableCount": 2}, checked="2026-01-04T00:00:00Z")
    rows, cursor = run(T3Code(), cursor, now="2026-01-04T00:10:00Z")
    added = [r for r in channel(rows, "t3code.resets") if r["fields"]["event"] == "credit_added"]
    assert added and added[0]["fields"]["credit_change"] == 2
    subs = channel(rows, "t3code.subscriptions")
    assert subs[0]["fields"]["available_credits"] == 2


def test_email_never_leaves_and_signed_out_cache_skipped(env, home):
    home.cache("claudeAgent", label="Claude Max Subscription", windows=[])
    home.cache("cursor", label=None, windows=[], status="unknown", email=None)
    rows, _ = run(T3Code())
    assert EMAIL_CANARY not in json.dumps(rows)
    assert [s["fields"]["instance"] for s in channel(rows, "t3code.subscriptions")] == ["claudeAgent"]


def test_export_roundtrip(env, home, tmp_path):
    home.claude_event("2026-01-01T00:00:00.000Z", 0.10, RESET_1)
    out = tmp_path / "export.jsonl"
    assert fetch.main(["export", "--out", str(out), "--home", f"test={home.root}", "--codex", "none"],
                      environ={}) == 0
    assert EMAIL_CANARY not in out.read_text()
    env.setenv("T3CODE_SOURCE", f"export:{out}")
    rows, cursor = run(T3Code())
    assert len(channel(rows, "t3code.messages")) == 5
    rows, _ = run(T3Code(), cursor)
    assert not channel(rows, "t3code.messages")  # the run was already mapped


def test_jittering_snapshots_never_double_count(env, home):
    for hour, value in enumerate((0.10, 0.40, 0.38, 0.41, 0.39, 0.45)):
        home.claude_event(f"2026-01-01T{hour:02d}:00:00.000Z", value, RESET_1)
    rows, _ = run(T3Code(), now="2026-01-01T12:00:00Z")
    burns = [u["fields"]["burn_since_previous"] for u in channel(rows, "t3code.usage")]
    assert burns == [None, 30, 0, 1, 0, 4]
    assert sum(b for b in burns if b) == 35  # 10% -> 45%: 35 points, not 41


def test_credit_reset_keeps_the_weekly_anchor(env, home):
    for hour, value in enumerate((0.80, 1.00, 0.0, 0.03)):
        home.claude_event(f"2026-01-02T{hour:02d}:00:00.000Z", value, RESET_1)
    rows, _ = run(T3Code(), now="2026-01-02T12:00:00Z")
    usage = channel(rows, "t3code.usage")
    assert [(u["fields"]["event"], u["fields"]["used_percent"]) for u in usage] == [
        ("first", 80), ("observed", 100), ("early_reset", 0), ("observed", 3)]
    assert usage[-1]["fields"]["burn_since_previous"] == 3
    early = [r for r in channel(rows, "t3code.resets") if r["fields"]["event"] == "early_reset"]
    assert early[0]["fields"]["used_before_percent"] == 100
    assert early[0]["fields"]["next_reset_at"] == "2026-01-05T05:00:00Z"


def test_import_dir_merges_other_machines_once(env, home, tmp_path):
    laptop = Home(tmp_path / "laptop")
    laptop.add_message("lap-1", "t1", "user", "From the laptop.", "2026-01-06T09:00:00Z", "user", "web")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    assert fetch.main(["export", "--out", str(inbox / "laptop.jsonl"), "--home",
                       f"laptop={laptop.root}", "--codex", "none"], environ={}) == 0
    env.setenv("T3CODE_IMPORT_DIR", str(inbox))
    rows, cursor = run(T3Code())
    by_id = {m["external_id"]: m for m in channel(rows, "t3code.messages")}
    assert by_id["lap-1"]["fields"]["environment"] == "laptop"
    rows, _ = run(T3Code(), cursor)
    assert "lap-1" not in {m["external_id"] for m in channel(rows, "t3code.messages")}


def test_missing_home_database_fails_or_reports_partial(env, home, tmp_path):
    import pytest
    from throughline_sdk import Record

    missing = tmp_path / "gone"
    (missing / "userdata").mkdir(parents=True)
    env.setenv("T3CODE_HOMES", f"gone={missing}")
    plugin = T3Code()
    plugin.now = "2026-01-03T12:00:00Z"
    items = []
    with pytest.raises(Exception, match="could not read any home"):
        for item in plugin.records({}, "backfill"):
            items.append(item)
    assert any(isinstance(i, Checkpoint) for i in items)

    env.setenv("T3CODE_HOMES", f"gone={missing},test={home.root}")
    plugin = T3Code()
    plugin.now = "2026-01-03T12:00:00Z"
    items = list(plugin.records({}, "backfill"))
    reports = [i.data for i in items if isinstance(i, Record) and i.kind == "coverage"]
    assert {r["channel"] for r in reports} == {
        "t3code.projects", "t3code.threads", "t3code.messages"}
    assert all(r["scope_key"] == "gone" and r["status"] == "partial"
               and r["reasons"] == ["source_error"] for r in reports)
    assert len(channel([i for i in items if isinstance(i, dict)], "t3code.messages")) == 5


def test_unreadable_home_database_fails_the_run(env, tmp_path):
    import pytest

    bad = tmp_path / "bad"
    (bad / "userdata").mkdir(parents=True)
    (bad / "userdata" / "statev2.sqlite").write_bytes(b"not a database" * 64)
    env.setenv("T3CODE_HOMES", f"bad={bad}")
    plugin = T3Code()
    plugin.now = "2026-01-03T12:00:00Z"
    with pytest.raises(Exception, match="bad: statev2.sqlite unreadable"):
        list(plugin.records({}, "backfill"))
