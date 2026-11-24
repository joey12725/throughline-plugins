# Wave transcripts

Independent, text-only Throughline plugin. Reads the official Wave REST API
(the default), or imports transcripts you already hold elsewhere from a
Postgres database laid out as [`archive-schema.sql`](archive-schema.sql)
(`WAVE_SOURCE=archive`). Neither source fetches or stores audio,
media URLs, voice samples, or voiceprints. No model calls are made.

## Sources and research

Official documentation checked on 2026-11-17:

- https://api.wave.co/llms.txt — bearer authentication, scopes, session pagination,
  detail and timed transcript response shapes, rate limits, events and webhooks.
- https://help.wave.co/integrations/wave-developer-api
- https://help.wave.co/managing-sharing/sharing-and-exporting-your-wave-sessions

Wave currently documents an API, signed webhooks and a pull event feed. This
plugin implements **polling the API**, using GET /v1/sessions (limit 100 and
next_cursor), GET /v1/sessions/{id}, and GET /v1/sessions/{id}/transcript.
It does not implement a webhook receiver or consume/acknowledge Wave events.
The API lists finished sessions with summaries. Shared sessions are not imported.

Wave documents PDF and Word exports, bulk ZIPs of PDFs, and clipboard/share
transcript text/Markdown. It explicitly says there is no plain-text or SRT file
export. Those files are not imported or watched: the official timed API is used.
No account was accessed. Token issuance, actual account responses, pagination
under concurrent edits, availability across account plans, and voice identity
enrichment could not be verified with credentials. Fixtures are invented from
the official documented response structure, not captured owner traffic.

## Channels

Both channels are ordinary ground truth, neither reference nor outside content.

| Channel | Shape and fields |
| --- | --- |
| transcripts.wave | One message per turn, external ID recording-id:turn-no, recording thread/title. Fields: speaker_label (text), start_s/end_s (number), speaker_provenance (text), ignore_reason (text), place (text). |
| recordings.wave | One message per recording, external ID recording ID, same recording thread/title. Content is title plus metadata summary. Fields: duration_s (number), device/source_platform/recording_type (text), turn_count (integer), ignore_reason (text). |

Turn time is said_at_manual when non-null, otherwise said_at. End time adds
end_s - start_s to that chosen instant when both offsets exist. Recording time
is started_at through started_at + duration_s. Turn coordinate pairs take priority,
then recording coordinate pairs. Place falls back to the recording place.
Ignore flags do not suppress import. Arrival rules should filter ignore_reason
and may also inspect the linked recording's flag.

Archive speakers mapping to wave/voice_profile use that identifier unchanged,
enabling later registry attachment without reimport. Others use
wave/speaker_label with value recording-id:label and display_name label.
Null/empty labels use the deterministic label "Unknown speaker".
The public timed API example supplies labels, not a verified profile-ID mapping;
API authors therefore use per-recording speaker labels. Names are never guessed
to be voice profile IDs. A later archive mapping is detected by reconciliation.

Every turn has a part_of link to its recording message, method source.
The transcript channel registers `conversation_whole_thread=true`, so each recording thread stays one conversation regardless of elapsed gaps. Embeddings, arrival rules and voice enrollment remain core responsibilities.

Raw payloads retain source records: archive recording rows, or turn/speaker/
recording objects; API detail objects, or session/transcript/segment objects.
Postgres timestamps and numerics are serialized to JSON ISO strings/numbers;
JSONB remains structured, including ignored-record metadata. PostgreSQL
timestamptz has already discarded original input offsets, so archive timestamps
are consistently emitted in UTC. API offsets are preserved.
Empty turn text becomes "[Empty transcript turn]" with its original raw text
retained. A missing real recording/turn timestamp, naive timestamp or invalid
negative interval fails import instead of inventing a time.
The archive anchor table is not used to recalculate already supplied real times.

## Install and settings

From the repository root:

```powershell
uv sync --all-packages
```

| Environment | Meaning |
| --- | --- |
| THROUGHLINE_URL | Throughline HTTP base URL |
| THROUGHLINE_TOKEN | Throughline token scoped to plugin wave (or owner) |
| WAVE_SOURCE | api (default) or archive |
| WAVE_ARCHIVE_DSN | Secret libpq DSN for a read-only archive connection |
| WAVE_API_TOKEN | Secret Wave bearer token for API mode |
| WAVE_API_FIXTURE | Optional offline invented response-map JSON for tests; unset for real API use |
| WAVE_API_OVERLAP_HOURS | How far behind the newest imported session each API sync lists again (default 168) |

Archive credentials must have CONNECT, USAGE on transcripts, and SELECT on
transcripts.recording, transcripts.turn and transcripts.speaker. Source queries
use repeatable-read, read-only transactions; no writes or schema changes.
Tables must match [`archive-schema.sql`](archive-schema.sql) (create it in any
Postgres database and load your transcripts into it; views with the same
columns work too). The DSN is configured explicitly; it is never discovered
automatically.

For API credentials open https://app.wave.co/settings/integrations/api
(Settings → Developers → API tokens and webhooks), create a token with only
sessions:read and transcripts:read, and save it securely. No media scope is needed.
Requests are paced below 60/minute; transport failures, 429 and 5xx receive
bounded retries. The documented 10,000/day cap still applies: each explicit backfill costs
approximately two calls per recording plus list pages. Large libraries should
run less frequently rather than repeatedly exhausting that quota.

Archive backfill (replace placeholders locally):

```powershell
$env:THROUGHLINE_URL = 'http://localhost:8000'
$env:THROUGHLINE_TOKEN = '<Throughline wave plugin token>'
$env:WAVE_SOURCE = 'archive'
$env:WAVE_ARCHIVE_DSN = '<read-only archive DSN>'
uv run --package throughline-wave throughline-wave backfill --dry-run
uv run --package throughline-wave throughline-wave backfill
uv run --package throughline-wave throughline-wave sync --once
```

API backfill:

```powershell
$env:WAVE_SOURCE = 'api'
$env:WAVE_API_TOKEN = '<Wave scoped API token>'
Remove-Item Env:WAVE_API_FIXTURE -ErrorAction SilentlyContinue
uv run --package throughline-wave throughline-wave backfill --dry-run
uv run --package throughline-wave throughline-wave backfill
uv run --package throughline-wave throughline-wave sync --once
```

The API commands also require THROUGHLINE_URL and THROUGHLINE_TOKEN as above.
Use sync without --once for hourly polling. Both commands use the same generator.
Dry-run starts at the beginning and writes neither messages nor cursor/links.

## Coverage

Each run reports coverage for both channels, scope key `archive` or `api`:

| Source | Status and reasons |
|---|---|
| Archive | A full reconciliation of the recording set: `complete` (verified); quarantined rows make it `partial` with `quarantined_rows` and their count |
| API | Wave lists a session only once it is processed, and each sync lists again over the overlap window (`WAVE_API_OVERLAP_HOURS`), so a sync is `best_effort` with `listing_lag` (no gap opens) |
| API, daily budget reached | `partial` with `rate_limited`, the requests spent and the limit in detail; no window, so no gap: the next sync resumes the cursor |

A failed run is reported by the SDK as `partial` with `run_failed`.

## Curation, speakers and late enrolment

The importer sends what the source holds and never curates. The core does:

- Turns and recordings with a source `ignore_reason` are excluded from recall
  by default (`include_ignored` brings them back). Owner curation is an overlay
  annotation keyed on the external id (`POST /v1/annotations`, attribute
  `ignore`), so it survives re-imports and new versions; an owner
  `{ignored: false}` keeps a source-ignored turn in recall across re-imports.
- `speaker_label` authors stay unclaimed: the core never attaches a person to
  a label. They are listed in the speaker inbox (`GET
  /v1/transcripts/speakers`, `throughline lib transcripts speakers list`),
  where the owner assigns or ignores them.
- Late enrolment (IDN-8): when the archive later maps a label to a
  `wave/voice_profile`, the next reconciliation re-sends the recording's turns
  with that author. They become new versions of the same records and resolve
  through the identifier to its person; nothing is re-imported by hand.

## Resume, reconciliation and limits

Archive keyset order is recording ID using C collation, then integer turn_no
(200-turn reads). Checkpoints follow each acknowledged turn and link; a resumed
recording replays its parent safely. Completed scans reset to the start so later
polls discover edits, late turns, older newly inserted recordings and changed
speaker mappings. Sync is full reconciliation, not an updated_at-only feed:
turns and speaker mappings have no reliable modification watermark in this schema.

API uses the returned opaque pagination cursor, checkpointing after each complete
session. A crash can replay the current session. Checkpoints retain the bounded
page and index so newly inserted recordings cannot shift a resumed page.
Changes older than the overlap behind the watermark require an explicit backfill.
A session removed mid-import fails safely for review rather than skipping it. Session
segment ordinals (1-based) are the external turn numbers because the documented
API provides no segment ID. If Wave resegments text, existing ordinal IDs get
new versions; removed segments and deleted recordings are not deleted from
Throughline's append-only history.

The two sources share channels but may have different recording IDs and turn
numbering. No archive/API crosswalk is invented. Prefer one source per library;
switch sources only after a completed scan (empty cursor), or explicitly reset
the plugin cursor through Throughline's API after reviewing overlap. Running
multiple wave processes against one Throughline instance is unsupported.

No private database or account was accessed during development. Tests create
only randomly named tl_test_ databases on the brief's local test service.
Run only this plugin's suite:

```powershell
uv run pytest -q plugins/wave
uv run ruff check plugins/wave
```

Contract tests cover each source, replay/idempotency, dry-run, fresh sync and
three simulated crash boundaries. Focused tests exercise timing, manual
overrides, identity mappings, ignore flags, raw payloads, locations and resume.



### Incremental API polling and archive exceptions

API sync retains the maximum real session timestamp as its watermark and sends
`since` on the next list request, using the documented filter at
https://api.wave.co/llms.txt. Wave lists a session only once it has finished
processing, which can be after a newer session has already moved the watermark,
so `since` is the watermark minus `WAVE_API_OVERLAP_HOURS` (default 168, one
week), not the watermark itself. The cursor keeps the list-row signature and
timestamp of each imported session inside that window (`seen`); an unchanged
row skips its detail/transcript requests, so the overlap costs list requests
only. A session quarantined as a whole (for example a transcript without
segments yet) is not remembered as imported. The cursor records it in `failed`
with its list-row signature, an attempt count and the next retry time: it is
fetched again after 1 hour, then 2, 4, 8, 16 and from then on every 24 hours
while it is inside the window, and at once when its list row changes. A
waiting session costs no detail/transcript requests and reserves no budget, so
sessions that never become importable cannot use up the daily request budget.
An explicit backfill ignores the wait. Mapping errors of every kind are quarantined the same way in
the archive and API paths.
Only explicit backfill performs a full API reconciliation; use it to discover
edits or late processing older than the overlap. This assumes `since` filters
on the session timestamp, which the API description does not state. API checkpoints retain
the fetched page and offset through interruptions. The source does not document
an update watermark, so ordinary sync cannot guarantee older metadata edits.
Requests are paced at 1.05 seconds, and a persisted UTC-day budget stops new work
at 8,900 reserved requests (five attempts reserved and checkpointed before each
network call, so even a crash cannot erase request consumption), leaving headroom below the token's 10,000
request daily quota. This budget assumes this is the only process using the token.
Resume on a later day with the same cursor; do not reset it to bypass the budget.

Archive NULL started_at uses the earliest real said_at_manual/said_at from its
turns, marked `time_precision=first_turn`, preserving the original payload.
With no usable turn time the recording is quarantined; its ID and the final
count are logged, and dry-run returns `quarantined`. Bad turn times are likewise
quarantined without pinning the turn cursor. Reversed end/start offsets retain
the source fields and omit ended_at rather than emitting a negative interval.
