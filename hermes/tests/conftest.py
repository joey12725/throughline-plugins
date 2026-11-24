"""Invented Hermes session stores and exports. No real data anywhere."""

import json
import sqlite3
import sys
from datetime import UTC, datetime

import pytest

from throughline_hermes import fetch

# Canaries: they must never leave the fetcher.
PROMPT_CANARY = "SYSPROMPT-CANARY-7f3a9e"
REASONING_CANARY = "REASONING-CANARY-91b2c4"
OWNER_DISCORD = "100000000000000001"
FRIEND_TELEGRAM = "200000002"
SESSION_A = "20240301_100000_aaaa0001"  # discord, the owner
SESSION_B = "20240302_090000_bbbb0002"  # telegram, a friend
SESSION_C = "cron_inventedjob_20240303"  # cron
SESSION_D = "20240304_200000_dddd0004"  # cli, the owner
SESSION_E = "20240310_080000_eeee0005"  # desktop, appears in run 2
LONG_TOOL_OUTPUT = "Invented search result line. " * 120  # 3480 characters

SCHEMA = """
CREATE TABLE schema_version (version INTEGER NOT NULL);
CREATE TABLE system_prompts (hash TEXT PRIMARY KEY, prompt TEXT NOT NULL);
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, runtime_revision INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL, created_source TEXT, user_id TEXT, session_key TEXT,
    chat_id TEXT, chat_type TEXT, thread_id TEXT, display_name TEXT,
    origin_json TEXT, model TEXT, model_config TEXT, system_prompt TEXT,
    system_prompt_hash TEXT, parent_session_id TEXT, started_at REAL NOT NULL,
    ended_at REAL, end_reason TEXT, message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0, input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0, cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0, reasoning_tokens INTEGER DEFAULT 0,
    billing_provider TEXT, estimated_cost_usd REAL, actual_cost_usd REAL,
    title TEXT, last_activity_at REAL, api_call_count INTEGER DEFAULT 0,
    profile_name TEXT, last_read_at REAL, future_session_column TEXT
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL, content TEXT, tool_call_id TEXT, tool_calls TEXT,
    tool_name TEXT, effect_disposition TEXT, timestamp REAL NOT NULL,
    token_count INTEGER, finish_reason TEXT, reasoning TEXT,
    reasoning_content TEXT, reasoning_details TEXT, codex_reasoning_items TEXT,
    codex_message_items TEXT, platform_message_id TEXT, observed INTEGER DEFAULT 0,
    _compressed_summary INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
    compacted INTEGER NOT NULL DEFAULT 0, api_content TEXT, display_kind TEXT,
    display_metadata TEXT, display_identity BLOB, display_order INTEGER,
    message_uid TEXT, future_message_column TEXT
);
CREATE VIRTUAL TABLE messages_fts USING fts5(content);
"""


def ts(text):
    return datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp()


def tool_calls(*calls):
    return json.dumps(
        [
            {
                "id": cid,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
            for cid, name, args in calls
        ]
    )


def session_row(sid, source, started, **extra):
    row = dict(id=sid, source=source, started_at=ts(started), model="invented/model-1")
    row.update(extra)
    return row


def message_row(sid, role, at, content=None, **extra):
    row = dict(session_id=sid, role=role, timestamp=ts(at), content=content)
    row.update(extra)
    return row


RUN1_SESSIONS = [
    session_row(
        SESSION_A,
        "discord",
        "2024-03-01T10:00:00",
        user_id=OWNER_DISCORD,
        chat_id="300000000000000003",
        chat_type="dm",
        origin_json=json.dumps({"platform": "discord", "user_name": "invented_owner"}),
        system_prompt_hash="invented-hash-a",
        title="Invented garden plan",
        message_count=6,
        tool_call_count=1,
        input_tokens=1200,
        output_tokens=300,
        billing_provider="invented-provider",
        estimated_cost_usd=0.0123,
        last_activity_at=ts("2024-03-01T10:05:00"),
        last_read_at=ts("2024-03-01T10:06:00"),
        future_session_column="kept as is",
    ),
    session_row(
        SESSION_B,
        "telegram",
        "2024-03-02T09:00:00",
        user_id=FRIEND_TELEGRAM,
        chat_type="dm",
        origin_json=json.dumps(
            {"platform": "telegram", "user_name": "Invented Friend"}
        ),
        system_prompt="Short invented prompt for B.",
        ended_at=ts("2024-03-02T09:10:00"),
        end_reason="idle",
        message_count=2,
    ),
    session_row(
        SESSION_C,
        "cron",
        "2024-03-03T06:00:00",
        ended_at=ts("2024-03-03T06:01:00"),
        end_reason="completed",
        message_count=3,
        tool_call_count=1,
    ),
    session_row(
        SESSION_D,
        "cli",
        "2024-03-04T20:00:00",
        title="Invented shell session",
        message_count=3,
    ),
]

RUN1_MESSAGES = [
    message_row(
        SESSION_A,
        "user",
        "2024-03-01T10:00:05",
        "When should I plant invented beans?",
        platform_message_id="400000000000000004",
    ),
    message_row(
        SESSION_A,
        "assistant",
        "2024-03-01T10:00:09",
        "Let me look that up.",
        tool_calls=tool_calls(
            ("call_a1", "web_search", {"query": "invented bean planting"})
        ),
        reasoning=REASONING_CANARY + " hidden thoughts",
        finish_reason="tool_calls",
        api_content="api copy of the turn",
    ),
    message_row(
        SESSION_A,
        "tool",
        "2024-03-01T10:00:09",  # Same second as the call: the id breaks the tie.
        LONG_TOOL_OUTPUT,
        tool_call_id="call_a1",
        tool_name="web_search",
    ),
    message_row(
        SESSION_A,
        "assistant",
        "2024-03-01T10:00:15",
        "Plant invented beans after the last frost.",
        reasoning_content=REASONING_CANARY,
        token_count=42,
        finish_reason="stop",
        display_identity=b"\x00\x01binary",
    ),
    message_row(
        SESSION_A,
        "user",
        "2024-03-01T10:04:00",
        fetch.CONTENT_JSON_PREFIX
        + json.dumps(
            [
                {"type": "text", "text": "Here is my invented garden"},
                {"type": "image_url", "image_url": {"url": "file:///invented.png"}},
            ]
        ),
    ),
    message_row(
        SESSION_A,
        "assistant",
        "2024-03-01T10:05:00",
        "Nice invented garden!",
        future_message_column="new upstream column",
    ),
    message_row(
        SESSION_B, "user", "2024-03-02T09:00:10", "Hi Wren, invented question here"
    ),
    message_row(
        SESSION_B, "assistant", "2024-03-02T09:00:20", "Hello, invented answer."
    ),
    message_row(
        SESSION_C, "user", "2024-03-03T06:00:00", "Run the invented daily digest job"
    ),
    message_row(
        SESSION_C,
        "assistant",
        "2024-03-03T06:00:05",
        None,
        tool_calls=tool_calls(("call_c1", "terminal", {"command": "echo invented"})),
    ),
    message_row(
        SESSION_C,
        "tool",
        "2024-03-03T06:00:06",
        "invented",
        tool_call_id="call_c1",
        tool_name="terminal",
    ),
    message_row(SESSION_D, "user", "2024-03-04T20:00:05", "List my invented files"),
    message_row(
        SESSION_D,
        "assistant",
        "2024-03-04T20:00:08",
        "An answer that was undone.",
        active=0,
        compacted=0,
    ),
    message_row(
        SESSION_D, "assistant", "2024-03-04T20:00:20", "Here are your invented files."
    ),
]

RUN2_SESSION_UPDATES = {
    SESSION_A: dict(
        ended_at=ts("2024-03-05T12:00:00"),
        end_reason="compression",
        message_count=8,
        last_activity_at=ts("2024-03-05T12:00:00"),
    ),
}
RUN2_SESSIONS = [
    session_row(
        SESSION_E,
        "desktop",
        "2024-03-10T08:00:00",
        title="Invented desktop chat",
        message_count=4,
    ),
]
RUN2_MESSAGES = [
    message_row(SESSION_A, "user", "2024-03-05T11:59:00", "One more invented question"),
    message_row(
        SESSION_A, "assistant", "2024-03-05T11:59:30", "One more invented answer."
    ),
    message_row(SESSION_E, "user", "2024-03-10T08:00:05", "Invented desktop hello"),
    message_row(
        SESSION_E,
        "assistant",
        "2024-03-10T08:00:07",
        "",
        tool_calls=tool_calls(
            ("call_e1", "read_file", {"path": "/invented/notes.md"}),
            ("call_e2", "memory", {"action": "read"}),
        ),
    ),
    message_row(
        SESSION_E,
        "tool",
        "2024-03-10T08:00:08",
        "invented notes",
        tool_call_id="call_e1",
        tool_name="read_file",
    ),
    message_row(
        SESSION_E,
        "tool",
        "2024-03-10T08:00:08",
        "",
        tool_call_id="call_e2",
        tool_name="memory",
    ),
    message_row(
        SESSION_E, "assistant", "2024-03-10T08:00:12", "Your invented notes say hi."
    ),
]
# Newer Hermes rows carry a message_uid that survives transcript rewrites.
for number, row in enumerate(RUN2_MESSAGES):
    if row["session_id"] == SESSION_E:
        row["message_uid"] = f"invented-uid-e{number}"


def external(session, number):
    """The external id of message row `number` (1-based, in insertion order)."""
    from throughline_hermes.mapping import message_external_id

    row = (RUN1_MESSAGES + RUN2_MESSAGES)[number - 1]
    assert row["session_id"] == session
    return message_external_id(session, number, row.get("message_uid"))


def insert(conn, table, row):
    keys = list(row)
    conn.execute(
        f"INSERT INTO {table} ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})",
        [row[k] for k in keys],
    )


def create_store(path):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO schema_version VALUES (33)")
    conn.execute(
        "INSERT INTO system_prompts VALUES (?, ?)",
        ("invented-hash-a", "You are Wren. " + PROMPT_CANARY + " persona and memory."),
    )
    for row in RUN1_SESSIONS:
        insert(conn, "sessions", row)
    for row in RUN1_MESSAGES:
        insert(conn, "messages", row)
    conn.commit()
    return conn


def advance_store(conn):
    for sid, values in RUN2_SESSION_UPDATES.items():
        sets = ", ".join(f"{k} = ?" for k in values)
        conn.execute(
            f"UPDATE sessions SET {sets} WHERE id = ?", [*values.values(), sid]
        )
    for row in RUN2_SESSIONS:
        insert(conn, "sessions", row)
    for row in RUN2_MESSAGES:
        insert(conn, "messages", row)
    conn.commit()


def clock(text):
    moment = datetime.fromisoformat(text).replace(tzinfo=UTC)
    return lambda: moment


def fetch_run(db, state, *, mode="sync", at="2024-03-06T00:00:00", **settings):
    cfg = fetch.config(dict(reader="snapshot", db=str(db), **settings))
    fetcher = fetch.Fetcher(cfg, mode=mode, now=clock(at))
    records = list(fetcher.run(state))
    return records, fetcher.report


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    return path


def cli_export_lines(conn):
    """The store as `hermes sessions export -` writes it (session rows with messages)."""
    conn.row_factory = sqlite3.Row
    prompts = dict(conn.execute("SELECT hash, prompt FROM system_prompts").fetchall())
    lines = []
    for session in conn.execute("SELECT * FROM sessions ORDER BY started_at"):
        row = dict(session)
        if row.get("system_prompt_hash") and not row.get("system_prompt"):
            row["system_prompt"] = prompts.get(row["system_prompt_hash"])
        messages = []
        for message in conn.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY id", (row["id"],)
        ):
            m = dict(message)
            m.pop("display_identity", None)
            content = m.get("content")
            if isinstance(content, str) and content.startswith(
                fetch.CONTENT_JSON_PREFIX
            ):
                m["content"] = json.loads(content[len(fetch.CONTENT_JSON_PREFIX) :])
            if m.get("tool_calls"):
                m["tool_calls"] = json.loads(m["tool_calls"])
            if m.pop("_compressed_summary"):
                m["_compressed_summary"] = True
            messages.append(m)
        row["messages"] = messages
        row["timings"] = {"available": True, "complete": False}
        lines.append(json.dumps(row, ensure_ascii=False))
    conn.row_factory = None
    return lines


@pytest.fixture
def data():
    return sys.modules[__name__]


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "home" / "state.db"
    db.parent.mkdir()
    conn = create_store(db)
    yield db, conn
    conn.close()


@pytest.fixture
def export_dir(tmp_path, store):
    """Two runs fetched from the invented store, as a remote host would ship them."""
    db, conn = store
    folder = tmp_path / "export"
    folder.mkdir()
    state = {}
    first, _ = fetch_run(db, state, mode="backfill", at="2024-03-06T00:00:00")
    write_jsonl(folder / "hermes-1.jsonl", first)
    advance_store(conn)
    second, _ = fetch_run(db, state, at="2024-03-11T00:00:00")
    write_jsonl(folder / "hermes-2.jsonl", second)
    return folder


@pytest.fixture
def hermes_env(monkeypatch):
    """Clear every Hermes variable so the developer's environment is ignored."""
    from throughline_hermes import Hermes

    for declaration in Hermes.manifest.settings.values():
        monkeypatch.delenv(declaration.env, raising=False)
    monkeypatch.setenv("HERMES_OWNER_USER_IDS", "discord:" + OWNER_DISCORD)
    return monkeypatch
