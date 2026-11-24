import json
import shutil
from pathlib import Path

import pytest
import zstandard
from throughline_sdk import Checkpoint, Record
from throughline_sdk.contract import check_plugin

from throughline_codex import Codex
from throughline_codex.mapping import injected
from throughline_codex.scrub import scrub_text

RECORDED = Path(__file__).parent / "recorded" / "home"
CANARY = "ghp_INVENTEDcanaryTOKEN000000000000000000"
S1 = "00000000-0000-7000-8000-0000000c0001"
S2 = "00000000-0000-7000-8000-0000000c0002"
S3 = "4b000000-0000-4000-8000-0000000c0003"
S4 = "00000000-0000-7000-8000-0000000c0004"
SHED_FILE = Path("sessions/2026/01/04") / f"rollout-2026-01-04T10-00-00-{S1}.jsonl"
SUB_FILE = Path("sessions/2026/01/04") / f"rollout-2026-01-04T10-05-03-{S2}.jsonl"


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "codex"
    shutil.copytree(RECORDED, root)
    monkeypatch.setenv("CODEX_HOMES", f"test={root}")
    for key in ("CODEX_ARCHIVED", "CODEX_ORIGINATORS", "CODEX_REASONING", "CODEX_SCRUB", "CODEX_BATCH_SIZE",
                "CODEX_HOME"):
        monkeypatch.delenv(key, raising=False)
    return root


def run(cursor=None):
    items = list(Codex().records(cursor or {}, "sync"))
    rows = [i for i in items if isinstance(i, dict)]
    checkpoints = [i for i in items if isinstance(i, Checkpoint)]
    return rows, checkpoints[-1].cursor if checkpoints else cursor, items


def messages(rows):
    return {r["external_id"]: r for r in rows if r["channel"] == "codex.messages"}


def test_contract_kit():
    result = check_plugin(
        "throughline_codex:Codex",
        {"CODEX_HOMES": f"test={RECORDED}", "CODEX_BATCH_SIZE": "3"},
        secrets=[CANARY],
    )
    assert result["messages"] >= 15


def test_conversation_and_skipped_items(home):
    m = messages(run()[0])
    k = lambda ident: f"{S1}:{ident}"  # noqa: E731
    assert m[k("msg_u1")]["fields"]["author_kind"] == "owner"
    assert m[k("msg_u1")]["author"]["system"] == "codex"
    assert m[k("msg_a1")]["fields"]["phase"] == "commentary"
    assert m[k("msg_a2")]["fields"]["phase"] == "final_answer"
    assert m[k("msg_a2")]["fields"]["model"] == "gpt-invented-1"
    assert m[k("msg_a3")]["fields"]["model"] == "gpt-invented-2"  # From turn_context.
    assert m[k("msg_a2")]["fields"]["reasoning_effort"] == "high"
    assert m[k("msg_u2")]["fields"]["image_count"] == 1
    assert m[k("msg_u2")]["fields"]["turn_id"] == "turn-2"
    reasoning = m[k("rs_1")]
    assert reasoning["fields"]["role"] == "reasoning" and reasoning["fields"]["author_kind"] == "agent"
    # Developer rules, injected context, shell output, tool outputs and UI
    # events (which repeat the response items) are not messages.
    for ident in ("msg_dev", "msg_env", "msg_shell", "ctco_1", "fco_1", "ctc_1", "fc_1"):
        assert k(ident) not in m, ident
    assert not any("duplicate" in r["content"] for r in m.values())
    compact = [r for r in m.values() if r["fields"]["role"] == "compact_summary"]
    assert [c["content"] for c in compact] == ["Invented summary of the shed work so far."]
    assert m[k("msg_u1")]["thread"]["title"] == "Invented garden shed"  # Latest index name.
    assert m[k("msg_u1")]["thread"]["metadata"]["originator"] == "codex_cli_rs"


def test_tool_calls_fold_onto_the_next_reply(home):
    m = messages(run()[0])
    reply = m[f"{S1}:msg_a2"]["fields"]
    assert reply["tool_calls"] == 2
    assert reply["tools"] == {"apply_patch": 1, "exec": 1}
    assert reply["files"] == ["/home/invented/shed/plan.md", "/home/invented/shed/roof.md"]
    trailing = m[f"{S1}:fc_2:tools"]
    assert trailing["fields"]["role"] == "tool_calls"
    assert trailing["fields"]["tools"] == {"collaboration.spawn_agent": 1}
    assert not any("invented tool output" in json.dumps(r) for r in m.values())


def test_subagent_and_legacy_sessions(home):
    rows = run()[0]
    m = messages(rows)
    brief = m[f"{S2}:msg_s1"]
    assert brief["fields"]["author_kind"] == "agent_prompt"
    assert brief["fields"]["is_subagent"] is True
    assert brief["fields"]["parent_session"] == S1
    note = m[f"{S2}:amsg_1"]
    assert note["fields"]["role"] == "agent_message"
    assert note["fields"]["agent_from"] == "/root/measure" and note["fields"]["agent_to"] == "/root"
    legacy = [r for r in m.values() if r["fields"]["session_id"] == S3]
    assert [r["content"] for r in legacy] == ["Mend the invented fence.", "The invented fence is mended."]
    assert legacy[0]["external_id"].startswith(f"{S3}:@")  # No item id: byte offset.
    assert legacy[1]["fields"]["tools"] == {"shell": 1}
    sessions = {r["external_id"]: r for r in rows if r["channel"] == "codex.sessions"}
    assert sessions[S3]["fields"]["git_branch"] == "main"
    assert sessions[S1]["fields"]["title"] == "Invented garden shed"
    assert sessions[S1]["fields"]["owner_messages"] == 3
    assert sessions[S1]["fields"]["reasoning_summaries"] == 1
    assert sessions[S1]["fields"]["models"] == {"gpt-invented-1": 2, "gpt-invented-2": 1}
    assert sessions[S2]["fields"]["source"] == json.dumps({"subagent": {"parent_thread_id": S1}})


def test_secrets_are_scrubbed(home):
    rows = run()[0]
    assert CANARY not in json.dumps(rows)
    prompt = messages(rows)[f"{S1}:msg_u1"]
    assert "[REDACTED:github_token]" in prompt["content"] and prompt["fields"]["redactions"] == 1


def test_incremental_partial_line_and_archive_move(home):
    _, cursor, _ = run()
    again, _, items = run(cursor)
    assert again == [] and items == []
    path = home / SHED_FILE
    text = path.read_text()
    with open(path, "w") as handle:
        handle.write(text[: text.rfind("\n") + 1])
        handle.write(json.dumps({"timestamp": "2026-01-04T10:40:00Z", "type": "response_item", "payload": {
            "type": "message", "id": "msg_u4", "role": "user",
            "content": [{"type": "input_text", "text": "Thanks."}]}}) + "\n")
    rows, cursor, _ = run(cursor)
    assert {r["external_id"] for r in rows} == {f"{S1}:msg_u4", S1}
    # Codex archives a session: the file moves, its place is kept.
    archived = home / "archived_sessions" / SHED_FILE.name
    path.rename(archived)
    rows, cursor, _ = run(cursor)
    assert rows == []
    assert f"test/{SHED_FILE.name}" in cursor["files"]


def test_compressed_rollouts(home):
    rows, cursor, _ = run()
    pond = messages(rows)
    assert pond[f"{S4}:msg_p2"]["content"] == "The invented pond is dug."
    assert pond[f"{S4}:msg_p2"]["fields"]["originator"] == "codex_vscode"
    # Codex compresses a rollout that was already read: nothing new, and
    # the stamp means the next run does not even decompress it.
    path = home / SHED_FILE
    packed = path.with_name(path.name + ".zst")
    packed.write_bytes(zstandard.ZstdCompressor().compress(path.read_bytes()))
    path.unlink()
    rows, cursor, _ = run(cursor)
    assert rows == []
    entry = cursor["files"][f"test/{SHED_FILE.name}"]
    assert entry["stamp"][0] == packed.stat().st_size
    assert run(cursor)[2] == []
    # A compressed rollout shorter than the stored offset is read again.
    lines = zstandard.ZstdDecompressor().decompress(packed.read_bytes()).decode().splitlines(keepends=True)
    packed.write_bytes(zstandard.ZstdCompressor().compress("".join(lines[:6]).encode()))
    rows, _, _ = run(cursor)
    assert f"{S1}:msg_u1" in messages(rows)


def test_rewritten_file_is_read_again(home):
    _, cursor, _ = run()
    path = home / SUB_FILE
    lines = path.read_text().splitlines(keepends=True)
    path.write_text("".join(lines[1:]))
    rows, _, _ = run(cursor)
    assert len([r for r in rows if r["channel"] == "codex.messages"]) == 3


def test_index_rename_refreshes_the_session_record(home):
    _, cursor, _ = run()
    with open(home / "session_index.jsonl", "a") as handle:
        handle.write(json.dumps({"id": S1, "thread_name": "Renamed invented shed",
                                 "updated_at": "2026-01-05T00:00:00Z"}) + "\n")
    rows, _, _ = run(cursor)
    assert [(r["channel"], r["fields"]["title"]) for r in rows] == [("codex.sessions", "Renamed invented shed")]


def test_filters(home, monkeypatch):
    monkeypatch.setenv("CODEX_ORIGINATORS", "codex_cli_rs")
    monkeypatch.setenv("CODEX_REASONING", "false")
    monkeypatch.setenv("CODEX_ARCHIVED", "false")
    rows, _, items = run()
    sessions = {r["fields"]["session_id"] for r in rows}
    assert sessions == {S1}
    assert not any(r["fields"].get("role") == "reasoning" for r in rows)


def test_resume_from_any_checkpoint_matches_one_run(home, monkeypatch):
    monkeypatch.setenv("CODEX_BATCH_SIZE", "2")
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
    monkeypatch.setenv("CODEX_HOMES", f"gone={tmp_path / 'missing'}")
    with pytest.raises(Exception, match="no Codex home"):
        run()
    monkeypatch.setenv("CODEX_HOMES", f"test={home},gone={tmp_path / 'missing'}")
    _, _, items = run()
    partial = [i for i in items if isinstance(i, Record) and i.kind == "coverage"]
    assert {p.data["scope_key"] for p in partial} == {"gone"}


def test_injected_context_detection():
    assert injected("<environment_context>\n<cwd>/x</cwd>\n</environment_context>")
    assert injected("# AGENTS.md instructions for /x\n\n...")
    assert not injected("Please fix <b>bold</b> rendering")
    assert not injected("Plain prompt")


def test_scrub_patterns():
    text, count = scrub_text("key AKIA" + "Q" * 16 + " and sk-" + "a" * 40)
    assert text == "key [REDACTED:aws_access_key] and [REDACTED:openai_key]" and count == 2


def test_large_unmapped_lines_are_not_parsed(home):
    from throughline_codex import files

    big = "x" * (files.LARGE_LINE + 10)
    path = home / SUB_FILE
    with open(path, "a") as handle:
        handle.write(json.dumps({"timestamp": "2026-01-04T10:06:00Z", "type": "event_msg",
                                 "payload": {"type": "item_completed", "item": big}}) + "\n")
        handle.write(json.dumps({"timestamp": "2026-01-04T10:06:01Z", "type": "response_item",
                                 "payload": {"type": "function_call_output", "output": big}}) + "\n")
        handle.write(json.dumps({"timestamp": "2026-01-04T10:06:02Z", "type": "response_item",
                                 "payload": {"type": "message", "id": "msg_big", "role": "assistant",
                                             "content": [{"type": "output_text", "text": big}]}}) + "\n")
    parsed = [line for _, _, line in files.lines(path, 0)]
    assert parsed[-3:-1] == [None, None]
    assert parsed[-1]["payload"]["id"] == "msg_big"
    big_reply = messages(run()[0])[f"{S2}:msg_big"]
    assert big_reply["fields"]["text_truncated"] is True
