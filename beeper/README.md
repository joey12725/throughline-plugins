# Throughline Beeper plugin

Imports connected-network chat history through the **Beeper Desktop API**. A
second source, `archive`, imports chat history you already hold elsewhere from
a Postgres database laid out as [`archive-schema.sql`](archive-schema.sql)
(see [Importing from a compatible Postgres schema](#importing-from-a-compatible-postgres-schema)).
Both sources use `chat.<network>` channels,
one thread per source conversation, the same typed fields, and raw source records.
By default there are no model calls or media downloads. Reactions are always
normalized into content and a typed field. Three optional enrichments, all off
by default, convert document attachments to text, turn Instagram links into
transcripts, frames and images, and turn YouTube links into transcripts with
one frame per minute; see [Enrichment](#enrichment). The original
attachment metadata stays in `attachments` and the untouched payload; `images`
stays empty until the core's Immich store exists.

## Settings and credentials

| Environment variable | Meaning |
|---|---|
| `THROUGHLINE_URL` | Running Throughline HTTP origin |
| `THROUGHLINE_TOKEN` | Throughline owner token or plugin token scoped to `beeper` |
| `BEEPER_SOURCE` | `live` (default) or `archive` |
| `BEEPER_BASE_URL` | Desktop API origin, default `http://localhost:23373` |
| `BEEPER_ACCESS_TOKEN` | Secret; required for live imports |
| `BEEPER_ARCHIVE_DSN` | Secret; required for archive imports; use a read-only role |
| `BEEPER_BATCH_SIZE` | Archive fetch/checkpoint batch size, default 2000, range 1–5000 |
| `BEEPER_RECORDING` | Optional invented JSON recording for offline tests; replaces source access |
| `BEEPER_ATTACHMENTS` / `BEEPER_INSTAGRAM_LINKS` / `BEEPER_YOUTUBE_LINKS` | `1` enables an enrichment (same as `--attachments` / `--instagram-links` / `--youtube-links`); see [Enrichment](#enrichment) for the rest |
| `THROUGHLINE_TRANSCRIBE_URL` / `_MODEL` / `_API_KEY` | OpenAI-compatible transcription endpoint shared with other plugins; see [Transcription](#transcription) |
| `BEEPER_LIVE_HISTORY_CAPS` | `network=count` pairs, default `instagram=1000`: Desktop history caps; a chat whose loaded history reaches its cap is reported `partial` with `source_cap`, see [Coverage](#coverage) |
| `BEEPER_NO_HISTORY_NETWORKS` | Networks whose Desktop chats have no history listing, default `snapchat`; reported `best_effort` |

Enable the Desktop API in Beeper Desktop's Settings → Integrations, then use
the `+` next to Approved connections to create a token. Keep Beeper Desktop
running. The API is local by default: a container must be configured to reach
the Desktop API origin. This plugin uses Bearer authentication, does not create
accounts, send messages, or modify chats. Dependencies, including `psycopg`,
belong to this plugin package.

### Importing from a compatible Postgres schema

`BEEPER_SOURCE=archive` reads the tables defined in
[`archive-schema.sql`](archive-schema.sql): `archive.conversations`,
`archive.messages` and, optionally, `archive.identities` and
`archive.message_aliases`. Create that schema in any Postgres database, load
your existing chat export into it (views with the same columns work too), and
give the plugin a read-only role through `BEEPER_ARCHIVE_DSN`. The comments in
the schema file describe each column; `tests/recorded/archive.json` is an
invented example of the rows, and `tests/test_beeper_archive_schema.py` checks
the schema against the source.

Install from the workspace with `uv sync --all-packages`. In PowerShell:

```powershell
$env:THROUGHLINE_URL = 'http://localhost:8000'
$env:THROUGHLINE_TOKEN = '<Throughline token>'
$env:BEEPER_SOURCE = 'archive'
$env:BEEPER_ARCHIVE_DSN = '<Postgres DSN for the archive database>'
uv run --package throughline-beeper throughline-beeper backfill --dry-run
uv run --package throughline-beeper throughline-beeper backfill
uv run --package throughline-beeper throughline-beeper sync --once

$env:BEEPER_SOURCE = 'live'
$env:BEEPER_BASE_URL = 'http://localhost:23373'
$env:BEEPER_ACCESS_TOKEN = '<approved Desktop API token>'
uv run --package throughline-beeper throughline-beeper backfill --dry-run
uv run --package throughline-beeper throughline-beeper backfill
uv run --package throughline-beeper throughline-beeper sync
```

`sync` polls every 300 seconds; `--once` performs one pass. `backfill` also
resumes after interruption. Dry-run always starts at the beginning, validates
normalized messages, reports channel counts/date bounds and zero model cost,
and leaves messages, links, cursors and heartbeat unchanged. Registration is
permitted. Live/archive progress share the SDK's `default` cursor but occupy
separate keys, so switching sources does not discard the other's progress.

## Channels and normalization

The baseline manifest declares `chat.discord`, `chat.instagram`, `chat.sms`,
`chat.reddit` and `chat.snapchat`. At import time discovered networks are added
and registered before their messages. Network names are lowercased and
punctuation becomes underscores; Google Messages maps to `sms`. Other network
names are preserved in that form. Archive conversation IDs and Beeper chat IDs
are source-specific, because the schema supplies no guaranteed conversation
crosswalk. Thread metadata includes `kind`; archive titles and kinds are kept.
Desktop API `single` maps to `dm`, `group` to `group_dm`: the documented API
does not distinguish server channels from group chats or expose a guild ID.

Core integration limitation: the plugin emits `thread.metadata={title, kind}`,
but the current core's `Thread` model (`src/throughline/models.py`, class
`Thread`) silently discards metadata, and `src/throughline/ingest.py` inserts
only thread title/ID. There is no general thread-metadata write endpoint to use
as a plugin workaround. Title persists; `conversation_kind` remains available
as a typed message field. Persisting the emitted thread metadata needs a core
fix outside this plugin's permitted directory.

All channels declare:

| Field | Type | Meaning |
|---|---|---|
| `direction` | text | incoming, outgoing, unknown |
| `time_precision` | text | exact, minute, displayed_block, timezone_unknown, inferred, unknown |
| `time_inferred_from` | json | Absent (the key is not sent) when the source supplied the time; otherwise `{method, message?, sent_at?}` |
| `reply_to` | text | Replied-to external ID; null for unresolved archive references |
| `edited_at` | timestamp | Source edit time |
| `attachments` | json | Original attachment metadata, unchanged |
| `reactions` | json | Original source reaction metadata, unchanged |
| `reaction_details` | json | Normalized reactions; key absent when there are none |
| `attachment_files` | json | Converted document attachments; key absent until enrichment handled one |
| `instagram_links` | json | Fetched Instagram links; key absent until enrichment handled one |
| `youtube_links` | json | Transcribed YouTube links; key absent until enrichment handled one |
| `identity_quality` | text | Archive provenance or `beeper_id` |
| `conversation_kind` | text | dm, group_dm, channel, unknown |
| `guild_id` | text | Archive guild ID, null when unavailable |

Authors use the network as `system`. Archive `(platform, sender_id)` matches
`archive.identities.(platform, native_id)` to obtain `kind=namespace`. Ambiguous
or missing matches default to phone for SMS, username for Snapchat, otherwise
user_id. Empty sender IDs produce no invented author. Live uses the documented
fully qualified Beeper sender ID with kind `beeper_user_id`, since stripping its
network/homeserver would incorrectly assert a native identity. `sender_name` /
`senderName` supplies the display name.

Empty bodies produce readable attachment labels, or `[empty chat message]` if
there is no attachment metadata. Missing archive `sent_at` first tries `time_raw`, then the neighbouring dated
row of the same conversation (`time_precision=inferred`), and only then falls back to
`first_imported_at` with `time_precision=unknown`; `time_inferred_from` records which was used. All missing is quarantined while the cursor advances past that row. Original time offsets are retained. JSON stored as
TEXT is decoded for typed fields while the original TEXT remains in the payload.

## Coverage

Every run reports coverage per chat: channel `chat.<network>`, scope key the
chat (archive conversation ID or Desktop chat ID), also when nothing changed.
Before the run the SDK notes an attempt; a failed run is reported `partial`
with `run_failed`.

| Case | Status and reasons |
|---|---|
| Archive chat read fully | `complete`, window from its first to its last message, `rows` in detail |
| Archive rows quarantined | `partial`, `quarantined_rows` with their count (no window, so no gap opens for rows that cannot be mapped) |
| Archive chat with a recorded `history_status` | that word is sent as the status (the core keeps unexpected words raw and degrades them to `partial`; never a hard stop) |
| Desktop chat whose history reached its cap (`BEEPER_LIVE_HISTORY_CAPS`, Instagram's 1,000 messages) | `partial`, `source_cap`, the cap and loaded row count in detail |
| Desktop history load failed (for example SMS history loading) | `partial`, `source_error` and `retry_pending`, `pending_retries` and an explicit gap over the window that did not load |
| Network without history listing (`BEEPER_NO_HISTORY_NETWORKS`) | `best_effort` (no gap opens) |
| Desktop history ended without an error before it was complete | `partial`, `partial_history` |

An unknown conversation kind is reported in the detail (`conversation_kind:
unknown`, `unknown_kind_rows`), never as a failure.

**Closing gaps.** The plugin keeps no gap state of its own. At the start of a
run it reads its open gaps back from the core (`GET
/v1/plugins/beeper/coverage/gaps`); when the window of a chat loads in that run,
its report names those gap ids in `closes`. A failed reload leaves them open
and counts them in `pending_retries`; a history that stopped paging before its
end (`partial_history`) leaves them open as well.

## Message aliases and identity

When the core advertises `message_aliases` in `GET /v1/describe`, each archive
message whose `archive.message_aliases` rows give an unambiguous same-network
native or live id carries `aliases`: those ids in the plugin's external-id
format (a live id as the Desktop source would send it, a native id as
`<network>:<native id>`). Ambiguous ids (one alias naming two messages) and
cross-network rows are never sent. Live messages carry
`fields.identity_quality` (`beeper_id`) and archive messages their archive
provenance (for example `fingerprint`), so the core's rank rule applies: a
fingerprint-identified record is upgraded when its native id appears, and a
live delivery of an archived message resolves to the archive record instead
of creating a duplicate. Aliases are outside the payload hash, so alternating
archive and live runs create no new versions. On an older core no aliases are
sent and nothing else changes.

Identity reaches the core only through ingest identifiers (message authors).
The archive's people crosswalk (`people`, `person_identity_links`) is never
read or imported: Throughline has one identity registry.

## Reactions

Every source reaction becomes one line at the end of the content, for
example `reactions: ❤️ Invented Alex, 👍 Sam, 👍 ×3`, and a `reaction_details`
field, `[{emoji, sender_id?, sender_kind?, sender_name?, at?, count?}]` in source
order. Both are visible to search and SQL, e.g.
`fields->'reaction_details'->0->>'sender_id'`. The raw `reactions` field and the
payload are unchanged. Shapes handled: Discord exports (`emoji {name, id}`,
`count`, `users[]`, one entry per user plus an anonymous `count` remainder;
custom server emoji render as `:name:`), Meta exports (`reaction`, `actor`,
`timestamp` in seconds or milliseconds), bare strings (Instagram), the Desktop
API (`participantID`, `reactionKey`) and generic `sender_id`/`sender_name`
objects. Reactors are resolved to sender identifiers where the source allows:
in the archive, a reactor known only by name gets the `sender_id` of the one
sender with that name in the same conversation (and an ID gets its name), with
the namespace from `archive.identities`; live reactors are Beeper user IDs
(`beeper_user_id`), named from the chat's participants. Ambiguous names stay
names. Messages without reactions have no `reaction_details` key and unchanged
content, so they keep their stored version; messages with reactions get one new
version, once. The line is capped at 50 entries and 4,000 bytes.

## Enrichment

All enrichments are optional, bounded and resumable. They are off unless
enabled per run with flags or environment:

```powershell
uv run --package throughline-beeper throughline-beeper backfill --attachments --instagram-links
uv run --package throughline-beeper throughline-beeper backfill --instagram-links --cookies-from-browser firefox --instagram-limit 10
uv run --package throughline-beeper throughline-beeper backfill --youtube-links --youtube-limit 5
```

A scheduled `sync` can switch them on without flags:
`BEEPER_ENRICH=attachments,instagram,youtube` (any subset). The regular sync
loop then enriches new inbound messages as they arrive, under the limits below.

### New messages only vs backlog

By default an enrichment applies only to messages sent at or after the moment
it was first enabled. That moment is stored per kind (attachments, Instagram,
YouTube) in the state directory on the first run with the switch on; older
messages are left as they are and counted as `backlog_skipped` in the run
result. To choose the moment yourself, pass `--enrich-since 2026-01-31` (or
`BEEPER_ENRICH_SINCE`, an ISO date or time; a date alone is midnight UTC); it replaces the stored moment of
every enabled kind and is kept for later runs.

The backlog is processed only with `--backlog` (`BEEPER_BACKLOG=1`), and still
within the daily and per-run limits, so it can run over many days or not at
all. Work is always done newest first, so recent messages are enriched before
old ones.

```powershell
# New inbound only (the default): nothing older than the first enabled run.
uv run --package throughline-beeper throughline-beeper sync --instagram-links
# Work through older messages too, 20 reels a day.
uv run --package throughline-beeper throughline-beeper sync --instagram-links --backlog --instagram-per-day 20
```

### Daily limits

Daily limits are a rolling 24-hour count kept in `enrichment.sqlite3` (the
transcription minutes in the shared `transcribe.sqlite3`), so they
hold across separate runs and processes. A fetch counts when it is attempted,
whether it succeeds or not; outcomes already cached cost nothing. `0` switches
that kind off, `unlimited` lifts the limit. Per-run limits still apply.

| Limit | Variable / flag | Default | Counts |
|---|---|---|---|
| Instagram | `BEEPER_INSTAGRAM_PER_DAY` / `--instagram-per-day` | 20 | Instagram reels and posts fetched |
| YouTube | `BEEPER_YOUTUBE_PER_DAY` / `--youtube-per-day` | 20 | YouTube videos fetched for links |
| Attachments | `BEEPER_ATTACHMENTS_PER_DAY` / `--attachments-per-day` | 500 | Attachments downloaded |
| Transcription | `THROUGHLINE_TRANSCRIBE_MINUTES_PER_DAY` / `--transcribe-minutes-per-day` | 120 | Audio minutes sent to the OpenAI-compatible endpoint (Instagram and YouTube together, and shared with the YouTube connector: one pool per machine, kept in `THROUGHLINE_TRANSCRIBE_STATE_DIR`); `0` switches the endpoint off |
| Per run | `--attachment-limit`, `--instagram-limit`, `--youtube-limit`, `THROUGHLINE_TRANSCRIBE_MAX_MINUTES` | 200, 25, 10, 600 | As before, within one run |

When a limit is reached, the remaining items are deferred: not marked failed,
nothing in the message changes (no field, no version), and a later run picks
them up, newest first. The run result reports `deferred` counts per kind and
`deferred_until`, the time each daily window frees up again, for example
`{"deferred": {"instagram": 4}, "deferred_until": {"instagram": "2026-02-01T09:12:44+00:00"}}`.
A reel whose transcript waits for minutes is not shown until it is
transcribed; one longer than the whole daily allowance is shown with
"transcript unavailable (longer than the transcription minutes allowed per
run or day)" and transcribed from its stored audio once the allowance is
raised. A reel already shown (for example with "transcript unavailable"
from before an endpoint was configured) stays exactly as shown while its new
transcript waits for minutes. Live sync keeps deferred messages in a queue in the same file and
finishes them on later syncs, newest first.

| Variable / flag | Default | Meaning |
|---|---|---|
| `BEEPER_ATTACHMENTS` / `--attachments` | `0` | Download and convert document attachments |
| `BEEPER_INSTAGRAM_LINKS` / `--instagram-links` | `0` | Fetch Instagram links found in message bodies |
| `BEEPER_ATTACHMENT_LIMIT` / `--attachment-limit` | 200 | New downloads/conversions per run |
| `BEEPER_INSTAGRAM_LIMIT` / `--instagram-limit` | 25 | New Instagram links (or re-transcriptions) per run |
| `BEEPER_ATTACHMENT_MAX_BYTES` | 25 MiB | Size cap per attachment, checked against the declared size before download and while streaming |
| `BEEPER_INSTAGRAM_MAX_BYTES` / `_MAX_SECONDS` / `_MAX_FRAMES` | 200 MiB / 1800 / 200 | Caps per Instagram video |
| `BEEPER_STATE_DIR` | `%LOCALAPPDATA%\throughline\beeper` (Windows), `$XDG_STATE_HOME/throughline/beeper` or `~/.local/state/throughline/beeper` | Holds `enrichment.sqlite3` (the cache) and, by default, `assets/` |
| `BEEPER_ASSET_DIR` | `<state>/assets` | Content-addressed asset store |
| `BEEPER_ASSET_BASE_URL` | `throughline-asset://beeper` | Base of rendered asset links |
| `BEEPER_YTDLP_COOKIES` / `--cookies` | none | Netscape `cookies.txt` passed to yt-dlp |
| `BEEPER_YTDLP_COOKIES_FROM_BROWSER` / `--cookies-from-browser` | none | Browser name passed to yt-dlp |
| `BEEPER_YOUTUBE_LINKS` / `--youtube-links` | `0` | Transcribe YouTube links found in message bodies, with one frame per minute |
| `BEEPER_YOUTUBE_LIMIT` / `--youtube-limit` | 10 | New YouTube videos processed per run |
| `BEEPER_YOUTUBE_TRANSCRIPT_SOURCE` / `--transcript-source` | `auto` | `auto` (the transcriber when one is configured, else YouTube captions), `endpoint` (the transcriber) or `captions` |
| `BEEPER_YOUTUBE_MAX_SECONDS` | 7200 | Longest YouTube video processed; longer ones are recorded as skipped until this is raised |
| `BEEPER_YOUTUBE_FRAME_EVERY` | 60 | Seconds between kept YouTube frames (Shorts too) |
| `BEEPER_YOUTUBE_MAX_BYTES` | 2 GiB | Cap per temporary YouTube audio or video download |
| `BEEPER_YOUTUBE_CAPTION_LANGS` | `en` | Comma-separated preferred caption languages; the video's own language also counts |
| `BEEPER_TRANSCRIBER` / `--transcriber` | `auto` | `auto`, `faster-whisper`, `openai` (OpenAI-compatible), `none` |
| `BEEPER_WHISPER_MODEL` | `small` | faster-whisper model |
| `THROUGHLINE_TRANSCRIBE_URL`, `THROUGHLINE_TRANSCRIBE_MODEL` | none, `whisper-1` | OpenAI-compatible API base (`…/v1`) and model; `BEEPER_TRANSCRIBE_URL` / `_MODEL` still work as aliases |
| `THROUGHLINE_TRANSCRIBE_API_KEY` | none | Secret: Bearer key for that endpoint; `BEEPER_TRANSCRIBE_API_KEY` (also a declared secret) still works as an alias |
| `THROUGHLINE_TRANSCRIBE_MAX_BYTES` / `_CHUNK_SECONDS` / `_TIMEOUT` / `_RETRIES` / `_MAX_MINUTES` | 24 MiB / 1200 / 600 / 4 / 600 | Endpoint upload limit, chunk length, request timeout, retries, audio minutes per run; see [Transcription](#transcription) |

When both a `THROUGHLINE_TRANSCRIBE_*` variable and its `BEEPER_` alias are
set, the `THROUGHLINE_` one wins.

**Cache and determinism.** Every outcome (converted, failed, unavailable, too
large, no source, private, ...) is stored in the SQLite cache, keyed by a hash
of the attachment ID (else its URL without the query string, since Discord CDN
signatures change; else message, index and file name) or of the canonical
Instagram URL, or the YouTube video ID. Content and fields are a pure function of the message and that
cache: a rerun does no repeated work and creates no new versions. A run with
enrichment switched off, and every `--dry-run`, still renders what is cached
(the cache is then opened read-only and never created), so a later plain
`sync` does not revert enriched messages. Only work this run did changes a
message, which then gets one new version (its content hash changes). Transient
failures (network errors, HTTP 429/5xx, yt-dlp login walls or rate limits) are
retried on later runs, at most three attempts. Until the last attempt they
are not rendered at all (neither content nor field), so retries add no
versions; the final outcome is rendered once. A run's or a day's limit leaves the remaining attachments and links
untouched (no field, no version) for a later run. Archive runs reconcile the
whole table after a completed pass and spend each run's allowance on the
newest due items, so repeated `backfill`/`sync` runs walk through the work one
limit at a time. Live sync reads only new messages, plus the queue of messages
a limit held back; enrich older live history with `--backlog` after resetting
the plugin cursor and running `backfill`.
Converter changes bump `convert.REVISION`, which redoes converted entries only
when enrichment is enabled. Message checkpoints are unchanged by enrichment.

**Assets.** Core has no asset store yet. Files go to a plugin-managed store
named `<sha256>.<ext>` (originals, page images, figures, frames and audio), and
links render as `<BEEPER_ASSET_BASE_URL>/<sha256>.<ext>`, so a later core asset
endpoint can serve the same names. The store is a small interface
(`assets.AssetStore`: `put`, `path`, `link`); `LocalAssetStore` is the one
implementation. Both, the cache, transcription, ffmpeg and the yt-dlp runner
come from the shared [`throughline-media`](../media/README.md) package; Beeper
keeps the `beeper` asset namespace, so links written before the move are
unchanged.

### Document attachments

Converted kinds, detected by file extension, then declared MIME type, then
confirmed by content sniffing (`%PDF-`, the Office/OpenDocument zip layout, text
versus binary; an HTML error page saved as `.pdf` is treated as HTML): TXT and
Markdown (`txt`, `text`, `log`, `md`, `markdown`), CSV (`csv`, `tsv`), PDF,
Word (`docx`), Excel (`xlsx`, `xlsm`), HTML (`html`, `htm`, `xhtml`), and as
extras ODT and PPTX (text only). Photos, audio and video attachments are not
documents and are left alone. Each converted file adds to the content:

```text
attachment report.pdf: Markdown-converted content
### Page 1
...text...
### Page 2
attachment report.pdf page 2 image: throughline-asset://beeper/<sha256>.jpg
attachment data.csv: CSV-converted content
a,b
attachment budget.xlsx sheet Invented budget: CSV-converted content
item,cost
```

- Text is decoded as UTF-8 (BOMs and UTF-16 honoured, else Windows-1252);
  control characters (NUL above all) are dropped, emoji joiners are kept.
- CSV text is kept as is; TSV is rewritten as CSV. Every Excel sheet is a
  separate CSV section named after the sheet (cached values; at most 500,000
  cells per workbook, with a marker when cut).
- HTML drops scripts, styles and embedded `data:` images, then becomes Markdown
  (headings, lists, tables).
- PDF: text per page (`### Page N`). A page is also stored as a JPEG (long side
  1,600 px) when it has enough visual content: raster images over 25% of the
  page, or many vector drawing objects (charts, diagrams: at least 40 plus one
  per 10 characters of extractable text, so table rules, underlines and cell
  shading on a text-heavy page do not count as a figure), or little extractable
  text (under 80 characters, a scan) with any image or drawing. Text pages with
  a small logo or a ruled table stay text only; blank pages store nothing. At most 500 pages of
  text and 40 page images per file; encrypted PDFs record `encrypted`.
- Word: without LibreOffice there is no page renderer, so pages are never
  rendered. The document becomes Markdown (mammoth, then markdownify, so
  tables survive) and its **embedded figures** are stored instead, each as a
  line `attachment plan.docx image 1: <link>` where it appears; images smaller
  than 64 px are skipped, at most 40 per file. If `soffice` (or `libreoffice`)
  is on PATH, the document is converted to PDF and the PDF page logic applies
  (`renderer: soffice` instead of `embedded_images`).
- The original file is stored too (`file` in the field).

Where files come from: a Discord archive `url`, then its `proxy_url` (HTTP
401/403/404/410 records `unavailable`: old Discord CDN links are signed and
usually expired, and the plugin cannot refresh them without a Discord bot
token); a Desktop API `srcURL` that is `file://` (live only) or `mxc://` /
`localmxc://`, which Beeper Desktop downloads to its own device through
`POST /v1/assets/download`. Archive Instagram, SMS, Reddit and Snapchat
attachments carry no URL at all: they are recorded once as `no_source` (free,
outside the run limit), and no crosswalk to Desktop messages exists (see
cross-source deduplication below). Local paths are never read for archive
rows. The Desktop asset endpoint and `srcURL` shapes follow the published API
reference but were not verified against a real Beeper installation.

`attachment_files` holds, per document attachment:
`{name, kind, status, file?, sha256?, bytes?, pages?, renderer?, assets?: [{role:
page_image|embedded_image, page|index, link, sha256}], error?, attempts?,
truncated?, text?}`. Statuses: `converted`, `failed`, `encrypted`,
`unsupported` (content is not a document, including an HTML login or error
page served under a document name), `too_large`, `unavailable`, `no_source`,
`error` (transient; shown only after the third attempt). If the asset store
cannot write (disk full, permissions) the run stops instead of recording a
failure, and the next run redoes that work.

### Content size

Message content stays within the plugin's 100,000-byte full-text budget (core
itself sets no limit). The text of all converted entries in one message shares
what the body and reaction line leave, fairly (small entries whole, large ones
split the rest); asset link lines are always kept. A cut entry ends with
`[truncated: N of M bytes of text shown; full text in field attachment_files]`
and its field entry gets `truncated: true` and the full rendered `text` (capped
at 1,000,000 characters, with `text_truncated`). If not even the link lines fit,
content keeps only the body and the fields keep everything. Ingest requests are
also flushed early once enriched content reaches 16 MiB.

### Instagram links

Links `instagram.com/reel/`, `/reels/`, `/p/`, `/tv/` (also `m.`, a username
prefix, and `instagr.am`; with or without `https://`, but not inside a longer
host or path; `/reels/audio/...` and similar pages are not posts) in message
bodies on any network are canonicalized to
`https://www.instagram.com/<kind>/<code>/`, deduplicated, and fetched with
yt-dlp (`--dump-single-json`, then a download capped at 720p and the size cap;
carousels item by item). Videos get one frame every 3 seconds (ffmpeg
`fps=1/3`, frames at 0:00, 0:03, ... before the end, at most 200, scaled to 960
px wide), a mono 16 kHz Opus audio file (AAC fallback), and a transcript;
image posts store each image (the largest thumbnail yt-dlp reports, streamed
under the size cap and kept only when it decodes as JPEG, PNG, WebP or GIF; a
login page in its place counts as a transient failure). Content:

```text
attachment instagram reel <code>: video transcript
<transcript, or "transcript unavailable", "(no audio)", "(no speech)">
attachment instagram reel <code> audio: <link>
attachment instagram reel <code> frame 0:03 image: <link>
attachment instagram post <code> image 1: <link>
attachment instagram post <code> item 2: video transcript
attachment instagram reel <code>: unavailable (private)
```

Private posts record `private`, removed ones `dead` (both final, shown in
content as unavailable). Login walls and rate limits are transient: after three
attempts they become `unavailable` ("private, removed or login required").
Five transient failures in a row pause Instagram for the rest of the run. A
failure never drops or delays the message. Cookies (`--cookies` or
`--cookies-from-browser`) help with login walls; use your own account only.

Transcription is described under [Transcription](#transcription). For
Instagram the audio is kept, so when a transcriber becomes available later an
enabled run transcribes from the stored audio without downloading again.

`instagram_links` holds `{url, kind, shortcode, status, items?: [{type, index,
link?, audio?, frames?: [{at_s, link}], duration_s?, transcript_status?,
transcriber?, skipped?}], error?, attempts?, truncated?, text?}`. The transcript
text is in content (and in `text` when cut), not repeated in the field.

### YouTube links

Links `youtube.com/watch?v=`, `youtu.be/`, `/shorts/` and `/live/` (also `m.`
and `music.`; with or without `https://`, but not inside a longer host or
path; the video ID must be the 11-character YouTube form) in message bodies on
any network are deduplicated by video ID and processed by the shared YouTube
processor: one yt-dlp metadata call, then a transcript and one frame per minute
(`BEEPER_YOUTUBE_FRAME_EVERY`; Shorts use the same interval). With
`BEEPER_YOUTUBE_TRANSCRIPT_SOURCE=auto` the transcript comes from the
configured transcriber (OpenAI-compatible endpoint or faster-whisper) when
there is one, otherwise from YouTube's captions via yt-dlp (manual captions in
a preferred language first, then automatic captions of the original speech).
Only the audio stream is downloaded for a transcriber, and only a video stream
of at most 480p for frames; both go to a temporary directory that is deleted
after the step. Videos are never stored, only frame JPEGs. Content, within the
existing content budget (the transcript text is shared fairly with other
enrichments and cut with the usual marker; frame links always stay):

```text
youtube <video id>: transcript
[00:00] <text of the first five minutes>
[05:00] <text of the next five minutes>
youtube <video id> frame 01:00: <link>
youtube <video id>: unavailable (private)
youtube <video id>: not processed (longer than the duration cap)
```

`youtube_links` holds `{url, kind (watch, short, live), video_id, status,
title?, duration_s?, transcript_status?, transcript_source?, caption_kind?,
language?, transcriber?, model?, timestamps?, transcript, frames?: [{at_s,
link}], error?, truncated?, text?}`; `transcript` is always the full
transcript text (up to 1,000,000 characters). Private, removed, members-only,
age-restricted and Premium-only videos are final (`unavailable (...)`);
private, members-only and age-restricted ones are retried once when cookies
are configured later. Bot checks, rate limits and other transient failures are
retried on later runs and become `unavailable (failed repeatedly)` after three
attempts; until then nothing is shown. Five transient failures in a row pause
YouTube for the rest of the run. The cache key is the video ID, so the same
video linked in many messages is fetched once.

### Transcription

Transcription sits behind `Transcriber` in the shared `throughline-media`
package. **The primary setup is an OpenAI-compatible endpoint**: any server
that implements `POST <base>/audio/transcriptions` the way OpenAI does (OpenAI
itself, or a self-hosted Whisper server). Point the plugin at it; the
placeholders below are not real hosts or keys:

```powershell
$env:THROUGHLINE_TRANSCRIBE_URL = 'https://stt.example.invalid/v1'
$env:THROUGHLINE_TRANSCRIBE_MODEL = 'whisper-1'         # the server's model name
$env:THROUGHLINE_TRANSCRIBE_API_KEY = '<bearer key, if the server needs one>'
$env:BEEPER_TRANSCRIBER = 'openai'                      # or leave auto
uv run --package throughline-beeper throughline-beeper backfill --instagram-links --youtube-links
```

Long audio is converted to mono 16 kHz 32 kb/s Opus, split into chunks below
`THROUGHLINE_TRANSCRIBE_MAX_BYTES` (and `_CHUNK_SECONDS`) preferably at
silences, each chunk is sent with `response_format=verbose_json` (plain `json`
when the server refuses it) and the segments are merged on one timeline with
absolute timestamps. HTTP 429 and 5xx, timeouts and connection errors are
retried with exponential backoff (honouring a numeric `Retry-After`), and
`THROUGHLINE_TRANSCRIBE_MAX_MINUTES` caps the audio sent per run; work beyond
it is left for the next run, not recorded as failed. See the
[media README](../media/README.md) for the details.

The other transcribers: `faster-whisper` (install the optional extra: `uv sync
--all-packages --extra whisper`; `auto` uses it when installed; model from
`BEEPER_WHISPER_MODEL`, CPU int8 by default via `device=auto`; `auto` uses the
endpoint when the URL is set and faster-whisper is not installed) and the null
transcriber, which records `transcript unavailable` (and, for YouTube, makes
`auto` use captions). The run result's `estimated_model_cost` stays 0: a paid
endpoint's cost is not estimated.

The run result adds `enrichment` counts (`attachments_converted`,
`instagram_video`, `youtube_ok`, ...) when anything was done.

## Pagination, replay, and limits

Archive messages use **`archive.messages.id` unchanged** as external ID.
`native_id` is deliberately never used as a message key: it can be empty or
duplicated. Message rows stream through a named server-side cursor with
the composite conversation/time/ID seek documented below; no OFFSET, per-message SQL, or entire
message-payload materialization. Every acknowledged batch checkpoints its last composite key. Small conversation/identity tables and compact ID/alias lookup maps are kept
in memory. Replies are written in a resumable second pass after messages exist,
resolving exact archive IDs or unambiguous `message_aliases` on the same network.
Missing/ambiguous targets keep their source reference in the raw payload, with a
null normalized reply field, and do not create a link.
Reply links have type `reply_to`, method `source`, and are sent in batches.

A local synthetic test with 355,020 TEXT-shaped archive rows read and mapped
31,734 messages/second (11.19 seconds, including 59,170 reply records). This is
source-adapter throughput only: it excludes HTTP ingestion and is not a claim
about end-to-end core performance. No owner archive was accessed.

After a completed archive pass, the next invocation reconciles the full table.
This intentionally catches edits and newly inserted IDs that sort before the
previous checkpoint; archive IDs are opaque, not time-ordered. A resumed
incomplete pass seeks directly after its acknowledged ID. Rows added behind
that position are picked up on the next completed reconciliation. Source reads
run in read-only transactions. Use a stable restored archive during an initial
import. No DDL, indexes or writes are performed on it.

Live lists `/v1/accounts` and all pages of `/v1/chats` (limit 200), then each
chat's `/v1/chats/{chatID}/messages`. No network filter is applied. Historical
pages use `oldestCursor` / `direction=before`; subsequent sync pages use
`newestCursor` / `direction=after`. All cursors remain opaque. Chats are
rediscovered each run so newly connected networks are included. Reply-bearing
chats get checkpointed reply-link delivery after ingestion to resolve targets across historical
pages; this uses memory proportional to that chat's IDs/replies, not all raw
history. Chats without pending replies do not incur that extra read. A target
outside the available history receives no link. Reads retry transport errors,
429 and 5xx up to five attempts with bounded backoff and numeric Retry-After.
The official documentation says local reads are unrestricted and publishes no
numeric read quota. The plugin still handles throttling defensively.

**Cross-source deduplication is not reliably feasible from the supplied schema.**
`metadata_json` is only specified as an original platform/Beeper record, with no
guaranteed Beeper account/chat/message key layout. Beeper's live message ID can
be installation-local. Guessing from native IDs or content risks merging distinct
messages. Live external IDs therefore namespace the encoded account/chat/message
tuple, while archive IDs stay intact. Importing overlapping live and archive
history can create duplicates and separate threads. Metadata is retained for a
future explicitly validated crosswalk; this plugin does not claim deduplication.

Beeper can expose only locally indexed history. Network depth, disappearing
messages, bridge-specific sender semantics and real credentials have not been
verified. Incremental timestamp pagination does not guarantee discovery of edits
or history indexed later behind an existing cursor. A fresh cursor/full replay
is needed for such reconciliation. No deletion propagation is inferred. Network
display-name changes may change the channel slug; unusual display names that
normalize identically share a channel. Beeper accounts are used for routing and
retained in live external IDs; this plugin does not create account-event messages.

## Tests

```powershell
uv run pytest -q plugins/beeper
uv run ruff check plugins/beeper
```

Both source modes run the SDK contract kit on obviously invented recordings,
including dry-run, versions, idempotent full replay, fresh sync, raw payloads,
secret canaries and three crash barriers per source; a third contract run has
enrichment on, downloading generated PDF/XLSX files from a loopback HTTP server,
and proves replays add no versions. `test_beeper_enrichment.py` builds every
document (PDF, DOCX, XLSX, ODT, PPTX, PNG) from code and synthetic test-pattern
videos with ffmpeg (skipped without ffmpeg); downloads use fake transports,
Instagram and transcription use fakes, and the enrichment state lives in a
per-test temporary directory. `test_beeper_youtube.py` drives YouTube links
through an in-process fake fetcher that makes synthetic tone audio and
test-pattern video with ffmpeg. `test_beeper_pacing.py` covers new-inbound-only,
`--backlog`, daily limits across runs (the rolling window is advanced by ageing
the usage rows), the live revisit queue and transcription-minute deferrals.
Most other tests set `BEEPER_ENRICH_SINCE=2000-01-01` so the invented, older
fixture messages count as new inbound. No real Instagram, YouTube, Discord or owner
content is fetched. Focused tests also create
and drop a random `tl_test_*` Postgres database with the exact archive TEXT
columns/keys to exercise the real server-side cursor and resume behavior. Only
the administrative `postgres` database and throwaway databases are accessed;
owner databases and real services are never used. Fixtures can be regenerated
with `python plugins/beeper/tests/make_recordings.py`.

The contract tests retry one known core setup race: concurrent migrations in
separate disposable databases both `ALTER ROLE` the same cluster-wide roles,
causing `tuple concurrently updated` at `src/throughline/db.py:52`. The core's
advisory migration lock is database-local. Only that migration subprocess error
is retried (at most five attempts); plugin failures and contract assertions are
never retried or skipped. No shared core files are changed.

## Official references consulted

- https://developers.beeper.com/desktop-api/ — history limits and local reads
- https://developers.beeper.com/desktop-api/auth/ — enabling access and Bearer tokens
- https://developers.beeper.com/desktop-api-reference/ — API overview
- https://developers.beeper.com/desktop-api-reference/resources/accounts/methods/list/ — account/network identity
- https://developers.beeper.com/desktop-api-reference/resources/chats/methods/list/ — chat pagination, cursors and limits
- https://developers.beeper.com/desktop-api-reference/resources/chats/ — chat types and participants
- https://developers.beeper.com/desktop-api-reference/resources/messages/methods/list/ — message fields and historical/incremental pagination


### Archive ordering and quarantine

Messages now stream by `(COALESCE(conversation_id, ''), COALESCE(sent_at, ''), id)`,
using PostgreSQL `C` collation on all three TEXT expressions. Empty/NULL times
sort first; duplicate times remain stable through the ID tiebreak. External IDs
are unchanged. Checkpoints contain this three-part key. Old ID-only importer
checkpoints restart an idempotent reconciliation because they cannot represent
the new order. Source rows must remain unchanged during a resumable scan;
completed scans reconcile inserts and edits on the next run.

For an indexed seek, the source administrator can provide exactly this index:

```sql
CREATE INDEX archive_messages_conversation_sent_id
ON archive.messages ((COALESCE(conversation_id, '') COLLATE "C"),
                     (COALESCE(sent_at, '') COLLATE "C"), (id COLLATE "C"));
```

The plugin never creates this index: its source connection is read-only. Without
it, PostgreSQL sorts once per named-cursor scan (again after a restart), rather
than once per batch. A synthetic 355,000-row temporary table with 1,000 conversations, duplicate
and empty times took 42.911 seconds to sort and fetch in 2,000-row chunks on the
concurrently busy development machine (Postgres, no index). This one-time cost
is acceptable; this is not a full-import throughput benchmark.

Malformed JSON, NUL-containing decoded values and content above the conservative
100,000 UTF-8 byte full-text budget are quarantined before batch ingestion.
Logs contain only the source ID and end-of-run count; dry-run returns
`quarantined` as well. Quarantined IDs are excluded from archive reply links.
Raw payloads of imported rows remain untouched. Naive archive timestamps use a
UTC storage convention with `time_precision=timezone_unknown` for event time;
this does not assert knowledge of the original timezone. When sent_at is absent,
parseable ISO or `Mar 3, 2019, 4:15 PM` time_raw precedes first_imported_at;
explicit offsets are preserved. A row with neither (in the real archive these
rows have no `time_raw` at all) takes the time of the neighbouring dated row of
its conversation in stream order. Undated rows sort first in a conversation, so
no dated row precedes them and the neighbour is always the next one: the
conversation's earliest dated message. Such a row gets
`time_precision=inferred` and `time_inferred_from={"method":
"next_in_conversation", "message": <that row's ID>, "sent_at": <its time>}`.
Only a conversation with no dated row at all, or a row with no
`conversation_id` (such rows form no conversation and are never used as each
other's neighbours), falls back to `first_imported_at`
(`time_precision=unknown`, `time_inferred_from={"method": "first_imported_at"}`).
The payload keeps the original empty `sent_at`. Messages whose time came from
the source carry no `time_inferred_from` key at all, so their payload hash is
the same as before the field existed and a rescan writes no new version.

Live replies use the fetched rows; there is no second whole-history read. A
reply link is sent once a checkpoint has acknowledged both of its messages, so
the cursor carries only replies whose target has not been seen yet in this run,
not the whole chat's. Those leftovers are sent when the chat's scan ends, since
the target may have been stored by an earlier run. A link whose target the
destination does not hold (deleted, older than the bridged history, or
quarantined) is dropped with a log line and counted as `unlinked` in the run
result: the plugin stores the batch's messages, then bisects the links and
drops only those the core answers with `link message does not exist`. Any other
link error still stops the run before its checkpoint.
A chat without a newest cursor remains a historical scan on the next sync.
