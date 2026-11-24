"""Read Cursor's chat stores (standard library only).

Cursor keeps its AI chat history in VS Code-style SQLite key/value stores
under its `User` directory. The format is undocumented and changes between
releases, so everything here is defensive: every value is parsed on its own,
unknown shapes are counted in `Reader.warnings` and skipped, never fatal.

Shapes understood (see the README for the full list):

* globalStorage/state.vscdb, table cursorDiskKV
  - `composerData:<composerId>` with an inline `conversation` list
    (composer `_v` 1-2), or with `fullConversationHeadersOnly` whose bubbles
    live under `bubbleId:<composerId>:<bubbleId>` (`_v` 3 and later)
* workspaceStorage/<hash>/state.vscdb, table ItemTable
  - `composer.composerData`: the workspace's composer list (maps composers to
    the workspace folder; very old releases kept whole conversations here)
  - `workbench.panel.aichat.view.aichat.chatdata`: the pre-composer chat
    panel (`tabs[].bubbles[]`)
  - `aiService.prompts`: prompt history; it has no timestamps or ids, so
    entries are counted (`untimed_prompt`) unless they carry a time

The databases are live and in WAL mode: each one (with its -wal file) is
copied to a temporary directory and read from the copy, never opened in
place.
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

GLOBAL_DB = Path("globalStorage") / "state.vscdb"
WORKSPACES = Path("workspaceStorage")
LEGACY_CHAT_KEY = "workbench.panel.aichat.view.aichat.chatdata"
COMPOSER_LIST_KEY = "composer.composerData"
PROMPTS_KEY = "aiService.prompts"


def default_user_dir():
    """Cursor's `User` directory on this operating system."""
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Cursor" / "User"
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(home / "AppData" / "Roaming")
        return Path(base) / "Cursor" / "User"
    base = os.environ.get("XDG_CONFIG_HOME") or str(home / ".config")
    return Path(base) / "Cursor" / "User"


@contextmanager
def snapshot(db):
    """A connection to a private copy of a (possibly locked) SQLite store."""
    with tempfile.TemporaryDirectory(prefix="throughline-cursor-") as tmp:
        target = Path(tmp) / "state.vscdb"
        shutil.copyfile(db, target)
        wal = Path(str(db) + "-wal")
        try:
            shutil.copyfile(wal, str(target) + "-wal")
        except FileNotFoundError:
            pass
        conn = sqlite3.connect(target)
        try:
            yield conn
        finally:
            conn.close()


def signature(paths):
    """(mtime_ns, size) of each store file and its WAL: unchanged files are
    not copied or read again."""
    out = {}
    for path in paths:
        for file in (Path(path), Path(str(path) + "-wal")):
            try:
                stat = file.stat()
            except FileNotFoundError:
                continue
            out[str(file)] = [stat.st_mtime_ns, stat.st_size]
    return out


def loads(value):
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        try:
            value = bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def has_table(conn, name):
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
    )


def prefixed(conn, table, prefix):
    """(key, value) rows whose key starts with prefix, using the key index."""
    upper = prefix[:-1] + chr(ord(prefix[-1]) + 1)
    return conn.execute(
        f"SELECT key, value FROM {table} WHERE key >= ? AND key < ? ORDER BY key",
        (prefix, upper),
    )


def item(conn, key):
    row = conn.execute("SELECT value FROM ItemTable WHERE key=?", (key,)).fetchone()
    return loads(row[0]) if row else None


def moment(value):
    """An aware datetime from epoch milliseconds/seconds or an ISO string."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if value > 1e11:
            value = value / 1000
        if not 1e9 <= value < 1e10:
            return None  # Not an epoch time (a performance counter, say).
        return datetime.fromtimestamp(value, timezone.utc)
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.isdigit():
            return moment(int(text))
        try:
            when = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        return when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    return None


def lexical_text(node):
    """Plain text of a Lexical editor state (`richText`)."""
    if isinstance(node, str):
        node = loads(node)
    if isinstance(node, dict) and "root" in node:
        node = node["root"]
    if not isinstance(node, dict):
        return ""
    if isinstance(node.get("text"), str):
        return node["text"]
    parts = [lexical_text(child) for child in node.get("children") or []]
    joiner = "\n" if node.get("type") in ("root",) else ""
    return joiner.join(p for p in parts if p is not None)


def folder_of(workspace_dir):
    meta = loads((workspace_dir / "workspace.json").read_text(encoding="utf-8")) if (
        workspace_dir / "workspace.json"
    ).is_file() else None
    uri = (meta or {}).get("folder") or (meta or {}).get("workspace")
    if not isinstance(uri, str):
        return None
    parsed = urlparse(uri)
    if parsed.scheme == "file":
        return unquote(parsed.path)
    return uri


class Reader:
    """Collects conversations from a Cursor `User` directory.

    A conversation is {key, id, title, mode, model, workspace, format,
    created, updated, messages: [{id, role, text, at, model, ...}]}; `at`
    is None when the store kept no time for the message (the mapper
    estimates one).
    """

    def __init__(self, user_dir):
        self.user_dir = Path(user_dir).expanduser()
        self.warnings = Counter()
        self.skipped = Counter()
        self.workspace_of = {}

    def store_paths(self):
        paths = []
        if (self.user_dir / GLOBAL_DB).is_file():
            paths.append(self.user_dir / GLOBAL_DB)
        if (self.user_dir / WORKSPACES).is_dir():
            paths.extend(sorted((self.user_dir / WORKSPACES).glob("*/state.vscdb")))
        return paths

    def conversations(self):
        found = {}
        for db in sorted((self.user_dir / WORKSPACES).glob("*/state.vscdb")) if (
            self.user_dir / WORKSPACES
        ).is_dir() else []:
            try:
                for conv in self.workspace_conversations(db):
                    found.setdefault(conv["key"], conv)
            except sqlite3.DatabaseError:
                self.warnings["unreadable_workspace_db"] += 1
        db = self.user_dir / GLOBAL_DB
        if db.is_file():
            with snapshot(db) as conn:
                if has_table(conn, "cursorDiskKV"):
                    for conv in self.global_conversations(conn):
                        found[conv["key"]] = conv  # The global store wins.
                else:
                    self.warnings["no_cursorDiskKV_table"] += 1
        for conv in found.values():
            if conv.get("workspace") is None:
                conv["workspace"] = self.workspace_of.get(conv["id"])
        return sorted(
            found.values(),
            key=lambda c: (c["updated"] or c["created"] or datetime.min.replace(tzinfo=timezone.utc), c["key"]),
        )

    # -- workspace stores --------------------------------------------------

    def workspace_conversations(self, db):
        folder = folder_of(db.parent)
        with snapshot(db) as conn:
            if not has_table(conn, "ItemTable"):
                self.warnings["no_ItemTable"] += 1
                return []
            out = []
            composers = item(conn, COMPOSER_LIST_KEY)
            if composers is not None:
                listed = composers.get("allComposers") if isinstance(composers, dict) else None
                if not isinstance(listed, list):
                    self.warnings["unknown_composer_list"] += 1
                    listed = []
                for entry in listed:
                    if not isinstance(entry, dict) or not entry.get("composerId"):
                        self.warnings["unknown_composer_list_entry"] += 1
                        continue
                    self.workspace_of[entry["composerId"]] = folder
                    if isinstance(entry.get("conversation"), list) and entry["conversation"]:
                        conv = self.composer(entry, inline_format="composer_workspace")
                        if conv:
                            conv["workspace"] = folder
                            out.append(conv)
            chat = item(conn, LEGACY_CHAT_KEY)
            if chat is not None:
                tabs = chat.get("tabs") if isinstance(chat, dict) else None
                if not isinstance(tabs, list):
                    self.warnings["unknown_legacy_chat"] += 1
                    tabs = []
                for tab in tabs:
                    conv = self.legacy_tab(tab)
                    if conv:
                        conv["workspace"] = folder
                        out.append(conv)
            prompts = item(conn, PROMPTS_KEY)
            if isinstance(prompts, list):
                timed = [p for p in prompts if isinstance(p, dict) and moment(p.get("unixMs") or p.get("timestamp"))]
                self.skipped["untimed_prompt"] += len(prompts) - len(timed)
                if timed:
                    conv = self.prompt_history(db.parent.name, timed)
                    conv["workspace"] = folder
                    out.append(conv)
            elif prompts is not None:
                self.warnings["unknown_prompt_history"] += 1
            return out

    def legacy_tab(self, tab):
        if not isinstance(tab, dict) or not tab.get("tabId") or not isinstance(tab.get("bubbles"), list):
            self.warnings["unknown_legacy_tab"] += 1
            return None
        messages = []
        for index, bubble in enumerate(tab["bubbles"]):
            if not isinstance(bubble, dict):
                self.warnings["unknown_legacy_bubble"] += 1
                continue
            role = {"user": "user", "ai": "assistant"}.get(bubble.get("type"))
            if role is None:
                self.warnings["unknown_legacy_bubble_type"] += 1
                continue
            text = bubble.get("text") or bubble.get("rawText") or ""
            if not isinstance(text, str) or not text.strip():
                self.skipped["empty"] += 1
                continue
            messages.append(
                dict(
                    id=str(bubble.get("id") or index),
                    role=role,
                    text=text,
                    at=moment(bubble.get("createdAt") or bubble.get("timestamp")),
                    model=bubble.get("modelType") if role == "assistant" else None,
                )
            )
        if not messages:
            return None
        sent = moment(tab.get("lastSendTime"))
        return dict(
            key="chat:" + str(tab["tabId"]),
            id=str(tab["tabId"]),
            title=tab.get("chatTitle") or None,
            mode="chat",
            model=None,
            workspace=None,
            format="legacy_chat",
            created=None,
            updated=sent,
            # Legacy tabs only record the last send: messages are placed just
            # before it, in order.
            anchor="end",
            messages=messages,
        )

    def prompt_history(self, workspace_hash, prompts):
        messages = [
            dict(
                id=str(p.get("id") or p.get("generationUUID") or index),
                role="user",
                text=p.get("text") or p.get("textDescription") or "",
                at=moment(p.get("unixMs") or p.get("timestamp")),
                model=None,
            )
            for index, p in enumerate(prompts)
            if (p.get("text") or p.get("textDescription"))
        ]
        return dict(
            key="prompts:" + workspace_hash,
            id=workspace_hash,
            title="Prompt history",
            mode="prompt_history",
            model=None,
            workspace=None,
            format="prompt_history",
            created=None,
            updated=max((m["at"] for m in messages), default=None),
            messages=messages,
        )

    # -- global store --------------------------------------------------------

    def global_conversations(self, conn):
        for key, value in prefixed(conn, "cursorDiskKV", "composerData:"):
            data = loads(value)
            if not isinstance(data, dict):
                self.warnings["unparseable_composer"] += 1
                continue
            data.setdefault("composerId", key.split(":", 1)[1])
            conv = self.composer(data, conn=conn)
            if conv:
                yield conv

    def composer(self, data, conn=None, inline_format=None):
        cid = str(data["composerId"])
        version = data.get("_v")
        headers = data.get("fullConversationHeadersOnly")
        inline = data.get("conversation")
        if isinstance(inline, list) and inline:
            bubbles = inline
            fmt = inline_format or f"composer_inline_v{version or 0}"
        elif isinstance(headers, list) and headers and conn is not None:
            bubbles = list(self.bubbles(conn, cid, headers))
            fmt = f"composer_v{version or 0}"
        elif isinstance(headers, list) or isinstance(inline, list):
            self.skipped["empty_conversation"] += 1
            return None
        else:
            self.warnings["unknown_composer_shape"] += 1
            return None
        model = (data.get("modelConfig") or {}).get("modelName") if isinstance(
            data.get("modelConfig"), dict
        ) else None
        messages = []
        for index, bubble in enumerate(bubbles):
            message = self.bubble(bubble, index, model)
            if message:
                messages.append(message)
        if not messages:
            self.skipped["empty_conversation"] += 1
            return None
        return dict(
            key="composer:" + cid,
            id=cid,
            title=data.get("name") or None,
            mode=data.get("unifiedMode") or data.get("forceMode") or None,
            model=model,
            workspace=None,
            format=fmt,
            created=moment(data.get("createdAt")),
            updated=moment(data.get("lastUpdatedAt")),
            messages=messages,
        )

    def bubbles(self, conn, cid, headers):
        stored = {}
        for key, value in prefixed(conn, "cursorDiskKV", f"bubbleId:{cid}:"):
            stored[key.rsplit(":", 1)[1]] = value
        for header in headers:
            bid = header.get("bubbleId") if isinstance(header, dict) else None
            if not bid:
                self.warnings["unknown_bubble_header"] += 1
                continue
            if bid not in stored:
                self.skipped["missing_bubble"] += 1
                continue
            bubble = loads(stored[bid])
            if not isinstance(bubble, dict):
                self.warnings["unparseable_bubble"] += 1
                continue
            bubble.setdefault("bubbleId", bid)
            bubble.setdefault("type", header.get("type"))
            yield bubble

    def bubble(self, bubble, index, model):
        if not isinstance(bubble, dict):
            self.warnings["unknown_bubble"] += 1
            return None
        role = {1: "user", 2: "assistant", "user": "user", "ai": "assistant", "assistant": "assistant"}.get(
            bubble.get("type")
        )
        if role is None:
            self.warnings["unknown_bubble_type"] += 1
            return None
        text = bubble.get("text")
        if not isinstance(text, str) or not text.strip():
            text = lexical_text(bubble.get("richText")) if bubble.get("richText") else ""
        if not text.strip():
            if bubble.get("toolFormerData"):
                self.skipped["tool_call"] += 1
            elif bubble.get("thinking"):
                self.skipped["thinking"] += 1
            else:
                self.skipped["empty"] += 1
            return None
        timing = bubble.get("timingInfo") if isinstance(bubble.get("timingInfo"), dict) else {}
        info = bubble.get("modelInfo") if isinstance(bubble.get("modelInfo"), dict) else {}
        tokens = bubble.get("tokenCount") if isinstance(bubble.get("tokenCount"), dict) else {}
        return dict(
            id=str(bubble.get("bubbleId") or index),
            role=role,
            text=text,
            at=moment(bubble.get("createdAt")) or moment(timing.get("clientStartTime")),
            model=(info.get("modelName") or bubble.get("model") or model) if role == "assistant" else None,
            input_tokens=tokens.get("inputTokens") or None,
            output_tokens=tokens.get("outputTokens") or None,
        )


def estimate_times(conv):
    """Fill missing message times so messages stay in order: the previous
    message's time plus a millisecond, starting at the conversation's
    creation (or, for legacy tabs, ending just before the last send).
    Returns False when no time at all is known for the conversation."""
    messages = conv["messages"]
    if conv.get("anchor") == "end" and conv["updated"] and not any(m["at"] for m in messages):
        last = conv["updated"]
        for offset, message in enumerate(reversed(messages)):
            message["at"] = last - timedelta(seconds=offset)
            message["estimated"] = True
        return True
    start = conv["created"] or next((m["at"] for m in messages if m["at"]), None) or conv["updated"]
    if start is None:
        return False
    previous = None
    for message in messages:
        if message["at"] is None:
            message["at"] = (previous + timedelta(seconds=1)) if previous else start
            message["estimated"] = True
        previous = message["at"]
    return True
