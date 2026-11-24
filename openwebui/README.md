# throughline-openwebui

A Throughline source for [Open WebUI](https://openwebui.com). It polls your Open WebUI server's REST API with your API key, so new chats and replies arrive on every run.

## What it captures

| Channel | Contents |
|---|---|
| `openwebui.messages` | Every message node of every chat, on **every branch**: regenerated replies and edited prompts are sibling nodes in Open WebUI's message tree, and all are kept. `is_current_path` marks the branch the chat currently shows. One thread per chat. |

Your messages have `author_kind` `owner` and use `OPENWEBUI_OWNER_IDENTIFIER`. Model replies have `author_kind` `agent` and the author `{system: "openwebui", kind: "agent", value: <model id>, display_name: <model name>}`.

Fields: `role` (user/assistant/system), `author_kind`, `model`, `model_name`, `chat_id`, `message_id`, `parent_id`, `is_current_path`, `branch_index`, `sibling_count`, `chat_title`, `tags`, `archived`, `pinned`, `folder_id`, `rating` (your thumbs up/down: 1 or -1), `has_error`, `file_count`, `input_tokens`, `output_tokens`, `text_length`, `text_truncated`, `mapping_revision`.

## Setup

1. In Open WebUI, open **Settings > Account > API keys** and create a key. An admin may need to enable API keys under **Admin Panel > Settings > General**, and, if endpoint restrictions are on, allow `/api/v1/chats`.
2. Create a plugin token: `throughline token create openwebui --kind plugin --plugin openwebui --json`.
3. Run the plugin anywhere that can reach the server.

| Variable | Default | Meaning |
|---|---|---|
| `THROUGHLINE_URL`, `THROUGHLINE_TOKEN` | (required) | Core URL and plugin token |
| `OPENWEBUI_URL` | (required) | Base URL of your server, e.g. `https://chat.example.com` |
| `OPENWEBUI_API_KEY` | (required, secret) | The API key from step 1 |
| `OPENWEBUI_INCLUDE_ARCHIVED` | `true` | Also import archived chats |
| `OPENWEBUI_BATCH_SIZE` | `25` | Chats per checkpoint |
| `OPENWEBUI_TIMEOUT_S` | `30` | HTTP timeout |
| `OPENWEBUI_OWNER_IDENTIFIER` | `openwebui:owner:owner` | Your author identifier, `system:kind:value` (e.g. `email:address:you@example.com`) |
| `OPENWEBUI_OWNER_NAME` | none | Display name on your messages |
| `OPENWEBUI_FIXTURE` | none | Testing only: answer from a recorded JSON file instead of the network |

```sh
throughline-openwebui backfill
throughline-openwebui sync --once   # for a timer
throughline-openwebui sync          # run forever, every 15 minutes
```

## How it works

Endpoints:

- `GET /api/v1/chats/?page=N` lists chats newest-updated first, one page at a time.
- `GET /api/v1/chats/pinned` lists pinned chats. Newer releases leave them out of the main list; a 404 is ignored.
- `GET /api/v1/chats/archived?page=N` lists archived chats; a 404 is ignored and reported as `archived_unsupported`.
- `GET /api/v1/chats/{id}` returns one chat with `chat.history.messages` (a tree: `parentId`, `childrenIds`) and `chat.history.currentId`. Very old releases with only a linear `chat.messages` list are read as a single branch.

**Incremental.** The cursor keeps the `updated_at` of the newest chat delivered. Each run pages through the list until a page holds nothing updated at or after that mark, then fetches only those chats, oldest first, and checkpoints every `OPENWEBUI_BATCH_SIZE` chats. Every node of a changed chat is re-sent. Unchanged nodes are no-ops. A node whose content or `is_current_path` changed (you picked another branch, or regenerated) becomes a new version.

**Ids.** Ids are Open WebUI's own: message `<chat id>:<message id>`, thread `<chat id>`.

**Failures.** A rejected key (401/403) or an unreachable server fails the run, so the source turns `failing` and then `down`. 429 and 5xx responses are retried twice with backoff. A chat deleted between listing and fetching is skipped and counted (`vanished_chats`).

No third-party dependencies beyond `httpx`, which the SDK already uses.

## Limitations

- Times are the node's `timestamp` (seconds). Nodes without one use the chat's creation time.
- Deleting a chat or a message in Open WebUI does not delete it in Throughline. Its records stay.
- Only chats of the API key's user are visible. Shared chats, channels and notes are not imported.
- Attached files and images are counted (`file_count`), not downloaded. Citations, status history and tool output are not recorded.
- **API shape assumed from Open WebUI's source and docs, not verified against a live server.** That covers the list pagination, the `pinned` and `archived` routes, the message tree keys and the usage keys `usage.prompt_tokens`/`completion_tokens` and Ollama's `info.prompt_eval_count`/`eval_count`. The tests use an invented recording through `httpx.MockTransport`.
