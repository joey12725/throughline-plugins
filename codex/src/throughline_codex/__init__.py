"""OpenAI Codex conversations as a Throughline source.

Reads the rollout JSONL files Codex (CLI, IDE extension, or any app built on
codex app-server) keeps under each configured home, live and incrementally:
a byte-offset cursor per file, re-read from the start when a file is
truncated or rewritten. Your prompts, Codex's replies and its reasoning
summaries become messages (author_kind owner / agent / agent_prompt); tool
calls are kept as compact metadata on the reply that follows them, never
their output. One reference record per session carries its title, folder,
git state and counts. Secret-shaped strings are scrubbed before anything
leaves the plugin. Token and rate-limit events are not read.
"""

import copy
import itertools
import hashlib
import json
import logging

from throughline_sdk import Channel, Checkpoint, Declaration, Manifest, Plugin, coverage, run

from .files import (
    HomeError,
    Truncated,
    fingerprint,
    is_compressed,
    lines,
    parse_homes,
    rollouts,
    session_names,
    split_list,
)
from .mapping import MAPPING_REVISION, MESSAGES, SESSIONS, Mapper, new_state, one_line, pack, unpack

__all__ = ["Codex", "MAPPING_REVISION", "main"]

log = logging.getLogger("throughline_codex")


def fields(**values):
    return [dict(name=k, type=v[0], description=v[1]) for k, v in values.items()]


MESSAGE_FIELDS = fields(
    role=(
        "text",
        "user, assistant, reasoning (a summary of the model's reasoning), agent_message "
        "(one agent writing to another), compact_summary or tool_calls (tool calls with no "
        "reply after them)",
    ),
    author_kind=(
        "text",
        "owner (you typed it), agent (written by the model) or agent_prompt (written by an "
        "agent to another agent: a subagent's brief, an inter-agent message)",
    ),
    session_id=("text", "Codex session (thread) id"),
    parent_session=("text", "Parent session of a subagent session"),
    is_subagent=("boolean", "The message is in a subagent session"),
    project_dir=("text", "Working directory of the session or turn"),
    git_branch=("text", "Git branch when the session started"),
    originator=(
        "text",
        "Client that ran the session: codex_cli_rs, codex_vscode, or an app on codex "
        "app-server (e.g. T3 Code)",
    ),
    model=("text", "Model of the turn (agent messages)"),
    reasoning_effort=("text", "Reasoning effort of the turn (agent messages)"),
    turn_id=("text", "Codex turn id"),
    phase=("text", "Assistant message phase when Codex reports one: commentary or final_answer"),
    tool_calls=("integer", "Tool calls made since the previous message (the work behind this reply)"),
    tools=("json", "Those tool calls by tool name: {name: count}"),
    files=("json", "Files those tool calls patched (apply_patch), at most 50"),
    image_count=("integer", "Images attached to a prompt"),
    agent_from=("text", "Sending agent of an agent_message"),
    agent_to=("text", "Receiving agent of an agent_message"),
    text_length=("integer", "Characters of the text as stored"),
    text_truncated=("boolean", "The text was cut to 100,000 characters"),
    redactions=("integer", "Secret-shaped strings replaced by [REDACTED:...] in this record"),
    cli_version=("text", "Codex version that wrote the session"),
    mapping_revision=("integer", "Mapping revision that produced this record"),
)

SESSION_FIELDS = fields(
    session_id=("text", "Codex session (thread) id"),
    parent_session=("text", "Parent session of a subagent session"),
    is_subagent=("boolean", "A subagent session"),
    title=("text", "Thread name from session_index.jsonl, else the first prompt"),
    project_dir=("text", "Working directory"),
    git_branch=("text", "Git branch when the session started"),
    git_commit=("text", "Git commit when the session started"),
    git_repository=("text", "Git remote URL"),
    originator=("text", "Client that ran the session"),
    source=("text", "Session source Codex reports: cli, vscode, exec, mcp, ..."),
    cli_version=("text", "Codex version"),
    model_provider=("text", "Model provider, e.g. openai"),
    owner_messages=("integer", "Your messages so far"),
    agent_messages=("integer", "Agent messages so far"),
    agent_prompts=("integer", "Agent-written prompts so far"),
    reasoning_summaries=("integer", "Reasoning summaries so far"),
    tool_calls=("integer", "Tool calls so far"),
    models=("json", "Agent messages by model"),
    home=("text", "Label of the Codex home (CODEX_HOMES)"),
    mapping_revision=("integer", "Mapping revision that produced this record"),
)

SETTINGS = [
    (
        "homes",
        "CODEX_HOMES",
        None,
        "Codex homes to read: LABEL=PATH, comma-separated (default local=$CODEX_HOME or "
        "local=~/.codex)",
    ),
    ("archived", "CODEX_ARCHIVED", "true", "Also read archived_sessions/"),
    (
        "originators",
        "CODEX_ORIGINATORS",
        "all",
        "Keep only sessions from these originators, comma-separated (e.g. codex_cli_rs,"
        "codex_vscode to skip apps that run Codex for you); 'all' keeps every one",
    ),
    ("reasoning", "CODEX_REASONING", "true", "Keep reasoning summaries"),
    ("scrub", "CODEX_SCRUB", "true", "Replace secret-shaped strings with [REDACTED:...]"),
    (
        "owner_identifier",
        "CODEX_OWNER_IDENTIFIER",
        "codex:owner:owner",
        "Author identifier of your own messages, system:kind:value",
    ),
    ("owner_name", "CODEX_OWNER_NAME", None, "Display name on your messages"),
    ("batch_size", "CODEX_BATCH_SIZE", "500", "Messages between cursor checkpoints"),
]


def truthy(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


class Codex(Plugin):
    manifest = Manifest(
        name="codex",
        version="0.1.0",
        channels=[
            Channel(
                name=MESSAGES,
                description=(
                    "OpenAI Codex conversations: your prompts (author_kind owner), Codex's "
                    "replies and reasoning summaries (agent) and agent-written prompts "
                    "(agent_prompt); one thread per session, with tool calls summarised on the "
                    "reply that follows them"
                ),
                fields=MESSAGE_FIELDS,
                conversation_gap_s=3600,
            ),
            Channel(
                name=SESSIONS,
                description=(
                    "One reference record per Codex session: title, project folder, git "
                    "branch and commit, client, message and tool-call counts"
                ),
                fields=SESSION_FIELDS,
                is_reference=True,
            ),
        ],
        settings={
            key: Declaration(env=env, required=False, default=default, description=description)
            for key, env, default, description in SETTINGS
        },
        secrets={},
        run_mode="poll",
        default_interval_s=900,
        link_types=[],
    )

    def __init__(self):
        super().__init__()
        self.homes = parse_homes(self.settings["homes"])
        originators = split_list(self.settings["originators"] or "all")
        self.map_settings = {
            "owner_identifier": self.settings["owner_identifier"] or "codex:owner:owner",
            "owner_name": self.settings["owner_name"],
            "reasoning": truthy(self.settings["reasoning"] or "true"),
            "scrub": truthy(self.settings["scrub"] or "true"),
            "originators": None if originators in ([], ["all"]) else sorted(set(originators)),
        }
        if len(self.map_settings["owner_identifier"].split(":", 2)) != 3:
            raise ValueError("CODEX_OWNER_IDENTIFIER must be system:kind:value")
        self.archived = truthy(self.settings["archived"] or "true")
        try:
            self.batch_size = max(1, int(self.settings["batch_size"] or 500))
        except ValueError:
            raise ValueError("CODEX_BATCH_SIZE must be a whole number") from None
        self.config_key = hashlib.sha256(
            json.dumps(self.map_settings, sort_keys=True).encode()
        ).hexdigest()[:16]
        self.stats = {}

    def execute(self, client, mode, dry_run=False):
        report = super().execute(client, mode, dry_run)
        if self.stats:
            report["codex"] = self.stats
        return report

    def records(self, cursor, mode):
        state = copy.deepcopy(cursor or {})
        if state.get("revision") != MAPPING_REVISION or state.get("config") != self.config_key:
            state = {"revision": MAPPING_REVISION, "config": self.config_key, "files": {}}
        files = state["files"]

        def snapshot():
            out = {k: v for k, v in state.items() if k != "files"}
            out["files"] = {
                key: {k: (pack(v) if k == "s" else v) for k, v in entry.items()}
                for key, entry in files.items()
            }
            return copy.deepcopy(out)

        mapper = Mapper(self.map_settings)
        stats = self.stats = {"files_read": 0, "lines": 0, "compressed_read": 0}
        seen = set()
        since = 0
        read, failed = [], {}
        for label, home in self.homes:
            try:
                found = rollouts(home, self.archived)
            except HomeError as error:
                failed[label] = str(error)
                log.warning("Codex home %s not read: %s", label, error)
                continue
            read.append(label)
            names = session_names(home)
            live = set()
            for name, path in found:
                key = f"{label}/{name}"
                live.add(key)
                entry = files.get(key)
                if entry and "name" not in entry["s"]:
                    entry["s"] = unpack(entry["s"], name, label)
                index_title = names.get(entry["s"]["session"] if entry else None)
                if entry and index_title:
                    index_title = mapper.clean(one_line(index_title))[0]
                    if entry["s"]["titles"].get("index") != index_title:
                        entry["s"]["titles"]["index"] = index_title
                        entry["session_due"] = True
                compressed = is_compressed(path)
                try:
                    status = path.stat()
                    # A compressed rollout's length is unknown until it is
                    # read: its stamp says whether it changed since.
                    stamp = [status.st_size, status.st_mtime_ns] if compressed else None
                    if entry and not entry.get("session_due"):
                        if stamp and entry.get("stamp") == stamp:
                            continue
                        if not compressed and entry["offset"] == status.st_size:
                            continue
                    first = fingerprint(path)
                except (OSError, EOFError, ValueError) as error:
                    log.warning("Codex rollout %s not read: %s", key, type(error).__name__)
                    continue
                if first is None:
                    continue  # No complete first line yet.
                if (
                    not entry
                    or entry["fp"] != first
                    or (not compressed and entry["offset"] > status.st_size)
                ):
                    entry = files[key] = {"fp": first, "offset": 0, "s": new_state(name, label)}
                stats["files_read"] += 1
                stats["compressed_read"] += compressed
                try:
                    stream = lines(path, entry["offset"])
                    try:
                        head = [next(stream)]
                    except StopIteration:
                        head = []
                    except Truncated:
                        entry = files[key] = {"fp": first, "offset": 0, "s": new_state(name, label)}
                        stream, head = lines(path, 0), []
                    s = entry["s"]
                    for begin, end, line in itertools.chain(head, stream):
                        out = mapper.line(s, line, begin) if line else []
                        if begin == 0 and names.get(s["session"]):
                            s["titles"]["index"] = mapper.clean(one_line(names[s["session"]]))[0]
                        entry["offset"] = end
                        entry["session_due"] = True
                        stats["lines"] += 1
                        for message in out:
                            ident = (message["channel"], message["external_id"])
                            if ident in seen:
                                continue
                            seen.add(ident)
                            since += 1
                            yield message
                        if since >= self.batch_size:
                            since = 0
                            yield Checkpoint(cursor=snapshot())
                except (OSError, EOFError, ValueError) as error:
                    log.warning("Codex rollout %s stopped: %s", key, type(error).__name__)
                    continue  # Resumes from the last complete line next run.
                # Only a rollout read to its end is marked as unchanged.
                if stamp:
                    entry["stamp"] = stamp
                else:
                    entry.pop("stamp", None)
                if entry.get("session_due"):
                    record = mapper.session_record(s)
                    if record and (SESSIONS, record["external_id"]) not in seen:
                        seen.add((SESSIONS, record["external_id"]))
                        since += 1
                        yield record
                    entry["session_due"] = False
            for key in [k for k in files if k.startswith(label + "/") and k not in live]:
                del files[key]  # Deleted from disk; the records stay.
        if failed and read:
            for label, error in sorted(failed.items()):
                for channel in (MESSAGES, SESSIONS):
                    yield coverage(
                        channel=channel,
                        scope_key=label,
                        status="partial",
                        reasons=["source_error"],
                        detail={"error": error},
                    )
        stats["homes_read"] = len(read)
        final = snapshot()
        if final != cursor:  # An idle run leaves the stored cursor alone.
            yield Checkpoint(cursor=final)
        if failed and not read:
            raise HomeError(
                "no Codex home could be read: "
                + "; ".join(f"{label}: {error}" for label, error in sorted(failed.items()))
            )


def main(argv=None):
    run(Codex, argv)
