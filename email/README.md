# Throughline email plugin

> **Needs a Composio account.** This plugin reads Gmail through a connection you
> make in [Composio](https://composio.dev); it does not talk to Google directly.
> For mail without Composio use the `imap` plugin (any IMAP server, including
> Gmail with an app password) or `msgraph` (Microsoft 365 / Outlook.com).

Email from one or more Gmail accounts connected in [Composio](https://composio.dev)
(Gmail toolkit). One Throughline message per email in the `email.message`
channel, threaded per Gmail thread, with the sender as an email identifier.

The package has two layers:

- `src/throughline_email/fetch.py`: a fetcher that uses only the Python
  standard library (urllib, json; Python 3.12 to 3.14). It reads Composio and
  writes raw records as JSON Lines. Copy this one file to any host that holds
  the credentials and run it there.
- the plugin (`throughline_email:Email`, SDK based): maps raw records to
  Throughline messages, either **live** (it runs the fetcher in-process) or
  from **export** files that `fetch.py` wrote elsewhere.

## What it captures

| Item | Where |
|---|---|
| Gmail message id, thread id, history id | `gmail_message_id`, `gmail_thread_id`, `gmail_history_id`; external id `<mailbox>:<message id>` |
| Thread | Throughline thread `<mailbox>:<thread id>`, titled by the subject without `Re:`/`Fwd:` |
| From | author `{system: "email", kind: "email", value: <address>}` (the core lower-cases `email` identifiers); `from_address`, `from_name` |
| To, Cc, Bcc, Reply-To | `to`, `cc`, `bcc`, `reply_to` as `[{address, name}]`; `participants` lists every address |
| Subject, Date header | `subject` (RFC 2047 decoded), `date_header` |
| Time | `happened_at` is Gmail's internal date (when the mailbox received or sent it), in UTC |
| Labels | `labels` (user label names resolved through Composio), `label_ids`, `is_unread`, `is_starred`, `is_important` |
| Snippet and body | `snippet`; content = Subject/From/To lines, then the body: text/plain when present, otherwise HTML converted to text (`body_format`, `body_chars`, `body_truncated`) |
| Attachments | metadata only: `attachments` `[{filename, mime_type, size, attachment_id, content_id, inline}]`, `attachment_count`. No attachment content is downloaded |
| Direction | `direction`: `received`, `sent` (SENT label or sent by the mailbox) or `draft` |
| Mailing list, size | `list_id` (List-Id header), `size_estimate` (Gmail's size estimate in bytes) |
| Revision | `mapping_revision` (currently 2) |
| Payload | A compact summary, not the raw Composio message: ids, internal date, label ids and names, snippet, size estimate, the headers From, To, Cc, Bcc, Reply-To, Subject, Date, Message-ID, In-Reply-To, References and List-Id as sent, attachment metadata (`filename`, `mime_type`, `size`, `attachment_id`) and `body_format`. When content was truncated, `body_text` holds the full readable body. No MIME parts, base64 data, HTML source or full header list is stored |

Each fetched state is one version: a label change found by incremental sync
produces a new version of the same message; rerunning on the same input
produces no new versions. Emails are life events with no separate subject
time. The channel sets `conversation_whole_thread`, so a Gmail thread is one
conversation.

Spam is skipped unless `EMAIL_INCLUDE_SPAM_TRASH=true`. Deletions reported by
Gmail history are written to exports as `gmail.deleted` records but are not
mapped: the earlier versions stay in Throughline.

## Setup

1. Connect each Gmail account in Composio and note its connected account id
   (REST, for example `ca_EXAMPLE1`) or its account name (MCP).
2. Create a Composio API key (REST, recommended) or have the MCP URL ready.
3. Install the plugin in the Throughline workspace and run it with a plugin token:

```sh
export THROUGHLINE_URL=http://127.0.0.1:8765 THROUGHLINE_TOKEN=<plugin token>
export EMAIL_ACCOUNTS=ca_EXAMPLE1,ca_EXAMPLE2
export COMPOSIO_API_KEY=<from your secret store>
export EMAIL_SINCE=2024-01-01 EMAIL_MAX_MESSAGES=5000
throughline-email backfill --dry-run
throughline-email backfill
throughline-email sync          # polls every 15 minutes
```

## Environment

Credentials (secrets: read from the environment only, never from arguments,
never printed or logged):

| Variable | Meaning |
|---|---|
| `COMPOSIO_API_KEY` | Composio API key, REST transport |
| `COMPOSIO_MCP_URL` | Composio MCP URL, MCP transport. It embeds a token: treat the whole URL as a secret |
| `COMPOSIO_MCP_TOKEN` | Optional bearer token sent to the MCP URL |

Settings (the plugin and `fetch.py` read the same names):

| Variable | Default | Meaning |
|---|---|---|
| `EMAIL_SOURCE` | `live` | Plugin only: `live`, or `export:<file or directory>` |
| `EMAIL_ACCOUNTS` | | Comma-separated connected account ids (REST) or account names (MCP). Always name the accounts: a Composio default account may be another mailbox |
| `EMAIL_TRANSPORT` | `rest` | `rest` or `mcp` |
| `COMPOSIO_BASE_URL` | `https://backend.composio.dev` | REST base URL |
| `COMPOSIO_USER_ID` | | Optional user id sent with REST executions |
| `COMPOSIO_TOOLKIT_VERSION` | | Optional Gmail toolkit version sent with REST executions |
| `EMAIL_SINCE` / `EMAIL_UNTIL` | | Backfill bounds: `YYYY-MM-DD` (UTC day, `UNTIL` inclusive) or an ISO timestamp with offset |
| `EMAIL_QUERY` | | Extra Gmail search filter added to every listing, e.g. `-category:promotions` |
| `EMAIL_MAX_MESSAGES` | `1000` | Backfill cap per account, newest first (`0` = no cap) |
| `EMAIL_PAGE_SIZE` | `25` | Messages per list call (1 to 500); MCP always uses 2 |
| `EMAIL_INCREMENTAL` | `auto` | `auto` (Gmail history, date fallback), `history`, or `date` |
| `EMAIL_OVERLAP_S` | `86400` | Date mode re-reads this far before the newest message seen |
| `EMAIL_MAX_CALLS_PER_RUN` | `500` | Composio executions per run (`0` = unlimited) |
| `EMAIL_MAX_CALLS_PER_DAY` | `5000` | Composio executions per UTC day for one state (`0` = unlimited) |
| `EMAIL_MIN_INTERVAL_S` | `1.0` | Minimum seconds between requests |
| `EMAIL_RETRY_BASE_S` | `2.0` | First retry delay; doubles per attempt, capped at 60 s |
| `EMAIL_MAX_RETRIES` | `5` | Retries on 429, 5xx and transport errors |
| `EMAIL_TIMEOUT_S` | `60` | HTTP timeout per request |
| `EMAIL_INCLUDE_SPAM_TRASH` | `false` | Include spam and trash in listings |
| `EMAIL_BODY_LIMIT` | `50000` | Plugin only: body characters kept in content (`0` = all); when truncated, the payload keeps the full body text as `body_text` |
| `EMAIL_EXPORT_BATCH` | `500` | Plugin only: messages per checkpoint when reading exports |

## Sync, limits and transports

- **Backfill** (first run per account): `GMAIL_GET_PROFILE` records the
  mailbox address and the current history id, then `GMAIL_FETCH_EMAILS` pages
  newest first through `after:`/`before:` (from `EMAIL_SINCE`/`EMAIL_UNTIL`)
  and `EMAIL_QUERY`, up to `EMAIL_MAX_MESSAGES`. Every page is a checkpoint.
- **Incremental**: `GMAIL_LIST_HISTORY` from the stored history id; every
  message added, relabelled or deleted since is collected, and each changed
  message is fetched with `GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID`. When Gmail no
  longer has that history id (it keeps about a week), `auto` falls back to a
  date query from the newest message seen minus `EMAIL_OVERLAP_S` and takes a
  fresh history id; `history` fails instead; `date` always uses the date query.
  `EMAIL_UNTIL` bounds the backfill only.
- **Limits**: every Composio execution counts against `EMAIL_MAX_CALLS_PER_RUN`
  and `EMAIL_MAX_CALLS_PER_DAY` (Composio counts executions against plan
  quotas). A run that reaches a limit stops at its last checkpoint and the
  next run resumes there, with no loss. Requests are paced by
  `EMAIL_MIN_INTERVAL_S`; 429, 5xx and transport errors are retried with
  exponential backoff that honours `Retry-After` (seconds or an HTTP date);
  other HTTP errors fail at once. A "not found" answer means a message or
  history id is gone (the message is skipped, the history falls back to a date
  query) only when it does not concern the connection itself: errors about the
  account, OAuth, the key or the tool always fail the run, and the changed
  messages stay queued for the next run. Redirects are refused, so the key header is never sent to another host.
  The live plugin keeps its call counter in its cursor and `fetch.py` in its
  `--state` file: they are separate budgets.
- **REST** (preferred): `POST {COMPOSIO_BASE_URL}/api/v3/tools/execute/{TOOL}`
  with `x-api-key` and `connected_account_id`; full messages (Gmail payload
  with headers and MIME parts) arrive in each list page.
- **MCP** (alternative): JSON-RPC `tools/call` of `COMPOSIO_MULTI_EXECUTE_TOOL`
  on the MCP URL (initialize handshake, JSON or event-stream answers). The MCP
  proxy turns lists longer than two items into lossy previews, so this
  transport pages at two messages, requests the flattened message shape, and
  marks records whose nested lists (labels, recipients) were cut:
  `partial = true`. Use REST for backfills.
- No model calls are made; nothing paid besides Composio executions.

## Export workflow

When the credentials live on another host where only the standard library is
available, run the fetcher there and map the files locally:

```sh
# On the remote host (credentials from its own secret store, in env only):
export COMPOSIO_API_KEY="$(your-secret-reader COMPOSIO_API_KEY)"
python3 fetch.py export --account ca_EXAMPLE1 --account ca_EXAMPLE2 \
    --since 2024-01-01 --max-messages 5000 \
    --state email-state.json --out exports/email-$(date +%Y%m%d-%H%M).jsonl
# Later runs with the same --state are incremental; write a new --out file each
# time (an existing file is refused unless --append).

# Back on the Throughline host, after copying the exports directory:
export EMAIL_SOURCE=export:/path/to/exports
throughline-email backfill
```

`fetch.py export` prints a JSON summary (`counts` by record kind, `calls`,
`stopped` when a limit ended the run). Options mirror the settings table:
`--account`, `--transport`, `--since`, `--until`, `--query`,
`--max-messages`, `--page-size`, `--incremental`, `--max-calls-per-run`,
`--max-calls-per-day`, `--min-interval`, `--include-spam-trash`, `--state`,
`--append`.

The export source reads one `.jsonl`/`.jsonl.gz` file or every such file
under a directory, in path order. For each message the record with the latest
`fetched_at` wins over all files, so files can be added over time: each run
maps only new or changed files and ends in exactly the state a fresh import of
all files gives. Malformed lines stop the run with the file name and line
number; a record that cannot be mapped is skipped with a warning.

Record format, one JSON object per line: `kind` (`gmail.message`,
`gmail.profile`, `gmail.deleted`, `export.meta`), `account` (mailbox
address), `account_ref` (Composio account), `id` (native id), `thread_id`,
`fetched_at`, `transport`, `labels` (user label id to name), optional
`truncated`, and `raw` (the payload exactly as Composio returned it).

## Tests

```sh
uv run --no-sync pytest -q plugins/email
uv run --no-sync ruff check plugins/email
```

All tests are offline: invented mailboxes behind a fake Composio (REST and
MCP) on localhost, the fetcher CLI as a subprocess, and the SDK contract kit
over both the export and the live source (it needs the local test Postgres).
