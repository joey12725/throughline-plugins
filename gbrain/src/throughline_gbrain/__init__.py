"""Read-only GBrain import. The source connection is never used for writes."""

import hashlib
import json
import logging
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, time, timezone
from urllib.parse import unquote

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from throughline_sdk import (
    Channel,
    Checkpoint,
    Client,
    Declaration,
    Manifest,
    Plugin,
    Record,
    coverage,
    provenance,
    run,
)


def wire(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def clean(value):
    return json.loads(json.dumps(value, default=wire))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


PAGE_COLUMNS = "id,source_id,slug,type,page_kind,title,compiled_truth,timeline,frontmatter,content_hash,created_at,updated_at,deleted_at,effective_date,effective_date_source,source_path,source_uri,source_kind,ingested_via,ingested_at"


# Every source column this adapter reads, by table. The source is checked for
# exactly these before any data query; extra upstream tables and columns are
# tolerated and never read (search and embedding bookkeeping stays out).
REQUIRED_SCHEMA = {
    "pages": PAGE_COLUMNS.split(","),
    "page_versions": ["id", "page_id", "compiled_truth", "frontmatter", "snapshot_at"],
    "page_aliases": ["id", "source_id", "alias_norm", "slug", "created_at"],
    "slug_aliases": [
        "id",
        "source_id",
        "alias_slug",
        "canonical_slug",
        "notes",
        "created_at",
    ],
    "links": [
        "id",
        "from_page_id",
        "to_page_id",
        "link_type",
        "context",
        "link_source",
        "link_kind",
        "origin_page_id",
        "origin_field",
        "resolution_type",
        "created_at",
    ],
    "tags": ["id", "page_id", "tag"],
    "timeline_entries": [
        "id",
        "page_id",
        "date",
        "source",
        "summary",
        "detail",
        "event_page_id",
        "created_at",
    ],
    "sources": ["id", "name"],
}
# The upstream GBrain release REQUIRED_SCHEMA was verified against. None until
# the owner verifies one (README, "Upstream compatibility").
PINNED_UPSTREAM = None


class SchemaMismatch(ValueError):
    """The source lacks columns the adapter reads."""


def check_schema(conn):
    """Compare the source with REQUIRED_SCHEMA through information_schema.

    Runs inside the caller's transaction, so it sees the snapshot the data
    queries read. Raises SchemaMismatch naming every missing table.column.
    """
    present = {
        (row["table_name"], row["column_name"])
        for row in conn.execute(
            "SELECT table_name::text AS table_name, column_name::text AS column_name"
            " FROM information_schema.columns"
            " WHERE table_schema='public' AND table_name = ANY(%s)",
            (list(REQUIRED_SCHEMA),),
        ).fetchall()
    }
    missing = [
        f"{table}.{column}"
        for table, columns in REQUIRED_SCHEMA.items()
        for column in columns
        if (table, column) not in present
    ]
    if missing:
        raise SchemaMismatch(
            "GBrain source schema is incompatible; missing column(s): "
            + ", ".join(missing)
        )
    return {
        "compatible": True,
        "columns": sum(len(c) for c in REQUIRED_SCHEMA.values()),
        "pinned_upstream": PINNED_UPSTREAM,
    }


def read_source(dsn):
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        schema = check_schema(conn)
        result = {}
        for table, columns in REQUIRED_SCHEMA.items():
            statement = sql.SQL("SELECT {} FROM public.{}").format(
                sql.SQL(",").join(sql.Identifier(c) for c in columns),
                sql.Identifier(table),
            )
            result[table] = clean(conn.execute(statement).fetchall())
        result["schema_check"] = schema
        return result


def text(*parts):
    return "\n\n".join(p for p in parts if p and p.strip()) or "Untitled imported page"


# PostgreSQL refuses a tsvector above 1 MB, and unusual text (many short unique
# tokens or URLs) can index to several times its own size. Text above this
# budget is cut for indexing only; the payload always keeps the full page.
CONTENT_BUDGET = 250_000
TRUNCATED = "\n\n[Truncated for indexing; the payload holds the full text]"
# Part of the live-page signature. Raising it makes the next sync send every
# current page once more, so pages stored by an older mapping gain new fields.
# 3: a page revision's record time is its revision time, effective_date is its
# subject time, and timeline entries carry a day-precision subject.
MAPPING_REVISION = 3


def fit(content):
    raw = content.encode()
    if len(raw) <= CONTENT_BUDGET:
        return content
    keep = CONTENT_BUDGET - len(TRUNCATED.encode())
    return raw[:keep].decode("utf-8", "ignore") + TRUNCATED


def stored_candidates(content, title, bodies):
    """The known bodies a stored version's content could have been built from.

    content is fit(text(title, body, timeline)). The timeline of an old version
    is not known, so a body qualifies only as a whole part: the content after
    the title is the body itself or the body followed by the part separator.
    Returns (candidates, cut). When the content was cut for indexing, every
    body that starts with the kept text qualifies, and the stored text cannot
    tell them apart.
    """
    head = title + "\n\n" if title and title.strip() else ""
    if not content.startswith(head):
        return [], False
    rest = content[len(head) :]
    cut = rest.endswith(TRUNCATED)
    if cut:
        rest = rest[: -len(TRUNCATED)]
    return [
        body
        for body in bodies
        if rest == body
        or rest.startswith(body + "\n\n")
        or (cut and (body + "\n\n").startswith(rest))
    ], cut


def stored_body(content, title, bodies):
    """Which of the known bodies a stored version's content was built from.

    When several qualify (one body continues another after a blank line) the
    version holds the longest; a shorter one is not present just because a
    later body starts with it.
    """
    return max(stored_candidates(content, title, bodies)[0], key=len, default=None)


def held_bodies(rows, bodies):
    """(external_id, body) pairs proven present by stored page versions.

    A version with one candidate proves that body. A version can have several:
    content cut for indexing fits every body that starts with the kept text,
    and a body that later gained a paragraph equal to the timeline reads the
    same as the shorter body followed by that timeline. Each such version
    proves one more of its candidates, longest first, so n versions prove at
    most n bodies and a lost snapshot still shows up as missing.
    """
    held, ambiguous = set(), []
    for row in rows:
        known = bodies.get(row["external_id"], ())
        found, cut = stored_candidates(
            row["content"], row["fields"].get("title", row.get("thread_title")), known
        )
        if not found:
            continue
        if len(found) > 1:
            ambiguous.append((row["external_id"], sorted(found, key=len, reverse=True)))
        else:
            held.add((row["external_id"], max(found, key=len)))
    for external_id, found in sorted(ambiguous, key=lambda item: len(item[1])):
        for body in found:
            if (external_id, body) not in held:
                held.add((external_id, body))
                break
    return held


def endpoint(channel, external_id):
    return {"channel": channel, "external_id": external_id}


PAGES = "gbrain.pages"
TIMELINE = "gbrain.timeline"
LEGACY = "gbrain.legacy"
LEGACY_TIMELINE = "gbrain.legacy_timeline"
CHANNELS = (PAGES, TIMELINE, LEGACY, LEGACY_TIMELINE)
PAGE_CHANNELS = (PAGES, LEGACY)
IN_CHANNELS = "(" + ",".join(f"'{c}'" for c in CHANNELS) + ")"
# The timeline channel of each page channel.
TIMELINE_OF = {PAGES: TIMELINE, LEGACY: LEGACY_TIMELINE}
# Page columns that can carry the legacy marker as a flag value.
FLAG_COLUMNS = ("page_kind", "type", "source_kind", "ingested_via")


def truthy(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def flagged(value, marker):
    """A frontmatter value names the marker: the value itself, a list item,
    or a key set to a true value."""
    if isinstance(value, str):
        return value.strip().lower() == marker
    if isinstance(value, list):
        return any(flagged(item, marker) for item in value)
    if isinstance(value, dict):
        return any(
            (key.strip().lower() == marker and item not in (None, False, "", 0))
            or flagged(item, marker)
            for key, item in value.items()
            if isinstance(key, str)
        )
    return False


def is_legacy(page, tags, marker):
    """True when the page carries the legacy bulk marker.

    The marker (GBRAIN_LEGACY_MARKER, default source_copy) is found in the
    live page's frontmatter (a key set to a true value, or a value or list
    item equal to it), in a page flag column (page_kind, type, source_kind,
    ingested_via) or as a tag. Matching ignores case and surrounding space.
    """
    marker = (marker or "").strip().lower()
    if not marker:
        return False
    if any(
        isinstance(page.get(c), str) and page[c].strip().lower() == marker
        for c in FLAG_COLUMNS
    ):
        return True
    if any(isinstance(t, str) and t.strip().lower() == marker for t in tags):
        return True
    return flagged(page.get("frontmatter"), marker)


def normalize_type(raw):
    """The core registry's name for a raw link type (tl.normalize_link_type)."""
    t = re.sub(r"[^a-z0-9]+", "_", str(raw).strip(" ").lower()).strip("_")[:63]
    if not t:
        return "t_" + hashlib.sha256(str(raw).encode()).hexdigest()[:12]
    if not t[0].isalpha() or not t[0].isascii():
        t = ("t_" + t)[:63]
    return t


def instant(value):
    """A source timestamp that carries a UTC offset, or None when unusable."""
    if not isinstance(value, str):
        return None
    try:
        at = datetime.fromisoformat(value)
    except ValueError:
        return None
    return at if at.utcoffset() is not None else None


def utc(value):
    return datetime.fromisoformat(value).astimezone(timezone.utc).isoformat()


def revision_time(page, version):
    """When a revision was written: the snapshot time of a snapshot, the update
    time of the live page, and the creation time only when that is missing."""
    when = version["snapshot_at"] if version else page["updated_at"]
    return when or page["created_at"]


def day_subject(day):
    return {
        "start": datetime.combine(day, time(), timezone.utc).isoformat(),
        "start_precision": "day",
    }


def subject_of(value):
    """The subject interval for an effective date; None without one.

    A date, or a timestamptz at exactly 00:00 UTC (how a date is held in a
    timestamptz column), has day precision; any other time is exact. Raises
    ValueError for a value that is not a usable time.
    """
    if value is None:
        return None
    if isinstance(value, str) and len(value) == 10:
        return day_subject(date.fromisoformat(value))
    at = instant(value)
    if at is None:
        raise ValueError("unusable effective date")
    if at.astimezone(timezone.utc).time() == time():
        return day_subject(at.astimezone(timezone.utc).date())
    return {"start": value, "start_precision": "exact"}


URI_PREFIX = "throughline://"
# POST /v1/provenance takes at most this many sources per call; a revision
# naming more is cited in several records.
MAX_CITATIONS = 1000


def nameable(*values):
    """Strings the core can store as a channel or external ID (no NUL)."""
    return all(isinstance(v, str) and v and "\x00" not in v for v in values)


def uri_citation(value):
    """{channel, external_id} for throughline://<channel>/<external_id>."""
    if not isinstance(value, str) or not value.startswith(URI_PREFIX):
        return None
    channel, slash, external_id = value[len(URI_PREFIX) :].partition("/")
    external_id = unquote(external_id)
    if not slash or not nameable(channel, external_id):
        return None
    return endpoint(channel, external_id)


def frontmatter_citations(frontmatter):
    """Entries of the frontmatter key throughline_sources that name a record."""
    if not isinstance(frontmatter, dict):
        return []
    named = frontmatter.get("throughline_sources")
    if not isinstance(named, list):
        return []
    return [
        endpoint(item["channel"], item["external_id"])
        for item in named
        if isinstance(item, dict)
        and nameable(item.get("channel"), item.get("external_id"))
    ]


def unique(citations):
    """Citations in a stable order, without repeats."""
    seen = {}
    for item in citations:
        if item:
            seen.setdefault((item["channel"], item["external_id"]), item)
    return [seen[key] for key in sorted(seen)]


def page_citations(page, version):
    """The Throughline records a page revision names as its sources.

    Only the frontmatter key throughline_sources and a throughline:// source
    URI count; file paths, URLs and other forms stay in fields and payload.
    """
    frontmatter = version["frontmatter"] if version else page["frontmatter"]
    return unique(
        [*frontmatter_citations(frontmatter), uri_citation(page.get("source_uri"))]
    )


def citations(derived, cited):
    """Provenance records citing `cited` for `derived`, within the core's
    per-call limit."""
    for start in range(0, len(cited), MAX_CITATIONS):
        yield provenance(derived, cited[start : start + MAX_CITATIONS], "cites")


def cited_first(pages, ids, versions):
    """Page ids in delivery order: a page cited by another page of this
    source (frontmatter or source URI, any revision) is delivered before the
    page citing it, so the citation resolves in the same run; otherwise by
    id. Citation cycles fall back to id order."""
    by_external = {ids[i]: i for i in pages}
    named = defaultdict(set)
    revisions = defaultdict(list)
    for v in versions:
        revisions[v["page_id"]].append(v)
    for i, page in pages.items():
        for version in [None, *revisions.get(i, ())]:
            try:
                cited = page_citations(page, version)
            except (TypeError, KeyError, AttributeError):
                continue
            for c in cited:
                target = by_external.get(c["external_id"])
                if c["channel"] in PAGE_CHANNELS and target not in (None, i):
                    named[i].add(target)
    order, state = [], {}
    for root in sorted(pages):
        if root in state:
            continue
        state[root] = "open"
        stack = [(root, iter(sorted(named[root])))]
        while stack:
            node, children = stack[-1]
            child = next(children, None)
            if child is None:
                stack.pop()
                state[node] = "done"
                order.append(node)
            elif child not in state:
                state[child] = "open"
                stack.append((child, iter(sorted(named[child]))))
    return order


class Verification(tuple):
    """verify() pairs (expected, actual); `report` is informational only."""

    report = {}


def source_link(kind, start, end, evidence):
    return Record(
        kind="link",
        data={
            "type": kind,
            "from": start,
            "to": end,
            "method": "source",
            "evidence": evidence,
        },
    )


class GBrain(Plugin):
    manifest = Manifest(
        name="gbrain",
        version="0.1.0",
        channels=[
            Channel(
                name="gbrain.pages",
                description="Imported synthesised GBrain knowledge: one page revision per version, at its revision time; never a location observation.",
                is_reference=True,
                record_kind="page_revision",
                passage_text_paths=[
                    "/page/title",
                    "/version/compiled_truth|/page/compiled_truth",
                    "/page/timeline",
                ],
                provenance_link_types=["cites"],
                fields=[
                    {"name": n, "type": t, "description": d}
                    for n, t, d in [
                        (
                            "source_hash",
                            "text",
                            "SHA256 of the complete imported raw payload",
                        ),
                        ("slug", "text", "Canonical source page slug"),
                        ("source_id", "text", "Upstream source namespace"),
                        ("page_type", "text", "Upstream page type"),
                        ("page_kind", "text", "Upstream page kind"),
                        ("title", "text", "Page title"),
                        ("tags", "json", "Array of source tags"),
                        (
                            "aliases",
                            "json",
                            "Array of page alias and prior slug values, retaining multiplicity",
                        ),
                        ("frontmatter", "json", "Original structured page frontmatter"),
                        ("updated_at", "timestamp", "Source update or snapshot time"),
                        ("deleted_at", "timestamp", "Source soft deletion time"),
                        ("source_uri", "text", "Original source URI when provided"),
                    ]
                ],
            ),
            Channel(
                name="gbrain.timeline",
                description="Dated events from GBrain page timelines.",
                record_kind="derived",
                fields=[
                    {
                        "name": "source_hash",
                        "type": "text",
                        "description": "SHA256 of the complete imported raw entry",
                    },
                    {
                        "name": "source",
                        "type": "text",
                        "description": "Timeline entry source",
                    },
                    {
                        "name": "page_external_id",
                        "type": "text",
                        "description": "Parent page source namespace and slug",
                    },
                ],
            ),
            Channel(
                name="gbrain.legacy",
                description="Legacy bulk GBrain pages (the legacy marker, default source_copy): reference text, opt-in in recall with kinds ['reference']; no passages.",
                is_reference=True,
                record_kind="reference",
                fields=[
                    {"name": n, "type": t, "description": d}
                    for n, t, d in [
                        (
                            "source_hash",
                            "text",
                            "SHA256 of the complete imported raw payload",
                        ),
                        ("slug", "text", "Canonical source page slug"),
                        ("source_id", "text", "Upstream source namespace"),
                        ("page_type", "text", "Upstream page type"),
                        ("page_kind", "text", "Upstream page kind"),
                        ("title", "text", "Page title"),
                        ("tags", "json", "Array of source tags"),
                        (
                            "aliases",
                            "json",
                            "Array of page alias and prior slug values, retaining multiplicity",
                        ),
                        ("frontmatter", "json", "Original structured page frontmatter"),
                        ("updated_at", "timestamp", "Source update or snapshot time"),
                        ("deleted_at", "timestamp", "Source soft deletion time"),
                        ("source_uri", "text", "Original source URI when provided"),
                    ]
                ],
            ),
            Channel(
                name="gbrain.legacy_timeline",
                description="Dated events from the timelines of legacy bulk GBrain pages: reference text, opt-in in recall.",
                is_reference=True,
                record_kind="reference",
                fields=[
                    {
                        "name": "source_hash",
                        "type": "text",
                        "description": "SHA256 of the complete imported raw entry",
                    },
                    {
                        "name": "source",
                        "type": "text",
                        "description": "Timeline entry source",
                    },
                    {
                        "name": "page_external_id",
                        "type": "text",
                        "description": "Parent page source namespace and slug",
                    },
                ],
            ),
        ],
        settings={
            "include_deleted": Declaration(
                env="GBRAIN_INCLUDE_DELETED", required=False, default="false"
            ),
            "legacy_marker": Declaration(
                env="GBRAIN_LEGACY_MARKER",
                required=False,
                default="source_copy",
                description="Marker of legacy bulk pages (frontmatter, page flag or tag)",
            ),
            "legacy_channel": Declaration(
                env="GBRAIN_LEGACY_CHANNEL",
                required=False,
                default="true",
                description="true sends marked pages to gbrain.legacy and their timeline to gbrain.legacy_timeline",
            ),
            "oversize_chars": Declaration(
                env="GBRAIN_OVERSIZE_CHARS",
                required=False,
                default="20000",
                description="Pages longer than this many characters are listed by the report command",
            ),
        },
        secrets={
            "dsn": Declaration(
                env="GBRAIN_DSN", description="Read-only PostgreSQL source DSN"
            )
        },
        link_types=["about"],
    )

    def source(self):
        return read_source(self.secrets["dsn"])

    def execute(self, client, mode, dry_run=False):
        self.client, self.dry_run = client, dry_run
        self.quarantined = 0
        self.legacy_citations = 0
        result = super().execute(client, mode, dry_run)
        result["quarantined"] = self.quarantined
        if self.legacy_citations:
            result["legacy_citations_in_payload"] = self.legacy_citations
        logging.warning("GBrain quarantined=%d", self.quarantined)
        return result

    def included(self, data):
        """Source pages this run imports, by id (the deletion policy)."""
        keep = self.settings["include_deleted"].lower() == "true"
        return {p["id"]: p for p in data["pages"] if keep or not p["deleted_at"]}

    def legacy_on(self):
        return truthy(self.settings.get("legacy_channel", "true"))

    def channel_map(self, data, pages=None):
        """page id -> page channel, computed once per run from the live pages.

        Every endpoint (page versions, citations, timeline 'about' links and
        page-to-page links in both directions) and verify() use this map.
        pages defaults to every source page, deleted ones included.
        """
        if pages is None:
            pages = {p["id"]: p for p in data["pages"]}
        legacy = self.legacy_on()
        marker = self.settings.get("legacy_marker") or ""
        tags = defaultdict(list)
        for t in data["tags"]:
            tags[t["page_id"]].append(t["tag"])
        return {
            i: LEGACY if legacy and is_legacy(p, tags[i], marker) else PAGES
            for i, p in pages.items()
        }

    def quarantine(self, channel, message, *args):
        self.quarantined = getattr(self, "quarantined", 0) + 1
        self.quarantined_by[channel] += 1
        logging.warning(message, *args)

    def records(self, cursor, mode):
        data = self.source()
        self.quarantined_by = Counter()
        delivered, filtered = Counter(), Counter()
        legacy_citations = 0
        pages = self.included(data)
        every = self.channel_map(data)
        channel_of = {i: every[i] for i in pages}
        ids = {i: f"{p['source_id']}:{p['slug']}" for i, p in pages.items()}
        if len(set(ids.values())) != len(ids):
            raise ValueError("source page external IDs are not unique")
        by_external = {ids[i]: channel_of[i] for i in ids}
        for p in data["pages"]:
            if p["id"] not in pages:
                filtered[every[p["id"]]] += 1

        def located(cited):
            """Citations of imported pages resolve to the page's own channel."""
            return unique(
                [
                    endpoint(by_external[c["external_id"]], c["external_id"])
                    if c
                    and c["channel"] in PAGE_CHANNELS
                    and c["external_id"] in by_external
                    else c
                    for c in cited
                ]
            )

        def cite(channel, external_id, cited):
            nonlocal legacy_citations
            cited = located(cited)
            if channel in (LEGACY, LEGACY_TIMELINE):
                # Reference records are not derived: the core records provenance
                # only for page_revision and derived records. The citation stays
                # in the payload and is counted in the run report.
                legacy_citations += len(cited)
                return
            yield from citations(endpoint(channel, external_id), cited)

        for i in cited_first(pages, ids, data["page_versions"]):
            page = pages[i]
            channel = channel_of[i]
            try:
                aliases = [
                    a["alias_norm"]
                    for a in data["page_aliases"]
                    if (a["source_id"], a["slug"]) == (page["source_id"], page["slug"])
                ]
                aliases += [
                    a["alias_slug"]
                    for a in data["slug_aliases"]
                    if (a["source_id"], a["canonical_slug"])
                    == (page["source_id"], page["slug"])
                ]
                tags = sorted(t["tag"] for t in data["tags"] if t["page_id"] == i)
                # Progress is per page and channel: a page that later gains the
                # legacy marker is imported afresh into gbrain.legacy (the earlier
                # gbrain.pages record is never moved or deleted).
                key = "page_" + digest(
                    ids[i] if channel == PAGES else [channel, ids[i]]
                )
                state = (
                    {} if getattr(self, "dry_run", False) else self.client.cursor(key)
                )
                seen = set(state.get("snapshots", []))
                last = state.get("content")
                live = state.get("live")
                live_signature = digest([MAPPING_REVISION, page, aliases, tags])
                versions = sorted(
                    (v for v in data["page_versions"] if v["page_id"] == i),
                    key=lambda v: (v["snapshot_at"] or "", v["id"]),
                )
                # Dry-run reports current logical messages, not historical writes.
                if getattr(self, "dry_run", False):
                    versions = []
                sent = False
                for version in [*versions, None]:
                    signature = digest(version) if version else None
                    if signature and signature in seen:
                        continue
                    content = text(
                        page["title"],
                        version["compiled_truth"]
                        if version
                        else page["compiled_truth"],
                        page["timeline"],
                    )
                    content_hash = digest(content)
                    if signature:
                        seen.add(signature)
                    if (version is not None and content_hash != last) or (
                        version is None and live_signature != live
                    ):
                        fields = {
                            "slug": page["slug"],
                            "source_id": page["source_id"],
                            "page_type": page["type"],
                            "page_kind": page["page_kind"],
                            "title": page["title"],
                            "tags": tags,
                            "aliases": sorted(aliases),
                            "frontmatter": version["frontmatter"]
                            if version
                            else page["frontmatter"],
                            "updated_at": version["snapshot_at"]
                            if version
                            else page["updated_at"],
                            "deleted_at": page["deleted_at"],
                            "source_uri": page["source_uri"],
                        }
                        when = revision_time(page, version)
                        try:
                            usable = instant(when) is not None
                            subject = subject_of(page["effective_date"])
                        except ValueError:
                            usable, subject = False, None
                        mapped = dict(
                            channel=channel,
                            external_id=ids[i],
                            thread={"external_id": ids[i], "title": page["title"]},
                            happened_at=when,
                            content=fit(content),
                            fields=fields,
                            payload={
                                "page": page,
                                "version": version,
                                "tags": [t for t in data["tags"] if t["page_id"] == i],
                                "page_aliases": [
                                    a
                                    for a in data["page_aliases"]
                                    if (a["source_id"], a["slug"])
                                    == (page["source_id"], page["slug"])
                                ],
                                "slug_aliases": [
                                    a
                                    for a in data["slug_aliases"]
                                    if (a["source_id"], a["canonical_slug"])
                                    == (page["source_id"], page["slug"])
                                ],
                                "source": next(
                                    (
                                        s
                                        for s in data["sources"]
                                        if s["id"] == page["source_id"]
                                    ),
                                    None,
                                ),
                            },
                        )
                        if subject:
                            mapped["subject"] = subject
                        mapped["fields"]["source_hash"] = digest(mapped["payload"])
                        if not usable or "\\u0000" in json.dumps(mapped).replace(
                            "\\\\u0000", ""
                        ):
                            if version is None:
                                raise ValueError("unsafe page row")
                            # One bad snapshot costs only itself: later
                            # snapshots and the current page still import.
                            seen.discard(signature)
                            self.quarantine(
                                channel,
                                "GBrain quarantined page id=%s version id=%s",
                                i,
                                version.get("id"),
                            )
                            continue
                        yield mapped
                        delivered[channel] += 1
                        sent = True
                        # Delivered after the revision, so it cites that one.
                        yield from cite(channel, ids[i], page_citations(page, version))
                        last = content_hash
                        if version is None:
                            live = live_signature
                    yield Checkpoint(
                        key=key,
                        cursor={
                            "snapshots": sorted(seen),
                            "content": last,
                            "live": live,
                        },
                    )
                # An unchanged live page is still the current version: its
                # citations are sent again (the core keeps existing rows), so a
                # source imported after the page resolves on a later sync.
                if (
                    not sent
                    and not getattr(self, "dry_run", False)
                    and live == live_signature
                    and last
                    == digest(
                        text(page["title"], page["compiled_truth"], page["timeline"])
                    )
                ):
                    yield from cite(channel, ids[i], page_citations(page, None))
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                self.quarantine(channel, "GBrain quarantined page id=%s", i)
        # Endpoints exist before any links are delivered.
        for entry in sorted(data["timeline_entries"], key=lambda e: e["id"]):
            if entry["page_id"] not in pages:
                if entry["page_id"] in every:
                    filtered[TIMELINE_OF[every[entry["page_id"]]]] += 1
                continue
            page_channel = channel_of[entry["page_id"]]
            timeline = TIMELINE_OF[page_channel]
            external = str(entry["id"])
            try:
                day = date.fromisoformat(entry["date"])
            except (ValueError, TypeError):
                self.quarantine(
                    timeline, "GBrain quarantined timeline id=%s", entry["id"]
                )
                continue
            yield dict(
                channel=timeline,
                external_id=external,
                thread={"external_id": ids[entry["page_id"]]},
                happened_at=datetime.combine(day, time(), timezone.utc).isoformat(),
                subject=day_subject(day),
                content=text(entry["summary"], entry["detail"]),
                fields={
                    "source_hash": digest(entry),
                    "source": entry["source"],
                    "page_external_id": ids[entry["page_id"]],
                },
                payload=entry,
            )
            delivered[timeline] += 1
            yield from cite(timeline, external, [uri_citation(entry["source"])])
            yield Checkpoint(cursor={"timeline": external})
            yield source_link(
                "about",
                endpoint(timeline, external),
                endpoint(page_channel, ids[entry["page_id"]]),
                {},
            )
        for link in sorted(data["links"], key=lambda row: row["id"]):
            start, end = link["from_page_id"], link["to_page_id"]
            if start in ids and end in ids:
                # Sent as written: the core normalises the type, folds
                # synonyms and holds unregistered ones as candidates, so no
                # type fails a batch; evidence.source_record keeps the raw row.
                yield source_link(
                    link["link_type"],
                    endpoint(channel_of[start], ids[start]),
                    endpoint(channel_of[end], ids[end]),
                    {"context": link["context"], "source_record": link},
                )
        self.legacy_citations = legacy_citations
        yield from self.coverage_records(delivered, filtered)
        yield Checkpoint(cursor={"complete": True})

    def coverage_records(self, delivered, filtered):
        """One coverage report per channel: the whole source snapshot was read.

        complete (verified) unless rows were quarantined (partial with
        quarantined_rows and their count); pages and entries skipped by the
        deletion policy add the reason filtered_by_policy with their count in
        detail. Legacy channels are reported while GBRAIN_LEGACY_CHANNEL is on.
        """
        for channel in CHANNELS:
            if channel in (LEGACY, LEGACY_TIMELINE) and not self.legacy_on():
                continue
            reasons, detail = [], {}
            if filtered[channel]:
                reasons.append("filtered_by_policy")
                detail["skipped_deleted"] = filtered[channel]
            quarantined = self.quarantined_by[channel]
            if quarantined:
                reasons.append("quarantined_rows")
                detail["quarantined"] = quarantined
            yield coverage(
                channel,
                status="partial" if quarantined else "complete",
                reasons=reasons,
                rows_delivered=delivered[channel],
                verified=not quarantined,
                detail=detail or None,
            )

    def verify(self, client):
        data = self.source()
        pages = [
            p
            for p in data["pages"]
            if self.settings["include_deleted"].lower() == "true" or not p["deleted_at"]
        ]
        keys = {(p["source_id"], p["slug"]) for p in pages}
        aliases = sum(
            (a["source_id"], a["slug"]) in keys for a in data["page_aliases"]
        ) + sum(
            (a["source_id"], a["canonical_slug"]) in keys for a in data["slug_aliases"]
        )
        # Generate the complete expected stream independently of saved progress.
        import copy

        expected_plugin = copy.copy(self)
        expected_plugin.source = lambda: data
        expected_plugin.dry_run = False
        expected_plugin.client = type(
            "EmptyProgress", (), {"cursor": lambda self, key: {}}
        )()
        records = list(expected_plugin.records({}, "backfill"))
        expected = [r for r in records if isinstance(r, dict)]
        latest = {(r["channel"], r["external_id"]): r for r in expected}

        def read_rows(statement, key="id"):
            after = 0
            while True:
                result = client.request(
                    "POST",
                    "sql",
                    {
                        "statement": statement
                        + f" AND {key}>{after} ORDER BY {key} LIMIT 100"
                    },
                )
                rows = result["rows"]
                if not rows:
                    if result.get("truncated"):
                        raise ValueError("verification response exceeds SQL byte limit")
                    return
                yield from rows
                if len(rows) < 100 and not result.get("truncated"):
                    return
                next_id = int(rows[-1]["id"])
                if next_id <= after:
                    raise ValueError("verification pagination did not advance")
                after = next_id

        columns = "m.id,m.channel,m.external_id,m.content,m.fields,m.happened_at,m.thread_external_id,m.thread_title,r.subject_from,r.subject_from_precision"
        joined = (
            " m JOIN api.record_versions r ON r.message_id=m.id WHERE m.channel IN "
            + IN_CHANNELS
        )
        actual = list(read_rows("SELECT " + columns + " FROM api.messages" + joined))
        versions = list(
            read_rows("SELECT " + columns + " FROM api.message_versions" + joined)
        )

        def subject_key(row, source):
            if source:
                subject = row.get("subject") or {}
                start, precision = subject.get("start"), subject.get("start_precision")
            else:
                start, precision = (
                    row.get("subject_from"),
                    row.get("subject_from_precision"),
                )
            return [utc(start), precision] if start else None

        def projection(row, source=False):
            at = utc(row["happened_at"])
            fields = dict(row["fields"])
            for key in ("updated_at", "deleted_at"):
                if fields.get(key):
                    fields[key] = (
                        datetime.fromisoformat(fields[key])
                        .astimezone(timezone.utc)
                        .isoformat()
                    )
            return digest(
                [
                    row["channel"],
                    row["external_id"],
                    row["content"],
                    fields,
                    at,
                    row["thread"]["external_id"]
                    if source
                    else row["thread_external_id"],
                    row["thread"].get("title") if source else row["thread_title"],
                    subject_key(row, source),
                ]
            )

        # Types are compared as written: the core normalises and folds them
        # and keeps the raw text in type_as_written.
        links = list(
            read_rows(
                "SELECT l.id,coalesce(l.type_as_written,l.type) AS type,l.method,l.evidence,a.channel AS from_channel,a.external_id AS from_external_id,b.channel AS to_channel,b.external_id AS to_external_id FROM api.links l JOIN api.message_versions a ON l.from_kind='message' AND a.id=l.from_id JOIN api.message_versions b ON l.to_kind='message' AND b.id=l.to_id WHERE a.channel IN "
                + IN_CHANNELS
                + " AND l.status='active'",
                "l.id",
            )
        )
        expected_links = {
            digest(
                [
                    r.data["type"],
                    r.data["method"],
                    r.data.get("evidence", {}),
                    r.data["from"]["channel"],
                    r.data["from"]["external_id"],
                    r.data["to"]["channel"],
                    r.data["to"]["external_id"],
                ]
            )
            for r in records
            if isinstance(r, Record) and r.kind == "link"
        }
        actual_links = {
            digest(
                [
                    r["type"],
                    r["method"],
                    r["evidence"],
                    r["from_channel"],
                    r["from_external_id"],
                    r["to_channel"],
                    r["to_external_id"],
                ]
            )
            for r in links
        }
        page_rows = [r for r in actual if r["channel"] in PAGE_CHANNELS]
        # The destination's history only grows: it legitimately keeps earlier
        # live versions that the source no longer has, and a snapshot equal to
        # the version stored before it is not stored twice. So the history is
        # checked in one direction: every source snapshot must be the body of
        # some stored version of its page. Being a part of a longer stored
        # body is not enough: on a page that grows by appending, every earlier
        # snapshot is the start of the current text.
        external = {p["id"]: f"{p['source_id']}:{p['slug']}" for p in pages}
        bodies = {}
        for p in pages:
            if (p["compiled_truth"] or "").strip():
                bodies.setdefault(external[p["id"]], set()).add(p["compiled_truth"])
        for v in data["page_versions"]:
            if v["page_id"] in external and (v["compiled_truth"] or "").strip():
                bodies.setdefault(external[v["page_id"]], set()).add(
                    v["compiled_truth"]
                )
        held = held_bodies(
            [row for row in versions if row["channel"] in PAGE_CHANNELS], bodies
        )
        missing_snapshots = {
            v["id"]
            for v in data["page_versions"]
            if v["page_id"] in external
            and (v["compiled_truth"] or "").strip()
            and (external[v["page_id"]], v["compiled_truth"]) not in held
        }
        # Revision times of history, in the same one direction: every source
        # snapshot the replay imports must be held by a stored version at its
        # snapshot time, or by an earlier stored version of the same body whose
        # record time is its own revision time (GBrain snapshotted a body that
        # was already stored as the live page). A version stored under the old
        # mapping has a record time that is not its revision time.
        held_times = defaultdict(list)
        for row in versions:
            if row["channel"] not in PAGE_CHANNELS:
                continue
            at = utc(row["happened_at"])
            revised = row["fields"].get("updated_at")
            consistent = not revised or utc(revised) == at
            found, _ = stored_candidates(
                row["content"],
                row["fields"].get("title", row.get("thread_title")),
                bodies.get(row["external_id"], ()),
            )
            for body in found:
                held_times[(row["external_id"], body)].append((at, consistent))
        mistimed = set()
        for r in expected:
            version = (
                r["payload"].get("version") if r["channel"] in PAGE_CHANNELS else None
            )
            if (
                not version
                or version["id"] in missing_snapshots
                or not (version["compiled_truth"] or "").strip()
            ):
                continue
            wanted = utc(r["happened_at"])
            if not any(
                at == wanted or (at < wanted and consistent)
                for at, consistent in held_times[
                    (r["external_id"], version["compiled_truth"])
                ]
            ):
                mistimed.add(version["id"])
        result = Verification(
            (
                (len(pages), len(page_rows)),
                (aliases, sum(len(r["fields"].get("aliases", [])) for r in page_rows)),
                (
                    Counter(projection(r, True) for r in latest.values()),
                    Counter(projection(r) for r in actual),
                ),
                (set(), missing_snapshots),
                (expected_links, actual_links),
                (set(), mistimed),
            )
        )
        expected_counts = Counter(channel for channel, _ in latest)
        actual_counts = Counter(r["channel"] for r in actual)
        result.report = dict(
            pinned_upstream=PINNED_UPSTREAM,
            schema_check=data.get("schema_check", "not run"),
            **self.citation_counts(pages, read_rows),
            # Per channel: current records the source maps there and current
            # records stored there. Pages imported into gbrain.pages before
            # they carried the legacy marker show up as extra gbrain.pages
            # records (README, "Legacy pages").
            channels={
                name: {
                    "expected": expected_counts[name],
                    "actual": actual_counts[name],
                }
                for name in CHANNELS
            },
        )
        return result

    @staticmethod
    def citation_counts(pages, read_rows):
        """How many current pages name sources the destination holds.

        Reported, never compared: a page without resolvable sources is gated
        by its own channel grant only.
        """
        cited = {p["id"]: page_citations(p, None) for p in pages}
        keys = sorted(
            {(c["channel"], c["external_id"]) for cs in cited.values() for c in cs}
        )
        found = set()

        def literal(value):
            return "'" + value.replace("'", "''") + "'"

        for start in range(0, len(keys), 100):
            values = ",".join(
                f"({literal(channel)},{literal(external_id)})"
                for channel, external_id in keys[start : start + 100]
            )
            for row in read_rows(
                "SELECT m.id,m.channel,m.external_id FROM api.messages m WHERE (m.channel,m.external_id) IN ("
                + values
                + ")"
            ):
                found.add((row["channel"], row["external_id"]))
        resolved = {
            page: [c for c in cs if (c["channel"], c["external_id"]) in found]
            for page, cs in cited.items()
        }
        return dict(
            pages_with_sources=sum(bool(cs) for cs in resolved.values()),
            pages_without_sources=sum(not cs for cs in resolved.values()),
            unresolved_citations=sum(
                len(cited[page]) - len(cs) for page, cs in resolved.items()
            ),
        )

    def report(self, client):
        """Graph hygiene of the source for the owner (needs an owner token).

        Reads the source snapshot and the core registry; writes nothing.
        Returns orphan pages (no link in or out in the source), link types
        split by registry status (active, folded synonyms, candidate,
        retired_but_written, retired, unregistered), oversized pages with
        their stored passage counts and person pages compared with the people
        of the identity registry. A part the core cannot answer (an older
        core, or a token without access) is null with a reason in `missing`.
        """
        data = self.source()
        pages = self.included(data)
        channel_of = self.channel_map(data, pages)
        ids = {i: f"{p['source_id']}:{p['slug']}" for i, p in pages.items()}
        missing = {}
        linked = set()
        for link in data["links"]:
            linked.update((link["from_page_id"], link["to_page_id"]))
        orphans = [
            {"external_id": ids[i], "channel": channel_of[i], "title": p["title"]}
            for i, p in sorted(pages.items())
            if i not in linked
        ]
        written = Counter(
            link["link_type"]
            for link in data["links"]
            if link["from_page_id"] in ids and link["to_page_id"] in ids
        )
        link_types = None
        try:
            rows = {}
            for params in (
                {"with_counts": "true"},
                {"with_counts": "true", "status": "candidate"},
                {"with_counts": "true", "status": "retired"},
            ):
                answer = client.request("GET", "link-types", params=params)
                for row in answer.get("rows", []):
                    rows[row["type"]] = row
            link_types = link_type_report(written, rows)
        except Exception as error:
            missing["link_types"] = type(error).__name__
            link_types = link_type_report(written, None)
        limit = int(self.settings.get("oversize_chars") or 20000)
        big = {
            i: len(text(p["title"], p["compiled_truth"], p["timeline"]))
            for i, p in pages.items()
        }
        big = {i: n for i, n in big.items() if n > limit}
        counts = {}
        if big:
            try:
                counts = self.passage_counts(
                    client, sorted({(channel_of[i], ids[i]) for i in big})
                )
            except Exception as error:
                missing["passages"] = type(error).__name__
        oversized = [
            {
                "external_id": ids[i],
                "channel": channel_of[i],
                "title": pages[i]["title"],
                "characters": n,
                "passages": counts.get((channel_of[i], ids[i]))
                if not missing.get("passages")
                else None,
            }
            for i, n in sorted(big.items(), key=lambda item: (-item[1], item[0]))
        ]
        person_pages = [
            p for p in pages.values() if (p["type"] or "").lower() == "person"
        ]
        people = None
        try:
            people = [
                p
                for p in client.request("GET", "identity/people")
                if not p.get("merged_into")
            ]
        except Exception as error:
            missing["people"] = type(error).__name__
        known = {
            str(value).strip().lower()
            for p in people or []
            for value in (p.get("display_name"), p.get("handle"))
            if value
        }
        return {
            "pages": len(pages),
            "orphan_pages": {"count": len(orphans), "pages": orphans},
            "link_types": link_types,
            "oversized_pages": {
                "threshold_chars": limit,
                "count": len(oversized),
                "pages": oversized,
            },
            "person_pages": {
                "pages": len(person_pages),
                "registry_people": None if people is None else len(people),
                "without_registry_person": None
                if people is None
                else sorted(
                    ids[p["id"]]
                    for p in person_pages
                    if (p["title"] or "").strip().lower() not in known
                    and p["slug"].strip().lower() not in known
                ),
            },
            "missing": missing,
        }

    @staticmethod
    def passage_counts(client, keys):
        """{(channel, external_id): passages of the current version}."""
        found = {}

        def literal(value):
            return "'" + value.replace("'", "''") + "'"

        for start in range(0, len(keys), 100):
            values = ",".join(
                f"({literal(channel)},{literal(external_id)})"
                for channel, external_id in keys[start : start + 100]
            )
            rows = client.request(
                "POST",
                "sql",
                {
                    "statement": "SELECT m.channel,m.external_id,"
                    "(SELECT count(*) FROM api.passages_of(m.id)) AS passages"
                    " FROM api.messages m WHERE (m.channel,m.external_id) IN ("
                    + values
                    + ")"
                },
            )["rows"]
            for row in rows:
                found[(row["channel"], row["external_id"])] = int(row["passages"])
        return found


def link_type_report(written, rows):
    """Source link types by registry status, with source and stored counts.

    written: Counter of raw source types. rows: registry rows by type (None
    when the core has no registry): each raw type normalises to a name that
    is a synonym (folded), a registered type (by its status) or unknown.
    """
    groups = {
        k: []
        for k in (
            "active",
            "folded",
            "candidate",
            "retired_but_written",
            "retired",
            "unregistered",
        )
    }
    synonyms = {}
    for row in (rows or {}).values():
        for alias in row.get("synonyms") or []:
            synonyms[alias] = row["type"]
    merged = {}
    for raw, count in sorted(written.items(), key=lambda item: str(item[0])):
        name = normalize_type(raw)
        if name in synonyms:
            group, target = "folded", synonyms[name]
        elif rows is not None and name in rows:
            status = rows[name].get("status")
            target = name
            group = (
                "retired_but_written"
                if status == "retired" and rows[name].get("retired_but_written")
                else status
                if status in groups
                else "unregistered"
            )
        else:
            group, target = "unregistered", name
        entry = merged.setdefault(
            (group, target),
            {
                "type": target,
                "raw_types": [],
                "source_links": 0,
                "stored_active_links": (rows or {}).get(target, {}).get("active_links"),
            },
        )
        entry["raw_types"].append(raw)
        entry["source_links"] += count
    for (group, _), entry in sorted(merged.items()):
        groups[group].append(entry)
    return dict(
        groups,
        distinct_source_types=len(written),
        distinct_types_after_registry=len({t for _, t in merged}),
        registry=None if rows is None else len(rows),
    )


def main():
    if sys.argv[1:] == ["report"]:
        try:
            plugin = GBrain()
            client = Client(
                os.environ["THROUGHLINE_URL"], os.environ["THROUGHLINE_TOKEN"], "gbrain"
            )
            try:
                print(json.dumps(plugin.report(client), sort_keys=True, indent=2))
            finally:
                client.close()
        except Exception:
            raise SystemExit(1) from None
        return
    if sys.argv[1:] == ["verify"]:
        try:
            plugin = GBrain()
            client = Client(
                os.environ["THROUGHLINE_URL"], os.environ["THROUGHLINE_TOKEN"], "gbrain"
            )
            try:
                pairs = plugin.verify(client)
                for a, b in pairs:
                    print(a, b) if isinstance(a, int) else print(
                        "match" if a == b else "MISMATCH"
                    )
                for name, value in pairs.report.items():
                    print(name, json.dumps(value, sort_keys=True))
                raise SystemExit(int(any(a != b for a, b in pairs)))
            finally:
                client.close()
        except Exception:
            raise SystemExit(1) from None
    run(GBrain)
