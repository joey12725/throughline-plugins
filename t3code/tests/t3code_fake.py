"""Invented T3 homes: no real data anywhere."""

import json
import sqlite3

EMAIL_CANARY = "owner-canary@example.invalid"
ENV_ID = "env-0000-test"

SCHEMA = """
CREATE TABLE projection_projects (project_id TEXT PRIMARY KEY, title TEXT NOT NULL,
  workspace_root TEXT NOT NULL, scripts_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, deleted_at TEXT);
CREATE TABLE orchestration_v2_projection_threads (thread_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL, title TEXT NOT NULL, default_provider TEXT NOT NULL,
  runtime_mode TEXT NOT NULL DEFAULT 'full-access', interaction_mode TEXT NOT NULL DEFAULT 'default',
  active_provider_thread_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  archived_at TEXT, deleted_at TEXT, payload_json TEXT NOT NULL, provider_instance_id TEXT);
CREATE TABLE orchestration_v2_projection_runs (run_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL, provider TEXT NOT NULL, provider_thread_id TEXT, status TEXT NOT NULL,
  requested_at TEXT NOT NULL, completed_at TEXT, payload_json TEXT NOT NULL, provider_instance_id TEXT);
CREATE TABLE orchestration_v2_projection_messages (message_id TEXT PRIMARY KEY,
  thread_id TEXT NOT NULL, run_id TEXT, node_id TEXT, role TEXT NOT NULL, streaming INTEGER NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, payload_json TEXT NOT NULL, search_text TEXT);
"""


def thread_payload(tid, project, title, parent=None, root=None, model="claude-opus-5-5"):
    return json.dumps({
        "id": tid, "projectId": project, "title": title, "createdBy": "user",
        "creationSource": "web", "providerInstanceId": "claudeAgent",
        "modelSelection": {"instanceId": "claudeAgent", "model": model},
        "branch": "main", "worktreePath": None,
        "lineage": {"parentThreadId": parent, "relationshipToParent": "subagent" if parent else None,
                    "rootThreadId": root or tid},
        "forkedFrom": None, "settledAt": None, "pinnedAt": None,
    })


def message_row(mid, tid, role, text, at, created_by, source, run=None, streaming=0):
    payload = {"id": mid, "threadId": tid, "runId": run, "role": role, "text": text,
               "createdBy": created_by, "creationSource": source, "attachments": [],
               "createdAt": at, "updatedAt": at}
    return (mid, tid, run, None, role, streaming, at, at, json.dumps(payload), text)


class Home:
    def __init__(self, root):
        self.root = root
        (root / "userdata" / "logs" / "provider").mkdir(parents=True)
        (root / "caches").mkdir()
        (root / "userdata" / "environment-id").write_text(ENV_ID + "\n")
        self.db = sqlite3.connect(root / "userdata" / "statev2.sqlite")
        self.db.executescript(SCHEMA)
        self.db.execute(
            "INSERT INTO projection_projects VALUES (?,?,?,?,?,?,?)",
            ("p1", "Orchard", "/work/orchard", "[]", "2026-01-01T00:00:00Z", "2026-01-05T00:00:00Z", None),
        )
        for tid, title, parent in (("t1", "Plan the orchard", None), ("t1-sub", "Research pears", "t1"),
                                   ("t2", "Fix the fence", None)):
            self.db.execute(
                "INSERT INTO orchestration_v2_projection_threads (thread_id, project_id, title,"
                " default_provider, created_at, updated_at, payload_json) VALUES (?,?,?,?,?,?,?)",
                (tid, "p1", title, "claudeAgent", "2026-01-01T10:00:00Z", "2026-01-02T00:00:00Z",
                 thread_payload(tid, "p1", title, parent=parent, root="t1" if parent else tid)),
            )
        self.db.execute(
            "INSERT INTO orchestration_v2_projection_runs VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("r1", "t1", 1, "claudeAgent", None, "completed", "2026-01-01T10:00:00Z", None,
             json.dumps({"modelSelection": {"instanceId": "claudeAgent", "model": "claude-fable-5"}}),
             "claudeAgent"),
        )
        rows = [
            message_row("m1", "t1", "user", "How many pear trees fit?", "2026-01-01T10:00:00Z", "user", "web", "r1"),
            message_row("m2", "t1", "assistant", "About forty.", "2026-01-01T10:01:00Z", "agent", "provider", "r1"),
            message_row("m3", "t1-sub", "user", "Research pear spacing.", "2026-01-01T10:02:00Z", "agent", "provider"),
            message_row("m4", "t1-sub", "assistant", "Five metres apart.", "2026-01-01T10:03:00Z", "agent", "provider"),
            message_row("m5", "t2", "user", "The fence is down.", "2026-01-02T09:00:00Z", "user", "mobile"),
            message_row("m6", "t2", "assistant", "", "2026-01-02T09:01:00Z", "agent", "provider", streaming=1),
        ]
        self.db.executemany(
            "INSERT INTO orchestration_v2_projection_messages VALUES (?,?,?,?,?,?,?,?,?,?)", rows
        )
        self.db.commit()

    def add_message(self, *row):
        self.db.execute(
            "INSERT OR REPLACE INTO orchestration_v2_projection_messages VALUES (?,?,?,?,?,?,?,?,?,?)",
            message_row(*row),
        )
        self.db.commit()

    def cache(self, instance, *, label, windows, credits=None, checked="2026-01-03T00:00:00Z",
              email=EMAIL_CANARY, status="authenticated"):
        data = {
            "displayName": instance, "enabled": True, "status": "ready", "instanceId": instance,
            "auth": {"status": status, "email": email, "type": "sub", "label": label},
            "checkedAt": checked,
            "usageLimits": {"checkedAt": checked, "windows": windows},
        }
        if credits is not None:
            data["usageLimits"]["resetCredits"] = credits
        (self.root / "caches" / f"{instance}.json").write_text(json.dumps(data))

    def claude_event(self, at, utilization, resets_epoch, name="events.t1.log"):
        event = {
            "provider": "claudeAgent",
            "providerSessionId": "provider-session:provider-instance:claudeAgent:thread:t1",
            "event": {"payload": {"type": "rate_limit_event", "rate_limit_info": {
                "status": "allowed", "resetsAt": resets_epoch,
                "unifiedWindows": {"seven_day": {"utilization": utilization,
                                                 "resetsAt": resets_epoch, "status": "allowed"}}}}},
        }
        with open(self.root / "userdata" / "logs" / "provider" / name, "a") as out:
            out.write(f"[{at}] NATIVE: {json.dumps(event)}\n")


