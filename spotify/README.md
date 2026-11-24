# Spotify for Throughline

Imports the owner's listening plays into `music.spotify`, with one message per
play, through the same `records` generator for backfill and sync. Supports tracks,
podcast episodes, extracted Extended streaming history `Streaming_History_Audio_*.json`
and short account-data `StreamingHistory*.json` files, recursively. Export imports
need no Spotify credentials. No audio is downloaded. The optional
`music.spotify_playlists` membership/history channel is intentionally omitted:
context resolution is small, but reliably tracking membership changes is a separate
feature and development-mode apps cannot read every playlist's items.

## Message shape

`happened_at` is the play start (or the best source-supported estimate), `ended_at`
is the export end or an observed transition estimate; it is null when unknown.
Content is `Listened to <track> by <artist>` or `Listened to <episode> from <show>`.
Threads are UTC calendar days, so chronological listening remains useful even
when the context is absent or changes. Input offsets are preserved in timestamps;
short exports' documented UTC times are assigned +00:00.

Typed fields (all nullable unless the source supplies them):

| Fields | Type and meaning |
| --- | --- |
| track_name, album_name | text; episode/show names for podcasts |
| artist_names | JSON ordered list of names |
| track_uri, album_uri | text; Spotify item and album URIs |
| ms_played, duration_ms | integer; actual exported listening time versus full item duration |
| context_type, context_uri | text; playlist/album/artist/show context and original URI |
| playlist_name, playlist_owner | text; resolved name and owner display name or ID |
| skipped, shuffle, offline | boolean; source flag, null when unknown |
| reason_start, reason_end | text; export reason codes, null for API |
| device, platform | JSON API device object; text export platform |
| source | text; api or export |
| timing_basis | text; api_played_at, observed_progress, export_elapsed, export_minute_precision |
| end_uncertainty_ms | integer; polling interval bounding a transition estimate, null when unknown |

The raw export object or API history/playback object is retained unchanged as
`payload`. When merged, `payload` wraps the unchanged objects as `history` and
`playback`. The owner needs no author identifier. Links use `method=source`:
play -> work `consumed`, play -> playlist `played_from`, work -> album `part_of`,
work -> artist `created_by`. URI-keyed work entities are shared across plays
(MED-1). A short export without a URI gets no invented work/artist identity.

## Settings and secrets

| Environment variable | Meaning |
| --- | --- |
| SPOTIFY_EXPORT_PATH | Optional extracted export folder |
| SPOTIFY_RECORDING | Test session JSON; when set, completely disables Spotify network access |
| SPOTIFY_TOKEN_FILE | Private JSON refresh-token file outside the repository; preferred for rotation |
| SPOTIFY_CLIENT_ID | Declared secret: developer application client ID |
| SPOTIFY_CLIENT_SECRET | Declared secret: developer application secret |
| SPOTIFY_REFRESH_TOKEN | Declared secret: initial refresh token, alternative to token file |
| SPOTIFY_COMPOSIO_MCP_URL | Declared secret: Composio MCP endpoint URL (alternative to native Spotify OAuth) |
| SPOTIFY_COMPOSIO_MCP_TOKEN | Declared secret: Composio MCP bearer token |
| SPOTIFY_COMPOSIO_ACCOUNT | Optional Composio Spotify connected account ID |
| THROUGHLINE_URL | Core API URL, normally ending in /v1/ |
| THROUGHLINE_TOKEN | Core token scoped to plugin spotify, or owner token |

If a token file is set it takes precedence over SPOTIFY_REFRESH_TOKEN. Newly
issued refresh tokens are atomically saved to that file; environment-only tokens
can rotate in memory but are not durable across restarts. Use a token file in
production. POSIX creation permissions are 0600; on Windows use a private directory
with an appropriate account ACL. Tokens never enter cursors, messages or logs.

For hosts where the authenticated Composio connection must remain remote,
`remote_snapshot.py` is a standalone standard-library fetcher (see
[the remote-fetch pattern](../README.md#remote-fetch-pattern)). Copy it to that
host, run it beside the connection, and pass its private output file through
`SPOTIFY_RECORDING`:

```sh
COMPOSIO_MCP_TOKEN=... python3 remote_snapshot.py --out snapshot.json \
    --expected-user <your Spotify user id>
# or read the token from a secret manager on that host:
python3 remote_snapshot.py --out snapshot.json --expected-user <id> \
    --token-command "pass show composio/mcp"
```

It uses the official Composio Connect endpoint by default (`--mcp-url`), refuses
to run unless the connection belongs to `--expected-user`, and returns only
playback, recently-played, and referenced playlist
metadata. The MCP credential is never serialized into the snapshot.

## Create an app and authorize

1. Sign in to the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard),
   create an application for personal listening history, and enable Web API.
   Read the app's Client ID and Client Secret from Settings. Add the owner's
   account to Users Management / the development-mode allowlist if needed.
2. Register **exactly** `http://127.0.0.1:8765/callback` as a redirect URI.
   This is a local loopback callback: current Spotify rules reject the literal
   hostname `localhost`. Run authorization on a machine with the owner's browser.
3. Set SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET and SPOTIFY_TOKEN_FILE in the
   environment using a secret manager or private local shell session. The token
   file's parent directory must already exist. Run:

   ```powershell
   uv sync --all-packages
   uv run throughline-spotify-auth
   ```

   The helper opens the consent page, listens only on 127.0.0.1:8765, validates a
   random state, and exchanges the code using HTTP Basic client authentication.
   It saves only the refresh token, never prints it, and times out after five minutes.
   It does not contact Throughline. Access tokens refresh automatically and one
   401 triggers a refresh/retry. `invalid_grant` clears the token file and requires
   rerunning this helper; it is never retried indefinitely.

Exact requested scopes:

- `user-read-recently-played`: GET /me/player/recently-played.
- `user-read-playback-state`: GET /me/player, including device and shuffle state.
- `playlist-read-private` and `playlist-read-collaborative`: access to permitted
  private/collaborative playlist metadata. They do not bypass playlist restrictions.

The plugin uses /me/player rather than /me/player/currently-playing, so it does
not request the latter endpoint's `user-read-currently-playing` scope. It requests
`additional_types=track,episode` and checks item types; unknown types are ignored.
No write, streaming, email, library or profile scopes are requested.

## Backfill and sync

Request **Extended streaming history** from the Spotify account privacy/download
page, extract its archive, and point SPOTIFY_EXPORT_PATH at the extracted folder.
The short account-data export is supported too, but has less information. Do not
put the owner's export or token file in this repository.

From the repository root, with the core running and its token provisioned:

```powershell
uv sync --all-packages
$env:THROUGHLINE_URL = 'http://127.0.0.1:8000/v1/'
$env:THROUGHLINE_TOKEN = '<core token for plugin spotify>'
$env:SPOTIFY_EXPORT_PATH = '<absolute extracted export directory>'
uv run throughline-spotify backfill --dry-run
uv run throughline-spotify backfill
```

For ongoing API sync, additionally set the credentials and token file described
above, then run:

```powershell
$env:SPOTIFY_CLIENT_ID = '<application client ID>'
$env:SPOTIFY_CLIENT_SECRET = '<application client secret>'
$env:SPOTIFY_TOKEN_FILE = '<absolute private token JSON path>'
uv run throughline-spotify-auth
uv run throughline-spotify sync --once
uv run throughline-spotify sync
```

Clear SPOTIFY_RECORDING for real imports. API-only operation can leave
SPOTIFY_EXPORT_PATH unset. Export-only operation should leave SPOTIFY_CLIENT_ID
unset. A backfill with both configured imports the export and available API
window. Both modes checkpoint only after prior messages and links are acknowledged;
dry-run uses the SDK's rollback/count behavior and writes no core cursors or links.
Credential refresh may still write the private local token file during API dry-runs.

The daemon polls playback and recently played every 15 seconds (plus request time).
History requests use limit=50, exclusive after timestamps, and a five-minute
replay overlap for delayed publication. Full pages are traversed backward using
exclusive before cursors without combining before and after. Only acknowledged
results advance the high-water mark. Repeated cursors terminate safely; a full
window logs a possible-gap warning. This cannot recover plays that have fallen
outside Spotify's retained window. No finite polling interval guarantees capture
under arbitrarily fast skipping, downtime, or quota exhaustion; exports repair
historical gaps. Check core plugin health for failed/stale polling.

Playlist names/owners, including null lookups, are cached in the durable cursor
for 24 hours. Private/deleted/algorithmic/inaccessible contexts keep their URI and
null metadata on 403/404. Other errors fail the poll without advancing its cursor.
HTTP transport errors, 429 and 5xx retry at most five attempts with exponential
backoff; a numeric Retry-After is a minimum wait, never shortened. Spotify does not
publish a universal requests-per-second allowance. Its rolling 30-second limits
and development-mode quota buckets can constrain this polling frequency.

## Timing and reconciliation limits

Recently played supplies played_at and context, but no actual end, skipped flag,
or listened milliseconds. Full duration is never substituted for ms_played.
Active observations are checkpointed and emitted when playback changes/disappears.
History matches a same-URI observed interval with five seconds of boundary
tolerance, across polls. A row that fits two back-to-back plays of one track keeps
a claim it already holds, otherwise goes to the oldest unclaimed play; it never
becomes a third play. The official recently-played reference does not
promise that played_at means start or end; real-account semantics are unverified.
Completed intervals are retained for ten minutes, capped at 128. Durable aliases
in the existing play ledger suppress later replays. First accepted completed data
wins: late history does not enrich it or create a second play. History near the start supplies a preferred timestamp/context
before completion; progress-derived starts are labelled estimates,
especially for resumed podcasts. Spotify's playback timestamp means last state
change, not response time; it is deliberately not treated as a play start.

A continuously observed natural completion whose expected end agrees with the
next item's estimated start within two seconds uses that end, with a 2000ms
uncertainty marker. Other transitions use receive time and the full polling gap
as uncertainty. A gap longer than 45 seconds leaves end unknown. Pauses/seeks
invalidate continuous-completion inference. Adjacent same-track repeats are
recognized when progress resets near zero; a seek to zero can resemble a repeat,
and a complete repeat entirely between polls can be missed. Only the initial
playback snapshot is retained, rather than every sample. API-only completed plays
are immutable after acceptance; a later source does not fill null fields.

Exports supply an end and ms_played but **no playlist/context information**;
all export context and playlist fields are null. Start=end-ms_played is an
elapsed-listening estimate, not a provable wall-clock start across pauses,
seeks, buffering or offline playback. Short export endTime has minute precision,
no URI, and no reliable cross-source match. The public Spotify description gives
field meanings; the archive's included Read Me First is the authoritative field
schema. Tests use invented instances of the known technical keys, not an owner's
archive. Video-specific history files, audiobooks, and older endsong filenames
are outside this import scope.

IDs are source-prefixed hashes of URI (or names when absent) plus the exact
millisecond API start or export end. No independent plays are rounded together.
A durable reconciliation ledger aliases a new source ID only when exactly one
opposite-source play has the same URI and both start AND known end within two
seconds, with end uncertainty <=2000ms on both sides. First accepted data wins:
an export matched to an existing API play creates no second message/version and
preserves its playlist context; API matched to export preserves export data.
Ambiguous, missing-end, coarse-time and uncertain matches remain separate.
Pauses, latency, timestamp semantics, resumed episodes, relinked URIs or offline
clock differences can therefore leave duplicates; very fast same-track repeats
can produce a false match. Identical URI/end exports are treated as the same play,
including overlapping files. Without a shared source play ID perfect deduplication
is impossible, and suppressing uncertain records would lose listening events.

Keep the core cursor: it holds pending playback, aliases, metadata cache, export
content-hash offsets, and a ledger proportional to total imported plays. This is
a simple self-contained implementation, with growing cursor size; very large
histories may need a future indexed reconciliation store. Exports are loaded one
file at a time and checkpointed every 500 rows and at file boundaries. Rewritten
files replay safely through stable IDs. Renamed identical files resume by hash.
Clearing the cursor loses reconciliation aliases, so future cross-source matches
may become duplicate messages; ordinary same-source replay remains ID-stable.

## Current Spotify access restrictions (researched 2026-11-17)

- Development mode requires Premium for the app owner and an allowlist of up to
  five users (with grandfathering described for existing counts). February 2026
  initially limited creation to one client ID; the July 2026 update superseded
  that limit with **25 client IDs**, sharing quota per developer account. New
  February restrictions began February 11; existing apps migrated March 9.
- The 2025 OAuth migration removed implicit grant and localhost aliases by
  November 27. HTTP is allowed only for explicit loopback IP redirects here;
  the helper uses Authorization Code flow and the 127.0.0.1 exception.
- The November 2024 restrictions removed algorithmic/editorial playlist access
  for affected apps along with recommendations, audio features/analysis and
  several discovery endpoints. This connector uses none of those discovery APIs.
- Development-mode playlist item endpoints moved from /tracks to /items in 2026;
  items are available only for owned/collaborated playlists. This connector only
  resolves metadata with GET /playlists/{id}; it does not enumerate memberships.
- Several metadata fields were removed in 2026. The parser tolerates missing
  optional fields and never requires popularity, market lists or publisher.
- Refresh tokens require reauthorization after six months; refresh does not
  extend that lifetime. New apps were affected June 18, existing apps July 20, 2026.
- Actual account access, Premium/allowlisting, consent, expiry, quota capacity,
  playlist visibility and real-device timestamp behavior require owner testing.
  Tests never contact Spotify or any owner's account.

## Validation

```powershell
uv sync --all-packages
uv run pytest -q plugins/spotify
uv run ruff check plugins/spotify
```

The contract kit uses only invented recordings and creates/drops disposable local
Postgres databases. Unit coverage includes 50-item cursor windows, repeated
cursors, refresh/rotation/401, invalid_grant, 429 Retry-After, bounded retries,
playlist failures, export parsing, podcasts, shared works, conservative two-way
reconciliation, OAuth state, observation timing and replay.

Official documentation used:

- [Authorization Code flow](https://developer.spotify.com/documentation/web-api/tutorials/code-flow)
- [Refreshing tokens](https://developer.spotify.com/documentation/web-api/tutorials/refreshing-tokens)
- [Redirect URIs](https://developer.spotify.com/documentation/web-api/concepts/redirect_uri)
- [Apps/dashboard setup](https://developer.spotify.com/documentation/web-api/concepts/apps)
- [Recently played](https://developer.spotify.com/documentation/web-api/reference/get-recently-played)
- [Playback state](https://developer.spotify.com/documentation/web-api/reference/get-information-about-the-users-current-playback)
- [Currently playing](https://developer.spotify.com/documentation/web-api/reference/get-the-users-currently-playing-track)
- [Get playlist](https://developer.spotify.com/documentation/web-api/reference/get-playlist)
- [Scopes](https://developer.spotify.com/documentation/web-api/concepts/scopes)
- [Rate limits](https://developer.spotify.com/documentation/web-api/concepts/rate-limits)
- [Quota modes and allowlisting](https://developer.spotify.com/documentation/web-api/concepts/quota-modes)
- [November 2024 restrictions](https://developer.spotify.com/blog/2024-11-27-changes-to-the-web-api)
- [February 2026 migration](https://developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide)
- [November 2025 OAuth migration](https://developer.spotify.com/blog/2025-10-14-reminder-oauth-migration-27-nov-2025)
- [July 2026 quota and app-limit updates](https://developer.spotify.com/blog/2026-07-23-web-api-quota-updates)
- [June 2026 refresh-token expiry](https://developer.spotify.com/blog/2026-06-18-refresh-token-expiration)
- [Export field meanings](https://support.spotify.com/us/article/understanding-your-data/)
