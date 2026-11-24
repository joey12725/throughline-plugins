# Throughline iMessage plugin

Imports iMessage, SMS and RCS conversations from the macOS Messages app by
reading its local database, `~/Library/Messages/chat.db`, which Messages keeps
up to date (including messages your iPhone syncs through Messages in iCloud).
It is a live source: every run picks up new messages, tapbacks, edits and
unsends since the previous run. The database is opened **read-only** (a
`mode=ro` SQLite URI, falling back to `immutable=1`); the plugin never writes to it.

It runs on the Mac. No dependencies beyond the Throughline SDK.

## What it captures

One channel, `imessage.messages`, one thread per Messages chat (one-to-one or
group). Text comes from `message.text`, or, on newer macOS where `text` is
NULL, from the `attributedBody` blob (an archived `NSAttributedString` in
Apple's typedstream format) through a built-in decoder for the common
single-string case.

- **Tapbacks** (`associated_message_type` 2000–2007, removals 3000–3007) never
  become messages of their own. They are folded into the target message's
  `reaction_details` field and a `Reactions:` line in its content; a late
  tapback on an older message produces a new version of that message.
- **Inline replies** (`thread_originator_guid`) set `reply_to` and a
  `reply_to` link.
- **Edits / unsends** (`date_edited`, `date_retracted`, macOS 13+) set
  `edited_at` / `unsent_at`; an unsent message's content is `[unsent message]`.
  Edits to messages delivered in earlier runs are picked up by a scan of
  those columns and produce new versions.
- **Attachments** are metadata only (file name, path, MIME type, UTI, size);
  files are not copied.
- **Group events** (renames, members added/removed) become short system
  messages with `event_kind`.

| Field | Type | Meaning |
|---|---|---|
| `direction` | text | `incoming` / `outgoing` |
| `is_from_me` | boolean | `message.is_from_me` |
| `author_kind` | text | `owner` (you) or `other` |
| `service` | text | `iMessage`, `SMS` or `RCS` |
| `conversation_kind` | text | `dm` or `group_dm` |
| `chat_guid` | text | Messages chat GUID |
| `participants` | json | Other handles in the chat |
| `sender_handle` | text | Sender's handle for incoming messages |
| `reply_to` | text | External ID of the message replied to |
| `edited_at` / `unsent_at` | timestamp | Last edit / unsend time |
| `attachments` | json | `[{guid, transfer_name, filename, mime_type, uti, total_bytes, is_sticker?}]` |
| `reaction_details` | json | Current tapbacks `[{emoji, kind, sender_id? \| sender_kind: "owner", at}]` |
| `event_kind` | text | `membership`, `rename`, `group_update`, `system` |
| `app_bundle` / `send_effect` | text | iMessage app balloon id / send effect id |

Authors use identity schemes the registry understands: handles that are phone
numbers become `{system: "phone", kind: "number"}`, Apple ID emails become
`{system: "email", kind: "email"}`. Your own messages are authored by the
handle Messages recorded as yours (`destination_caller_id` / `account`), or by
`IMESSAGE_OWNER_HANDLE`.

External IDs are `imessage:<message GUID>` (GUIDs are globally unique);
thread IDs are `imessage:chat:<chat GUID>`. Times are Apple-epoch values
(nanoseconds since 2001-01-01 on macOS 10.13+, seconds before), emitted as UTC.

## Setup

1. Turn on **Messages in iCloud** (Messages → Settings → iMessage) if you want
   your phone's history on the Mac, and leave Messages signed in.
2. Grant **Full Disk Access** to whatever runs the plugin: System Settings →
   Privacy & Security → Full Disk Access → add your terminal app (Terminal,
   iTerm), or the `uv`/`python` binary used by a launchd job. Without it macOS
   refuses to open `chat.db` and the run fails with a message saying so.
3. Create a plugin token: `throughline token create imessage --kind plugin --plugin imessage`.

| Environment variable | Default | Meaning |
|---|---|---|
| `THROUGHLINE_URL` / `THROUGHLINE_TOKEN` | – | Core origin and token |
| `IMESSAGE_DB` | `~/Library/Messages/chat.db` | Database path (read-only) |
| `IMESSAGE_OWNER_HANDLE` | – | Your phone/email for authoring your messages when chat.db lacks it |
| `IMESSAGE_BATCH_SIZE` | `1000` | Messages per checkpointed batch |

## Run

```sh
uv run --package throughline-imessage throughline-imessage backfill --dry-run
uv run --package throughline-imessage throughline-imessage backfill
uv run --package throughline-imessage throughline-imessage sync          # every 5 minutes
uv run --package throughline-imessage throughline-imessage sync --once   # under launchd
```

Under a launchd `StartInterval` job, set `THROUGHLINE_INTERVAL_S` to the same
interval. The cursor is `{rowid, changed_hw}`: the highest message ROWID
delivered and the newest edit/unsend time already scanned.

## Limitations

- macOS only (the database lives on the Mac). Messages that only exist on an
  iPhone without Messages in iCloud are not visible.
- The `attributedBody` decoder handles the common single-`NSString` archive;
  exotic archives fall back to attachment labels or `[empty message]`.
- Edit history (`message_summary_info`) is not decoded; only the current text
  and the last edit time are kept.
- Messages deleted locally disappear from chat.db; already-imported copies
  stay in Throughline.
- Contact names are not in chat.db (they live in the Contacts database);
  people are resolved from phone numbers and emails.
- Tested against a synthetic database built from the documented schema
  (the tests run on Linux), not a real chat.db.
