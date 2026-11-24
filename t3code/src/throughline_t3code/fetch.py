"""Read T3 Code work and subscription usage, read-only, standard library only.

Copy this one file to any machine that runs T3 Code and run

    python fetch.py export --out FILE [--home LABEL=PATH ...] [--codex PATH ...]

It writes raw JSONL records (one header per run). Nothing here needs or reads a
credential: account e-mail addresses are reduced to a short sha256 key and
never written.

Sources, per T3 home (default ~/.t3):

- userdata/statev2.sqlite: projects, threads, user and assistant messages
  (opened read-only; the live server keeps writing it);
- userdata/environment-id: the environment (machine) id;
- caches/<provider instance>.json: subscription plan and the provider's
  current usage windows and reset credits (T3 refreshes these);
- userdata/logs/provider/*.log*: Claude `rate_limit_event` and Codex
  `account/rateLimits/updated` events, the usage history T3 saw.

Per Codex home (default ~/.codex): sessions/**/*.jsonl `rate_limits`
snapshots, the Codex usage history.
"""

import argparse
import glob
import hashlib
import json
import os
import socket
import sqlite3
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

FORMAT = 1
# Characters of message text kept; longer text is cut and flagged.
TEXT_LIMIT = 100_000
# Re-read this many seconds before the watermark: rows written in the same
# second as the last run, or finished streaming after it, are seen again
# (the core skips unchanged records).
OVERLAP_S = 3600
CACHE_SKIP = {"pi-status-probe"}

SETTINGS = [
    (
        "homes",
        "T3CODE_HOMES",
        "local=~/.t3",
        "T3 homes to read: LABEL=PATH, comma-separated; the first home that holds a "
        "message names its environment label",
    ),
    (
        "codex_homes",
        "T3CODE_CODEX_HOMES",
        "~/.codex",
        "Codex homes whose sessions/ hold rate-limit history (comma-separated, "
        "'none' to skip)",
    ),
    ("since", "T3CODE_SINCE", None, "Lower bound for messages: date or ISO time"),
    ("usage", "T3CODE_USAGE", "true", "Read subscription usage (caches and logs)"),
    ("work", "T3CODE_WORK", "true", "Read projects, threads and messages"),
]


class FetchError(RuntimeError):
    pass


def truthy(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def split_list(value):
    return [p.strip() for p in str(value or "").split(",") if p.strip()]


def parse_moment(value):
    """Aware UTC datetime from ISO text, epoch seconds or epoch milliseconds."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = value / 1000 if value > 10**11 else value
        return datetime.fromtimestamp(seconds, UTC)
    text = str(value).strip()
    if text.replace(".", "", 1).isdigit():
        return parse_moment(float(text))
    if len(text) == 10:
        text += "T00:00:00"
    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def iso(value):
    moment = parse_moment(value)
    return None if moment is None else moment.isoformat().replace("+00:00", "Z")


def parse_homes(value):
    homes = []
    for item in split_list(value):
        label, sep, path = item.partition("=")
        if not sep:
            label, path = Path(item).expanduser().parent.name or "local", item
        label = label.strip()
        if not label or not path.strip():
            raise FetchError(f"bad T3CODE_HOMES entry {item!r}: use LABEL=PATH")
        homes.append((label, Path(path.strip()).expanduser()))
    if not homes:
        raise FetchError("T3CODE_HOMES names no home")
    if len({label for label, _ in homes}) != len(homes):
        raise FetchError("T3CODE_HOMES labels must be unique")
    return homes


def config(values):
    cfg = {key: default for key, _, default, _ in SETTINGS}
    cfg.update({k: v for k, v in values.items() if v is not None})
    cfg["homes"] = parse_homes(cfg["homes"])
    codex = split_list(cfg["codex_homes"])
    cfg["codex_homes"] = [] if codex == ["none"] else [Path(p).expanduser() for p in codex]
    cfg["since"] = iso(cfg["since"]) if cfg.get("since") else None
    cfg["usage"] = truthy(cfg["usage"])
    cfg["work"] = truthy(cfg["work"])
    return cfg


def account_key(provider, email):
    """Stable, non-reversible account key: never the address itself."""
    basis = f"{provider}:{(email or '').strip().lower()}"
    return hashlib.sha256(basis.encode()).hexdigest()[:12]


def loads(value, default=None):
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def connect(path):
    """Read-only connection; works while the T3 server writes the database."""
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=30)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
    except sqlite3.OperationalError:
        # A copy on a read-only mount cannot take the WAL lock: read it as
        # an immutable file instead.
        conn = sqlite3.connect(uri + "&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn, name):
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def model_of(selection):
    selection = selection if isinstance(selection, dict) else loads(selection, {})
    if not isinstance(selection, dict):
        return None, None
    return selection.get("instanceId"), selection.get("model")


class Fetcher:
    def __init__(self, cfg, *, now=None, log=None):
        self.cfg = cfg
        self.now = parse_moment(now) if now else datetime.now(UTC)
        self.log = log or (lambda text: None)
        self.run = uuid.uuid4().hex
        self.report = {"homes": {}, "usage_sources": {}}

    def record(self, kind, **data):
        return dict(kind=kind, run=self.run, **data)

    def run_records(self, state):
        """Raw records of one run; updates `state` (watermarks) in place."""
        yield self.record(
            "run",
            format=FORMAT,
            fetched_at=iso(self.now),
            host=socket.gethostname(),
        )
        homes_state = state.setdefault("homes", {})
        for label, home in self.cfg["homes"]:
            home_state = homes_state.setdefault(label, {})
            if self.cfg["work"]:
                try:
                    yield from self.work(label, home, home_state)
                except (sqlite3.Error, OSError) as error:
                    # One unreadable home must not hide the others: the
                    # plugin reports it (and fails the run if no home read).
                    self.report["homes"].setdefault(label, {})["work"] = "unreadable"
                    self.log(f"home {label}: statev2.sqlite unreadable: {error}")
                    yield self.record(
                        "home_error", home=label, error=f"statev2.sqlite unreadable: {error}"
                    )
            if self.cfg["usage"]:
                yield from self.caches(label, home)
                yield from self.provider_logs(label, home, home_state)
        if self.cfg["usage"]:
            codex_state = state.setdefault("codex", {})
            for codex in self.cfg["codex_homes"]:
                yield from self.codex_sessions(codex, codex_state)
        yield self.record("run_end", complete=True)

    # -- work -------------------------------------------------------------

    def work(self, label, home, home_state):
        db = home / "userdata" / "statev2.sqlite"
        report = self.report["homes"].setdefault(label, {})
        if not db.is_file():
            report["work"] = "no statev2.sqlite"
            self.log(f"home {label}: no statev2.sqlite at {db}")
            yield self.record("home_error", home=label, error="no statev2.sqlite")
            return
        env_file = home / "userdata" / "environment-id"
        environment_id = (
            env_file.read_text().strip() if env_file.is_file() else f"unknown:{label}"
        )
        yield self.record(
            "environment", home=label, environment_id=environment_id, path=str(home)
        )
        conn = connect(db)
        try:
            since = home_state.get("messages_since") or self.cfg["since"]
            if since:
                since = iso(parse_moment(since).timestamp() - OVERLAP_S)
            projects = 0
            for row in conn.execute("SELECT * FROM projection_projects"):
                projects += 1
                yield self.record(
                    "project",
                    home=label,
                    environment_id=environment_id,
                    payload={
                        "id": row["project_id"],
                        "title": row["title"],
                        "workspace_root": row["workspace_root"],
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                        "deleted_at": row["deleted_at"],
                    },
                )
            runs = {}
            if table_exists(conn, "orchestration_v2_projection_runs"):
                for row in conn.execute(
                    "SELECT run_id, provider, provider_instance_id, requested_at,"
                    " completed_at, status, payload_json"
                    " FROM orchestration_v2_projection_runs"
                ):
                    payload = loads(row["payload_json"], {})
                    instance, model = model_of(payload.get("modelSelection"))
                    runs[row["run_id"]] = {
                        "provider": row["provider"],
                        "instance": row["provider_instance_id"] or instance,
                        "model": model,
                    }
            threads = 0
            for row in conn.execute(
                "SELECT thread_id, project_id, title, default_provider, created_at,"
                " updated_at, archived_at, deleted_at, payload_json"
                " FROM orchestration_v2_projection_threads"
            ):
                payload = loads(row["payload_json"], {})
                lineage = payload.get("lineage") or {}
                instance, model = model_of(payload.get("modelSelection"))
                threads += 1
                yield self.record(
                    "thread",
                    home=label,
                    environment_id=environment_id,
                    payload={
                        "id": row["thread_id"],
                        "project_id": row["project_id"],
                        "title": row["title"],
                        "provider": row["default_provider"],
                        "provider_instance": payload.get("providerInstanceId")
                        or instance,
                        "model": model,
                        "created_by": payload.get("createdBy"),
                        "creation_source": payload.get("creationSource"),
                        "branch": payload.get("branch"),
                        "worktree_path": payload.get("worktreePath"),
                        "parent_thread_id": lineage.get("parentThreadId"),
                        "relationship_to_parent": lineage.get("relationshipToParent"),
                        "root_thread_id": lineage.get("rootThreadId")
                        or row["thread_id"],
                        "forked_from": (payload.get("forkedFrom") or {}).get("threadId")
                        if isinstance(payload.get("forkedFrom"), dict)
                        else payload.get("forkedFrom"),
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                        "archived_at": row["archived_at"],
                        "deleted_at": row["deleted_at"],
                        "settled_at": payload.get("settledAt"),
                        "pinned": bool(payload.get("pinnedAt")),
                    },
                )
            query = (
                "SELECT message_id, thread_id, run_id, role, streaming, created_at,"
                " updated_at, payload_json FROM orchestration_v2_projection_messages"
            )
            params = ()
            if since:
                query += " WHERE updated_at >= ?"
                params = (since,)
            query += " ORDER BY updated_at, message_id"
            messages = streaming = 0
            newest = home_state.get("messages_since")
            for row in conn.execute(query, params):
                if row["streaming"]:
                    streaming += 1
                    continue  # Read once it is final.
                payload = loads(row["payload_json"], {})
                text = payload.get("text") or ""
                cut = len(text) > TEXT_LIMIT
                messages += 1
                newest = max(newest or row["updated_at"], row["updated_at"])
                yield self.record(
                    "message",
                    home=label,
                    environment_id=environment_id,
                    payload={
                        "id": row["message_id"],
                        "thread_id": row["thread_id"],
                        "run_id": row["run_id"],
                        "role": row["role"],
                        "created_by": payload.get("createdBy"),
                        "creation_source": payload.get("creationSource"),
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                        "text": text[:TEXT_LIMIT],
                        "text_length": len(text),
                        "text_truncated": cut,
                        "attachment_count": len(payload.get("attachments") or []),
                    },
                    run_info=runs.get(row["run_id"]),
                )
            if newest:
                home_state["messages_since"] = newest
            report.update(
                environment_id=environment_id,
                projects=projects,
                threads=threads,
                messages=messages,
                streaming_skipped=streaming,
            )
        finally:
            conn.close()

    # -- usage ------------------------------------------------------------

    def caches(self, label, home):
        """Current plan, usage windows and reset credits of every provider instance."""
        count = 0
        for path in sorted((home / "caches").glob("*.json")):
            if path.stem in CACHE_SKIP:
                continue
            data = loads(path.read_text(encoding="utf-8", errors="replace"), None)
            if not isinstance(data, dict) or "auth" not in data:
                continue
            auth = data.get("auth") or {}
            instance = data.get("instanceId") or path.stem
            provider = provider_of(instance)
            if auth.get("status") != "authenticated":
                continue  # Signed out: its account is unknown here.
            usage = data.get("usageLimits") or {}
            count += 1
            yield self.record(
                "subscription",
                home=label,
                instance=instance,
                provider=provider,
                account=account_key(provider, auth.get("email")),
                payload={
                    "display_name": data.get("displayName"),
                    "plan": auth.get("label"),
                    "auth_type": auth.get("type"),
                    "auth_status": auth.get("status"),
                    "enabled": data.get("enabled"),
                    "status": data.get("status"),
                    "version": data.get("version"),
                    "checked_at": data.get("checkedAt"),
                    "usage_checked_at": usage.get("checkedAt"),
                    "usage_unavailable": (usage.get("unavailable") or {}).get("reason"),
                },
            )
            observed = usage.get("checkedAt") or data.get("checkedAt")
            for window in usage.get("windows") or []:
                if window.get("usedPercent") is None:
                    continue
                yield self.record(
                    "usage",
                    home=label,
                    instance=instance,
                    provider=provider,
                    account=account_key(provider, auth.get("email")),
                    source="t3_cache",
                    observed_at=iso(observed),
                    window_id=window.get("id"),
                    window_label=window.get("label"),
                    window_kind=window.get("kind"),
                    window_minutes=window.get("windowDurationMins"),
                    used_percent=float(window["usedPercent"]),
                    resets_at=iso(window.get("resetsAt")),
                )
            credits = usage.get("resetCredits")
            if isinstance(credits, dict):
                yield self.record(
                    "credits",
                    home=label,
                    instance=instance,
                    provider=provider,
                    account=account_key(provider, auth.get("email")),
                    source="t3_cache",
                    observed_at=iso(observed),
                    available=credits.get("availableCount"),
                    next_expires_at=iso(credits.get("nextExpiresAt")),
                    options=[
                        {
                            "credit_id": o.get("creditId"),
                            "label": o.get("label"),
                            "description": o.get("description"),
                            "available": o.get("availableCount"),
                            "expires_at": iso(o.get("expiresAt")),
                        }
                        for o in credits.get("options") or []
                        if isinstance(o, dict)
                    ],
                )
        self.report["usage_sources"][f"{label}:caches"] = count

    def accounts(self, home):
        """{instance: (provider, account key)} from the caches, for log events."""
        found = {}
        for path in (home / "caches").glob("*.json"):
            data = loads(path.read_text(encoding="utf-8", errors="replace"), None)
            if (
                isinstance(data, dict)
                and isinstance(data.get("auth"), dict)
                and data["auth"].get("email")
            ):
                instance = data.get("instanceId") or path.stem
                provider = provider_of(instance)
                found[instance] = (
                    provider,
                    account_key(provider, data["auth"].get("email")),
                )
        return found

    def provider_logs(self, label, home, home_state):
        """Usage events T3 logged while talking to Claude and Codex."""
        accounts = self.accounts(home)
        seen = home_state.setdefault("log_offsets", {})
        count = 0
        files = sorted(
            glob.glob(str(home / "userdata" / "logs" / "provider" / "*.log*"))
        )
        for name in files:
            path = Path(name)
            try:
                stat = path.stat()
            except OSError:
                continue
            key = path.name
            mark = seen.get(key)
            offset = 0
            if mark and mark.get("size", 0) <= stat.st_size and mark.get(
                "inode"
            ) == getattr(stat, "st_ino", None):
                offset = mark.get("size", 0)
            with open(path, "rb") as handle:
                handle.seek(offset)
                for raw in handle:
                    if b"rate_limit_event" not in raw and b"rateLimits/updated" not in raw:
                        continue
                    for item in log_usage(raw.decode("utf-8", "replace"), accounts):
                        count += 1
                        yield self.record("usage", home=label, **item)
            seen[key] = {"size": stat.st_size, "inode": getattr(stat, "st_ino", None)}
        live = {Path(n).name for n in files}
        for key in list(seen):
            if key not in live:
                del seen[key]
        self.report["usage_sources"][f"{label}:provider_logs"] = count

    def codex_sessions(self, codex, codex_state):
        count = 0
        offsets = codex_state.setdefault(str(codex), {})
        for name in sorted(glob.glob(str(codex / "sessions" / "**" / "*.jsonl"), recursive=True)):
            path = Path(name)
            size = path.stat().st_size
            start = offsets.get(path.name, 0)
            if start > size:
                start = 0
            if start == size:
                continue
            plan = None
            with open(path, "rb") as handle:
                handle.seek(start)
                for raw in handle:
                    if b'"rate_limits"' not in raw:
                        continue
                    line = loads(raw.decode("utf-8", "replace"), None)
                    if not isinstance(line, dict):
                        continue
                    limits = (line.get("payload") or {}).get("rate_limits")
                    if not isinstance(limits, dict):
                        continue
                    plan = limits.get("plan_type") or plan
                    for window_id in ("primary", "secondary"):
                        window = limits.get(window_id)
                        if not isinstance(window, dict) or window.get("used_percent") is None:
                            continue
                        count += 1
                        yield self.record(
                            "usage",
                            source="codex_session",
                            instance="codex",
                            provider="codex",
                            account=None,  # The session file names no account.
                            observed_at=iso(line.get("timestamp")),
                            window_id=window_id,
                            window_label=None,
                            window_kind=None,
                            window_minutes=window.get("window_minutes"),
                            used_percent=float(window["used_percent"]),
                            resets_at=iso(window.get("resets_at")),
                            plan=plan,
                        )
            offsets[path.name] = size
        self.report["usage_sources"][f"codex:{codex}"] = count


def provider_of(instance):
    instance = str(instance or "")
    for name in ("claudeAgent", "codex", "cursor", "opencode", "grok", "antigravity", "kiro", "pi"):
        if instance == name or instance.startswith(name + "_") or instance.startswith(name):
            return name
    return instance or "unknown"


WINDOW_LABELS = {
    "seven_day": "Weekly",
    "five_hour": "5-hour",
    "seven_day_opus": "Weekly · Opus",
    "seven_day_sonnet": "Weekly · Sonnet",
    "seven_day_fable": "Weekly · Fable",
    "seven_day_overage_included": "Weekly · overage included",
}


def log_usage(line, accounts):
    """Usage observations in one provider log line (Claude or Codex events)."""
    start = line.find("{")
    if start < 0:
        return []
    event = loads(line[start:], None)
    if not isinstance(event, dict):
        return []
    session = str(event.get("providerSessionId") or "")
    instance = None
    marker = "provider-instance:"
    if marker in session:
        instance = session.split(marker, 1)[1].split(":", 1)[0]
    instance = instance or event.get("provider") or "unknown"
    provider, account = accounts.get(instance, (provider_of(instance), None))
    payload = ((event.get("event") or {}).get("payload")) or {}
    stamp = line[1:25] if line.startswith("[") else None
    observed = iso(stamp) if stamp else None
    out = []
    if payload.get("type") == "rate_limit_event":
        info = payload.get("rate_limit_info") or {}
        windows = info.get("unifiedWindows") or {}
        if not windows and info.get("rateLimitType") and info.get("utilization") is not None:
            windows = {info["rateLimitType"]: info}
        for window_id, window in windows.items():
            if not isinstance(window, dict) or window.get("utilization") is None:
                continue
            out.append(
                dict(
                    source="t3_provider_log",
                    instance=instance,
                    provider=provider,
                    account=account,
                    observed_at=observed,
                    window_id=window_id,
                    window_label=WINDOW_LABELS.get(window_id),
                    window_kind="weekly" if window_id.startswith("seven_day") else None,
                    window_minutes=10080 if window_id.startswith("seven_day") else (
                        300 if window_id == "five_hour" else None
                    ),
                    used_percent=round(float(window["utilization"]) * 100, 2),
                    resets_at=iso(window.get("resetsAt") or info.get("resetsAt")),
                    status=window.get("status") or info.get("status"),
                )
            )
    elif payload.get("method") == "account/rateLimits/updated":
        limits = ((payload.get("params") or {}).get("rateLimits")) or {}
        for window_id in ("primary", "secondary"):
            window = limits.get(window_id)
            if not isinstance(window, dict) or window.get("usedPercent") is None:
                continue
            out.append(
                dict(
                    source="t3_provider_log",
                    instance=instance,
                    provider=provider,
                    account=account,
                    observed_at=observed,
                    window_id=window_id,
                    window_label=None,
                    window_kind=None,
                    window_minutes=window.get("windowDurationMins"),
                    used_percent=float(window["usedPercent"]),
                    resets_at=iso(window.get("resetsAt")),
                    plan=limits.get("planType"),
                )
            )
    return out


def env_values(environ):
    return {key: environ.get(env) for key, env, _, _ in SETTINGS if environ.get(env)}


def parse_args(argv):
    parser = argparse.ArgumentParser(prog="fetch.py", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export", help="write raw JSONL records")
    export.add_argument("--out", required=True, help="output file ('-' for stdout)")
    export.add_argument("--home", action="append", help="LABEL=PATH of a T3 home")
    export.add_argument("--codex", action="append", help="a Codex home (or 'none')")
    export.add_argument("--since", help="messages updated on or after this time")
    export.add_argument("--state", help="JSON file with watermarks, read and updated")
    return parser.parse_args(argv)


def main(argv=None, environ=None, stdout=None, stderr=None):
    environ = os.environ if environ is None else environ
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    args = parse_args(argv)
    values = env_values(environ)
    if args.home:
        values["homes"] = ",".join(args.home)
    if args.codex:
        values["codex_homes"] = ",".join(args.codex)
    if args.since:
        values["since"] = args.since
    try:
        cfg = config(values)
    except (FetchError, ValueError) as error:
        print(f"error: {error}", file=stderr)
        return 2
    state = {}
    if args.state and os.path.exists(args.state):
        state = json.loads(Path(args.state).read_text())
    fetcher = Fetcher(cfg, log=lambda text: print(text, file=stderr))
    out = stdout if args.out == "-" else open(args.out + ".part", "w", encoding="utf-8")
    try:
        for record in fetcher.run_records(state):
            out.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    finally:
        if out is not stdout:
            out.close()
    if out is not stdout:
        os.replace(args.out + ".part", args.out)
    if args.state:
        Path(args.state).write_text(json.dumps(state, sort_keys=True))
    print(json.dumps(fetcher.report, sort_keys=True), file=stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
