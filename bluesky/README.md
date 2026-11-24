# Bluesky

Your own Bluesky (AT Protocol) activity: every post and reply you wrote, every
repost and like, and optionally the replies, mentions and quotes other people
direct at you. Live: each run reads your repository on your PDS and picks up
whatever is new since the last run. The full history is in the repository, so
the first run backfills everything.

## Setup

1. Create an **app password**: Bluesky → Settings → Privacy and security →
   App passwords. Never use your main password. (No special privileges are
   needed; direct-message access is *not* required.)
2. Create a plugin token on the Throughline box:
   `throughline token create bluesky --kind plugin --plugin bluesky --json`.
3. Run:

   ```sh
   export THROUGHLINE_URL=http://localhost:8000 THROUGHLINE_TOKEN=<plugin token>
   export BLUESKY_IDENTIFIER=alice.bsky.social
   export BLUESKY_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
   export BLUESKY_NOTIFICATIONS=true          # optional
   uv run --package throughline-bluesky throughline-bluesky backfill --dry-run
   uv run --package throughline-bluesky throughline-bluesky backfill
   uv run --package throughline-bluesky throughline-bluesky sync --once     # on a timer
   ```

   Self-hosted PDS: set `BLUESKY_SERVICE=https://pds.example.com`.

## How it works

- `com.atproto.server.createSession` signs in at `BLUESKY_SERVICE`
  (`bsky.social` by default) with the identifier and app password. The access
  JWT is kept in memory only and renewed with
  `com.atproto.server.refreshSession` when the PDS answers `ExpiredToken`
  (signing in again if the refresh token has expired too).
- The PDS is the `#atproto_pds` service of your DID document (from the session
  answer, else `plc.directory` for `did:plc`, or `/.well-known/did.json` for
  `did:web`), so accounts on any PDS work.
- `com.atproto.repo.listRecords` pages each collection
  (`app.bsky.feed.post`, `app.bsky.feed.repost`, `app.bsky.feed.like`)
  newest-first, `BLUESKY_PAGE_SIZE` records per page. Each page is a
  checkpoint; the cursor keeps the newest record key delivered per collection
  and, mid-pass, the page cursor, so a crash resumes in place and a sync stops
  as soon as it meets a record already delivered.
- Liked and reposted posts are looked up with `app.bsky.feed.getPosts` (25 per
  request, through your PDS) so the like says *what* you liked.
- Notifications (`app.bsky.notification.listNotifications`) are read
  newest-first until the newest one already delivered. Only the reasons in
  `BLUESKY_NOTIFICATION_REASONS` are kept (default `mention,reply,quote`). If
  the AppView no longer serves notifications back to the previous run, a
  `partial` coverage report with reason `source_cap` records the gap.

## Channels

| Channel | Kind | What |
|---|---|---|
| `bluesky.posts` | life event | Your posts and replies. Content = text plus `[image: alt]`, `[video: alt]`, `[link: title url]`, `[quoting at://…]`. Thread = the reply root (a top-level post starts its own thread). |
| `bluesky.reposts` | reference | Posts you reposted, with their author and text when available. |
| `bluesky.likes` | reference | Posts you liked, likewise. |
| `bluesky.notifications` | life event | Replies, mentions and quotes by other people (only with `BLUESKY_NOTIFICATIONS=true`). Threaded by reply root. |

External ids are AT URIs (`at://did/collection/rkey`), globally unique;
notifications are `reason:uri`. Authors use system `bluesky`, kind `account`,
value = DID (stable across handle changes), display name = handle. Every
record has `author_kind` = `owner` (your record) or `other`.

Fields:

- posts: `author_kind`, `uri`, `rkey`, `cid`, `handle`, `is_reply`,
  `reply_parent_uri`, `reply_root_uri`, `quote_uri`, `embed_type`,
  `image_count`, `external_uri`, `link_uris`, `mention_dids`, `tags`, `langs`.
- likes / reposts: `author_kind`, `uri`, `rkey`, `cid`, `subject_uri`,
  `subject_cid`, `subject_author_did`, `subject_author_handle`,
  `subject_text`, `subject_available`, `subject_created_at`.
- notifications: `author_kind`, `uri`, `rkey`, `cid`, `reason`,
  `reason_subject`, `from_did`, `from_handle`, `is_read`, `indexed_at`,
  `reply_parent_uri`, `reply_root_uri`.

Links (method `source`, to `bluesky_post` entity placeholders keyed by AT
URI): `reply_to` (a reply to its parent), `quotes` (a quote post to the quoted
post), `reposted` and `liked` (to the subject).

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `BLUESKY_IDENTIFIER` | required | Handle or DID |
| `BLUESKY_APP_PASSWORD` | required | Secret: app password |
| `BLUESKY_SERVICE` | `https://bsky.social` | Where to sign in |
| `BLUESKY_PLC_URL` | `https://plc.directory` | PLC directory |
| `BLUESKY_HYDRATE` | `true` | Look up text of liked/reposted posts |
| `BLUESKY_NOTIFICATIONS` | `false` | Also read notifications |
| `BLUESKY_NOTIFICATION_REASONS` | `mention,reply,quote` | Reasons to keep (`like`, `repost`, `follow` also work) |
| `BLUESKY_PAGE_SIZE` | 100 | Records per request (1–100) |
| `BLUESKY_FIXTURE` | — | Tests only: offline PDS fixture |
| `THROUGHLINE_INTERVAL_S` | 1800 | Set to your timer's cadence |

## Limitations

- **Deletions and edits are not followed.** A post or like you delete after it
  was delivered stays in Throughline. Bluesky's post editing, where supported,
  rewrites the record in place; an edit of an already delivered post is not
  re-read.
- Sync stops at the newest record key already delivered. Records written with
  an older, back-dated key (repository imports) are only found by a fresh
  backfill (reset the cursor).
- Likes of feeds, lists or posts that were deleted, blocked or are otherwise
  not visible get `subject_available=false` and no text. Hydrated text is the
  text when the like was first delivered.
- Notifications come from the AppView, which keeps a limited history; replies
  older than that cannot be backfilled. Replies in threads you did not get
  notified about are not captured (the plugin reads your repository, not the
  network).
- Direct messages (chat.bsky.*) are not read.
- Built from the public lexicons and XRPC docs (atproto.com, docs.bsky.app);
  not verified against a live account. Fixtures are invented.

## Development

No dependencies beyond `throughline-sdk` and `httpx`. `throughline_bluesky.fake`
is an offline PDS/AppView (`httpx.MockTransport`) used by the unit tests and by
the contract kit through `BLUESKY_FIXTURE`:

```sh
THROUGHLINE_DEV=1 uv run pytest -q plugins/bluesky
```
