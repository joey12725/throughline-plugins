# Throughline Granola plugin

Brings your [Granola](https://www.granola.ai) meetings into Throughline:
what was said (the transcript), the meeting itself (title, attendees,
calendar event, times), the notes you typed, and the notes Granola's AI wrote.
Granola's AI notes are kept apart from what people said or typed.

## Two ways to read Granola

| `GRANOLA_SOURCE` | Reads | Status |
|---|---|---|
| `api` | Granola's official public API (`https://public-api.granola.ai`, [docs](https://docs.granola.ai/introduction)) | **Recommended.** Documented and versioned (OpenAPI 1.7.0 as of November 2026). |
| `cache` | The desktop app's local plaintext cache, `~/Library/Application Support/Granola/cache-v*.json` | **Legacy, undocumented.** Works only with older app versions. |
| `auto` (default) | `api` when `GRANOLA_API_KEY` (or `GRANOLA_FIXTURE`) is set, otherwise `cache` | |

Both readers are live: run the plugin on a timer and each run picks up new
and changed meetings. Neither one asks you to request an export.

### Why the API is recommended

When this plugin was written (November 2026), public reports said Granola had
replaced `cache-v3.json` with `cache-v6` around March 2026. Around April to
May 2026 it began encrypting the cache on disk (`cache-v6.json.enc`). Since
mid-2026 the key is kept in the macOS keychain, where only the Granola app
can read it. The `cache` mode cannot decrypt that file, and it never tries
to. If it finds only `cache-*.json.enc`, it stops with an error that tells
you to use `api`. Nobody has checked this against Granola's source code. It
comes from community write-ups and tools that stopped working at that point.

The API needs a **Business or Enterprise** plan. The free plan cannot create
API keys.

## Channels

Every record is threaded by meeting (thread external id = the Granola
document or note id; thread title = the note title). Every channel has
`meeting_id` and `source_mode` (`cache` or `api`).

### `granola.transcript` (primary)

One record per transcript segment, the unit Granola itself stores. Each
segment carries its own start and end time and its own audio source. A
merged record per meeting would lose both and would be one huge text. The
channel is `conversation_whole_thread`, so one meeting is one recording that
is never split into conversations. This is the same transcript model the
core uses for other recorders (speaker labels, `start_s`/`end_s`, media-noise
flags, the speaker inbox).

| Field | Type | Meaning |
|---|---|---|
| `speaker_attribution` | text | `me` (the note-taker), `them` or `unknown` |
| `speaker_source` | text | `microphone` (your mic), `system` (cache) or `speaker` (API): the other side's audio |
| `author_kind` | text | `owner` when you said it, else `other` |
| `speaker_name` | text | A name Granola identified, or the inferred attendee's name |
| `speaker_label` | text | `Others`, `Speaker A`, ... when the speaker could not be tied to an attendee |
| `speaker_inferred` | boolean | The author was inferred (see below) |
| `start_s`, `end_s` | number | Seconds from the meeting's first segment |
| `segment_id` | text | The cache's segment id, or the position in the API transcript |

How authors are set:

- **Your microphone** (`microphone`, or API attribution `me`): the
  note-taker's e-mail (`{system: "email", kind: "email"}`).
- **The other side's audio** (`system`/`speaker`, or attribution `them`):
  - When Granola gives a speaker name that matches exactly one attendee, the
    author is that attendee's e-mail.
  - In a one-to-one meeting (exactly one other attendee), the author is that
    attendee's e-mail, with `speaker_inferred: true`.
  - Otherwise the author is a per-meeting `speaker_label` identifier
    (`<meeting id>:Others`). The core never attaches a speaker label to a
    person automatically. Use the speaker inbox to assign it.
- **Phone and watch recordings** (API) capture a single microphone stream.
  Their lines get `speaker_attribution: unknown`, and their diarization
  labels (`Speaker A`, `Speaker B`) become per-meeting speaker labels.

### `granola.meetings` (reference)

One record per meeting note. `happened_at` is the earlier of the scheduled
start and the first transcript segment, falling back to the note's creation
time. `ended_at` is the later of the scheduled end and the last segment. The
author is the calendar organizer when known, otherwise the note-taker.

Fields: `meeting_title`, `calendar_event_id`, `calendar_title`,
`scheduled_start`, `scheduled_end`, `organizer_email`, `note_owner_email`,
`is_own_note`, `attendee_emails` (json), `attendees` (json: email, name,
organizer, self), `attendee_count`, `folders` (json), `transcript_status`
(`ok`, `missing`, `disabled`), `utterance_count`, `web_url` (API only),
`note_created_at`, `note_updated_at`.

### `granola.my_notes`

The notes you typed yourself, owner-authored, one record per meeting. The
record updates in place as you edit them, because Throughline versions it.
Cache: `notes_markdown`, else `notes_plain`, else the editor document. API:
`private_notes_markdown` or `private_notes_text`. The API returns these only
when the key belongs to the note's owner. Fields: `meeting_title`,
`author_kind`, `note_format`, `note_updated_at`.

### `granola.summaries` (record_kind `derived`)

Notes written by Granola's AI: one record per AI panel in the cache (Summary,
Action items, ...), or the note's summary from the API. The author is
`{system: "granola", kind: "agent", value: "granola-ai"}`, with
`author_kind: agent`. Each summary has a `provenance(...)` citation to every
transcript segment and to your typed notes for the same meeting (split into
lists of 1,000). Fields: `meeting_title`, `author_kind`, `panel_id`,
`panel_title`, `template`, `source_count`.

## Setup

### API mode (recommended)

1. In the Granola desktop app, open **Settings → API** and create a key
   (`grn_...`).
   - A **personal** key sees your own notes, including your typed notes.
   - A **workspace** key sees the workspace's shared notes. Set
     `GRANOLA_OWNER_EMAIL` with a workspace key, so that other people's notes
     are not marked as yours.
2. Run:

```sh
export GRANOLA_API_KEY=grn_...
throughline-granola backfill        # everything the key can see
throughline-granola sync --once     # then on a timer, e.g. every 15 minutes
```

Each sync asks for notes updated since the newest one already delivered,
with a one-second overlap; the overlap is deduplicated. Each changed note is
fetched in full, with its transcript paged in when it is too large to return
in one response. Requests are spaced `GRANOLA_REQUEST_PAUSE_S` apart (the
API allows five per second). A 429 or 5xx response is retried, honouring
`Retry-After`. A 401 response, or a network that stays unreachable, fails
the run, so the source shows as failing.

### Cache mode (legacy app versions only)

Run the plugin on the Mac that runs Granola:

```sh
export GRANOLA_SOURCE=cache            # optional; this is the default without a key
throughline-granola backfill
throughline-granola sync --once        # on a timer
```

The newest `cache-v*.json` in `~/Library/Application Support/Granola` is
used. Set `GRANOLA_CACHE_PATH` to pick a file or folder. The file is opened
read-only and never written. The app rewrites it whenever something changes,
so each run reads the current state. A run that finds the file half-written
fails, and the next run retries. Each meeting's mapped content is
fingerprinted in the cursor, and only new or changed meetings are delivered
again.

The shape this mode assumes comes from community tools and has not been
checked against Granola's source. Every key is optional:

```
{"cache": "<JSON string>"}                    (an object is accepted too)
  inner: {"state": {...}, "version": 3}
  state.documents        {doc_id: {id, title, created_at, updated_at, deleted_at,
                          notes_markdown, notes_plain, notes (ProseMirror),
                          people {creator {name,email}, attendees [...]},
                          google_calendar_event {id, summary, start {dateTime},
                          end {dateTime}, attendees [{email, displayName, self}],
                          organizer {email}}}}
  state.transcripts      {doc_id: [{id, text, source: microphone|system,
                          start_timestamp, end_timestamp, is_final}]}
  state.documentPanels   {doc_id: {panel_id: {id, title, original_content (HTML),
                          content (ProseMirror), created_at, template_slug}}}
  state.meetingsMetadata {doc_id: {attendees: [...]}}
  state.documentLists / documentListsMetadata   folders
```

- Unknown shapes, for example a new cache version without
  `state.documents`, fail with an error that names the version and the
  top-level keys, so you can see that the format has drifted.
- Interim segments (`is_final: false`), empty segments, deleted documents and
  panels, and link-only panels are skipped.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `GRANOLA_SOURCE` | `auto` | `auto`, `api` or `cache` |
| `GRANOLA_API_KEY` (secret) | | Granola API key |
| `GRANOLA_CACHE_PATH` | `~/Library/Application Support/Granola` | Cache file or folder |
| `GRANOLA_OWNER_EMAIL` | | Your e-mail; a note owned by anyone else is marked `is_own_note: false` and its microphone lines `author_kind: other` |
| `GRANOLA_OWNER_NAME` | | Your display name |
| `GRANOLA_ACCESS_LABEL` | | Access label for every record, e.g. `work`. Seeded labels gate records for non-owner askers and block egress to remote models. |
| `GRANOLA_BATCH_SIZE` | `20` | Meetings per checkpoint |
| `GRANOLA_REQUEST_PAUSE_S` | `0.25` | Seconds between API requests |
| `GRANOLA_API_URL` | `https://public-api.granola.ai` | API base URL |
| `GRANOLA_FIXTURE` | | Tests only: a JSON file of recorded API responses used in place of the network |

Production runs use a scoped plugin token:
`throughline token create granola --kind plugin --plugin granola --json`.

## Limitations

- **The two modes use different ids.** The cache uses document UUIDs, the
  API uses `not_...` ids, so switching modes imports the same meetings a
  second time. Pick one.
- **The API lists only notes that already have an AI summary and a
  transcript.** A meeting appears after Granola has finished processing it.
  A note with no summary at all gets no `granola.summaries` record.
- **A workspace admin can turn off transcript access over the API.** The
  meeting is then delivered with `transcript_status: disabled`, and the run
  reports `granola.transcript` as partial (`transcript_access_disabled`).
- **Deletions are not propagated.** Deleted notes are skipped from then on,
  but records already delivered stay.
- **In the cache, transcripts may exist only for meetings the app has
  loaded.** A meeting can first arrive without a transcript
  (`transcript_status: missing`). It is delivered again once the transcript
  appears.
- **Speaker identity is coarse.** Granola knows "me" and "them". Other
  speakers become e-mail authors only for a matched name or in one-to-one
  meetings. Everyone else is a per-meeting speaker label.
- **Phone and watch recordings without timings** give every line the same
  time, the time the transcript was written. The ids stay stable because
  they come from each line's position.
- **Attachments are not downloaded.** Their signed URLs are removed from
  stored payloads, and only the ids and kinds are kept.

## Dependencies

`throughline-sdk` and `httpx`, which the SDK already uses. Everything else is
standard library.

## Tests

```sh
THROUGHLINE_DEV=1 uv run pytest -q plugins/granola
```

The tests run only on invented data: `tests/recorded/cache-v3.json` (a
synthetic cache) and `tests/recorded/api.json` (recorded API responses),
both generated by `tests/granola_fixtures.py`. The contract kit runs against
both.
