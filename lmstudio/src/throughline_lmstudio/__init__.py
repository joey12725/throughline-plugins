"""LM Studio conversations as a Throughline source.

Reads LM Studio's conversation files (`*.conversation.json`) and yields one
message per turn version: your messages (author_kind owner) and model
replies (agent, with the model). Regenerated replies are kept as separate
versions; `is_selected` marks the one LM Studio shows.

The file format is undocumented and has changed between releases; parsing
is defensive and skips (and counts) shapes it does not know. LM Studio keeps
no per-message times: see `time_source` and the README.

Change detection: a file is read again only when its mtime or size changed,
and mapped again only when its content hash changed. A changed conversation
re-sends every message (unchanged ones are no-ops in the core; edited or
re-selected ones become new versions).
"""

import copy
import hashlib
import json
import logging
import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from throughline_sdk import Channel, Checkpoint, Declaration, Manifest, Plugin, run

__all__ = ["LMStudio", "MAPPING_REVISION", "main"]

MAPPING_REVISION = 1
CHANNEL = "lmstudio.messages"
TEXT_LIMIT = 100_000
SUFFIX = ".conversation.json"
log = logging.getLogger("throughline_lmstudio")


def fields(**values):
    return [dict(name=k, type=v[0], description=v[1]) for k, v in values.items()]


MESSAGE_FIELDS = fields(
    role=("text", "user, assistant or system"),
    author_kind=("text", "owner (you wrote it) or agent (a model's reply)"),
    model=("text", "Model that generated the reply, as LM Studio identifies it"),
    conversation_file=("text", "Conversation file name (its stem is the conversation key)"),
    folder=("text", "LM Studio folder the conversation is in, if any"),
    chat_title=("text", "Conversation name"),
    ordinal=("integer", "Position of the turn in the conversation"),
    version_index=("integer", "Version of the turn (regenerations and edits add versions)"),
    version_count=("integer", "Versions of this turn"),
    is_selected=("boolean", "The version LM Studio currently shows"),
    time_source=(
        "text",
        "message (the file had a time for it), created (estimated from the conversation's "
        "creation, one second per turn), seen (estimated: the file's modification time when "
        "the plugin first saw the turn) or file (no creation time: the file's modification time)",
    ),
    prompt_tokens=("integer", "Prompt tokens LM Studio reported"),
    predicted_tokens=("integer", "Generated tokens LM Studio reported"),
    tokens_per_second=("number", "Generation speed LM Studio reported"),
    file_count=("integer", "Files attached to the message"),
    has_reasoning=("boolean", "The reply had a reasoning (thinking) block, left out of the text"),
    text_length=("integer", "Characters of the message text"),
    text_truncated=("boolean", "The text was cut to 100,000 characters"),
    mapping_revision=("integer", "Mapping revision that produced this record"),
)

SETTINGS = [
    ("dirs", "LMSTUDIO_CONVERSATIONS_DIRS", None,
     "Conversation folders, separated by the OS path separator; default: "
     "~/.lmstudio/conversations and ~/.cache/lm-studio/conversations (whichever exist)"),
    ("batch_size", "LMSTUDIO_BATCH_SIZE", "500", "Messages per checkpoint (whole conversations)"),
    ("owner_identifier", "LMSTUDIO_OWNER_IDENTIFIER", "lmstudio:owner:owner",
     "Author identifier of your own messages, system:kind:value"),
    ("owner_name", "LMSTUDIO_OWNER_NAME", None, "Display name on your messages"),
]


def default_dirs():
    home = Path.home()
    return [home / ".lmstudio" / "conversations", home / ".cache" / "lm-studio" / "conversations"]


def moment(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if value > 1e11:
            value = value / 1000
        if not 1e9 <= value < 1e10:
            return None
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


def ms(when):
    return int(when.timestamp() * 1000)


def own_time(*objects):
    for obj in objects:
        if isinstance(obj, dict):
            for key in ("timestamp", "createdAt", "sentAt", "time"):
                when = moment(obj.get(key))
                if when:
                    return when
    return None


class Parser:
    """Turns one conversation document into turns of versions."""

    def __init__(self):
        self.warnings = Counter()

    def parts(self, content):
        """(text, file count) of a content value: a string or a part list."""
        if isinstance(content, str):
            return content, 0
        if not isinstance(content, list):
            return "", 0
        texts, files = [], 0
        for part in content:
            if isinstance(part, str):
                texts.append(part)
            elif isinstance(part, dict):
                kind = part.get("type")
                if kind == "text" and isinstance(part.get("text"), str):
                    texts.append(part["text"])
                elif kind in ("file", "image"):
                    files += 1
                else:
                    self.warnings["unknown_content_part"] += 1
        return "\n".join(texts), files

    def version(self, version, fallback_model):
        if not isinstance(version, dict) or version.get("role") not in ("user", "assistant", "system"):
            self.warnings["unknown_version"] += 1
            return None
        sender = version.get("senderInfo") if isinstance(version.get("senderInfo"), dict) else {}
        gen = version.get("genInfo") if isinstance(version.get("genInfo"), dict) else {}
        texts, files, reasoning = [], 0, False
        steps = version.get("steps")
        if isinstance(steps, list):
            for step in steps:
                if not isinstance(step, dict):
                    self.warnings["unknown_step"] += 1
                    continue
                if step.get("type") != "contentBlock":
                    continue  # Tool calls, debug info, ...
                style = step.get("style") if isinstance(step.get("style"), dict) else {}
                text, count = self.parts(step.get("content"))
                files += count
                if style.get("type") == "thinking":
                    reasoning = True
                    continue
                if isinstance(step.get("genInfo"), dict):
                    gen = step["genInfo"]
                texts.append(text)
        elif "content" in version:
            text, files = self.parts(version.get("content"))
            texts.append(text)
        else:
            self.warnings["unknown_version"] += 1
            return None
        stats = gen.get("stats") if isinstance(gen.get("stats"), dict) else {}
        role = version["role"]
        model = None
        if role == "assistant":
            model = (
                gen.get("indexedModelIdentifier") or gen.get("identifier")
                or sender.get("senderName") or fallback_model
            )
        return dict(
            role=role,
            text="\n\n".join(t.strip() for t in texts if t and t.strip()),
            files=files,
            reasoning=reasoning,
            model=model,
            at=own_time(version),
            prompt_tokens=stats.get("promptTokensCount"),
            predicted_tokens=stats.get("predictedTokensCount"),
            tokens_per_second=stats.get("tokensPerSecond"),
        )

    def turns(self, data):
        last = data.get("lastUsedModel") if isinstance(data.get("lastUsedModel"), dict) else {}
        fallback = last.get("indexedModelIdentifier") or last.get("identifier")
        messages = data.get("messages")
        if not isinstance(messages, list):
            self.warnings["unknown_conversation"] += 1
            return None
        out = []
        for message in messages:
            if not isinstance(message, dict):
                self.warnings["unknown_message"] += 1
                out.append(None)
                continue
            versions = message.get("versions")
            if not isinstance(versions, list):
                versions = [message] if "role" in message else []
            selected = message.get("currentlySelected", 0)
            if not isinstance(selected, int) or not 0 <= selected < len(versions):
                selected = 0
            parsed = [self.version(v, fallback) for v in versions]
            if not any(parsed):
                self.warnings["unknown_message"] += 1
                out.append(None)
                continue
            out.append(dict(versions=parsed, selected=selected, at=own_time(message)))
        return out


class LMStudio(Plugin):
    manifest = Manifest(
        name="lmstudio",
        version="0.1.0",
        channels=[
            Channel(
                name=CHANNEL,
                description=(
                    "LM Studio chats with local models: your messages (author_kind owner) "
                    "and model replies (agent, with the model); every version of a "
                    "regenerated turn, is_selected marks the one shown; one thread per "
                    "conversation"
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
        configured = self.settings["dirs"]
        self.dirs = (
            [Path(p).expanduser() for p in configured.split(os.pathsep) if p.strip()]
            if configured
            else default_dirs()
        )
        self.batch_size = max(1, int(self.settings["batch_size"] or 500))
        parts = (self.settings["owner_identifier"] or "lmstudio:owner:owner").split(":", 2)
        if len(parts) != 3 or not all(parts):
            raise ValueError("LMSTUDIO_OWNER_IDENTIFIER must be system:kind:value")
        self.owner = dict(system=parts[0], kind=parts[1], value=parts[2], display_name=self.settings["owner_name"])
        self.stats = {}

    def execute(self, client, mode, dry_run=False):
        report = super().execute(client, mode, dry_run)
        if self.stats:
            report["lmstudio"] = self.stats
        return report

    def files(self):
        existing = [d for d in self.dirs if d.is_dir()]
        if not existing:
            raise FileNotFoundError(
                "no LM Studio conversations folder found (set LMSTUDIO_CONVERSATIONS_DIRS)"
            )
        found = {}
        for root in existing:
            for path in sorted(root.rglob("*" + SUFFIX)):
                key = path.name[: -len(SUFFIX)]
                folder = path.parent.relative_to(root).as_posix()
                found.setdefault(key, (path, None if folder == "." else folder))  # First folder wins.
        return found

    def author(self, role, model):
        if role == "assistant":
            return dict(system="lmstudio", kind="agent", value=model or "assistant",
                        display_name=model or "LM Studio model")
        return dict(self.owner)

    def map_conversation(self, key, path, folder, data, turns, entry, mtime):
        created = moment(data.get("createdAt"))
        anchors = entry.get("anchors")
        if not anchors:
            anchors = [[0, ms(created) if created else mtime, "created" if created else "file"]]
        seen = entry.get("seen", 0)
        if len(turns) > seen and seen and anchors[-1][0] < seen:
            # Turns added since the last read: first seen at this modification time.
            anchors.append([seen, mtime, "seen"])
        anchors = [a for a in anchors if a[0] < max(1, len(turns))]
        entry["anchors"], entry["seen"] = anchors, len(turns)
        title = data.get("name") or "Untitled conversation"
        system_prompt = data.get("systemPrompt") if isinstance(data.get("systemPrompt"), str) else None
        thread = {
            "external_id": f"lmstudio:{key}",
            "title": str(title)[:300],
            "metadata": {
                "kind": "lmstudio_conversation",
                "folder": folder,
                "pinned": bool(data.get("pinned")),
                "system_prompt": (system_prompt or None) and system_prompt[:2000],
            },
        }
        out = []
        previous = None
        for ordinal, turn in enumerate(turns):
            if turn is None:
                continue
            anchor = next(a for a in reversed(anchors) if a[0] <= ordinal)
            estimate = datetime.fromtimestamp(anchor[1] / 1000, timezone.utc) + timedelta(
                seconds=ordinal - anchor[0]
            )
            if previous and estimate <= previous:
                estimate = previous + timedelta(seconds=1)
            turn_time = None
            for index, version in enumerate(turn["versions"]):
                if version is None:
                    continue
                when = version["at"] or turn["at"]
                source = "message" if when else anchor[2]
                when = when or estimate
                text = version["text"]
                truncated = len(text) > TEXT_LIMIT
                content = text[:TEXT_LIMIT] or f"(empty {version['role']} message)"
                selected = index == turn["selected"]
                if selected or turn_time is None:
                    turn_time = when
                out.append(
                    dict(
                        channel=CHANNEL,
                        external_id=f"lmstudio:{key}:{ordinal}.{index}",
                        thread=thread,
                        happened_at=when.isoformat(),
                        author=self.author(version["role"], version["model"]),
                        content=content,
                        fields=dict(
                            role=version["role"],
                            author_kind="agent" if version["role"] == "assistant" else "owner",
                            model=version["model"],
                            conversation_file=path.name,
                            folder=folder,
                            chat_title=str(title),
                            ordinal=ordinal,
                            version_index=index,
                            version_count=len(turn["versions"]),
                            is_selected=selected,
                            time_source=source,
                            prompt_tokens=version["prompt_tokens"] if isinstance(version["prompt_tokens"], int) else None,
                            predicted_tokens=version["predicted_tokens"] if isinstance(version["predicted_tokens"], int) else None,
                            tokens_per_second=version["tokens_per_second"] if isinstance(version["tokens_per_second"], (int, float)) else None,
                            file_count=version["files"],
                            has_reasoning=version["reasoning"],
                            text_length=len(text),
                            text_truncated=truncated,
                            mapping_revision=MAPPING_REVISION,
                        ),
                        payload=dict(
                            conversation=key,
                            ordinal=ordinal,
                            version=index,
                            selected=selected,
                            role=version["role"],
                            text=content,
                            model=version["model"],
                            at=when.isoformat(),
                            time_source=source,
                        ),
                    )
                )
            previous = max(turn_time, estimate) if turn_time else estimate
        return out

    def records(self, cursor, mode):
        state = copy.deepcopy(cursor or {})
        if state.get("mapping_revision") != MAPPING_REVISION:
            state = {"mapping_revision": MAPPING_REVISION}
        known = state.setdefault("conversations", {})
        parser = Parser()
        self.stats = dict(files=0, changed=0, messages=0, unreadable=0)
        pending = 0
        found = self.files()
        order = sorted(found.items(), key=lambda item: (item[1][0].stat().st_mtime_ns, item[0]))
        for key, (path, folder) in order:
            self.stats["files"] += 1
            stat = path.stat()
            entry = known.get(key) or {}
            signature = [stat.st_mtime_ns, stat.st_size]
            if entry.get("sig") == signature:
                continue
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()[:16]
            if entry.get("hash") == digest:
                entry["sig"] = signature
                known[key] = entry
                continue
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                # LM Studio may be mid-write: try again next run.
                self.stats["unreadable"] += 1
                continue
            if not isinstance(data, dict):
                parser.warnings["unknown_conversation"] += 1
                continue
            turns = parser.turns(data)
            if turns is None:
                continue
            entry = dict(entry)
            messages = self.map_conversation(key, path, folder, data, turns, entry, stat.st_mtime_ns // 1_000_000)
            entry.update(sig=signature, hash=digest)
            known[key] = entry
            self.stats["changed"] += 1
            self.stats["messages"] += len(messages)
            yield from messages
            pending += len(messages)
            if pending >= self.batch_size:
                pending = 0
                yield Checkpoint(cursor=copy.deepcopy(state))
        if parser.warnings:
            log.warning(
                "LM Studio: skipped parts of unknown shape (the file format may have changed): %s",
                dict(sorted(parser.warnings.items())),
            )
            self.stats["unknown_shapes"] = dict(parser.warnings)
        yield Checkpoint(cursor=copy.deepcopy(state))


def main(argv=None):
    run(LMStudio, argv)
