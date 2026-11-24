# Throughline Plex plugin

Imports the Plex server's available watch/listen history and polls active sessions into
`media.plex`. Every viewing is a message; every episode, movie or track is a shared
`work` entity. Source links connect viewing -> work (`consumed`), episode -> show,
episode -> season and season -> show (`part_of`). GUIDs are entity external refs;
missing GUIDs use `plex:<machineIdentifier>:<ratingKey>`. The core creates these as
placeholders through external entity link endpoints (LNK-3); a later works plugin
can enrich/merge them. This plugin imports no captions or reference text (MED-2).

Threads are account + server + library, keeping each person's library consumption
together, including films and music, without mixing different accounts' timelines.
Authors use Plex `account_id` and the account's name. Unknown account IDs remain
`unknown` rather than guessing the owner. Account/library filtering is local and
applies to both modes. Filters default to all; changes apply to future incremental
windows. For a complete newly expanded filter import, reset the plugin cursor.

## Configuration and commands

The manifest declares all settings and the token secret. In PowerShell:

```powershell
uv sync --all-packages
$env:THROUGHLINE_URL = 'http://localhost:8000'
$env:THROUGHLINE_TOKEN = '<Throughline token scoped to plex>'
$env:PLEX_SERVER_URL = '<Plex server HTTP(S) URL>'
$env:PLEX_TOKEN = '<Plex server owner token>'
# Optional comma-separated IDs or names (case insensitive); unset means all:
$env:PLEX_ACCOUNTS = '<account id or name>,<another account id or name>'
$env:PLEX_LIBRARIES = '<library id or name>'
$env:PLEX_PAGE_SIZE = '100'
uv run --package throughline-plex throughline-plex manifest
uv run --package throughline-plex throughline-plex backfill --dry-run
uv run --package throughline-plex throughline-plex backfill
uv run --package throughline-plex throughline-plex sync --once
uv run --package throughline-plex throughline-plex sync
```

Keep the same settings and Throughline installation for resume. The process polls
once every 30 seconds; backfill also samples active sessions once at the end.
`PLEX_RECORDING` optionally selects an invented JSON response bundle for offline
tests and disables every source network call. Unset it for real imports.
Use the owner's server token to see all users the server permits. Obtain the token
using [Plex's token instructions](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/).
Tokens go only in `X-Plex-Token`, never URL query strings; redirects are refused.
No cloud account login, token creation or user switching is performed.

## Fields and timing

Declared fields: `media_type`, `title`, `show_title`, `season`, `episode`, `year`,
`duration_s` (work runtime), `view_offset_s` (last observed media position), `device`,
`player`, `library`, `plex_rating_key`, `plex_guid`, `account_name`, `timing_basis`,
`playback_state`, `position_observed_at`, `time_precision`, `initial_offset_s`.
Missing values stay null. Times use UTC;
Plex epoch timestamps do not provide an original local UTC offset. Raw history or
session objects and fetched metadata objects are kept untouched under payload
`source` and `metadata` respectively.

History supplies `viewedAt`, a played-mark timestamp, not a start or stop.
We retain it as happened_at, with time_precision=source_mark and no ended_at.
Runtime is metadata, never subtracted to fabricate a viewing start. Historical
exact overlap queries are unavailable. Current position is not historical position.

Active sessions use first poll time with time_precision=first_seen, preserve the
initial media position in initial_offset_s, and record current position and poll time.
A closed session reappearing, a gap over 120 seconds, or a restart starts a new
viewing. A restart is a backward move to a position that playback from zero could
have reached since the previous poll (repeat, or the item started over). Any other
backward seek stays in the same viewing; rewinding to the first seconds of an item
is indistinguishable from a restart and counts as one.
When a session disappears, end is its last poll that was not paused-and-stationary,
so a session left paused for hours ends when it was last seen playing. Start and
end are therefore accurate to one poll interval (30 seconds by default). A missed
poll/network outage can leave an arbitrarily large timing uncertainty. Paused
sessions remain open while listed. Multiple
polls update one message through immutable versions, never extra viewing messages.
Position plus observation time provides the anchor for later caption matching
(MED-3); captions and a guaranteed 30-second caption alignment are outside this
plugin and cannot be inferred from history alone.

History is paged ascending by viewedAt with Plex container start/size parameters.
A checkpoint stores a fixed lower bound and page offset after acknowledged writes;
completed passes overlap the last watermark by one day, including equal timestamps.
Stable historyKey IDs make retries idempotent. Appended history does not shift
ascending offsets, but concurrent deletions or backdated inserts during paging can;
restart/reset the cursor to recover older changes. Sync on an empty store imports
all history. Existing cursors resume rather than forcing a new full import.

Observed sessions are reconciled to history when exactly one unmatched observation
has the same account/rating key and contains viewedAt between estimated start and
last poll + 120 seconds. This updates the original viewing ID. Simultaneous matching
viewings are ambiguous and stay separate; Plex exposes no reliable history/session
foreign key, so perfect deduplication in those cases is impossible. A matched
viewing keeps its observed start and end: the played mark is a point inside the
interval, not a replacement for it. While the session is live the history row is
kept under payload `history` beside the session `source`; a row matched after the
session closed becomes `source`. A matched session keeps updating and still gets
its closing version. Closed observations drop raw session, metadata and history
row after final emission.
Closed observations expire after 24 hours (later while an alias still refers to
them). One still open is never expired: after any downtime the next run closes it
at its last playing poll and emits that end. Observations
are capped at 2,048; history fingerprints
and aliases cover the one-day watermark overlap and are capped at 4,096.
Unchanged history rows incur no metadata lookup or message/link emission. Pages
are still read over the overlap to find delayed rows and corrections. Exceeding
caps can cause replay; rows outside retained observations cannot reconcile to
live IDs. Clearing the cursor also loses aliases. Real-server filtering and
client ID reuse remain unverified; tests use invented responses.
The query key is viewedAt> (wire operator >=), following Media Queries at
https://developer.plex.tv/pms/.

## Read-only endpoint allowlist

Only these Plex HTTP calls are possible (all GET):

- `/identity`: machineIdentifier for server-scoped IDs.
- `/accounts`: account IDs and names (python-plexapi SystemAccount endpoint).
- `/devices`: historical device names/platforms (SystemDevice endpoint).
- `/status/sessions/history/all`: paged history, sort/viewedAt/container parameters.
- `/status/sessions`: currently playing sessions, users, players and positions.
- `/library/metadata/{numeric ratingKey}`: missing duration, GUID and hierarchy data.

No scans, refreshes, scrobble/unscrobble, timeline updates, playback control,
termination or other mutations are called, including mutating GET endpoints.
Transport failures, 429 and 5xx responses retry four times with bounded exponential
backoff; other errors fail without advancing the cursor. Missing/deleted metadata
(404) leaves sparse history importable. Server permissions, history retention,
version differences and availability of account/device listings require real-server
verification. Only history retained by Plex can be imported. One Plex server per
plugin installation/cursor is supported; a changed server identity fails clearly.
Metadata/account renames can legitimately create new normalized versions on replay.

## Verification and API references

Run `uv run pytest -q plugins/plex`. Fixtures are invented JSON MediaContainer
responses and never contact any source account or server. The contract kit uses
only disposable local PostgreSQL databases. Unit coverage includes movies, episodes,
music, sparse records, users, filters, paging/resume, positions, reconciliation and
an HTTP transport test asserting the GET allowlist and token header.

Researched API references:

- [Official Plex Media Server API](https://developer.plex.tv/pms/): JSON Accept header,
  authentication, identity, sessions, history fields, sort/filter parameters, metadata.
- [python-plexapi server implementation/docs](https://python-plexapi.readthedocs.io/en/latest/_modules/plexapi/server.html):
  history batching, sessions, `/accounts`, `/devices`, SystemAccount/SystemDevice.
- [python-plexapi history/session mixins](https://python-plexapi.readthedocs.io/en/latest/_modules/plexapi/base.html):
  historyKey, viewedAt, accountID/deviceID and session semantics.
- [python-plexapi video fields](https://python-plexapi.readthedocs.io/en/latest/_modules/plexapi/video.html):
  GUID, runtime and library/episode hierarchy.
