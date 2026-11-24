# Throughline Apple Health (`apple_health`)

Live Apple Health data for Throughline through the
[Health Auto Export](https://www.healthyapps.dev) iOS app. The app's **REST
API automation** posts your Health data as JSON on a schedule; a small
receiver shipped with this package spools each post, and the plugin turns the
samples and workouts into messages shaped like the `health` (Oura / smart
scale) connector's.

There is no export-zip step: once the automation is set up, data keeps
flowing. Apple offers no server API for Health, so an app on the phone has to
push it; Health Auto Export is a paid third-party app (its automations need
the premium tier).

## What it captures

| Channel | One message per | `happened_at` |
| --- | --- | --- |
| `apple_health.metrics` | metric sample (steps, active energy, distance, flights, HRV, resting heart rate, SpO2, respiratory rate, blood pressure, VO2 max, ...) | sample time, `ended_at` when the app gives an end |
| `apple_health.heart_rate` | heart rate sample (min/avg/max per export interval) | sample time |
| `apple_health.sleep` | night (aggregated export) or stage segment (unaggregated export) | sleep start, `ended_at` sleep end |
| `apple_health.weight` | body weight sample | sample time |
| `apple_health.workouts` | workout | workout start, `ended_at` workout end, located at the route start |

As in the `health` connector, every message has a short readable `content`
line (`Step count: 1,430 count [iPhone]`, `Heart rate 61-131 bpm, mean 78`,
`Slept 7h 15m (23:10-06:40): deep 1h 15m, REM 2h 00m, core 4h 00m, awake 18m`,
`Weigh-in: 172.4 lb`, `Workout: Outdoor Run, 32m, 341 kcal, 5.02 km, avg HR 152`),
typed fields, threads per channel (and metric) and month
(`Step count 2024-03`, `Sleep 2024-03`, `Workouts 2024-03`), no author (it is
all the owner's own data), and the unchanged source object as the payload.

Fields on every channel: `provider` (`health_auto_export`),
`mapping_revision`, `day` (the calendar day in the offset the app reported),
`source` (the Health source: device or app). Then:

- metrics: `metric`, `value`, `units`, `value_min`, `value_max`, `systolic`, `diastolic`
- heart rate: `bpm_min`, `bpm_max`, `bpm_mean`, `units`
- sleep: `aggregated`, `stage`, `total_sleep_s`, `asleep_s`, `core_sleep_s`,
  `deep_sleep_s`, `rem_sleep_s`, `awake_s`, `time_in_bed_s`, `duration_s`,
  `sleep_start`, `sleep_end`, `in_bed_start`, `in_bed_end`
- weight: `weight`, `weight_unit`, `weight_kg`
- workouts: `activity`, `workout_id`, `duration_s`, `calories` (kcal; kJ is
  converted), `distance_m`, `heart_rate_mean`, `heart_rate_max`,
  `heart_rate_min`, `elevation_gain_m`, `steps`, `environment`
  (Indoor/Outdoor), `temperature`, `humidity`, `route_points`

Missing values are omitted, never invented.

### Deduplication and revisions

A sample's id is `metric + UTC timestamp + source` (sleep segments add the
stage; workouts use the app's workout id, else activity + start). Health Auto
Export re-sends overlapping ranges, so the same sample arrives many times;
repeats are dropped within a run and are `unchanged` in the core across runs.
A sample whose value changed (for example today's step total with daily
aggregation, which grows all day) becomes a new version of the same record,
so the latest value is current and the history is kept.

### Workout routes

A workout's GPS route is not kept in the payload (it can be thousands of
points); `route_points` counts it and the message is located at its first
point. With `APPLE_HEALTH_ROUTE_TRACK=1` the route points are also sent as
track points (source `apple_health`), which places other life events during
the workout. That needs a token with the location-source capability (see
below); without it the run fails with 403 and the cursor does not move.

## Setup

### 1. Run the receiver

```sh
export APPLE_HEALTH_SPOOL_DIR=/var/lib/throughline/health-spool
export APPLE_HEALTH_RECEIVER_SECRET='<long random string>'
uv run --package throughline-apple-health throughline-apple-health-receiver --listen 127.0.0.1:8766
```

It authenticates with `Authorization: Bearer <secret>` (or HTTP Basic with
the secret as the password; or `?token=<secret>` only with
`APPLE_HEALTH_ALLOW_QUERY_TOKEN=1`, which risks the secret in proxy logs),
validates that the body is a Health Auto Export document
(`{"data": {"metrics": [...], "workouts": [...]}}`), and appends it as one line
to `health-YYYY-MM-DD.jsonl` in the spool. `GET /healthz` answers `ok`.
Bodies over `APPLE_HEALTH_MAX_BODY_BYTES` (default 100 MiB) are refused.

Put it behind a TLS reverse proxy and keep it listening on loopback:

```
health.example.com {
    reverse_proxy 127.0.0.1:8766
}
```

(nginx: `proxy_pass http://127.0.0.1:8766; client_max_body_size 100m;`.)
Run it as a long-lived service (systemd `Restart=always`).

### 2. Configure Health Auto Export

Automations → new **REST API** automation:

- URL: `https://health.example.com/`
- Headers: `Authorization` = `Bearer <secret>`
- Export format **JSON**, version 2 if offered; data type Health Metrics
  and/or Workouts (one automation each is fine), select the metrics you want
- Date range "since last sync" (or "today"; overlaps are harmless)
- Aggregation **Hours** is a good default. "None" exports every raw sample
  (heart rate is then one message every few minutes); "Days" gives one
  message per day that is revised all day long.
- Sleep: "aggregate sleep data" gives one message per night; unaggregated
  gives one message per stage segment.
- Include workout routes if you want `route_points` / track points.
- Sync cadence: as often as you like (the app runs it in the background when
  iOS lets it).

### 3. Run the plugin

```sh
throughline token create apple-health --kind plugin --plugin apple_health --json
# add --location-source when APPLE_HEALTH_ROUTE_TRACK=1
export THROUGHLINE_URL=http://localhost:8000 THROUGHLINE_TOKEN='<plugin token>'
export APPLE_HEALTH_SPOOL_DIR=/var/lib/throughline/health-spool
uv run --package throughline-apple-health throughline-apple-health backfill --dry-run
uv run --package throughline-apple-health throughline-apple-health backfill
uv run --package throughline-apple-health throughline-apple-health sync --once   # hourly timer
```

The plugin reads the spool from a byte offset per file kept in its cursor and
checkpoints every `APPLE_HEALTH_BATCH_LINES` documents, so a crash resumes at
the last checkpoint and nothing is read twice in steady state. It never
modifies the spool; delete old files after they have been read. To import
history, run a one-off manual export from the app to the same URL with a
wide date range.

## Environment

| Variable | Default | Meaning |
| --- | --- | --- |
| `APPLE_HEALTH_SPOOL_DIR` | – (required) | Spool directory (receiver and plugin) |
| `APPLE_HEALTH_RECEIVER_SECRET` | – | Shared secret the receiver requires (receiver only) |
| `APPLE_HEALTH_LISTEN` | `127.0.0.1:8766` | Receiver listen address |
| `APPLE_HEALTH_ALLOW_QUERY_TOKEN` | `0` | `1` accepts `?token=<secret>` |
| `APPLE_HEALTH_MAX_BODY_BYTES` | `104857600` | Largest accepted body |
| `APPLE_HEALTH_METRICS` | empty (all) | Comma list of metric names to keep |
| `APPLE_HEALTH_EXCLUDE_METRICS` | empty | Comma list of metric names to drop |
| `APPLE_HEALTH_ROUTE_TRACK` | `0` | `1` sends workout routes as track points |
| `APPLE_HEALTH_MAX_ACCURACY_M` | `1000` | Route points less accurate than this are dropped |
| `APPLE_HEALTH_BATCH_LINES` | `20` | Spool documents per checkpoint |

## Limitations

- Only what Health Auto Export exports: its metric and workout sections. Other
  sections (State of Mind, ECG, symptoms, medications, cycle tracking, heart
  rate notifications) are kept in the spool but not mapped.
- iOS decides when background automations run; when the phone is locked or in
  Low Power Mode data arrives late (but with its original timestamps).
- Values are as the app aggregates them; the plugin does not re-aggregate
  across posts. Day-aggregated metrics are revised during the day.
- Health Auto Export's field names have changed between app versions; the
  mapping reads both the older (`activeEnergy`, `avgHeartRate`, `lat`/`lon`
  route) and newer (`activeEnergyBurned`, `heartRate.avg`,
  `latitude`/`longitude` route) workout shapes. The shapes were taken from the
  app's documentation and published samples, not from a live account.
- Body composition other than weight (body fat, BMI, lean mass) lands in
  `apple_health.metrics`, one message per metric sample.
