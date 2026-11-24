"""Notion pages and database schemas read through Composio."""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
import logging
from pathlib import Path

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

from . import fetch


MAPPING_REVISION = 1
PAGES = "notion.pages"
DATABASES = "notion.databases"
LOG = logging.getLogger("throughline_notion")
SUFFIXES = (".jsonl", ".ndjson", ".jsonl.gz")


def fields(*items):
    return [dict(name=name, type=kind, description=description) for name, kind, description in items]


COMMON = [
    ("title", "text", "Notion title at this revision"),
    ("url", "text", "Notion URL"),
    ("parent_type", "text", "Notion parent type"),
    ("parent_id", "text", "Parent page, database, data source, block, or workspace identifier"),
    ("created_time", "timestamp", "Notion object creation time"),
    ("last_edited_time", "timestamp", "Notion last-edited time; observation time for an unavailable tombstone"),
    ("created_by_id", "text", "Notion user id that created the object"),
    ("created_by_name", "text", "Display name resolved from the workspace user list"),
    ("last_edited_by_id", "text", "Notion user id that last edited the object"),
    ("last_edited_by_name", "text", "Display name resolved from the workspace user list"),
    ("archived", "boolean", "Notion reports the object archived"),
    ("in_trash", "boolean", "Notion reports the object in trash"),
    ("missing", "boolean", "Object vanished from a complete accessible-scope scan; may be deleted or access-revoked"),
    ("mapping_revision", "integer", "Mapping revision that produced this record"),
]


def declaration_map(rows):
    return {
        key: Declaration(env=env, required=False, default=default, description=description)
        for key, env, default, description in rows
    }


def secret_map(rows):
    return {
        key: Declaration(env=env, required=False, description=description)
        for key, env, description in rows
    }


class Notion(Plugin):
    manifest = Manifest(
        name="notion",
        version="0.1.0",
        run_mode="poll",
        default_interval_s=3600,
        channels=[
            Channel(
                name=PAGES,
                description=(
                    "Accessible Notion pages and database rows, one immutable Throughline version per observed "
                    "Notion edit; content is readable Notion-flavoured Markdown plus property text"
                ),
                is_reference=True,
                record_kind="page_revision",
                passage_text_paths=["/content/title", "/content/properties_text", "/content/markdown"],
                fields=fields(
                    *COMMON,
                    ("database_id", "text", "Parent database or data-source id when this page is a row"),
                    ("properties", "json", "Notion property values with their source types"),
                    ("properties_text", "text", "Readable rendering of non-title properties"),
                    ("content_chars", "integer", "Markdown characters reported before the local cap"),
                    ("content_truncated", "boolean", "Local content limit cut the Markdown"),
                    ("notion_truncated", "boolean", "Composio/Notion reported truncated Markdown"),
                    ("unknown_block_ids", "json", "Block ids the Markdown action could not render"),
                    ("content_source", "text", "markdown, blocks fallback, or metadata-only lifecycle state"),
                    ("content_unavailable", "boolean", "Page metadata was read but neither content action returned a usable body"),
                ),
            ),
            Channel(
                name=DATABASES,
                description="Accessible Notion database schemas, versioned when their metadata or properties change",
                is_reference=True,
                record_kind="page_revision",
                passage_text_paths=["/content/title", "/content/description", "/content/schema_text"],
                fields=fields(
                    *COMMON,
                    ("description", "text", "Readable database description"),
                    ("schema", "json", "Notion database property schema"),
                    ("schema_text", "text", "Readable rendering of the database schema"),
                    ("is_inline", "boolean", "Database is rendered inline in its parent page"),
                ),
            ),
        ],
        settings={
            "source": Declaration(
                env="NOTION_SOURCE",
                required=False,
                default="live",
                description="live, or export:<JSONL file or directory written by fetch.py>",
            ),
            "checkpoint_every": Declaration(
                env="NOTION_CHECKPOINT_EVERY",
                required=False,
                default="100",
                description="Mapped records per durable plugin checkpoint",
            ),
            **declaration_map(fetch.SETTINGS),
        },
        secrets=secret_map(fetch.SECRETS),
        link_types=["part_of"],
    )

    def __init__(self):
        super().__init__()
        self.checkpoint_every = fetch.integer(
            self.settings.get("checkpoint_every") or "100",
            "NOTION_CHECKPOINT_EVERY",
            1,
        )
        self.client = None

    def execute(self, client, mode, dry_run=False):
        self.client = client
        return super().execute(client, mode, dry_run)

    def superseded_gap_ids(self, records):
        """Close old broad retry gaps, but keep the current error window open."""
        if self.client is None:
            return []
        metas = [record for record in records if record.get("kind") == "export.meta"]
        if not metas:
            return []
        errors = (metas[-1].get("raw") or {}).get("page_errors") or []
        times = sorted(str(item.get("at")) for item in errors if item.get("at"))
        current = (
            (fetch.parse_time(times[0]), fetch.parse_time(times[-1])) if times else None
        )
        try:
            opened = self.client.coverage_gaps(channel=PAGES)
        except Exception as error:  # Coverage cleanup must not block source data.
            LOG.warning("Notion coverage gaps could not be read: %s", error)
            return []
        return [
            gap["id"]
            for gap in opened
            if current is None
            or (
                fetch.parse_time(gap.get("from")),
                fetch.parse_time(gap.get("to")),
            )
            != current
        ]

    def records(self, cursor, mode):
        kind, path = source_spec(self.settings.get("source"))
        if kind == "export":
            yield from self.exported(path, cursor)
        else:
            yield from self.live(cursor)

    def _config(self):
        values = {key: self.settings.get(key) for key, _, _, _ in fetch.SETTINGS}
        values.update(self.secrets)
        return fetch.Config.from_env({}, **values)

    def live(self, cursor):
        state = copy.deepcopy(cursor or {})
        records, fetch_state = fetch.Fetcher(self._config(), state.get("live")).run()
        users = users_of(records)
        mapped = [map_record(record, users) for record in records]
        mapped = [item for item in mapped if item is not None]
        for start in range(0, len(mapped), self.checkpoint_every):
            for item in mapped[start : start + self.checkpoint_every]:
                yield item
            if start + self.checkpoint_every < len(mapped):
                yield Checkpoint(cursor=state)
        for item in coverage_records(
            records, mapped, closes=self.superseded_gap_ids(records)
        ):
            yield item
        state["live"] = fetch_state
        yield Checkpoint(cursor=state)

    def exported(self, root, cursor):
        state = copy.deepcopy(cursor or {})
        progress = state.setdefault("export", {})
        done = progress.setdefault("files", {})
        pending = progress.get("pending") or {}
        for name, path in export_files(root):
            signature = file_digest(path)
            if done.get(name) == signature:
                continue
            records = list(read_records(path, name))
            users = users_of(records)
            offset = pending.get("offset", 0) if pending.get("name") == name and pending.get("digest") == signature else 0
            emitted = 0
            mapped_for_coverage = []
            links = []
            for record in records:
                item = map_record(record, users)
                if item is None:
                    continue
                emitted += 1
                mapped_for_coverage.append(item)
                link = parent_link(item)
                if link:
                    # Collect links even for records already acknowledged at an
                    # interim checkpoint. Links are emitted after messages, so a
                    # crash between the last message checkpoint and link delivery
                    # must replay them on resume.
                    links.append(link)
                if emitted <= offset:
                    continue
                yield item
                if emitted % self.checkpoint_every == 0:
                    progress["pending"] = {"name": name, "digest": signature, "offset": emitted}
                    yield Checkpoint(cursor=copy.deepcopy(state))
            # Links are emitted after all messages in the export so parent databases
            # delivered in the same batch already exist. Existing links replay safely.
            for link in links:
                yield link
            for item in coverage_records(
                records,
                mapped_for_coverage,
                closes=self.superseded_gap_ids(records),
            ):
                yield item
            progress.pop("pending", None)
            done[name] = signature
            pending = {}
            yield Checkpoint(cursor=copy.deepcopy(state))
        progress.pop("pending", None)
        yield Checkpoint(cursor=state)


def source_spec(value):
    value = (value or "live").strip()
    if value == "live":
        return "live", None
    if value.startswith("export:") and value[7:].strip():
        path = Path(value[7:].strip()).expanduser()
        if not path.exists():
            raise ValueError("the NOTION_SOURCE export path does not exist")
        return "export", path
    raise ValueError("NOTION_SOURCE must be 'live' or 'export:<file or directory>'")


def export_files(root):
    if root.is_file():
        return [(root.name, root)]
    files = sorted(p for p in root.rglob("*") if p.is_file() and p.name.endswith(SUFFIXES))
    if not files:
        raise ValueError("the NOTION_SOURCE export directory holds no JSONL files")
    return [(str(path.relative_to(root)), path) for path in files]


def file_digest(path):
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def read_records(path, name):
    opener = gzip.open if path.name.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                raise ValueError(f"{name} line {number} is not JSON") from None
            if not isinstance(record, dict) or not isinstance(record.get("kind"), str):
                raise ValueError(f"{name} line {number} is not a Notion export record")
            yield record


def users_of(records):
    result = {}
    for record in records:
        if record.get("kind") != "notion.user":
            continue
        raw = record.get("raw") or {}
        ident = str(raw.get("id") or "")
        if ident:
            result[ident] = raw
    return result


def rich_text(value):
    if not isinstance(value, list):
        return ""
    return "".join(
        str(item.get("plain_text") or ((item.get("text") or {}).get("content")) or "")
        for item in value
        if isinstance(item, dict)
    ).strip()


def title(raw):
    if raw.get("title_text"):
        return str(raw["title_text"])
    if raw.get("object") == "database":
        return rich_text(raw.get("title")) or "Untitled database"
    for prop in (raw.get("properties") or {}).values():
        if isinstance(prop, dict) and prop.get("type") == "title":
            return rich_text(prop.get("title")) or "Untitled"
    return str(raw.get("title") or "Untitled")


def scalar(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (str, int, float)):
        return str(value)
    if isinstance(value, list):
        values = [scalar(item) for item in value]
        return ", ".join(item for item in values if item)
    if not isinstance(value, dict):
        return str(value)
    if "plain_text" in value:
        return str(value.get("plain_text") or "")
    for key in ("name", "content", "start", "url", "email", "phone_number", "number"):
        if value.get(key) is not None:
            return scalar(value[key])
    values = [scalar(item) for key, item in value.items() if key not in {"id", "type", "color", "object"}]
    return ", ".join(item for item in values if item)


def property_lines(properties, *, schema=False):
    lines = []
    for name in sorted(properties or {}, key=str.casefold):
        prop = properties[name]
        if not isinstance(prop, dict):
            text = scalar(prop)
        elif schema:
            kind = prop.get("type") or "unknown"
            detail = scalar(prop.get(kind))
            text = kind + (f": {detail}" if detail else "")
        else:
            kind = prop.get("type")
            text = scalar(prop.get(kind)) if kind else scalar(prop)
        if text:
            lines.append(f"{name}: {text}")
        elif schema:
            lines.append(f"{name}: {prop.get('type') or 'unknown'}")
    return "\n".join(lines)


def parent_fields(raw):
    parent = raw.get("parent") or {}
    kind = str(parent.get("type") or "")
    ident = parent.get(kind)
    if ident is None:
        ident = next((v for k, v in parent.items() if k != "type" and isinstance(v, str)), None)
    database = None
    if kind in {"database_id", "data_source_id"}:
        database = ident
    return kind or None, str(ident) if ident is not None else None, str(database) if database else None


def person(raw, users):
    ident = str((raw or {}).get("id") or "")
    user = users.get(ident) or {}
    return ident or None, user.get("name")


def author(raw, users):
    ident, name = person(raw.get("last_edited_by"), users)
    if not ident:
        return None
    return {"system": "notion", "kind": "account", "value": ident, "display_name": name}


def common_fields(raw, record, users):
    parent_type, parent_id, _ = parent_fields(raw)
    created_id, created_name = person(raw.get("created_by"), users)
    edited_id, edited_name = person(raw.get("last_edited_by"), users)
    missing = bool(record.get("missing"))
    return {
        "title": title(raw),
        "url": raw.get("url"),
        "parent_type": parent_type,
        "parent_id": parent_id,
        "created_time": raw.get("created_time"),
        "last_edited_time": record.get("observed_at") if missing else raw.get("last_edited_time"),
        "created_by_id": created_id,
        "created_by_name": created_name,
        "last_edited_by_id": edited_id,
        "last_edited_by_name": edited_name,
        "archived": bool(raw.get("archived") or raw.get("is_archived")),
        "in_trash": bool(raw.get("in_trash")),
        "missing": missing,
        "mapping_revision": MAPPING_REVISION,
    }


def map_page(record, users):
    raw = record.get("raw") or {}
    ident = str(record.get("id") or raw.get("id") or "")
    if not ident:
        raise ValueError("Notion page record has no id")
    values = common_fields(raw, record, users)
    _, _, database_id = parent_fields(raw)
    properties = raw.get("properties") or {}
    props = property_lines(properties)
    markdown = str(record.get("markdown") or "").strip()
    content_unavailable = bool(record.get("content_unavailable"))
    heading = values["title"]
    if values["missing"]:
        body = f"{heading}\n\nUnavailable in the latest complete Notion integration scan. It may be deleted, trashed, or no longer shared with the integration."
    elif values["archived"] or values["in_trash"]:
        body = f"{heading}\n\nArchived or in trash."
    elif content_unavailable:
        body = "\n\n".join(
            part
            for part in (
                heading,
                "Properties:\n" + props if props else "",
                "Page body unavailable from both Notion content actions; metadata is complete and content remains queued for retry.",
            )
            if part
        )
    else:
        body = "\n\n".join(part for part in (heading, "Properties:\n" + props if props else "", markdown) if part)
    values.update(
        database_id=database_id,
        properties=properties,
        properties_text=props or None,
        content_chars=int(record.get("source_chars") or len(markdown)),
        content_truncated=bool(record.get("content_truncated")),
        notion_truncated=bool(record.get("notion_truncated")),
        unknown_block_ids=record.get("unknown_block_ids") or [],
        content_source=record.get("content_source") or "markdown",
        content_unavailable=content_unavailable,
    )
    happened = values["last_edited_time"] or values["created_time"] or record.get("fetched_at")
    return {
        "channel": PAGES,
        "external_id": ident,
        "thread": {"external_id": ident, "title": heading},
        "happened_at": happened,
        "author": author(raw, users),
        "content": body,
        "fields": values,
        "payload": {
            "notion": raw,
            "content": {"title": heading, "properties_text": props, "markdown": markdown},
            "fetch": {
                "fetched_at": record.get("fetched_at"),
                "content_truncated": values["content_truncated"],
                "notion_truncated": values["notion_truncated"],
                "content_source": values["content_source"],
                "content_unavailable": values["content_unavailable"],
                "missing": values["missing"],
            },
        },
    }


def map_database(record, users):
    raw = record.get("raw") or {}
    ident = str(record.get("id") or raw.get("id") or "")
    if not ident:
        raise ValueError("Notion database record has no id")
    values = common_fields(raw, record, users)
    schema = raw.get("properties") or {}
    schema_text = property_lines(schema, schema=True)
    description = rich_text(raw.get("description"))
    values.update(
        description=description or None,
        schema=schema,
        schema_text=schema_text or None,
        is_inline=bool(raw.get("is_inline")),
    )
    heading = values["title"]
    if values["missing"]:
        body = f"{heading}\n\nUnavailable in the latest complete Notion integration scan."
    else:
        body = "\n\n".join(part for part in (heading, description, "Schema:\n" + schema_text if schema_text else "") if part)
    happened = values["last_edited_time"] or values["created_time"] or record.get("fetched_at")
    return {
        "channel": DATABASES,
        "external_id": ident,
        "thread": {"external_id": ident, "title": heading},
        "happened_at": happened,
        "author": author(raw, users),
        "content": body,
        "fields": values,
        "payload": {
            "notion": raw,
            "content": {"title": heading, "description": description, "schema_text": schema_text},
            "fetch": {"fetched_at": record.get("fetched_at"), "missing": values["missing"]},
        },
    }


def map_record(record, users):
    try:
        if record.get("kind") == "notion.page":
            return map_page(record, users)
        if record.get("kind") == "notion.database":
            return map_database(record, users)
    except (TypeError, ValueError, KeyError) as error:
        LOG.warning("Notion record skipped: %s", error)
    return None


def parent_link(message):
    if message.get("channel") != PAGES:
        return None
    database = (message.get("fields") or {}).get("database_id")
    if not database:
        return None
    return Record(
        kind="link",
        data={
            "type": "part_of",
            "from": {"channel": PAGES, "external_id": message["external_id"]},
            "to": {"channel": DATABASES, "external_id": database},
            "confidence": 1,
            "method": "source",
            "evidence": {"notion_parent_type": message["fields"].get("parent_type")},
        },
    )


def coverage_records(records, mapped, closes=()):
    metas = [record for record in records if record.get("kind") == "export.meta"]
    if not metas:
        return []
    raw = metas[-1].get("raw") or {}
    errors = raw.get("page_errors") or []
    truncated = raw.get("truncated_page_ids") or []
    reasons = []
    gaps = []
    if errors:
        reasons.append("source_error")
        times = sorted(str(item.get("at")) for item in errors if item.get("at"))
        if times:
            gaps.append({"from": times[0], "to": times[-1], "reason": "source_error"})
    if truncated:
        reasons.append("source_cap")
    page_count = sum(item.get("channel") == PAGES for item in mapped)
    db_count = sum(item.get("channel") == DATABASES for item in mapped)
    detail = {
        "accessible_scope": raw.get("accessible_scope"),
        "full_metadata_scan": bool(raw.get("full_metadata_scan")),
        "page_errors": len(errors),
        "truncated_pages": len(truncated),
    }
    reports = [
        coverage(
            PAGES,
            status="partial" if reasons else "complete",
            reasons=reasons,
            window={"from": raw.get("window_from"), "to": raw.get("window_to")}
            if raw.get("window_from") and raw.get("window_to")
            else None,
            high_water=raw.get("high_water"),
            rows_delivered=page_count,
            gaps=gaps,
            closes=closes,
            verified=not reasons and bool(raw.get("full_metadata_scan")),
            detail=detail,
        ),
        coverage(
            DATABASES,
            status="complete",
            window={"from": raw.get("window_from"), "to": raw.get("window_to")}
            if raw.get("window_from") and raw.get("window_to")
            else None,
            high_water=raw.get("high_water"),
            rows_delivered=db_count,
            verified=bool(raw.get("full_metadata_scan")),
            detail={"accessible_scope": raw.get("accessible_scope"), "full_metadata_scan": bool(raw.get("full_metadata_scan"))},
        ),
    ]
    return reports


def main(argv=None):
    run(Notion, argv)


__all__ = ["DATABASES", "MAPPING_REVISION", "Notion", "PAGES", "main"]
