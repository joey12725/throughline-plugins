"""Claude Code transcript lines -> Throughline messages.

One `Transcript` state per file (kept in the cursor, JSON only) carries what
a later line needs from earlier ones: the session's folder and branch, the
title, the tool calls made since the last reply, and running counts for the
session record. Mapping a line depends only on that state and the line, so a
run resumed from any checkpoint emits exactly what an uninterrupted run would.
"""

import json
import re
from datetime import UTC, datetime

from .scrub import scrub_value

MAPPING_REVISION = 1
MESSAGES = "claude_code.messages"
SESSIONS = "claude_code.sessions"
MAX_TEXT = 100_000
MAX_FILES = 50
TITLE_CHARS = 80

AGENT = {"system": "claude_code", "kind": "agent", "value": "claude", "display_name": "Claude"}
ORCHESTRATOR = {
    "system": "claude_code",
    "kind": "agent",
    "value": "orchestrator",
    "display_name": "Agent prompt",
}

COMMAND = re.compile(r"<command-name>(.*?)</command-name>", re.S)
COMMAND_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S)
SHELL_INPUT = re.compile(r"^<bash-input>(.*?)</bash-input>", re.S)
LEADING_TAG = re.compile(r"^<([a-z][a-z0-9_-]*)[\s>]")
INTERRUPTED = re.compile(r"^\[Request interrupted by user[^\]]*\]$")
# User-role lines Claude Code writes itself: command output, notices,
# reminders. They are not anyone's words.
SKIP_TAGS = {
    "local-command-stdout",
    "local-command-stderr",
    "local-command-caveat",
    "bash-stdout",
    "bash-stderr",
    "task-notification",
    "system-reminder",
    "command-message",
}
# Tool input keys that name a file the tool touched.
PATH_KEYS = ("file_path", "notebook_path", "path")


def iso(value):
    """Offset-aware ISO text from an ISO timestamp, or None."""
    if not value or not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()


def cut(text):
    return (text[:MAX_TEXT], True) if len(text) > MAX_TEXT else (text, False)


def one_line(text, limit=TITLE_CHARS):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def owner_author(settings):
    system, kind, value = settings["owner_identifier"].split(":", 2)
    return {"system": system, "kind": kind, "value": value, "display_name": settings.get("owner_name")}


def new_pending():
    return {"calls": 0, "tools": {}, "files": [], "errors": 0, "last": None, "at": None}


def new_state(key, kind, home):
    """Fresh per-file state; the session/agent ids come from the path."""
    parts = key.split("/")
    project = parts[0]
    if kind == "session":
        session, agent, run = parts[-1].removesuffix(".jsonl"), None, None
        thread = session
    else:
        session = parts[1]
        agent = parts[-1].removesuffix(".jsonl").removeprefix("agent-")
        run = parts[4] if len(parts) > 5 and parts[3] == "workflows" else None
        thread = f"{session}/agent-{agent}"
    return {
        "thread": thread,
        "session": session,
        "agent": agent,
        "workflow_run": run,
        "project_key": project,
        "home": home,
        "cwd": None,
        "branch": None,
        "entrypoint": None,
        "version": None,
        "model": None,
        "agent_type": None,
        "titles": {},
        "pending": None,
        "counts": {"owner": 0, "agent": 0, "agent_prompt": 0, "tool_calls": 0, "skipped": 0},
        "models": {},
        "first_at": None,
        "last_at": None,
    }


# Per-file state the path cannot give back; the rest is rebuilt on load.
DERIVED = ("thread", "session", "agent", "workflow_run", "project_key", "home")


def pack(s):
    """Cursor form of a file state: no path-derived keys, no empty values."""
    out = {k: v for k, v in s.items() if k not in DERIVED and v not in (None, {}, [], 0)}
    counts = {k: v for k, v in (s.get("counts") or {}).items() if v}
    if counts:
        out["counts"] = counts
    else:
        out.pop("counts", None)
    return out


def unpack(stored, key, kind, home):
    s = new_state(key, kind, home)
    counts = dict(s["counts"], **(stored.get("counts") or {}))
    s.update({k: v for k, v in stored.items() if k not in DERIVED})
    s["counts"] = counts
    return s


def title_of(s):
    t = s["titles"]
    for key in ("custom", "ai", "summary", "agent_description", "agent_name", "first"):
        if t.get(key):
            return t[key]
    return None


class Mapper:
    def __init__(self, settings):
        self.settings = settings
        self.owner = owner_author(settings)
        self.thinking = settings["thinking"]
        self.scrub = settings["scrub"]
        self.entrypoints = settings["entrypoints"]  # None: every entrypoint.

    # -- helpers ---------------------------------------------------------

    def clean(self, value):
        return scrub_value(value) if self.scrub else (value, 0)

    def thread(self, s):
        metadata = {
            "session_id": s["session"],
            "project_dir": s["cwd"],
            "git_branch": s["branch"],
            "entrypoint": s["entrypoint"],
            "is_subagent": bool(s["agent"]),
            "home": s["home"],
        }
        if s["agent"]:
            metadata.update(parent_session=s["session"], agent_id=s["agent"], agent_type=s["agent_type"])
        return {"external_id": s["thread"], "title": title_of(s), "metadata": metadata}

    def emit(self, s, ext, at, role, author_kind, text, line, extra=None, tools=None):
        text, truncated = cut(text)
        length = len(text)
        tools = tools or {}
        if author_kind == "owner":
            author = self.owner
        elif author_kind == "agent_prompt":
            author = ORCHESTRATOR
        else:
            author = AGENT
        message = line.get("message") if isinstance(line.get("message"), dict) else {}
        usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
        fields = dict(
            role=role,
            author_kind=author_kind,
            session_id=s["session"],
            agent_id=s["agent"],
            is_subagent=bool(s["agent"] or line.get("isSidechain")),
            project_dir=line.get("cwd") or s["cwd"],
            git_branch=line.get("gitBranch") or s["branch"],
            entrypoint=line.get("entrypoint") or s["entrypoint"],
            model=message.get("model") if author_kind == "agent" else None,
            tool_calls=tools.get("calls", 0),
            tools=tools.get("tools") or None,
            files=tools.get("files") or None,
            tool_errors=tools.get("errors", 0),
            image_count=0,
            text_length=length,
            text_truncated=truncated,
            claude_version=line.get("version") or s["version"],
            mapping_revision=MAPPING_REVISION,
        )
        fields.update(extra or {})
        payload = {
            k: line.get(k)
            for k in (
                "type",
                "uuid",
                "parentUuid",
                "logicalParentUuid",
                "sessionId",
                "agentId",
                "isSidechain",
                "promptId",
                "requestId",
                "entrypoint",
                "userType",
                "cwd",
                "gitBranch",
                "version",
            )
            if line.get(k) is not None
        }
        if message.get("id"):
            payload["message_id"] = message["id"]
        if message.get("stop_reason"):
            payload["stop_reason"] = message["stop_reason"]
        if usage:
            payload["usage"] = {
                k: usage[k]
                for k in (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_input_tokens",
                    "cache_creation_input_tokens",
                )
                if isinstance(usage.get(k), int)
            }
        if tools.get("tools"):
            payload["tools"] = {"calls": tools["calls"], "names": tools["tools"], "files": tools["files"]}
        record = dict(
            channel=MESSAGES,
            external_id=f"{s['thread']}:{ext}",
            thread=self.thread(s),
            happened_at=at,
            author=author,
            content=text,
            fields=fields,
            images=[],
            payload=payload,
        )
        record, redactions = self.clean(record)
        record["fields"]["redactions"] = redactions
        counts = s["counts"]
        counts[author_kind] = counts.get(author_kind, 0) + 1
        counts["tool_calls"] += tools.get("calls", 0)
        if author_kind == "agent" and fields["model"]:
            s["models"][fields["model"]] = s["models"].get(fields["model"], 0) + 1
        s["first_at"] = min(filter(None, [s["first_at"], at]))
        s["last_at"] = max(filter(None, [s["last_at"], at]))
        return record

    def flush_tools(self, s, line):
        """Tool calls after the last reply of a turn, with no reply after
        them (an interrupted turn, a turn ending on a tool): one record."""
        pending, s["pending"] = s["pending"], None
        if not pending or not pending["calls"]:
            return []
        names = ", ".join(f"{name} ×{count}" for name, count in pending["tools"].items())
        return [
            self.emit(
                s,
                f"{pending['last']}:tools",
                pending["at"],
                "tool_calls",
                "agent",
                f"Tool calls without a text reply: {names}",
                {"cwd": line.get("cwd"), "gitBranch": line.get("gitBranch")},
                tools=pending,
            )
        ]

    # -- lines -----------------------------------------------------------

    def line(self, s, d):
        """Messages for one parsed transcript line (updates s)."""
        kind = d.get("type")
        titles = s["titles"]
        if kind == "custom-title" and d.get("customTitle"):
            titles["custom"] = self.clean(one_line(d["customTitle"]))[0]
            return []
        if kind == "ai-title" and d.get("aiTitle"):
            titles["ai"] = self.clean(one_line(d["aiTitle"]))[0]
            return []
        if kind == "summary" and d.get("summary"):
            titles["summary"] = self.clean(one_line(d["summary"]))[0]
            return []
        if kind == "agent-name" and d.get("agentName"):
            titles["agent_name"] = self.clean(one_line(d["agentName"]))[0]
            return []
        if kind not in ("user", "assistant"):
            return []
        entrypoint = d.get("entrypoint")
        if self.entrypoints is not None and entrypoint and entrypoint not in self.entrypoints:
            s["counts"]["skipped"] += 1
            return []
        for key, field in (("cwd", "cwd"), ("gitBranch", "branch"), ("entrypoint", "entrypoint"), ("version", "version")):
            if d.get(key):
                s[field] = d[key]
        at = iso(d.get("timestamp")) or s["last_at"]
        message = d.get("message") if isinstance(d.get("message"), dict) else {}
        if not at or not d.get("uuid"):
            return []
        if kind == "user":
            return self.user(s, d, message, at)
        return self.assistant(s, d, message, at)

    def user(self, s, d, message, at):
        content = message.get("content")
        texts, images = [], 0
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    texts.append(block.get("text") or "")
                elif block.get("type") == "image":
                    images += 1
                elif block.get("type") == "tool_result" and block.get("is_error"):
                    if s["pending"]:
                        s["pending"]["errors"] += 1
        text = "\n\n".join(t for t in texts if t and t.strip()).strip()
        if d.get("isMeta") or (not text and not images):
            return []
        origin = (d.get("origin") or {}).get("kind") if isinstance(d.get("origin"), dict) else None
        if origin == "task-notification" or d.get("turnOrigin") == "task_notification":
            return []
        role = "user"
        if d.get("isCompactSummary"):
            role, author_kind = "compact_summary", "agent"
        else:
            command = COMMAND.search(text)
            shell = SHELL_INPUT.match(text)
            tag = LEADING_TAG.match(text)
            if command:
                args = COMMAND_ARGS.search(text)
                text = (command.group(1).strip() + " " + (args.group(1).strip() if args else "")).strip()
                role = "command"
            elif shell:
                text, role = "! " + shell.group(1).strip(), "shell"
            elif tag and tag.group(1) in SKIP_TAGS:
                return []
            elif INTERRUPTED.match(text):
                return []
            sidechain = bool(s["agent"] or d.get("isSidechain"))
            author_kind = "agent_prompt" if sidechain or origin == "coordinator" else "owner"
        out = self.flush_tools(s, d)
        if author_kind in ("owner", "agent_prompt") and not s["titles"].get("first") and text:
            s["titles"]["first"] = self.clean(one_line(text))[0]
        out.append(
            self.emit(
                s,
                d["uuid"],
                at,
                role,
                author_kind,
                text or "(image)",
                d,
                {"image_count": images},
            )
        )
        return out

    def assistant(self, s, d, message, at):
        if d.get("isApiErrorMessage") or message.get("model") == "<synthetic>":
            return []
        content = message.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if not isinstance(content, list):
            return []
        if message.get("model"):
            s["model"] = message["model"]
        out = []
        for index, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            ext = d["uuid"] if index == 0 else f"{d['uuid']}#{index}"
            kind = block.get("type")
            if kind == "text" and (block.get("text") or "").strip():
                tools, s["pending"] = s["pending"] or new_pending(), None
                out.append(self.emit(s, ext, at, "assistant", "agent", block["text"].strip(), d, tools=tools))
            elif kind == "thinking" and self.thinking and (block.get("thinking") or "").strip():
                out.append(self.emit(s, ext, at, "thinking", "agent", block["thinking"].strip(), d))
            elif kind == "tool_use" and block.get("name") == "StructuredOutput":
                # A subagent's final answer, given as a tool call: its reply.
                value = block.get("input")
                text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=1)
                if text and text.strip() not in ("{}", "null"):
                    tools, s["pending"] = s["pending"] or new_pending(), None
                    out.append(self.emit(s, ext, at, "structured_output", "agent", text, d, tools=tools))
            elif kind == "tool_use":
                pending = s["pending"] = s["pending"] or new_pending()
                name = str(block.get("name") or "tool")
                pending["calls"] += 1
                pending["tools"][name] = pending["tools"].get(name, 0) + 1
                pending["last"], pending["at"] = ext, at
                tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
                for key in PATH_KEYS:
                    value = tool_input.get(key)
                    if isinstance(value, str) and value and len(pending["files"]) < MAX_FILES:
                        value = value[:300]
                        if value not in pending["files"]:
                            pending["files"].append(value)
                        break
        return out

    # -- session record --------------------------------------------------

    def session_record(self, s):
        counts = s["counts"]
        if not s["first_at"]:
            return None
        title = title_of(s)
        sub = bool(s["agent"])
        where = s["cwd"] or s["project_key"]
        lines = [title or "(untitled)", f"Claude Code {'subagent' if sub else 'session'} in {where}"]
        if s["branch"]:
            lines[-1] += f" on branch {s['branch']}"
        fields = dict(
            session_id=s["session"],
            agent_id=s["agent"],
            is_subagent=sub,
            agent_type=s["agent_type"],
            workflow_run=s["workflow_run"],
            title=title,
            project_dir=s["cwd"],
            git_branch=s["branch"],
            entrypoint=s["entrypoint"],
            claude_version=s["version"],
            owner_messages=counts.get("owner", 0),
            agent_messages=counts.get("agent", 0),
            agent_prompts=counts.get("agent_prompt", 0),
            tool_calls=counts.get("tool_calls", 0),
            models=dict(sorted(s["models"].items())) or None,
            home=s["home"],
            mapping_revision=MAPPING_REVISION,
        )
        record = dict(
            channel=SESSIONS,
            external_id=s["thread"],
            thread={"external_id": f"project:{s['project_key']}", "title": s["cwd"] or s["project_key"]},
            happened_at=s["first_at"],
            ended_at=s["last_at"] if s["last_at"] and s["last_at"] >= s["first_at"] else None,
            content="\n".join(lines),
            fields=fields,
            images=[],
            payload=dict(fields, titles=dict(sorted(s["titles"].items()))),
        )
        record, _ = self.clean(record)
        return record
