# throughline-lastfm

Live Last.fm connector: the owner's complete scrobble history and loved tracks.

## What it captures

| Channel | Kind | What |
| --- | --- | --- |
| `lastfm.scrobbles` | life event, not embedded | Every scrobble from `user.getRecentTracks` (full history on first run, then incremental). The currently playing track is ignored until Last.fm records it as a scrobble. |
| `lastfm.loved` | reference | Loved tracks from `user.getLovedTracks`, dated when they were loved. |

Fields follow the same vocabulary as the unified `media.consumption` channel, so
"what did I listen to" reads alike across sources: `service` (`lastfm`),
`source_kind` (`scrobble` / `loved_track`), `event_type` (`play` / `liked`),
`media_type` (`track`), `title`, `creator`, `creators`, `album`, `account`,
`native_id` (track URL), `native_ids` (recording/artist/release MusicBrainz IDs when
Last.fm knows them), `url`, `timing_basis`, `time_precision` (`source_mark`),
`source` (`api`), `author_kind` (`owner`), `mapping_revision`; scrobbles also carry
`loved`.

Messages are authored by the owner's account (`system: lastfm`). Each scrobble
links `consumed` to a `work` entity, the work links `created_by` an `artist` and
`part_of` an `album`. Entities use MusicBrainz references
(`musicbrainz:recording:<mbid>`) when present, so the same recording joins across
Last.fm, ListenBrainz and other sources; otherwise `lastfm:track:<artist>|<title>`.

Threads: one per UTC day (`Last.fm 2024-03-01`); loved tracks share one thread.

## Setup

1. Create an API account at <https://www.last.fm/api/account/create> and copy the
   **API key** (the shared secret is not needed: only public read methods are used).
2. Set the environment and run:

```sh
export LASTFM_USER=your_user_name LASTFM_API_KEY=...
throughline-lastfm backfill       # full history (large libraries take a while)
throughline-lastfm sync --once    # incremental, e.g. from a 15-minute timer
```

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `LASTFM_USER` | yes | | Last.fm user name |
| `LASTFM_API_KEY` | yes (secret) | | API key |
| `LASTFM_PAGE_SIZE` | no | `200` | Items per page (max 200); one checkpoint per page |
| `LASTFM_REQUEST_DELAY_S` | no | `0.25` | Pause between requests (Last.fm asks for at most ~5/s) |
| `LASTFM_LOVED` | no | `true` | Also import loved tracks |
| `LASTFM_TEST_TRANSPORT` | no | | Testing only: `module:factory` returning an httpx transport |

Plus the standard `THROUGHLINE_URL`, `THROUGHLINE_TOKEN` and, under a timer,
`THROUGHLINE_INTERVAL_S`.

## How sync works

Each run fixes a window `from = last delivered scrobble time` (inclusive) to
`to = now`, reads the page count, then walks pages from the oldest to the newest,
checkpointing the newest scrobble delivered after every page. The fixed `to`
keeps page boundaries stable while new scrobbles arrive; the inclusive `from`
re-reads only the boundary second, which stable external IDs deduplicate. A crash
resumes from the last checkpoint. Loved tracks are read newest first and stop at
the previous high-water mark (a `backfill` re-reads all of them).

## Limitations

- Last.fm has no scrobble ID. The external ID is `user + timestamp + hash(artist,
  title, album)`; two identical scrobbles in the same second collapse to one.
- Scrobbles deleted or edited on Last.fm after import are not removed or changed
  here (Last.fm offers no change feed). Edits made before the first import arrive
  as edited.
- Un-loving a track is not detected; the loved entry stays.
- Track duration and listening time are not available from `getRecentTracks`.
- Private profiles ("hide recent listening") return errors without user auth.
