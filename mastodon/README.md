# throughline-mastodon

A live Throughline connector for **any Mastodon instance** (and servers that
implement the Mastodon client API). It polls your instance with a personal
access token and captures:

- your own statuses: posts, replies and reblogs (boosts),
- statuses you favourited,
- statuses you bookmarked,
- optionally, your notifications (mentions and replies by default).

The first run reads each stream from newest back to the very beginning
(`max_id` paging); later runs read forward from the newest item seen
(`min_id` paging). A checkpoint follows every page, so an interrupted run
resumes where it stopped. Dependencies: `throughline-sdk` only (httpx and
the stdlib `html.parser`).

## Channels

| Channel | What | Reference? |
| --- | --- | --- |
| `mastodon.statuses` | Your posts, replies and boosts (`is_reblog`) | no |
| `mastodon.favourites` | Statuses you favourited | yes (`is_reference`, outside) |
| `mastodon.bookmarks` | Statuses you bookmarked | yes (`is_reference`, outside) |
| `mastodon.notifications` | Notifications, when `MASTODON_NOTIFICATIONS=true` | no |

Status content is HTML converted to plain text: paragraphs and line breaks
are kept, links keep their target, `CW: <warning>` leads when the status has
a content warning, polls are listed as `Poll: a / b / c`, and each media
attachment becomes `[image: <description>]` (or `[video]` without one). A
boost reads `Boosted @user@host: <text>`.

Fields shared by status channels: `status_id`, `uri`, `url`, `visibility`,
`author_kind` (`owner` = written by you, `other`), `status_author`
(`user@host`), `is_reply`, `in_reply_to_id`, `spoiler_text`, `sensitive`,
`language`, `tags`, `mentions`, `links`, `media` (`[{type,url,description}]`),
`media_count`, `replies_count`, `reblogs_count`, `favourites_count`,
`edited_at`, `mapping_revision`.

- `mastodon.statuses` adds `is_reblog`, `reblog_of_id`, `reblog_of_author`,
  `application`.
- `mastodon.favourites` / `mastodon.bookmarks` add `time_basis`
  (always `status_created_at`, see Limitations).
- `mastodon.notifications`: `notification_id`, `notification_type`,
  `author_kind`, `status_author`, `status_id`, `url`, `visibility`,
  `in_reply_to_id`, `spoiler_text`, `mapping_revision`.

**Authors** use system `mastodon`, kind `acct`, value the fully qualified
`user@host` (lowercased; local accounts get your instance's domain) and the
display name, so the identity registry can attach Mastodon handles to people.

**External ids** are `<instance host>:<status id>` (notifications:
`<host>:notification:<id>`), because Mastodon ids are only unique per
instance. One thread per stream per account.

**Links** point at placeholder entities of kind `mastodon_status` with
`external_ref` `<host>:<status id>`, so the same status reached as a reply
parent, boost target, favourite or bookmark resolves to one entity:

| From | Type | To |
| --- | --- | --- |
| your reply | `reply_to` | the parent status |
| your boost | `reposted` (declared plugin type) | the boosted status |
| a favourite | `liked` | the status |
| a bookmark | `saved` | the status |
| a notification | `reply_to` / `cites` / `liked` / `reposted` | the status involved |

**Direct messages** (`visibility: direct`) get the access label
`third_party_confidence` (change with `MASTODON_DIRECT_LABEL`, empty
disables), which hides them from non-owner askers and keeps them away from
remote model providers by default. Their text is never copied into a
placeholder entity name.

## Setup

1. On your instance open **Preferences -> Development -> New application**.
2. Give it a name, and select the scopes `read:accounts`, `read:statuses`,
   `read:favourites`, `read:bookmarks` (and `read:notifications` if you
   enable notifications). Plain `read` also works. No write scopes needed.
3. Save, open the application and copy **Your access token**.
4. Create a scoped plugin token:
   `throughline token create mastodon --kind plugin --plugin mastodon --json`.

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `MASTODON_BASE_URL` | yes | | Your instance, e.g. `https://mastodon.example` |
| `MASTODON_ACCESS_TOKEN` | yes (secret) | | The application's access token |
| `MASTODON_CAPTURE` | no | `statuses,favourites,bookmarks` | Streams to read |
| `MASTODON_NOTIFICATIONS` | no | `false` | `true` adds `mastodon.notifications` |
| `MASTODON_NOTIFICATION_TYPES` | no | `mention` | Comma list: `mention,status,reblog,favourite,follow,follow_request,poll,update,quote` (replies to you are mentions) |
| `MASTODON_ACCT_DOMAIN` | no | host of the base URL | Handle domain when your server's `LOCAL_DOMAIN` differs from its web host |
| `MASTODON_DIRECT_LABEL` | no | `third_party_confidence` | Access label for direct messages |
| `MASTODON_BATCH_SIZE` | no | `40` | Items per page (Mastodon caps at 40) |
| `MASTODON_FIXTURE` | no | | Recorded-response JSON used instead of the network (tests) |

Plus the usual `THROUGHLINE_URL` / `THROUGHLINE_TOKEN`.

## Run

```sh
throughline-mastodon backfill          # full history
throughline-mastodon sync --once       # new items since the cursor (timer)
throughline-mastodon sync              # loop every 15 minutes
throughline-mastodon backfill --dry-run
```

`sync` on an empty cursor also reads the full history, so a timer running
`sync --once` is enough. The default interval is 900 s; set
`THROUGHLINE_INTERVAL_S` to your timer cadence. HTTP 429 and 5xx answers are
retried with the server's `Retry-After` / `X-RateLimit-Reset` (default limit:
300 requests per 5 minutes).

If the instance is unreachable (or verify_credentials fails) the run fails
and the source shows as failing. If only one stream fails (for example a
token without `read:bookmarks`), the others still import and that channel
gets a `partial` coverage report with reason `source_error`.

## Limitations

- **Favourite/bookmark time is not available.** The API never says when you
  favourited or bookmarked a status, so `happened_at` is the status's own
  post time and `time_basis` says so. Their cursor uses the opaque internal
  ids from the `Link` header, as Mastodon requires.
- **Edits and deletions after capture are not seen.** Items are read once;
  a later edit or delete of an already captured status is not re-fetched.
  Counts (`replies_count`, ...) are as of capture.
- **Reply parents are referenced, not fetched.** A reply links to its parent
  by instance id; the parent's text is only present if it is also one of
  your statuses, favourites or bookmarks.
- **Notifications are pruned by servers** (and filtered by your notification
  settings); the history reachable is whatever the server still keeps.
- Server-side limits may stop very old pages for some account timelines on
  some servers; the run then ends that stream's backfill at the last page
  served.
- Remote (federated) statuses' ids are your instance's ids for them;
  `uri` holds the global ActivityPub id.

## Tests

`tests/recorded/mastodon.json` is an invented recording (regenerate with
`python tests/recorded/generate.py tests/recorded/mastodon.json`). Unit
tests inject `httpx.MockTransport`; the contract-kit test runs the plugin
through `check_plugin` with `MASTODON_FIXTURE`.

```sh
THROUGHLINE_DEV=1 uv run pytest -q plugins/mastodon
```
