# Throughline consumption

One media consumption source for Throughline. It records what the owner
played, watched, listened to, liked or saved on four services in one channel,
`media.consumption`, with a `service` field and per-service detail fields:

| Service | What is captured | How |
| --- | --- | --- |
| Spotify | recently played tracks every five minutes; liked songs and playlist entries from the backup | the existing Composio MCP Spotify connection for the newest-50 fast lane; Web API/Composio REST also remain supported; backup files hourly |
| Spotify backup | liked songs and playlist entries from a directory of Spotify-API-shaped JSON backups (weekly snapshots plus a `latest` copy) | files, no API calls |
| Plex | watch/listen history, active-session positions and intervals, and library additions for movies, episodes and albums | read-only Plex API: history, sessions, section inventory and metadata; additions backfill `addedAt`, then use a per-section watermark |
| Audiobookshelf | listening sessions (start, end, time listened, start and end position, device), progress/finished state, and matching Kavita ebook text | read-only Audiobookshelf API hourly; optional read-only Kavita API download for ebook extraction |
| YouTube | continuous watch history and viewing sessions | reads the existing youtube plugin watcher exports, including its archive; that plugin alone owns the Chromium-cookie scraper, Watch Later and subscriptions |

Nothing is written to any service: every service call is a read. Kavita ebook
files may be downloaded into a private temporary directory for text extraction
and are removed at the end of the fetch.

## Which media plugin to use

This plugin overlaps with the single-service `spotify`, `plex` and `youtube`
plugins. Pick one per service so nothing is captured twice:

- Use **consumption** when you want one `media.consumption` channel across
  Spotify, Plex, Audiobookshelf and YouTube, fetched in one scheduled run (for
  example every five minutes, with the slower Audiobookshelf/Kavita and Spotify
  backup reads hourly).
- Use the standalone **spotify** or **plex** plugin when you want only that
  service, in its own channel. Do not run it alongside this plugin's Spotify or
  Plex service.
- Keep the **youtube** plugin enabled either way: it owns the watch-history
  scraper, Watch Later and subscriptions; this plugin only reads its watcher
  exports.

When the credentials live on another host, run `fetch.py export` there and
ship only the export JSONL and non-secret state back (see
[the remote-fetch pattern](../README.md#remote-fetch-pattern)).

## Two layers

`src/throughline_consumption/fetch.py` is a fetcher that uses only the
Python standard library (urllib, json; Python 3.12 to 3.14). It reads the
services and writes raw records as JSONL. It runs on its own wherever the
credentials live:

```sh
python3 fetch.py export --out consumption-<UTC time>.jsonl.gz --state consumption-state.json
```

The plugin (`throughline-consumption`, built on throughline-sdk) maps raw
records to Throughline messages. It reads either:

- `CONSUMPTION_SOURCE=live`: it runs the same fetcher in process, with the
  fetch state kept in the Throughline cursor; or
- `CONSUMPTION_SOURCE=export:<path>`: one export file, or a directory of
  them (`*.jsonl` and `*.jsonl.gz`, read in file-name order).

Both paths produce the same records and therefore the same messages.

### Export format

One JSON object per line. The first line is a header
(`{"kind": "export.header", "format": "throughline-consumption-export", "version": 1, ...}`),
the last a footer (`{"kind": "export.footer", "complete": true, "counts": ..., "stopped": ..., "errors": ...}`).
Every line between is a record:

```json
{"kind": "plex.history", "id": "<server>:<historyKey>", "service": "plex", "fetched_at": "...", "raw": {...}, "context": {...}}
```

`kind` is one of `spotify.play`, `spotify.saved_track`,
`spotify.playlist_item`, `plex.history`, `plex.session`,
`plex.library_added`, `audiobookshelf.session`,
`audiobookshelf.progress`, `youtube.watch`. `id` is a stable native ID,
`raw` the source object, `context` what the record needs from other calls
(Plex account, device, library section and stable metadata; the Audiobookshelf user and item
summary; the playlist; parsed YouTube fields). File-based records may carry
`observed_at` (the backup snapshot date). The file is written to
`<out>.partial` and renamed when complete; the plugin refuses a file without
its footer. `--out -` writes to stdout.

Kept out of ordinary consumption records on purpose, so that re-exporting the same play
gives the same bytes: Spotify `popularity`, `available_markets`,
`preview_url`, `is_playable`; Plex metadata other than identity, title,
numbering, year, duration and library. Library-addition records intentionally
keep `addedAt`, `ratingKey`, primary/provider GUIDs and section identity;
Audiobookshelf session `chapters` and `deviceInfo.ipAddress`.

## Messages

- **Record time** (`happened_at`) is when the consumption happened; there is
  no separate subject time, because each record is a life event at its own
  time. Spotify plays use `played_at`; Plex `viewedAt` (a played mark: start
  and stop are not reported); Audiobookshelf sessions run from `startedAt` to
  Plex active sessions run from their first observed poll to their last
  observed playing poll and carry position; Plex additions use `addedAt`.
  Audiobookshelf sessions run from `startedAt` to their last `updatedAt` (`ended_at`), with `listened_s` the real listening
  time; progress records start at `startedAt` and end at `finishedAt` when
  finished; likes and playlist entries use `added_at`; YouTube uses the
  Takeout watch time. `timing_basis` and `time_precision` say which.
- **First seen.** A like or playlist entry without `added_at` (Spotify
  reports none for very old playlists, some backups omit it) gets the
  `observed_at`/`fetched_at` of the first export that contained it,
  `time_precision=first_seen`. The plugin pins that time in its cursor; a
  later export never moves it forward.
- **Threads.** Plays are grouped per service and UTC day
  (`spotify:day:2020-01-10`); Audiobookshelf sessions and progress per item
  or episode (`audiobookshelf:item:<id>[:episode:<id>]`); likes in
  `spotify:liked`; playlist entries per playlist (`spotify:playlist:<id>`).
- **Senders.** Plex history has author `{system: plex, kind: account_id}`
  (as in the plex plugin), Audiobookshelf `{system: audiobookshelf, kind: user_id}`,
  playlist entries `{system: spotify, kind: user_id}` from `added_by`.
  Spotify plays and likes and YouTube views are the owner's and carry no author.
- **Links** (`method=source`): message `consumed` work (Spotify track URI,
  Plex GUID, `youtube:<video id>` as the youtube plugin uses, or
  `audiobookshelf:item:<id>[:episode:<id>]`); `liked` and `saved` for likes
  and playlist entries; `added_to` playlist (`spotify:playlist:<id>`); work
  `part_of` album, show or podcast; work `created_by` artist; video
  `published_by` channel (`youtube:<channel id>`, as the youtube plugin uses).
  A Plex addition `saved` link points at exactly the same work external ref as
  a Plex session/history row; episodes also link through season to show.
- **IDs** are `<service>:<kind>:<native id>`. YouTube Takeout views use the
  youtube plugin's Takeout ID (`takeout:watch:<digest>:<occurrence>`), so a
  view imported by both plugins has the same native ID. Parity holds for JSON
  history; HTML times can be written differently.
- **Payload** is the record without `fetched_at`/`observed_at`.

### Fields

| Field | Type | Meaning |
| --- | --- | --- |
| service | text | spotify, plex, audiobookshelf, youtube |
| source_kind | text | service-specific kind; Plex uses `history`, `active_session`, `library_added` (`kind` itself is a core-reserved field name) |
| event_type | text | play, session, library_added, progress, finished, liked, playlist_add |
| media_type | text | track, episode, movie, video, audiobook, podcast |
| title, creator, creators | text, text, json | work title; artists, author, channel or host |
| album, show, series | text | album of a track; show or podcast of an episode; book series with sequence |
| season, episode, year | integer | TV numbering, release year |
| duration_s | number | runtime of the work, not time spent |
| position_s, start_position_s, progress, listened_s | number | last position, session start position, fraction done, real listening time |
| is_finished | boolean | Audiobookshelf finished flag |
| device, client, account, library | text | device, player/app, source account, server library |
| added_at, plex_rating_key, guids | timestamp, text, json | Plex library-addition timestamp and stable join identifiers |
| playlist_name, playlist_id | text | playlist of an entry |
| context_type, context_uri | text | Spotify playback context |
| play_method | text | Audiobookshelf direct play, direct stream, transcode, local |
| is_short, is_music, is_ad | boolean | YouTube flags |
| native_id, native_ids, url | text, json, text | primary and all source IDs, public link |
| detail | json | further service values (explicit, narrators, media player, ...) |
| timing_basis, time_precision, source | text | timing meaning; source_mark, session, progress or first_seen; api, backup, takeout, youtube_plugin |
| mapping_revision | integer | mapping revision (currently 2) |

`started_at`/`ended_at` are the message's `happened_at` and `ended_at`
(`ended_at` is a reserved field name).

### Reruns and versions

Mapping is a pure function of the record, so re-importing the same export,
or a re-export of unchanged source data, creates no new versions. A record
the source really changed (an open Audiobookshelf session, progress, a
renamed playlist) becomes a new version of the same message. An older export
of such a record, imported after a newer one, is skipped: the cursor keeps
the source's own clock (`updatedAt`, `lastUpdate`) per changing record.
The cursor also keeps each export file's content hash and position, so a
crash resumes inside the file. `MAPPING_REVISION` in `mapping.py` is bumped
when the mapping of unchanged input changes; that is one deliberate
re-version of the channel.

## Environment

Credentials are read only from the environment, never from arguments, and
are never written to exports, state, cursors or logs. Both layers use the
same names.

| Variable | Default | Meaning |
| --- | --- | --- |
| CONSUMPTION_SOURCE | live | `live`, or `export:<file or directory>` (plugin only) |
| CONSUMPTION_SERVICES | all configured | subset of spotify, spotify_backup, plex, audiobookshelf, youtube; naming an unconfigured one is an error |
| CONSUMPTION_SINCE / CONSUMPTION_UNTIL | none | backfill bounds (ISO date or time; since inclusive, until exclusive) |
| CONSUMPTION_MAX_REQUESTS | 1000 | HTTP requests per run (0 = unlimited) |
| CONSUMPTION_MAX_REQUESTS_PER_DAY | 5000 | HTTP requests per UTC day, counted in the fetch state |
| CONSUMPTION_REQUEST_DELAY_S | 0.25 | minimum pause between requests |
| CONSUMPTION_MAX_ITEMS | none | records per service per run |
| CONSUMPTION_CHECKPOINT_EVERY | 500 | plugin checkpoint interval |
| SPOTIFY_ACCESS_TOKEN | | secret: access token used as is |
| SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_REFRESH_TOKEN | | secrets: refresh flow; refreshed tokens stay in memory |
| COMPOSIO_API_KEY | | secret: Composio REST API key (Spotify toolkit transport) |
| SPOTIFY_COMPOSIO_ACCOUNT_ID | | Composio connected account of the Spotify connection |
| COMPOSIO_USER_ID | | optional Composio user ID |
| SPOTIFY_COMPOSIO_TOOLS | defaults below | JSON overriding tool slugs `recent`, `saved`, `playlists`, `playlist_items` |
| SPOTIFY_COMPOSIO_MCP_URL | `https://connect.composio.dev/mcp` | existing Composio MCP endpoint for the near-real-time recently-played lane |
| SPOTIFY_COMPOSIO_MCP_TOKEN | | secret: MCP bearer token; sent only in the Authorization header |
| SPOTIFY_COMPOSIO_MCP_ACCOUNT | | connected Spotify account name passed to the workbench (required for the MCP lane) |
| SPOTIFY_EXPECTED_USER | | identity guard: set it to your Spotify user id and a different Spotify profile fails the run |
| CONSUMPTION_SPOTIFY_PLAYLISTS | 1 | 0 skips reading playlist entries from the API |
| CONSUMPTION_SPOTIFY_BACKUP_DIR | | Spotify backup directory |
| PLEX_SERVER_URL | | Plex server URL, e.g. `http://127.0.0.1:32400` |
| PLEX_TOKEN | | secret: Plex token (header only) |
| CONSUMPTION_PLEX_ACCOUNTS | all | Plex account IDs or names to keep |
| CONSUMPTION_PLEX_PAGE_SIZE | 200 | history page size, 1..1000 |
| CONSUMPTION_PLEX_METADATA_LOOKUPS | 1000 | metadata lookups per run |
| AUDIOBOOKSHELF_URL | | server URL, with or without `/api` |
| AUDIOBOOKSHELF_TOKEN | | secret: API token (Bearer) |
| CONSUMPTION_ABS_PAGE_SIZE | 50 | listening-session page size |
| CONSUMPTION_ABS_ITEM_LOOKUPS | 300 | item lookups per run (titles for progress records) |
| KAVITA_URL, KAVITA_API_KEY | | optional Kavita base URL and secret plugin key for matching ebook text |
| CONSUMPTION_BOOK_MIN_PROGRESS | 0.25 | ebook text is ingested only above this Audiobookshelf progress, or when finished |
| CONSUMPTION_BOOK_CHUNK_CHARS | 8000 | maximum text characters per `audiobooks.text` message |
| CONSUMPTION_BOOK_MAX_DOWNLOADS | 0 | Kavita ebook downloads per run (0 is unlimited) |
| CONSUMPTION_YOUTUBE_TAKEOUT | | extracted Takeout directory or a watch-history file |
| CONSUMPTION_YOUTUBE_MESSAGES | | JSON/JSONL file or directory of `youtube.watch` messages |
| CONSUMPTION_YOUTUBE_WATCHER | | export file or directory of the youtube plugin's history watcher (`fetch.py poll`): viewing sessions (`event_type=session`, estimated start and end, start and end position, `listened_s`) and history days (`event_type=play`, `time_precision=day`) |
| CONSUMPTION_SPOTIFY_API_BASE, CONSUMPTION_SPOTIFY_TOKEN_URL, COMPOSIO_BASE_URL | public endpoints | endpoint overrides (tests use them) |
| THROUGHLINE_URL, THROUGHLINE_TOKEN | | core URL and a plugin token scoped to `consumption` |

A service runs when its settings are present: Spotify needs an access token,
the three refresh-flow values, or `COMPOSIO_API_KEY` plus
`SPOTIFY_COMPOSIO_ACCOUNT_ID`, or the MCP token/account pair. MCP mode reads
only the newest 50 recently-played items; saved tracks and playlists come from
the backup. Default REST
Composio slugs: `SPOTIFY_GET_RECENTLY_PLAYED_TRACKS`,
`SPOTIFY_GET_USER_S_SAVED_TRACKS`, `SPOTIFY_GET_CURRENT_USER_S_PLAYLISTS`,
`SPOTIFY_GET_PLAYLIST_ITEMS`; the call is
`POST <base>/tools/execute/<slug>` with `x-api-key` and
`{connected_account_id, arguments}`, and the Spotify JSON is unwrapped from
`data` (or `data.response_data`). The REST path pages properly; do not use
the Composio MCP URL for backfill (it truncates lists).

## Limits, cursors and politeness

- Every HTTP attempt counts against the per-run and per-day request limits.
  At a limit the service stops cleanly (`stopped` in the footer and the run
  report) and the next run resumes. Other services still run.
- 429 and 5xx are retried up to five attempts with exponential backoff; a
  numeric `Retry-After` is honoured (capped at 120 s). Redirects are refused,
  so a credential header never follows a redirect. Other 4xx fail the service
  with a message that names the call and status only.
- Incremental cursors (the `--state` file, or the plugin cursor in live mode):
  Spotify recently played (newest played time; the API keeps about the last
  50 plays, so run at least every few hours), liked songs (newest `added_at`),
  playlist `snapshot_id`s (unchanged playlists are skipped; unreadable ones,
  403/404 or a failed tool, are noted and skipped); Plex `viewedAt` watermark
  with one day of overlap (none when resuming after a limit; history is read
  oldest first so a stopped run still advances), active Plex session
  observations, Plex library-addition `addedAt` per section with a one-day
  overlap; Audiobookshelf session
  `updatedAt` with six hours of overlap (sessions stay open) and progress
  `lastUpdate`; Takeout, message export and backup files by size and mtime.
  YouTube watcher files are tracked individually and read recursively, so the
  youtube plugin can move a poll into `archive/` without losing it.
- `--since`/`--until` bound a backfill. A run with `--until` never moves a
  cursor, and file and playlist marks only move on unbounded runs, so date
  windows can be backfilled one at a time and the regular run picks up after.
- Liked songs and Audiobookshelf sessions move their cursors only when a
  service finishes; for a first backfill above the request limit, raise the
  limit or backfill by date windows.
- Per-day counts persist only with `--state` (CLI) or in the cursor (live).

## Export workflow (credentials on another host)

1. Copy `fetch.py` to the host that holds the credentials. It needs Python
   3.12+ and nothing else.
2. Export the credentials into the environment of that one command, for
   example from the host's secret store, and run it on a schedule:

   ```sh
   export PLEX_SERVER_URL='http://<plex host>:32400'
   export PLEX_TOKEN="$(<secret reader> PLEX_TOKEN)"
   export AUDIOBOOKSHELF_URL='http://<audiobookshelf host>'
   export AUDIOBOOKSHELF_TOKEN="$(<secret reader> AUDIOBOOKSHELF_TOKEN)"
   export CONSUMPTION_SPOTIFY_BACKUP_DIR='<spotify backup directory>'
   python3 fetch.py services
   python3 fetch.py export \
     --out "<export dir>/$(date -u +%Y%m%dT%H%M%SZ)-consumption.jsonl.gz" \
     --state '<private dir>/consumption-state.json'
   ```

   Exit status 0 is success, 2 means some service failed (see the footer),
   1 a fatal error. Only call and status are printed, never values.
3. Ship the export files back to the Throughline host (any file copy), keep
   them in one directory, and import:

   ```sh
   export THROUGHLINE_URL='http://127.0.0.1:8000/v1/'
   export THROUGHLINE_TOKEN='<plugin token for consumption>'
   export CONSUMPTION_SOURCE='export:<directory of export files>'
   uv run throughline-consumption backfill --dry-run
   uv run throughline-consumption backfill
   ```

   Re-running the import is safe; files already imported are skipped by
   content hash. Keep the state file next to the fetcher: deleting it only
   makes the next export a full one.

For a live install (credentials on the Throughline host) set the service
variables and `CONSUMPTION_SOURCE=live`, then `uv run throughline-consumption sync`.
The deployed export workflow uses the five-minute/hourly cadence above.

## Spotify backup directory

The backup reader walks every `*.json` file (snapshots first, files under a
`latest` folder last; later files win for equal IDs) and finds
Spotify-API-shaped objects:

- a playlist: an object with `id`, `name` and `owner` or `snapshot_id` or
  `type: playlist`, whose entries are in `items`, `tracks` (a list or
  `{items: [...]}`) or `entries`, each with `track`/`item` and `added_at`;
- liked songs: a list under a file or key whose name contains `liked`,
  `saved` or `favorite`, of `{added_at, track}` entries or bare track objects.

Unreadable files are counted (`spotify_backup_unreadable_files`), not fatal.
CSV backups are not read. A song in many snapshots is one record, with the
earliest snapshot date as its first-seen time when `added_at` is missing.
The real backup layout has not been verified by this build: run
`fetch.py export --services spotify_backup` once and compare the counts
with the backup's own totals before trusting it.

## Coverage and gaps

| Service | Expected live coverage | Gap signal / known boundary |
| --- | --- | --- |
| Spotify | newest 50 plays polled about every five minutes; likes/playlists from the backup | if a full 50-row window starts after the prior watermark, the plugin emits `coverage(status=partial, reason=source_cap)` with exact `from`/`to`; Spotify cannot recover that interval |
| Plex | history plus active polls every five minutes; all existing movie/episode/album additions backfilled, then overlapped watermark sync | active start/end are poll observations, not player events; history `viewedAt` is a played mark |
| Audiobookshelf | sessions/progress hourly with overlap; finished/progress changes are mutable by source clock | other users' admin-only session endpoints are not read |
| YouTube | every watcher export produced by the existing youtube timer, including archived exports | history days are day precision and session boundaries are estimates; if the youtube watcher fails to produce an export, consumption has no independent cookie scraper |

Fetch footer `errors`, `stopped`, request counts and per-kind counts are kept in
the sync log. Spotify's irrecoverable newest-50 cap is additionally represented
as a Throughline coverage record, not just a log warning.

## Known limits

- Spotify recently played only reaches about the last 50 plays; older
  listening needs the Spotify extended history export (plugins/spotify).
  `played_at` is a played mark: Spotify does not say whether it is the start
  or end. Unlikes and playlist removals are not recorded.
- Plex history has a played mark only; active-session position and interval are
  separately polled and may coexist with the later history mark.
- Plex additions cover movies, episodes and albums. Shows, seasons, artists and
  tracks are hierarchy/container objects rather than requested addition events.
- Audiobookshelf admin listening sessions of other users
  (`/api/users/<id>/listening-sessions`) are not read.
- A view imported both from Takeout and from a youtube plugin message export
  has the same message ID; importing both can version it between the two
  payload shapes. Use one of them per view.

## Tests

```sh
uv run --no-sync pytest -q plugins/consumption
uv run --no-sync ruff check plugins/consumption
```

The tests use invented data only, served by a fake HTTP server on
127.0.0.1 (Spotify, its token endpoint, Composio, Plex, Audiobookshelf),
plus invented backup and Takeout files. They run `fetch.py` in an isolated
interpreter, check retries, limits, redirects, cursors and secret canaries,
and run the SDK contract kit twice: from export files and live. The kit
needs the local test PostgreSQL (`THROUGHLINE_TEST_DATABASE_URL`, default
`127.0.0.1:54331`).
