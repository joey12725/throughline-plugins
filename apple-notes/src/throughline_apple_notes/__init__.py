"""macOS Apple Notes as versioned pages, read from a copy of NoteStore.sqlite."""

from __future__ import annotations

import base64
import datetime as dt
import gzip
import hashlib
import json
import logging

from throughline_sdk import (
    Channel,
    Checkpoint,
    Declaration,
    Manifest,
    Plugin,
    Record,
    coverage,
    run,
)

from .proto import DecodeError, note_proto, render
from .store import DEFAULT_DB, TRASH_FOLDER_TYPE, Store, apple_time, snapshot

MAPPING_REVISION = 1
NOTES = "apple_notes.notes"
LOG = logging.getLogger("throughline_apple_notes")


def fields(*items):
    return [dict(name=name, type=kind, description=description) for name, kind, description in items]


def setting(env, default, description):
    return Declaration(env=env, required=False, default=default, description=description)


def pack(value):
    raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    return base64.b64encode(gzip.compress(raw, mtime=0)).decode("ascii")


def unpack(text):
    return json.loads(gzip.decompress(base64.b64decode(text))) if text else {}


class AppleNotes(Plugin):
    manifest = Manifest(
        name="apple_notes",
        version="0.1.0",
        run_mode="poll",
        default_interval_s=900,
        channels=[
            Channel(
                name=NOTES,
                description=(
                    "Apple Notes (iCloud and On My Mac accounts) read from the local NoteStore; one immutable "
                    "version per observed edit, plus a tombstone when a note is purged. Locked notes are skipped"
                ),
                is_reference=True,
                record_kind="page_revision",
                passage_text_paths=["/content/title", "/content/body"],
                fields=fields(
                    ("title", "text", "Note title"),
                    ("folder", "text", "Folder name"),
                    ("folder_path", "text", "Folder path from the account root, / separated"),
                    ("folder_id", "text", "Folder identifier"),
                    ("account", "text", "Account name (iCloud, On My Mac, ...)"),
                    ("account_id", "text", "Account identifier"),
                    ("created", "timestamp", "Note creation time"),
                    ("modified", "timestamp", "Note modification time of this revision"),
                    ("snippet", "text", "Apple's preview snippet"),
                    ("in_trash", "boolean", "Note is in Recently Deleted"),
                    ("is_pinned", "boolean", "Note is pinned"),
                    ("is_shared", "boolean", "Note is shared through iCloud collaboration"),
                    ("links", "json", "Link URLs in the body"),
                    ("attachments", "json", "Type identifiers (UTIs) of inline attachments"),
                    ("checklist_total", "integer", "Checklist items"),
                    ("checklist_done", "integer", "Checked checklist items"),
                    ("content_sha256", "text", "SHA-256 of the rendered body"),
                    ("content_truncated", "boolean", "Body was cut at APPLE_NOTES_MAX_CHARS"),
                    ("deleted", "boolean", "Note was purged from the store (tombstone version)"),
                    ("author_kind", "text", "owner (shared notes may include edits by collaborators)"),
                    ("mapping_revision", "integer", "Mapping revision that produced this record"),
                ),
            )
        ],
        settings={
            "db": setting("APPLE_NOTES_DB", DEFAULT_DB, "Path to NoteStore.sqlite (copied before reading)"),
            "include_trash": setting("APPLE_NOTES_INCLUDE_TRASH", "true", "Keep notes in Recently Deleted (flagged in_trash)"),
            "max_chars": setting("APPLE_NOTES_MAX_CHARS", "200000", "Body characters kept per revision"),
            "checkpoint_every": setting("APPLE_NOTES_CHECKPOINT_EVERY", "200", "Changed notes per checkpoint"),
        },
        link_types=["part_of"],
    )

    def __init__(self):
        super().__init__()
        self.include_trash = (self.settings.get("include_trash") or "").lower() in ("1", "true", "yes")
        self.max_chars = positive(self.settings.get("max_chars"), "APPLE_NOTES_MAX_CHARS")
        self.every = positive(self.settings.get("checkpoint_every"), "APPLE_NOTES_CHECKPOINT_EVERY")
        self.now = None

    def records(self, cursor, mode):
        """Cursor: {"v": 1, "notes": packed {note id: [modified, sha256[:16],
        title, folder pk]}}.
        Every run reads the whole (small) store; only notes whose
        modification date moved are decoded and hashed."""
        known = unpack((cursor or {}).get("notes"))
        counts = {"revisions": 0, "unchanged": 0, "locked_skipped": 0, "undecodable": 0, "trash_skipped": 0, "deleted": 0}
        seen = set()
        since = 0
        with snapshot(self.settings["db"]) as conn:
            store = Store(conn)
            for note in store.notes():
                ident = str(note["ZIDENTIFIER"])
                if note["deleted"]:
                    continue  # marked for deletion: recorded as a tombstone below
                seen.add(ident)
                if note["locked"]:
                    counts["locked_skipped"] += 1
                    continue  # keep any earlier unlocked version as it is
                folder = note["folder_row"]
                in_trash = folder.get("type") == TRASH_FOLDER_TYPE
                if in_trash and not self.include_trash:
                    counts["trash_skipped"] += 1
                    seen.discard(ident)
                    continue
                modified = note["modified"]
                before = known.get(ident)
                if before and before[0] == modified and before[3:4] == [note["folder"]]:
                    counts["unchanged"] += 1
                    continue
                try:
                    text, runs = note_proto(note["data"])
                    body, summary = render(text, runs)
                except (DecodeError, OSError, EOFError, ValueError) as error:
                    LOG.warning("Apple note body could not be decoded: %s", type(error).__name__)
                    counts["undecodable"] += 1
                    continue
                message = self.map_note(note, body, summary, in_trash)
                digest = message["fields"]["content_sha256"][:16]
                known[ident] = [modified, digest, message["fields"]["title"][:120], note["folder"]]
                yield message
                if folder.get("ZIDENTIFIER"):
                    yield folder_link(message, folder)
                counts["revisions"] += 1
                since += 1
                if since >= self.every:
                    since = 0
                    yield Checkpoint(cursor={"v": 1, "notes": pack(known)})
        observed = self.now or dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()
        for ident in sorted(set(known) - seen):
            before = known.pop(ident)
            counts["deleted"] += 1
            yield tombstone(ident, observed, before[2] if len(before) > 2 else None)
        yield coverage(NOTES, status="complete", rows_delivered=counts["revisions"] + counts["deleted"],
                       verified=True, detail=counts)
        yield Checkpoint(cursor={"v": 1, "notes": pack(known)})

    def map_note(self, note, body, summary, in_trash):
        ident = str(note["ZIDENTIFIER"])
        folder = note["folder_row"]
        account = note["account_row"]
        lines = body.split("\n", 1)
        title = (note["note_title"] or "").strip() or lines[0].lstrip("# ").strip() or "Untitled note"
        # The first line repeats the title; keep the rest as the body.
        if lines and lines[0].lstrip("# ").strip() == title:
            body = lines[1].strip("\n") if len(lines) > 1 else ""
        truncated = len(body) > self.max_chars
        if truncated:
            body = body[: self.max_chars]
        digest = hashlib.sha256((title + "\n" + body).encode()).hexdigest()
        modified = apple_time(note["modified"]) or apple_time(note["created"])
        created = apple_time(note["created"])
        if modified is None:
            raise ValueError("Apple note has no dates")
        values = {
            "title": title,
            "folder": folder.get("title"),
            "folder_path": folder.get("path"),
            "folder_id": folder.get("ZIDENTIFIER"),
            "account": account.get("name"),
            "account_id": account.get("id"),
            "created": created.isoformat() if created else None,
            "modified": modified.isoformat(),
            "snippet": note["snippet"],
            "in_trash": in_trash,
            "is_pinned": bool(note["pinned"]),
            "is_shared": bool(note["shared"]),
            "links": summary["links"],
            "attachments": summary["attachments"],
            "checklist_total": summary["checklist_total"],
            "checklist_done": summary["checklist_done"],
            "content_sha256": digest,
            "content_truncated": truncated,
            "deleted": False,
            "author_kind": "owner",
            "mapping_revision": MAPPING_REVISION,
        }
        return {
            "channel": NOTES,
            "external_id": ident,
            "thread": {"external_id": ident, "title": title},
            "happened_at": modified.isoformat(),
            "content": "\n\n".join(part for part in (title, body) if part),
            "fields": values,
            "payload": {
                "note": {k: v for k, v in values.items() if k not in ("mapping_revision", "author_kind")},
                "content": {"title": title, "body": body},
            },
        }


def tombstone(ident, observed, title=None):
    title = title or "Deleted note"
    return {
        "channel": NOTES,
        "external_id": ident,
        "thread": {"external_id": ident, "title": title},
        "happened_at": observed,
        "content": f"{title}\n\nDeleted: this note is no longer in the Apple Notes store.",
        "fields": {"title": title, "deleted": True, "author_kind": "owner", "mapping_revision": MAPPING_REVISION},
        "payload": {"note": {"deleted": True, "observed_at": observed}, "content": {"title": title, "body": ""}},
    }


def folder_link(message, folder):
    return Record(kind="link", data={
        "type": "part_of",
        "from": {"channel": NOTES, "external_id": message["external_id"]},
        "to": {"kind": "notes_folder", "external_ref": "apple_notes:folder:" + str(folder["ZIDENTIFIER"]),
               "name": folder.get("path") or folder.get("title")},
        "confidence": 1,
        "method": "source",
        "evidence": {"via": "folder"},
    })


def positive(value, name):
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a positive integer") from None
    if number < 1:
        raise ValueError(f"{name} must be a positive integer")
    return number


def main(argv=None):
    run(AppleNotes, argv)


__all__ = ["AppleNotes", "MAPPING_REVISION", "NOTES", "main"]
