"""Writes the invented Codex home under tests/recorded/home.

Run `python plugins/codex/tests/make_recorded.py` after changing it.
Everything here is made up: invented folders, ids and text, and a fake
secret (CANARY) that the scrubber must remove.
"""

import json
from pathlib import Path

import zstandard

ROOT = Path(__file__).parent / "recorded" / "home"
CANARY = "ghp_INVENTEDcanaryTOKEN000000000000000000"
S1 = "00000000-0000-7000-8000-0000000c0001"
S2 = "00000000-0000-7000-8000-0000000c0002"
S3 = "4b000000-0000-4000-8000-0000000c0003"
S4 = "00000000-0000-7000-8000-0000000c0004"
SHED = "/home/invented/shed"

PATCH = (
    "*** Begin Patch\n*** Update File: /home/invented/shed/plan.md\n@@\n-old\n+new\n"
    "*** Add File: /home/invented/shed/roof.md\n+roof\n*** End Patch"
)


class Rollout:
    def __init__(self):
        self.lines = []

    def add(self, at, kind, payload, **extra):
        line = {"timestamp": at, "type": kind, "payload": payload}
        line.update(extra)
        self.lines.append(line)

    def item(self, at, payload, turn="turn-1"):
        payload = dict(payload, internal_chat_message_metadata_passthrough={"turn_id": turn})
        self.add(at, "response_item", payload)

    def write(self, path, partial=None, raw=False):
        path.parent.mkdir(parents=True, exist_ok=True)
        text = "".join(json.dumps(line) + "\n" for line in self.lines)
        if partial:
            text += json.dumps(partial)  # Still being written: no newline.
        path.write_text(text)


def user(text, *more):
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}, *more]}


def assistant(ident, text, phase=None):
    item = {"type": "message", "id": ident, "role": "assistant", "content": [{"type": "output_text", "text": text}]}
    if phase:
        item["phase"] = phase
    return item


def shed():
    r = Rollout()
    r.add("2026-01-04T10:00:00.000Z", "session_meta", {
        "id": S1, "session_id": S1, "timestamp": "2026-01-04T10:00:00.000Z", "cwd": SHED,
        "originator": "codex_cli_rs", "cli_version": "9.9.0", "source": "cli", "model_provider": "openai",
        "base_instructions": {"text": "Invented base instructions."},
        "git": {"branch": "main", "commit_hash": "0000000000000000000000000000000000000abc",
                "repository_url": "https://example.com/invented/shed.git"},
    })
    r.add("2026-01-04T10:00:00.100Z", "turn_context", {"turn_id": "turn-1", "cwd": SHED, "model": "gpt-invented-1",
                                                       "effort": "high", "summary": "auto"})
    r.item("2026-01-04T10:00:00.200Z", {"type": "message", "id": "msg_dev", "role": "developer",
                                        "content": [{"type": "input_text", "text": "Invented developer rules."}]})
    r.item("2026-01-04T10:00:00.300Z", dict(user("<environment_context>\n  <cwd>/home/invented/shed</cwd>\n"
                                                  "</environment_context>"), id="msg_env"))
    r.item("2026-01-04T10:00:01.000Z", dict(user(f"Plan the invented garden shed. Token {CANARY} is for the API."),
                                            id="msg_u1"))
    r.add("2026-01-04T10:00:01.100Z", "event_msg", {"type": "user_message", "message": "duplicate of the prompt"})
    r.item("2026-01-04T10:00:03.000Z", {"type": "reasoning", "id": "rs_1", "encrypted_content": "AAAA",
                                        "summary": [{"type": "summary_text", "text": "**Planning** the invented shed."}]})
    r.item("2026-01-04T10:00:04.000Z", assistant("msg_a1", "I'll sketch the plan first.", "commentary"))
    r.item("2026-01-04T10:00:05.000Z", {"type": "custom_tool_call", "id": "ctc_1", "call_id": "call_1",
                                        "name": "apply_patch", "input": PATCH, "status": "completed"})
    r.item("2026-01-04T10:00:06.000Z", {"type": "custom_tool_call_output", "id": "ctco_1", "call_id": "call_1",
                                        "output": "invented tool output"})
    r.item("2026-01-04T10:00:07.000Z", {"type": "function_call", "id": "fc_1", "call_id": "call_2", "name": "exec",
                                        "arguments": json.dumps({"cmd": "ls"})})
    r.item("2026-01-04T10:00:08.000Z", {"type": "function_call_output", "id": "fco_1", "call_id": "call_2",
                                        "output": "invented listing"})
    r.add("2026-01-04T10:00:08.500Z", "event_msg", {"type": "token_count", "info": None,
                                                    "rate_limits": {"primary": {"used_percent": 12.0}}})
    r.item("2026-01-04T10:00:10.000Z", assistant("msg_a2", "The shed plan is in plan.md and roof.md.", "final_answer"))
    r.add("2026-01-04T10:00:10.100Z", "event_msg", {"type": "agent_message", "message": "duplicate"})
    r.add("2026-01-04T10:05:00.000Z", "turn_context", {"turn_id": "turn-2", "cwd": SHED, "model": "gpt-invented-2"})
    r.item("2026-01-04T10:05:01.000Z", dict(user("Here is the invented site photo.",
                                                 {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}),
                                            id="msg_u2"), turn="turn-2")
    r.item("2026-01-04T10:05:02.000Z", {"type": "function_call", "id": "fc_2", "call_id": "call_3",
                                        "namespace": "collaboration", "name": "spawn_agent",
                                        "arguments": json.dumps({"task": "measure"})}, turn="turn-2")
    r.add("2026-01-04T10:06:00.000Z", "event_msg", {"type": "turn_aborted", "reason": "interrupted"})
    r.add("2026-01-04T10:30:00.000Z", "compacted", {"message": "Invented summary of the shed work so far.",
                                                    "replacement_history": []})
    r.item("2026-01-04T10:31:00.000Z", dict(user("<user_shell_command>git status\nclean</user_shell_command>"),
                                            id="msg_shell"), turn="turn-3")
    r.item("2026-01-04T10:31:30.000Z", dict(user("Now price the invented lumber."), id="msg_u3"), turn="turn-3")
    r.item("2026-01-04T10:32:00.000Z", assistant("msg_a3", "About 400 invented dollars.", "final_answer"),
           turn="turn-3")
    r.write(ROOT / "sessions" / "2026" / "01" / "04" / f"rollout-2026-01-04T10-00-00-{S1}.jsonl",
            partial={"timestamp": "2026-01-04T10:33:00Z", "type": "response_item"})


def subagent():
    r = Rollout()
    r.add("2026-01-04T10:05:03.000Z", "session_meta", {
        "id": S2, "session_id": S1, "timestamp": "2026-01-04T10:05:03.000Z", "cwd": SHED,
        "originator": "Invented App", "cli_version": "9.9.0", "source": {"subagent": {"parent_thread_id": S1}},
        "thread_source": "subagent", "model_provider": "openai", "git": {"branch": "main"},
    })
    r.add("2026-01-04T10:05:03.100Z", "turn_context", {"turn_id": "turn-s1", "cwd": SHED, "model": "gpt-invented-1"})
    r.item("2026-01-04T10:05:04.000Z", dict(user("Measure the invented site from the photo."), id="msg_s1"),
           turn="turn-s1")
    r.item("2026-01-04T10:05:20.000Z", assistant("msg_s2", "The invented site is 3 by 4 metres."), turn="turn-s1")
    r.item("2026-01-04T10:05:21.000Z", {"type": "agent_message", "id": "amsg_1", "author": "/root/measure",
                                        "recipient": "/root", "content": [{"type": "input_text",
                                                                           "text": "Measurement done."}]},
           turn="turn-s1")
    r.write(ROOT / "sessions" / "2026" / "01" / "04" / f"rollout-2026-01-04T10-05-03-{S2}.jsonl")


def legacy():
    lines = [
        {"id": S3, "timestamp": "2025-08-13T19:41:15.372Z", "instructions": None,
         "git": {"commit_hash": "0000000000000000000000000000000000000def", "branch": "main",
                 "repository_url": "https://example.com/invented/fence.git"}},
        {"record_type": "state"},
        {"type": "message", "id": None, "role": "user",
         "content": [{"type": "input_text", "text": "Mend the invented fence."}]},
        {"type": "reasoning", "id": "rs_legacy", "summary": [], "encrypted_content": "AAAA"},
        {"type": "function_call", "id": "fc_legacy", "name": "shell",
         "arguments": json.dumps({"command": ["bash", "-lc", "ls"]}), "call_id": "call_l"},
        {"type": "function_call_output", "call_id": "call_l", "output": json.dumps({"output": "invented"})},
        {"record_type": "state"},
        {"type": "message", "id": "msg_legacy", "role": "assistant",
         "content": [{"type": "output_text", "text": "The invented fence is mended."}]},
    ]
    path = ROOT / "archived_sessions" / f"rollout-2025-08-13T19-41-15-{S3}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))


def compressed():
    # Codex compresses idle rollouts in place.
    r = Rollout()
    r.add("2026-01-03T08:00:00.000Z", "session_meta", {
        "id": S4, "session_id": S4, "timestamp": "2026-01-03T08:00:00.000Z", "cwd": "/home/invented/pond",
        "originator": "codex_vscode", "cli_version": "9.8.0", "source": "vscode", "git": {"branch": "dev"},
    })
    r.add("2026-01-03T08:00:00.100Z", "turn_context", {"turn_id": "turn-p1", "model": "gpt-invented-1"})
    r.item("2026-01-03T08:00:01.000Z", dict(user("Dig the invented pond."), id="msg_p1"), turn="turn-p1")
    r.item("2026-01-03T08:00:09.000Z", assistant("msg_p2", "The invented pond is dug.", "final_answer"),
           turn="turn-p1")
    path = ROOT / "sessions" / "2026" / "01" / "03" / f"rollout-2026-01-03T08-00-00-{S4}.jsonl.zst"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(line) + "\n" for line in r.lines)
    path.write_bytes(zstandard.ZstdCompressor().compress(text.encode()))


def index():
    rows = [
        {"id": S1, "thread_name": "Old invented name", "updated_at": "2026-01-04T10:00:00Z"},
        {"id": S1, "thread_name": "Invented garden shed", "updated_at": "2026-01-04T11:00:00Z"},
    ]
    (ROOT / "session_index.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


if __name__ == "__main__":
    shed()
    subagent()
    legacy()
    compressed()
    index()
