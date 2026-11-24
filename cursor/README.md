# throughline-cursor

A Throughline source for [Cursor](https://cursor.com)'s AI chat and composer (agent) history. It reads the SQLite stores Cursor keeps on your machine, so it stays live while Cursor runs: every poll picks up new prompts and replies. No export step and no account credentials are needed.

## What it captures

One message per prompt you typed and per assistant reply, with one thread per conversation (a composer, or a chat tab in the older chat panel).

| Channel | Contents |
|---|---|
| `cursor.messages` | Your prompts (`author_kind` `owner`) and model replies (`author_kind` `agent`, with the model name) |

Fields: `role` (user/assistant), `author_kind`, `model`, `chat_id`, `bubble_id`, `title`, `mode` (agent, chat, edit, ...), `workspace` (the project folder, when Cursor recorded it), `source_format` (which store shape the record came from), `time_estimated`, `input_tokens`, `output_tokens`, `text_length`, `text_truncated`, `mapping_revision`.

Assistant authors look like `{system: "cursor", kind: "agent", value: <model>}`. Your own prompts use `CURSOR_OWNER_IDENTIFIER`. Set it to an identity scheme the registry knows, such as `email:address:you@example.com`, so your prompts resolve to you.

## Where the data comes from

Cursor's `User` directory:

| OS | Default |
|---|---|
| Linux | `~/.config/Cursor/User` (respects `XDG_CONFIG_HOME`) |
| macOS | `~/Library/Application Support/Cursor/User` |
| Windows | `%APPDATA%\Cursor\User` |

The plugin understands these shapes:

- `globalStorage/state.vscdb`, table `cursorDiskKV`:
  - `composerData:<composerId>` with an inline `conversation` list (composer `_v` 1-2), or
  - `composerData:<composerId>` with `fullConversationHeadersOnly`, whose messages are stored under `bubbleId:<composerId>:<bubbleId>` (`_v` 3 and later). Bubble `type` 1 is you and 2 is the assistant. Text comes from `text`, or from the Lexical `richText` when `text` is empty.
- `workspaceStorage/<hash>/state.vscdb`, table `ItemTable`:
  - `composer.composerData`: maps composers to the workspace folder named in `workspace.json`. Very old releases kept whole conversations here, and those are read too.
  - `workbench.panel.aichat.view.aichat.chatdata`: the chat panel from before composer (`tabs[].bubbles[]`, types `user` and `ai`).
  - `aiService.prompts`: prompt history. It has no timestamps or ids, so entries are counted (`untimed_prompt`) and skipped unless they carry a time.

The databases are live, locked and in WAL mode. On each run every store and its `-wal` file is **copied to a temporary directory** and read there. The plugin never opens or writes Cursor's own files. When the store files' modification times and sizes have not changed since the last run, nothing is copied.

### Undocumented, changing format

Cursor does not document this format and changes it between releases. Every value is parsed on its own. A shape the plugin does not recognise is **skipped and counted, never fatal**. The run report (`"cursor": {"unknown_shapes": {...}, "skipped": {...}}`) and a single log warning list how many records of each kind were skipped. If those counts grow after a Cursor update, the format has changed. Please open an issue with the shape, stripped of any content.

Skipped on purpose and counted: assistant bubbles with no text (`tool_call`, `thinking`), headers whose bubble is missing (`missing_bubble`), and empty composers.

## Setup

1. Install Throughline and this plugin (`uv sync --all-packages` in the workspace).
2. Create a plugin token: `throughline token create cursor --kind plugin --plugin cursor --json`.
3. Run it on the machine where Cursor is installed.

| Variable | Default | Meaning |
|---|---|---|
| `THROUGHLINE_URL`, `THROUGHLINE_TOKEN` | (required) | Core URL and plugin token |
| `CURSOR_USER_DIR` | per OS, above | Cursor's `User` directory (holds `globalStorage/` and `workspaceStorage/`) |
| `CURSOR_BATCH_SIZE` | `500` | Messages per checkpoint (whole conversations) |
| `CURSOR_OWNER_IDENTIFIER` | `cursor:owner:owner` | Your author identifier, `system:kind:value` |
| `CURSOR_OWNER_NAME` | none | Display name on your prompts |
| `THROUGHLINE_INTERVAL_S` | `900` | Set it to your timer's cadence |

```sh
throughline-cursor backfill            # everything currently in the stores
throughline-cursor sync --once         # one incremental run (for a timer)
throughline-cursor sync                # run forever, every 15 minutes
throughline-cursor backfill --dry-run  # count without writing
```

## How sync works

Each conversation's mapped messages are fingerprinted, and the cursor keeps one short hash per conversation. A conversation is re-sent only when its fingerprint changed: a new message, an edit, or a rename. All of its messages are re-sent, so unchanged ones are no-ops in the core and changed ones become new versions. Ids come from Cursor: `composer:<composerId>:<bubbleId>` and `chat:<tabId>:<bubbleId>`.

## Limitations

- **Times.** Newer bubbles carry `createdAt` or `timingInfo.clientStartTime`. Older shapes often keep only the conversation's creation time, or, for legacy chat tabs, only the last send. Messages without their own time are placed one second after the previous message (or ending at the last send) and flagged `time_estimated`.
- **Deletes.** A conversation you delete in Cursor is not deleted in Throughline.
- **Not synced across machines.** Cursor stores chats per machine. Run the plugin on each machine. Composer ids are UUIDs, so records from different machines never collide.
- **Content.** Only prompt and reply text is kept, cut at 100,000 characters. Attached code context, diffs and tool-call arguments are not recorded.
- **No real-sample verification.** The shapes above come from public descriptions of Cursor's stores. The tests use synthetic databases, not a captured Cursor profile.
