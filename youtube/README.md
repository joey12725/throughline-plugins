# Throughline YouTube

Imports extracted Google Takeout history, playlist CSVs (including Watch Later),
and subscription CSVs. Polls subscriptions with the official YouTube Data API.
Optionally observes history and Watch Later using an independently installed
yt-dlp and the owner's browser cookies. Both live routes are **off by default**.
There are no transcripts, downloads, video details requests, or metadata
enrichment.
A standard-library **history watcher** (`fetch.py`) turns periodic polls of
the signed-in history, Watch Later, and subscriptions pages into viewing
sessions and state snapshots, so a video watched in parts becomes several
events; see [History watcher](#history-watcher-sessions).

## History watcher (sessions)

`src/throughline_youtube/fetch.py` uses only the Python standard library, so it
can be copied alone to the machine that holds the owner's signed-in browser
cookies. Each poll reads signed-in YouTube web pages through the web
client's own API (InnerTube `youtubei/v1/browse`, the same calls the web
app makes; authenticated with the cookies plus the `SAPISIDHASH` header):

- `FEhistory`, the watch history, grouped under day headings ("Today",
  "Yesterday", "Monday", "Nov 3", "Nov 3, 2024"), each video with its resume
  bar (percent watched) and length;
- `VLWL`, Watch Later, each entry with its playlist item ID (`setVideoId`);
- `FEchannels`, subscribed channels, each with its channel ID and title.

```sh
# Cookies are read in place, never copied, printed or logged. One of:
export YOUTUBE_COOKIES_FILE=/private/youtube-cookies.txt       # Netscape cookies.txt
export YOUTUBE_COOKIES_FIREFOX=/home/me/.mozilla/firefox/abcd.default  # cookies.sqlite read in place (immutable, read-only)
export YOUTUBE_COOKIES_CHROMIUM=/home/me/.config/chromium/Default      # Linux Chromium Cookies read in place, decrypted in memory via secret-tool + libcrypto
export YOUTUBE_COOKIES_BROWSER=chromium                         # needs yt-dlp importable; decrypted in memory
export YOUTUBE_COOKIE_HEADER="$(secret-tool lookup youtube cookie)"     # raw Cookie header
export YOUTUBE_TZ=America/New_York   # the account's day boundary for history headings
python3 fetch.py poll --state yt-watch-state.json --full --out yt-$(date -u +%Y%m%dT%H%M%SZ).jsonl.gz   # once: backfill
python3 fetch.py poll --state yt-watch-state.json --out yt-$(date -u +%Y%m%dT%H%M%SZ).jsonl.gz          # every 15 minutes
```

Then import the exports (a file or a directory of them, read in name order):

```sh
export YOUTUBE_WATCHER=/path/to/youtube-watcher
uv run throughline-youtube sync --once
```

How sessions are found: the history page lists each video once, under the
day it was last watched, most recent first, and moves a video back to the top
when it is watched again. Every poll is compared with the previous one. A
video is in a session when it is new above videos seen before, when it moved
above a video that used to be ahead of it, or when its resume bar changed by
at least one point. Watching a video in parts across polls therefore yields
one session per part, with `progress_before`/`progress_after`,
`watched_fraction` and `watched_s` (fraction times length). A resume bar that
went backwards is a `rewatch` (at least the new position was watched). The
sessions of one poll window are laid out back to back from the poll time,
most recent first: `happened_at`/`ended_at` are estimates
(`time_precision=estimated`) inside `window_start`..`window_end`. The shorter
the poll interval, the tighter the estimate. The first poll is a baseline
(no sessions). A video newly visible under a heading older than the window,
or below everything the previous poll saw, is not a session.

Records (JSONL with header and footer, `kind`, `id`, `raw`):

| kind | id | meaning | plugin channel |
|---|---|---|---|
| `history_item` | `<video>:<day>` | watched on that history day; `progress` pinned at first sighting | `youtube.history`, `record_type=day`, `time_precision=day`, `happened_at` the local day start |
| `session` | `<video>:<poll time>` | one viewing session | `youtube.history`, `record_type=session`, `time_precision=estimated`, with `ended_at` |
| `watch_later` | `setVideoId` | entry with `first_seen`; `removed` and `removed_seen` once gone from a complete snapshot | `youtube.watch_later`, `time_precision=first_seen` |
| `subscription` | `channel_id` | subscribed channel with `first_seen`; `removed` and `removed_seen` once gone from a complete snapshot | `youtube.subscriptions`, `time_precision=first_seen` |
| `poll` | poll time | counts only | not mapped |

Because a video moves to its latest day, the union of `history_item`
records over time is one record per video per viewing day; a day record and
that day's sessions describe the same viewing at two granularities.
The `consumption` plugin reads the same exports (`CONSUMPTION_YOUTUBE_WATCHER`)
into `media.consumption`: sessions as `event_type=session` (positions,
`listened_s`), history days as `event_type=play`.

Limits: this is YouTube's unofficial web API and may change; an unparseable
response fails the poll and leaves the state untouched. It needs a live
signed-in cookie session (cookies expire or are rotated when the browser
signs out). The history page holds one entry per video (the latest day), so a
backfill cannot see repeat views on earlier days; only Takeout
(`watch-history.json`) or Google My Activity has every view with its time.
Viewing with history paused, or from a signed-out device, is invisible.
Requests are paced (`--min-interval`, default 1 s); a normal poll reads
`--history-pages` (default 3), Watch Later, and subscriptions.

## Verified capabilities (17 November 2026)

YouTube's current `playlistItems.list` docs explicitly report
`watchHistoryNotAccessible` and `watchLaterNotAccessible`. OAuth does not unlock
these lists. `subscriptions.list?part=snippet&mine=true&maxResults=50` supports
pagination; each request costs one quota unit. Its `snippet.publishedAt` is the
subscription creation time, not a video's publication time.

Takeout is the supported history backfill. Select **YouTube and YouTube Music**,
include history, playlists and subscriptions, choose JSON for history when
available, download and extract every archive part into one folder. Google does
not publish a stable, exhaustive localized file schema in the Takeout help;
parsers here use invented representative exports, not a claim that every export
variant has been verified. Prefer English export settings and JSON history.

yt-dlp documents `:ythistory`, `:ytwatchlater`, `--flat-playlist`,
`--dump-single-json`, and `--cookies-from-browser`. Its current YouTube tab
extractor does not supply per-view watch timestamps. The `timestamp` it may
return describes video publication (possibly approximate), **not watching**.
The plugin ignores it and all upload/release dates. Flat Watch Later entries
also lack a reliable added time. These become `time_precision=first_seen`.
The initial yt-dlp history snapshot is a silent baseline, including when enabled
after Takeout. Only unseen video IDs in later snapshots emit first-seen observations.
A baselined video is never emitted later, whatever changes in its feed entry (title,
thumbnail, counts). A cursor that already held yt-dlp history observations before
the baseline marker existed is treated as baselined and keeps emitting new IDs.
Their happened_at is an observation clock, not a known watch time. An old video
newly exposed by a truncated or reordered feed can still look new. Watch Later
position is null: sibling inserts/removals must not version unchanged saves.
Untouched source payloads are retained.
History feeds can coalesce repeated videos and omit earlier activity: this route
is one first-seen observation per video, not a complete per-view event log.
Only Takeout supplies individual historical views, including repeated views.

## Run

From the repository root:

```sh
uv sync --all-packages
export THROUGHLINE_URL='http://127.0.0.1:8791'
export THROUGHLINE_TOKEN='<plugin token scoped to youtube>'
export YOUTUBE_WATCHER='/path/to/youtube-watcher'
uv run throughline-youtube manifest
uv run throughline-youtube backfill --dry-run
uv run throughline-youtube backfill
uv run throughline-youtube sync --once
```

`sync` without `--once` polls hourly. Backfill and sync use the same generator,
settings and normalization; enabled live routes are also consulted on backfill.
No enabled input is a valid empty run. No model keys are needed.

## OAuth subscriptions

1. In Google Cloud Console create/select a project and enable **YouTube Data
   API v3** in APIs & Services → Library.
2. Configure Google Auth Platform branding, audience/consent, and add the owner
   as a test user when using External/Testing. Request exactly
   `https://www.googleapis.com/auth/youtube.readonly` (View your YouTube account).
3. Create an OAuth client of type **Desktop app** in Clients/Credentials. Keep
   its client ID and client secret outside the repository. Choose the correct
   YouTube/Brand Account during consent.
4. Run the loopback PKCE helper (`throughline_youtube.auth`) to save a refresh
   token to a private 0600 file and set `YOUTUBE_API=true`.

## Optional cookie route

Install and maintain yt-dlp separately on the machine running the plugin. It is
never bundled or installed as a dependency. Sign into the owner's own YouTube
account in a browser on that machine, then explicitly enable `YOUTUBE_YTDLP=true`
and `YOUTUBE_BROWSER=<selector>`.

## Messages, entities and limitations

All channels use one `owner` thread. Owner actions have no author. The plugin
does not infer duration. Source offsets remain in offset ISO timestamps.
Raw JSON/API/yt-dlp objects and CSV header/value mappings are retained unchanged;
HTML payloads retain the exact source content-cell fragment.

| Channel | Fields |
|---|---|
| `youtube.history` | `video_id`, `video_title`, `youtube_channel_id`, `channel_title`, `url`, `time_precision`, `source`, optional `is_short`, `is_music`, `is_ad`, and watcher session/day fields |
| `youtube.watch_later` | `video_id`, `title`, `channel_title`, `url`, `position` (null; rank is not an event property), `playlist` (`WL` for Watch Later), `time_precision`, `source`, `removed` |
| `youtube.subscriptions` | `youtube_channel_id`, `channel_title`, `url`, `time_precision`, `source`, `removed` |

**Core compatibility:** the requested source field `channel_id` is exposed as
`youtube_channel_id`, and Watch Later's requested `channel` as `channel_title`.
`src/throughline/migrations/0002_channel_views.sql` selects `m.*` and adds typed
fields: declaring `channel_id` causes duplicate-column SQLSTATE 42701 (HTTP 400)
because `api.messages` already has core `channel_id`. The validator in
`src/throughline/models.py` allows that name but explicitly rejects `channel`
(HTTP 422).

`time_precision=source` means the source supplied the action timestamp;
`day` and `estimated` come from watcher history days and sessions;
`first_seen` means happened_at holds the observation clock, not a known action time.
For repeatable unknown-time imports **retain the plugin cursor**.

Work entities have `kind=work`, `external_ref=youtube:<video_id>`; publisher
entities have `kind=organization`, `external_ref=youtube:<channel_id>`. Source
links are view → video `consumed`, video → publisher `published_by`, saved
item → video `saved`, and subscription → channel `subscribed_to`, all with
`method=source`. Repeated views share one work.

## Settings and localization

All settings are optional strings; booleans are enabled only by `true`.

| Environment variable | Default / meaning |
|---|---|
| `YOUTUBE_TAKEOUT` | Unset; recursively read extracted directory |
| `YOUTUBE_WATCHER` | Unset; history watcher export file or directory of exports (`fetch.py poll`) |
| `YOUTUBE_API` | `false`; OAuth subscription snapshots |
| `YOUTUBE_YTDLP` | `false`; cookie route for history and Watch Later |
| `YOUTUBE_COMMAND` | `["yt-dlp"]`; argv prefix JSON |
| `YOUTUBE_HISTORY_NAMES` | `["watch-history.json","watch-history.html"]` |
| `YOUTUBE_WATCH_LATER_NAMES` | `["Watch later","Watch Later","WL"]`; playlist stems, optional `-videos` suffix removed |
| `YOUTUBE_PLAYLIST_DIRS` | `["playlists"]`; immediate CSV directory names |
| `YOUTUBE_SUBSCRIPTION_DIRS` | `["subscriptions"]`; immediate CSV directory names |
| `YOUTUBE_CSV_HEADERS` | `{}`; additional translated header → canonical column mappings |
| `YOUTUBE_DATE_FORMAT` | Unset; explicit `strptime` format for localized dates, must include `%z` |
| `YOUTUBE_RECORDING` | Unset; invented offline fixture bundle; disables live API, normally replaces yt-dlp |

Secrets declared in the manifest: `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`,
`YOUTUBE_REFRESH_TOKEN`, `YOUTUBE_BROWSER`. Also supply SDK variables
`THROUGHLINE_URL` and `THROUGHLINE_TOKEN`. No secrets belong in source files.

## Tests

```sh
uv run --no-sync pytest plugins/youtube/tests -q
```
