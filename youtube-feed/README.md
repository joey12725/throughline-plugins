# Throughline YouTube feed plugin

Follows a list of YouTube channels and records their new uploads, and
optionally records a model's watch/skip decision for each new upload (the
"router"). Two channels:

- `youtube.uploads`: one message per video (external ID = video ID), at the
  video's published time, threaded per YouTube channel (`channel:<id>`). Fields:
  video ID, URL, title, channel ID and title, description, duration (when
  known), Shorts flag (null when unknown), live flag, transport and time
  precision. The author is the channel (`youtube` / `channel_id`). Links:
  upload `upload_of` the work `youtube:<video id>`, work `published_by` the
  organization `youtube:<channel id>`; these external refs match the
  `youtube` plugin, so both plugins meet on the same entities. The channel is
  `is_outside` (someone else's upload, not the owner's life) with location
  tolerance 0, so the owner's location track is not attached to it.
- `youtube.picks`: one message per (video, scorer) decision, record kind
  `derived`, at the scoring time, with the video's published time as subject
  time. Fields: scores (`watch_probability`, `progress_if_watched`,
  `junk_food_probability`, `watch_later_probability`), `verdict` (pick or
  skip), `picked`, `is_junk_food`, `reason`, `scorer`, `origin` (router,
  predictions, flagged), `scored_at`, `thresholds`, and `playlist_id` /
  `playlist_added_at` when this plugin added it to a playlist. Each pick links
  `pick_of` to its upload message; `pick_of` is the channel's provenance link
  type. Re-scoring by the same scorer is a new version of the same pick.

Every message carries `mapping_revision`. The mapping is deterministic: the
same input produces no new versions, and a reset cursor reproduces the same
current state.

## Two layers

`src/throughline_youtube_feed/fetch.py` is a standard-library-only script
(Python 3.12 to 3.14, no third-party imports). It reads the remote sources
and writes raw records as JSON lines, each with `kind`, a stable `id` and the
raw source data:

| kind | id | content |
|---|---|---|
| `upload` | video ID | `transport` (`rss`, `page`), `raw` entry, optional `details` (watch page) and `short_check`; volatile data (views, rating, `updated`, relative times) kept apart in `observed` and never mapped |
| `pick` | `<video id>:<scorer>` | `origin` `router` (model `decision` and raw answer) or `predictions`/`flagged` (an imported youtube-notify line as `raw`) |
| `playlist_add` | `<video id>:<playlist id>` | a video this tool added to a playlist |
| `feed_error` | channel ID | a channel feed that failed in that run (reported only) |
| `run` | start time | run statistics (reported only) |

The plugin maps those records to messages. It reads them either live (calling
`fetch.py` in-process) or from export files, so the fetching can run on a host
where the credentials live and only the JSONL comes back.

## Setup

```powershell
uv sync --all-packages
$env:THROUGHLINE_URL = 'http://localhost:8000'
$env:THROUGHLINE_TOKEN = '<Throughline token scoped to youtube_feed>'
# Live: fetch here.
$env:YOUTUBE_FEED_SOURCE = 'live'
$env:YOUTUBE_FEED_CHANNELS = 'C:\path\to\subscriptions.opml'
uv run --package throughline-youtube-feed throughline-youtube-feed backfill --dry-run
uv run --package throughline-youtube-feed throughline-youtube-feed sync
# Or map files exported elsewhere (a file or a directory, read recursively):
$env:YOUTUBE_FEED_SOURCE = 'export:C:\path\to\exports'
uv run --package throughline-youtube-feed throughline-youtube-feed backfill
```

Channel lists (`YOUTUBE_FEED_CHANNELS`, several files separated by the OS path
separator; plus `YOUTUBE_FEED_CHANNEL_IDS`, comma separated) may be:

- OPML (YouTube's old subscription export; `xmlUrl` with `channel_id=`);
- JSON: a list of IDs, objects with `channel_id`/`channelId`/`id` and a title,
  Data API `subscriptions.list` responses (`snippet.resourceId.channelId`), a
  mapping of ID to title, or a file with a `channels`, `subscriptions` or
  `items` key (such as a youtube-notify state or seed file: only that key is read);
- CSV, such as Takeout `subscriptions/subscriptions.csv`;
- text: one channel ID or channel URL per line, optionally followed by a title;
  `#` starts a comment.

Only `UC...` channel IDs are accepted; handles are not resolved.

## Export workflow (remote host)

Copy the single file `fetch.py` to the remote host and run it there with its
credentials in the environment:

```sh
export YOUTUBE_FEED_ROUTER_API_KEY=...        # only for --route
python3 fetch.py export --out exports/feed-$(date +%Y%m%dT%H%M).jsonl \
  --state state/feed-state.json --channels subscriptions.json \
  --transport auto --details --route
# Keep past decisions: convert youtube-notify files (or just copy them into
# the export directory; the plugin reads predictions*.jsonl and flagged*.json too).
python3 fetch.py import-notify --out exports/notify.jsonl --scorer jev \
  --predictions /path/to/predictions.jsonl --flagged /path/to/flagged.json
```

Ship the export directory back and set `YOUTUBE_FEED_SOURCE=export:<dir>`.
Every run re-reads all files and sends only what changed (the cursor holds a
digest per message), so leaving old files in place is fine. Other commands:
`fetch.py route --in FILE --out FILE --state FILE` scores uploads from earlier
exports, `fetch.py playlist --in FILE --out FILE --state FILE [--apply]` adds
picks from earlier exports to the playlist. `--state` makes runs incremental
(seen videos, scored videos, rotation, channel health, daily counters); a run
without it starts from scratch. Exit status 1 with a one-line message on
failure; statistics go to stderr as JSON. Nothing secret is ever written.

## Settings

All names start with `YOUTUBE_FEED_`; `fetch.py` and the plugin read the same
variables (command-line flags of `fetch.py` override non-secret ones).

| Variable | Default | Meaning |
|---|---|---|
| `SOURCE` | `live` | `live` or `export:<file or dir>[;more]` (plugin only) |
| `CHANNELS` / `CHANNEL_IDS` | | channel list files / IDs (required live) |
| `TRANSPORT` | `rss` | `rss`, `page` (public channel videos page) or `auto` (RSS, then page) |
| `DETAILS` / `DETAILS_PER_RUN` | `0` / `50` | fetch each new video's watch page once (duration, description, exact date) |
| `CHECK_SHORTS` | `0` | one HEAD of `/shorts/<id>` (redirects not followed) per new video of unknown type |
| `CHANNELS_PER_RUN` | `0` (all) | rotate through the list in batches |
| `REQUESTS_PER_DAY` | `5000` | daily cap on YouTube requests, retries included |
| `DELAY_S`, `TIMEOUT_S`, `RETRIES`, `BACKOFF_S` | `1.0`, `20`, `4`, `2` | pacing; 429/5xx/transport errors retry with exponential backoff, honouring `Retry-After` (max 300 s) |
| `SEEN_DAYS` | `120` | how long seen/scored videos are remembered |
| `ROUTER` | `0` | `1` scores new uploads (paid; off by default; never in a dry run) |
| `ROUTER_URL`, `ROUTER_MODEL` | | OpenAI-compatible base URL (`.../v1`) and model |
| `ROUTER_API_KEY` | | secret, sent as Bearer |
| `ROUTER_PROMPT` | built in | system prompt file; the answer must be one JSON object |
| `ROUTER_PER_RUN` / `ROUTER_PER_DAY` | `25` / `200` | scoring limits (UTC day) |
| `ROUTER_MAX_AGE_H` | `72` | only uploads this recent are scored, only picks this recent are added |
| `ROUTER_SKIP_SHORTS` | `1` | never score Shorts |
| `ROUTER_JSON_MODE`, `ROUTER_MAX_TOKENS` | `0`, `400` | `response_format` json_object; answer length |
| `THRESHOLDS` | 0.70 / 50 / 0.50 | JSON object or file: `min_watch`, `min_progress`, `max_junk` (youtube-notify names `watch_probability`, `progress_if_watched`, `junk_food_probability` also accepted). Pick = watch >= min_watch and progress >= min_progress and junk < max_junk |
| `CACHE_DIR` | | caches router answers so a crashed or repeated run does not pay twice |
| `PLAYLIST` | | playlist ID, `WL`, or the title of one of your playlists |
| `PLAYLIST_APPLY` | `0` | `1` really inserts; otherwise only the plan is reported (also `fetch.py --apply`) |
| `PLAYLIST_PER_DAY` | `20` | inserts per UTC day (50 quota units each) |
| `OAUTH_TOKEN` | | secret: an access token with a YouTube scope, or: |
| `CLIENT_ID`, `CLIENT_SECRET`, `REFRESH_TOKEN` | | OAuth client and refresh token (secrets except the ID) |
| `IMPORT_SCORER` | `imported` | scorer name for imported youtube-notify predictions (e.g. `jev`) |
| `SINCE` / `UNTIL` | | backfill bounds: uploads by published time, picks by scoring time (an upload a pick needs is still sent) |
| `BATCH` | `500` | messages per checkpoint |
| `ARCHIVE` | | live mode: append every fetched raw record to this JSONL file |
| `RSS_URL`, `WEB_URL`, `API_URL`, `TOKEN_URL`, `USER_AGENT` | public endpoints | overridable for tests and proxies |

## Behaviour and limits

- RSS feeds list a channel's latest 15 uploads; there is no deeper backfill
  from feeds. Older history comes from imported youtube-notify predictions
  (which carry title, channel, duration and published time) and from the
  `youtube` plugin's Takeout import.
- The first read of a channel records its current feed entries. Later runs
  send only new or edited entries; view counts and `updated` stamps never
  create versions. A more complete record of a video (watch-page details, a
  Shorts check, RSS instead of page or prediction) replaces a poorer one; a
  poorer record never replaces a richer one.
- `page` transport (and the `auto` fallback) reads the public channel videos
  page. Its times are relative ("3 hours ago"), so the estimate is fixed at
  first sight (time precision `approximate`) until watch-page details supply
  the exact date. Page parsing follows YouTube's current page data and may
  need updates when the page changes.
- Feed failures are recorded per channel and reported; one failing channel
  never stops the run. Daily caps stop a run cleanly and resume the next day.
- Router answers are parsed from the first JSON object in the reply; scores
  outside their range are clamped (percentages given as 0-100 are scaled).
  A failed answer is retried once on a later run, then left alone.
- In live mode the fetch state (seen, scored, counters) is stored in the cursor
  only with the final checkpoint: a crash re-fetches, and may re-score up to one
  run's worth unless `CACHE_DIR` is set.
- The YouTube Data API has historically refused inserts into Watch Later
  (`WL`); use a named playlist. Playlist writes are a dry run unless applied,
  only for picks newer than `ROUTER_MAX_AGE_H`, and each video is tried once.
  Missing OAuth credentials stop the run before any router call; a token or
  playlist lookup failure at run time is reported in the stats
  (`playlist_setup_error`) and retried next run, so paid picks are still
  written.
- Credentials (router key, OAuth tokens, client secret) are only sent over
  HTTPS (plain HTTP is accepted for localhost only), and requests that carry
  them never follow redirects.
- Picks carry the video's published time as their subject; when that time is
  only a date or a page estimate, the subject says so (`day` or
  `approximate` precision).

## Verification

`uv run --no-sync pytest -q plugins/youtube-feed` and
`uv run --no-sync ruff check plugins/youtube-feed`. Tests use invented
fixtures written in the tests and a local fake HTTP server; no test reaches
the network. They cover the SDK contract kit (dry run, replay, reset, crash
and resume, reversed delivery, secrets), channel list formats, feed parsing,
incremental fetching, retries and daily limits, rotation, page transport and
details, Shorts checks, the standalone `fetch.py` command, router thresholds,
limits and cache, playlist dry run and apply, youtube-notify imports, and the
mapping's determinism.
