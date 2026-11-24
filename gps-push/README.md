# Throughline GPS push (`gps_push`)

A live location source for phone GPS tracker apps. It gives Throughline the
owner's location track (so every other life event can be placed where it
happened) plus region and visit events.

Supported apps:

| App | Platform | How it delivers |
| --- | --- | --- |
| [OwnTracks](https://owntracks.org) (HTTP mode) | iOS, Android | push to the receiver |
| [Overland](https://overland.p3k.app) | iOS | push to the receiver |
| [GPSLogger for Android](https://gpslogger.app) (Custom URL) | Android | push to the receiver |
| [OwnTracks Recorder](https://github.com/owntracks/recorder) | server | the plugin polls its HTTP API |

## What it captures

| Output | One item per | Notes |
| --- | --- | --- |
| track points (source `gps_push`) | location fix | OwnTracks `location`, Overland location features, GPSLogger requests, Recorder history |
| `gps_push.transitions` | OwnTracks region enter/leave, Overland visit | life events, located at the reported position |
| `gps_push.regions` (reference) | OwnTracks region (waypoint) definition | geofence name, centre and radius |

Fields:

- `gps_push.transitions`: `event` (`enter`, `leave`, `visit`), `region`,
  `region_id`, `trigger`, `accuracy_m`, `arrived_at`, `departed_at` (absent
  while an Overland visit is still open; it gets a new version when the app
  reports the departure), `device`, `app`, `mapping_revision`.
- `gps_push.regions`: `region`, `region_id`, `radius_m`, `center_lat`,
  `center_lon`, `device`, `app`, `mapping_revision`.

The unchanged source object is each message's payload. Messages carry no
author: everything is the owner's own device data. `device` is the OwnTracks
`user/device` (from the `X-Limit-U`/`X-Limit-D` headers, the Basic-auth user
name or the `topic`), the Overland `device_id`, or the GPSLogger `device`
parameter.

Fixes with no timestamp, impossible coordinates, `0,0`, or an accuracy worse
than `GPS_PUSH_MAX_ACCURACY_M` are dropped. OwnTracks `lwt`, `card`,
`status`, `cmd` and `steps` messages are accepted and ignored.

## How push mode works

The SDK has no webhook runtime, so this package ships a tiny standard-library
receiver, `throughline-gps-push-receiver`. It authenticates every request with
one shared secret, checks the body has the shape of one of the three formats,
and appends it as one JSON line to `GPS_PUSH_SPOOL_DIR/gps-YYYY-MM-DD.jsonl`.
The plugin (`throughline-gps-push sync`) reads the spool from a byte offset
kept in its cursor, so the receiver keeps accepting points while Throughline is
down, and the plugin catches up from where it stopped. Spool files are never
modified by the plugin; delete old ones whenever you like (after the plugin
has read them).

```
phone ──HTTPS──▶ reverse proxy ──HTTP──▶ receiver (127.0.0.1:8765) ──▶ spool/*.jsonl ◀── plugin (timer) ──▶ Throughline
```

### Receiver endpoints

| Path | Method | Format | Reply |
| --- | --- | --- | --- |
| `/owntracks` | POST | OwnTracks JSON (`_type`) | `[]` |
| `/overland` | POST | `{"locations": [GeoJSON Feature, ...]}` | `{"result":"ok"}` (Overland deletes its queue only on this) |
| `/gpslogger` | GET or POST | query string, form body or JSON body | `OK` |
| `/` | POST | any of the above, detected from the body | as above |
| `/healthz` | GET | none (unauthenticated) | `ok` |

Authentication: `Authorization: Bearer <secret>`, or HTTP Basic auth with any
user name and the secret as the password (OwnTracks: put the secret in the
app's password field; the user name becomes the device's user). Apps that
can only put credentials in the URL can use `?token=<secret>` when
`GPS_PUSH_ALLOW_QUERY_TOKEN=1`; the token is stripped before spooling, but
query strings may end up in proxy access logs, so prefer headers.

### Running the receiver

```sh
export GPS_PUSH_SPOOL_DIR=/var/lib/throughline/gps-spool
export GPS_PUSH_RECEIVER_SECRET='<long random string>'
uv run --package throughline-gps-push throughline-gps-push-receiver --listen 127.0.0.1:8765
```

Put it behind a TLS reverse proxy; phones should only ever talk HTTPS. Caddy:

```
gps.example.com {
    reverse_proxy 127.0.0.1:8765
}
```

nginx:

```nginx
location / {
    proxy_pass http://127.0.0.1:8765;
    client_max_body_size 5m;
}
```

Run the receiver as a long-lived service (systemd `Restart=always`) and the
plugin on a timer.

### App setup

- **OwnTracks**: Settings → Connection → Mode `HTTP`, URL
  `https://gps.example.com/owntracks`, Identification: user name of your
  choice, password = the secret, Device ID e.g. `phone`. Regions you define in
  the app are sent as waypoints when you publish them (Regions → publish).
- **Overland**: Settings → Receiver Endpoint
  `https://gps.example.com/overland`, Access Token = the secret (sent as a
  Bearer header), set a Device ID. Enable "Visits" to get visit events.
- **GPSLogger**: Logging details → Log to custom URL, URL
  `https://gps.example.com/gpslogger?lat=%LAT&lon=%LON&acc=%ACC&alt=%ALT&spd=%SPD&time=%TIME&batt=%BATT&device=%SER`,
  HTTP method GET (or POST with the same parameters in the body), and add an
  HTTP header `Authorization: Bearer <secret>` (or Basic auth with the secret
  as the password).

## Poll mode: OwnTracks Recorder

If you already run an OwnTracks Recorder, set `GPS_PUSH_RECORDER_URL`
instead of (or as well as) the spool. The plugin lists users and devices
(`/api/0/list`) and reads `/api/0/locations` in windows of
`GPS_PUSH_RECORDER_WINDOW_DAYS`, starting at `GPS_PUSH_RECORDER_SINCE` on an
empty cursor and afterwards at the newest point read minus
`GPS_PUSH_RECORDER_OVERLAP_H` (phones upload queued points late). It checkpoints
the newest point per device. Recorder mode yields track points only.

A Recorder (or a shared receiver) often holds other people's phones. Set
`GPS_PUSH_DEVICES` to the owner's devices so only those become the owner's
track.

## Environment

| Variable | Default | Meaning |
| --- | --- | --- |
| `GPS_PUSH_SPOOL_DIR` | – | Spool directory written by the receiver (plugin and receiver) |
| `GPS_PUSH_RECEIVER_SECRET` | – | Shared secret the receiver requires (receiver only; never give it to the plugin) |
| `GPS_PUSH_LISTEN` | `127.0.0.1:8765` | Receiver listen address |
| `GPS_PUSH_ALLOW_QUERY_TOKEN` | `0` | `1` accepts `?token=<secret>` |
| `GPS_PUSH_MAX_BODY_BYTES` | `5242880` | Largest accepted request body |
| `GPS_PUSH_RECORDER_URL` | – | OwnTracks Recorder base URL |
| `GPS_PUSH_RECORDER_USER` | – | Basic-auth user for the Recorder (if it is behind an authenticating proxy) |
| `GPS_PUSH_RECORDER_PASSWORD` | – | Basic-auth password (secret) |
| `GPS_PUSH_RECORDER_SINCE` | `2015-01-01` | First day read from the Recorder |
| `GPS_PUSH_RECORDER_WINDOW_DAYS` | `30` | Days per Recorder request |
| `GPS_PUSH_RECORDER_OVERLAP_H` | `24` | Hours re-read before the newest point |
| `GPS_PUSH_DEVICES` | empty (all) | Comma list of device keys to keep (`user/device` or the device id) |
| `GPS_PUSH_MAX_ACCURACY_M` | `1000` | Drop fixes less accurate than this |
| `GPS_PUSH_BATCH_LINES` | `500` | Spool lines per checkpoint |

At least one of `GPS_PUSH_SPOOL_DIR` and `GPS_PUSH_RECORDER_URL` is required.

## Running the plugin

Track points need a token with the location-source capability:

```sh
throughline token create gps-push --kind plugin --plugin gps_push --location-source --json
# or, for an existing token:
throughline token location-source <token id>
```

```sh
export THROUGHLINE_URL=http://localhost:8000 THROUGHLINE_TOKEN='<plugin token>'
export GPS_PUSH_SPOOL_DIR=/var/lib/throughline/gps-spool
uv run --package throughline-gps-push throughline-gps-push backfill --dry-run
uv run --package throughline-gps-push throughline-gps-push backfill
uv run --package throughline-gps-push throughline-gps-push sync --once   # every 5 minutes
```

Without `--location-source` the first track write is refused (403) and the
run fails without moving the cursor. After a large import, run
`POST /v1/track/detect-visits` (or the CLI equivalent) to rebuild visits.

## Limitations

- Push apps only send what they record; while the phone has no network the
  apps queue points and send them later (they arrive with their original
  timestamps). Overland and OwnTracks drop their queue only on a 200 reply, so
  a receiver outage loses nothing that the phone still holds.
- Track points are keyed by time per source: two devices reporting the exact
  same second keep the first point received.
- The Recorder API serves location history only; region transitions and
  waypoints come only through push mode.
- GPSLogger requests carry only the parameters you put in the URL template;
  without `%TIME`/`%TIMESTAMP` the receipt time is used.
- No friend/family features: the receiver answers OwnTracks with `[]`.
