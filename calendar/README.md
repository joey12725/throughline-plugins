# Throughline Calendar plugin

> **Needs a Composio account.** This plugin reads Google Calendar through a
> connection you make in [Composio](https://composio.dev); it does not talk to
> Google directly. For a calendar without Composio use the `caldav` plugin
> (any CalDAV server, including Google, iCloud and Fastmail) or `msgraph`
> (Microsoft 365 / Outlook.com).

Google Calendar through [Composio](https://docs.composio.dev/) (Google Calendar
toolkit), for one or more connected accounts and every calendar they list.

Two layers:

- `src/throughline_calendar/fetch.py` is a standalone, standard-library-only
  fetcher (Python 3.12 to 3.14, no third-party imports). Copy that one file to
  any host and run `python fetch.py export --out FILE`. It reads Composio and
  writes raw JSONL records. Credentials come only from environment variables.
- The plugin (`throughline-calendar`) maps those raw records to Throughline
  messages. It reads either export files (`CALENDAR_SOURCE=export:<path>`) or
  calls the same fetcher in-process (`CALENDAR_SOURCE=live`). The mapping is
  identical in both cases, so a live run and a shipped export give the same
  messages.

## What it captures

| Channel | One message per | Record time (`happened_at`) | Subject |
|---|---|---|---|
| `calendar.events` | event occurrence; recurring series expanded inside the fetch window | when the occurrence starts (`ended_at` = its end) | the occurrence's start and end |
| `calendar.changes` | lifecycle step of an event: `created`, `changed`, `cancelled`, `removed`, `restored` | the API's `created` or `updated` time; for changes found by snapshot diff, the fetch time | the event's start and end after the step |

All-day events have no zone of their own: they start at UTC midnight, the
subject has `day` precision, and the subject end is the last day (inclusive)
while `ended_at` is the exclusive end. Timed events keep the offset Google
returns.

Occurrence fields: account, calendar id and name, event id, series id, iCalUID,
title, status, all-day flag, time zone, original start of a moved instance,
place (location text), description (HTML converted to text), organizer email
and name, attendees (json: email, name, response, organizer, self, optional,
resource), attendee count, the owner's own response, video meeting link, Google
link, event type, visibility, transparency, API created/updated times,
`observed_at`, `mapping_revision`. The organizer is the author, as an email
identifier (`system` `email`, `kind` `email`); calendar addresses such as
`...@group.calendar.google.com` are not people and get no author. The creator
is the author of a `created` record.

Change fields: the same identifiers plus `change`, `changed_fields` (which of
title, event_start, event_end, place, description, attendees, responses,
recurrence differ from the previous snapshot; null when unknown), event start
and end, recurrence lines, creator and organizer email, `time_precision`
(`source` for an API timestamp, `observed` for a snapshot diff),
`occurrence_count` (occurrences of a series that vanished together).

Lifecycle rules:

- `created` once per event (a series or a single event) at its API `created`
  time; exceptions (moved or cancelled instances) carry the series' `created`
  and get no record of their own. A series first seen through one of its instances
  is recorded as created with no subject period (its start is unknown there).
- `changed` at the API `updated` time when the tracked fields differ from the
  last snapshot. When an event is first seen after it was already modified
  (more than 60 s after creation), one `changed` record at `updated` says so
  with `changed_fields` null. A difference with no new `updated` time (the API
  did not timestamp it) is recorded at the fetch time with
  `time_precision=observed`.
- `cancelled` when Google reports `status: cancelled` (deleted events and
  cancelled instances arrive as tombstones because every list call sets
  `showDeleted=true`); the occurrence gets a new version with status
  `cancelled` and keeps its last known title.
- `removed`: an occurrence that a later complete window pass no longer returns,
  with no tombstone and no change-pass item showing where it went, gets a new
  version with status `removed`; the event gets a `removed` record (for a
  series, one record per run listing the vanished dates). `restored` records
  an event that comes back. A complete pass that returns nothing at all (not
  even tombstones) while live occurrences are known in its window removes
  nothing: that is far likelier a misread answer than an emptied calendar
  (`skipped_empty_window` in the run report).
- An event moved outside the window is not removed: the change pass carries the
  same resource, so the occurrence is updated to its new time.

Every message links (`method` `source`) to a `calendar_event` entity whose
external ref is `ical:<iCalUID>`: occurrences with `part_of`, lifecycle
records with `about`. Copies of one meeting on several calendars or accounts
share the entity. `CALENDAR_LINKS=false` turns links off.

## Setup

```powershell
uv sync --all-packages
$env:THROUGHLINE_URL = 'http://localhost:8000'
$env:THROUGHLINE_TOKEN = '<Throughline plugin token scoped to calendar>'
# Live, through Composio REST v3:
$env:CALENDAR_SOURCE = 'live'
$env:COMPOSIO_API_KEY = '<Composio API key>'
$env:CALENDAR_ACCOUNTS = '<connected account id>[,<another>]'
uv run --package throughline-calendar throughline-calendar manifest
uv run --package throughline-calendar throughline-calendar backfill --dry-run
uv run --package throughline-calendar throughline-calendar backfill
uv run --package throughline-calendar throughline-calendar sync --once
uv run --package throughline-calendar throughline-calendar sync   # every 30 minutes
```

`backfill` uses `CALENDAR_BACKFILL_FROM`/`CALENDAR_BACKFILL_TO` for the
occurrence window when they are set, else the rolling window; `sync` always
uses the rolling window (`CALENDAR_PAST_DAYS` before today to
`CALENDAR_FUTURE_DAYS` after). A dry run in live mode still calls Composio
(and starts from an empty cursor, so it does not see the stored day counter).

### Transports

- **REST (default, preferred):** `POST {COMPOSIO_BASE_URL}/tools/execute/{tool}`
  with header `x-api-key: COMPOSIO_API_KEY` and body
  `{connected_account_id, arguments, user_id?, version?}`. Pages of up to
  `CALENDAR_PAGE_SIZE` (default 250) items.
- **MCP (`CALENDAR_TRANSPORT=mcp`):** JSON-RPC `tools/call` of
  `COMPOSIO_MULTI_EXECUTE_TOOL` with `{tools:[{tool_slug, account, arguments}],
  sync_response_to_workbench:false}` posted to `COMPOSIO_MCP_URL` with
  `Authorization: Bearer COMPOSIO_MCP_TOKEN`; JSON or `text/event-stream`
  answers; an `initialize` handshake is done when the server asks for a
  session. The MCP proxy turns lists longer than two items into lossy previews,
  so this transport always pages at 2 (many calls: prefer REST for backfills).
  Any page that contains a non-object item (such as a literal `"..."`) is
  rejected rather than trusted.

Tools used (read only): `GOOGLECALENDAR_LIST_CALENDARS`
`{max_results, show_hidden:true, page_token}` and `GOOGLECALENDAR_EVENTS_LIST`.
Each calendar gets two passes per run:

1. occurrences: `{calendarId, timeMin, timeMax, singleEvents:true,
   showDeleted:true, orderBy:"startTime", maxResults, pageToken}` over the
   window; a complete pass writes a `window` marker, which is what allows
   "removed" to be detected (an interrupted pass never removes anything);
2. changes: `{calendarId, singleEvents:false, showDeleted:true, updatedMin,
   maxResults, pageToken}` for masters, single events, exceptions and
   tombstones. `updatedMin` is the newest `updated` time seen by the last
   complete pass minus one minute, or on the first pass
   `CALENDAR_CHANGES_SINCE` (a date, an ISO time, or `all`), else the window
   start. When Google refuses an old `updatedMin` (`updatedMinTooLongAgo`), the
   pass reads everything once and applies the bound locally.

## Environment

Every setting is optional for the plugin; live mode needs an account and the
credentials of its transport. `fetch.py` reads the same names.

| Variable | Default | Meaning |
|---|---|---|
| `CALENDAR_SOURCE` | `live` | `live`, or `export:<JSONL file or directory>` (plugin only) |
| `CALENDAR_ARCHIVE_DIR` | | Keep each live run as an export file here (plugin only) |
| `CALENDAR_LINKS` | `true` | Emit `calendar_event` entity links (plugin only) |
| `CALENDAR_BATCH_SIZE` | `500` | Messages per acknowledged batch and checkpoint (plugin only) |
| `CALENDAR_TRANSPORT` | `rest` | `rest` or `mcp` |
| `CALENDAR_ACCOUNTS` | | Comma-separated connected account ids (REST) or MCP account names; not secret |
| `CALENDAR_CALENDARS` | all | Comma-separated calendar ids to include |
| `CALENDAR_EXCLUDE_CALENDARS` | | Comma-separated calendar ids to skip |
| `CALENDAR_PAST_DAYS` | `30` | Rolling window: days before today (UTC) |
| `CALENDAR_FUTURE_DAYS` | `90` | Rolling window: days after today |
| `CALENDAR_BACKFILL_FROM` | | Backfill window start, `YYYY-MM-DD` |
| `CALENDAR_BACKFILL_TO` | | Backfill window end (exclusive), `YYYY-MM-DD` |
| `CALENDAR_CHANGES` | `true` | Run the change pass |
| `CALENDAR_CHANGES_SINCE` | window start | First change pass bound: date, ISO time or `all` |
| `CALENDAR_PAGE_SIZE` | `250` | Items per call (REST, max 2500); MCP uses 2 |
| `CALENDAR_MAX_CALLS_PER_RUN` | `400` | Composio calls per run, retries included |
| `CALENDAR_MAX_CALLS_PER_DAY` | `2000` | Composio calls per UTC day, retries included |
| `CALENDAR_MIN_INTERVAL_S` | `1.0` | Minimum seconds between calls |
| `CALENDAR_RETRIES` | `4` | Retries on 429, 5xx and transport errors |
| `COMPOSIO_BASE_URL` | `https://backend.composio.dev/api/v3` | REST base URL |
| `COMPOSIO_USER_ID` | | Optional `user_id` sent with REST executions |
| `COMPOSIO_TOOLKIT_VERSION` | | Optional toolkit `version` sent with REST executions |
| `COMPOSIO_API_KEY` | | **Secret.** REST API key |
| `COMPOSIO_MCP_URL` | | **Secret** (the token is embedded in the URL). MCP endpoint |
| `COMPOSIO_MCP_TOKEN` | | **Secret.** MCP bearer token |

Secrets are never accepted on the command line, never printed or logged, never
put into records, exports, state files or error messages (errors name the tool
and HTTP status only, and known secret values are redacted). Redirects are
refused so a key is never sent to another host.

## Limits and politeness

Composio counts tool executions against plan quotas, so every call (including
each retry) is charged to a per-run cap (`CALENDAR_MAX_CALLS_PER_RUN`) and a
per-UTC-day cap (`CALENDAR_MAX_CALLS_PER_DAY`, kept in the plugin cursor or the
fetcher state file). Calls are at least `CALENDAR_MIN_INTERVAL_S` apart; 429,
5xx and transport errors are retried with exponential backoff (2 s doubling,
at most 60 s) honouring `Retry-After` (at most 120 s). When a cap is reached
the run stops cleanly: `run_end` says `complete:false` and why, interrupted
passes write no marker, and change cursors only move after a complete pass.
A calendar that fails (for example a subscribed calendar the account may not
read) is reported in `run_end.errors` and skipped; in live mode, a run in
which no calendar could be read at all fails so the heartbeat records it (its
spent calls are still checkpointed, so the day cap holds across failing
runs). The day cap of `fetch.py` only persists with `--state`.
No paid model calls are made.

## Export workflow (remote host)

Credentials may live on a host where only the standard library is available.
Copy `fetch.py` there and run it with the variables set in its environment
(for example filled at run time from a secret store, never written to a file):

```sh
export CALENDAR_ACCOUNTS='<connected account id>'
export COMPOSIO_API_KEY="$(<command that prints the key>)"
python fetch.py export --out calendar-$(date -u +%Y%m%dT%H%M%SZ).jsonl \
  --state calendar-state.json
# A historical window once:
python fetch.py export --mode backfill --from 2018-01-01 --to 2025-01-01 \
  --changes-since all --max-calls 2000 --out calendar-backfill.jsonl \
  --state calendar-state.json
```

`--state` keeps the change cursors and the day counter between runs; it is
written only after the export file is complete (the file is written to
`<out>.part` and renamed). `--append` adds a run to an existing file. Command
options override the environment: `--mode`, `--from`, `--to`, `--past-days`,
`--future-days`, `--accounts`, `--calendars`, `--exclude-calendars`,
`--transport`, `--changes-since`, `--no-changes`, `--page-size`, `--max-calls`,
`--max-calls-per-day`, `--min-interval`. A JSON summary (run id, record counts,
calls, complete, stopped, errors) goes to stdout; exit status 0 on success
(also when a cap stopped the run), 1 when the run failed, 2 for bad settings.

Ship the files back and map them locally:

```powershell
$env:CALENDAR_SOURCE = 'export:C:\path\to\calendar-exports'   # a file or a directory
uv run --package throughline-calendar throughline-calendar backfill
```

A directory is read recursively (`*.jsonl`, `*.jsonl.gz`, `*.ndjson`). Runs are
grouped by run id and applied oldest first (by `fetched_at`); the cursor
remembers the last applied run, so adding new files and running `sync` maps
only the new runs. A run older than the last applied one is skipped: reset the
plugin cursor to fold a late file in.

### Export record format

One JSON object per line, every line with `kind` and `run`:

| kind | Fields |
|---|---|
| `run` | `fetched_at`, `fetcher_version`, `transport`, `mode`, `accounts`, `window` |
| `calendar` | `account`, `id` (calendar id), `payload` (calendar list entry) |
| `occurrence` | `account`, `calendar_id`, `id` (occurrence id), `payload` (event resource) |
| `window` | `account`, `calendar_id`, `time_min`, `time_max`, `count`: a complete occurrence pass |
| `event` | `account`, `calendar_id`, `id` (event id), `payload` (event resource) |
| `changes` | `account`, `calendar_id`, `updated_min`, `full`, `count`: a complete change pass |
| `run_end` | `complete`, `stopped`, `calls`, `errors` |

## Determinism, replay and state

The mapping depends only on the raw records and the mapping state in the
plugin cursor (occurrence snapshot, event units, calendar names, last applied
run), never on the clock. Within one invocation each record is sent once, in
its last state, so replaying everything after a cursor reset creates no new
versions, and the core's reversed-delivery check holds. Between invocations a
changed occurrence gets a new immutable version. Interim checkpoints (every
`CALENDAR_BATCH_SIZE` messages) store a resume offset, so a crash resumes
where the acknowledged messages end. Every record carries `mapping_revision`;
when the plugin's revision changes, the cursor is reset (keeping only the day
counter) and the inputs are replayed, which versions exactly the records whose
mapping changed (in live mode the next fetch re-reads the window and the
change pass from its first bound).

Snapshot entries leave the cursor once their occurrence ends before the start
of a later window, so the cursor stays bounded by the window; a later backfill
over old dates re-sends those occurrences unchanged (the core keeps them as
they are). Event units are kept for good (about 150 bytes each).

## Verification

```powershell
$env:THROUGHLINE_DEV = '1'
uv run --no-sync pytest -q plugins/calendar
uv run --no-sync ruff check plugins/calendar
```

Tests are offline. Fixtures are invented records written by the tests
(`tests/conftest.py`); a fake Composio server on 127.0.0.1 serves REST and MCP
(JSON and event-stream) answers, failures, redirects and truncated previews.
They cover the SDK contract kit (export mode, three runs, crash and resume,
reversed delivery, replay), occurrence and lifecycle mapping, removal and
moved-event rules, filters, resume offsets, mapping revision replay, paging,
retries, cursors, call caps, the `fetch.py` command line in a subprocess, the
standard-library-only rule for `fetch.py`, and live mode matching its own
archived export.

Not verified against the real service: Composio's exact response nesting for
these two tools (the fetcher accepts `items`/`calendars`/`events` lists under
`data`, `data.response_data` and similar wrappers), whether the REST API needs
`COMPOSIO_TOOLKIT_VERSION` for your account, how far back Google accepts
`updatedMin` (handled by the fallback above), and whether the MCP proxy also
shortens lists nested inside an event (attendees, recurrence) to a preview;
if it does, those fields are incomplete through MCP, another reason to use
REST.
