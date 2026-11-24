#!/usr/bin/env python3
"""Hermes Agent sessions: a standard-library-only raw fetcher.

Copy this one file to the host that runs Hermes (Python 3.12+) and run

    python fetch.py export --out hermes-export.jsonl [options]

It reads the Hermes session store and writes raw records as JSON lines. Three
readers, in order of preference:

- cli       runs Hermes's own export (`<HERMES_CLI> sessions export -`) and
            reads its JSONL from stdout through a private temporary file.
- file      reads a JSONL file that `hermes sessions export FILE` wrote.
- snapshot  takes a consistent read-only copy of `state.db` with the SQLite
            online backup API from a `file:...?mode=ro` connection into a
            private temporary file, reads the copy and deletes it. The live
            file is never written, never opened read-write, and only read
            for the duration of one backup step.

`auto` (the default) uses `cli` when HERMES_CLI is set or `hermes` is on the
PATH, `file` when HERMES_EXPORT_FILE is set, and `snapshot` otherwise.

No credential is needed or read. Hidden reasoning (reasoning,
reasoning_content, reasoning_details, codex_reasoning_items) and the raw API
content are never written to the export; only whether reasoning was present.
A session's system prompt is written as a sha256 and a length; its text only
with HERMES_STORE_SYSTEM_PROMPT=1. Every string is cut to HERMES_MAX_TEXT
characters (the cut is listed in `truncated`, the original size in `sizes`).

Record kinds (one JSON object per line, every line carries `run`):

- run       run header: fetched_at, reader, mode, fetcher_version, schema
- session   one sessions row (`payload`), `id` = session id, `system_prompt`
- message   one messages row (`payload`), `id` = message id, `session_id`,
            `reply_to` ({id, uid} of the assistant row that made the tool
            call a tool row answers, when known), `reasoning_present`
- run_end   complete flag, counts, why it stopped, the message watermark

The plugin package imports this module for its live mode, so the mapping is
identical whether data arrives live or from an export file.
"""

import argparse
import gzip
import hashlib
import heapq
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from datetime import UTC, date, datetime, time as dtime
from pathlib import Path

FETCHER_VERSION = 1
# The upstream source this reader was written against (see README).
UPSTREAM = "NousResearch/hermes-agent@95a1d228 (state.db schema version 33)"
CONTENT_JSON_PREFIX = "\x00json:"
# Re-read sessions active this long before the previous run started.
SESSION_OVERLAP_S = 3600
# Never written to an export: hidden reasoning and the raw provider request.
REASONING_KEYS = (
    "reasoning",
    "reasoning_content",
    "reasoning_details",
    "codex_reasoning_items",
)
DROPPED_MESSAGE_KEYS = REASONING_KEYS + ("api_content", "codex_message_items")
# Operational session columns that change without the conversation changing;
# left out so a session record is versioned only by meaningful changes.
VOLATILE_SESSION_KEYS = frozenset(
    {
        "runtime_revision",
        "runtime_generation",
        "prompt_cache_clock",
        "last_read_at",
        "last_activity_at",
        "last_activity_description",
        "last_activity_provenance",
        "compression_failure_cooldown_until",
        "compression_failure_error",
        "compression_fallback_streak",
        "compression_ineffective_count",
        "compression_recovery_deadline",
        "compression_overload_streak",
        "handoff_state",
        "handoff_platform",
        "handoff_error",
        "git_metadata_generation",
        "expiry_finalized",
        "transcript_revision",
        "tool_names",
        "messages",
        "timings",
        "segments",
    }
)
# JSON text columns decoded into objects so every reader yields one shape.
JSON_MESSAGE_KEYS = ("tool_calls", "display_metadata")
REQUIRED_MESSAGE_COLUMNS = ("id", "session_id", "role", "timestamp")
# Key names of credentials inside JSON columns (model_config, origin_json,
# display_metadata); their values never reach an export.
SECRET_KEY = re.compile(
    r"api[_-]?key|secret|password|passwd|authorization|cookie|credential"
    r"|private[_-]?key|bearer|(^|[_-])token$",
    re.IGNORECASE,
)
# Columns kept when a row is too large even after hard cuts.
CORE_KEYS = (
    "id",
    "session_id",
    "role",
    "content",
    "timestamp",
    "tool_call_id",
    "tool_name",
    "message_uid",
    "active",
    "compacted",
    "source",
    "user_id",
    "started_at",
    "ended_at",
    "title",
    "model",
)
# A record's payload may hold this many times HERMES_MAX_TEXT characters.
RECORD_BUDGET = 4
REQUIRED_SESSION_COLUMNS = ("id", "started_at")

# (key, environment variable, default, description). Shared with the plugin
# manifest so one table documents both the fetcher and the plugin.
SETTINGS = [
    ("reader", "HERMES_READER", "auto", "auto, cli, file, snapshot or readonly"),
    (
        "home",
        "HERMES_HOME",
        "~/.hermes",
        "Hermes home directory (holds state.db); /opt/data in the Hermes container image",
    ),
    ("db", "HERMES_DB", None, "state.db path; default <HERMES_HOME>/state.db"),
    (
        "cli",
        "HERMES_CLI",
        None,
        "Command that runs the Hermes CLI, e.g. 'hermes' or 'docker exec -i hermes hermes'",
    ),
    ("cli_timeout_s", "HERMES_CLI_TIMEOUT_S", "900", "Seconds the CLI export may take"),
    (
        "export_file",
        "HERMES_EXPORT_FILE",
        None,
        "A JSONL file written by `hermes sessions export` (file reader)",
    ),
    (
        "snapshot_dir",
        "HERMES_SNAPSHOT_DIR",
        None,
        "Directory for the private temporary snapshot; default the system temp dir",
    ),
    (
        "busy_timeout_s",
        "HERMES_BUSY_TIMEOUT_S",
        "30",
        "Seconds to wait for a lock before the read-only backup step",
    ),
    (
        "max_messages",
        "HERMES_MAX_MESSAGES",
        "20000",
        "Messages per run (0 = no cap); the rest follow in the next run",
    ),
    (
        "max_text",
        "HERMES_MAX_TEXT",
        "100000",
        "Characters kept of any one string (content, tool output, arguments)",
    ),
    ("since", "HERMES_SINCE", None, "Backfill lower bound: date or ISO time"),
    (
        "until",
        "HERMES_UNTIL",
        None,
        "Backfill upper bound (exclusive): date or ISO time",
    ),
    (
        "store_system_prompt",
        "HERMES_STORE_SYSTEM_PROMPT",
        "0",
        "1 to export the system prompt text (default: sha256 and length only)",
    ),
    (
        "exclude_sources",
        "HERMES_EXCLUDE_SOURCES",
        None,
        "Comma-separated session sources never exported (e.g. test)",
    ),
    (
        "redact",
        "HERMES_REDACT",
        "0",
        "1 to pass --redact to the CLI export (Hermes scrubs keys and tokens from text)",
    ),
]
# Hermes's session store needs no credential.
SECRETS = []


class FetchError(RuntimeError):
    """The session store could not be read."""


def truthy(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def split_list(value):
    return [p.strip() for p in str(value or "").split(",") if p.strip()]


def parse_moment(value):
    """A UTC datetime from a date, an ISO time or an epoch number."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value), UTC)
    text = str(value).strip()
    if len(text) == 10:
        return datetime.combine(date.fromisoformat(text), dtime(0), UTC)
    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def epoch(value):
    """Epoch seconds from a stored REAL, a numeric string or an ISO time; else None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        return parse_moment(value).timestamp()
    except (TypeError, ValueError):
        return None


def env_values(environ):
    return {key: environ.get(env, default) for key, env, default, _ in SETTINGS}


def config(values):
    """Validated fetch configuration from setting values (None = default)."""
    defaults = {key: default for key, _, default, _ in SETTINGS}
    merged = dict(defaults)
    merged.update({k: v for k, v in values.items() if v is not None})
    reader = str(merged["reader"]).strip().lower()
    if reader not in ("auto", "cli", "file", "snapshot", "readonly"):
        raise ValueError("HERMES_READER must be auto, cli, file, snapshot or readonly")
    cfg = {"reader": reader}
    cfg["home"] = os.path.expanduser(str(merged["home"]))
    cfg["db"] = str(merged["db"] or os.path.join(cfg["home"], "state.db"))
    cfg["cli"] = shlex.split(merged["cli"]) if merged["cli"] else None
    cfg["export_file"] = merged["export_file"]
    cfg["snapshot_dir"] = merged["snapshot_dir"]
    for key, low in (
        ("cli_timeout_s", 1),
        ("busy_timeout_s", 0),
        ("max_messages", 0),
        ("max_text", 1000),
    ):
        try:
            number = int(merged[key])
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be an integer") from None
        if number < low:
            raise ValueError(f"{key} must be at least {low}")
        cfg[key] = number
    for key in ("since", "until"):
        try:
            cfg[key] = parse_moment(merged[key]).timestamp() if merged[key] else None
        except (TypeError, ValueError):
            raise ValueError(
                f"HERMES_{key.upper()} must be a date or ISO time"
            ) from None
    if cfg["since"] is not None and cfg["until"] is not None:
        if cfg["until"] <= cfg["since"]:
            raise ValueError("HERMES_UNTIL must be after HERMES_SINCE")
    cfg["store_system_prompt"] = truthy(merged["store_system_prompt"])
    cfg["exclude_sources"] = set(split_list(merged["exclude_sources"]))
    cfg["redact"] = truthy(merged["redact"])
    return cfg


def choose_reader(cfg):
    if cfg["reader"] != "auto":
        return cfg["reader"]
    if cfg["cli"] or shutil.which("hermes"):
        return "cli"
    if cfg["export_file"]:
        return "file"
    return "snapshot"


# --- Row normalization ---------------------------------------------------------


def decode_content(value):
    """Hermes stores list/dict content as a sentinel-prefixed JSON string."""
    if isinstance(value, str) and value.startswith(CONTENT_JSON_PREFIX):
        try:
            return json.loads(value[len(CONTENT_JSON_PREFIX) :])
        except ValueError:
            return value
    return value


def decode_json(value):
    if isinstance(value, str) and value.strip()[:1] in ("[", "{"):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def scrub(value):
    """Credential-named keys of a decoded JSON column, recursively, with the value dropped."""
    if isinstance(value, dict):
        return {
            k: ("[removed]" if SECRET_KEY.search(str(k)) else scrub(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


def jsonable(value):
    """True for values that survive JSON unchanged (blobs do not)."""
    return not isinstance(value, (bytes, bytearray, memoryview))


def bound(value, limit, path, cut):
    """`value` with every string cut to `limit` characters; cuts listed in `cut`."""
    if isinstance(value, str):
        # PostgreSQL text and JSONB reject U+0000 even though SQLite and JSON
        # permit it. Keep the position and meaning visible without emitting an
        # invalid database character.
        value = value.replace("\x00", "\\u0000")
        if len(value) > limit:
            cut.append(path)
            return value[:limit]
        return value
    if isinstance(value, dict):
        return {
            k: bound(v, limit, f"{path}.{k}" if path else str(k), cut)
            for k, v in value.items()
            if jsonable(v)
        }
    if isinstance(value, (list, tuple)):
        return [bound(v, limit, f"{path}[{i}]", cut) for i, v in enumerate(value)]
    if isinstance(value, float) and value != value:  # NaN is not JSON
        return None
    return value


def text_length(value):
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True))


def normalize_message(row):
    """One shape for a messages row from any reader (columns kept, types aligned)."""
    row = {k: v for k, v in dict(row).items() if jsonable(v)}
    row["content"] = decode_content(row.get("content"))
    for key in JSON_MESSAGE_KEYS:
        if key in row:
            row[key] = decode_json(row[key])
    if isinstance(row.get("display_metadata"), (dict, list)):
        row["display_metadata"] = scrub(row["display_metadata"])
    summary = row.pop("_compressed_summary", None)
    if summary:
        row["_compressed_summary"] = True
    for key in ("id", "observed", "active", "compacted"):
        if isinstance(row.get(key), bool):
            row[key] = int(row[key])
    return row


def message_record(run, row, limit):
    row = normalize_message(row)
    has_flag = row.pop("_tl_reasoning_present", None)
    if has_flag is not None:
        reasoning = bool(has_flag)
    else:
        reasoning = any(
            row.get(key) not in (None, "", [], {}, "[]") for key in REASONING_KEYS
        )
    for key in DROPPED_MESSAGE_KEYS:
        row.pop(key, None)
    sizes = {"content": text_length(row.get("content"))}
    cut = []
    payload = bound_payload(row, limit, cut)
    return {
        "kind": "message",
        "run": run,
        "id": int(row["id"]),
        "session_id": str(row["session_id"]),
        "payload": payload,
        "reasoning_present": reasoning,
        "sizes": sizes,
        "truncated": cut,
    }


def system_prompt_summary(text, stored_hash, keep_text, limit):
    if not text and not stored_hash:
        return None
    summary = {"hermes_hash": stored_hash or None, "sha256": None, "length": None}
    if text:
        summary["sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        summary["length"] = len(text)
        if keep_text:
            summary["text"] = text.replace("\x00", "\\u0000")[:limit]
    return summary


def session_record(run, row, prompt_text, keep_prompt, limit):
    row = {k: v for k, v in dict(row).items() if jsonable(v)}
    stored_hash = row.pop("system_prompt_hash", None)
    inline = row.pop("system_prompt", None)
    text = prompt_text if prompt_text else inline
    for key in ("model_config", "origin_json"):
        if key in row:
            row[key] = scrub(decode_json(row[key]))
    payload = {k: v for k, v in row.items() if k not in VOLATILE_SESSION_KEYS}
    cut = []
    payload = bound_payload(payload, limit, cut)
    return {
        "kind": "session",
        "run": run,
        "id": str(row["id"]),
        "payload": payload,
        "system_prompt": system_prompt_summary(
            text if isinstance(text, str) else None, stored_hash, keep_prompt, limit
        ),
        "truncated": cut,
    }


def call_target(message_id, row):
    """How a tool row names the assistant row that made its call."""
    uid = row.get("message_uid")
    return {"id": int(message_id), "uid": str(uid) if uid else None}


def bound_payload(row, limit, cut):
    """`row` bounded per string, then as a whole to RECORD_BUDGET x `limit` characters.

    A row with many long strings (a hundred tool calls, a huge argument list)
    is cut harder, string by string, until it fits; as a last resort only its
    identifying columns and the cut content are kept.
    """
    budget = RECORD_BUDGET * limit
    for each in (limit, limit // 8, 500):
        attempt = []
        payload = bound(row, each, "", attempt)
        if len(json.dumps(payload, ensure_ascii=False)) <= budget:
            cut.extend(attempt)
            return payload
    keep = {k: row.get(k) for k in CORE_KEYS if k in row}
    payload = bound(keep, 500, "", cut)
    cut.append("*")
    return payload


def call_ids(tool_calls):
    ids = []
    for call in tool_calls if isinstance(tool_calls, list) else []:
        if isinstance(call, dict) and call.get("id"):
            ids.append(str(call["id"]))
    return ids


def session_activity(row):
    """The latest moment a session row says it was active (epoch seconds)."""
    values = [epoch(row.get(k)) for k in ("last_activity_at", "ended_at", "started_at")]
    values = [v for v in values if v is not None]
    return max(values) if values else None


# --- Readers ---------------------------------------------------------------------


class Snapshot:
    """A consistent read-only copy of state.db in a private temporary directory."""

    def __init__(self, db_path, snapshot_dir=None, busy_timeout_s=30):
        self.db_path = Path(db_path)
        self.snapshot_dir = snapshot_dir
        self.busy_timeout_s = busy_timeout_s
        self.directory = None
        self.conn = None

    def __enter__(self):
        if not self.db_path.is_file():
            raise FetchError(f"no Hermes session store at {self.db_path}")
        if self.snapshot_dir:
            os.makedirs(self.snapshot_dir, exist_ok=True)
        self.directory = tempfile.mkdtemp(
            prefix="hermes-snapshot-", dir=self.snapshot_dir
        )
        target = os.path.join(self.directory, "snapshot.db")
        uri = self.db_path.resolve().as_uri() + "?mode=ro"
        try:
            source = sqlite3.connect(uri, uri=True, timeout=self.busy_timeout_s)
        except sqlite3.Error as error:
            self.close()
            raise FetchError(
                f"cannot open the session store read-only: {error}"
            ) from None
        try:
            copy = sqlite3.connect(target)
            # One step (pages=-1): a single read transaction, so the copy is
            # consistent; under WAL it never blocks Hermes's writers.
            source.backup(copy, pages=-1)
        except sqlite3.Error as error:
            source.close()
            self.close()
            raise FetchError(
                f"read-only backup of the session store failed: {error}"
            ) from None
        source.close()
        copy.row_factory = sqlite3.Row
        self.conn = copy
        return self

    def close(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None
        if self.directory:
            shutil.rmtree(self.directory, ignore_errors=True)
            self.directory = None

    def __exit__(self, *exc):
        self.close()
        return False


class ReadOnlyConnection:
    """Direct SQLite connection (`?mode=ro`, query_only=ON): no copy or write."""

    def __init__(self, db_path, busy_timeout_s=30):
        self.db_path = Path(db_path)
        self.busy_timeout_s = busy_timeout_s
        self.conn = None

    def __enter__(self):
        if not self.db_path.is_file():
            raise FetchError(f"no Hermes session store at {self.db_path}")
        uri = self.db_path.resolve().as_uri() + "?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=self.busy_timeout_s)
            conn.execute("PRAGMA query_only = ON")
            conn.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_s) * 1000}")
        except sqlite3.Error as error:
            raise FetchError(
                f"cannot open the session store read-only: {error}"
            ) from None
        conn.row_factory = sqlite3.Row
        self.conn = conn
        return self

    def close(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def __exit__(self, *exc):
        self.close()
        return False


def quote_ident(name):
    return '"' + str(name).replace('"', '""') + '"'


def table_columns(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


class Fetcher:
    """One run over the session store; `report` describes it afterwards."""

    def __init__(self, cfg, *, mode="sync", now=None, log=None, popen=None):
        self.cfg = cfg
        self.mode = mode
        self.now = now or (lambda: datetime.now(UTC))
        self.log = log or (lambda text: None)
        self.popen = popen or subprocess.Popen
        self.report = {}

    # The message window of this run: (after_id, since, until).
    def window(self, state):
        if self.mode == "backfill":
            return (
                int(state.get("backfill_after") or 0),
                self.cfg["since"],
                self.cfg["until"],
            )
        return int(state.get("after_id") or 0), None, None

    def wanted_session(self, row, state, touched):
        sid = str(row.get("id"))
        if sid in touched:
            return True
        if self.mode == "backfill":
            started = epoch(row.get("started_at"))
            if started is None:
                return self.cfg["since"] is None and self.cfg["until"] is None
            return (self.cfg["since"] is None or started >= self.cfg["since"]) and (
                self.cfg["until"] is None or started < self.cfg["until"]
            )
        previous = state.get("since_ts")
        if previous is None:
            return True
        active = session_activity(row)
        return active is None or active >= float(previous) - SESSION_OVERLAP_S

    def excluded(self, row):
        return str(row.get("source") or "") in self.cfg["exclude_sources"]

    def run(self, state):
        """Yield the run's records; update `state` (the fetch watermarks) in place."""
        started = self.now()
        run = uuid.uuid4().hex
        reader = choose_reader(self.cfg)
        self.report = {
            "reader": reader,
            "mode": self.mode,
            "messages": 0,
            "sessions": 0,
        }
        if reader in ("snapshot", "readonly"):
            records = self.run_sqlite(run, started, state, reader)
        else:
            records = self.run_jsonl(run, started, state, reader)
        yield from records

    def header(self, run, started, reader, schema):
        return {
            "kind": "run",
            "run": run,
            "fetched_at": started.isoformat(),
            "fetcher_version": FETCHER_VERSION,
            "reader": reader,
            "mode": self.mode,
            "schema": schema,
            "upstream": UPSTREAM,
        }

    def finish(self, run, state, started, emitted_ids, more, highest):
        """The run_end record; moves the watermarks."""
        complete = not more
        last = max(emitted_ids) if emitted_ids else None
        if self.mode == "backfill":
            if more:
                state["backfill_after"] = last
            else:
                state.pop("backfill_after", None)
                if self.cfg["until"] is None and highest is not None:
                    state["after_id"] = max(int(state.get("after_id") or 0), highest)
                if self.cfg["since"] is None and self.cfg["until"] is None:
                    # Every session was read: the next sync needs only active ones.
                    state["since_ts"] = started.timestamp()
        else:
            if more:
                state["after_id"] = last
            else:
                if highest is not None:
                    state["after_id"] = max(int(state.get("after_id") or 0), highest)
                state["since_ts"] = started.timestamp()
        self.report.update(
            complete=complete,
            stopped="max_messages" if more else None,
            after_id=state.get("after_id"),
            backfill_after=state.get("backfill_after"),
            since_ts=state.get("since_ts"),
        )
        return {
            "kind": "run_end",
            "run": run,
            "complete": complete,
            "stopped": "max_messages" if more else None,
            "messages": self.report["messages"],
            "sessions": self.report["sessions"],
            "last_message_id": last,
        }

    # -- sqlite readers (snapshot and direct readonly) --

    @staticmethod
    def message_select_sql(message_columns):
        present_reasoning = [k for k in REASONING_KEYS if k in message_columns]
        kept = [c for c in message_columns if c not in DROPPED_MESSAGE_KEYS]
        parts = [quote_ident(c) for c in kept]
        if present_reasoning:
            checks = " OR ".join(
                f"({quote_ident(k)} IS NOT NULL AND {quote_ident(k)} NOT IN ('', '[]', '{{}}'))"
                for k in present_reasoning
            )
            parts.append(f"(CASE WHEN {checks} THEN 1 ELSE 0 END) AS _tl_reasoning_present")
        return ", ".join(parts)

    @staticmethod
    def load_prompts(conn, tables, hashes):
        if "system_prompts" not in tables or not hashes:
            return {}
        cols = table_columns(conn, "system_prompts")
        if "hash" not in cols or "prompt" not in cols:
            return {}
        prompts = {}
        unique = sorted(h for h in hashes if h)
        for i in range(0, len(unique), 500):
            chunk = unique[i : i + 500]
            placeholders = ",".join("?" for _ in chunk)
            for row in conn.execute(
                f"SELECT hash, prompt FROM system_prompts WHERE hash IN ({placeholders})",
                chunk,
            ):
                prompts[row[0]] = row[1]
        return prompts

    @staticmethod
    def load_incremental_sessions(conn, session_columns, touched, cutoff):
        sessions = {}
        touched_list = sorted(touched)
        for i in range(0, len(touched_list), 500):
            chunk = touched_list[i : i + 500]
            placeholders = ",".join("?" for _ in chunk)
            for r in conn.execute(
                f"SELECT * FROM sessions WHERE id IN ({placeholders})", chunk
            ):
                sessions[str(r["id"])] = dict(r)
        ts_cols = [
            c
            for c in ("last_activity_at", "ended_at", "started_at")
            if c in session_columns
        ]
        if ts_cols:
            # A session active since cutoff (or with NULL timestamps) may have updated.
            conds = " OR ".join(f"{quote_ident(c)} >= ?" for c in ts_cols)
            null_cond = " AND ".join(f"{quote_ident(c)} IS NULL" for c in ts_cols)
            sql = f"SELECT * FROM sessions WHERE ({conds}) OR ({null_cond})"
            for r in conn.execute(sql, [cutoff] * len(ts_cols)):
                sessions[str(r["id"])] = dict(r)
        return sessions

    def run_sqlite(self, run, started, state, reader):
        cfg = self.cfg
        opener = (
            Snapshot(cfg["db"], cfg["snapshot_dir"], cfg["busy_timeout_s"])
            if reader == "snapshot"
            else ReadOnlyConnection(cfg["db"], cfg["busy_timeout_s"])
        )
        with opener as handle:
            conn = handle.conn
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "sessions" not in tables or "messages" not in tables:
                raise FetchError("the session store has no sessions/messages tables")
            message_columns = table_columns(conn, "messages")
            session_columns = table_columns(conn, "sessions")
            for name in REQUIRED_MESSAGE_COLUMNS:
                if name not in message_columns:
                    raise FetchError(f"messages table lacks column {name}")
            for name in REQUIRED_SESSION_COLUMNS:
                if name not in session_columns:
                    raise FetchError(f"sessions table lacks column {name}")
            version = None
            if "schema_version" in tables:
                found = conn.execute(
                    "SELECT max(version) FROM schema_version"
                ).fetchone()
                version = found[0] if found else None
            yield self.header(
                run,
                started,
                reader,
                {
                    "version": version,
                    "session_columns": session_columns,
                    "message_columns": message_columns,
                },
            )
            incremental_sessions = (
                self.mode == "sync"
                and state.get("since_ts") is not None
                and not cfg["exclude_sources"]
            )
            sessions = None
            if not incremental_sessions:
                sessions = {
                    str(r["id"]): dict(r)
                    for r in conn.execute("SELECT * FROM sessions")
                }
            after, since, until = self.window(state)
            select_cols = self.message_select_sql(message_columns)
            sql = f"SELECT {select_cols} FROM messages WHERE id > ?"
            args = [after]
            if since is not None:
                sql += " AND timestamp >= ?"
                args.append(since)
            if until is not None:
                sql += " AND timestamp < ?"
                args.append(until)
            sql += " ORDER BY id"
            cap = cfg["max_messages"]
            if cap and not cfg["exclude_sources"]:
                sql += " LIMIT ?"
                args.append(cap + 1)
            emitted, touched, calls, more = [], set(), {}, False
            highest = conn.execute("SELECT max(id) FROM messages").fetchone()[0]
            cursor = conn.execute(sql, args)
            while not more:
                rows = cursor.fetchmany(500)
                if not rows:
                    break
                for row in rows:
                    sid = str(row["session_id"])
                    if sessions is not None:
                        session = sessions.get(sid)
                        if session is not None and self.excluded(session):
                            continue
                    if cap and len(emitted) >= cap:
                        more = True
                        break
                    record = message_record(run, row, cfg["max_text"])
                    payload = record["payload"]
                    for call in call_ids(payload.get("tool_calls")):
                        calls[(sid, call)] = call_target(record["id"], payload)
                    target = payload.get("tool_call_id")
                    if payload.get("role") == "tool" and target:
                        found = calls.get((sid, str(target)))
                        if found is None:
                            found = self.find_call(
                                conn, sid, str(target), record["id"], message_columns
                            )
                        record["reply_to"] = found
                    emitted.append(record["id"])
                    touched.add(sid)
                    self.report["messages"] += 1
                    yield record
            if sessions is None:
                cutoff = float(state["since_ts"]) - SESSION_OVERLAP_S
                sessions = self.load_incremental_sessions(
                    conn, session_columns, touched, cutoff
                )
            emit_sids = []
            for sid in sorted(sessions):
                row = sessions[sid]
                if self.excluded(row):
                    continue
                if more and (
                    self.mode == "backfill" or state.get("since_ts") is None
                ):
                    if sid not in touched:
                        continue
                elif not self.wanted_session(row, state, touched):
                    continue
                emit_sids.append(sid)
            needed_hashes = {
                sessions[sid].get("system_prompt_hash")
                for sid in emit_sids
                if not sessions[sid].get("system_prompt")
                and sessions[sid].get("system_prompt_hash")
            }
            prompts = self.load_prompts(conn, tables, needed_hashes)
            for sid in emit_sids:
                row = sessions[sid]
                text = row.get("system_prompt") or prompts.get(
                    row.get("system_prompt_hash")
                )
                self.report["sessions"] += 1
                yield session_record(
                    run, row, text, cfg["store_system_prompt"], cfg["max_text"]
                )
            yield self.finish(run, state, started, emitted, more, highest)

    @staticmethod
    def find_call(conn, session_id, call_id, before, message_columns=None):
        """The assistant row of `session_id` below `before` whose tool_calls name `call_id`."""
        cols = ["id", "tool_calls"]
        if message_columns is None or "message_uid" in message_columns:
            cols.append("message_uid")
        escaped = call_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = conn.execute(
            f"SELECT {', '.join(cols)} FROM messages WHERE session_id = ? "
            "AND role = 'assistant' AND tool_calls IS NOT NULL AND id < ? "
            "AND tool_calls LIKE ? ESCAPE '\\' ORDER BY id DESC LIMIT 20",
            (session_id, before, f"%{escaped}%"),
        ).fetchall()
        for row in rows:
            if call_id in call_ids(decode_json(row["tool_calls"])):
                return call_target(row["id"], dict(row))
        return None

    # -- cli and file readers (the `hermes sessions export` JSONL) --

    def jsonl_lines(self, path):
        with open(path, encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                line = line.strip()
                if not line.startswith("{"):
                    # A status line on stdout, not a session. Hermes prints its
                    # refusals there ("Error: ..."), sometimes with exit 0.
                    if line.startswith("Error"):
                        raise FetchError(f"the Hermes export refused: {line[:300]}")
                    continue
                try:
                    session = json.loads(line)
                except ValueError:
                    raise FetchError(f"export line {number} is not JSON") from None
                if isinstance(session, dict) and session.get("id"):
                    yield session

    @staticmethod
    def status_line(path):
        """The last non-JSON line Hermes printed to stdout (its error message)."""
        last = ""
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.strip() and not line.lstrip().startswith("{"):
                    last = line.strip()
        return last

    def cli_export(self, directory):
        """Run `<HERMES_CLI> sessions export -` into a private file; returns its path."""
        command = list(self.cfg["cli"] or ["hermes"]) + ["sessions", "export", "-"]
        if self.cfg["redact"]:
            command.append("--redact")
        target = os.path.join(directory, "hermes-sessions.jsonl")
        with open(target, "w", encoding="utf-8") as handle:
            try:
                process = self.popen(
                    command,
                    stdout=handle,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL,
                    text=True,
                )
            except OSError as error:
                raise FetchError(
                    f"cannot run the Hermes CLI: {error.strerror}"
                ) from None
            try:
                _, err = process.communicate(timeout=self.cfg["cli_timeout_s"])
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise FetchError("the Hermes CLI export timed out") from None
        if process.returncode != 0:
            tail = (err or "").strip().splitlines()[-1:] or [""]
            if not tail[0]:
                tail = [self.status_line(target)]
            raise FetchError(
                f"the Hermes CLI export failed (exit {process.returncode}): {tail[0][:300]}"
            )
        return target

    def run_jsonl(self, run, started, state, reader):
        cfg = self.cfg
        if cfg["snapshot_dir"]:
            os.makedirs(cfg["snapshot_dir"], exist_ok=True)
        # Private (0700) spool for the CLI's stdout; removed with everything in it.
        directory = tempfile.mkdtemp(prefix="hermes-export-", dir=cfg["snapshot_dir"])
        try:
            if reader == "cli":
                path = self.cli_export(directory)
            else:
                if not cfg["export_file"] or not os.path.isfile(cfg["export_file"]):
                    raise FetchError("HERMES_EXPORT_FILE does not name an export file")
                path = cfg["export_file"]
            yield from self.read_jsonl(run, started, state, reader, path)
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def candidate(self, message, window):
        after, since, until = window
        try:
            mid = int(message.get("id"))
        except (TypeError, ValueError):
            return None
        if mid <= after:
            return None
        at = epoch(message.get("timestamp"))
        if since is not None and (at is None or at < since):
            return None
        if until is not None and (at is None or at >= until):
            return None
        return mid

    def read_jsonl(self, run, started, state, reader, path):
        cfg = self.cfg
        window = self.window(state)
        # Pass 1: the ids this run takes (the lowest `cap` above the watermark).
        ids, highest, keys = [], None, set()
        message_keys = set()
        for session in self.jsonl_lines(path):
            keys.update(session)
            if self.excluded(session):
                continue
            for message in session.get("messages") or []:
                if not isinstance(message, dict):
                    continue
                message_keys.update(message)
                try:
                    mid = int(message.get("id"))
                except (TypeError, ValueError):
                    continue
                highest = mid if highest is None else max(highest, mid)
                if self.candidate(message, window) is not None:
                    ids.append(mid)
        cap = cfg["max_messages"]
        more = bool(cap) and len(ids) > cap
        taken = set(heapq.nsmallest(cap, ids) if cap else ids)
        yield self.header(
            run,
            started,
            reader,
            {
                "version": None,
                "session_columns": sorted(
                    k for k in keys if k not in ("messages", "timings")
                ),
                "message_columns": sorted(message_keys),
            },
        )
        # Pass 2: write the taken messages and the wanted sessions.
        emitted, touched = [], set()
        for session in self.jsonl_lines(path):
            if self.excluded(session):
                continue
            sid = str(session["id"])
            messages = [m for m in session.get("messages") or [] if isinstance(m, dict)]
            calls = {}
            for message in sorted(messages, key=lambda m: int(m.get("id") or 0)):
                mid = self.candidate(message, window)
                for call in call_ids(decode_json(message.get("tool_calls"))):
                    calls.setdefault(
                        call, call_target(int(message.get("id") or 0), message)
                    )
                if mid is None or mid not in taken:
                    continue
                message = dict(message, session_id=sid)
                record = message_record(run, message, cfg["max_text"])
                target = record["payload"].get("tool_call_id")
                if record["payload"].get("role") == "tool" and target:
                    found = calls.get(str(target))
                    record["reply_to"] = found if found and found["id"] < mid else None
                emitted.append(mid)
                touched.add(sid)
                self.report["messages"] += 1
                yield record
            if self.wanted_session(session, state, touched):
                self.report["sessions"] += 1
                yield session_record(
                    run,
                    session,
                    session.get("system_prompt"),
                    cfg["store_system_prompt"],
                    cfg["max_text"],
                )
        yield self.finish(run, state, started, emitted, more, highest)


# --- Command line ----------------------------------------------------------------


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="fetch.py",
        description="Export Hermes Agent sessions and messages as raw JSONL. "
        "Read-only: the live state.db is never written. No credentials are used.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export", help="fetch one run and write it as JSONL")
    export.add_argument("--out", required=True, help="output JSONL file, or '-' for stdout")
    export.add_argument(
        "--gzip",
        action="store_true",
        help="gzip-compress the output (also enabled automatically for .gz paths)",
    )
    export.add_argument(
        "--append",
        action="store_true",
        help="append the run to --out instead of replacing it",
    )
    export.add_argument("--state", help="JSON state file: the message watermarks")
    export.add_argument(
        "--after-id",
        type=int,
        help="seed message id watermark (after_id in sync, backfill_after in backfill)",
    )
    export.add_argument(
        "--since-ts",
        type=float,
        help="seed previous run epoch seconds (limits sync sessions to recently active)",
    )
    export.add_argument("--mode", choices=["sync", "backfill"], default="sync")
    export.add_argument(
        "--reader", choices=["auto", "cli", "file", "snapshot", "readonly"]
    )
    export.add_argument("--home", help="Hermes home directory (holds state.db)")
    export.add_argument("--db", help="state.db path")
    export.add_argument("--export-file", help="a `hermes sessions export` JSONL file")
    export.add_argument("--snapshot-dir", help="directory for the temporary snapshot")
    export.add_argument("--since", help="backfill lower bound: date or ISO time")
    export.add_argument("--until", help="backfill upper bound (exclusive)")
    export.add_argument(
        "--max-messages", type=int, help="messages per run (0 = no cap)"
    )
    export.add_argument(
        "--max-text", type=int, help="characters kept of any one string"
    )
    export.add_argument("--exclude-sources", help="comma-separated sources to skip")
    return parser.parse_args(argv)


def export_main(args, environ, stdout, stderr):
    values = env_values(environ)
    for key in (
        "reader",
        "home",
        "db",
        "export_file",
        "snapshot_dir",
        "since",
        "until",
        "max_messages",
        "max_text",
        "exclude_sources",
    ):
        value = getattr(args, key)
        if value is not None:
            values[key] = str(value)
    if args.home and not args.db:
        values["db"] = None
    try:
        cfg = config(values)
    except ValueError as error:
        print(f"configuration error: {error}", file=stderr)
        return 2
    state = {}
    if args.state and os.path.exists(args.state):
        with open(args.state, encoding="utf-8") as handle:
            state = json.load(handle)
    if args.after_id is not None:
        state["backfill_after" if args.mode == "backfill" else "after_id"] = (
            args.after_id
        )
    if args.since_ts is not None:
        state["since_ts"] = args.since_ts
    fetcher = Fetcher(cfg, mode=args.mode, log=lambda text: print(text, file=stderr))
    use_gzip = bool(args.gzip or args.out.endswith(".gz"))
    if args.out == "-":
        try:
            if use_gzip:
                buf = getattr(stdout, "buffer", stdout)
                with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6) as gz:
                    for item in fetcher.run(state):
                        line = (
                            json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n"
                        )
                        gz.write(line.encode("utf-8"))
                buf.flush()
            else:
                for item in fetcher.run(state):
                    stdout.write(
                        json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n"
                    )
                stdout.flush()
        except (FetchError, sqlite3.Error, OSError) as error:
            print(f"export failed: {error}", file=stderr)
            return 1
        if args.state:
            temporary = args.state + ".part"
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(state, handle, sort_keys=True, indent=1)
            os.replace(temporary, args.state)
        print(json.dumps(dict(fetcher.report, out="-"), sort_keys=True), file=stderr)
        return 0
    partial = args.out + ".part"
    status = 0
    try:
        if use_gzip:
            with gzip.open(partial, "wt", encoding="utf-8", newline="\n") as handle:
                for item in fetcher.run(state):
                    handle.write(
                        json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n"
                    )
        else:
            with open(partial, "w", encoding="utf-8", newline="\n") as handle:
                for item in fetcher.run(state):
                    handle.write(
                        json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n"
                    )
    except (FetchError, sqlite3.Error, OSError) as error:
        print(f"export failed: {error}", file=stderr)
        status = 1
    if status:
        try:
            os.remove(partial)
        except OSError:
            pass
        return status
    if args.append and os.path.exists(args.out):
        mode_r, mode_a = ("rb", "ab") if use_gzip else ("r", "a")
        kw = {} if use_gzip else {"encoding": "utf-8"}
        kw_a = {} if use_gzip else {"encoding": "utf-8", "newline": "\n"}
        with (
            open(partial, mode_r, **kw) as source,
            open(args.out, mode_a, **kw_a) as target,
        ):
            shutil.copyfileobj(source, target)
        os.remove(partial)
    else:
        os.replace(partial, args.out)
    if args.state:
        # Written only after the export file is complete on disk.
        temporary = args.state + ".part"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True, indent=1)
        os.replace(temporary, args.state)
    print(json.dumps(dict(fetcher.report, out=args.out), sort_keys=True), file=stdout)
    return 0


def main(argv=None, environ=None, stdout=None, stderr=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    environ = os.environ if environ is None else environ
    if args.command == "export":
        return export_main(args, environ, stdout or sys.stdout, stderr or sys.stderr)
    return 2  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
