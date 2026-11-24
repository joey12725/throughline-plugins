# Throughline Instagram feed plugin

> **Opt-in, use with care.** This plugin reads public Instagram pages the way a
> browser does; Instagram offers no API for it. Automated collection may break
> [Instagram's Terms of Use](https://help.instagram.com/581066165581870), can
> get the requesting IP address or a signed-in account rate limited or blocked,
> and can stop working whenever Instagram changes its pages. It does nothing
> until you list accounts in `INSTAGRAM_FEED_ACCOUNTS` (or an accounts file),
> and the story reader runs only when you supply your own cookies file. Follow
> only accounts you could read yourself, keep the default pacing, and check
> that this use is acceptable to you and allowed where you live.

Follows a list of **public** Instagram accounts and records their posts and
active 24-hour stories as they appear in the `instagram.feed` channel: canonical
URL, shortcode/story ID, type (`image`, `video`, `carousel`, `reel`, `story`),
caption, posted time, story expiry time, location tag, hashtags, mentions,
story links/stickers, collaborators and the account. Public posts can be fetched
via the web profile API (`/api/v1/users/web_profile_info/?username=<handle>`) or
public profile/post HTML (`INSTAGRAM_FEED_FETCH_MODE=html` or `auto`); active
24h stories are fetched via `/api/v1/feed/reels_media/?reel_ids=<user_id>` when
authenticated cookies are supplied via `INSTAGRAM_FEED_COOKIES_FILE`. No media
is downloaded: posts and stories are stored as text and links.

## Two layers

| Layer | File | Runs where | Dependencies |
|---|---|---|---|
| Fetcher | `src/throughline_instagram_feed/fetch.py` | any host with Python 3.12+ | standard library only |
| Plugin | `src/throughline_instagram_feed/__init__.py`, `mapping.py` | next to Throughline | `throughline-sdk` |

`fetch.py` reads a rotating batch of accounts and appends raw JSONL records,
one JSON object per line:

| `kind` | `id` | Contents |
|---|---|---|
| `post` | shortcode | the raw post node, the account it was fetched through, the post owner's config (label, category) |
| `story` | numeric story pk | the raw 24h story item from `reels_media`, the account and its config |
| `profile` | numeric user id | the raw profile without its media lists (kept for later use; not mapped) |
| `status` | `<handle>:<fetched_at>` | `not_found`, `error` or another non-OK result for an account |

The plugin maps `post` and `story` records to messages, either **live** (it calls the
fetcher in-process) or from **export** files written by `fetch.py`
elsewhere: `INSTAGRAM_FEED_SOURCE=export:<file.jsonl or directory of *.jsonl>`.

## Messages

- Channel `instagram.feed`, `is_outside: true` (posts and stories by other people, not
  the owner's own activity; recall leaves it out unless `include_outside`).
  Record kind is the default life event with `location_tolerance_s: 0`, so
  the owner's track position is practically never attached to someone
  else's post. The record time `happened_at` is the post/story posted time
  (`taken_at`); there is no separate subject time.
- `external_id` is the shortcode for posts and `story:<story_id>` for stories
  (deterministic so 24h stories are preserved permanently in Throughline without
  duplicates across polls). One thread per posting account:
  `instagram:user:<numeric user id>` (or `instagram:username:<handle>` when
  no id is known), titled `@handle`.
- Author identifier: system `instagram`, kind `user_id`, value the numeric
  user id (the same scheme the Beeper plugin uses for Instagram senders);
  falls back to kind `username` with the lower-case handle. The display name
  is `@handle`.
- Content: `@handle (label) posted a reel at <location>:\n<caption>\n<url>`
  (or `@handle (label) posted a video story:\n<caption>\nLinks: ...\nStickers: ...\n<url>`).
- Fields: `url`, `shortcode`, `media_id`, `post_type`, `caption`, `account`,
  `account_label`, `account_category`, `owner_id`, `carousel_count`,
  `is_video`, `video_duration_s`, `location_name`, `location_id`,
  `accessibility_caption`, `hashtags`, `mentions`, `coauthors`, `tagged`,
  `story_id`, `expires_at`, `story_media_type`, `story_links`, `story_stickers`,
  `source`, `mapping_revision` (currently 2).
- The payload is a **stable projection** of the raw node: like, comment and
  view counts, pinned flags and the signed, expiring CDN image URLs are left
  out (they stay in the JSONL export). Fetching the same post again therefore
  creates no new version, while an edited caption, location or carousel does.
- A collaboration post fetched through two followed accounts maps to one
  identical message: identity comes from the post's owner, and the fetcher
  attaches the owner's config whichever account it came through. When a
  post's owner is given only by numeric id and differs from the fetched
  profile, the handle is left empty rather than credited to the followed
  account.

## Setup

```powershell
uv sync --all-packages
$env:THROUGHLINE_URL = 'http://localhost:8000'
$env:THROUGHLINE_TOKEN = '<Throughline plugin token for instagram_feed>'
$env:INSTAGRAM_FEED_ACCOUNTS_FILE = '<path to accounts.json>'
uv run --package throughline-instagram-feed throughline-instagram-feed manifest
uv run --package throughline-instagram-feed throughline-instagram-feed sync --dry-run
uv run --package throughline-instagram-feed throughline-instagram-feed sync --once
```

Accounts file (any of these forms; handles are case insensitive):

```json
[
  {"handle": "example_venue", "label": "Example Venue", "category": "venues/music"},
  {"handle": "example_gallery", "category": "art"},
  "example_shop"
]
```

`{"accounts": [...]}` and a text file with one `handle [label]` per line
(`#` comments allowed) work too. `INSTAGRAM_FEED_ACCOUNTS=a,b,c` adds
handles without a file. Check the list without any network call:
`python fetch.py accounts --accounts-file accounts.json`.

## Environment

Every option is read by both `fetch.py` and the plugin (`fetch.py` also
accepts each as `--option-name`). Defaults are in brackets.

| Variable | Meaning |
|---|---|
| `INSTAGRAM_FEED_SOURCE` | Plugin only: `live` [default] or `export:<file or directory>` |
| `INSTAGRAM_FEED_SINCE`, `INSTAGRAM_FEED_UNTIL` | Plugin only: skip posts before / at or after these ISO dates (backfill bounds) |
| `INSTAGRAM_FEED_ALL` | Plugin only, live: `1` fetches every account each run instead of one batch [0] |
| `INSTAGRAM_FEED_ACCOUNTS` | Comma separated handles |
| `INSTAGRAM_FEED_ACCOUNTS_FILE` | Accounts file (see above) |
| `INSTAGRAM_FEED_FETCH_MODE` | Profile fetch mode: `api` [default], `html` (public profile/post HTML), or `auto` (`api` with cookies, `html` when anonymous) |
| `INSTAGRAM_FEED_STORIES` | `1` fetches active 24h stories via `/api/v1/feed/reels_media/` when `INSTAGRAM_FEED_COOKIES_FILE` is configured; `0` disables [1] |
| `INSTAGRAM_FEED_BATCH` | Accounts per run, least recently fetched first [12] |
| `INSTAGRAM_FEED_PER_DAY` | HTTP requests per UTC day across runs, priming and retries included [200] |
| `INSTAGRAM_FEED_POSTS` | Newest posts kept per account fetch [12] |
| `INSTAGRAM_FEED_DELAY_S` | Seconds between requests, plus up to 1 s jitter [2.0] |
| `INSTAGRAM_FEED_DEADLINE_S` | Stop starting new accounts after this many seconds [90] |
| `INSTAGRAM_FEED_RETRIES` | Attempts per request on 5xx or transport errors, backoff 2 s, 4 s, ... [3] |
| `INSTAGRAM_FEED_COOLDOWN_S` | First pause after 401/429/login wall; doubles per strike, `Retry-After` honoured [900] |
| `INSTAGRAM_FEED_MAX_COOLDOWN_S` | Longest pause [86400] |
| `INSTAGRAM_FEED_TIMEOUT_S` | Socket timeout per request [20] |
| `INSTAGRAM_FEED_PRIME` | `1` loads the home page once per run for anonymous cookies; skipped with a cookies file [1] |
| `INSTAGRAM_FEED_USER_AGENT` | User-Agent header [a desktop browser string] |
| `INSTAGRAM_FEED_APP_ID` | `X-IG-App-ID` of the public web client [built in] |
| `INSTAGRAM_FEED_BASE_URL` | Base URL [https://www.instagram.com]; tests use a loopback fake |
| `INSTAGRAM_FEED_COOKIES_FILE` | Secret, optional: path to a Netscape `cookies.txt` or a JSON cookie list. Read at run time; values are never printed, logged or exported |

Credentials come only from the environment, never from arguments.

## Limits and politeness

- One batch per run (12 accounts by default) on a rolling rotation: accounts
  never fetched first, then the least recently fetched; ties keep config
  order. With the default 2 hour interval every account is visited about
  every `ceil(accounts / 12) * 2` hours.
- A per-day request budget (200) is kept in the state: the plugin cursor in
  live mode, `<out>.state.json` for `fetch.py`. When it is spent the run
  stops and resumes on the next UTC day.
- 401, 429, a redirect to the login or challenge page, a non-JSON 2xx
  answer, or a JSON error body with any status that says "please wait",
  `require_login`, `checkpoint_required`, `challenge_required` or `spam`
  stop the run at once and start a cool-down (15 min, doubling per
  consecutive strike, at most a day; `Retry-After` in seconds or as an HTTP
  date is honoured). Other 4xx answers mark only that account. No request is made while
  cooling down. The first successful account resets the strikes.
- 5xx and transport errors retry with exponential backoff; when retries are
  exhausted the account is marked `error` and the run moves on.
- The endpoint returns only the newest 12 posts of an account, so history
  older than that cannot be backfilled anonymously. `SINCE`/`UNTIL` bound
  what is imported from exports; changing them later needs a cursor reset
  to re-read files already consumed.
- No paid calls of any kind.

## Export workflow

On a host that can reach Instagram (only Python 3.12+ is needed), copy
`fetch.py` and an accounts file there and schedule:

```sh
INSTAGRAM_FEED_ACCOUNTS_FILE=/path/to/accounts.json \
python3 fetch.py export --out /path/to/instagram-feed/feed-$(date +%Y-%m).jsonl
```

Each run appends to the file and keeps its rotation, request count and
cool-down in `<out>.state.json` (or `--state FILE`). A one-line JSON summary
(accounts, posts, requests, why it stopped) goes to stderr. Copy the
directory back and import it:

```powershell
$env:INSTAGRAM_FEED_SOURCE = 'export:<local copy of the instagram-feed directory>'
uv run --package throughline-instagram-feed throughline-instagram-feed sync --once
```

The export cursor keeps a byte offset per file name, so appended lines are
picked up on the next sync, a partially written last line is left for later,
and a replaced file (different first line) is read again from the start
(idempotent: unchanged posts create no versions). Within one run a post
seen twice is sent once; if a later line carries a changed caption, a
checkpoint separates the two versions.

## Tests

```powershell
uv run --no-sync pytest -q plugins/instagram-feed
uv run --no-sync ruff check plugins/instagram-feed
```

All payloads are invented and built in the tests. A loopback HTTP server
stands in for Instagram; the standalone fetcher is run as a copied file with
`python -I -S` to prove it needs only the standard library. The SDK contract
kit runs against both an export directory and the live path.
