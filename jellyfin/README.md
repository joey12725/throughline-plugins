# throughline-jellyfin

Live Jellyfin connector: what the owner played on a Jellyfin server (movies,
episodes, music, audiobooks) and what was added to its libraries.

## What it captures

| Channel | Kind | What |
| --- | --- | --- |
| `jellyfin.plays` | life event, not embedded | Plays by the configured user: "Watched Show S01E02 - Title (45 min)", "Listened to Track by Artist". |
| `jellyfin.library` | life event, not embedded | Items added to the libraries (`event_type: library_added`, like `media.consumption`'s Plex additions): "Added Example Film (2019) to My Server (Movies)". |

Fields use the shared media vocabulary: `service`=`jellyfin`, `source_kind`
(`playback_reporting`, `last_played` or `library_added`), `event_type` (`play`,
`progress`, `library_added`), `media_type` (`movie`, `episode`, `show`, `track`,
`album`, `audiobook`, `book`, `video`), `title`, `creator`, `creators`, `album`,
`show`, `season`, `episode`, `year`, `duration_s` (runtime), `position_s`,
`progress`, `is_finished`, `device`, `client`, `account`, `library`, `added_at`,
`native_id` (`jellyfin:<server>:item:<id>`), `native_ids` (IMDb, TMDB, TVDB,
MusicBrainz provider IDs), `detail`, `timing_basis`, `time_precision`,
`source`=`api`, `author_kind`=`owner`, `mapping_revision`. Plays also carry
`listened_s` (seconds actually played), `play_method` and `play_count`.

Plays are authored by the Jellyfin user (`system: jellyfin`, `kind: user_id`) and
threaded per UTC day. Each play links `consumed` (each addition `saved`) to a
`work` entity, keyed by IMDb ID (`imdb:tt...`) or MusicBrainz recording ID when
the item has one, so the same film joins Trakt and other sources; episodes link
`part_of` their show, tracks `part_of` their album and `created_by` their artists.

## Play history: two sources

**Core Jellyfin does not keep a play history.** It stores, per user and item,
only `LastPlayedDate`, `PlayCount`, `Played` and the resume position. So:

- **With the [Playback Reporting](https://github.com/jellyfin/jellyfin-plugin-playbackreporting)
  plugin installed** (Dashboard > Plugins > Catalog), every playback is recorded
  with its start time, client, device, method and seconds played. The connector
  reads it through the plugin's query endpoint, oldest first, so every play
  (including repeats) is delivered. The plugin stores times in the server's local
  clock: set `JELLYFIN_TIMEZONE` to the server's zone. Its history only reaches
  back to when it was installed, and it deletes rows older than its retention
  setting (Playback Reporting settings), so run the connector well within that.
- **Without it**, each run delivers the items whose `LastPlayedDate` moved since
  the previous run: one message per (item, last-played time), with `play_count`.
  The first run gets only the latest play of each item, and an item played twice
  between two runs yields one message. Every such run reports `best_effort`
  coverage with reason `source_cap` and says why. Partly watched items are
  `event_type: progress` with `progress` and `position_s`.

`JELLYFIN_HISTORY=auto` (default) uses Playback Reporting when the server lists it.

## Setup

1. Dashboard > API Keys > add a key (API keys have administrator rights, which
   `/System/Info`, `/Users`, `/Plugins` and the Playback Reporting query need).
2. Configure and run:

```sh
export JELLYFIN_URL=http://jellyfin.local:8096 JELLYFIN_USER=yourname JELLYFIN_API_KEY=...
throughline-jellyfin backfill
throughline-jellyfin sync --once     # e.g. every 15 minutes
```

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `JELLYFIN_URL` | yes | | Server URL |
| `JELLYFIN_USER` | yes | | User whose plays are imported (case-insensitive name) |
| `JELLYFIN_API_KEY` | yes (secret) | | API key |
| `JELLYFIN_HISTORY` | no | `auto` | `auto`, `playback_reporting` or `last_played` |
| `JELLYFIN_TIMEZONE` | no | `UTC` | IANA zone of the server clock (Playback Reporting times) |
| `JELLYFIN_ADDED_TYPES` | no | `Movie,Series,Episode,MusicAlbum,AudioBook,Book` | Types reported as additions (empty disables) |
| `JELLYFIN_PLAYED_TYPES` | no | `Movie,Episode,Audio,AudioBook,MusicVideo,Video` | Types read for last-played history |
| `JELLYFIN_PAGE_SIZE` | no | `500` | Rows/items per request; one checkpoint per page |
| `JELLYFIN_TEST_TRANSPORT` | no | | Testing only: `module:factory` returning an httpx transport |

## Limitations

- See "Play history" above: without Playback Reporting, history is the latest
  play per item, observed at each run.
- Playback Reporting rows of items since deleted from the library keep the name
  the plugin recorded but lose metadata (`detail.item_missing`).
- Library additions are read newest first down to the previous high-water mark per
  library; items removed from the library are not removed here. Individual music
  tracks are not reported as additions by default (albums are).
- The Playback Reporting query endpoint and its response (`colums`, `results`)
  are the plugin's own API, not Jellyfin's; it is assumed stable across versions
  10.8-10.10.
