# Throughline Notion connector

Read-only import of the owner's Notion workspace through the Composio Notion
toolkit. The connector never calls a Notion write action.

It has the same two-layer shape as the calendar and email connectors:

- `src/throughline_notion/fetch.py` uses only the Python standard library. Run
  it beside the Composio credential to produce JSONL plus a non-secret cursor.
- `throughline_notion:Notion` maps a live fetch or one or more JSONL exports to
  the Throughline HTTP contract.

## Channels

`notion.pages` is a page-revision channel. A stable Notion page UUID is the
external id and its own thread id, so a later edit becomes a new immutable
version rather than another page. The record includes the title, typed Notion
properties, readable property text, parent/database, URL, creation and edit
times, creator/editor ids and names, archive/trash state, and Notion-flavoured
Markdown returned by `NOTION_GET_PAGE_MARKDOWN`.

`notion.databases` is a page-revision channel for database schemas. Database
rows are already pages and therefore live in `notion.pages`; their
`database_id` and a source `part_of` link connect them to the schema record.

Both channels store their full searchable text under `payload.content` for
page passages. Content is capped at `NOTION_CONTENT_LIMIT` (500,000 characters
by default), and both local and upstream truncation are explicit in fields and
coverage. Set the limit to `0` for no local cap.

Notion's public API exposes the current page and timestamps, not historical
revision bodies. A first import captures every currently accessible page and
its original creation time; Throughline then accumulates every observed edit
as a page version. Revisions from before the connector was installed cannot be
reconstructed through this API.

## Incremental model and deletion honesty

Every run paginates a complete metadata scan of all pages shared with the
Notion integration, sorted newest-first by `last_edited_time`. The stored
high-water mark plus `NOTION_OVERLAP_S` determines which page bodies are
refetched. Metadata fingerprints catch changes at the cursor boundary.
Database schemas and workspace users are also paginated.

A page visible as `archived` or `in_trash` is recorded that way. If a known
page disappears from a complete scan, the connector records `missing=true`
and says it may be deleted, trashed, or no longer shared. It deliberately does
not claim a hard deletion that Notion can no longer prove. A later reappearance
creates another version.

Coverage is complete for the integration's accessible scope after a successful
full metadata scan. Page fetch errors open a `source_error` gap over their edit
times. Truncated pages make page coverage partial with `source_cap`.

## Configuration

Secrets are environment-only and are never accepted as command arguments or
written to JSONL/state:

| Variable | Meaning |
|---|---|
| `COMPOSIO_MCP_TOKEN` | Required Composio MCP bearer token |
| `COMPOSIO_MCP_URL` | Optional MCP URL; defaults to `https://connect.composio.dev/mcp` and is treated as secret because some URLs embed credentials |

Settings:

| Variable | Default | Meaning |
|---|---:|---|
| `NOTION_SOURCE` | `live` | `live` or `export:<file-or-directory>` |
| `NOTION_ACCOUNT` | automatic | Optional Composio connected-account alias/id; omit when the MCP session has one Notion account |
| `NOTION_PAGE_SIZE` | `100` | Metadata page size, 1–100 |
| `NOTION_CONTENT_BATCH` | `8` | Markdown actions per Remote Workbench request |
| `NOTION_OVERLAP_S` | `3600` | Last-edited overlap around the cursor |
| `NOTION_CONTENT_LIMIT` | `500000` | Markdown characters stored per page; `0` is unlimited |
| `NOTION_MAX_CALLS_PER_RUN` | `0` | Composio action budget; `0` is unlimited |
| `NOTION_MIN_INTERVAL_S` | `0.5` | Minimum delay between MCP requests |
| `NOTION_RETRY_BASE_S` | `2` | Initial exponential retry delay |
| `NOTION_MAX_RETRIES` | `5` | Retries for 429, 5xx, timeout, and transient tool errors |
| `NOTION_TIMEOUT_S` | `240` | MCP request timeout |
| `NOTION_CHECKPOINT_EVERY` | `100` | Mapped records per Throughline checkpoint |

## Export and import

Run the standalone fetcher where the credential exists:

```sh
export COMPOSIO_MCP_TOKEN="$(your-secret-reader COMPOSIO_MCP_TOKEN)"
python3 fetch.py export --out notion.jsonl --state notion-state.json
```

The first state-less run is the full accessible-history import. Reusing the
state file makes later runs incremental. Advance that state only after the
JSONL has been ingested successfully; the installed sync wrapper does this.

Import an export:

```sh
export NOTION_SOURCE=export:/path/to/notion.jsonl
export THROUGHLINE_URL=http://127.0.0.1:8791
throughline-notion backfill
throughline-notion sync --once
```

For an in-process fetch, set `COMPOSIO_MCP_TOKEN` (or
`COMPOSIO_MCP_TOKEN_COMMAND`, a command that prints it, such as your secret
manager) and leave `NOTION_SOURCE=live`. When the token must stay on another
host, copy the standard-library `fetch.py` there, run it beside the token and
ship only the JSONL plus state back (see
[the remote-fetch pattern](../README.md#remote-fetch-pattern)).

## Tests

All tests use invented fixtures and fake transports; they never call Notion or
Composio:

```sh
uv run --no-sync pytest plugins/notion/tests -q
```
