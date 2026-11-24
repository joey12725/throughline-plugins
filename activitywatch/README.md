# Throughline ActivityWatch (`activitywatch`)

Desktop activity from [ActivityWatch](https://activitywatch.net) — which
apps and windows you used, which pages you read, which files you edited, and
when you were at the computer — read live from ActivityWatch's local REST
API and stored as **coalesced sessions**, not raw watcher events.

## What it captures

| Channel | Source bucket type | One message per |
| --- | --- | --- |
| `activitywatch.windows` | `currentwindow` (aw-watcher-window) | app + window-title session |
| `activitywatch.web` | `web.tab.current` (aw-watcher-web-chrome/-firefox/...) | url + title session |
| `activitywatch.editor` | `app.editor.activity` (aw-watcher-vscode and similar) | project + file + language session |
| `activitywatch.afk` | `afkstatus` (aw-watcher-afk) | active or away period |

Each message has `happened_at` (session start, UTC as ActivityWatch stores
it), `ended_at` (session end), a short `content` line
(`Invented Editor: notes.txt - Invented Editor (7m)`,
`Invented Docs (docs.example.org) (20m)`,
`Editing notes.txt in /home/me/project (2m 40s)`,
`Away from the computer for 22h 13m`) and fields:

- all: `host`, `bucket`, `duration_s` (first to last event), `active_s`
  (time actually covered by events), `event_count`, `mapping_revision`
- windows: `app`, `title`, `afk_filtered`
- web: `url`, `title`, `domain`, `browser`, `audible`, `afk_filtered`
- editor: `project`, `file`, `language`, `editor`, `afk_filtered`
- afk: `status` (`active` / `afk`)

Threads are one per host and UTC day. Messages carry no author (it is the
owner's own computer). The payload is the session's watcher data and the
list of `[start, end]` pieces it was built from.

### From events to sessions

Raw watcher events are very noisy (every title change and alt-tab is an
event). For each bucket the plugin:

1. clips events to the window being read;
2. for window, web and editor buckets, intersects them with the same host's
   not-AFK time (as ActivityWatch's own reports do), so time spent away with a
   window focused is not counted — hosts without an AFK watcher are kept
   unfiltered (`afk_filtered: false`);
3. drops pieces shorter than `ACTIVITYWATCH_MIN_EVENT_S` (blips);
4. merges consecutive pieces with the same key (app + title; url + title;
   project + file + language; AFK status) separated by at most
   `ACTIVITYWATCH_MERGE_GAP_S`;
5. drops app/web/editor sessions with less than
   `ACTIVITYWATCH_MIN_SESSION_S` of active time.

### In-progress events

The newest event of a bucket keeps growing while you work (watchers extend
it with heartbeats), and AFK detection is retroactive (it marks the last few
minutes as away once it notices). So a session is emitted only once it can
no longer change: it must end at least the merge gap before the bucket's
`last_updated` (and the AFK bucket's, for filtered buckets) and before now
minus `ACTIVITYWATCH_SETTLE_S`. Each bucket's cursor is the start of its
first session not yet emitted, so a held session is recomputed from scratch
on the next run and emitted exactly once, complete, under an id built from
its start (`<host>/<bucket>/<start>`). Nothing is re-versioned in normal
operation. When a computer is switched off mid-session, its last session is
emitted after the next activity on that machine.

## Setup

ActivityWatch runs a local server on `http://localhost:5600` (no
authentication; it only listens on localhost). Run the plugin on the same
machine, or point `ACTIVITYWATCH_URL` at a machine that syncs other hosts'
buckets (aw-sync). Every bucket's hostname is kept, so several computers can
feed one Throughline; with one plugin per machine, set `ACTIVITYWATCH_HOSTS`
to that machine's hostname to avoid reading synced copies twice.

```sh
throughline token create activitywatch --kind plugin --plugin activitywatch --json
export THROUGHLINE_URL=http://throughline.example.com:8000 THROUGHLINE_TOKEN='<plugin token>'
uv run --package throughline-activitywatch throughline-activitywatch backfill --dry-run
uv run --package throughline-activitywatch throughline-activitywatch backfill
uv run --package throughline-activitywatch throughline-activitywatch sync --once   # every 15 minutes
```

Backfill starts at each bucket's creation (or `ACTIVITYWATCH_SINCE`) and
reads `ACTIVITYWATCH_WINDOW_H` hours per request with a checkpoint after each
window. A session longer than the window (a night away) widens the window
until it fits.

## Environment

| Variable | Default | Meaning |
| --- | --- | --- |
| `ACTIVITYWATCH_URL` | `http://localhost:5600/api/0` | REST API base URL |
| `ACTIVITYWATCH_SINCE` | bucket creation | Earliest time imported on an empty cursor (ISO) |
| `ACTIVITYWATCH_WINDOW_H` | `24` | Hours per request / checkpoint |
| `ACTIVITYWATCH_MIN_EVENT_S` | `5` | Drop event pieces shorter than this |
| `ACTIVITYWATCH_MERGE_GAP_S` | `60` | Merge same-key pieces separated by at most this |
| `ACTIVITYWATCH_MIN_SESSION_S` | `30` | Drop app/web/editor sessions with less active time |
| `ACTIVITYWATCH_SETTLE_S` | `600` | Data newer than now minus this is not final yet |
| `ACTIVITYWATCH_HOSTS` | all | Comma list of hostnames to import |
| `ACTIVITYWATCH_EXCLUDE_APPS` | none | Comma list of app names never stored (case-insensitive) |
| `ACTIVITYWATCH_INCLUDE_INCOGNITO` | `0` | `1` also stores private/incognito tabs (dropped by default) |
| `ACTIVITYWATCH_FIXTURE` | – | Recorded JSON served instead of the API (tests only) |

Changing the coalescing settings changes how future sessions are cut; already
stored sessions are not rebuilt (reset the cursor and re-import to rebuild).

## Limitations

- Window titles and URLs can contain sensitive text (document names, mail
  subjects). Use `ACTIVITYWATCH_EXCLUDE_APPS`, and the watchers' own
  exclusion settings, before importing.
- Web sessions are intersected with AFK time but not with the browser being
  the focused window: a tab left open in a background browser window still
  counts while you are active elsewhere.
- Buckets of other types (input, media, custom watchers) are skipped.
- If a bucket fails to read, the others continue and that channel reports
  partial coverage for the bucket; if all fail, or the server is
  unreachable, the run fails.
- Events added later with old timestamps (e.g. a synced host that was
  offline) are only picked up if they are newer than that bucket's cursor.
- ActivityWatch stores times in UTC; session times and day threads are UTC.
