# Browser history

Records the pages you visit and your bookmarks from the browsers on this
machine: Chrome, Chromium, Brave, Edge, Vivaldi (all Chromium), Firefox and
LibreWolf, and Safari. It is a live connector: browsers keep their history
databases current and the plugin reads new visits on a timer. Each run
copies the database (plus `-wal`/`-shm`/`-journal`) to a temporary folder
and reads the copy, so a running browser is never locked or modified.

Standard library only.

## Channels

### `browser_history.visits` (life events, not embedded by default)

One record per visit. Content is the page title and URL. External id
`<device>:<browser>/<profile>:<visit id>:<raw visit time>`; threads are one
browser profile's day (`Chrome Work · 2025-01-01`).

| Field | Type | Meaning |
| --- | --- | --- |
| url, title, domain | text | Cleaned URL, page title, host without `www.` |
| browser, profile, profile_name, device | text | Where it came from (`profile` is `browser/folder`) |
| transition | text | `link`, `typed`, `reload`, `form_submit`, `auto_bookmark`, `redirect_permanent`, ... |
| typed, forward_back, redirect | boolean | Address bar / back-forward / redirect target |
| duration_s | number | Time the page was open (Chromium only) |
| referrer_url, from_visit | text | The visit this one came from |
| synced | boolean | Synced from another device (only kept when enabled) |
| author_kind | text | `owner` |

Visits are embedded off (`embed: false`) because history is high-volume;
full-text and field search cover it. Turn vectors on with
`throughline embed channels` if you want meaning search over titles.

### `browser_history.bookmarks` (reference)

Chromium-family `Bookmarks` files and Firefox `moz_bookmarks`. External id
`<browser>:<bookmark guid>` — synced bookmarks keep their GUID across
devices, so two machines report the same record. Fields: `url`, `title`,
`domain`, `folder` (path), `browser`, `date_added`, `removed` (tombstone
when a bookmark disappears). Read again only when the bookmark file or table
changes.

## Privacy

Applied on the machine, before anything is sent:

- Only `http`/`https` URLs are kept: `file:`, `chrome://`, `about:`,
  `edge://`, extension pages, `javascript:` bookmarklets and `data:` URLs
  are dropped (`BROWSER_HISTORY_SCHEMES`).
- `localhost`, `127.0.0.1`, `0.0.0.0` and `::1` are excluded by default.
  Add anything else you never want recorded — banks, health portals, work
  intranets — to `BROWSER_HISTORY_EXCLUDE_DOMAINS` (suffix match:
  `example.org` also excludes `www.example.org`; `*` globs allowed).
- Credentials in URLs (`user:pass@`) are removed and the values of
  sensitive query/fragment parameters (`token`, `access_token`, `code`,
  `password`, `key`, `secret`, `session`, `sig`, `signature`, AWS/GCS
  signatures, ...) are replaced with `REDACTED`. Add names with
  `BROWSER_HISTORY_REDACT_PARAMS`, or drop every query string and fragment
  with `BROWSER_HISTORY_STRIP_QUERY=true`.
- Private/incognito windows are never written to history by the browsers,
  so they never reach Throughline.
- Sub-frame/embed loads (ads, widgets) and pages that only redirected
  onwards are skipped (`BROWSER_HISTORY_INCLUDE_REDIRECTS=true` keeps the
  latter).
- Visits synced from your other devices are skipped by default: run the
  plugin on each device instead, so every visit is reported once by the
  device it happened on.

Exclusions apply from the next run on; they do not remove records that were
already ingested.

## Setup

```sh
throughline token create browser-history --kind plugin --plugin browser_history --json
uv run throughline-browser-history backfill      # first import (whatever the browser still keeps)
uv run throughline-browser-history sync --once   # on a timer, e.g. every 15 minutes
```

Profiles are discovered automatically:

| Browser | Linux | macOS | Windows |
| --- | --- | --- | --- |
| Chrome | `~/.config/google-chrome` (+ Flatpak) | `~/Library/Application Support/Google/Chrome` | `%LOCALAPPDATA%\Google\Chrome\User Data` |
| Chromium | `~/.config/chromium` (+ Flatpak, Snap) | `.../Chromium` | `%LOCALAPPDATA%\Chromium\User Data` |
| Brave | `~/.config/BraveSoftware/Brave-Browser` | `.../BraveSoftware/Brave-Browser` | `%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data` |
| Edge | `~/.config/microsoft-edge` | `.../Microsoft Edge` | `%LOCALAPPDATA%\Microsoft\Edge\User Data` |
| Vivaldi | `~/.config/vivaldi` | `.../Vivaldi` | `%LOCALAPPDATA%\Vivaldi\User Data` |
| Firefox | `~/.mozilla/firefox` (+ Flatpak, Snap) | `.../Firefox/Profiles` | `%APPDATA%\Mozilla\Firefox\Profiles` |
| LibreWolf | `~/.librewolf` | `.../librewolf/Profiles` | `%APPDATA%\librewolf\Profiles` |
| Safari | — | `~/Library/Safari/History.db` | — |

Every Chromium profile folder with a `History` file (`Default`,
`Profile 1`, ...) and every Firefox profile with `places.sqlite` is read.
On macOS, Safari needs **Full Disk Access** for the program running the
plugin.

| Variable | Default | Meaning |
| --- | --- | --- |
| `BROWSER_HISTORY_PROFILES` | | Explicit `browser:path` items (profile folder or history file), comma separated; disables discovery |
| `BROWSER_HISTORY_BROWSERS` | all | Discover only these (`chrome,firefox`) |
| `BROWSER_HISTORY_HOME` | your home | Home folder searched by discovery |
| `BROWSER_HISTORY_PLATFORM` | this system | `linux`, `darwin` or `windows` layout |
| `BROWSER_HISTORY_DEVICE` | host name | Device label in visit ids — keep it stable |
| `BROWSER_HISTORY_TZ` | system zone | Zone for day threads |
| `BROWSER_HISTORY_EXCLUDE_DOMAINS` | | Domains never recorded |
| `BROWSER_HISTORY_DEFAULT_EXCLUDES` | `true` | Also exclude localhost addresses |
| `BROWSER_HISTORY_SCHEMES` | `http,https` | URL schemes recorded |
| `BROWSER_HISTORY_REDACT_PARAMS` | | Extra parameter names to redact |
| `BROWSER_HISTORY_STRIP_QUERY` | `false` | Drop all query strings and fragments |
| `BROWSER_HISTORY_INCLUDE_SYNCED` | `false` | Keep visits synced from other devices |
| `BROWSER_HISTORY_INCLUDE_REDIRECTS` | `false` | Keep redirect-only visits |
| `BROWSER_HISTORY_BOOKMARKS` | `true` | Read bookmarks |
| `BROWSER_HISTORY_CHECKPOINT_EVERY` | `500` | Visits per checkpoint |

## How it syncs

The cursor keeps, per profile, the newest visit id read, the newest visit
time and a bookmark signature (plus the packed bookmark GUID list for
removal detection). Each run reads visits with a larger id. If the newest id
in the database is lower than the cursor's (you cleared recent history and
SQLite reused the ids), that run reads by time instead; visit ids include
the visit time so reused ids never overwrite older records.

If the oldest visit still in the database is newer than the cursor's
high-water mark, the browser expired history the plugin never saw (Chrome
keeps about 90 days); the run reports `partial` coverage with a
`source_cap` gap for that window. A profile that cannot be copied or read is
reported as `partial` (`source_error`); when no profile can be read the run
fails.

## Limitations

- The first backfill only gets what the browser still keeps: about 90 days
  for Chromium browsers, longer for Firefox and Safari (by their own expiry
  settings).
- Firefox and Safari do not record how long a page was open.
- Page titles come from the browser's URL table at read time; a title the
  browser changes later is not re-read.
- Firefox's `moz_historyvisits.source` (synced visits) and Safari's
  `history_visits.origin` are used as documented in their source trees;
  older Firefox versions without the column report every visit as local.
- Safari bookmarks and Reading List are not read (`Bookmarks.plist` has no
  creation dates). Chromium "Tab groups", reading list and Firefox
  keywords/tags are not read.
- Removing a domain from the exclude list does not backfill visits skipped
  earlier.
- Built from the browsers' documented schemas and tested against synthetic
  databases; not verified against every browser version.
