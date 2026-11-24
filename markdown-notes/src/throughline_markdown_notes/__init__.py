"""Markdown folders, Obsidian vaults and Logseq graphs as versioned pages."""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
from zoneinfo import ZoneInfo

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

from .parse import parse_note, parse_when
from .vault import DEFAULT_IGNORE, Index, daily_date, pack, page_name, parse_roots, scan, unpack

MAPPING_REVISION = 1
NOTES = "markdown_notes.notes"
LOG = logging.getLogger("throughline_markdown_notes")
SKIPPED = "-"  # state hash marker for a file that was too large or unreadable


def fields(*items):
    return [dict(name=name, type=kind, description=description) for name, kind, description in items]


def setting(env, default, description):
    return Declaration(env=env, required=False, default=default, description=description)


class MarkdownNotes(Plugin):
    manifest = Manifest(
        name="markdown_notes",
        version="0.1.0",
        run_mode="poll",
        default_interval_s=900,
        channels=[
            Channel(
                name=NOTES,
                description=(
                    "Markdown notes from local folders, Obsidian vaults and Logseq graphs; one immutable "
                    "version per observed content change, plus a tombstone version when a file disappears"
                ),
                is_reference=True,
                record_kind="page_revision",
                passage_text_paths=["/content/title", "/content/body"],
                fields=fields(
                    ("vault", "text", "Root label (vault, graph or folder name)"),
                    ("vault_format", "text", "obsidian, logseq or markdown"),
                    ("path", "text", "Path of the note inside its root, with / separators"),
                    ("title", "text", "Frontmatter/Logseq title, else the page name"),
                    ("aliases", "json", "Frontmatter aliases / Logseq alias:: values"),
                    ("tags", "json", "Frontmatter tags plus inline #tags"),
                    ("frontmatter", "json", "Parsed YAML frontmatter (dates as ISO strings)"),
                    ("properties", "json", "Logseq page properties (key:: value)"),
                    ("link_targets", "json", "Link targets as written: [[wikilinks]], ![[embeds]], relative Markdown links"),
                    ("is_daily", "boolean", "Daily note (Obsidian daily notes, Logseq journal, YYYY-MM-DD file name)"),
                    ("daily_date", "text", "Day a daily note is about (YYYY-MM-DD)"),
                    ("created", "timestamp", "Frontmatter created/date-created value"),
                    ("modified", "timestamp", "Frontmatter modified/updated value"),
                    ("mtime", "timestamp", "File modification time of this revision"),
                    ("size_bytes", "integer", "File size of this revision"),
                    ("content_sha256", "text", "SHA-256 of the file bytes"),
                    ("content_truncated", "boolean", "Body was cut at MARKDOWN_NOTES_MAX_CHARS"),
                    ("frontmatter_error", "boolean", "Frontmatter present but not valid YAML"),
                    ("deleted", "boolean", "The file is gone from the root (tombstone version)"),
                    ("renamed_to", "text", "For a tombstone: external id of a new file with identical bytes"),
                    ("author_kind", "text", "owner: notes are written by the owner"),
                    ("mapping_revision", "integer", "Mapping revision that produced this record"),
                ),
            )
        ],
        settings={
            "roots": Declaration(
                env="MARKDOWN_NOTES_ROOTS",
                description="Folders to read: label=path or bare paths, comma separated (or os.pathsep)",
            ),
            "ignore": setting("MARKDOWN_NOTES_IGNORE", "", "Extra ignore globs relative to each root, comma separated"),
            "extensions": setting("MARKDOWN_NOTES_EXTENSIONS", ".md,.markdown", "Note file extensions"),
            "include_hidden": setting("MARKDOWN_NOTES_INCLUDE_HIDDEN", "false", "Read dot-files and dot-folders too"),
            "max_bytes": setting("MARKDOWN_NOTES_MAX_BYTES", "2000000", "Skip (and count) files larger than this"),
            "max_chars": setting("MARKDOWN_NOTES_MAX_CHARS", "200000", "Body characters kept per revision"),
            "subject_keys": setting(
                "MARKDOWN_NOTES_SUBJECT_KEYS",
                "date,event_date",
                "Frontmatter keys whose date becomes the record subject of a non-daily note",
            ),
            "timezone": setting("MARKDOWN_NOTES_TZ", "", "IANA zone for daily-note days and naive dates (default: system)"),
            "checkpoint_every": setting("MARKDOWN_NOTES_CHECKPOINT_EVERY", "200", "Changed notes per checkpoint"),
        },
        link_types=["links", "about"],
    )

    def __init__(self):
        super().__init__()
        self.roots = parse_roots(self.settings["roots"])
        self.patterns = list(DEFAULT_IGNORE) + [
            p.strip() for p in (self.settings.get("ignore") or "").split(",") if p.strip()
        ]
        self.extensions = tuple(
            e.strip().lower() if e.strip().startswith(".") else "." + e.strip().lower()
            for e in (self.settings.get("extensions") or ".md").split(",")
            if e.strip()
        )
        self.include_hidden = (self.settings.get("include_hidden") or "").lower() in ("1", "true", "yes")
        self.max_bytes = positive(self.settings.get("max_bytes"), "MARKDOWN_NOTES_MAX_BYTES")
        self.max_chars = positive(self.settings.get("max_chars"), "MARKDOWN_NOTES_MAX_CHARS")
        self.every = positive(self.settings.get("checkpoint_every"), "MARKDOWN_NOTES_CHECKPOINT_EVERY")
        self.subject_keys = [k.strip().lower() for k in (self.settings.get("subject_keys") or "").split(",") if k.strip()]
        zone = (self.settings.get("timezone") or "").strip()
        self.tz = ZoneInfo(zone) if zone else dt.datetime.now().astimezone().tzinfo
        self.now = None  # tests pin the tombstone observation time

    # --- cursor -------------------------------------------------------------

    @staticmethod
    def load(cursor):
        """Cursor: {"v": 1, "roots": {label: packed {"f": {rel: [mtime_ns,
        size, sha256[:16]]}, "p": [rels whose note-to-note links are not yet
        delivered]}}}."""
        roots = (cursor or {}).get("roots") or {}
        return {label: unpack(blob) for label, blob in roots.items()}

    @staticmethod
    def dump(state):
        return {"v": 1, "roots": {label: pack(value) for label, value in sorted(state.items())}}

    # --- run ----------------------------------------------------------------

    def records(self, cursor, mode):
        state = self.load(cursor)
        reachable = 0
        for root in self.roots:
            if not root.path.is_dir():
                LOG.warning("markdown notes root %s is not a readable folder", root.label)
                yield coverage(NOTES, scope_key=root.label, status="partial", reasons=["source_error"],
                               detail={"error": "root folder missing or unreadable"})
                continue
            root.detect()
            files = scan(root, patterns=self.patterns, extensions=self.extensions, include_hidden=self.include_hidden)
            previous = state.get(root.label) or {}
            if not files and previous.get("f"):
                # An unmounted drive or emptied sync folder must not delete every note.
                LOG.warning("markdown notes root %s is empty; keeping its notes", root.label)
                yield coverage(NOTES, scope_key=root.label, status="partial", reasons=["source_error"],
                               detail={"error": "root folder is empty but held notes before; no deletions recorded"})
                continue
            reachable += 1
            yield from self.sync_root(root, files, state)
        if self.roots and not reachable:
            raise RuntimeError("no markdown notes root is readable")
        yield Checkpoint(cursor=self.dump(state))

    def sync_root(self, root, files, state):
        entry = state.setdefault(root.label, {})
        known = entry.setdefault("f", {})
        pending = set(entry.get("p") or [])
        changed = sorted(rel for rel, (mtime, size) in files.items() if (known.get(rel) or [None, None])[:2] != [mtime, size])
        gone = sorted(rel for rel in known if rel not in files)
        counts = {"revisions": 0, "unchanged": 0, "skipped_large": 0, "unreadable": 0, "deleted": 0}
        parsed = {}
        new_hashes = {}
        since = 0
        for rel in changed:
            mtime, size = files[rel]
            before = known.get(rel)
            if size > self.max_bytes:
                counts["skipped_large"] += 1
                known[rel] = [mtime, size, SKIPPED]
                continue
            try:
                data = (root.path / rel).read_bytes()
            except OSError:
                counts["unreadable"] += 1
                continue
            digest = hashlib.sha256(data).hexdigest()
            if before is None:
                new_hashes.setdefault(digest[:16], rel)
            if before and before[2] == digest[:16]:
                known[rel] = [mtime, size, digest[:16]]
                counts["unchanged"] += 1
                continue
            text = data.decode("utf-8", errors="replace")
            note = parse_note(text, logseq=root.format == "logseq")
            parsed[rel] = note
            message = self.map_note(root, rel, note, mtime, len(data), digest)
            yield message
            for tag in note["tags"]:
                yield tag_link(message, tag)
            known[rel] = [mtime, len(data), digest[:16]]
            pending.add(rel)
            counts["revisions"] += 1
            since += 1
            if since >= self.every:
                since = 0
                entry["p"] = sorted(pending)
                yield Checkpoint(cursor=self.dump(state))
        observed = self.observed()
        for rel in gone:
            before = known.pop(rel)
            pending.discard(rel)
            if before[2] == SKIPPED:
                continue
            renamed = new_hashes.get(before[2])
            yield self.tombstone(root, rel, observed, renamed)
            counts["deleted"] += 1
        entry["p"] = sorted(pending)
        if changed or gone:
            yield Checkpoint(cursor=self.dump(state))
        # Note-to-note links go last: both ends must already be stored.
        index = Index(root, [rel for rel, value in known.items() if value[2] != SKIPPED])
        for rel in sorted(pending):
            if rel not in known or known[rel][2] == SKIPPED:
                continue
            note = parsed.get(rel)
            if note is None:  # Delivered before a crash: read it again.
                try:
                    note = parse_note((root.path / rel).read_bytes().decode("utf-8", errors="replace"),
                                      logseq=root.format == "logseq")
                except OSError:
                    continue
            source = external_id(root, rel)
            for target, kind in resolved_links(index, rel, note):
                yield Record(kind="link", data={
                    "type": "links",
                    "from": {"channel": NOTES, "external_id": source},
                    "to": {"channel": NOTES, "external_id": external_id(root, target)},
                    "confidence": 1,
                    "method": "source",
                    "evidence": {"via": kind},
                })
        if pending:
            entry["p"] = []
            yield Checkpoint(cursor=self.dump(state))
        yield coverage(
            NOTES,
            scope_key=root.label,
            status="complete",
            rows_delivered=counts["revisions"] + counts["deleted"],
            verified=True,
            detail={**counts, "files": len(files), "format": root.format},
        )

    def observed(self):
        if self.now:
            return self.now
        return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()

    # --- mapping ------------------------------------------------------------

    def map_note(self, root, rel, note, mtime_ns, size, digest):
        title = note["title"] or page_name(root, rel)
        body = note["body"]
        truncated = len(body) > self.max_chars
        if truncated:
            body = body[: self.max_chars]
        meta = note["meta"]
        day = daily_date(root, rel)
        modified_at = dt.datetime.fromtimestamp(mtime_ns / 1e9, dt.UTC).replace(microsecond=0)
        values = {
            "vault": root.label,
            "vault_format": root.format,
            "path": rel,
            "title": title,
            "aliases": note["aliases"],
            "tags": note["tags"],
            "frontmatter": note["frontmatter"],
            "properties": note["properties"],
            "link_targets": sorted({target for target, _ in note["links"]}),
            "is_daily": day is not None,
            "daily_date": day.isoformat() if day else None,
            "created": iso(first_when(meta, ("created", "date created", "created_at", "creation_date"), self.tz)),
            "modified": iso(first_when(meta, ("modified", "updated", "date modified", "updated_at", "last_modified"), self.tz)),
            "mtime": modified_at.isoformat(),
            "size_bytes": size,
            "content_sha256": digest,
            "content_truncated": truncated,
            "frontmatter_error": note["frontmatter_error"],
            "deleted": False,
            "renamed_to": None,
            "author_kind": "owner",
            "mapping_revision": MAPPING_REVISION,
        }
        message = {
            "channel": NOTES,
            "external_id": external_id(root, rel),
            "thread": {"external_id": external_id(root, rel), "title": title},
            "happened_at": modified_at.isoformat(),
            "content": "\n\n".join(part for part in (title, body) if part),
            "fields": values,
            "payload": {
                "note": {
                    "vault": root.label,
                    "vault_format": root.format,
                    "path": rel,
                    "mtime_ns": mtime_ns,
                    "size": size,
                    "sha256": digest,
                    "frontmatter": note["frontmatter"],
                    "properties": note["properties"],
                    "tags": note["tags"],
                    "aliases": note["aliases"],
                    "links": [list(item) for item in note["links"]],
                },
                "content": {"title": title, "body": body},
            },
        }
        subject = None
        if day:
            start = dt.datetime(day.year, day.month, day.day, tzinfo=self.tz).isoformat()
            subject = {"start": start, "end": start, "start_precision": "day", "end_precision": "day"}
        else:
            for key in self.subject_keys:
                when = parse_when(meta.get(key), self.tz)
                if when:
                    precise = "T" in str(meta[key]) or " " in str(meta[key]).strip()
                    subject = {"start": when.isoformat(), "start_precision": "exact" if precise else "day"}
                    break
        if subject:
            message["subject"] = subject
        return message

    def tombstone(self, root, rel, observed, renamed):
        title = page_name(root, rel)
        ident = external_id(root, rel)
        where = f" It was renamed or moved to {renamed}." if renamed else ""
        return {
            "channel": NOTES,
            "external_id": ident,
            "thread": {"external_id": ident, "title": title},
            "happened_at": observed,
            "content": f"{title}\n\nDeleted: {rel} is no longer in {root.label}.{where}",
            "fields": {
                "vault": root.label,
                "vault_format": root.format,
                "path": rel,
                "title": title,
                "deleted": True,
                "renamed_to": external_id(root, renamed) if renamed else None,
                "author_kind": "owner",
                "mapping_revision": MAPPING_REVISION,
            },
            "payload": {
                "note": {"vault": root.label, "path": rel, "deleted": True, "observed_at": observed,
                         "renamed_to": renamed},
                "content": {"title": title, "body": ""},
            },
        }


def resolved_links(index, rel, note):
    seen = set()
    for target, kind in note["links"]:
        hit = index.resolve(rel, target, kind)
        if hit and hit != rel and hit not in seen:
            seen.add(hit)
            yield hit, kind


def tag_link(message, tag):
    return Record(kind="link", data={
        "type": "about",
        "from": {"channel": NOTES, "external_id": message["external_id"]},
        "to": {"kind": "tag", "external_ref": "tag:" + tag.casefold(), "name": "#" + tag},
        "confidence": 1,
        "method": "source",
        "evidence": {"via": "tag"},
    })


def external_id(root, rel):
    return f"{root.label}:{rel}"


def first_when(meta, keys, tz):
    for key in keys:
        when = parse_when(meta.get(key), tz)
        if when:
            return when
    return None


def iso(value):
    return value.isoformat() if value else None


def positive(value, name):
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a positive integer") from None
    if number < 1:
        raise ValueError(f"{name} must be a positive integer")
    return number


def main(argv=None):
    run(MarkdownNotes, argv)


__all__ = ["MAPPING_REVISION", "NOTES", "MarkdownNotes", "main"]
