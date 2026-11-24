import json
import shutil
from pathlib import Path

import pytest
from throughline_sdk import Checkpoint, Record
from throughline_sdk.contract import check_plugin

from throughline_claude_code import ClaudeCode
from throughline_claude_code.scrub import scrub_text

RECORDED = Path(__file__).parent / "recorded" / "home"
CANARY = "sk-ant-api03-INVENTEDcanaryKEY0000000000000000"
S1 = "00000000-0000-4000-8000-00000000a001"
S2 = "00000000-0000-4000-8000-00000000b002"
ORCHARD_FILE = Path("projects") / "-home-invented-orchard" / f"{S1}.jsonl"
FENCE_FILE = Path("projects") / "-home-invented-fence" / f"{S2}.jsonl"


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "claude"
    shutil.copytree(RECORDED, root)
    monkeypatch.setenv("CLAUDE_CODE_HOMES", f"test={root}")
    for key in (
        "CLAUDE_CODE_ENTRYPOINTS",
        "CLAUDE_CODE_SUBAGENTS",
        "CLAUDE_CODE_THINKING",
        "CLAUDE_CODE_SCRUB",
        "CLAUDE_CODE_BATCH_SIZE",
        "CLAUDE_CONFIG_DIR",
    ):
        monkeypatch.delenv(key, raising=False)
    return root


def run(cursor=None):
    items = list(ClaudeCode().records(cursor or {}, "sync"))
    rows = [i for i in items if isinstance(i, dict)]
    checkpoints = [i for i in items if isinstance(i, Checkpoint)]
    return rows, checkpoints[-1].cursor if checkpoints else cursor, items


def by_id(rows, channel="claude_code.messages"):
    return {r["external_id"].rsplit(":", 1)[-1] if r["channel"] == channel else r["external_id"]: r
            for r in rows if r["channel"] == channel}


def uid(n):
    return f"10000000-0000-4000-8000-{n:012d}"


def append(path, *lines):
    with open(path, "a") as handle:
        for line in lines:
            handle.write(json.dumps(line) + "\n")


def test_contract_kit():
    result = check_plugin(
        "throughline_claude_code:ClaudeCode",
        {"CLAUDE_CODE_HOMES": f"test={RECORDED}", "CLAUDE_CODE_BATCH_SIZE": "3"},
        secrets=[CANARY],
    )
    assert result["messages"] >= 20


def test_roles_authors_and_skipped_lines(home):
    rows, _, _ = run()
    m = {r["external_id"]: r for r in rows if r["channel"] == "claude_code.messages"}
    k = lambda n: f"{S1}:{uid(n)}"  # noqa: E731
    assert m[k(1)]["fields"]["author_kind"] == "owner"
    assert m[k(1)]["author"] == {"system": "claude_code", "kind": "owner", "value": "owner", "display_name": None}
    assert m[k(4)]["fields"]["author_kind"] == "agent"
    assert m[k(4)]["fields"]["model"] == "claude-invented-1"
    assert m[k(14)]["content"] == "/review orchard" and m[k(14)]["fields"]["role"] == "command"
    assert m[k(17)]["content"] == "! git status" and m[k(17)]["fields"]["role"] == "shell"
    assert m[k(22)]["fields"]["image_count"] == 1
    assert m[k(26)]["fields"]["role"] == "compact_summary"
    assert m[k(26)]["fields"]["author_kind"] == "agent"
    structured = m[f"{S1}/agent-b0000000000000002:{uid(203)}"]
    assert structured["fields"]["role"] == "structured_output"
    assert json.loads(structured["content"]) == {"errors": 0}
    # Tool results, command output, meta lines, notifications, interrupts,
    # synthetic API errors, attachments and thinking (off by default) are skipped.
    for n in (2, 3, 5, 6, 8, 15, 16, 18, 21, 24, 25):
        assert k(n) not in m, n
    # The partial last line (no newline) is left for the next run.
    assert k(29) not in m
    sub = m[f"{S1}/agent-a0000000000000001:{uid(101)}"]
    assert sub["fields"]["author_kind"] == "agent_prompt"
    assert sub["fields"]["is_subagent"] is True
    assert sub["thread"]["title"] == "Count invented trees"
    assert sub["thread"]["metadata"]["agent_type"] == "general-purpose"
    assert m[k(1)]["thread"]["metadata"]["project_dir"] == "/home/invented/orchard"


def test_tool_calls_fold_onto_the_next_reply(home):
    rows, _, _ = run()
    m = {r["external_id"]: r for r in rows}
    reply = m[f"{S1}:{uid(11)}"]["fields"]
    assert reply["tool_calls"] == 3
    assert reply["tools"] == {"Read": 1, "Bash": 1, "Edit": 1}
    assert reply["files"] == ["/home/invented/orchard/plan.md"]
    assert reply["tool_errors"] == 1
    assert m[f"{S1}:{uid(4)}"]["fields"]["tool_calls"] == 0
    # Tool calls with no reply after them (the turn was interrupted).
    trailing = m[f"{S1}:{uid(20)}:tools"]
    assert trailing["fields"]["role"] == "tool_calls"
    assert trailing["fields"]["tools"] == {"Agent": 1, "Write": 1}
    assert "Agent ×1" in trailing["content"]
    assert not any("invented output" in json.dumps(r) for r in rows)  # No tool output.


def test_secrets_are_scrubbed_everywhere(home):
    rows, _, _ = run()
    assert CANARY not in json.dumps(rows)
    owner = {r["external_id"]: r for r in rows}[f"{S1}:{uid(12)}"]
    assert "[REDACTED:anthropic_key]" in owner["content"]
    assert owner["fields"]["redactions"] == 1


def test_scrub_can_be_turned_off(home, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SCRUB", "false")
    rows, _, _ = run()
    assert CANARY in json.dumps(rows)


def test_session_records(home):
    rows, _, _ = run()
    sessions = {r["external_id"]: r for r in rows if r["channel"] == "claude_code.sessions"}
    orchard = sessions[S1]
    assert orchard["fields"]["title"] == "Orchard planning"  # custom title beats AI title
    assert orchard["fields"]["owner_messages"] == 6
    assert orchard["fields"]["tool_calls"] == 5
    assert orchard["fields"]["entrypoint"] == "cli"
    assert orchard["happened_at"] == "2026-01-02T09:00:00+00:00"
    assert orchard["ended_at"] == "2026-01-02T10:01:30+00:00"
    assert sessions[S2]["fields"]["entrypoint"] == "sdk-ts"
    assert sessions[f"{S1}/agent-b0000000000000002"]["fields"]["workflow_run"] == "wf_invented-1"
    assert sessions[f"{S1}/agent-b0000000000000002"]["fields"]["title"] == "Review the invented plan for errors."


def test_incremental_append_and_partial_line(home):
    rows, cursor, _ = run()
    again, cursor2, items = run(cursor)
    assert again == [] and items == []  # Idle: not even a checkpoint.
    path = home / ORCHARD_FILE
    text = path.read_text()
    path.write_text(text[: text.rfind("\n") + 1])  # Drop the partial line.
    append(
        path,
        {"type": "user", "uuid": uid(30), "timestamp": "2026-01-02T11:00:00Z", "sessionId": S1,
         "cwd": "/home/invented/orchard", "entrypoint": "cli", "message": {"role": "user", "content": "One more thing."}},
    )
    rows, _, _ = run(cursor2)
    ids = {r["external_id"] for r in rows}
    assert ids == {f"{S1}:{uid(30)}", S1}  # The new prompt and the refreshed session.


def test_rewritten_file_is_read_again(home):
    _, cursor, _ = run()
    path = home / FENCE_FILE
    lines = path.read_text().splitlines(keepends=True)
    path.write_text("".join(lines[1:]))  # A different first line.
    rows, _, _ = run(cursor)
    assert {r["external_id"].rsplit(":", 1)[-1] for r in rows if r["channel"] == "claude_code.messages"} == {
        uid(304), uid(305), uid(306)
    }


def test_deleted_files_leave_the_cursor(home):
    _, cursor, _ = run()
    (home / FENCE_FILE).unlink()
    _, cursor, _ = run(cursor)
    assert not any(k.endswith(FENCE_FILE.name) for k in cursor["files"])


def test_entrypoint_filter(home, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINTS", "cli")
    rows, _, _ = run()
    assert not any(r["fields"].get("session_id") == S2 for r in rows)
    assert any(r["fields"].get("session_id") == S1 for r in rows)


def test_thinking_is_opt_in(home, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_THINKING", "true")
    rows, _, _ = run()
    thinking = [r for r in rows if r["fields"].get("role") == "thinking"]
    assert [t["content"] for t in thinking] == ["Invented thought."]
    assert thinking[0]["fields"]["author_kind"] == "agent"


def test_resume_from_any_checkpoint_matches_one_run(home, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_BATCH_SIZE", "2")
    full, _, items = run()
    final = {(r["channel"], r["external_id"]): r for r in full}
    checkpoints = [n for n, i in enumerate(items) if isinstance(i, Checkpoint)]
    assert len(checkpoints) > 5
    for index in checkpoints:
        before = [i for i in items[:index] if isinstance(i, dict)]
        resumed, _, _ = run(json.loads(json.dumps(items[index].cursor)))
        merged = {(r["channel"], r["external_id"]): r for r in before}
        merged.update({(r["channel"], r["external_id"]): r for r in resumed})
        assert merged == final, index


def test_unreadable_homes(tmp_path, monkeypatch, home):
    monkeypatch.setenv("CLAUDE_CODE_HOMES", f"gone={tmp_path / 'missing'}")
    with pytest.raises(Exception, match="no Claude Code home"):
        run()
    monkeypatch.setenv("CLAUDE_CODE_HOMES", f"test={home},gone={tmp_path / 'missing'}")
    _, _, items = run()
    partial = [i for i in items if isinstance(i, Record) and i.kind == "coverage"]
    assert {p.data["scope_key"] for p in partial} == {"gone"}
    assert {p.data["status"] for p in partial} == {"partial"}


def test_scrub_patterns():
    samples = {
        "github_token": "ghp_" + "A" * 36,
        "aws_access_key": "AKIA" + "B" * 16,
        "openai_key": "sk-proj-" + "c" * 30,
        "slack_token": "xoxb-1234567890-abcdefghij",
        "private_key": "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----",
        "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.abcdefghijklmnop",
    }
    for kind, secret in samples.items():
        text, count = scrub_text(f"before {secret} after")
        assert text == f"before [REDACTED:{kind}] after" and count == 1, kind
    text, count = scrub_text("export API_KEY=invented-value-123 and postgres://bob:hunter2pw@db.example.com/x")
    assert "invented-value-123" not in text and "hunter2pw" not in text and count == 2
    assert scrub_text("max_tokens=4096, token_count: 12") == ("max_tokens=4096, token_count: 12", 0)
    assert scrub_text("Authorization: Bearer abcdefghijklmnopqrstuvwxyz")[0] == (
        "Authorization: Bearer [REDACTED:bearer]"
    )
