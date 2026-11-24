# Throughline health

One general health source for Throughline: the **Oura ring** (Oura API v2) and the
**Weight Gurus** smart scale (Greater Goods, unofficial API). It captures full
history and then keeps up incrementally.

Two layers, as in every connector of this generation:

- `src/throughline_health/fetch.py` is a standalone fetcher that needs nothing but
  the Python 3.12+ standard library. It reads the services with credentials from
  environment variables and writes raw records as JSONL. Copy it to whatever host
  holds the credentials and run it there.
- The plugin (`throughline_health`, SDK based) maps raw records to Throughline
  messages. It either fetches live in process (it imports the same `fetch.py`) or
  reads export files written by `fetch.py`. Both paths use the same mapping, so a
  live run and an import of the same data give identical messages.

No model calls are made, paid or otherwise. No data is written to Oura or Weight
Gurus; the only write is the optional Oura token file update described under
"Oura authentication", and only when you opt in.

## What it captures

| Channel | One message per | Record time (`happened_at`) | Subject time |
| --- | --- | --- | --- |
| `health.oura.daily` | Oura day | 00:00 of the day, in the offset Oura reports | the day (day precision) |
| `health.oura.sleep` | sleep period (night, nap, rest) | bedtime start, `ended_at` bedtime end | (same) |
| `health.oura.session` | breathing/meditation/rest session | session start, `ended_at` session end | (same) |
| `health.oura.workout` | workout | workout start, `ended_at` workout end | (same) |
| `health.oura.heartrate` | UTC day of heart rate samples | first sample, `ended_at` last sample | the UTC day (day precision) |
| `health.weight` | Weight Gurus weigh-in | the weigh-in time (`entryTimestamp`) | (same) |

Every message has a short readable `content` line, for example
`Oura 2024-03-30: sleep 71, readiness 66, activity 81, slept 7h 01m, 7,000 steps, ...`,
`Slept 7h 00m (long sleep, 23:10-06:40): deep 1h 20m, REM 1h 45m, ...` or
`Weigh-in: 180.4 lb, body fat 24.3%, muscle 38.9%, water 55.4%, BMI 28.1`.
All metrics are typed fields (`provider`, `mapping_revision` and `day` on every
Oura channel, then for example `sleep_score`, `readiness_score`,
`temperature_deviation`, `steps`, `spo2_average`, `resilience_level`, `vo2_max`,
the score contributors as `*_contrib_*`, `main_sleep_total_s`; sleep stages,
HRV and heart rate per sleep period; `bpm_min/max/mean` and per-source means for
heart rate; `weight`, `weight_unit`, `weight_kg`, `bmi`, `body_fat_pct`,
`muscle_mass_pct`, `water_pct`, `deleted` for weigh-ins). Run
`throughline-health manifest` for the full declared list with descriptions.
Missing values are omitted, never invented. The unchanged source documents are
the message payload: the daily message holds each daily document under its
collection name, a heart rate day holds all its samples, a weigh-in holds every
Weight Gurus operation for it.

The daily summary merges `daily_sleep`, `daily_readiness`, `daily_activity`,
`daily_stress`, `daily_spo2`, `daily_resilience`, `daily_cardiovascular_age` and
`vO2_max`, plus the day's main sleep period (the `long_sleep`, else the longest).
Heart rate samples (every few minutes) are summarised per UTC day rather than
stored one message per sample.

A day subject is sent as 00:00 UTC of that calendar day with day precision and
no end, which is how the core reads a day bound, so the record falls on exactly
that day whatever the local offset.

Threads are one per channel and month (`Oura daily 2024-03`, `Weigh-ins 2024-03`).
Messages carry no author: all of it is the owner's own data. One installation
reads one Oura account and one Weight Gurus account.

Weight Gurus stores weight, BMI and the percentages as integers times ten; the
plugin divides by ten. A body composition value of 0 means "not measured" and is
omitted. The weight unit is the operation's own `unit` when present, else
`WEIGHTGURUS_UNIT` (default `lb`). A weigh-in deleted in Weight Gurus gets a new
version with `deleted=true`; it is never removed.

## Oura authentication

Oura access tokens come from an OAuth app whose refresh token rotates on every
refresh. If another program owns that refresh token, a second program refreshing
it breaks the first. So by default **this plugin never refreshes**:

- `OURA_TOKEN_FILE`: a file with the current access token, either plain text or
  JSON with `access_token` (other keys are ignored). It is read at run time and
  re-read after a 401, so a rotation done by the owning app is picked up. It takes
  precedence over `OURA_ACCESS_TOKEN`.
- `OURA_ACCESS_TOKEN`: an access token (or a personal access token) in the
  environment.

A 401 that a re-read file cannot fix stops the run with a clear error and does
not advance the cursor. Only with `OURA_ALLOW_REFRESH=1` **and this
installation's own** `OURA_CLIENT_ID` / `OURA_CLIENT_SECRET` does the plugin
refresh: it takes the refresh token from the token file (or `OURA_REFRESH_TOKEN`),
posts to `OURA_TOKEN_URL`, and writes the new pair back to the token file
atomically (mode 600, other keys kept) under a `<file>.lock` lock file. Without a
token file the refreshed pair lives in memory only. Never set
`OURA_ALLOW_REFRESH=1` on a token another app refreshes.

Weight Gurus has no token API: the plugin logs in with email and password on each
fetch and keeps the access token in memory only.

## Environment

Credentials (declared as secrets; never printed, logged or put on a command line):

| Variable | Meaning |
| --- | --- |
| `OURA_ACCESS_TOKEN` | Oura access token or personal access token |
| `OURA_REFRESH_TOKEN` | Oura refresh token; used only with `OURA_ALLOW_REFRESH=1` |
| `OURA_CLIENT_ID` | This installation's own Oura OAuth client ID (refresh only) |
| `OURA_CLIENT_SECRET` | This installation's own Oura OAuth client secret (refresh only) |
| `WEIGHTGURUS_EMAIL` | Weight Gurus account email |
| `WEIGHTGURUS_PASSWORD` | Weight Gurus account password |

Settings:

| Variable | Default | Meaning |
| --- | --- | --- |
| `HEALTH_SOURCE` | `live` | `live`, or `export:<file or directory>` of `fetch.py` JSONL |
| `HEALTH_PROVIDERS` | `auto` | `auto` (every service with credentials) or a comma list of `oura,weightgurus` |
| `HEALTH_OURA_COLLECTIONS` | all | Oura collections: `daily_sleep, daily_readiness, daily_activity, daily_stress, daily_spo2, daily_resilience, daily_cardiovascular_age, vO2_max, sleep, session, workout, heartrate` |
| `HEALTH_BACKFILL_START` | `2015-01-01` | First day for backfill, and for sync on an empty cursor |
| `HEALTH_BACKFILL_END` | empty (today) | Last day for backfill |
| `HEALTH_SYNC_OVERLAP_DAYS` | `3` | Days re-read before the last synced day, for late Oura updates |
| `HEALTH_WINDOW_DAYS` | `60` | Days per Oura fetch window; one checkpoint per window |
| `HEALTH_MAX_REQUESTS_PER_RUN` | `1500` | Source HTTP requests per run (every retry counts) |
| `HEALTH_MAX_REQUESTS_PER_DAY` | `6000` | Source HTTP requests per local day, tracked in the cursor |
| `HEALTH_MIN_INTERVAL_S` | `0.2` | Minimum seconds between source requests |
| `HEALTH_RETRIES` | `5` | Retries on 429, 5xx and transport errors |
| `HEALTH_BACKOFF_S` | `1` | First retry delay; doubles per retry, capped at 60 s; `Retry-After` is honoured |
| `HEALTH_WEIGHTGURUS_EVERY_H` | `6` | Minimum hours between Weight Gurus logins during sync |
| `WEIGHTGURUS_UNIT` | `lb` | Unit of Weight Gurus weights when an operation names none (`lb` or `kg`) |
| `OURA_TOKEN_FILE` | empty | Token file, see above |
| `OURA_ALLOW_REFRESH` | `0` | `1` allows refreshing with this installation's own client |
| `OURA_TOKEN_URL` | `https://api.ouraring.com/oauth/token` | OAuth token endpoint for refresh |
| `OURA_API_BASE` | `https://api.ouraring.com` | Oura API base URL |
| `WEIGHTGURUS_API_BASE` | `https://api.weightgurus.com` | Weight Gurus API base URL |

Base URLs must be HTTPS (plain HTTP is accepted only for a loopback address, for
local fakes and tunnels). Redirects are refused so a bearer token never follows
one. `THROUGHLINE_URL` and `THROUGHLINE_TOKEN` point the plugin at the core as for
every plugin.

## Running live

```sh
uv sync --all-packages
export THROUGHLINE_URL=http://localhost:8000 THROUGHLINE_TOKEN='<plugin token for health>'
export OURA_TOKEN_FILE=/path/to/oura-token.json         # or OURA_ACCESS_TOKEN=...
export WEIGHTGURUS_EMAIL='<email>' WEIGHTGURUS_PASSWORD='<password>'
uv run --package throughline-health throughline-health manifest
uv run --package throughline-health throughline-health backfill --dry-run
uv run --package throughline-health throughline-health backfill
uv run --package throughline-health throughline-health sync     # hourly
```

**Backfill** walks Oura from `HEALTH_BACKFILL_START` to `HEALTH_BACKFILL_END`
(or today) in windows and checkpoints after each one, so an interrupted backfill
resumes at the next window; changing the bounds starts the walk again. It always
reads the Weight Gurus history. **Sync** continues from the newest day already
read minus `HEALTH_SYNC_OVERLAP_DAYS` up to today (an empty cursor starts at
`HEALTH_BACKFILL_START`), and logs in to Weight Gurus at most every
`HEALTH_WEIGHTGURUS_EVERY_H` hours. Oura requests ask for one day past each
window and drop documents outside it, so windows are complete whichever way the
API reads `end_date`; heart rate is read in chunks of at most 30 days.

## Export workflow (credentials on another host)

On the host that holds the credentials, with only Python 3.12+:

```sh
export OURA_TOKEN_FILE=/path/to/oura-token.json  # or OURA_ACCESS_TOKEN
export WEIGHTGURUS_EMAIL='<email>' WEIGHTGURUS_PASSWORD='<password>'
python fetch.py check                                   # one cheap call per service
python fetch.py export --out exports/ --start 2015-01-01   # full history once
python fetch.py export --out exports/ --state fetch-state.json   # then on a schedule
```

`--out` takes a file or a directory (a timestamped `health-*.jsonl` is created).
A directory export never replaces an earlier file. The file is written as `<name>.partial` and renamed when finished; it is created
with mode 600. With `--state`, the next export starts at the day after the last
complete window minus `--overlap-days` (default 3), and the per-day request limit
(`--max-requests-per-day`) is enforced. Other options: `--sources`, `--end`,
`--collections`, `--window-days`, `--max-requests` (per run), `--min-interval`,
`--retries`, `--backoff`; their defaults come from the `HEALTH_*` variables above.
Exit status is 0 when complete, 3 when a request limit stopped it (everything
written is complete and the next run continues), 1 on an error.

Copy the files back and import them:

```sh
export HEALTH_SOURCE=export:/path/to/exports    # a file or a directory
uv run --package throughline-health throughline-health backfill
```

A directory is read recursively (`*.jsonl` and `*.jsonl.gz`), records from all
files are merged (the newest copy of each record wins, and for each Oura
collection and day only the newest fetch is used), mapped, and sent in groups of
one channel and month. The cursor keeps a digest per group, so re-importing the
same files sends nothing and a new file only re-sends the months it changes. A
crash resumes at the first unacknowledged group.

### Export file format

One JSON object per line:

```json
{"kind": "oura.daily_sleep", "id": "<native id>", "provider": "oura", "fetched_at": "2024-01-01T00:00:00Z", "payload": {"...": "unchanged API document"}}
```

Kinds: `oura.<collection>` for every Oura collection (`id` is Oura's document id,
or `<collection>:<day>` when it has none), `oura.heartrate` (one per UTC day,
`id` is the day, payload `{"day", "items": [samples]}`), `weightgurus.operation`
(`id` is `entryTimestamp|operationType|serverTimestamp`), `export.window` (a
completed fetch window, with collections skipped after a 403/404) and
`export.end` (summary: `format`, `complete`, `stopped`, `oura_next`, `requests`,
`counts`). Records are only written for complete windows. The personal profile
endpoint is called only by `check`, and its answer is discarded.

## Limits and politeness

- Oura documents a limit of 5000 requests per 5 minutes; the defaults stay far
  below it (0.2 s between requests, 1500 per run, 6000 per day). A full history of
  ten years at the default window is roughly 60 windows x 11 collections plus
  about 120 heart rate chunks.
- Weight Gurus is unofficial, so it is touched at most every 6 hours during sync:
  one login and one request for the whole history.
- 429, 5xx and transport errors are retried with exponential backoff and
  `Retry-After`; other errors fail the run without advancing the cursor. When a
  request limit is reached the run ends after the last complete window, logs a
  warning, and the next run continues from there.
- A Weight Gurus failure (bad login, outage) does not block Oura: Oura is still
  fetched and checkpointed, then the run (or `fetch.py export`, exit 1) reports
  the Weight Gurus error.
- Oura pagination that repeats a page token fails the window rather than
  silently dropping its remaining pages.
- A collection refused with 403 or 404 (for example a scope the token lacks) is
  skipped for the rest of the run and named in the `export.window` record.

## Mapping revision and idempotence

`mapping_revision` (currently 1) is a field on every message. Re-running on the
same input produces identical messages, so the core stores no new versions.
Raising `MAPPING_REVISION` in `mapping.py` invalidates the export cursor's group
digests, so the next import re-sends every message in the new shape.

Known limits: a sleep period, session or workout that Oura deletes or regenerates
under a new id after it was imported stays in Throughline (there is no removal
signal in the API); a daily summary is always rebuilt from the newest fetch of
its day. Heart rate days are UTC days. The Weight Gurus API is unofficial and its
field set is not documented; unknown fields stay in the payload.

## Tests

```sh
uv run --no-sync pytest -q plugins/health
uv run --no-sync ruff check plugins/health
```

All tests are offline: invented data, a fake Oura and Weight Gurus server bound to
127.0.0.1, and the SDK contract kit (in export mode and in live mode against the
fake server) with disposable local PostgreSQL databases. `fetch.py` is also run
as a plain script with `python -I -S` to prove it needs only the standard library.
