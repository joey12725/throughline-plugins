# throughline-aider

A Throughline source for [aider](https://aider.chat) sessions. aider appends every session to `.aider.chat.history.md` in the repository it runs in. This plugin finds those files under folders you choose and follows them as they grow.

## What it captures

| Channel | Contents |
|---|---|
| `aider.messages` | Your inputs (`author_kind` `owner`), the model's replies (`agent`, with the model aider reported) and aider's own tool output such as edits applied, commits and command output (`other`). One thread per aider session. |

Fields: `role` (user/assistant/tool), `author_kind`, `model`, `repo`, `history_file`, `session_id`, `session_started_at`, `ordinal`, `is_command` (`/add`, `/run`, ...), `aider_version`, `time_estimated`, `text_length`, `text_truncated`, `mapping_revision`.

Authors: your inputs use `AIDER_OWNER_IDENTIFIER`, replies are `{system: "aider", kind: "agent", value: <model>}`, and tool output is `{system: "aider", kind: "tool"}`.

## How the file is parsed

```
# aider chat started at 2024-05-01 10:00:00      one header per session

> Aider v0.50.0                                  "> " lines: aider tool output
> Main model: gpt-4o with diff edit format

#### your prompt                                 "#### " lines: your input
#### second line of it

The model's reply, until the next "####" or "> " line.
```

- The banner at the start of a session (version, model, repo map) is kept as session metadata, not as messages. Later `Model:` or `Main model:` lines (after `/model`) update the model for the replies that follow.
- `#### <blank>` (an empty Enter) is skipped.
- **Times.** The history file only records when each session started. If `.aider.input.history` sits next to it (aider writes it by default), each of your inputs is matched to its timestamped entry there by text. All other messages, and inputs that cannot be matched, are placed one second after the previous message and flagged `time_estimated`. Header times are local and naive, so they are read in `AIDER_TIMEZONE` (default: this machine's zone).
- **Ids.** The file has no ids. A session's id is a hash of the file path, the byte offset of its header and the header line. A message's id is the session id plus its position in the session. The file is append-only, so ids are stable, and a message whose text changes is recorded as a new version, not a duplicate.

## Incremental reads

The cursor keeps a byte offset per history file. Earlier sessions are never read again. Each file's **last** session is re-read on every run, because aider may still be appending to it. Re-sent messages are no-ops in the core. If a file shrinks or no longer starts with a session header at the stored offset (it was truncated or replaced), it is read again from the start.

## Setup

1. Install Throughline and this plugin (`uv sync --all-packages` in the workspace).
2. Create a plugin token: `throughline token create aider --kind plugin --plugin aider --json`.
3. Point `AIDER_ROOTS` at the folders that hold your repositories.

| Variable | Default | Meaning |
|---|---|---|
| `THROUGHLINE_URL`, `THROUGHLINE_TOKEN` | (required) | Core URL and plugin token |
| `AIDER_ROOTS` | (required) | Folders to search, or history files themselves, separated by `:` (`;` on Windows) |
| `AIDER_SCAN_DEPTH` | `3` | Folder levels below each root to search (`.git`, `node_modules`, `.venv`, ... are skipped) |
| `AIDER_HISTORY_NAME` | `.aider.chat.history.md` | Chat history file name (if you use `--chat-history-file`) |
| `AIDER_INPUT_HISTORY_NAME` | `.aider.input.history` | Input history next to it; empty to ignore |
| `AIDER_TIMEZONE` | this machine's | IANA zone aider ran in, e.g. `Europe/Berlin` |
| `AIDER_INCLUDE_TOOL_OUTPUT` | `true` | Also record aider's tool output |
| `AIDER_BATCH_SIZE` | `500` | Messages per checkpoint (whole sessions) |
| `AIDER_OWNER_IDENTIFIER` | `aider:owner:owner` | Your author identifier, `system:kind:value` (e.g. `email:address:you@example.com`) |
| `AIDER_OWNER_NAME` | none | Display name on your inputs |

```sh
throughline-aider backfill
throughline-aider sync --once     # for a timer
throughline-aider sync            # run forever, every 15 minutes
```

## Limitations

- Reply times are estimates (aider records none). Input times are exact only when the input history is available.
- A markdown quote (`> ...`) inside a model reply is indistinguishable from tool output and is recorded as tool output. A multi-line tool message continues on lines without the `> ` prefix, and those lines are read as reply text.
- Moving a repository changes the history file path, so its sessions are recorded again under new ids.
- Deleting or rewriting history files does not delete records in Throughline.
- The format is taken from aider's source (`io.py`: `user_input`, `ai_output`, `tool_output`, and the `# aider chat started at` header) and covered by invented fixtures. No real user history was used.
