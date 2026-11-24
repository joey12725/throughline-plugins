# GBrain source importer

Independent read-only PostgreSQL importer; does not vendor, modify, or start
GBrain. Install with `uv sync --all-packages`. Runtime dependency `psycopg[binary]`
belongs to this package. Obtain a DSN for an existing GBrain database from its
administrator, preferably using a SELECT-only role. No upstream HTTP API or
external account is involved.

## Configuration and commands

Set these environment variables in the shell (values below are placeholders):

```powershell
$env:GBRAIN_DSN = '<source PostgreSQL DSN>'
$env:THROUGHLINE_URL = '<Throughline base URL>'
$env:THROUGHLINE_TOKEN = '<owner or gbrain-scoped plugin token>'
$env:GBRAIN_INCLUDE_DELETED = 'false'
uv run --package throughline-gbrain throughline-gbrain manifest
uv run --package throughline-gbrain throughline-gbrain backfill --dry-run
uv run --package throughline-gbrain throughline-gbrain backfill
uv run --package throughline-gbrain throughline-gbrain sync --once
uv run --package throughline-gbrain throughline-gbrain sync
```

`GBRAIN_DSN` is a declared secret and is never logged. `GBRAIN_INCLUDE_DELETED`
defaults to `false`; set `true` to include soft-deleted pages and their events.
The SDK reads `THROUGHLINE_TOKEN` and redacts it as well. Sync polls hourly and
scans a repeatable-read, read-only source snapshot each time, finding new pages,
snapshots and events through the same generator as backfill.

Verify requires an **owner token**, because aggregate SQL is unavailable to
plugin-scoped tokens (an agent can query but cannot import messages):

```powershell
uv run --package throughline-gbrain throughline-gbrain verify
```

Output starts with source/destination page and alias counts, followed by match
or MISMATCH for current records (including record time and subject time),
history, links and history revision times. Exit 1 on any mismatch or failure.
Report lines follow and are never compared: `pinned_upstream`, `schema_check`,
`channels` (expected and actual current records per channel),
`pages_with_sources`, `pages_without_sources` and `unresolved_citations`.
Comparisons use the same deletion filter and cover the complete channels; use
one source database per installation.

Graph hygiene report (owner token; reads the source and the core, writes
nothing):

```powershell
uv run --package throughline-gbrain throughline-gbrain report
```

It prints JSON: `orphan_pages` (pages with no link in or out in the source),
`link_types` (source types as written with counts, split into `active`,
`folded` (a synonym of an active type), `candidate`, `retired_but_written`,
`retired` and `unregistered`, from `GET /v1/link-types?with_counts=true` and
`?status=candidate|retired`, so the owner can merge the long tail of upstream
types into a few dozen), `oversized_pages` (pages over `GBRAIN_OVERSIZE_CHARS`,
default 20,000 characters, with their stored passage counts from
`api.passages_of`) and `person_pages` (person pages compared with the people of
the identity registry). A part the core cannot answer (an older core, a token
without access) is null and named in `missing`.

Further settings:

| Variable | Default | Meaning |
|---|---|---|
| `GBRAIN_LEGACY_MARKER` | `source_copy` | Marker of legacy bulk pages: a frontmatter key set to true, a frontmatter value or list item, a page flag column (`page_kind`, `type`, `source_kind`, `ingested_via`) or a tag equal to it (case and surrounding space ignored) |
| `GBRAIN_LEGACY_CHANNEL` | `true` | Send marked pages to `gbrain.legacy` and their timeline to `gbrain.legacy_timeline`; `false` keeps them in `gbrain.pages` |
| `GBRAIN_OVERSIZE_CHARS` | `20000` | Pages longer than this are listed by `report` |

## Coverage

Each run reports coverage per channel (`gbrain.pages`, `gbrain.timeline` and,
while `GBRAIN_LEGACY_CHANNEL` is on, `gbrain.legacy` and
`gbrain.legacy_timeline`), scope key `''`. A run reads the whole source
snapshot, so a channel is `complete` (verified). Pages and timeline entries
skipped by the deletion policy (`GBRAIN_INCLUDE_DELETED=false`) add reason
`filtered_by_policy` with `skipped_deleted` in the detail; quarantined rows
make the channel `partial` with `quarantined_rows` and their count. Neither
names a window, so no gap opens. A failed run is reported by the SDK as
`partial` with `run_failed`.

## Legacy bulk pages

Bulk copies of source documents (the legacy marker above) would swamp recall,
so they go to their own reference channels: `gbrain.legacy` (record kind
`reference`, `is_reference`, no passages) and `gbrain.legacy_timeline`
(record kind `reference`). Both stay out of default recall and are opt-in with
`kinds: ["reference"]`.

Endpoints are channel-aware: a page-id to channel map is computed once per run
and used for page versions, citations (`cites` provenance), timeline `about`
links and page-to-page links in both directions, so a link from a live page to
a legacy page resolves to the legacy record. `verify` covers all four channels
and reports counts per channel (the `channels` report line: expected and
actual current records of each channel).

The importer never moves or deletes records. Pages imported into
`gbrain.pages` before they carried the marker (or before this version) stay
there, and `verify` shows them as extra `gbrain.pages` records. To move them,
the owner purges the channels (`throughline purge gbrain.pages --confirm`,
likewise `gbrain.timeline`, `gbrain.legacy` and `gbrain.legacy_timeline`),
resets the plugin cursor (`PUT /v1/plugins/gbrain/cursor/default` with `{}`)
and runs `backfill` again.

## Link types

Upstream link types are sent as written. The core normalises every type
(total: symbol-only and very long types get a stable name), folds synonyms and
holds undeclared types as candidates; it never refuses a source link for its
type, so a batch never fails on a type. Nothing is invented: the raw type is
kept in `evidence.source_record` and as `type_as_written`, and `verify`
compares types as written.

## Imported shape

`gbrain.pages` has record kind `page_revision` and stays a reference channel
(`is_reference`), so legacy core search excludes it by default and the core never
attaches locations (`loc_reason` is `not_applicable`). Pages enter recall through
passages: `passage_text_paths` is `/page/title`,
`/version/compiled_truth|/page/compiled_truth` (a snapshot's own body, else the
live body) and `/page/timeline`, all read from the stored payload, so passage
text is the full page even when the indexed content is cut. Stable external IDs
are `<source_id>:<slug>`; the page is its thread.

Every snapshot and the live page are versions of one record, and each version's
record time (`happened_at`) is its revision time: `snapshot_at` for a snapshot,
`updated_at` for the live page, and `created_at` only when that is missing.
`api.records_asof(at)` therefore returns the text the page had at `at`.
`effective_date` is subject time: `subject.start` with precision `day` for a date
(a date column, or a timestamptz at exactly 00:00 UTC, which is how GBrain holds
a date) and `exact` for any other time; a page without one has no subject. A
revision whose time is unusable (missing or without an offset) is quarantined
like other unsafe rows: a snapshot costs only itself, a live page the whole page.
The `updated_at` field keeps the same revision time.
Content joins title, compiled truth, and free-form timeline text.
Fields: `slug`, `source_id`, `page_type`, `page_kind`, `title`, `tags` (JSON array),
`aliases` (JSON array combining page aliases and prior slugs), `frontmatter`
(JSON), `updated_at`, `deleted_at`, and `source_uri`. Timestamps are typed;
other scalar fields are text. Readable descriptions are in the manifest.
The raw payload contains the selected source page columns, untouched snapshot
record, tag and alias rows, and source metadata. Search/embedding bookkeeping
columns are deliberately excluded. PostgreSQL timestamptz loses the original
lexical offset; the connection's returned offset is preserved, not invented.

Snapshots replay by `(snapshot_at,id)` before the current page, one acknowledged
write at a time. Consecutive identical text does not create a version. Durable
per-page cursors remember acknowledged snapshot hashes and last content; even a
reset of the default cursor cannot roll current text back through old history.
Do not delete these per-page cursors independently of the destination messages.
On a lost acknowledgement, the next run repeats only the pending version.
Dry-run validates current logical messages and reports logical counts/date
bounds, while leaving history/cursors/links unchanged.

`gbrain.timeline` has record kind `derived`: one message per entry ID, record
time at UTC midnight of its date and subject `{start: that date, start_precision:
'day'}` (source dates have no clock or timezone), summary plus detail, fields
`source` and `page_external_id`, untouched entry payload. An `about` source link
connects it to the page. Page links preserve `link_type`, with method `source`,
context and original link row as evidence. Links to excluded or missing pages
are omitted. `link_types` declares `about`; additional types are source-defined.
`provenance_link_types` declares `cites`: a source link of that type from a
page makes its target a provenance source of the page.

## Source citations

A page or entry names its sources only where the upstream data names
Throughline records; nothing is inferred:

* page frontmatter key `throughline_sources`, a list of
  `{"channel": ..., "external_id": ...}` (the frontmatter of each snapshot is
  used for that snapshot);
* a page `source_uri` or timeline entry `source` of the form
  `throughline://<channel>/<external_id>` (the external ID is URL-decoded).

Each cited revision is followed by a provenance record (SDK `provenance`,
relation `cites`; more than 1,000 sources are split over several records),
delivered to `POST /v1/provenance` after the revision itself. Unknown keys are
only counted by the core; the run report gives `provenance.records`, `written`
and `unresolved`. Every sync sends the citations of each unchanged live page and
each timeline entry again (the core keeps rows it already has), so a source
imported after the page resolves on a later sync. A page cited by another page
of the same source is delivered before the page citing it, so such citations
(also of legacy pages) resolve in the same run. A key containing NUL, also
after URL decoding, is not a citation. Other citation forms (file
paths, URLs, free text) stay in `fields` and the payload. A page with sources is
visible to a restricted asker only when every source is. **Unsourced pages are
flagged unsupported and are gated by their own channel grant only.** verify
reports how many current pages have resolvable sources and how many do not.

Provenance belongs to one stored version. Versions stored before mapping
revision 3 carry none, and a suppressed snapshot (same body as the version
before it) adds no citations of its own, so in history a restricted asker with
a `gbrain.pages` grant can see such versions whatever their sources; the current
version is always cited. A purge and re-import (below) records citations for
the whole history.

## Upstream compatibility

`REQUIRED_SCHEMA` lists every source table and column the adapter reads. Inside
the same `REPEATABLE READ READ ONLY` transaction, before any data query, the
adapter checks it through `information_schema.columns` and stops with an error
naming each missing `table.column`; extra tables and columns are tolerated and
never read. `PINNED_UPSTREAM` is `None`: no upstream GBrain release has been
verified yet. The owner sets it to the verified upstream release, and
`tests/source.sql` is then regenerated from that release's real schema (tables
and columns in `REQUIRED_SCHEMA`, types as upstream defines them). verify
reports the pin and the check result.

Before v1.1 every table except `pages` was read with all its columns. A source
with columns outside `REQUIRED_SCHEMA` in `links`, `timeline_entries` or
`sources` therefore gets narrower payloads and link evidence after the upgrade:
the timeline entries take one new version, and each page link is asserted again
with the narrower evidence beside the old assertion, which verify reports as a
links MISMATCH. Compare the source schema with `REQUIRED_SCHEMA` before
upgrading; the purge and re-import below removes the old assertions.

## Mapping revision 3 (one-off re-version)

Mapping revision 3 moved page record time from `effective_date` to revision
time and added subject time. This is a deliberate one-off: the next sync after
the upgrade sends every live page and every timeline entry once more, which
stores one new version of each; later syncs send nothing new. Snapshots
imported under the old mapping are not sent again and keep their old record
time, so verify reports them as history with wrong revision times. Exact times
for already imported history need an owner purge of both channels
(`gbrain.pages`, `gbrain.timeline`), a cursor reset and a re-import. Identical
consecutive snapshots are still suppressed, and old snapshots carry the
present-day title, tags and aliases, so as-of answers on imported history are
approximate.

## Limits and core gaps

* A live page missing both update and creation timestamps is quarantined; no
  guessed date.
* This is a scan importer, currently holding the source snapshot in memory.
* Consecutive snapshots with identical bodies are suppressed. Current page
  metadata changes create a final version, including on the first import.
* Old snapshots contain only body/frontmatter/time. Historical titles, aliases,
  tags, and timeline text cannot be reconstructed; present page context is used.
* Imported pages are page revisions projected from GBrain, not native pages
  (BRN-1..3). Source URIs, source metadata, frontmatter and page links survive;
  no absent source-message IDs or claim evidence are invented. Rebuild/owner-edit
  precedence requires the native page subsystem, outside this source plugin.
* Source hard deletion, renaming a canonical slug, changing source namespaces,
  or removing links does not retract immutable imported records. Do not point
  an existing import's cursors at another source or restore them onto an empty
  destination. Core has no import transaction spanning all pages.
  A later soft deletion also does not retract a page already imported; the
  default filter only skips it on subsequent scans. Use a consistent deletion
  setting and an initially empty import channel for count comparisons.

Tests use synthetic tables matching the supplied schema in randomly named
`tl_test_` databases only. The contract fixture intentionally has no differing
history: the kit assumes one delivered version per external ID and reverses
all delivery. A separate real-core history test checks snapshot order, identical
snapshot suppression, acknowledgement interruption, replay and verification.
The test fixture retries only the core's known `ALTER ROLE` / `tuple concurrently
updated` startup race, up to eight attempts and only for loopback `tl_test_`
migrations. This is a test-only workaround for cluster-wide role DDL protected
by a database-local advisory lock (`src/throughline/db.py:52`); other errors fail.

```powershell
uv run --no-sync pytest -q plugins/gbrain
```

Contracts: [plugin authoring](../../docs/writing-a-plugin.md),
[HTTP API](../../docs/plugin-http-contract.md),
[architecture](../../docs/architecture.md).
Database reference: [Psycopg connections](https://www.psycopg.org/psycopg3/docs/basic/usage.html),
[PostgreSQL read-only transactions](https://www.postgresql.org/docs/current/sql-set-transaction.html).


Verification now compares current page/timeline IDs, content, every typed field,
event time and thread, raw payload SHA256 fingerprints, and source link endpoints,
types, methods and evidence, all exactly. History is checked separately and in
one direction only, as described below. A mismatch in any of these returns a
failing exit code.
The live page is always the final state when its metadata changes, even when its
body equals the latest snapshot. Snapshot-to-snapshot identical bodies remain
suppressed. Timeline entries without a valid date are quarantined by ID and
counted, rather than inventing a date or aborting later links.

The history check runs in one direction. The destination's history only grows:
after a sync that captured a source edit it also holds the earlier live
version, and a snapshot whose text equals the version stored before it is not
stored twice. Verify therefore requires that every source snapshot is the body
of some stored version of its page, and reports MISMATCH only when one is
absent. A stored version's content is title, body and timeline joined by blank
lines; the body it holds is the longest known body of that page (source
snapshots and the current text) that is followed there by the end of the
content or by the blank-line separator. A snapshot that is merely the start of
a longer stored body, as on a page that grows by appending, is therefore not
counted as present. Text cut for indexing is compared up to the cut. The
timeline of an old version is not readable through the SQL endpoint, so in the
rare case that a stored timeline begins with exactly the text by which a later
body continues an earlier one, the earlier snapshot is reported missing; rerun
after checking by hand. Extra stored history, including
snapshots the source deleted later, is not a mismatch. Current pages, timeline
entries and links are still compared exactly.

The live-page signature in the cursor includes a mapping revision. After an
upgrade that changes the mapping, the next sync sends every current page once
more, so a destination imported by an older version gains the new fields
(`source_hash` in revision 2; revision time and subject in revision 3, see
above) without a cursor reset; run sync before verify.

Content above 250,000 UTF-8 bytes is cut for full-text indexing and ends with
`[Truncated for indexing; the payload holds the full text]`; the payload keeps
the whole page or snapshot. The limit keeps unusual text under PostgreSQL's
1 MB tsvector cap. A snapshot with a NUL is quarantined alone (logged with its
page and version ID, counted, and reported again on later runs): the page's
other snapshots and its current version still import. A current page with a
NUL or a timestamp without an offset is quarantined as a whole.
