# throughline-trakt

Live Trakt connector: the owner's complete movie and episode watch history, their
ratings and their watchlist, read from the Trakt API with OAuth.

## What it captures

| Channel | Kind | What |
| --- | --- | --- |
| `trakt.history` | life event, not embedded | Every play in `/sync/history` (movies and episodes; scrobbles, check-ins and manual "watched" marks). Full history on the first run, then incremental. |
| `trakt.ratings` | reference | Movies, shows, seasons and episodes the owner rated (`/sync/ratings`), dated when the rating was last set. |
| `trakt.watchlist` | reference | Movies, shows, seasons and episodes on the watchlist (`/sync/watchlist`), dated when they were added, with rank and notes. |

Fields follow the same vocabulary as the unified `media.consumption` channel, so
"what did I watch" reads alike across Plex, Jellyfin, Trakt and others: `service`
(`trakt`), `source_kind` (`history` / `rating` / `watchlist`), `event_type` (`play` /
`rated` / `watchlist_add`), `media_type` (`movie`, `episode`, `show`, `season`),
`title`, `show`, `season`, `episode`, `year` (movies and shows), `duration_s` (runtime,
not time spent), `account`, `native_id` (`trakt:<type>:<id>`), `native_ids` (Trakt,
slug, IMDb, TMDB, TVDB and the show's IDs), `url` (trakt.tv page), `detail`,
`timing_basis`, `time_precision` (`source_mark`), `source` (`api`), `author_kind`
(`owner`), `mapping_revision`. History adds `action` (`scrobble` / `checkin` /
`watch`); ratings add `rating` (0-10; Trakt's 1-10 scale is already that) and
`rating_raw` (`8/10`); the watchlist adds `rank` and `notes`.

Content reads like the other video sources: `Watched Invented Station S01E02 - The
Placeholder`, `Watched The Invented Expedition (2019)`, `Rated ... 8/10 on Trakt`,
`Added ... to the Trakt watchlist`.

Messages are authored by the owner's account (`system: trakt`, the Trakt user name).
Each record links (`consumed`, `rated` or `saved`) to a `work` entity; episodes link
`part_of` their season and the season `part_of` the show. Entity references prefer
IMDb (`imdb:tt...`) so the same title joins across sources, else
`tmdb:<type>:<id>`, `tvdb:<type>:<id>` or `trakt:<type>:<id>`; seasons are
`<show ref>:season:<n>` both when rated and when reached through an episode.

Threads: one per UTC day for history (`Trakt 2024-03-01`); ratings and the watchlist
each share one thread.

## Setup

1. Create an API app at <https://trakt.tv/oauth/applications> (any name; redirect URI
   `urn:ietf:wg:oauth:2.0:oob`; no permissions need ticking). Copy the **Client ID**
   and **Client Secret**.
2. Sign in once with the device-code helper. It prints a code to enter at
   <https://trakt.tv/activate>, waits for approval and writes the token file with
   mode 0600:

```sh
export TRAKT_CLIENT_ID=... TRAKT_CLIENT_SECRET=...
throughline-trakt-login            # or --token-file /path/to/trakt-token.json
```

3. Run the connector:

```sh
throughline-trakt backfill         # full history
throughline-trakt sync --once      # incremental, e.g. from a 15-minute timer
```

The connector refreshes the access token itself (shortly before it expires, or on
a 401) and rewrites the token file atomically. If Trakt rejects the refresh token
(revoked app, password change), the run fails asking you to run
`throughline-trakt-login` again. Access and refresh tokens are never written to
records or logs; they are added to the log redaction filter as soon as they are
read.

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `TRAKT_CLIENT_ID` | yes (secret) | | API app client ID (sent as `trakt-api-key`) |
| `TRAKT_CLIENT_SECRET` | yes (secret) | | API app client secret (used for login and token refresh) |
| `TRAKT_TOKEN_FILE` | no | `~/.config/throughline/trakt-token.json` | OAuth token file (`access_token`, `refresh_token`, `expires_at`) |
| `TRAKT_USER` | no | from `/users/settings` | Trakt user name used as author and in IDs |
| `TRAKT_PAGE_SIZE` | no | `100` | Items per page (1-1000); one checkpoint per page |
| `TRAKT_REQUEST_DELAY_S` | no | `0.3` | Pause between requests (Trakt allows 1000 GETs per 5 minutes) |
| `TRAKT_RATINGS` | no | `true` | Also import ratings |
| `TRAKT_WATCHLIST` | no | `true` | Also import the watchlist |
| `TRAKT_TEST_TRANSPORT` | no | | Testing only: `module:factory` returning an httpx transport |

Plus the standard `THROUGHLINE_URL`, `THROUGHLINE_TOKEN` and, under a timer,
`THROUGHLINE_INTERVAL_S`. The token file must be writable by the connector's user.

## How sync works

History: each run fixes a window `start_at = newest watched_at delivered`
(inclusive) to `end_at = now`, reads the page count, then walks pages from the
oldest to the newest, checkpointing the newest play after every page. The fixed
`end_at` keeps page boundaries stable while new plays arrive; the inclusive
`start_at` re-reads only the boundary instant, which history IDs
(`trakt:history:<id>`) deduplicate. A crash resumes from the last checkpoint.

Ratings and watchlist: read in full (oldest first, one checkpoint per page size),
but a `sync` run first asks `/sync/last_activities` and skips a list whose
`rated_at` / `watchlisted_at` has not changed since the last complete read. A
re-rating updates the same record (`trakt:<user>:rating:<type>:<trakt id>`) with
the new value and date.

## Limitations

- Plays removed from Trakt history after import are not deleted here, and edits to
  a play's date arrive only if the edited play falls after the cursor (a `backfill`
  re-reads everything).
- Removing a rating or a watchlist entry is not reflected: the record stays. Ratings
  and watchlist entries show only their current state, not earlier values.
- `watched_at` for manual "watch" marks is whatever the owner entered (sometimes the
  release date); `action` tells them apart from scrobbles and check-ins.
- Directors, cast and genres are not fetched (no `created_by` links); that would
  cost one extra request per title.
- `duration_s` is the work's runtime from `extended=full`, not how long was watched.
- Assumed from Trakt's API documentation, not checked against a live account in
  tests: response shapes, the `X-Pagination-*` headers and the device-flow status
  codes.

No dependencies beyond `throughline-sdk` and `httpx`.
