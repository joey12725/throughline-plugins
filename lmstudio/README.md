# throughline-lmstudio

A Throughline source for [LM Studio](https://lmstudio.ai) chats with local models. LM Studio saves each conversation as a JSON file. This plugin reads those files as LM Studio updates them.

## What it captures

| Channel | Contents |
|---|---|
| `lmstudio.messages` | One message per turn version: your messages (`author_kind` `owner`) and model replies (`agent`, with the model). Regenerated replies and edited turns are kept as separate versions, and `is_selected` marks the one LM Studio shows. One thread per conversation. |

Fields: `role`, `author_kind`, `model`, `conversation_file`, `folder`, `chat_title`, `ordinal`, `version_index`, `version_count`, `is_selected`, `time_source`, `prompt_tokens`, `predicted_tokens`, `tokens_per_second`, `file_count`, `has_reasoning`, `text_length`, `text_truncated`, `mapping_revision`. The conversation's system prompt goes in the thread metadata.

Authors: your messages use `LMSTUDIO_OWNER_IDENTIFIER`, and replies are `{system: "lmstudio", kind: "agent", value: <model identifier>}`.

## Where the data comes from

`~/.lmstudio/conversations/**/*.conversation.json` (LM Studio 0.3 and later) and the older `~/.cache/lm-studio/conversations/`. Both are scanned when they exist, including subfolders, which LM Studio uses for its chat folders. On Windows, `~` is your user profile folder. Files are only read, never written.

Shapes understood:

- `messages[]` of `{versions: [...], currentlySelected}`.
  - A `singleStep` version has `role` and `content` parts: `text`, and `file`/`image`, which are counted.
  - A `multiStep` assistant version has `steps[]`. Text comes from `contentBlock` steps. Blocks styled `thinking` are left out of the text and noted as `has_reasoning`. Tool-call and debug steps are ignored.
- Older flat messages `{role, content}`.
- The model comes from `genInfo.indexedModelIdentifier`/`identifier`, then `senderInfo.senderName`, then the conversation's `lastUsedModel`. Token statistics come from `genInfo.stats`.

The format is undocumented and has changed between releases. Unknown shapes are **skipped and counted** (`"lmstudio": {"unknown_shapes": {...}}` in the run report, plus one log warning), never fatal. A file that is not valid JSON, for example one LM Studio is writing at that moment, is skipped and retried on the next run (`unreadable`).

## Times

LM Studio stores the conversation's `createdAt` but **no per-message times**. `time_source` says where each time came from:

| `time_source` | Meaning |
|---|---|
| `message` | The message carried its own time (`timestamp`/`createdAt`/`sentAt`), if a release writes one |
| `created` | Turns present when the plugin first read the conversation: `createdAt` plus one second per turn |
| `seen` | Turns added after the plugin first read the conversation: the file's modification time when the plugin first saw them. This is accurate to within your poll interval. |
| `file` | No `createdAt`: the file's modification time |

The times for `seen` turns live in the plugin's cursor. If you reset the cursor and backfill again, those turns are re-timed as `created`, and their records get new versions.

## Change detection

The cursor keeps each file's modification time, size and content hash. An untouched file is not read. A touched but identical file is not mapped. A changed file re-sends all its messages: unchanged ones are no-ops, and edited or re-selected ones become new versions. Ids are `lmstudio:<file stem>:<turn>.<version>`. LM Studio names files by their creation time, so moving a chat into a folder keeps its ids.

## Setup

1. Install Throughline and this plugin (`uv sync --all-packages` in the workspace).
2. Create a plugin token: `throughline token create lmstudio --kind plugin --plugin lmstudio --json`.
3. Run it on the machine where LM Studio is installed.

| Variable | Default | Meaning |
|---|---|---|
| `THROUGHLINE_URL`, `THROUGHLINE_TOKEN` | (required) | Core URL and plugin token |
| `LMSTUDIO_CONVERSATIONS_DIRS` | the two folders above | Conversation folders, separated by `:` (`;` on Windows) |
| `LMSTUDIO_BATCH_SIZE` | `500` | Messages per checkpoint (whole conversations) |
| `LMSTUDIO_OWNER_IDENTIFIER` | `lmstudio:owner:owner` | Your author identifier, `system:kind:value` |
| `LMSTUDIO_OWNER_NAME` | none | Display name on your messages |

```sh
throughline-lmstudio backfill
throughline-lmstudio sync --once   # for a timer
throughline-lmstudio sync          # run forever, every 15 minutes
```

## Limitations

- Message times are estimates (see above).
- If a turn is deleted in LM Studio, its old records stay in Throughline.
- Attachments are counted, not imported. Tool-call steps are not recorded.
- **The format was reconstructed from public descriptions of LM Studio's files, not from a captured real conversation.** The tests use invented files.
