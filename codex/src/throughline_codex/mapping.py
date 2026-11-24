"""Codex rollout lines -> Throughline messages.

Rollout lines are `{timestamp, type, payload}`: `session_meta` (first line),
`turn_context` (model and folder of each turn), `response_item` (the model
conversation: messages, reasoning, tool calls and outputs), `event_msg` (UI
events that repeat the response items, plus token counts) and `compacted`.
Early 2025 rollouts instead start with `{id, timestamp, instructions, git}`
and hold bare items. Only response items are mapped, so nothing is counted
twice; token and rate-limit events are left alone (plugins/t3code reads
those for subscription usage).

One state per file (kept in the cursor, JSON only) carries what a later
line needs: session metadata, the current model and turn, tool calls since
the last reply, and running counts for the session record. Mapping a line
depends only on that state and the line, so a run resumed from any
checkpoint emits exactly what an uninterrupted run would.
"""

import json
import re
from datetime import UTC, datetime

from .files import session_id_of
from .scrub import scrub_value

MAPPING_REVISION = 1
MESSAGES = "codex.messages"
SESSIONS = "codex.sessions"
MAX_TEXT = 100_000
MAX_FILES = 50
TITLE_CHARS = 80

AGENT = {"system": "codex", "kind": "agent", "value": "codex", "display_name": "Codex"}
ORCHESTRATOR = {"system": "codex", "kind": "agent", "value": "orchestrator", "display_name": "Agent prompt"}

LEGACY_ITEMS = {"message", "reasoning", "function_call", "function_call_output", "local_shell_call"}
PATCH_FILE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+?)\s*$", re.M)
PATCH_MOVE = re.compile(r"^\*\*\* Move to: (.+?)\s*$", re.M)
WRAPPED = re.compile(r"^<([A-Za-z][\w -]*?)>.*</\1>$", re.S)
# User-role text Codex writes itself: folder and sandbox context, AGENTS.md
# and skill instructions, shell command output, aborted-turn notices.
CONTEXT_PREFIXES = (
    "<environment_context",
    "<user_instructions",
    "<permissions instructions",
    "<collaboration_mode",
    "<user_shell_command",
    "<turn_aborted",
    "<subagent_notification",
    "<skill",
    "<INSTRUCTIONS>",
    "# AGENTS.md instructions",
)


def iso(value):
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


def injected(text):
    stripped = text.strip()
    return stripped.startswith(CONTEXT_PREFIXES) or bool(WRAPPED.match(stripped))


def owner_author(settings):
    system, kind, value = settings["owner_identifier"].split(":", 2)
    return {"system": system, "kind": kind, "value": value, "display_name": settings.get("owner_name")}


def new_pending():
    return {"calls": 0, "tools": {}, "files": [], "last": None, "at": None}


# Per-file state the file name cannot give back; the rest is rebuilt on load.
DERIVED = ("name", "home")


def new_state(name, home):
    return {
        "name": name,
        "home": home,
        "session": session_id_of(name),
        "meta_seen": False,
        "skip": False,
        "parent": None,
        "is_subagent": False,
        "cwd": None,
        "originator": None,
        "source": None,
        "cli_version": None,
        "model_provider": None,
        "git_branch": None,
        "git_commit": None,
        "git_repository": None,
        "model": None,
        "effort": None,
        "turn_id": None,
        "clock": None,
        "first_at": None,
        "last_at": None,
        "pending": None,
        "titles": {},
        "counts": {"owner": 0, "agent": 0, "agent_prompt": 0, "reasoning": 0, "tool_calls": 0},
        "models": {},
    }


def pack(s):
    out = {k: v for k, v in s.items() if k not in DERIVED and v not in (None, {}, [], 0, False)}
    counts = {k: v for k, v in (s.get("counts") or {}).items() if v}
    if counts:
        out["counts"] = counts
    else:
        out.pop("counts", None)
    return out


def unpack(stored, name, home):
    s = new_state(name, home)
    counts = dict(s["counts"], **(stored.get("counts") or {}))
    s.update({k: v for k, v in stored.items() if k not in DERIVED})
    s["counts"] = counts
    return s


def title_of(s):
    for key in ("index", "first"):
        if s["titles"].get(key):
            return s["titles"][key]
    return None


def texts(content, kinds):
    out = []
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get("type") in kinds and isinstance(block.get("text"), str):
            out.append(block["text"])
    return out


class Mapper:
    def __init__(self, settings):
        self.owner = owner_author(settings)
        self.reasoning = settings["reasoning"]
        self.scrub = settings["scrub"]
        self.originators = settings["originators"]  # None: every originator.

    def clean(self, value):
        return scrub_value(value) if self.scrub else (value, 0)

    def thread(self, s):
        metadata = {
            "session_id": s["session"],
            "project_dir": s["cwd"],
            "git_branch": s["git_branch"],
            "originator": s["originator"],
            "is_subagent": s["is_subagent"],
            "home": s["home"],
        }
        if s["parent"]:
            metadata["parent_session"] = s["parent"]
        return {"external_id": s["session"], "title": title_of(s), "metadata": metadata}

    def emit(self, s, ext, at, role, author_kind, text, item, extra=None, tools=None):
        text, truncated = cut(text)
        tools = tools or {}
        author = {"owner": self.owner, "agent_prompt": ORCHESTRATOR}.get(author_kind, AGENT)
        meta = item.get("internal_chat_message_metadata_passthrough")
        meta = meta if isinstance(meta, dict) else {}
        turn = meta.get("turn_id") or s["turn_id"]
        model = s["model"] if author_kind == "agent" else None
        fields = dict(
            role=role,
            author_kind=author_kind,
            session_id=s["session"],
            parent_session=s["parent"],
            is_subagent=s["is_subagent"],
            project_dir=s["cwd"],
            git_branch=s["git_branch"],
            originator=s["originator"],
            model=model,
            reasoning_effort=s["effort"] if author_kind == "agent" else None,
            turn_id=turn,
            phase=item.get("phase"),
            tool_calls=tools.get("calls", 0),
            tools=tools.get("tools") or None,
            files=tools.get("files") or None,
            image_count=0,
            text_length=len(text),
            text_truncated=truncated,
            cli_version=s["cli_version"],
            mapping_revision=MAPPING_REVISION,
        )
        fields.update(extra or {})
        payload = {k: item.get(k) for k in ("type", "id", "role", "phase", "status", "author", "recipient") if item.get(k)}
        if turn:
            payload["turn_id"] = turn
        if tools.get("tools"):
            payload["tools"] = {"calls": tools["calls"], "names": tools["tools"], "files": tools["files"]}
        record = dict(
            channel=MESSAGES,
            external_id=f"{s['session']}:{ext}",
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
        counts[role if role == "reasoning" else author_kind] += 1
        counts["tool_calls"] += tools.get("calls", 0)
        if model and role == "assistant":
            s["models"][model] = s["models"].get(model, 0) + 1
        s["first_at"] = min(filter(None, [s["first_at"], at]))
        s["last_at"] = max(filter(None, [s["last_at"], at]))
        return record

    def flush_tools(self, s):
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
                {},
                tools=pending,
            )
        ]

    # -- lines -----------------------------------------------------------

    def line(self, s, d, begin):
        kind = d.get("type")
        payload = d.get("payload") if isinstance(d.get("payload"), dict) else None
        if d.get("timestamp"):
            s["clock"] = iso(d["timestamp"]) or s["clock"]
        if kind == "session_meta" and payload:
            self.meta(s, payload)
            return []
        if kind is None and begin == 0 and d.get("id") and ("instructions" in d or "git" in d):
            self.meta(s, d)  # Early 2025 header line.
            return []
        if s["skip"]:
            return []
        if kind == "turn_context" and payload:
            for key, field in (("model", "model"), ("effort", "effort"), ("cwd", "cwd"), ("turn_id", "turn_id")):
                if payload.get(key):
                    s[field] = payload[key]
            return []
        at = iso(d.get("timestamp")) or s["clock"]
        if not at:
            return []
        if kind == "response_item" and payload:
            return self.item(s, payload, at, begin)
        if kind in LEGACY_ITEMS and "payload" not in d:
            return self.item(s, d, at, begin)
        if kind == "compacted" and payload and isinstance(payload.get("message"), str):
            text = payload["message"].strip()
            if text:
                out = self.flush_tools(s)
                out.append(self.emit(s, f"@{begin}", at, "compact_summary", "agent", text, {}))
                return out
        return []

    def meta(self, s, p):
        if s["meta_seen"]:
            return  # A resumed session repeats its header; the first one stands.
        s["meta_seen"] = True
        if p.get("id"):
            s["session"] = str(p["id"])
        if p.get("session_id") and p.get("session_id") != p.get("id"):
            s["parent"] = str(p["session_id"])
        source = p.get("source")
        s["source"] = source if isinstance(source, str) or source is None else json.dumps(source, sort_keys=True)
        s["is_subagent"] = bool(
            p.get("thread_source") == "subagent" or (isinstance(source, dict) and "subagent" in source)
        )
        if isinstance(source, dict) and isinstance(source.get("subagent"), dict):
            parent = source["subagent"].get("parent_thread_id")
            s["parent"] = s["parent"] or (str(parent) if parent else None)
        for key in ("cwd", "originator", "cli_version", "model_provider"):
            if isinstance(p.get(key), str):
                s[key] = p[key]
        git = p.get("git") if isinstance(p.get("git"), dict) else {}
        s["git_branch"] = git.get("branch")
        s["git_commit"] = git.get("commit_hash")
        s["git_repository"] = git.get("repository_url")
        s["clock"] = iso(p.get("timestamp")) or s["clock"]
        if self.originators is not None and s["originator"] not in self.originators:
            s["skip"] = True

    def item(self, s, p, at, begin):
        kind = p.get("type")
        ext = p.get("id") or f"@{begin}"
        if kind == "message":
            role = p.get("role")
            content = p.get("content")
            if role == "assistant":
                text = "\n\n".join(t.strip() for t in texts(content, ("output_text", "text")) if t.strip())
                if not text:
                    return []
                tools, s["pending"] = s["pending"] or new_pending(), None
                return [self.emit(s, ext, at, "assistant", "agent", text, p, tools=tools)]
            if role != "user":
                return []  # developer and system instructions.
            parts = [t.strip() for t in texts(content, ("input_text", "text")) if t.strip() and not injected(t)]
            images = sum(
                1 for b in content or [] if isinstance(b, dict) and b.get("type") in ("input_image", "image")
            )
            text = "\n\n".join(parts)
            if not text and not images:
                return []
            author_kind = "agent_prompt" if s["is_subagent"] else "owner"
            if not s["titles"].get("first") and text:
                s["titles"]["first"] = self.clean(one_line(text))[0]
            out = self.flush_tools(s)
            out.append(self.emit(s, ext, at, "user", author_kind, text or "(image)", p, {"image_count": images}))
            return out
        if kind == "reasoning":
            summary = p.get("summary") if isinstance(p.get("summary"), list) else []
            text = "\n\n".join(t.strip() for t in texts(summary, ("summary_text",)) if t.strip())
            if not self.reasoning or not text:
                return []
            return [self.emit(s, ext, at, "reasoning", "agent", text, p)]
        if kind == "agent_message":
            text = "\n\n".join(t.strip() for t in texts(p.get("content"), ("input_text", "text")) if t.strip())
            if not text:
                return []
            extra = {"agent_from": str(p.get("author") or "") or None, "agent_to": str(p.get("recipient") or "") or None}
            out = self.flush_tools(s)
            out.append(self.emit(s, ext, at, "agent_message", "agent_prompt", text, p, extra))
            return out
        if kind and kind.endswith("_call") and not kind.endswith("_output"):
            pending = s["pending"] = s["pending"] or new_pending()
            name = str(p.get("name") or kind.removesuffix("_call"))
            if p.get("namespace"):
                name = f"{p['namespace']}.{name}"
            pending["calls"] += 1
            pending["tools"][name] = pending["tools"].get(name, 0) + 1
            pending["last"], pending["at"] = ext, at
            raw = p.get("input") if isinstance(p.get("input"), str) else p.get("arguments")
            if not isinstance(raw, str):
                raw = json.dumps(raw) if raw is not None else ""
            if "*** " in raw:
                raw = raw.replace("\\n", "\n")
                for path in PATCH_FILE.findall(raw) + PATCH_MOVE.findall(raw):
                    path = path.strip().strip('"')[:300]
                    if path and path not in pending["files"] and len(pending["files"]) < MAX_FILES:
                        pending["files"].append(path)
        return []

    # -- session record --------------------------------------------------

    def session_record(self, s):
        if not s["first_at"] or s["skip"]:
            return None
        counts = s["counts"]
        title = title_of(s)
        where = s["cwd"] or "an unknown folder"
        lines = [title or "(untitled)", f"Codex {'subagent ' if s['is_subagent'] else ''}session in {where}"]
        if s["git_branch"]:
            lines[-1] += f" on branch {s['git_branch']}"
        fields = dict(
            session_id=s["session"],
            parent_session=s["parent"],
            is_subagent=s["is_subagent"],
            title=title,
            project_dir=s["cwd"],
            git_branch=s["git_branch"],
            git_commit=s["git_commit"],
            git_repository=s["git_repository"],
            originator=s["originator"],
            source=s["source"],
            cli_version=s["cli_version"],
            model_provider=s["model_provider"],
            owner_messages=counts["owner"],
            agent_messages=counts["agent"],
            agent_prompts=counts["agent_prompt"],
            reasoning_summaries=counts["reasoning"],
            tool_calls=counts["tool_calls"],
            models=dict(sorted(s["models"].items())) or None,
            home=s["home"],
            mapping_revision=MAPPING_REVISION,
        )
        project = s["cwd"] or "unknown"
        record = dict(
            channel=SESSIONS,
            external_id=s["session"],
            thread={"external_id": f"project:{project}", "title": project},
            happened_at=s["first_at"],
            ended_at=s["last_at"] if s["last_at"] and s["last_at"] >= s["first_at"] else None,
            content="\n".join(lines),
            fields=fields,
            images=[],
            payload=dict(fields, titles=dict(sorted(s["titles"].items()))),
        )
        record, _ = self.clean(record)
        return record
