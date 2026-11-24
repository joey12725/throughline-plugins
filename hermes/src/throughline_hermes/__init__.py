"""Hermes Agent conversations: every user, assistant and tool turn, and sessions.

Two layers: `fetch.py` (standard library only) reads the Hermes session store
read-only and writes raw JSONL records; this plugin maps those records to
Throughline messages, either from export files (HERMES_SOURCE=export:<file
or directory>) or by running the fetcher in-process (HERMES_SOURCE=live).
"""

import copy
import json
import logging
import sqlite3

from throughline_sdk import (
    Channel,
    Checkpoint,
    Declaration,
    Manifest,
    Plugin,
    Record,
    run,
)

from . import fetch, sources
from .mapping import (
    AUTOMATION,
    MAPPING_REVISION,
    MESSAGES,
    PREVIEW_CHARS,
    SESSIONS,
    Settings,
    map_message,
    map_session,
)

__all__ = ["Hermes", "MAPPING_REVISION", "main"]


BATCH_BYTES = 4_000_000


def fields(**values):
    return [dict(name=k, type=v[0], description=v[1]) for k, v in values.items()]


MESSAGE_FIELDS = fields(
    role=("text", "user, assistant, tool or system"),
    platform=("text", "Session source: discord, telegram, cli, api_server, cron, ..."),
    session_id=("text", "Hermes session id (the thread)"),
    message_id=(
        "integer",
        "Hermes message row id (changes when Hermes rewrites a row)",
    ),
    message_uid=("text", "Hermes message uid, stable across rewrites (the record key)"),
    model=("text", "The session's model at fetch time"),
    tool_call_count=("integer", "Number of tool calls an assistant row made"),
    token_count=("integer", "Tokens Hermes counted for this row"),
    finish_reason=("text", "Model finish reason of an assistant row"),
    tool_name=("text", "Tool that produced a tool row"),
    tool_call_id=("text", "Tool call a tool row answers"),
    tool_names=("json", "Tools an assistant row called, in order"),
    tool_call_ids=("json", "Ids of the tool calls an assistant row made"),
    tool_calls=(
        "json",
        "Tool calls of an assistant row: id, name, arguments (bounded)",
    ),
    tool_output=("text", "Full tool result text, bounded by HERMES_MAX_TEXT"),
    tool_output_length=("integer", "Characters of the tool result as stored by Hermes"),
    tool_output_truncated=("boolean", "The tool result was cut to HERMES_MAX_TEXT"),
    content_length=("integer", "Characters of the stored content"),
    content_truncated=("boolean", "The stored content was cut to HERMES_MAX_TEXT"),
    reasoning_present=(
        "boolean",
        "The model returned hidden reasoning (never stored here)",
    ),
    visibility=(
        "text",
        "active, compacted (archived by compaction) or rewound (removed by undo/edit)",
    ),
    compressed_summary=("boolean", "A context-compression summary row"),
    display_kind=("text", "Hermes display kind of the row"),
    platform_message_id=(
        "text",
        "The platform's own message id (Discord, Telegram, ...)",
    ),
    automated=("boolean", "Part of an automated (cron) session"),
    mapping_revision=("integer", "Mapping revision that produced this record"),
)

SESSION_FIELDS = fields(
    session_id=("text", "Hermes session id"),
    platform=("text", "Session source: discord, telegram, cli, api_server, cron, ..."),
    created_source=("text", "Source the session was created from"),
    user_id=("text", "Platform user id that owns the session"),
    user_name=("text", "Platform user name from the session origin"),
    chat_id=("text", "Platform chat id"),
    chat_type=("text", "dm, group, channel or thread"),
    chat_name=("text", "Platform chat name from the session origin"),
    owner_session=(
        "boolean",
        "The owner's own chat (user turns authored by the owner)",
    ),
    automated=("boolean", "Automated (cron) session"),
    model=("text", "Model"),
    provider=("text", "Billing provider"),
    title=("text", "Session title"),
    parent_session_id=(
        "text",
        "Parent session (compression, branch, reset or subagent)",
    ),
    end_reason=("text", "Why the session ended"),
    message_count=("integer", "Messages Hermes counted"),
    tool_call_count=("integer", "Tool calls Hermes counted"),
    api_call_count=("integer", "Model API calls"),
    input_tokens=("integer", "Input tokens"),
    output_tokens=("integer", "Output tokens"),
    cache_read_tokens=("integer", "Cache read tokens"),
    cache_write_tokens=("integer", "Cache write tokens"),
    reasoning_tokens=("integer", "Reasoning tokens"),
    estimated_cost_usd=("number", "Estimated cost in USD"),
    actual_cost_usd=("number", "Actual cost in USD"),
    profile_name=("text", "Hermes profile"),
    system_prompt_sha256=("text", "sha256 of the system prompt text"),
    system_prompt_length=("integer", "Characters of the system prompt"),
    system_prompt_hermes_hash=("text", "Hermes's own system prompt hash"),
    system_prompt=(
        "text",
        "System prompt text; only with HERMES_STORE_SYSTEM_PROMPT=1",
    ),
    mapping_revision=("integer", "Mapping revision that produced this record"),
)

PLUGIN_SETTINGS = [
    (
        "source",
        "HERMES_SOURCE",
        "live",
        "live, or export:<JSONL file or directory of export files>",
    ),
    (
        "archive_dir",
        "HERMES_ARCHIVE_DIR",
        None,
        "Optional directory where live runs are kept as export files",
    ),
    (
        "links",
        "HERMES_LINKS",
        "true",
        "Link tool results to their tool call (reply_to)",
    ),
    (
        "batch_size",
        "HERMES_BATCH_SIZE",
        "500",
        "Messages per acknowledged batch and checkpoint",
    ),
    (
        "agent_name",
        "HERMES_AGENT_NAME",
        "hermes",
        "Agent identity value (hermes:agent:<name>)",
    ),
    ("agent_display_name", "HERMES_AGENT_DISPLAY_NAME", None, "Agent display name"),
    (
        "owner_sources",
        "HERMES_OWNER_SOURCES",
        "cli,tui,desktop,gui,webui,acp,local",
        "Sources whose user turns are the owner's own (comma-separated)",
    ),
    (
        "owner_user_ids",
        "HERMES_OWNER_USER_IDS",
        None,
        "The owner's platform user ids: <source>:<id> or <id>, comma-separated",
    ),
    (
        "owner_identifier",
        "HERMES_OWNER_IDENTIFIER",
        "hermes:owner:owner",
        "Author identifier of owner turns, system:kind:value",
    ),
    ("owner_name", "HERMES_OWNER_NAME", None, "Display name on owner turns"),
    (
        "automation_sources",
        "HERMES_AUTOMATION_SOURCES",
        "cron",
        "Sources whose sessions go to hermes.automation (comma-separated)",
    ),
    (
        "preview_chars",
        "HERMES_PREVIEW_CHARS",
        str(PREVIEW_CHARS),
        "Characters of a tool result shown in the message content",
    ),
]


class FetchFailed(RuntimeError):
    """A live run could not read the session store."""


class Hermes(Plugin):
    manifest = Manifest(
        name="hermes",
        version="0.1.0",
        channels=[
            Channel(
                name=MESSAGES,
                description=(
                    "Every user, assistant and tool turn of Hermes Agent conversations (Discord, "
                    "Telegram, CLI, desktop, API, voice, ...); one thread per session, in order"
                ),
                fields=MESSAGE_FIELDS,
            ),
            Channel(
                name=AUTOMATION,
                description=(
                    "Turns of automated Hermes sessions (cron jobs), kept apart so they do not "
                    "flood conversation search; one thread per session"
                ),
                fields=MESSAGE_FIELDS,
            ),
            Channel(
                name=SESSIONS,
                description=(
                    "One reference record per Hermes session: platform, model, start and end, "
                    "end reason, counts, tokens, cost, title, parent session"
                ),
                fields=SESSION_FIELDS,
                is_reference=True,
            ),
        ],
        settings={
            key: Declaration(
                env=env, required=False, default=default, description=description
            )
            for key, env, default, description in PLUGIN_SETTINGS + fetch.SETTINGS
        },
        secrets={},
        run_mode="poll",
        default_interval_s=1800,
        link_types=["reply_to"],
    )

    def __init__(self):
        super().__init__()
        self.kind, self.path = sources.source_spec(self.settings["source"])
        self.batch_size = int(self.settings["batch_size"])
        if self.batch_size < 1:
            raise ValueError("HERMES_BATCH_SIZE must be positive")
        self.with_links = fetch.truthy(self.settings["links"])
        self.mapping = Settings(
            agent_name=self.settings["agent_name"] or "hermes",
            agent_display_name=self.settings["agent_display_name"],
            owner_sources=fetch.split_list(self.settings["owner_sources"]),
            owner_user_ids=fetch.split_list(self.settings["owner_user_ids"]),
            owner_identifier=self.settings["owner_identifier"] or "hermes:owner:owner",
            owner_name=self.settings["owner_name"],
            automation_sources=fetch.split_list(self.settings["automation_sources"]),
            preview_chars=int(self.settings["preview_chars"]),
        )
        fetch.config(self.fetch_settings())  # Fail early on a bad setting.
        self.dry_run = False
        self.stats = {}
        # Test seam for the live fetch: now, popen.
        self.fetch_options = {}

    def fetch_settings(self):
        return {key: self.settings[key] for key, _, _, _ in fetch.SETTINGS}

    def execute(self, client, mode, dry_run=False):
        self.dry_run = dry_run
        report = super().execute(client, mode, dry_run)
        if self.stats:
            report["hermes"] = self.stats
        return report

    def pending_runs(self, state, resume, mode):
        """(runs to map, resume offset) for this invocation."""
        last = state.get("last_run")
        if self.kind == "export":
            runs, skipped = sources.export_runs(self.path)
            upto = (resume or {}).get("upto")
            pending = [
                (key, records)
                for key, records in runs
                if (last is None or key > last) and (upto is None or key <= upto)
            ]
            self.stats.update(runs_in_export=len(runs), runs_without_header=skipped)
            return pending, ((resume or {}).get("offset", 0) if upto else 0)
        fetch_state = state.setdefault("fetch", {})
        try:
            key, records, report = sources.live_run(
                self.fetch_settings(),
                mode,
                fetch_state,
                log=lambda text: logging.warning("Hermes %s", text),
                **self.fetch_options,
            )
        except (fetch.FetchError, sqlite3.Error, OSError) as error:
            raise FetchFailed(f"Hermes fetch failed: {error}") from None
        self.stats["fetch"] = report
        if self.settings["archive_dir"] and not self.dry_run and records:
            sources.archive(self.settings["archive_dir"], records)
        if key is None or (last is not None and key <= last):
            return [], 0
        return [(key, records)], 0

    def map_runs(self, pending):
        """{(channel, external_id): (message, link)} of the pending runs; the last run wins."""
        plan = {}
        stats = dict(messages=0, sessions=0, orphans=0, unmapped=0, incomplete_runs=0)
        for _, records in pending:
            run_sessions = {
                r["id"]: r
                for r in records
                if r.get("kind") == "session" and r.get("id")
            }
            for record in records:
                kind = record.get("kind")
                if kind == "run_end" and not record.get("complete", True):
                    stats["incomplete_runs"] += 1
                if kind == "session":
                    message = map_session(record, self.mapping)
                    link = None
                elif kind == "message":
                    session = run_sessions.get(record.get("session_id"))
                    if session is None:
                        stats["orphans"] += 1
                    message, link = map_message(
                        record, (session or {}).get("payload"), self.mapping
                    )
                else:
                    continue
                if message is None:
                    stats["unmapped"] += 1
                    continue
                stats["sessions" if kind == "session" else "messages"] += 1
                plan[(message["channel"], message["external_id"])] = (message, link)
        self.stats.update(stats)
        return plan

    @staticmethod
    def order(item):
        message = item[1][0]
        if message["channel"] == SESSIONS:
            return (0, 0, message["external_id"])
        return (1, message["fields"]["message_id"], message["external_id"])

    def records(self, cursor, mode):
        self.stats = {}
        state = copy.deepcopy(cursor or {})
        fingerprint = self.mapping.fingerprint()
        if (
            state.get("mapping_revision") != MAPPING_REVISION
            or state.get("config") != fingerprint
        ):
            # A new mapping or mapping setting replays every input.
            state = {"mapping_revision": MAPPING_REVISION, "config": fingerprint}
        resume = state.pop("resume", None)
        base = copy.deepcopy(state)
        pending, offset = self.pending_runs(state, resume, mode)
        plan = self.map_runs(pending)
        for key, _ in pending:
            state["last_run"] = key
        self.stats["runs_mapped"] = len(pending)
        items = sorted(plan.items(), key=self.order)
        upto = pending[-1][0] if pending else None
        start = offset
        while start < len(items):
            end = start
            batch_bytes = 0
            while end < len(items) and (end - start) < self.batch_size:
                msg = items[end][1][0]
                size = len(
                    json.dumps(
                        msg, ensure_ascii=False, separators=(",", ":"), default=str
                    ).encode("utf-8")
                )
                if end > start and batch_bytes + size > BATCH_BYTES:
                    break
                batch_bytes += size
                end += 1
            chunk = items[start:end]
            for _, (message, _) in chunk:
                yield message
            if self.with_links:
                for _, (_, link) in chunk:
                    if link:
                        yield Record(kind="link", data=link)
            if end < len(items):
                interim = copy.deepcopy(base)
                if self.kind == "export":
                    interim["resume"] = {
                        "upto": upto,
                        "offset": end,
                    }
                yield Checkpoint(cursor=interim)
            start = end
        yield Checkpoint(cursor=state)


def main(argv=None):
    run(Hermes, argv)
