"""Claude Code conversations as a Throughline source.

Reads the JSONL transcripts Claude Code keeps under each configured home
(`projects/<encoded cwd>/<session>.jsonl`, plus subagent transcripts), live
and incrementally: a byte-offset cursor per file, re-read from the start
when a file is truncated or rewritten. Your prompts and the assistant's
replies become messages (author_kind owner / agent / agent_prompt); tool
calls are kept as compact metadata on the reply that follows them, never
their full output. One reference record per session carries its title,
folder, branch and counts. Secret-shaped strings are scrubbed before
anything leaves the plugin.
"""

import copy
import hashlib
import json
import logging

from throughline_sdk import Channel, Checkpoint, Declaration, Manifest, Plugin, coverage, run

from .files import HomeError, fingerprint, lines, parse_homes, read_json, split_list, transcripts
from .mapping import MAPPING_REVISION, MESSAGES, SESSIONS, Mapper, new_state, one_line, pack, unpack

__all__ = ["ClaudeCode", "MAPPING_REVISION", "main"]

log = logging.getLogger("throughline_claude_code")


def fields(**values):
    return [dict(name=k, type=v[0], description=v[1]) for k, v in values.items()]


MESSAGE_FIELDS = fields(
    role=(
        "text",
        "user, command (a slash command you ran), shell (a ! command), assistant, "
        "structured_output (a subagent's final answer given as structured data), thinking, "
        "compact_summary (Claude's summary when a conversation was compacted) or tool_calls "
        "(tool calls with no reply after them)",
    ),
    author_kind=(
        "text",
        "owner (you typed it), agent (written by the model) or agent_prompt (a user-role "
        "turn an agent wrote: a subagent's brief, a coordinator message)",
    ),
    session_id=("text", "Claude Code session id"),
    agent_id=("text", "Subagent id when the message is in a subagent transcript"),
    is_subagent=("boolean", "The message is in a subagent (sidechain) transcript"),
    project_dir=("text", "Working directory of the session"),
    git_branch=("text", "Git branch at the time of the message"),
    entrypoint=(
        "text",
        "How Claude Code was started: cli, claude-vscode, sdk-ts, sdk-py, or an app "
        "embedding the Agent SDK (e.g. t3-code)",
    ),
    model=("text", "Model that wrote an agent message"),
    tool_calls=("integer", "Tool calls made since the previous message (the work behind this reply)"),
    tools=("json", "Those tool calls by tool name: {name: count}"),
    files=("json", "Files those tool calls named (read, written, edited), at most 50"),
    tool_errors=("integer", "Those tool calls that returned an error"),
    image_count=("integer", "Images attached to a prompt"),
    text_length=("integer", "Characters of the text as stored"),
    text_truncated=("boolean", "The text was cut to 100,000 characters"),
    redactions=("integer", "Secret-shaped strings replaced by [REDACTED:...] in this record"),
    claude_version=("text", "Claude Code version that wrote the line"),
    mapping_revision=("integer", "Mapping revision that produced this record"),
)

SESSION_FIELDS = fields(
    session_id=("text", "Claude Code session id"),
    agent_id=("text", "Subagent id (subagent transcripts)"),
    is_subagent=("boolean", "A subagent transcript"),
    agent_type=("text", "Subagent type, e.g. general-purpose, Explore"),
    workflow_run=("text", "Workflow run the subagent belonged to"),
    title=("text", "Session title: your custom title, else Claude's, else the first prompt"),
    project_dir=("text", "Working directory"),
    git_branch=("text", "Latest git branch"),
    entrypoint=("text", "How Claude Code was started"),
    claude_version=("text", "Latest Claude Code version seen"),
    owner_messages=("integer", "Your messages so far"),
    agent_messages=("integer", "Agent messages so far"),
    agent_prompts=("integer", "Agent-written prompts so far"),
    tool_calls=("integer", "Tool calls so far"),
    models=("json", "Agent messages by model"),
    home=("text", "Label of the Claude Code home (CLAUDE_CODE_HOMES)"),
    mapping_revision=("integer", "Mapping revision that produced this record"),
)

SETTINGS = [
    (
        "homes",
        "CLAUDE_CODE_HOMES",
        None,
        "Claude Code homes to read: LABEL=PATH, comma-separated (default "
        "local=$CLAUDE_CONFIG_DIR or local=~/.claude)",
    ),
    (
        "entrypoints",
        "CLAUDE_CODE_ENTRYPOINTS",
        "all",
        "Keep only lines from these entrypoints, comma-separated (e.g. cli,claude-vscode "
        "to skip sessions other apps ran through the Agent SDK); 'all' keeps every one",
    ),
    ("subagents", "CLAUDE_CODE_SUBAGENTS", "true", "Read subagent transcripts too"),
    ("thinking", "CLAUDE_CODE_THINKING", "false", "Also keep the model's thinking text"),
    ("scrub", "CLAUDE_CODE_SCRUB", "true", "Replace secret-shaped strings with [REDACTED:...]"),
    (
        "owner_identifier",
        "CLAUDE_CODE_OWNER_IDENTIFIER",
        "claude_code:owner:owner",
        "Author identifier of your own messages, system:kind:value",
    ),
    ("owner_name", "CLAUDE_CODE_OWNER_NAME", None, "Display name on your messages"),
    ("batch_size", "CLAUDE_CODE_BATCH_SIZE", "500", "Messages between cursor checkpoints"),
]


def truthy(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


class ClaudeCode(Plugin):
    manifest = Manifest(
        name="claude_code",
        version="0.1.0",
        channels=[
            Channel(
                name=MESSAGES,
                description=(
                    "Claude Code conversations: your prompts (author_kind owner), Claude's "
                    "replies (agent) and agent-written prompts to subagents (agent_prompt); one "
                    "thread per session or subagent transcript, with tool calls summarised on "
                    "the reply that follows them"
                ),
                fields=MESSAGE_FIELDS,
                conversation_gap_s=3600,
            ),
            Channel(
                name=SESSIONS,
                description=(
                    "One reference record per Claude Code session and subagent transcript: "
                    "title, project folder, branch, entrypoint, message and tool-call counts"
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
        entrypoints = split_list(self.settings["entrypoints"] or "all")
        self.map_settings = {
            "owner_identifier": self.settings["owner_identifier"] or "claude_code:owner:owner",
            "owner_name": self.settings["owner_name"],
            "thinking": truthy(self.settings["thinking"]),
            "scrub": truthy(self.settings["scrub"] if self.settings["scrub"] is not None else "true"),
            "entrypoints": None if entrypoints in ([], ["all"]) else sorted(set(entrypoints)),
            "subagents": truthy(self.settings["subagents"] or "true"),
        }
        if len(self.map_settings["owner_identifier"].split(":", 2)) != 3:
            raise ValueError("CLAUDE_CODE_OWNER_IDENTIFIER must be system:kind:value")
        try:
            self.batch_size = max(1, int(self.settings["batch_size"] or 500))
        except ValueError:
            raise ValueError("CLAUDE_CODE_BATCH_SIZE must be a whole number") from None
        self.config_key = hashlib.sha256(
            json.dumps(self.map_settings, sort_keys=True).encode()
        ).hexdigest()[:16]
        self.stats = {}

    def execute(self, client, mode, dry_run=False):
        report = super().execute(client, mode, dry_run)
        if self.stats:
            report["claude_code"] = self.stats
        return report

    def open_file(self, rel, path, kind, label, mapper):
        state = new_state(rel, kind, label)
        if kind == "subagent":
            meta = read_json(path.with_name(path.name.removesuffix(".jsonl") + ".meta.json"))
            if isinstance(meta.get("agentType"), str):
                state["agent_type"] = meta["agentType"]
            if isinstance(meta.get("description"), str) and meta["description"].strip():
                state["titles"]["agent_description"] = mapper.clean(one_line(meta["description"]))[0]
        return state

    def records(self, cursor, mode):
        state = copy.deepcopy(cursor or {})
        if state.get("revision") != MAPPING_REVISION or state.get("config") != self.config_key:
            state = {"revision": MAPPING_REVISION, "config": self.config_key, "files": {}}
        files = state["files"]

        def snapshot():
            # Files touched this run hold their state unpacked in "s".
            out = {k: v for k, v in state.items() if k != "files"}
            out["files"] = {
                key: {k: (pack(v) if k == "s" else v) for k, v in entry.items()}
                for key, entry in files.items()
            }
            return copy.deepcopy(out)

        mapper = Mapper(self.map_settings)
        stats = self.stats = {"files_read": 0, "lines": 0, "skipped_entrypoint": 0}
        seen = set()
        since = 0
        read, failed = [], {}
        for label, home in self.homes:
            try:
                found = transcripts(home, self.map_settings["subagents"])
            except HomeError as error:
                failed[label] = str(error)
                log.warning("Claude Code home %s not read: %s", label, error)
                continue
            read.append(label)
            live = set()
            for rel, path, kind in found:
                key = f"{label}/{rel}"
                live.add(key)
                entry = files.get(key)
                try:
                    size = path.stat().st_size
                    if entry and entry["offset"] == size and not entry.get("session_due"):
                        continue
                    first = fingerprint(path)
                except OSError as error:
                    log.warning("Claude Code transcript %s not read: %s", key, error.strerror)
                    continue
                if first is None:
                    continue  # No complete first line yet.
                if not entry or entry["fp"] != first or entry["offset"] > size:
                    entry = files[key] = {
                        "fp": first,
                        "offset": 0,
                        "s": self.open_file(rel, path, kind, label, mapper),
                    }
                s = entry["s"]
                if "thread" not in s:  # Packed by an earlier run.
                    s = entry["s"] = unpack(s, rel, kind, label)
                skipped_before = s["counts"]["skipped"]
                stats["files_read"] += 1
                try:
                    for _, end, line in lines(path, entry["offset"]):
                        out = mapper.line(s, line) if line else []
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
                except OSError as error:
                    log.warning("Claude Code transcript %s stopped: %s", key, error.strerror)
                stats["skipped_entrypoint"] += s["counts"]["skipped"] - skipped_before
                if entry.get("session_due"):
                    record = mapper.session_record(s)
                    if record and (SESSIONS, record["external_id"]) not in seen:
                        seen.add((SESSIONS, record["external_id"]))
                        since += 1
                        yield record
                    entry["session_due"] = False
            for key in [k for k in files if k.startswith(label + "/") and k not in live]:
                del files[key]  # Deleted by Claude Code's cleanup; the records stay.
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
                "no Claude Code home could be read: "
                + "; ".join(f"{label}: {error}" for label, error in sorted(failed.items()))
            )


def main(argv=None):
    run(ClaudeCode, argv)
