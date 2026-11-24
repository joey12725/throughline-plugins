"""aider chat sessions as a Throughline source.

Finds `.aider.chat.history.md` files under the configured roots and yields
one message per user input (author_kind owner), assistant reply (agent, with
the model aider reported) and, optionally, aider's own tool output (other).
One thread per aider session.

Incremental: a byte-offset cursor per file. The last session of a file is
re-read on every run (it may still be in progress); earlier sessions are
never read again.
"""

import copy
import hashlib
import logging
import os
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from throughline_sdk import Channel, Checkpoint, Declaration, Manifest, Plugin, run

from . import history

__all__ = ["Aider", "MAPPING_REVISION", "main"]

MAPPING_REVISION = 1
CHANNEL = "aider.messages"
TEXT_LIMIT = 100_000
PRUNE = {".git", "node_modules", ".venv", "venv", "__pycache__", ".tox", ".mypy_cache", "dist", "build", "target"}
log = logging.getLogger("throughline_aider")


def fields(**values):
    return [dict(name=k, type=v[0], description=v[1]) for k, v in values.items()]


MESSAGE_FIELDS = fields(
    role=("text", "user, assistant or tool (aider's own output: edits applied, commits, command output)"),
    author_kind=("text", "owner (you typed it), agent (the model's reply) or other (aider's tool output)"),
    model=("text", "Main model aider reported for the session at this point"),
    repo=("text", "Folder the history file is in (the repository aider ran in)"),
    history_file=("text", "Path of the .aider.chat.history.md file"),
    session_id=("text", "Session id (derived from the file, the header's byte offset and its time)"),
    session_started_at=("timestamp", "When the aider session started"),
    ordinal=("integer", "Position of the message in its session"),
    is_command=("boolean", "A user input that is an aider command (/add, /run, ...)"),
    aider_version=("text", "aider version the session reported"),
    time_estimated=(
        "boolean",
        "No time was recorded for this message: it is placed one second after the previous one",
    ),
    text_length=("integer", "Characters of the message text"),
    text_truncated=("boolean", "The text was cut to 100,000 characters"),
    mapping_revision=("integer", "Mapping revision that produced this record"),
)

SETTINGS = [
    ("roots", "AIDER_ROOTS", None,
     "Folders to search for aider history files (or the files themselves), separated by the "
     "OS path separator (':' on Linux/macOS, ';' on Windows)"),
    ("scan_depth", "AIDER_SCAN_DEPTH", "3", "How many folder levels below each root to search"),
    ("history_name", "AIDER_HISTORY_NAME", ".aider.chat.history.md", "Chat history file name"),
    ("input_history_name", "AIDER_INPUT_HISTORY_NAME", ".aider.input.history",
     "Input history file name next to it (gives user messages real times); empty to ignore"),
    ("timezone", "AIDER_TIMEZONE", None, "IANA time zone aider ran in (default: this machine's)"),
    ("include_tool_output", "AIDER_INCLUDE_TOOL_OUTPUT", "true", "Also record aider's tool output"),
    ("batch_size", "AIDER_BATCH_SIZE", "500", "Messages per checkpoint (whole sessions)"),
    ("owner_identifier", "AIDER_OWNER_IDENTIFIER", "aider:owner:owner",
     "Author identifier of your own inputs, system:kind:value"),
    ("owner_name", "AIDER_OWNER_NAME", None, "Display name on your inputs"),
]


def truthy(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def iso(when):
    return when.isoformat()


class Aider(Plugin):
    manifest = Manifest(
        name="aider",
        version="0.1.0",
        channels=[
            Channel(
                name=CHANNEL,
                description=(
                    "aider coding-assistant sessions: your inputs (author_kind owner), the "
                    "model's replies (agent, with the model name) and aider's tool output "
                    "(other); one thread per aider session"
                ),
                fields=MESSAGE_FIELDS,
                conversation_gap_s=1800,
            )
        ],
        settings={
            key: Declaration(env=env, required=key == "roots", default=default, description=description)
            for key, env, default, description in SETTINGS
        },
        secrets={},
        run_mode="poll",
        default_interval_s=900,
    )

    def __init__(self):
        super().__init__()
        self.roots = [Path(p).expanduser() for p in self.settings["roots"].split(os.pathsep) if p.strip()]
        if not self.roots:
            raise ValueError("AIDER_ROOTS names no folders")
        self.depth = max(0, int(self.settings["scan_depth"] or 3))
        self.tz = ZoneInfo(self.settings["timezone"]) if self.settings["timezone"] else None
        self.tool_output = truthy(self.settings["include_tool_output"])
        self.batch_size = max(1, int(self.settings["batch_size"] or 500))
        parts = (self.settings["owner_identifier"] or "aider:owner:owner").split(":", 2)
        if len(parts) != 3 or not all(parts):
            raise ValueError("AIDER_OWNER_IDENTIFIER must be system:kind:value")
        self.owner = dict(system=parts[0], kind=parts[1], value=parts[2], display_name=self.settings["owner_name"])
        self.stats = {}

    def execute(self, client, mode, dry_run=False):
        report = super().execute(client, mode, dry_run)
        if self.stats:
            report["aider"] = self.stats
        return report

    # -- discovery -----------------------------------------------------------

    def history_files(self):
        name = self.settings["history_name"] or ".aider.chat.history.md"
        found = set()
        existing = [r for r in self.roots if r.exists()]
        if not existing:
            raise FileNotFoundError("none of the AIDER_ROOTS folders exist")
        for root in existing:
            if root.is_file():
                found.add(root.resolve())
                continue
            base = len(root.resolve().parts)
            for folder, dirs, files in os.walk(root.resolve()):
                if len(Path(folder).parts) - base >= self.depth:
                    dirs[:] = []
                else:
                    dirs[:] = sorted(d for d in dirs if d not in PRUNE)
                if name in files:
                    found.add(Path(folder) / name)
        return sorted(found)

    def input_times(self, path):
        name = self.settings["input_history_name"]
        file = path.parent / name if name else None
        if not file or not file.is_file():
            return history.InputTimes([])
        text = file.read_text(encoding="utf-8", errors="replace")
        return history.InputTimes(history.parse_inputs(text, self.tz))

    # -- mapping -------------------------------------------------------------

    def author(self, role, model):
        if role == "user":
            return dict(self.owner)
        if role == "assistant":
            return dict(system="aider", kind="agent", value=model or "assistant",
                        display_name=model or "aider assistant")
        return dict(system="aider", kind="tool", value="aider", display_name="aider")

    def session_messages(self, path, session, times, next_start):
        key = str(path)
        sid = hashlib.sha256(f"{key}\0{session.offset}\0{session.header}".encode()).hexdigest()[:20]
        started = history.local_time(session.stamp, self.tz)
        times.seek(started)
        model = version = None
        repo = str(path.parent)
        title = f"{path.parent.name} · aider {session.stamp}"
        thread = {"external_id": f"aider:{sid}", "title": title[:300]}
        previous = None
        out = []
        seen_user = False
        for ordinal, block in enumerate(session.blocks):
            text = block.text
            if block.role == "tool":
                for line in block.lines:
                    if found := history.MODEL.match(line):
                        model = found.group(1)
                    if found := history.VERSION.match(line):
                        version = found.group(1)
            if not text:
                continue
            if block.role == "tool" and (not seen_user or not self.tool_output):
                # The startup banner (version, model, repo) is session metadata.
                continue
            if block.role == "user":
                seen_user = True
                if text == "<blank>":
                    self.stats["blank_inputs"] = self.stats.get("blank_inputs", 0) + 1
                    continue
            when = times.match(text, before=next_start) if block.role == "user" else None
            estimated = when is None
            if when is None:
                when = (previous + timedelta(seconds=1)) if previous else started
            previous = when
            truncated = len(text) > TEXT_LIMIT
            content = text[:TEXT_LIMIT]
            kind = {"user": "owner", "assistant": "agent"}.get(block.role, "other")
            message_model = model if block.role == "assistant" else None
            out.append(
                dict(
                    channel=CHANNEL,
                    external_id=f"aider:{sid}:{ordinal}",
                    thread=thread,
                    happened_at=iso(when),
                    author=self.author(block.role, model),
                    content=content,
                    fields=dict(
                        role=block.role,
                        author_kind=kind,
                        model=message_model,
                        repo=repo,
                        history_file=key,
                        session_id=sid,
                        session_started_at=iso(started),
                        ordinal=ordinal,
                        is_command=block.role == "user" and content.startswith("/"),
                        aider_version=version,
                        time_estimated=estimated,
                        text_length=len(text),
                        text_truncated=truncated,
                        mapping_revision=MAPPING_REVISION,
                    ),
                    payload=dict(
                        history_file=key,
                        session_offset=session.offset,
                        session_header=session.header,
                        ordinal=ordinal,
                        role=block.role,
                        text=content,
                        model=message_model,
                        at=iso(when),
                        time_estimated=estimated,
                    ),
                )
            )
        thread["metadata"] = {
            "kind": "aider_session",
            "repo": repo,
            "started_at": iso(started),
            "aider_version": version,
            "model": model,
        }
        return out

    def records(self, cursor, mode):
        state = copy.deepcopy(cursor or {})
        if state.get("mapping_revision") != MAPPING_REVISION or state.get("tool_output") != self.tool_output:
            state = {"mapping_revision": MAPPING_REVISION, "tool_output": self.tool_output}
        files = state.setdefault("files", {})
        self.stats = dict(files=0, sessions=0, messages=0)
        pending = 0
        for path in self.history_files():
            self.stats["files"] += 1
            key = str(path)
            size = path.stat().st_size
            offset = int((files.get(key) or {}).get("offset") or 0)
            if offset > size:
                offset = 0  # Truncated or replaced: read it again.
            with open(path, "rb") as handle:
                handle.seek(offset)
                data = handle.read()
            if offset and not history.HEADER.match(data.split(b"\n", 1)[0].rstrip(b"\r")):
                offset = 0
                with open(path, "rb") as handle:
                    data = handle.read()
            sessions, skipped = history.parse(data, offset)
            if skipped:
                self.stats["lines_before_first_session"] = self.stats.get("lines_before_first_session", 0) + skipped
            if not sessions:
                continue
            times = self.input_times(path)
            for index, session in enumerate(sessions):
                last = index == len(sessions) - 1
                next_start = None if last else history.local_time(sessions[index + 1].stamp, self.tz)
                messages = self.session_messages(path, session, times, next_start)
                self.stats["sessions"] += 1
                self.stats["messages"] += len(messages)
                yield from messages
                pending += len(messages)
                # Resume at the next session, or re-read this one if it is the
                # file's last (it may still be growing).
                files[key] = {"offset": session.offset if last else sessions[index + 1].offset}
                if pending >= self.batch_size or last:
                    pending = 0
                    yield Checkpoint(cursor=copy.deepcopy(state))


def main(argv=None):
    run(Aider, argv)
