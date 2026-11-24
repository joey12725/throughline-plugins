"""Cursor AI chat and composer history as a Throughline source.

Reads the SQLite stores Cursor keeps under its `User` directory (copied to a
temporary file first, never opened in place) and yields one message per user
prompt and assistant reply. Your prompts carry author_kind `owner`, assistant
replies author_kind `agent` with the model name.

Each run re-reads the stores when their files changed; a conversation is
mapped again only when its content fingerprint changed, and every message of
a changed conversation is re-sent (unchanged messages are no-ops in the core,
changed ones become new versions).
"""

import copy
import hashlib
import json
import logging
import sqlite3
from pathlib import Path

from throughline_sdk import Channel, Checkpoint, Declaration, Manifest, Plugin, run

from . import store

__all__ = ["Cursor", "MAPPING_REVISION", "main"]

MAPPING_REVISION = 1
CHANNEL = "cursor.messages"
TEXT_LIMIT = 100_000
log = logging.getLogger("throughline_cursor")


def fields(**values):
    return [dict(name=k, type=v[0], description=v[1]) for k, v in values.items()]


MESSAGE_FIELDS = fields(
    role=("text", "user or assistant"),
    author_kind=("text", "owner (you typed it) or agent (an AI model's reply)"),
    model=("text", "Model that answered (assistant turns), as Cursor names it"),
    chat_id=("text", "Cursor composer id or chat tab id"),
    bubble_id=("text", "Cursor bubble (message) id"),
    title=("text", "Conversation title"),
    mode=("text", "Composer mode: agent, chat, edit, ... (chat for the legacy panel)"),
    workspace=("text", "Workspace folder the conversation belongs to, when known"),
    source_format=(
        "text",
        "Store shape the record came from: composer_v<N>, composer_inline_v<N>, "
        "composer_workspace, legacy_chat or prompt_history",
    ),
    time_estimated=(
        "boolean",
        "Cursor kept no time for this message: it is placed just after the previous one",
    ),
    input_tokens=("integer", "Input tokens Cursor recorded for the turn"),
    output_tokens=("integer", "Output tokens Cursor recorded for the turn"),
    text_length=("integer", "Characters of the message text"),
    text_truncated=("boolean", "The text was cut to 100,000 characters"),
    mapping_revision=("integer", "Mapping revision that produced this record"),
)

SETTINGS = [
    ("user_dir", "CURSOR_USER_DIR", None,
     "Cursor's User directory (holds globalStorage/ and workspaceStorage/); "
     "default: the platform's standard location"),
    ("batch_size", "CURSOR_BATCH_SIZE", "500", "Messages per checkpoint (whole conversations)"),
    ("owner_identifier", "CURSOR_OWNER_IDENTIFIER", "cursor:owner:owner",
     "Author identifier of your own prompts, system:kind:value (e.g. email:address:you@example.com)"),
    ("owner_name", "CURSOR_OWNER_NAME", None, "Display name on your prompts"),
]


def iso(when):
    return when.isoformat(timespec="milliseconds")


def fingerprint(value):
    text = json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class Cursor(Plugin):
    manifest = Manifest(
        name="cursor",
        version="0.1.0",
        channels=[
            Channel(
                name=CHANNEL,
                description=(
                    "Cursor AI chat and composer (agent) conversations: your prompts "
                    "(author_kind owner) and the model's replies (author_kind agent, "
                    "with the model name); one thread per conversation"
                ),
                fields=MESSAGE_FIELDS,
                conversation_gap_s=1800,
            )
        ],
        settings={
            key: Declaration(env=env, required=False, default=default, description=description)
            for key, env, default, description in SETTINGS
        },
        secrets={},
        run_mode="poll",
        default_interval_s=900,
    )

    def __init__(self):
        super().__init__()
        self.user_dir = Path(self.settings["user_dir"] or store.default_user_dir()).expanduser()
        self.batch_size = max(1, int(self.settings["batch_size"] or 500))
        identifier = self.settings["owner_identifier"] or "cursor:owner:owner"
        parts = identifier.split(":", 2)
        if len(parts) != 3 or not all(parts):
            raise ValueError("CURSOR_OWNER_IDENTIFIER must be system:kind:value")
        self.owner = dict(
            system=parts[0], kind=parts[1], value=parts[2], display_name=self.settings["owner_name"]
        )
        self.stats = {}

    def execute(self, client, mode, dry_run=False):
        report = super().execute(client, mode, dry_run)
        if self.stats:
            report["cursor"] = self.stats
        return report

    def author(self, message):
        if message["role"] == "user":
            return dict(self.owner)
        model = message.get("model")
        return dict(
            system="cursor",
            kind="agent",
            value=model or "assistant",
            display_name=model or "Cursor assistant",
        )

    def map_message(self, conv, message):
        text = message["text"].strip()
        truncated = len(text) > TEXT_LIMIT
        payload = {
            "conversation": conv["id"],
            "conversation_key": conv["key"],
            "bubble": message["id"],
            "role": message["role"],
            "text": text[:TEXT_LIMIT],
            "model": message.get("model"),
            "at": iso(message["at"]),
            "time_estimated": bool(message.get("estimated")),
            "format": conv["format"],
            "input_tokens": message.get("input_tokens"),
            "output_tokens": message.get("output_tokens"),
        }
        title = conv.get("title") or "Untitled conversation"
        return dict(
            channel=CHANNEL,
            external_id=f"{conv['key']}:{message['id']}",
            thread={
                "external_id": conv["key"],
                "title": title[:300],
                "metadata": {
                    "kind": "cursor_conversation",
                    "workspace": conv.get("workspace"),
                    "mode": conv.get("mode"),
                },
            },
            happened_at=iso(message["at"]),
            author=self.author(message),
            content=text[:TEXT_LIMIT],
            fields=dict(
                role=message["role"],
                author_kind="owner" if message["role"] == "user" else "agent",
                model=message.get("model"),
                chat_id=conv["id"],
                bubble_id=message["id"],
                title=conv.get("title"),
                mode=conv.get("mode"),
                workspace=conv.get("workspace"),
                source_format=conv["format"],
                time_estimated=bool(message.get("estimated")),
                input_tokens=message.get("input_tokens"),
                output_tokens=message.get("output_tokens"),
                text_length=len(text),
                text_truncated=truncated,
                mapping_revision=MAPPING_REVISION,
            ),
            payload=payload,
        )

    def records(self, cursor, mode):
        state = copy.deepcopy(cursor or {})
        if state.get("mapping_revision") != MAPPING_REVISION or state.get("user_dir") != str(self.user_dir):
            state = {"mapping_revision": MAPPING_REVISION, "user_dir": str(self.user_dir)}
        done = state.setdefault("done", {})
        reader = store.Reader(self.user_dir)
        paths = reader.store_paths()
        if not paths:
            raise FileNotFoundError(
                f"no Cursor stores under {self.user_dir} (set CURSOR_USER_DIR to Cursor's User directory)"
            )
        files = store.signature(paths)
        self.stats = dict(conversations=0, changed=0, messages=0)
        if files == state.get("files"):
            self.stats["unchanged_stores"] = True
            return
        try:
            conversations = reader.conversations()
        except (sqlite3.DatabaseError, OSError) as error:
            raise RuntimeError(f"Cursor stores could not be read: {type(error).__name__}: {error}") from None
        pending = 0
        for conv in conversations:
            self.stats["conversations"] += 1
            if not store.estimate_times(conv):
                reader.skipped["untimed_conversation"] += 1
                continue
            mapped = [self.map_message(conv, m) for m in conv["messages"]]
            mark = fingerprint(mapped)
            if done.get(conv["key"]) == mark:
                continue
            self.stats["changed"] += 1
            for message in mapped:
                yield message
            self.stats["messages"] += len(mapped)
            done[conv["key"]] = mark
            pending += len(mapped)
            if pending >= self.batch_size:
                pending = 0
                yield Checkpoint(cursor=copy.deepcopy(state))
        if reader.warnings:
            log.warning(
                "Cursor: skipped records of unknown shape (the store format may have changed): %s",
                dict(sorted(reader.warnings.items())),
            )
        self.stats["unknown_shapes"] = dict(reader.warnings)
        self.stats["skipped"] = dict(reader.skipped)
        state["files"] = files
        yield Checkpoint(cursor=copy.deepcopy(state))


def main(argv=None):
    run(Cursor, argv)
