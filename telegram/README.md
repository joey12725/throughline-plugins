# Throughline Telegram plugin

Imports your Telegram private chats and small groups live through Telegram's
MTProto **user** API, using [Telethon](https://github.com/LonamiWebs/Telethon).
It logs in as you (not as a bot), so it sees the same history as your apps.
Backfill walks every included dialog's full history, oldest first; later runs
fetch only newer messages and re-read a recent window so edits and reactions
become new versions.

Dependency: `telethon` (the Bot API cannot read your own chats, so a user-API
MTProto client is required; Telethon is the maintained pure-Python one).

## What it captures

One channel, `telegram.messages`, one thread per dialog
(`telegram:<your user id>:<dialog id>`). Message ids are
`telegram:<your user id>:<dialog id>:<message id>`.

By default: private chats, Saved Messages, basic groups and supergroups with
at most `TELEGRAM_MAX_GROUP_SIZE` members (50). Broadcast channels, bots and
larger groups are skipped unless enabled; `TELEGRAM_INCLUDE` /
`TELEGRAM_EXCLUDE` override per dialog.

| Field | Type | Meaning |
|---|---|---|
| `direction` | text | `incoming` / `outgoing` |
| `author_kind` | text | `owner` (you) or `other` |
| `conversation_kind` | text | `dm`, `bot`, `group_dm`, `channel` |
| `dialog_id` | text | Marked peer id |
| `telegram_message_id` | integer | Id inside the dialog |
| `sender_is_bot` | boolean | Sent by a bot account |
| `sender_username` | text | Public @username |
| `reply_to` | text | External ID of the replied-to message (also a `reply_to` link) |
| `forwarded_from` | json | `{from_id, from_name, date, channel_post}` |
| `edited_at` | timestamp | Last edit time |
| `media` | json | `{type, name, mime_type, size, duration, width, height, lat, lon}`; files are not downloaded |
| `album_id` | text | Shared by the messages of one album |
| `service_action` | text | `MessageAction*` type for joins, renames, calls, pins |
| `reaction_details` | json | `[{emoji, sender_id, at}]` recent reactors, or `[{emoji, count}]` totals |

Authors are `{system: "telegram", kind: "user_id", value: "<id>"}` with the
display name.

## Setup

1. Create an application at <https://my.telegram.org> → *API development
   tools*; note `api_id` and `api_hash`.
2. Log in once on a terminal and copy the printed session string:

   ```sh
   TELEGRAM_API_ID=12345 TELEGRAM_API_HASH=... \
     uv run --package throughline-telegram throughline-telegram-login
   ```

   It asks for your phone number, the login code Telegram sends, and your
   2FA password if set. The session string is a full login to your account:
   store it only as a secret, and revoke it under Settings → Devices if it leaks.
3. Configure:

| Environment variable | Default | Meaning |
|---|---|---|
| `THROUGHLINE_URL` / `THROUGHLINE_TOKEN` | – | Core origin and token |
| `TELEGRAM_API_ID` | – | App api_id |
| `TELEGRAM_API_HASH` | – | Secret: app api_hash |
| `TELEGRAM_SESSION` | – | Secret: StringSession from `throughline-telegram-login` |
| `TELEGRAM_INCLUDE_CHANNELS` | `0` | `1` imports broadcast channels too |
| `TELEGRAM_INCLUDE_BOTS` | `0` | `1` imports bot chats |
| `TELEGRAM_MAX_GROUP_SIZE` | `50` | Larger groups are skipped; `0` = no limit |
| `TELEGRAM_INCLUDE` / `TELEGRAM_EXCLUDE` | – | Comma-separated dialog ids or @usernames |
| `TELEGRAM_RESCAN_DAYS` | `3` | Recent window re-read each run for edits/reactions (widened after outages) |
| `TELEGRAM_BATCH_SIZE` | `500` | Messages per request and checkpoint |
| `TELEGRAM_RECORDING` | – | Invented JSON recording instead of Telegram (tests only) |

## Run

```sh
uv run --package throughline-telegram throughline-telegram backfill --dry-run
uv run --package throughline-telegram throughline-telegram backfill
uv run --package throughline-telegram throughline-telegram sync      # every 15 minutes
```

The cursor is `{dialogs: {<dialog id>: <highest message id>}}`. A large first
backfill is checkpointed per page and resumes where it stopped; Telethon waits
out Telegram `FLOOD_WAIT` limits of up to 5 minutes automatically.

## Limitations

- Deletions are not detected (polling never sees them); deleted messages stay
  in Throughline.
- Edits and reaction changes are only noticed inside the rescan window, and
  only for dialogs with activity in that window.
- Secret chats are device-bound and never visible to the API.
- Media are metadata only. Replies to a message in another chat keep the ids
  in the payload but get no link.
- Tests use a Telethon look-alike built from an invented recording; the
  attribute shapes follow Telethon 1.x documentation and were not checked
  against a live account here.
