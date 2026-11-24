import hashlib
import io
import json
import sqlite3
import subprocess

import pytest

from throughline_hermes import fetch


def by_kind(records, kind):
    return [r for r in records if r["kind"] == kind]


def messages_by_id(records):
    return {r["id"]: r for r in by_kind(records, "message")}


def sessions_by_id(records):
    return {r["id"]: r for r in by_kind(records, "session")}


def file_digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_hermes_snapshot_reads_store_read_only(store, data):
    db, conn = store
    wal = db.with_name(db.name + "-wal")
    before, wal_before = file_digest(db), file_digest(wal)
    mtime = db.stat().st_mtime_ns
    # Hermes keeps a writer connection open: the snapshot must not need it closed.
    conn.execute("BEGIN")
    conn.execute("UPDATE sessions SET title = title WHERE id = ?", (data.SESSION_B,))
    records, report = data.fetch_run(db, {}, mode="backfill")
    conn.rollback()
    assert file_digest(db) == before and db.stat().st_mtime_ns == mtime
    assert file_digest(wal) == wal_before
    assert report["complete"] and report["messages"] == len(data.RUN1_MESSAGES)
    header = records[0]
    assert header["kind"] == "run" and header["reader"] == "snapshot"
    assert header["schema"]["version"] == 33
    assert "future_message_column" in header["schema"]["message_columns"]
    assert records[-1]["kind"] == "run_end" and records[-1]["complete"] is True
    # The private snapshot directory is gone.
    assert not list(db.parent.glob("hermes-snapshot-*"))


def test_hermes_snapshot_never_exports_reasoning_or_prompt_text(store, data):
    db, _ = store
    records, _ = data.fetch_run(db, {}, mode="backfill")
    text = json.dumps(records)
    assert data.PROMPT_CANARY not in text
    assert data.REASONING_CANARY not in text
    assert "api copy of the turn" not in text
    rows = messages_by_id(records)
    assert rows[2]["reasoning_present"] is True and rows[4]["reasoning_present"] is True
    assert rows[1]["reasoning_present"] is False
    for key in fetch.DROPPED_MESSAGE_KEYS:
        assert key not in rows[2]["payload"]
    prompt = sessions_by_id(records)[data.SESSION_A]["system_prompt"]
    expected = "You are Wren. " + data.PROMPT_CANARY + " persona and memory."
    assert prompt == {
        "hermes_hash": "invented-hash-a",
        "sha256": hashlib.sha256(expected.encode()).hexdigest(),
        "length": len(expected),
    }
    inline = sessions_by_id(records)[data.SESSION_B]["system_prompt"]
    assert (
        inline["length"] == len("Short invented prompt for B.") and "text" not in inline
    )


def test_hermes_store_system_prompt_opt_in(store, data):
    db, _ = store
    records, _ = data.fetch_run(db, {}, mode="backfill", store_system_prompt="1")
    prompt = sessions_by_id(records)[data.SESSION_A]["system_prompt"]
    assert data.PROMPT_CANARY in prompt["text"]


def test_hermes_rows_normalized_and_bounded(store, data):
    db, _ = store
    records, _ = data.fetch_run(db, {}, mode="backfill", max_text="1000")
    rows = messages_by_id(records)
    tool = rows[3]
    assert tool["reply_to"] == {"id": 2, "uid": None}
    assert len(tool["payload"]["content"]) == 1000
    assert tool["truncated"] == ["content"]
    assert tool["sizes"]["content"] == len(data.LONG_TOOL_OUTPUT)
    # Sentinel JSON content is decoded into its parts; tool calls into a list.
    assert rows[5]["payload"]["content"][0] == {
        "type": "text",
        "text": "Here is my invented garden",
    }
    assert rows[2]["payload"]["tool_calls"][0]["function"]["name"] == "web_search"
    # Blobs are dropped, unknown columns kept, volatile session columns left out.
    assert "display_identity" not in rows[4]["payload"]
    assert rows[6]["payload"]["future_message_column"] == "new upstream column"
    session = sessions_by_id(records)[data.SESSION_A]["payload"]
    assert session["future_session_column"] == "kept as is"
    assert "last_read_at" not in session and "last_activity_at" not in session
    assert session["origin_json"] == {
        "platform": "discord",
        "user_name": "invented_owner",
    }


def test_hermes_incremental_watermark_and_active_sessions(store, data):
    db, conn = store
    state = {}
    first, _ = data.fetch_run(db, state, mode="backfill")
    assert state["after_id"] == len(data.RUN1_MESSAGES)
    again, report = data.fetch_run(db, state, at="2024-03-06T01:00:00")
    assert not by_kind(again, "message") and report["complete"]
    data.advance_store(conn)
    second, _ = data.fetch_run(db, state, at="2024-03-11T00:00:00")
    assert sorted(messages_by_id(second)) == list(
        range(
            len(data.RUN1_MESSAGES) + 1,
            len(data.RUN1_MESSAGES) + len(data.RUN2_MESSAGES) + 1,
        )
    )
    # Only the sessions active since the last run (or touched) come again.
    assert set(sessions_by_id(second)) == {data.SESSION_A, data.SESSION_E}
    assert state["after_id"] == len(data.RUN1_MESSAGES) + len(data.RUN2_MESSAGES)


def test_hermes_cap_per_run_loses_nothing(store, data):
    db, conn = store
    data.advance_store(conn)
    state, seen, runs = {}, [], 0
    while True:
        records, report = data.fetch_run(
            db, state, max_messages="4", at=f"2024-03-12T0{runs}:00:00"
        )
        runs += 1
        ids = sorted(messages_by_id(records))
        seen += ids
        for record in by_kind(records, "message"):
            # The session of every message travels in the same run.
            assert record["session_id"] in sessions_by_id(records)
        if report["complete"]:
            break
        assert len(ids) == 4 and records[-1]["stopped"] == "max_messages"
    total = len(data.RUN1_MESSAGES) + len(data.RUN2_MESSAGES)
    assert seen == list(range(1, total + 1)) and runs == 6


def test_capped_incremental_keeps_session_watermark_until_complete(store, data):
    db, conn = store
    state = {"after_id": len(data.RUN1_MESSAGES), "since_ts": data.ts("2024-03-06")}
    data.advance_store(conn)
    _, report = data.fetch_run(
        db,
        state,
        max_messages="2",
        at="2024-03-12T00:00:00",
    )
    assert not report["complete"]
    assert state["since_ts"] == data.ts("2024-03-06")

    while not report["complete"]:
        _, report = data.fetch_run(
            db,
            state,
            max_messages="2",
            at="2024-03-12T00:01:00",
        )
    assert state["since_ts"] == data.ts("2024-03-12T00:01:00")


def test_hermes_backfill_bounds_and_resume(store, data):
    db, conn = store
    data.advance_store(conn)
    state = {"after_id": 3}
    records, report = data.fetch_run(
        db, state, mode="backfill", since="2024-03-02", until="2024-03-05"
    )
    days = {r["payload"]["timestamp"] for r in by_kind(records, "message")}
    assert min(days) >= data.ts("2024-03-02T00:00:00")
    assert max(days) < data.ts("2024-03-05T00:00:00")
    assert set(sessions_by_id(records)) == {
        data.SESSION_B,
        data.SESSION_C,
        data.SESSION_D,
    }
    assert state == {"after_id": 3}, "a bounded backfill never moves the sync watermark"
    # A capped backfill resumes where it stopped.
    state = {}
    first, report = data.fetch_run(db, state, mode="backfill", max_messages="10")
    assert not report["complete"] and state["backfill_after"] == 10
    rest, report = data.fetch_run(db, state, mode="backfill", max_messages="100")
    assert report["complete"] and "backfill_after" not in state
    assert min(messages_by_id(rest)) == 11
    assert state["after_id"] == len(data.RUN1_MESSAGES) + len(data.RUN2_MESSAGES)


def test_hermes_exclude_sources(store, data):
    db, _ = store
    records, _ = data.fetch_run(
        db, {}, mode="backfill", exclude_sources="cron,telegram"
    )
    assert {r["session_id"] for r in by_kind(records, "message")} == {
        data.SESSION_A,
        data.SESSION_D,
    }
    assert set(sessions_by_id(records)) == {data.SESSION_A, data.SESSION_D}


def test_hermes_schema_detected_at_run_time(tmp_path, data):
    """An older or reduced schema works; a missing essential column fails clearly."""
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, started_at REAL);"
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,"
        " content TEXT, timestamp REAL);"
    )
    conn.execute(
        "INSERT INTO sessions VALUES ('s1', 'cli', ?)",
        (data.ts("2024-01-01T00:00:00"),),
    )
    conn.execute(
        "INSERT INTO messages VALUES (1, 's1', 'user', 'invented', ?)",
        (data.ts("2024-01-01T00:00:01"),),
    )
    conn.commit()
    records, report = data.fetch_run(db, {})
    assert report["messages"] == 1 and records[0]["schema"]["version"] is None
    conn.execute("CREATE TABLE m2 AS SELECT id, session_id, content FROM messages")
    conn.execute("DROP TABLE messages")
    conn.execute("ALTER TABLE m2 RENAME TO messages")
    conn.commit()
    conn.close()
    with pytest.raises(fetch.FetchError, match="lacks column role"):
        data.fetch_run(db, {})


def test_hermes_escapes_nul_for_postgres(store, data):
    db, conn = store
    conn.execute("UPDATE messages SET content = ? WHERE id = 1", ("a\x00b",))
    conn.execute(
        "UPDATE sessions SET system_prompt = ? WHERE id = ?",
        ("prompt\x00text", data.SESSION_A),
    )
    conn.commit()
    cfg = fetch.config(
        dict(reader="readonly", db=str(db), store_system_prompt="1")
    )
    records = list(fetch.Fetcher(cfg, mode="backfill").run({}))
    assert messages_by_id(records)[1]["payload"]["content"] == "a\\u0000b"
    assert (
        sessions_by_id(records)[data.SESSION_A]["system_prompt"]["text"]
        == "prompt\\u0000text"
    )
    assert "\x00" not in json.dumps(records)


def test_hermes_missing_store_fails_clearly(tmp_path, data):
    with pytest.raises(fetch.FetchError, match="no Hermes session store"):
        data.fetch_run(tmp_path / "absent.db", {})


class FakePopen:
    """Stands in for `hermes sessions export -`: writes invented JSONL to stdout."""

    calls = []

    def __init__(self, lines, returncode=0, stderr=""):
        self.lines, self.code, self.err = lines, returncode, stderr

    def __call__(self, command, stdout, stderr, stdin, text):
        FakePopen.calls.append(command)
        stdout.write("Some status line\n")
        for line in self.lines:
            stdout.write(line + "\n")
        self.returncode = self.code
        return self

    def communicate(self, timeout=None):
        return None, self.err


def comparable(records):
    """What the mapping reads from message records, reader-independent."""
    out = {}
    for r in by_kind(records, "message"):
        p = r["payload"]
        out[r["id"]] = (
            r["session_id"],
            p["role"],
            json.dumps(p.get("content"), sort_keys=True),
            json.dumps(p.get("tool_calls"), sort_keys=True),
            p.get("tool_call_id"),
            p["timestamp"],
            r.get("reply_to"),
            r["reasoning_present"],
            p.get("active"),
            p.get("compacted"),
        )
    return out


def test_hermes_cli_reader_matches_snapshot(store, data):
    db, conn = store
    data.advance_store(conn)
    snapshot, _ = data.fetch_run(db, {}, mode="backfill")
    popen = FakePopen(data.cli_export_lines(conn))
    cfg = fetch.config(dict(reader="cli", cli="docker exec -i hermes hermes"))
    fetcher = fetch.Fetcher(
        cfg, mode="backfill", now=data.clock("2024-03-12T00:00:00"), popen=popen
    )
    records = list(fetcher.run({}))
    assert FakePopen.calls[-1] == [
        "docker",
        "exec",
        "-i",
        "hermes",
        "hermes",
        "sessions",
        "export",
        "-",
    ]
    assert records[0]["reader"] == "cli"
    assert comparable(records) == comparable(snapshot)
    assert data.PROMPT_CANARY not in json.dumps(records)
    assert data.REASONING_CANARY not in json.dumps(records)
    prompts = {r["id"]: r["system_prompt"] for r in by_kind(records, "session")}
    expected = {r["id"]: r["system_prompt"] for r in by_kind(snapshot, "session")}
    assert prompts[data.SESSION_A]["sha256"] == expected[data.SESSION_A]["sha256"]


def test_hermes_cli_reader_cap_and_watermark(store, data):
    db, conn = store
    lines = data.cli_export_lines(conn)
    cfg = fetch.config(dict(reader="cli", max_messages="5"))
    state = {}
    fetcher = fetch.Fetcher(
        cfg, now=data.clock("2024-03-12T00:00:00"), popen=FakePopen(lines)
    )
    first = list(fetcher.run(state))
    assert sorted(messages_by_id(first)) == [1, 2, 3, 4, 5] and state["after_id"] == 5
    fetcher = fetch.Fetcher(
        cfg, now=data.clock("2024-03-12T01:00:00"), popen=FakePopen(lines)
    )
    second = list(fetcher.run(state))
    assert sorted(messages_by_id(second)) == [6, 7, 8, 9, 10]


def test_hermes_cli_failure_and_file_reader(store, data, tmp_path):
    _, conn = store
    cfg = fetch.config(dict(reader="cli"))
    fetcher = fetch.Fetcher(
        cfg, popen=FakePopen([], returncode=3, stderr="boom\nlast line")
    )
    with pytest.raises(fetch.FetchError, match="exit 3"):
        list(fetcher.run({}))
    path = tmp_path / "hermes_sessions.jsonl"
    path.write_text("\n".join(data.cli_export_lines(conn)) + "\n", encoding="utf-8")
    cfg = fetch.config(dict(reader="file", export_file=str(path)))
    fetcher = fetch.Fetcher(cfg, mode="backfill", now=data.clock("2024-03-12T00:00:00"))
    records = list(fetcher.run({}))
    assert records[0]["reader"] == "file"
    assert len(by_kind(records, "message")) == len(data.RUN1_MESSAGES)
    assert path.exists(), "the file reader never removes the operator's export"
    cfg = fetch.config(dict(reader="file", export_file=str(tmp_path / "absent.jsonl")))
    with pytest.raises(fetch.FetchError, match="HERMES_EXPORT_FILE"):
        list(fetch.Fetcher(cfg).run({}))


def test_hermes_export_command_line(store, data, tmp_path, monkeypatch):
    db, _ = store
    out, state = tmp_path / "out.jsonl", tmp_path / "state.json"
    stdout, stderr = io.StringIO(), io.StringIO()
    code = fetch.main(
        [
            "export",
            "--out",
            str(out),
            "--state",
            str(state),
            "--reader",
            "snapshot",
            "--db",
            str(db),
            "--mode",
            "backfill",
        ],
        environ={},
        stdout=stdout,
        stderr=stderr,
    )
    assert code == 0, stderr.getvalue()
    lines = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["kind"] == "run" and lines[-1]["kind"] == "run_end"
    assert json.loads(state.read_text())["after_id"] == len(data.RUN1_MESSAGES)
    assert json.loads(stdout.getvalue())["messages"] == len(data.RUN1_MESSAGES)
    # Append a second (empty) run to the same file.
    code = fetch.main(
        [
            "export",
            "--out",
            str(out),
            "--append",
            "--state",
            str(state),
            "--db",
            str(db),
            "--reader",
            "snapshot",
        ],
        environ={},
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )
    assert code == 0
    runs = {
        json.loads(line)["run"] for line in out.read_text(encoding="utf-8").splitlines()
    }
    assert len(runs) == 2
    # Settings come from the environment; a bad one exits 2, a failure 1 with no file.
    code = fetch.main(
        ["export", "--out", str(tmp_path / "x.jsonl")],
        environ={"HERMES_MAX_MESSAGES": "lots"},
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )
    assert code == 2
    code = fetch.main(
        ["export", "--out", str(tmp_path / "y.jsonl"), "--reader", "snapshot"],
        environ={"HERMES_HOME": str(tmp_path / "nowhere")},
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )
    assert code == 1 and not (tmp_path / "y.jsonl").exists()
    assert not (tmp_path / "y.jsonl.part").exists()


def test_hermes_fetch_is_stdlib_only():
    import ast
    import sys

    tree = ast.parse(open(fetch.__file__, encoding="utf-8").read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").split(".")[0])
    assert names <= set(sys.stdlib_module_names), names - set(sys.stdlib_module_names)
    # No credential is ever taken on the command line.
    parser_text = open(fetch.__file__, encoding="utf-8").read()
    for word in ("--token", "--key", "--password", "--secret"):
        assert word not in parser_text


def test_hermes_cli_timeout(data):
    class Slow(FakePopen):
        def communicate(self, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired("hermes", timeout)
            return None, ""

        def kill(self):
            self.killed = True

    cfg = fetch.config(dict(reader="cli", cli_timeout_s="1"))
    with pytest.raises(fetch.FetchError, match="timed out"):
        list(fetch.Fetcher(cfg, popen=Slow([])).run({}))


def test_hermes_record_size_budget(tmp_path, data):
    """Many long strings in one row are cut harder until the record fits."""
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.executescript(data.SCHEMA)
    data.insert(conn, "sessions", data.session_row("s1", "cli", "2024-01-01T00:00:00"))
    calls = [(f"call_{n}", "write_file", {"text": "x" * 5000}) for n in range(60)]
    data.insert(
        conn,
        "messages",
        data.message_row(
            "s1",
            "assistant",
            "2024-01-01T00:00:01",
            "y" * 5000,
            tool_calls=data.tool_calls(*calls),
        ),
    )
    conn.commit()
    conn.close()
    records, _ = data.fetch_run(db, {}, max_text="1000")
    message = by_kind(records, "message")[0]
    assert len(json.dumps(message["payload"])) <= fetch.RECORD_BUDGET * 1000
    assert message["truncated"] and message["sizes"]["content"] == 5000
    assert message["payload"]["role"] == "assistant"


def test_hermes_credential_keys_scrubbed_and_redact_flag(store, data):
    db, conn = store
    conn.execute(
        "UPDATE sessions SET model_config = ? WHERE id = ?",
        (
            json.dumps(
                {"max_iterations": 90, "provider": {"api_key": "sk-invented-canary"}}
            ),
            data.SESSION_A,
        ),
    )
    conn.execute(
        "UPDATE messages SET display_metadata = ? WHERE id = 1",
        (json.dumps({"reactions": ["+1"], "bot_token": "invented-token-canary"}),),
    )
    conn.commit()
    records, _ = data.fetch_run(db, {}, mode="backfill")
    text = json.dumps(records)
    assert "sk-invented-canary" not in text and "invented-token-canary" not in text
    session = sessions_by_id(records)[data.SESSION_A]["payload"]
    assert session["model_config"] == {
        "max_iterations": 90,
        "provider": {"api_key": "[removed]"},
    }
    assert messages_by_id(records)[1]["payload"]["display_metadata"]["reactions"] == [
        "+1"
    ]
    cfg = fetch.config(dict(reader="cli", redact="1"))
    list(fetch.Fetcher(cfg, popen=FakePopen([])).run({}))
    assert FakePopen.calls[-1][-1] == "--redact"


def test_hermes_cli_refusal_on_stdout_fails_the_run(data):
    """`hermes sessions export` prints refusals to stdout, sometimes exiting 0."""
    refusal = "Error: session invented has 900000 stored rows (over sessions.max_export_messages)"
    for code in (0, 1):
        cfg = fetch.config(dict(reader="cli"))
        popen = FakePopen([refusal], returncode=code)
        with pytest.raises(fetch.FetchError, match="max_export_messages"):
            list(fetch.Fetcher(cfg, popen=popen).run({}))


def test_hermes_readonly_reader_matches_snapshot_and_streams_gzip(store, data, tmp_path):
    import gzip

    db, conn = store
    data.advance_store(conn)
    before = file_digest(db)
    snapshot, _ = data.fetch_run(db, {}, mode="backfill")
    cfg = fetch.config(dict(reader="readonly", db=str(db)))
    state = {}
    fetcher = fetch.Fetcher(
        cfg, mode="backfill", now=data.clock("2024-03-12T00:00:00")
    )
    records = list(fetcher.run(state))
    assert file_digest(db) == before
    assert records[0]["reader"] == "readonly"
    assert comparable(records) == comparable(snapshot)
    assert data.PROMPT_CANARY not in json.dumps(records)
    assert data.REASONING_CANARY not in json.dumps(records)
    assert not list(db.parent.glob("hermes-snapshot-*"))

    # Stream gzipped JSONL to stdout with --after-id and --since-ts.
    class FakeStdout:
        def __init__(self):
            self.buffer = io.BytesIO()

    out_stream = FakeStdout()
    stderr = io.StringIO()
    code = fetch.main(
        [
            "export",
            "--out",
            "-",
            "--gzip",
            "--reader",
            "readonly",
            "--db",
            str(db),
            "--mode",
            "sync",
            "--after-id",
            str(len(data.RUN1_MESSAGES)),
            "--since-ts",
            str(data.ts("2024-03-06T00:00:00")),
        ],
        environ={},
        stdout=out_stream,
        stderr=stderr,
    )
    assert code == 0, stderr.getvalue()
    report = json.loads(stderr.getvalue())
    assert report["messages"] == len(data.RUN2_MESSAGES)
    assert report["after_id"] == len(data.RUN1_MESSAGES) + len(data.RUN2_MESSAGES)
    decoded = gzip.decompress(out_stream.buffer.getvalue()).decode("utf-8").splitlines()
    streamed = [json.loads(line) for line in decoded]
    assert streamed[0]["kind"] == "run" and streamed[-1]["kind"] == "run_end"
    assert sorted(messages_by_id(streamed)) == list(
        range(
            len(data.RUN1_MESSAGES) + 1,
            len(data.RUN1_MESSAGES) + len(data.RUN2_MESSAGES) + 1,
        )
    )
