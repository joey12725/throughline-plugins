# Throughline Microsoft Graph plugin

Mail and calendar from Microsoft 365 (work or school) and Outlook.com /
Hotmail / Live (personal) accounts through
[Microsoft Graph](https://learn.microsoft.com/graph/) delta queries. Record
shapes match the other mail and calendar connectors: `msgraph.message` uses
the fields of `plugins/imap` and `plugins/email` (Gmail);
`msgraph.events` / `msgraph.changes` use the fields of `plugins/caldav` and
`plugins/calendar` (Google).

Sign-in uses your **own** Entra (Azure AD) app registration as a public
client with the OAuth device-code flow: no client secret, no MSAL, only
`httpx`. Access is read-only (`Mail.Read`, `Calendars.Read`).

## What it captures

| Channel | One message per | Record time |
|---|---|---|
| `msgraph.message` | email (one record per Message-ID across the folders read) | `receivedDateTime` |
| `msgraph.events` | event occurrence in the calendar window (Graph's calendarView expands recurring series) | the occurrence start (`ended_at` = its end) |
| `msgraph.changes` | lifecycle step: `created`, `changed`, `cancelled`, `removed`, `restored` | `createdDateTime` / `lastModifiedDateTime`, or the fetch time for removals |

Mail: external id `<account>:<internetMessageId>` (fallback hash without one),
thread `<account>:conv:<conversationId>`, author
`{system: "email", kind: "email", value: <from address>}`, `folders`
(display names), `labels` (folders plus categories), `is_unread` (`isRead`),
`is_starred` (flagged), `is_important` (importance high), `direction`
(`draft`, `sent` for Sent Items or mail from your addresses, `received`),
`author_kind`, body as text (HTML converted), `snippet` (`bodyPreview`),
attachment metadata (one extra call per message with attachments; content
is never downloaded), plus `graph_message_id`, `graph_conversation_id`,
`importance`. `date_header` holds `sentDateTime` (Graph does not expose the
raw Date header).

Calendar: the same fields as `plugins/caldav` (see its README). Graph specifics:
`event_type` is `singleInstance`, `occurrence` or `exception`; `visibility` is
`sensitivity`; `transparency` is `transparent` when `showAs` is free;
`conference_url` is the Teams join URL; `my_response` comes from
`responseStatus`; attendee `optional` / `resource` from the attendee type.
Times are requested in UTC (`Prefer: outlook.timezone="UTC"`) and
`start_time_zone` keeps `originalStartTimeZone`.

Lifecycle: a series gets one `created` record (from its first occurrence);
single events get `created`, `changed` (tracked fields differ, at
`lastModifiedDateTime`), `cancelled` (`isCancelled`) and `restored`;
exceptions (moved or cancelled instances) get `changed` / `cancelled` under
the series' thread. Deletions arrive in the delta as `@removed`: the
occurrence gets status `removed` and the event (or the series, listing the
dates) a `removed` record at the fetch time.

Every calendar record links to a `calendar_event` entity: `ical:<iCalUId>`
for single events (the same entity the CalDAV and Google connectors create
for the same invitation), `cal:<account>:<calendar>:<seriesMasterId>` for
recurring series.

## Setup

1. **Register an app** (once): [Entra admin center](https://entra.microsoft.com)
   (or portal.azure.com) > App registrations > New registration.
   - Supported account types: "Accounts in any organizational directory and
     personal Microsoft accounts" (or only personal / only your tenant).
   - No redirect URI is needed. Under **Authentication**, set **Allow public
     client flows** to **Yes** (required for the device-code flow).
   - Under **API permissions**, add Microsoft Graph delegated permissions
     `User.Read`, `Mail.Read`, `Calendars.Read`, `offline_access`. A work
     tenant may need an admin to consent.
   - Copy the **Application (client) ID**.
2. **Sign in** (once, on the machine that runs the plugin):

```sh
export MSGRAPH_CLIENT_ID=<application id> MSGRAPH_TENANT=consumers   # or common / organizations / <tenant id>
throughline-msgraph-login
# Prints: To sign in, use a web browser to open https://microsoft.com/devicelogin and enter the code ...
```

   The token cache (access token, refresh token, client id, tenant, scopes)
   is written to `MSGRAPH_TOKEN_CACHE` (default
   `~/.config/throughline/msgraph-token.json`) with mode 0600, atomically. The
   plugin refreshes the access token itself and saves rotated refresh tokens.
   If the refresh token is revoked or expires (about 90 days unused), runs fail
   with "run throughline-msgraph-login again". Refreshes always ask for the
   scopes originally requested, `offline_access` included. Tokens read from
   the cache are added to log redaction and refused in records like declared
   secrets.

3. **Run**:

```sh
export THROUGHLINE_URL=http://127.0.0.1:8765 THROUGHLINE_TOKEN=<plugin token>
throughline-msgraph backfill --dry-run
throughline-msgraph backfill
throughline-msgraph sync --once     # under a timer, or: throughline-msgraph sync (every 15 minutes)
```

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `MSGRAPH_CLIENT_ID` | | Application (client) id; used by the login helper and stored in the cache |
| `MSGRAPH_TENANT` | `common` | `common`, `consumers` (Outlook.com), `organizations`, or a tenant id |
| `MSGRAPH_TOKEN_CACHE` | `~/.config/throughline/msgraph-token.json` | Token cache file (contains a refresh token: keep it private) |
| `MSGRAPH_SCOPES` | `offline_access User.Read Mail.Read Calendars.Read` | Login helper only |
| `MSGRAPH_ACCESS_TOKEN` | | **Secret.** A static bearer token instead of the cache (no refresh; expires in about an hour) |
| `MSGRAPH_ACCOUNT` | `/me` mail | Account label used in ids |
| `MSGRAPH_OWNER_ADDRESSES` | | Extra addresses that are you (aliases); your `mail` and `userPrincipalName` are always included |
| `MSGRAPH_MAIL` | `true` | Read mail |
| `MSGRAPH_MAIL_FOLDERS` | `inbox,sentitems` | Well-known folder names (`inbox`, `sentitems`, `archive`, `drafts`, ...) or folder ids |
| `MSGRAPH_SINCE` | | First mail round only: `$filter=receivedDateTime ge <date>` |
| `MSGRAPH_CALENDAR` | `true` | Read calendars |
| `MSGRAPH_CALENDARS` | `primary` | Comma-separated calendar ids (`primary` = the default calendar) |
| `MSGRAPH_PAST_DAYS` / `MSGRAPH_FUTURE_DAYS` | `30` / `90` | Rolling calendar window around today (UTC) |
| `MSGRAPH_WINDOW_START` / `MSGRAPH_WINDOW_END` | | Fixed window `YYYY-MM-DD` (end exclusive) |
| `MSGRAPH_PAGE_SIZE` | `50` | `Prefer: odata.maxpagesize` |
| `MSGRAPH_MAX_PAGES` | `200` | Pages per folder or calendar per run; a larger first sync continues next run from the stored nextLink |
| `MSGRAPH_BATCH_SIZE` | `200` | Messages per acknowledged batch and checkpoint |
| `MSGRAPH_BODY_LIMIT` | `50000` | Body characters kept in content (0 = all) |
| `MSGRAPH_LINKS` | `true` | Emit `calendar_event` entity links |
| `MSGRAPH_TIMEOUT_S` | `60` | HTTP timeout |
| `MSGRAPH_FIXTURE` | | Tests only: recorded HTTP answers through `httpx.MockTransport` |

## How sync works

- **Mail:** `GET /me/mailFolders/{id}/messages/delta?$select=...` per folder;
  the run follows `@odata.nextLink` pages to the `@odata.deltaLink`, which is
  stored in the cursor; the next run calls that link and gets only new and
  changed messages (read/flag/category changes become new versions).
  `@removed` entries (deletions, moves out) are not mapped. A delta link
  Graph no longer accepts (410 / `syncStateNotFound`) restarts that folder.
- **Calendar:** `GET /me/calendarView/delta?startDateTime&endDateTime`
  (or `/me/calendars/{id}/calendarView/delta`). A delta link is bound to its
  window, so when the rolling window moves (daily) a fresh round reads the
  new window completely; occurrences known in the window but missing from a
  complete round are marked removed. Within a window, `@removed` entries mark
  deletions.
- **Paging links** are only followed on `graph.microsoft.com`, so the bearer
  token is never sent elsewhere. 429/5xx are retried honouring `Retry-After`;
  a 401 refreshes the token once.
- **Crash safety:** all pages of a run are read first, then messages are sent
  in batches with interim checkpoints that keep the run's start cursor plus a
  resume offset; delta links move only in the final checkpoint.
- **Failures:** `/me` unreachable, or every folder and calendar failing,
  fails the run; one failing folder or calendar is reported as partial
  coverage (`source_error`).

## Limitations

- Mail threads follow Graph's `conversationId`; `In-Reply-To`/`References`
  are not requested (Graph only returns them through `internetMessageHeaders`,
  which delta queries do not select).
- A message found in two folders (mail to yourself) is one record listing
  both. When an incremental round delivers one copy, the other folders'
  copies are looked up with `GET /me/messages?$filter=internetMessageId eq
  '...'` (one call per such message) so the version keeps every folder;
  messages without an internetMessageId list only the folders seen in that run.
- Within one delta round the last state of an item wins (an event updated
  and then removed is removed, and the reverse).
- All-day events are rounded to the nearest UTC midnight, since UTC
  rendering of an all-day event can move its day boundary.
- A crash resumes at the acknowledged offset only when the source answers exactly as before; if anything changed in between, the run starts over from its start cursor (re-sending is harmless: unchanged records stay unchanged).
- Graph's `iCalUId` differs per occurrence of a series, so recurring Outlook
  events do not share the `ical:` entity with the same series on other
  providers; single events do.
- Calendar history is the window only; use `MSGRAPH_WINDOW_START`/`END` for
  a one-off older range.
- Built from the Graph v1.0 documentation and not run against a live tenant:
  in particular `$select` / `$filter=receivedDateTime ge` on message delta,
  calendarView delta on non-default calendars, and the shape of `@removed`
  entries are as documented, not verified.

## Tests

```sh
THROUGHLINE_DEV=1 uv run pytest -q plugins/msgraph
uv run ruff check plugins/msgraph
```

Offline, through `httpx.MockTransport` (`tests/recorded/graph.json`, invented
example.com data): mail mapping and cross-folder dedupe, calendar occurrences,
exceptions and lifecycle, delta links on the next run, incremental changes and
`@removed`, window changes, expired delta links, partial and total failure,
paging-host refusal, token refresh and rotation with 0600 files, 401 retry,
the device-code login CLI (pending, slow_down, success), the token-cache
path, and the SDK contract kit.
