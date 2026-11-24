"""Writes the invented Claude Code home under tests/recorded/home.

Run `python plugins/claude-code/tests/make_recorded.py` after changing it.
Everything here is made up: invented folders, ids and text, and a fake
secret (CANARY) that the scrubber must remove.
"""

import json
from pathlib import Path

ROOT = Path(__file__).parent / "recorded" / "home"
CANARY = "sk-ant-api03-INVENTEDcanaryKEY0000000000000000"
S1 = "00000000-0000-4000-8000-00000000a001"
S2 = "00000000-0000-4000-8000-00000000b002"
ORCHARD = "/home/invented/orchard"
FENCE = "/home/invented/fence"


def uid(n):
    return f"10000000-0000-4000-8000-{n:012d}"


class Session:
    def __init__(self, session, cwd, entrypoint="cli", branch="main", agent=None):
        self.lines, self.session, self.cwd = [], session, cwd
        self.entrypoint, self.branch, self.agent = entrypoint, branch, agent
        self.parent = None

    def base(self, kind, n, at, **extra):
        line = {
            "parentUuid": self.parent,
            "isSidechain": bool(self.agent),
            "userType": "external",
            "cwd": self.cwd,
            "sessionId": self.session,
            "version": "9.9.0",
            "gitBranch": self.branch,
            "entrypoint": self.entrypoint,
            "type": kind,
            "uuid": uid(n),
            "timestamp": at,
        }
        if self.agent:
            line["agentId"] = self.agent
        line.update(extra)
        self.parent = uid(n)
        self.lines.append(line)
        return line

    def user(self, n, at, content, **extra):
        return self.base("user", n, at, message={"role": "user", "content": content}, **extra)

    def assistant(self, n, at, block, msg="msg_01", model="claude-invented-1"):
        return self.base(
            "assistant",
            n,
            at,
            requestId="req_invented",
            message={
                "model": model,
                "id": msg,
                "type": "message",
                "role": "assistant",
                "content": [block],
                "stop_reason": "tool_use" if block["type"] == "tool_use" else "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100},
            },
        )

    def tool(self, n, at, tool_id, name, **tool_input):
        return self.assistant(n, at, {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input})

    def result(self, n, at, tool_id, error=False):
        return self.user(
            n,
            at,
            [{"type": "tool_result", "tool_use_id": tool_id, "content": "invented output", "is_error": error}],
            sourceToolAssistantUUID=self.parent,
        )

    def other(self, line):
        self.lines.append(line)

    def write(self, path, partial=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        text = "".join(json.dumps(line) + "\n" for line in self.lines)
        if partial:
            text += json.dumps(partial)  # Still being written: no newline.
        path.write_text(text)


def text(s):
    return {"type": "text", "text": s}


def orchard():
    s = Session(S1, ORCHARD)
    s.other({"type": "summary", "summary": "Old invented summary", "leafUuid": uid(999)})
    s.user(1, "2026-01-02T09:00:00.000Z", "Plan the invented orchard layout, please.")
    s.base("attachment", 2, "2026-01-02T09:00:00.100Z", attachment={"type": "date"})
    s.assistant(3, "2026-01-02T09:00:05.000Z", {"type": "thinking", "thinking": "Invented thought.", "signature": "x"})
    s.assistant(4, "2026-01-02T09:00:06.000Z", text("I'll read the plan first."))
    s.tool(5, "2026-01-02T09:00:07.000Z", "toolu_1", "Read", file_path=f"{ORCHARD}/plan.md")
    s.result(6, "2026-01-02T09:00:08.000Z", "toolu_1")
    s.tool(7, "2026-01-02T09:00:09.000Z", "toolu_2", "Bash", command="ls", description="list")
    s.result(8, "2026-01-02T09:00:10.000Z", "toolu_2", error=True)
    s.tool(9, "2026-01-02T09:00:11.000Z", "toolu_3", "Edit", file_path=f"{ORCHARD}/plan.md")
    s.result(10, "2026-01-02T09:00:12.000Z", "toolu_3")
    s.assistant(11, "2026-01-02T09:00:20.000Z", text("Rows of apples face south; the plan is updated."), msg="msg_02")
    s.other({"type": "ai-title", "aiTitle": "Invented orchard layout", "sessionId": S1})
    s.user(12, "2026-01-02T09:05:00.000Z", f"Use this key {CANARY} for the invented weather API.")
    s.assistant(13, "2026-01-02T09:05:04.000Z", text("Noted; I will not store that key."), msg="msg_03")
    s.user(
        14,
        "2026-01-02T09:06:00.000Z",
        "<command-message>review is running…</command-message>\n<command-name>/review</command-name>\n"
        "<command-args>orchard</command-args>",
    )
    s.user(15, "2026-01-02T09:06:00.500Z", "<local-command-stdout>invented output</local-command-stdout>")
    s.user(16, "2026-01-02T09:06:01.000Z", "Invented skill body.", isMeta=True)
    s.user(17, "2026-01-02T09:07:00.000Z", "<bash-input>git status</bash-input>")
    s.user(18, "2026-01-02T09:07:01.000Z", "<bash-stdout>clean</bash-stdout><bash-stderr></bash-stderr>")
    s.tool(19, "2026-01-02T09:08:00.000Z", "toolu_4", "Agent", description="Count trees", prompt="...")
    s.tool(20, "2026-01-02T09:08:01.000Z", "toolu_5", "Write", file_path=f"{ORCHARD}/trees.txt")
    s.user(21, "2026-01-02T09:08:30.000Z", [text("[Request interrupted by user for tool use]")])
    s.user(
        22,
        "2026-01-02T09:09:00.000Z",
        [text("Here is a photo of the invented field."), {"type": "image", "source": {"type": "base64", "data": "AAAA"}}],
    )
    s.assistant(23, "2026-01-02T09:09:05.000Z", text("The field slopes gently east."), msg="msg_04")
    s.user(
        24,
        "2026-01-02T09:10:00.000Z",
        "<task-notification>invented task done</task-notification>",
        origin={"kind": "task-notification"},
    )
    s.assistant(25, "2026-01-02T09:10:01.000Z", text("API Error: invented"), msg="msg_e", model="<synthetic>")
    s.user(26, "2026-01-02T10:00:00.000Z", "This session is being continued. Invented summary of the work so far.",
           isCompactSummary=True, isVisibleInTranscriptOnly=True)
    s.user(27, "2026-01-02T10:01:00.000Z", "Now draft the invented fence plan with Bearer abcdefghijklmnop1234567890.")
    s.assistant(28, "2026-01-02T10:01:30.000Z", text("Drafted the invented fence plan."), msg="msg_05")
    s.other({"type": "custom-title", "customTitle": "Orchard planning", "sessionId": S1})
    s.other({"type": "last-prompt", "lastPrompt": "Now draft", "leafUuid": uid(28), "sessionId": S1})
    s.write(
        ROOT / "projects" / "-home-invented-orchard" / f"{S1}.jsonl",
        partial={"type": "user", "uuid": uid(29), "timestamp": "2026-01-02T10:02:00Z"},
    )


def subagents():
    a = Session(S1, ORCHARD, agent="a0000000000000001")
    a.user(101, "2026-01-02T09:08:02.000Z", "Count the invented trees in trees.txt and report the total.")
    a.tool(102, "2026-01-02T09:08:03.000Z", "toolu_s1", "Read", file_path=f"{ORCHARD}/trees.txt")
    a.result(103, "2026-01-02T09:08:04.000Z", "toolu_s1")
    a.assistant(104, "2026-01-02T09:08:10.000Z", text("There are 42 invented trees."), msg="msg_s1")
    folder = ROOT / "projects" / "-home-invented-orchard" / S1 / "subagents"
    a.write(folder / "agent-a0000000000000001.jsonl")
    (folder / "agent-a0000000000000001.meta.json").write_text(
        json.dumps({"agentType": "general-purpose", "description": "Count invented trees", "finished": True})
    )
    w = Session(S1, ORCHARD, agent="b0000000000000002")
    w.user(201, "2026-01-02T09:30:00.000Z", "Review the invented plan for errors.")
    w.assistant(202, "2026-01-02T09:30:20.000Z", text("No invented errors found."), msg="msg_w1")
    w.tool(203, "2026-01-02T09:30:21.000Z", "toolu_w1", "StructuredOutput", errors=0)
    w.write(folder / "workflows" / "wf_invented-1" / "agent-b0000000000000002.jsonl")


def fence():
    # Started through the Agent SDK by another app.
    s = Session(S2, FENCE, entrypoint="sdk-ts", branch="feat/gate")
    s.user(301, "2026-01-03T14:00:00.000Z", "Fix the invented gate latch.")
    s.tool(302, "2026-01-03T14:00:05.000Z", "toolu_f1", "Edit", file_path=f"{FENCE}/gate.py")
    s.result(303, "2026-01-03T14:00:06.000Z", "toolu_f1")
    s.assistant(304, "2026-01-03T14:00:10.000Z", text("Latch fixed in gate.py."), msg="msg_f1")
    s.user(305, "2026-01-03T14:01:00.000Z", "And paint it green.")
    s.assistant(306, "2026-01-03T14:01:05.000Z", text("Painted, in a manner of speaking."), msg="msg_f2")
    s.write(ROOT / "projects" / "-home-invented-fence" / f"{S2}.jsonl")


if __name__ == "__main__":
    orchard()
    subagents()
    fence()
