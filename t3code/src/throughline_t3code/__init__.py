"""T3 Code work and AI subscription usage as Throughline sources.

Work: every project, thread (including subagent threads) and user and
assistant message from each T3 home (machine) configured, keyed by T3's own
ids so a home copied to another machine never doubles a record.

Usage: one status per change of each subscription window (percent used and
left, time left in the window, pace, burn since the previous status), a 100%
status at every reset (observed or due by the window's reset time), and reset
events and reset credit changes in their own channel.

Two layers: `fetch.py` (standard library only) reads T3's files read-only;
this plugin maps the raw records, either in-process (T3CODE_SOURCE=live) or
from export files written by `fetch.py export` on another machine
(T3CODE_SOURCE=export:<file or directory>).
"""

import copy
import gzip
import json
import logging
import sqlite3
from pathlib import Path

from throughline_sdk import (
    Channel,
    Checkpoint,
    Declaration,
    Manifest,
    Plugin,
    coverage,
    run,
)

from . import fetch
from .mapping import (
    MAPPING_REVISION,
    MESSAGES,
    PROJECTS,
    RESETS,
    SUBSCRIPTIONS,
    THREADS,
    USAGE,
    Usage,
    Work,
    fingerprint,
    moment,
    now_utc,
)

__all__ = ["T3Code", "MAPPING_REVISION", "main"]


def fields(**values):
    return [dict(name=k, type=v[0], description=v[1]) for k, v in values.items()]


ENVIRONMENT = dict(
    environment_id=("text", "T3 environment (machine) id"),
    environment=("text", "Label of the T3 home the record was read from (T3CODE_HOMES)"),
)

MESSAGE_FIELDS = fields(
    role=("text", "user or assistant"),
    author_kind=(
        "text",
        "owner (you typed it), agent (an assistant reply) or agent_prompt (a user-role "
        "turn an agent wrote: subagent briefs, delegated tasks)",
    ),
    project_id=("text", "T3 project id"),
    project=("text", "Project title"),
    t3_thread_id=("text", "T3 thread id"),
    title=("text", "Thread title"),
    root_thread=(
        "text",
        "Top-level thread this message belongs to (the thread itself unless it is a "
        "subagent thread); group by it to count threads of work",
    ),
    is_subagent=("boolean", "The message is in a subagent (child) thread"),
    provider=("text", "Provider instance that answered: claudeAgent, codex, ..."),
    model=("text", "Model of the run (or the thread's model)"),
    run=("text", "T3 run id: one user turn and everything it caused"),
    creation_source=("text", "web, mobile, provider, server or mcp"),
    text_length=("integer", "Characters of the message text"),
    text_truncated=("boolean", "The text was cut to 100,000 characters"),
    attachment_count=("integer", "Attachments on the message"),
    mapping_revision=("integer", "Mapping revision that produced this record"),
    **ENVIRONMENT,
)

THREAD_FIELDS = fields(
    t3_thread_id=("text", "T3 thread id"),
    project_id=("text", "T3 project id"),
    project=("text", "Project title"),
    title=("text", "Thread title"),
    provider=("text", "Provider instance of the thread"),
    model=("text", "Model selected for the thread"),
    created_by=("text", "user or agent"),
    creation_source=("text", "Where the thread was started"),
    branch=("text", "Git branch"),
    worktree_path=("text", "Worktree path"),
    is_subagent=("boolean", "A subagent (child) thread"),
    parent_thread=("text", "Parent thread id of a subagent thread"),
    root_thread=("text", "Top-level thread id"),
    relationship_to_parent=("text", "How a child thread relates to its parent"),
    forked_from=("text", "Thread this one was forked from"),
    archived=("boolean", "Archived"),
    deleted=("boolean", "Deleted"),
    last_updated=("timestamp", "Last thread update"),
    mapping_revision=("integer", "Mapping revision that produced this record"),
    **ENVIRONMENT,
)

PROJECT_FIELDS = fields(
    project_id=("text", "T3 project id"),
    project=("text", "Project title"),
    workspace_root=("text", "Workspace folder"),
    deleted=("boolean", "Deleted"),
    top_level_threads=("integer", "Top-level threads at fetch time"),
    all_threads=("integer", "Threads including subagent threads at fetch time"),
    last_updated=("timestamp", "Last project update"),
    mapping_revision=("integer", "Mapping revision that produced this record"),
    **ENVIRONMENT,
)

ACCOUNT = dict(
    account=("text", "Account key: a short hash, never the e-mail address"),
    subscription=("text", "Subscription name: Claude Ultra, ChatGPT Pro 100, ..."),
    provider=("text", "claudeAgent, codex, ..."),
)

USAGE_FIELDS = fields(
    event=(
        "text",
        "first, observed (a new value), reset (window rolled over: 100% left) or "
        "early_reset (use dropped before the reset time: a reset credit or provider reset)",
    ),
    **ACCOUNT,
    window_id=("text", "Provider window id: seven_day, seven_day_fable, primary, ..."),
    window_label=("text", "Weekly, Weekly · Fable, 5-hour, ..."),
    window_minutes=("integer", "Window length in minutes (10080 = a week)"),
    used_percent=("number", "Percent of the window used"),
    left_percent=("number", "Percent of the window left"),
    resets_at=("timestamp", "When the window resets"),
    window_start=("timestamp", "When the current window began"),
    time_left_hours=("number", "Hours left in the window at this status"),
    time_left=("text", "Time left in the window, e.g. 3d 7h"),
    window_elapsed_percent=("number", "Percent of the window's time already past"),
    pace_points=(
        "number",
        "used_percent minus window_elapsed_percent: above 0 burns faster than an even pace",
    ),
    projected_used_at_reset=("number", "used_percent extrapolated to the reset at this pace"),
    burn_since_previous=(
        "number",
        "New percentage points used since the previous status: only a rise above this "
        "window cycle's high-water mark counts, so jittering snapshots never double "
        "count; sum it per week for the burn-down",
    ),
    hours_since_previous=("number", "Hours since the previous status of this window"),
    cycle_burn_per_day=(
        "number",
        "Average percentage points used per day since the window began",
    ),
    used_before_reset=("number", "On a reset: percent used just before it"),
    status=("text", "Provider status: allowed, allowed_warning, rejected"),
    plan_type=("text", "Provider plan type when the source names it"),
    source=("text", "t3_cache, t3_provider_log, codex_session or reset_time"),
    mapping_revision=("integer", "Mapping revision that produced this record"),
)

RESET_FIELDS = fields(
    event=(
        "text",
        "reset, early_reset, credit_balance (first seen), credit_added, credit_used, "
        "credit_expired",
    ),
    **ACCOUNT,
    window_id=("text", "Window that reset"),
    window_label=("text", "Window label"),
    used_before_percent=("number", "Percent used just before the reset"),
    left_before_percent=("number", "Percent left just before the reset"),
    next_reset_at=("timestamp", "Next scheduled reset"),
    hours_since_previous_reset=("number", "Hours since this window's previous reset"),
    scheduled_reset_at=("timestamp", "The scheduled reset time (reset events)"),
    available_credits=("integer", "Reset credits available after the change"),
    credit_change=("integer", "Change in available reset credits"),
    next_credit_expires_at=("timestamp", "When the next reset credit expires"),
    credit_options=("json", "Reset credit kinds: id, label, description, count, expiry"),
    mapping_revision=("integer", "Mapping revision that produced this record"),
)

SUBSCRIPTION_FIELDS = fields(
    **ACCOUNT,
    instance=("text", "T3 provider instance"),
    plan=("text", "Plan label T3 reports"),
    auth_type=("text", "Authentication type"),
    enabled=("boolean", "Enabled in T3"),
    usage_unavailable=("text", "Why the provider reports no usage, if it does not"),
    windows=("json", "Latest status per window: used, left, resets_at, observed_at"),
    available_credits=("integer", "Reset credits available"),
    mapping_revision=("integer", "Mapping revision that produced this record"),
)

PLUGIN_SETTINGS = [
    (
        "source",
        "T3CODE_SOURCE",
        "live",
        "live (read the T3 homes here), or export:<JSONL file or directory>",
    ),
    ("archive_dir", "T3CODE_ARCHIVE_DIR", None, "Keep each live run as an export file here"),
    (
        "import_dir",
        "T3CODE_IMPORT_DIR",
        None,
        "Live mode: also map export files from other machines dropped in this folder "
        "(each run once)",
    ),
    (
        "owner_identifier",
        "T3CODE_OWNER_IDENTIFIER",
        "t3code:owner:owner",
        "Author identifier of your own messages, system:kind:value",
    ),
    ("owner_name", "T3CODE_OWNER_NAME", None, "Display name on your messages"),
]

SUFFIXES = (".jsonl", ".jsonl.gz", ".ndjson")


class FetchFailed(RuntimeError):
    """A live run could not read a T3 home."""


def read_export(path):
    path = Path(path).expanduser()
    files = (
        sorted(p for p in path.rglob("*") if p.is_file() and p.name.endswith(SUFFIXES))
        if path.is_dir()
        else [path]
    )
    if not files or not files[0].exists():
        raise ValueError("the T3CODE_SOURCE export path holds no .jsonl files")
    runs = {}
    for file in files:
        opener = gzip.open if file.name.endswith(".gz") else open
        with opener(file, "rt", encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                record = json.loads(line)
                if not isinstance(record, dict) or not record.get("run"):
                    raise ValueError(f"{file.name} line {number} is not a T3 Code export record")
                runs.setdefault(record["run"], []).append(record)
    keyed = []
    for run_id, records in runs.items():
        header = next((r for r in records if r["kind"] == "run"), None)
        if header:
            keyed.append(([header["fetched_at"], run_id], records))
    keyed.sort(key=lambda pair: pair[0])
    return keyed


class T3Code(Plugin):
    manifest = Manifest(
        name="t3code",
        version="0.1.0",
        channels=[
            Channel(
                name=MESSAGES,
                description=(
                    "Every user and assistant message of every T3 Code thread, in every "
                    "environment: your prompts (author_kind owner), agent replies (agent) "
                    "and agent-written prompts (agent_prompt); one thread per T3 thread, "
                    "with project, root thread and model fields"
                ),
                fields=MESSAGE_FIELDS,
                conversation_whole_thread=False,
            ),
            Channel(
                name=THREADS,
                description=(
                    "One reference record per T3 Code thread (top-level and subagent): "
                    "title, project, model, branch, parent and root thread"
                ),
                fields=THREAD_FIELDS,
                is_reference=True,
            ),
            Channel(
                name=PROJECTS,
                description="One reference record per T3 Code project: title, folder, thread counts",
                fields=PROJECT_FIELDS,
                is_reference=True,
            ),
            Channel(
                name=USAGE,
                description=(
                    "AI subscription usage status (Claude, Codex, ...): one record per "
                    "change of a usage window with percent used and left, time left in "
                    "the week, pace and burn since the previous status, and a 100% "
                    "record at every reset"
                ),
                fields=USAGE_FIELDS,
                embed=False,
            ),
            Channel(
                name=RESETS,
                description=(
                    "Usage window resets (scheduled and early) and reset credit changes "
                    "(added, used, expired) of every AI subscription"
                ),
                fields=RESET_FIELDS,
                embed=False,
            ),
            Channel(
                name=SUBSCRIPTIONS,
                description=(
                    "One reference record per AI subscription connected to T3 Code: plan, "
                    "latest status of each usage window, reset credits"
                ),
                fields=SUBSCRIPTION_FIELDS,
                is_reference=True,
                embed=False,
            ),
        ],
        settings={
            key: Declaration(env=env, required=False, default=default, description=description)
            for key, env, default, description in PLUGIN_SETTINGS + fetch.SETTINGS
        },
        secrets={},
        run_mode="poll",
        default_interval_s=900,
        link_types=[],
    )

    def __init__(self):
        super().__init__()
        source = (self.settings["source"] or "live").strip()
        if source == "live":
            self.kind, self.path = "live", None
        elif source.startswith("export:") and source[7:].strip():
            self.kind, self.path = "export", source[7:].strip()
        else:
            raise ValueError("T3CODE_SOURCE must be 'live' or 'export:<file or directory>'")
        self.map_settings = {
            "owner_identifier": self.settings["owner_identifier"] or "t3code:owner:owner",
            "owner_name": self.settings["owner_name"],
        }
        if len(self.map_settings["owner_identifier"].split(":", 2)) != 3:
            raise ValueError("T3CODE_OWNER_IDENTIFIER must be system:kind:value")
        fetch.config(self.fetch_settings())
        self.stats = {}
        self.now = None  # Test seam: the clock used for due resets.

    def fetch_settings(self):
        return {key: self.settings[key] for key, _, _, _ in fetch.SETTINGS}

    def execute(self, client, mode, dry_run=False):
        self.dry_run = dry_run
        report = super().execute(client, mode, dry_run)
        if self.stats:
            report["t3code"] = self.stats
        return report

    def pending(self, state):
        if self.kind == "export":
            last = state.get("last_run")
            return [(k, r) for k, r in read_export(self.path) if last is None or k > last]
        fetch_state = state.setdefault("fetch", {})
        cfg = fetch.config(self.fetch_settings())
        fetcher = fetch.Fetcher(cfg, now=self.now, log=lambda t: logging.warning("T3 Code %s", t))
        try:
            records = list(fetcher.run_records(fetch_state))
        except (fetch.FetchError, sqlite3.Error, OSError) as error:
            raise FetchFailed(f"T3 Code fetch failed: {error}") from None
        self.stats["fetch"] = fetcher.report
        if self.settings["archive_dir"] and not getattr(self, "dry_run", False):
            directory = Path(self.settings["archive_dir"]).expanduser()
            directory.mkdir(parents=True, exist_ok=True)
            with open(directory / f"t3code-{records[0]['run']}.jsonl", "w", encoding="utf-8") as out:
                for record in records:
                    out.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        runs = [([records[0]["fetched_at"], records[0]["run"]], records)]
        folder = self.settings["import_dir"]
        if folder and Path(folder).expanduser().is_dir():
            done = set(state.setdefault("imported", []))
            try:
                found = read_export(folder)
            except ValueError:
                found = []
            fresh = [(k, r) for k, r in found if k[1] not in done]
            state["imported"] = sorted(done | {k[1] for k, _ in fresh})
            self.stats["imported_runs"] = len(fresh)
            runs = fresh + runs
        return runs

    def records(self, cursor, mode):
        self.stats = {}
        state = copy.deepcopy(cursor or {})
        if (
            state.get("mapping_revision") != MAPPING_REVISION
            or state.get("config") != fingerprint(self.map_settings)
        ):
            state = {"mapping_revision": MAPPING_REVISION, "config": fingerprint(self.map_settings)}
        runs = self.pending(state)
        usage = Usage(state.setdefault("usage", {}), moment(self.now) if self.now else now_utc())
        counts = dict(projects=0, threads=0, messages=0)
        plan = {}
        observations, credits = [], []
        read_homes, failed_homes = set(), {}
        for key, records in runs:
            work = Work(self.map_settings)
            seen, errors = set(), {}
            for record in records:
                work.remember(record)
                if record["kind"] == "environment":
                    seen.add(record["home"])
                elif record["kind"] == "home_error":
                    errors[record["home"]] = record.get("error")
                if record["kind"] == "subscription":
                    usage.add_subscription(record)
            read_homes |= seen - set(errors)
            failed_homes.update(errors)
            for record in records:
                kind = record["kind"]
                if kind == "project":
                    message = work.map_project(record)
                elif kind == "thread":
                    message = work.map_thread(record)
                elif kind == "message":
                    message = work.map_message(record)
                elif kind == "usage":
                    observations.append(record)
                    continue
                elif kind == "credits":
                    credits.append(record)
                    continue
                else:
                    continue
                ident = (message["channel"], message["external_id"])
                if ident not in plan:  # First home wins.
                    counts[{PROJECTS: "projects", THREADS: "threads"}.get(message["channel"], "messages")] += 1
                plan.setdefault(ident, message)
            if self.kind == "export":
                state["last_run"] = key
        observations.sort(key=lambda r: (r.get("observed_at") or "", r.get("source") or ""))
        for record in observations:
            usage.observe(record)
        for record in sorted(credits, key=lambda r: r.get("observed_at") or ""):
            usage.credits(record)
        usage.finish()
        for ident, message in usage.out.items():
            plan[ident] = message
        for message in usage.subscription_records():
            plan[(message["channel"], message["external_id"])] = message
        order = {PROJECTS: 0, THREADS: 1, MESSAGES: 2, SUBSCRIPTIONS: 3, USAGE: 4, RESETS: 5}
        for _, message in sorted(
            plan.items(), key=lambda item: (order[item[0][0]], item[1]["happened_at"], item[0][1])
        ):
            yield message
        failed = {k: v for k, v in failed_homes.items() if k not in read_homes}
        if failed and read_homes:
            # Some homes read, some not: the run stands, the missing homes'
            # work is reported as not delivered.
            for label, error in sorted(failed.items()):
                for name in (PROJECTS, THREADS, MESSAGES):
                    yield coverage(
                        channel=name,
                        scope_key=label,
                        status="partial",
                        reasons=["source_error"],
                        detail={"error": error},
                    )
        self.stats.update(counts, usage=usage.stats, runs=len(runs))
        yield Checkpoint(cursor=state)
        if failed and not read_homes:
            # No configured home could be read: a failed run, not an empty one.
            raise FetchFailed(
                "T3 Code could not read any home: "
                + "; ".join(f"{label}: {error}" for label, error in sorted(failed.items()))
            )


def main(argv=None):
    run(T3Code, argv)
