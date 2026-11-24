# Throughline Reddit

Captures **your own Reddit account**, read only: saved posts and comments,
upvoted (and, if enabled, downvoted) items, submitted posts and written
comments, each with subreddit, permalink, score, creation time and the
untouched Reddit object.

Two transports read the account listings:

- `oauth` (default): Reddit's OAuth API with your own app credentials.
- `session`: a signed-in headless Chromium with a private persistent profile
  (`browser.py`, standard library only), for accounts without API access. It
  reads the same listings through the signed-in page's `.json` endpoints.

Nothing on Reddit is changed: the only calls are `GET` listings, the OAuth
token endpoint and, for `session`, Reddit's own sign-in page.

The package has two layers:

- `src/throughline_reddit/fetch.py` is a **standard-library-only** fetcher
  (urllib, json; Python 3.12 to 3.14). Copy that single file to any
  host that holds the credentials and run `python fetch.py export --out FILE`.
  It writes raw JSONL records and keeps an incremental cursor in a state file.
- The plugin (`throughline_reddit`, built on `throughline-sdk`) maps raw records
  to Throughline messages. It reads either live (running the fetcher in
  process) or from export files: `REDDIT_SOURCE=live` or
  `REDDIT_SOURCE=export:<file or directory>`.

## Channels

| Channel | What | `happened_at` | Author |
|---|---|---|---|
| `reddit.saved` | Saved posts and comments | first time the save was observed | none (owner action) |
| `reddit.votes` | Upvoted / downvoted items (`vote` = 1 / -1) | first time the vote was observed | none (owner action) |
| `reddit.posts` | Posts the owner submitted | post creation time | the owner's username |
| `reddit.comments` | Comments the owner wrote | comment creation time | the owner's username |

Reddit does not timestamp saves or votes. Those records use the time they were
first observed (`time_precision=first_seen`) and carry the item's own creation
time as the record's **subject** (`subject.start`) and in `created_at`. All
other records use the item's creation time (`time_precision=source`).

Every channel declares the same fields: `item_type` (post/comment), `fullname`,
`subreddit`, `permalink`, `title` (for a comment: its post's title),
`item_author`, `link_url`, `domain`, `flair`, `reddit_score`, `num_comments`,
`upvote_ratio`, `over_18`, `created_at`, `edited_at`, `post_fullname`,
`parent_fullname`, `vote`, `listing`, `source`, `account`,
`time_precision`, `mapping_revision`. `source` is the transport (`oauth` or
`session`). The score field is `reddit_score`
because `score` is a reserved Throughline column. The payload keeps the raw
Reddit object (`payload.item`), the listing and the transport.

Threads: `saved` and `votes` are single threads; posts are threaded per
subreddit (`r/<name>`); comments are threaded per post
(`post:<t3 fullname>`). External IDs are Reddit fullnames (`t3_...` posts,
`t1_...` comments).

Sender identifiers: `author = {system: "reddit", kind: "username", value,
display_name: "u/<name>"}`; deleted authors are left out. Reddit usernames are
case-insensitive: an owner or agent token can register the scheme once with
`POST /v1/registry/schemes` `{system: "reddit", kind: "username", normalizer:
"lower"}` (plugin tokens cannot write identity data).

Links (all `method=source`): saved message `saved` → item entity, vote message
`voted` → item entity, post/comment message `represents` → item entity;
item entity `posted_in` → subreddit entity; comment entity `reply_to` → post
entity. Entities: `kind=post` / `comment` with `external_ref=reddit:<fullname>`,
`kind=community` with `external_ref=reddit:r/<lowercase name>`. A saved post, a
comment on it and the same post upvoted all meet at one post entity.

### Versions and idempotence

The mapping is a pure function of one raw record (plus the first-seen time).
The cursor keeps, per item, a digest of its significant content and its
first-seen time. An item is re-sent only when its content changes (edited text,
a changed vote, a changed title or flair, a deleted author) or the mapping
revision changes. **Score, comment count and upvote ratio change on every
fetch; a change in them alone does not create a version**, so those fields hold
the values captured with the stored version. Within one run (or one directory
of exports) the occurrences of an item collapse into one message: the earliest
occurrence that carries the item's latest content, dated by the earliest
observation. Re-reading the same exports therefore produces no new versions,
with or without the cursor. `mapping_revision` (currently 1) is part of each
record and of the digest: bumping it re-sends every item once, keeping
first-seen times.

Keep the plugin cursor: first-seen times of saves and votes live there (and,
in export mode, can also be recomputed from the export files themselves).

## Setup

### Reddit app and credentials

1. Create an app at <https://www.reddit.com/prefs/apps> while signed in to the
   account to capture. For personal use choose **script**; any redirect URI
   such as `http://127.0.0.1:65010/callback` is fine for a script app.
2. Pick one way to authenticate:
   - **Script app, password grant**: `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`,
     `REDDIT_USERNAME`, `REDDIT_PASSWORD`. Does not work for accounts with
     two-factor authentication.
   - **Refresh token** (any app type, scopes `identity history read`,
     `duration=permanent`): `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` (empty for
     an installed app), `REDDIT_REFRESH_TOKEN`. Reddit refresh tokens do not
     rotate; the fetcher never stores the short-lived access token.
   - **Ready access token**: `REDDIT_ACCESS_TOKEN` (no refresh; for short tests).
3. Set `REDDIT_USER_AGENT` to a unique, descriptive value as Reddit's API rules
   require, for example `<platform>:<app id>:<version> (by /u/<your name>)`.
   It is required for any request.

### Browser session instead of an OAuth app

With `REDDIT_TRANSPORT=session` no OAuth app is needed. Set
`REDDIT_SESSION_PROFILE` to a private directory for the browser profile (it
holds your Reddit sign-in cookies: keep it readable only by you). The first
run signs in with `REDDIT_USERNAME` / `REDDIT_PASSWORD`; later runs reuse the
profile and sign in again only when the session is gone. The account name
comes from the session when `REDDIT_USERNAME` is unset; when it is set and the
profile is signed in as someone else, the run stops. Chromium is found through
`REDDIT_CHROME`, then `chromium` / `google-chrome` on `PATH`, then a
Playwright-installed Chromium. `python browser.py --profile DIR status` shows
whether a profile is signed in. The automated sign-in cannot answer a
two-factor prompt; for such accounts use OAuth.

### Run the plugin live

```powershell
uv sync --all-packages
$env:THROUGHLINE_URL = 'http://127.0.0.1:8000'
$env:THROUGHLINE_TOKEN = '<plugin token scoped to reddit>'
$env:REDDIT_USER_AGENT = '<platform>:<app id>:0.1.0 (by /u/<name>)'
$env:REDDIT_CLIENT_ID = '<client id>'
$env:REDDIT_CLIENT_SECRET = '<client secret>'
$env:REDDIT_REFRESH_TOKEN = '<refresh token>'
uv run throughline-reddit manifest
uv run throughline-reddit backfill --dry-run
uv run throughline-reddit backfill
uv run throughline-reddit sync --once
```

`backfill` pages every enabled listing to its bounds (`--full` in fetcher
terms); `sync` stops each listing at the newest items it saw last time. A
`--dry-run` in live mode still calls Reddit (with an empty cursor, so a full
pass that counts against the day budget only in memory) and still writes
`REDDIT_ARCHIVE_DIR` files; it writes nothing to Throughline. Reddit
returns at most about 1,000 items per listing, so older saves and votes are not
reachable through the API (use a Reddit data request export for those).

### Export workflow (credentials on another host)

`fetch.py` is a single standard-library file, so it can run on whichever
machine holds the Reddit credentials (or the browser profile, with
`browser.py` copied next to it); only the raw JSONL travels to Throughline.
On that host (only Python is needed):

```sh
# credentials and user agent in the environment, never on the command line
export REDDIT_USER_AGENT='<platform>:<app id>:0.1.0 (by /u/<name>)'
export REDDIT_CLIENT_ID=... REDDIT_CLIENT_SECRET=... REDDIT_REFRESH_TOKEN=...
python3 fetch.py export --full --out reddit-$(date -u +%Y%m%dT%H%M%SZ).jsonl \
    --state reddit-state.json
# later runs: incremental, same state file
python3 fetch.py export --out reddit-$(date -u +%Y%m%dT%H%M%SZ).jsonl \
    --state reddit-state.json
```

Copy the JSONL files to the Throughline side, then:

```powershell
$env:REDDIT_SOURCE = 'export:C:\private\reddit-exports'   # a file or a directory
uv run throughline-reddit sync --once
```

A directory is read recursively (`*.jsonl` and `*.jsonl.gz`, sorted by relative path; partial
`.part` files are ignored). Re-reading old files is harmless. The fetcher
writes the output to `<out>.part` and renames it when done, advances the state
file only after a run without a fatal error, and prints only counts to stderr.
Exit codes: 0 success (possibly truncated by a limit, see the summary record),
1 fatal fetch error (the output still holds what was fetched; the state is not
advanced except for the per-day request count), 2 configuration error.

`fetch.py export --help` lists every option. Each option can also be given by
its environment variable below; the command line wins.

### Raw record format

One JSON object per line:

```json
{"kind": "reddit.item", "listing": "saved", "id": "t3_abc123", "account": "<owner>", "source": "oauth", "observed_at": "2026-01-01T12:00:00+00:00", "raw": {"kind": "t3", "data": {"...": "untouched Reddit object"}}}
{"kind": "reddit.export_summary", "fetcher_version": "2", "requests": 7, "listings": {}, "truncated": null}
```

`raw` is the listing child as Reddit returned it (`raw_json=1`). The plugin
ignores kinds it does not know.

## Environment

Secrets (declared in the manifest, all optional, never written anywhere):

| Variable | Meaning |
|---|---|
| `REDDIT_CLIENT_ID` | OAuth app client id |
| `REDDIT_CLIENT_SECRET` | OAuth app secret (empty for installed apps) |
| `REDDIT_REFRESH_TOKEN` | Permanent user refresh token |
| `REDDIT_PASSWORD` | Account password (script app password grant, or session sign-in) |
| `REDDIT_ACCESS_TOKEN` | Ready bearer token, used until it is refused |

Settings (fetcher flag in brackets):

| Variable | Default | Meaning |
|---|---|---|
| `REDDIT_SOURCE` | `live` | Plugin only: `live` or `export:<file or directory>` |
| `REDDIT_USER_AGENT` | none | Required for any request |
| `REDDIT_USERNAME` | none | Account name (password grant, session sign-in); otherwise read from `/api/v1/me` or the session |
| `REDDIT_TRANSPORT` [`--transport`] | `oauth` | `oauth` or `session` (signed-in browser) |
| `REDDIT_SESSION_PROFILE` [`--session-profile`] | none | Session transport: private persistent browser profile directory |
| `REDDIT_CHROME` | none | Session transport: Chromium binary |
| `REDDIT_LISTINGS` [`--listings`] | `saved,upvoted,submitted,comments` | Also `downvoted`; `none` disables account listings |
| `REDDIT_SINCE` / `REDDIT_UNTIL` [`--since` / `--until`] | none | Bounds on item creation time (ISO); stop paging early on date-ordered listings (submitted, comments), filter otherwise |
| `REDDIT_MAX_PAGES` [`--max-pages`] | 10 | Pages per account listing per run |
| `REDDIT_PAGE_SIZE` [`--page-size`] | 100 | Items per page (Reddit's maximum) |
| `REDDIT_MAX_REQUESTS` [`--max-requests`] | 200 | HTTP requests per run |
| `REDDIT_MAX_REQUESTS_PER_DAY` [`--max-requests-per-day`] | 2000 | HTTP requests per UTC day, counted in the state/cursor |
| `REDDIT_MIN_INTERVAL` [`--min-interval`] | 1.0 | Seconds between Reddit requests |
| `REDDIT_RETRIES` [`--retries`] | 5 | Retries after 429, 5xx and transport errors |
| `REDDIT_MAX_WAIT` [`--max-wait`] | 300 | Longest single wait; a longer `Retry-After` or rate-limit reset ends the run early (resumable) |
| `REDDIT_TIMEOUT` [`--timeout`] | 30 | Seconds per request |
| `REDDIT_API_BASE`, `REDDIT_AUTH_BASE` | `https://oauth.reddit.com`, `https://www.reddit.com` | Base URLs (HTTPS; plain HTTP only for loopback test servers) |
| `REDDIT_BATCH_SIZE` | 500 | Plugin: messages per checkpoint |
| `REDDIT_ARCHIVE_DIR` | none | Plugin, live mode: also keep each run's raw JSONL here |

Also supply the SDK variables `THROUGHLINE_URL` and `THROUGHLINE_TOKEN`.

## Limits and politeness

- Reddit allows OAuth clients 100 requests per minute (averaged over ten
  minutes). The default 1 s spacing stays well under it, and the fetcher obeys
  `X-Ratelimit-Remaining` / `X-Ratelimit-Reset`: when the remaining budget is
  spent it waits for the reset before the next request.
- 429, 5xx and transport errors are retried with exponential backoff (1, 2, 4,
  ... up to 60 s, or the server's `Retry-After`), at most `REDDIT_RETRIES` times.
  A 401 triggers one token refresh. Redirects are never followed (they could
  carry the bearer token elsewhere); a redirected request ends the run with
  an error.
- Per-run and per-day request budgets. When one runs out the run stops
  cleanly: a listing cut short keeps its `after` token and the next run resumes
  there before starting a new incremental pass; its known head only moves when
  a pass completes, so nothing in between is skipped. The per-day count is
  saved even when a run fails, so repeated failing runs cannot exceed it.
- Incremental passes stop at three consecutive ids from the last run's head
  (the 50 newest ids seen inside the bounds), not at the first one: saving or
  voting on an old item again moves it to the top of the listing, above items
  that are still new. Items left out by `--until` never enter the head, so a
  later run without the bound still finds them.
- No model calls and no paid APIs.

In live mode the fetch state lives in the plugin cursor (`fetch`); it advances
only in the final checkpoint of a run, so a crash before every fetched item is
acknowledged refetches instead of skipping. Only the per-day request count is
carried in earlier checkpoints (and in the checkpoint written before a failed
fetch is reported), so retries after a crash still count toward the daily limit.

## Not covered

- Unsaves and removed votes are not detected (Reddit offers no history of
  them); records stay as captured.
- Items beyond Reddit's ~1,000-item listing window are unreachable through the
  API; a Reddit data request (GDPR export) importer is not included.
- There is no helper for the OAuth authorization-code flow; obtain a refresh
  token with any standard tool, or use a script app with the password grant.
- Reddit chat is not covered here (the Beeper plugin carries chats).
- Gallery images, videos and other media are linked, never downloaded.

## Tests

```powershell
uv run --no-sync pytest -q plugins/reddit
uv run --no-sync ruff check plugins/reddit
```

All tests are offline. `test_reddit_fetch.py` runs the fetcher (including the
copied standalone `fetch.py` as a subprocess with `python -I`) against a
loopback fake Reddit server: OAuth refresh and password grants, pagination,
incremental heads, bounds, retries with backoff, rate-limit headers, run and
day budgets with resume, redirects, and that no secret reaches output, state,
stdout or stderr. `test_session_transport.py` covers the session transport
with a stand-in browser. `test_reddit.py` covers the mapping,
idempotent reruns, first-seen times, mapping revisions, crash resume, live mode
against the fake server, and the SDK contract kit on a synthetic two-file
export (all four channels). Fixtures are invented in the
tests; no real Reddit data is included.
