# Claude Code connector

Reads the conversation transcripts [Claude Code](https://docs.anthropic.com/en/docs/claude-code) keeps on disk and delivers them to Throughline, live and incrementally. Your prompts and Claude's replies become searchable messages, one thread per session. Each tool call is summarised (tool name, count, files touched) on the reply that follows it. Tool output is never sent. Before anything leaves the plugin, strings that look like secrets are replaced.

> **Claude Code deletes old transcripts.** By default it removes session files older than 30 days when it starts (`cleanupPeriodDays`, default `30`). This plugin can only deliver what is still on disk, and a run after a long gap cannot get back sessions that have already been cleaned up. Raise the limit in `~/.claude/settings.json`:
>
> ```json
> { "cleanupPeriodDays": 3650 }
> ```
>
> Use a large number, not `0`: in recent versions `0` turns off saving transcripts altogether. Records already in Throughline stay there when Claude Code deletes the files.

## What it reads

The plugin reads every transcript under each configured home (default `~/.claude`, or `$CLAUDE_CONFIG_DIR`). Files are opened read-only.

```
projects/<encoded cwd>/<session id>.jsonl                          one session
projects/<encoded cwd>/<session id>/subagents/agent-<id>.jsonl     a subagent (Task/Agent tool)
projects/<encoded cwd>/<session id>/subagents/agent-<id>.meta.json subagent type and description
projects/<encoded cwd>/<session id>/subagents/workflows/<run>/agent-<id>.jsonl
```

Transcripts are append-only JSONL. For each file the cursor stores the byte offset after the last complete line and a fingerprint of the first line. A sync reads only new lines. A line still being written (no trailing newline) waits for the next run. If a file is truncated or rewritten, it is read again from the start; replayed records have the same ids, so the core skips the ones that haven't changed. When a file is deleted, its cursor entry is dropped too. An idle run writes no cursor at all.

### How lines become messages

| Line | Becomes |
|---|---|
| Your prompt (`type: user`, text) | `role=user`, `author_kind=owner` |
| A slash command (`<command-name>/x</command-name>`) | `role=command`, content `/x args`, owner |
| A `!` shell command (`<bash-input>`) | `role=shell`, content `! cmd`, owner; its output is skipped |
| Assistant text block | `role=assistant`, `author_kind=agent`, with `model` |
| A subagent's final `StructuredOutput` | `role=structured_output`, agent, the structured answer as JSON |
| User-role text in a subagent transcript (the brief the parent agent wrote) | `author_kind=agent_prompt` |
| Compaction summary (`isCompactSummary`) | `role=compact_summary`, agent |
| Thinking block | `role=thinking`, agent: only with `CLAUDE_CODE_THINKING=true` |
| `tool_use` blocks | folded into the next reply: `tool_calls`, `tools` `{name: count}`, `files`, `tool_errors` |
| Tool calls with no reply after them (an interrupted turn) | one `role=tool_calls` record when the next prompt arrives |
| Tool results, command output, `<system-reminder>`, `<task-notification>`, `isMeta` lines, attachments, API-error and `<synthetic>` messages, `[Request interrupted…]` | skipped |
| `custom-title`, `ai-title`, `summary`, `agent-name` lines | the session title (priority in that order, then the subagent description, then the first prompt) |

The parent-message chain (`uuid`, `parentUuid`, `logicalParentUuid`), `promptId`, `requestId`, the message id, the stop reason and the token usage go into the raw payload. Message text is cut at 100,000 characters (`text_truncated`).

**Ids.** A message's `external_id` is `<thread>:<line uuid>`, with a `#<block>` suffix for the second and later blocks of a multi-block line. The thread is the session id, or `<session id>/agent-<agent id>` for a subagent transcript. Line uuids are random, but forked subagents copy their parent's context lines with the same uuids, so ids are scoped to the thread.

**Tool calls.** The `tools`, `files` and `tool_calls` fields on an agent reply describe the work done since the previous message: the calls that led to this answer. Tool names are kept as Claude Code reports them (`Bash`, `Read`, `Edit`, `mcp__server__tool`, ...). Files come from each tool's `file_path`, `notebook_path` or `path` input, up to 50 per reply. Commands and outputs are not kept, because they often contain credentials.

## Channels

### `claude_code.messages` (life event)

One thread per session or subagent transcript. Thread metadata: `session_id`, `project_dir`, `git_branch`, `entrypoint`, `is_subagent`, `home`, plus `parent_session`, `agent_id` and `agent_type` for subagents.

| Field | Type | Meaning |
|---|---|---|
| `role` | text | `user`, `command`, `shell`, `assistant`, `structured_output`, `thinking`, `compact_summary`, `tool_calls` |
| `author_kind` | text | `owner`, `agent` or `agent_prompt` |
| `session_id`, `agent_id`, `is_subagent` | text/text/boolean | Session and subagent |
| `project_dir`, `git_branch` | text | Working directory and branch at that line |
| `entrypoint` | text | `cli`, `claude-vscode`, `sdk-ts`, `sdk-py`, or the name of an app embedding the Agent SDK |
| `model` | text | Model of an agent message |
| `tool_calls`, `tools`, `files`, `tool_errors` | integer/json/json/integer | Tool work since the previous message |
| `image_count` | integer | Images attached to a prompt |
| `text_length`, `text_truncated` | integer/boolean | Stored text size |
| `redactions` | integer | Secret-shaped strings replaced in this record |
| `claude_version`, `mapping_revision` | text/integer | Provenance |

Authors are `{system: "claude_code", kind: "owner", value: "owner"}` for you (set `CLAUDE_CODE_OWNER_IDENTIFIER` to map yourself to an existing identity, e.g. `email:email:you@example.com`), `claude_code/agent/claude` for Claude, and `claude_code/agent/orchestrator` for agent-written prompts.

### `claude_code.sessions` (reference)

One record per session and subagent transcript. It is re-versioned whenever the file grows. `happened_at` is the first message and `ended_at` the last. Records are grouped into one thread per project folder. The fields are `session_id`, `agent_id`, `is_subagent`, `agent_type`, `workflow_run`, `title`, `project_dir`, `git_branch`, `entrypoint`, `claude_version`, `owner_messages`, `agent_messages`, `agent_prompts`, `tool_calls`, `models` (agent messages by model) and `home`.

## Setup

No credentials are needed. Run the plugin on the machine where Claude Code runs, or on a machine that can read a synced copy of the home.

```sh
throughline token create claude-code --kind plugin --plugin claude_code --json
export THROUGHLINE_URL=http://127.0.0.1:8765 THROUGHLINE_TOKEN=...
uv run throughline-claude-code backfill --dry-run   # count what would be sent
uv run throughline-claude-code backfill
uv run throughline-claude-code sync --once          # from a timer, e.g. every 15 minutes
uv run throughline-claude-code sync                 # or run continuously
```

| Variable | Default | Meaning |
|---|---|---|
| `CLAUDE_CODE_HOMES` | `local=$CLAUDE_CONFIG_DIR` or `local=~/.claude` | Homes to read, `LABEL=PATH,LABEL=PATH` (e.g. a laptop's synced home next to this machine's) |
| `CLAUDE_CODE_ENTRYPOINTS` | `all` | Keep only lines from these entrypoints, comma-separated |
| `CLAUDE_CODE_SUBAGENTS` | `true` | Read subagent transcripts |
| `CLAUDE_CODE_THINKING` | `false` | Also keep the model's thinking text |
| `CLAUDE_CODE_SCRUB` | `true` | Replace secret-shaped strings |
| `CLAUDE_CODE_OWNER_IDENTIFIER` | `claude_code:owner:owner` | Your author identifier, `system:kind:value` |
| `CLAUDE_CODE_OWNER_NAME` | unset | Display name on your messages |
| `CLAUDE_CODE_BATCH_SIZE` | `500` | Messages between cursor checkpoints |
| `THROUGHLINE_INTERVAL_S` | `900` | Set to your timer's period so health checks match |

Changing a mapping setting (owner identifier or name, thinking, scrubbing, entrypoints, subagents) resets the cursor. Everything is then read again, and records whose mapping changed get new versions.

### Sessions run by other apps

Apps that embed Claude Code through the Agent SDK, such as IDE extensions and agent orchestrators like T3 Code, write their sessions to the same folder, with their name in the `entrypoint` field. If such an app has its own Throughline connector (for example `plugins/t3code`), its conversations would show up twice. Skip them with `CLAUDE_CODE_ENTRYPOINTS=cli,claude-vscode` (list the entrypoints you want to keep). By default every entrypoint is kept, and the field lets you filter later.

## Secret scrubbing

Every string in a record (content, fields, payload, thread title and metadata) goes through a scrubber before it is sent. These shapes are replaced with `[REDACTED:<kind>]`: private key blocks, Anthropic (`sk-ant-`), OpenAI (`sk-`, `sk-proj-`), Stripe, GitHub (`ghp_`, `github_pat_`), GitLab, AWS (`AKIA`, `ASIA`), Slack, Google API and Hugging Face keys, JWTs, `Bearer`/`token` header values, passwords in URLs (`scheme://user:pass@`), and values of `name=value` / `name: value` assignments whose name contains key, secret, token, password or credential. The `redactions` field counts the replacements. Scrubbing is pattern matching: it misses credentials with no recognisable shape and sometimes catches harmless text. Turn it off with `CLAUDE_CODE_SCRUB=false` only if nothing it reads could hold secrets.

## Limitations

- Only what is on disk: sessions deleted by `cleanupPeriodDays` (see above), or written on another machine whose home isn't configured, can't be recovered.
- The transcript format is internal to Claude Code and changes between versions. Line types the plugin doesn't recognise are ignored, never fatal. Older versions that wrote subagent lines inline (`isSidechain: true` in the main file) are mapped as agent prompts and replies in the session's own thread.
- A thread title that changes after the last message (a title line with nothing after it) reaches Throughline only with the session's next message.
- Thinking text is Claude's own summary when the API returns summarised thinking, and is empty for redacted thinking.
- Resuming with `--fork-session` copies the history into a new session file. Those copies become separate messages in the new thread.
- If several homes hold the same session (a copied home), the messages are identical and stored once, but each home's session record overwrites the other's counts.
