# Throughline T3 Code plugin

All of your T3 Code work, from every machine, plus the AI subscriptions T3 Code
is connected to, as Throughline sources.

- **Work:** every project, every thread (top-level and subagent threads) and
  every user and assistant message. Messages are embedded. You can query them
  by project, thread, machine, provider and model.
- **Usage:** a status record for each change in a subscription's usage window
  (Claude weekly, Claude Fable weekly, Codex weekly, ...). Each status gives
  the percent used and left, the time left in the week, the pace and the burn
  since the previous status. A **100% status is recorded at every reset**,
  including resets that no snapshot has caught up with yet.
- **Resets:** scheduled resets, early resets (a reset credit spent) and changes
  in reset credits (added, used, expired), each in its own record.

Two layers:

- `src/throughline_t3code/fetch.py` uses only the standard library. Copy it to
  any machine that runs T3 Code and run
  `python fetch.py export --out FILE [--home LABEL=PATH] [--codex PATH]`.
  It reads T3's files **read-only** and writes raw JSONL. It reads no
  credential. Account e-mail addresses become a 12-character sha256 key, and
  the address itself is never written.
- The plugin (`throughline-t3code`) maps those records. It works in one of two
  ways:
  - in-process (`T3CODE_SOURCE=live`, the default). With live, export files
    from other machines can be dropped in `T3CODE_IMPORT_DIR`, and each one is
    mapped once.
  - from export files (`T3CODE_SOURCE=export:<path>`).

Written against T3 Code's `statev2.sqlite` (orchestration v2 projections) as of
2026-11.

## Sources read

| Source | What it gives |
|---|---|
| `<home>/userdata/statev2.sqlite` | `projection_projects`, `orchestration_v2_projection_threads`, `…_runs`, `…_messages` |
| `<home>/userdata/environment-id` | the machine's T3 environment id |
| `<home>/caches/<instance>.json` | plan label, current usage windows, reset credits (T3 refreshes these) |
| `<home>/userdata/logs/provider/*.log*` | Claude `rate_limit_event` and Codex `account/rateLimits/updated`: usage history |
| `<codex home>/sessions/**/*.jsonl` | Codex `rate_limits` snapshots: usage history |

Log files and Codex session files are read incrementally, by byte offset.
Messages are read by `updated_at` watermark, with a one-hour overlap. A message
that is still streaming is skipped until it is final.

## Channels

| Channel | One record per | Notes |
|---|---|---|
| `t3code.messages` | user or assistant message | thread = the T3 thread; `author_kind` is `owner`, `agent` or `agent_prompt` |
| `t3code.threads` (reference) | T3 thread | project, model, branch, parent and root thread, subagent flag |
| `t3code.projects` (reference) | T3 project | folder and thread counts |
| `t3code.usage` | change of a usage window, or a reset | not embedded |
| `t3code.resets` | reset, early reset or reset credit change | not embedded |
| `t3code.subscriptions` (reference) | subscription | plan, latest status of every window, credits |

Messages from agents:

- An assistant message is authored by `t3code:agent:<provider instance>`.
- A user-role message that an agent wrote (subagent briefs, delegated tasks)
  is `agent_prompt`, authored by `t3code:agent:orchestrator`.

Only your own words are `owner`, authored by `T3CODE_OWNER_IDENTIFIER`
(default `t3code:owner:owner`).

### Usage status fields

| Field | Meaning |
|---|---|
| `used_percent`, `left_percent` | how much of the window is used and left |
| `resets_at`, `window_start` | the window's bounds |
| `time_left`, `time_left_hours` | time left in the window |
| `window_elapsed_percent` | how much of the window's time has passed |
| `pace_points` | used minus elapsed; above 0 burns faster than an even pace |
| `projected_used_at_reset` | where the window ends up at this pace |
| `burn_since_previous` | new points used (see below) |
| `cycle_burn_per_day` | average daily burn so far this window |
| `event` | `first`, `observed`, `reset` or `early_reset` |

`burn_since_previous` counts only a rise above the window cycle's high-water
mark. Snapshots from parallel sessions jitter (27, 25, 28, ...), and counting
only new highs means the jitter is never counted twice. So the weekly sum of
`burn_since_previous` is the real burn-down.

### Reset detection

- **Scheduled reset:** the window's `resets_at` passes. The 100% record is
  stamped at the reset time, both when a newer snapshot shows a new window and
  when no snapshot has arrived yet. A run after the reset time writes it.
- **Early reset:**
  - The reset time moves earlier and use drops.
  - Or the reset time stays the same and use falls to 10% or less, at least
    25 points below the cycle's high. Claude keeps the weekly anchor when a
    reset credit refills the window.
- **Credits:** `availableCount` goes up (`credit_added`) or down. A drop is
  `credit_expired` when the next credit's expiry has passed, and
  `credit_used` otherwise.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `T3CODE_SOURCE` | `live` | `live` or `export:<file or directory>` |
| `T3CODE_HOMES` | `local=~/.t3` | `LABEL=PATH,...`; the first home holding a record names its `environment` |
| `T3CODE_CODEX_HOMES` | `~/.codex` | Codex homes for usage history; `none` to skip |
| `T3CODE_IMPORT_DIR` | | live mode: export files from other machines |
| `T3CODE_ARCHIVE_DIR` | | keep every live run as an export file |
| `T3CODE_SINCE` | | lower bound for messages on the first run |
| `T3CODE_USAGE`, `T3CODE_WORK` | `true` | turn either half off |
| `T3CODE_OWNER_IDENTIFIER`, `T3CODE_OWNER_NAME` | `t3code:owner:owner` | author of your messages |

A home copied to another machine (for example after moving from Windows to
Linux) keeps T3's ids. List the old home first: its records keep their original
`environment` label, and nothing is doubled.

## Recipes

The core ships four recipes that use these channels:

- `work_cadence`: weekly messages, active days, threads, projects;
- `work_concentration`: threads per week, the top thread's share, and the
  effective number of threads;
- `burn_vs_calendar`: weekly burn-down and resets next to calendar load;
- `subscription_status`: the latest percent left and time left of every
  window, and the reset cadence.

## Tests

`uv run pytest plugins/t3code` runs against invented T3 homes. The suite
covers:

- the contract kit, which also checks that the account e-mail never leaves
  the fetcher;
- authors, subagent threads, streaming and the watermark;
- two homes without duplicates;
- dedup, burn and jitter;
- the 100% record at a reset with no snapshot;
- early resets from both a moved and a kept anchor;
- credits;
- export round trips and the import folder.
