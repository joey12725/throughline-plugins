# Throughline Hermes plugin

Every message to and from a [Hermes Agent](https://github.com/NousResearch/hermes-agent)
assistant, as a Throughline source: user, assistant and tool turns from every
surface Hermes serves (Discord, Telegram, CLI and TUI, desktop and web apps, the
API server, voice, cron jobs, ...), plus one reference record per session.

Two layers:

- `src/throughline_hermes/fetch.py` is a standalone, standard-library-only
  fetcher (Python 3.12 to 3.14, no third-party imports). Copy that one file to
  the Hermes host and run `python fetch.py export --out FILE`. It reads the
  Hermes session store **read-only** and writes raw JSONL records. It needs no
  credential.
- The plugin (`throughline-hermes`) maps those raw records to Throughline
  messages. It reads either export files (`HERMES_SOURCE=export:<path>`) or calls
  the same fetcher in-process (`HERMES_SOURCE=live`, only where Hermes runs).
  The mapping is identical in both cases.

Written against upstream `NousResearch/hermes-agent` at commit `95a1d228`
(2026-11-18, state.db `SCHEMA_VERSION = 33`; `hermes sessions export` JSONL as
in `hermes_cli/sessions_cmd.py` and `hermes_cli/session_export.py`). The
schema is detected at run time: unknown and extra columns are kept in the raw
payload, missing optional columns read as null, and only `messages.id`,
`session_id`, `role`, `timestamp` and `sessions.id`, `started_at` are required.

## What it captures

| Channel | One message per | Record time (`happened_at`) | Thread |
|---|---|---|---|
| `hermes.messages` | user, assistant, tool or system turn of a conversation | the turn's Hermes timestamp | the session (one thread per session, in order) |
| `hermes.automation` | the same, for automated sessions (source `cron` by default, or a `cron_` session id) | the turn's timestamp | the session |
| `hermes.sessions` (reference) | Hermes session | session start (`ended_at` = session end) | `sessions:<source>` |

No subject time is set: a turn is about the moment it was said.

Message fields: role, platform (session source), session id, message row id,
message uid, the session's model (at fetch time), token count, finish reason,
tool name and tool call id (tool rows), tool names, call ids, call count and
calls (assistant rows; arguments bounded), tool output (the full result text,
bounded), its length and a truncation flag, content length and truncation flag,
`reasoning_present`, visibility (`active`, `compacted`, `rewound`), compressed
summary flag, display kind, the platform's own message id, `automated`,
`mapping_revision`.

Content:

- user and assistant turns: the text (multimodal parts become text plus
  `[image]` / `[audio]` markers); an assistant turn adds one line per tool
  call, `tool <name>: <short args>` (at most 50 listed);
- tool turns: `tool <name> result: <first 500 characters>`; the bounded full
  text is in `tool_output`;
- a turn without any text: `(<role> turn without text)`.

Tool results are linked to the assistant turn that made the call with a
`reply_to` link (method `source`, evidence `{tool_call_id}`).

Session fields: session id, platform, created source, user id and name, chat
id, type and name, `owner_session`, `automated`, model, provider, title, parent
session, end reason, message / tool call / API call counts, input, output,
cache and reasoning tokens, estimated and actual cost, profile, system prompt
sha256, length and Hermes hash, system prompt text (only with
`HERMES_STORE_SYSTEM_PROMPT=1`), `mapping_revision`.

### Authors

- Assistant and tool turns: the agent identity `hermes` / `agent` /
  `<HERMES_AGENT_NAME>` (default `hermes`, display name `Hermes`).
- User turns of the owner's own chat: the owner identifier
  `HERMES_OWNER_IDENTIFIER` (default `hermes:owner:owner`). The owner's own
  chat is a session from an owner surface (`HERMES_OWNER_SOURCES`, default
  `cli,tui,desktop,gui,webui,acp,local`) or a direct chat whose user id is in
  `HERMES_OWNER_USER_IDS` (`discord:<id>` or a bare id). Attach the owner
  identifier to the owner person once in the identity registry.
- Other user turns: the platform user id, `<source>` / `user_id` / `<id>`, with
  the user name from the session origin as display name. Group, channel and
  thread chats are never the owner's own chat (their user turns may come from
  anyone; Hermes stores one user id per session, so the session's user id is
  used).
- System rows: no author.

### Privacy

- Hidden reasoning (`reasoning`, `reasoning_content`, `reasoning_details`,
  `codex_reasoning_items`), `codex_message_items` and `api_content` are dropped
  by the fetcher and never leave the host. Only `reasoning_present` is kept.
  Reasoning that Hermes put into `content` stays, as stored.
- System prompts hold the agent's persona and memory. They are exported once
  per session as sha256 and length (plus Hermes's own hash); the text only with
  `HERMES_STORE_SYSTEM_PROMPT=1`.
- Credential-named keys inside JSON columns (`model_config`, `origin_json`,
  `display_metadata`) have their values replaced by `[removed]`.
- Message and tool text is exported as Hermes stored it. With the CLI reader,
  `HERMES_REDACT=1` passes `--redact` so Hermes scrubs keys and tokens first.
- Binary columns are dropped. Operational session columns that change without
  the conversation changing (`last_read_at`, `runtime_*`, compression and
  handoff bookkeeping, ...) are left out of the payload.

## Setup

```sh
uv sync --all-packages
throughline token create hermes --kind plugin --plugin hermes
export THROUGHLINE_URL=http://127.0.0.1:8765 THROUGHLINE_TOKEN=...
export HERMES_SOURCE=export:/path/to/hermes-exports
export HERMES_OWNER_USER_IDS=discord:<owner discord id>,telegram:<owner telegram id>
uv run throughline-hermes backfill --dry-run
uv run throughline-hermes backfill
uv run throughline-hermes sync --once
```

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `HERMES_SOURCE` | `live` | `live`, or `export:<JSONL file or directory>` (plugin only) |
| `HERMES_ARCHIVE_DIR` | | Keep each live run as an export file here (plugin only) |
| `HERMES_LINKS` | `true` | Emit tool result `reply_to` links (plugin only) |
| `HERMES_BATCH_SIZE` | `500` | Messages per acknowledged batch and checkpoint (plugin only) |
| `HERMES_AGENT_NAME` | `hermes` | Agent identity value `hermes:agent:<name>` (plugin only) |
| `HERMES_AGENT_DISPLAY_NAME` | capitalized name | Agent display name (plugin only) |
| `HERMES_OWNER_SOURCES` | `cli,tui,desktop,gui,webui,acp,local` | Sources whose user turns are the owner's (plugin only) |
| `HERMES_OWNER_USER_IDS` | | The owner's platform ids, `<source>:<id>` or `<id>` (plugin only) |
| `HERMES_OWNER_IDENTIFIER` | `hermes:owner:owner` | Owner author identifier `system:kind:value` (plugin only) |
| `HERMES_OWNER_NAME` | | Display name on owner turns (plugin only) |
| `HERMES_AUTOMATION_SOURCES` | `cron` | Sources whose sessions go to `hermes.automation` (plugin only) |
| `HERMES_PREVIEW_CHARS` | `500` | Characters of a tool result in the content (plugin only) |
| `HERMES_READER` | `auto` | `cli`, `file`, `snapshot`, `readonly`; `auto` = `cli` if `HERMES_CLI` is set or `hermes` is on PATH, else `file` if `HERMES_EXPORT_FILE` is set, else `snapshot` |
| `HERMES_HOME` | `~/.hermes` | Hermes home (holds `state.db`); `/opt/data` inside the Hermes container image |
| `HERMES_DB` | `<HERMES_HOME>/state.db` | Session store path (snapshot reader) |
| `HERMES_CLI` | | Command that runs the Hermes CLI, e.g. `hermes` or `docker exec -i hermes hermes` |
| `HERMES_CLI_TIMEOUT_S` | `900` | Seconds the CLI export may take |
| `HERMES_EXPORT_FILE` | | A JSONL file `hermes sessions export FILE` wrote (file reader) |
| `HERMES_SNAPSHOT_DIR` | system temp | Where the private temporary snapshot / CLI spool goes; needs free space about the size of `state.db` |
| `HERMES_BUSY_TIMEOUT_S` | `30` | Lock wait before the read-only backup |
| `HERMES_MAX_MESSAGES` | `20000` | Messages per run (0 = no cap); the rest follow next run |
| `HERMES_MAX_TEXT` | `100000` | Characters kept of any one string; a record is held to 4x this |
| `HERMES_SINCE` | | Backfill lower bound (message time): date or ISO time |
| `HERMES_UNTIL` | | Backfill upper bound, exclusive |
| `HERMES_STORE_SYSTEM_PROMPT` | `0` | `1` exports the system prompt text |
| `HERMES_EXCLUDE_SOURCES` | | Sources never exported (e.g. `test`) |
| `HERMES_REDACT` | `0` | `1` passes `--redact` to the CLI export |

There are no secrets: the session store is a local file and the CLI is local.
Nothing is ever taken on the command line except paths and bounds.

## Readers and the live file

In order of preference (the app's own export path first):

1. **cli**: runs `<HERMES_CLI> sessions export -` (Hermes's own JSONL export,
   an observational, read-only action) and spools its stdout to a private
   temporary file, read in two passes (ids first, so the per-run cap takes the
   lowest message ids) and deleted afterwards. A bare export holds every
   session and refuses sessions over `sessions.max_export_messages`; the
   refusal (printed on stdout) fails the run with Hermes's message. Raise that
   Hermes setting or use the snapshot reader for such sessions.
2. **file**: the same format from a file the operator wrote with
   `hermes sessions export FILE`.
3. **snapshot**: opens `file:<state.db>?mode=ro` (never read-write), copies it
   with the SQLite online backup API in one step into a private temporary
   directory, reads the copy and deletes it. The live file is never written or
   modified. Under WAL (Hermes's mode) the copy's read transaction never blocks
   Hermes's writers; it only holds back checkpoints for the length of the copy.
   SQLite's own `-wal`/`-shm` coordination files are used as by any reader.
4. **readonly**: opens `file:<state.db>?mode=ro` with `query_only=ON` and reads
   it directly, without making a snapshot copy. This is intended for a remote
   WAL-backed store whose JSONL output is streamed off-host. It avoids needing
   scratch space the size of `state.db`; keep each run bounded with
   `HERMES_MAX_MESSAGES` so the read transaction is short.

## Limits

- `HERMES_MAX_MESSAGES` per run; the watermark (highest message id fetched)
  moves only past what was written, so the next run continues without loss.
- `HERMES_BATCH_SIZE` messages per acknowledged batch and checkpoint.
- `HERMES_SINCE` / `HERMES_UNTIL` bound a backfill by message time (sessions by
  start time). A bounded backfill never moves the sync watermark; a capped
  backfill resumes where it stopped on the next `backfill`.
- Every string is cut to `HERMES_MAX_TEXT` characters (cuts listed in the raw
  record's `truncated`, original content length in `sizes`), a record's payload
  to four times that; content and `tool_output` to 100,000 characters.
- No network calls, no paid calls, no rate limits apply.

## Export workflow (remote host)

Copy `fetch.py` to the Hermes host, then:

```sh
# Hermes's own export (preferred), streamed out of the container:
HERMES_CLI="docker exec -i hermes hermes" HERMES_SNAPSHOT_DIR=/path/with/space \
  python3 fetch.py export --out hermes-$(date +%Y%m%d%H%M).jsonl \
  --state hermes-fetch-state.json --mode backfill --max-messages 0

# or a read-only snapshot of state.db:
python3 fetch.py export --reader snapshot --db /path/to/state.db \
  --snapshot-dir /path/with/space --out hermes-now.jsonl --state hermes-fetch-state.json
```

Later runs use `--mode sync` with the same `--state` file (only new messages
and the sessions active since the last run). Ship the JSONL files back and point
the plugin at the directory: `HERMES_SOURCE=export:/path/to/hermes-exports`.
Each file holds whole runs; runs are mapped oldest first and each only once.

For a large remote WAL database, stream a bounded gzip member directly instead
of writing either the database or export on the remote host:

```sh
python3 fetch.py export --reader readonly --db ~/.hermes/state.db \
  --out - --gzip --mode sync --after-id 12345 --since-ts 1795000000 \
  --max-messages 20000 > hermes-increment.jsonl.gz
```

Advance the local `after_id` and `since_ts` from the JSON report written to
stderr only after the corresponding export has been ingested successfully.

Raw record kinds (one JSON object per line, every line carries `run`):

| kind | Fields |
|---|---|
| `run` | `fetched_at`, `fetcher_version`, `reader`, `mode`, `schema` (version, columns), `upstream` |
| `session` | `id`, `payload` (sessions row), `system_prompt` (`sha256`, `length`, `hermes_hash`, `text` only when enabled), `truncated` |
| `message` | `id`, `session_id`, `payload` (messages row), `reply_to` (`{id, uid}` of the calling assistant row), `reasoning_present`, `sizes`, `truncated` |
| `run_end` | `complete`, `stopped`, `messages`, `sessions`, `last_message_id` |

Every message's session travels in the same run.

## Determinism, replay and state

- External ids: `<session id>:<message_uid>` (Hermes keeps the uid when it
  rewrites a transcript by deleting and re-inserting rows), or
  `<session id>:<row id, 10 digits>` for rows without a uid. Sessions: the
  session id.
- The mapping reads only the raw record and the session row of the same run:
  no clock, run id or fetch time. Payload = the raw record without `run`. The
  same export maps to the same messages; replays are unchanged.
- The cursor holds the mapping revision, a fingerprint of the mapping settings
  (agent, owner, automation, preview), the last mapped run (export mode) or the
  fetch watermarks (live mode). A new mapping revision or a changed mapping
  setting replays every input. Interim checkpoints never move the watermark, so
  a crash refetches and never skips.
- A row Hermes updates in place after it was fetched (a reaction, compaction
  flags) is picked up by the next backfill, not by sync.

## Not included

- No separate `hermes.runs` channel: API and cron runs are sessions; cron
  sessions go to `hermes.automation`.
- Per-message senders in group chats (Hermes stores one user id per session).
- No identity registration (plugin tokens cannot write identity): attach
  `hermes:owner:owner` and the agent identifier to people once by hand.

## Verification

```sh
export THROUGHLINE_DEV=1 THROUGHLINE_PG_BIN=<postgres bin directory>
uv run --no-sync pytest -q plugins/hermes
uv run --no-sync ruff check plugins/hermes
```

Tests use only invented stores and exports built in `tests/conftest.py`; the
contract kit runs the plugin against two shipped runs with prompt and reasoning
canaries that must never reach the store, the wire or the logs.
