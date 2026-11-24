# Codex connector

Reads the session rollouts [OpenAI Codex](https://github.com/openai/codex) (CLI, IDE extension, or any app built on `codex app-server`) keeps on disk and delivers the conversations to Throughline, live and incrementally. Your prompts, Codex's replies and its reasoning summaries become searchable messages, one thread per session. Each tool call is summarised (tool name, count, patched files) on the reply that follows it. Tool output is never sent. Before anything leaves the plugin, strings that look like secrets are replaced.

Token counts and rate-limit events are not read. `plugins/t3code` already turns Codex rate limits into subscription usage records, and this plugin doesn't duplicate them.

## What it reads

The plugin reads every rollout under each configured home (default `~/.codex`, or `$CODEX_HOME`). Files are opened read-only.

```
sessions/YYYY/MM/DD/rollout-<time>-<session id>.jsonl       live sessions
sessions/YYYY/MM/DD/rollout-<time>-<session id>.jsonl.zst   older sessions, compressed by Codex
archived_sessions/rollout-...jsonl[.zst]                     archived sessions (CODEX_ARCHIVED)
session_index.jsonl                                          thread names (titles)
```

Rollouts are append-only JSONL. For each file name the cursor stores the byte offset after the last complete line and a fingerprint of the first line. The name has `.zst` stripped and no folder, so a session keeps its place when Codex archives it or compresses it: the decompressed stream has the same offsets. A sync reads only new lines. A line still being written waits for the next run. If a file is truncated or rewritten, it is read again from the start; replayed records have the same ids, so the core skips the ones that haven't changed. A compressed rollout's size and mtime are stored once it has been read to the end, so later runs don't decompress it again. An idle run writes no cursor.

### How lines become messages

Lines are `{timestamp, type, payload}`. Only the model conversation (`response_item`) is mapped. `event_msg` lines repeat the same items for the UI and are skipped, so nothing is counted twice.

| Item | Becomes |
|---|---|
| `session_meta` (first line) | Session id, parent, folder, originator, version, git branch, commit and remote |
| `turn_context` | Model, reasoning effort, folder and turn id of the messages that follow |
| `message`, role `user` | `role=user`, `author_kind=owner`; in a subagent session `agent_prompt` |
| `message`, role `assistant` | `role=assistant`, `author_kind=agent`, with `model`, `reasoning_effort`, `phase` (`commentary` / `final_answer`) |
| `reasoning` with a summary | `role=reasoning`, agent (turn off with `CODEX_REASONING=false`); encrypted reasoning is never read |
| `agent_message` (one agent writing to another) | `role=agent_message`, `agent_prompt`, with `agent_from` / `agent_to` |
| `compacted` with a summary | `role=compact_summary`, agent |
| `function_call`, `custom_tool_call`, `local_shell_call`, `*_call` | folded into the next reply: `tool_calls`, `tools` `{name: count}` (`namespace.name` when namespaced), `files` (paths from `apply_patch` patches) |
| Tool calls with no reply after them | one `role=tool_calls` record when the next prompt arrives |
| `developer`/`system` messages, injected context (`<environment_context>`, `<user_instructions>`, `# AGENTS.md instructions`, `<user_shell_command>`, any prompt wholly wrapped in one tag), tool outputs, events, token counts | skipped |

Rollouts from early 2025, which start with `{id, timestamp, instructions, git}` and hold bare items, are read too.

**Ids.** A message's `external_id` is `<session id>:<item id>`. Items without an id (early rollouts, compaction lines) use `<session id>:@<byte offset>` instead, which is stable because the files are append-only. Lines larger than 1 MiB that are events or tool outputs are skipped without being parsed, because single tool outputs can be tens of megabytes.

## Channels

### `codex.messages` (life event)

One thread per session, titled from `session_index.jsonl` (else the first prompt). Thread metadata: `session_id`, `project_dir`, `git_branch`, `originator`, `is_subagent`, `home`, `parent_session`.

| Field | Type | Meaning |
|---|---|---|
| `role` | text | `user`, `assistant`, `reasoning`, `agent_message`, `compact_summary`, `tool_calls` |
| `author_kind` | text | `owner`, `agent` or `agent_prompt` |
| `session_id`, `parent_session`, `is_subagent` | text/text/boolean | Session and its parent |
| `project_dir`, `git_branch` | text | Folder of the turn; branch at session start |
| `originator` | text | `codex_cli_rs`, `codex_vscode`, `codex_exec`, or the app driving `codex app-server` |
| `model`, `reasoning_effort`, `turn_id`, `phase` | text | Turn details |
| `tool_calls`, `tools`, `files` | integer/json/json | Tool work since the previous message |
| `image_count` | integer | Images attached to a prompt |
| `agent_from`, `agent_to` | text | Agents of an `agent_message` |
| `text_length`, `text_truncated` | integer/boolean | Stored text size (cut at 100,000 characters) |
| `redactions` | integer | Secret-shaped strings replaced in this record |
| `cli_version`, `mapping_revision` | text/integer | Provenance |

Authors are `{system: "codex", kind: "owner", value: "owner"}` for you (set `CODEX_OWNER_IDENTIFIER`, e.g. `email:email:you@example.com`), `codex/agent/codex` for Codex, and `codex/agent/orchestrator` for agent-written prompts.

### `codex.sessions` (reference)

One record per session. It is re-versioned when the rollout grows or its name in `session_index.jsonl` changes. `happened_at` is the first message and `ended_at` the last. Records are grouped into one thread per project folder. The fields are `session_id`, `parent_session`, `is_subagent`, `title`, `project_dir`, `git_branch`, `git_commit`, `git_repository`, `originator`, `source`, `cli_version`, `model_provider`, `owner_messages`, `agent_messages`, `agent_prompts`, `reasoning_summaries`, `tool_calls`, `models` (assistant messages by model) and `home`.

## Setup

No credentials are needed. Run the plugin on the machine where Codex runs, or on a machine that can read a synced copy of the home.

```sh
throughline token create codex --kind plugin --plugin codex --json
export THROUGHLINE_URL=http://127.0.0.1:8765 THROUGHLINE_TOKEN=...
uv run throughline-codex backfill --dry-run   # count what would be sent
uv run throughline-codex backfill
uv run throughline-codex sync --once          # from a timer, e.g. every 15 minutes
uv run throughline-codex sync                 # or run continuously
```

| Variable | Default | Meaning |
|---|---|---|
| `CODEX_HOMES` | `local=$CODEX_HOME` or `local=~/.codex` | Homes to read, `LABEL=PATH,LABEL=PATH` |
| `CODEX_ARCHIVED` | `true` | Also read `archived_sessions/` |
| `CODEX_ORIGINATORS` | `all` | Keep only sessions from these originators, comma-separated |
| `CODEX_REASONING` | `true` | Keep reasoning summaries |
| `CODEX_SCRUB` | `true` | Replace secret-shaped strings |
| `CODEX_OWNER_IDENTIFIER` | `codex:owner:owner` | Your author identifier, `system:kind:value` |
| `CODEX_OWNER_NAME` | unset | Display name on your messages |
| `CODEX_BATCH_SIZE` | `500` | Messages between cursor checkpoints |
| `THROUGHLINE_INTERVAL_S` | `900` | Set to your timer's period so health checks match |

Changing a mapping setting (owner identifier or name, reasoning, scrubbing, originators) resets the cursor. Everything is then read again, and records whose mapping changed get new versions.

### Sessions run by other apps

Apps that drive Codex through `codex app-server`, such as T3 Code, write their sessions to the same folder, with their name in `originator`. If such an app has its own Throughline connector (for example `plugins/t3code`), its conversations would show up twice. Keep only the clients you want with `CODEX_ORIGINATORS=codex_cli_rs,codex_vscode`.

## Dependency

`zstandard` decompresses the `.jsonl.zst` rollouts Codex writes for older sessions, which on a long-used machine are most of the history. Python's standard library has no zstd support before 3.14. On 3.14 and later the plugin uses the standard library's `compression.zstd` instead.

## Secret scrubbing

Every string in a record (content, fields, payload, thread title and metadata) goes through a scrubber before it is sent. These shapes are replaced with `[REDACTED:<kind>]`: private key blocks, Anthropic, OpenAI, Stripe, GitHub, GitLab, AWS, Slack, Google API and Hugging Face keys, JWTs, `Bearer`/`token` header values, passwords in URLs, and values of `name=value` assignments whose name contains key, secret, token, password or credential. The `redactions` field counts the replacements. Scrubbing is pattern matching: it misses credentials with no recognisable shape and sometimes catches harmless text.

## Limitations

- Only what is on disk: deleted sessions, and sessions from machines whose home isn't configured, can't be recovered.
- The rollout format is internal to Codex and changes between versions. Unknown line and item types are ignored, never fatal.
- Reasoning is only available as Codex's summaries. The full reasoning is stored encrypted and is never read.
- Messages from early rollouts have no per-line timestamps and carry the session's start time. Their model is unknown because those files have no `turn_context`.
- `~/.codex/history.jsonl` (a log of prompts) isn't read, since the same prompts are in the rollouts.
- A thread title that changes after the last message reaches Throughline with the session record at once, but on the message thread only with the next message.
- Forked sessions copy their history into a new rollout. Those copies become separate messages in the new thread.
