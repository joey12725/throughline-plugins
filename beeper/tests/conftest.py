import json
import subprocess
import time
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

RECORDINGS = Path(__file__).parent / "recorded"


@pytest.fixture(autouse=True)
def isolated_enrichment(monkeypatch, tmp_path):
    """Never touch the owner's plugin state; enrichment off unless a test opts in.

    Subprocesses (the contract kit) inherit these variables.
    """
    monkeypatch.setenv("BEEPER_STATE_DIR", str(tmp_path / "beeper-state"))
    for name in (
        "BEEPER_ATTACHMENTS",
        "BEEPER_INSTAGRAM_LINKS",
        "BEEPER_ASSET_DIR",
        "BEEPER_ASSET_BASE_URL",
        "BEEPER_YTDLP_COOKIES",
        "BEEPER_YTDLP_COOKIES_FROM_BROWSER",
        "BEEPER_TRANSCRIBE_URL",
        "BEEPER_TRANSCRIBE_API_KEY",
        "BEEPER_TRANSCRIBE_MODEL",
        "BEEPER_YOUTUBE_LINKS",
        "BEEPER_YOUTUBE_TRANSCRIPT_SOURCE",
        "THROUGHLINE_TRANSCRIBE_URL",
        "THROUGHLINE_TRANSCRIBE_API_KEY",
        "THROUGHLINE_TRANSCRIBE_MODEL",
        "THROUGHLINE_TRANSCRIBE_MAX_MINUTES",
        "THROUGHLINE_TRANSCRIBE_MINUTES_PER_DAY",
        "BEEPER_ENRICH",
        "BEEPER_BACKLOG",
        "BEEPER_ATTACHMENTS_PER_DAY",
        "BEEPER_INSTAGRAM_PER_DAY",
        "BEEPER_YOUTUBE_PER_DAY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BEEPER_TRANSCRIBER", "none")
    # The endpoint's daily minutes are one pool for every plugin, kept outside
    # the plugin state directory: keep that file in the test too.
    monkeypatch.setenv("THROUGHLINE_TRANSCRIBE_STATE_DIR", str(tmp_path / "transcribe"))
    # The recorded fixtures are old messages: date the enable time before them
    # so enrichment tests see them as new inbound. Pacing tests unset this.
    monkeypatch.setenv("BEEPER_ENRICH_SINCE", "2000-01-01")
    return tmp_path / "beeper-state"


@pytest.fixture
def retry_core_role_race(monkeypatch):
    """Work around cluster-wide ALTER ROLE races between isolated core tests.

    Core's migration advisory lock is database-local; every kit database alters
    the same cluster roles. Retry only this proven transient setup failure, not
    plugin runs, contract assertions, or any other database error.
    """
    original = subprocess.run

    def guarded_run(*args, **kwargs):
        command = args[0] if args else kwargs.get("args", [])
        for attempt in range(5):
            try:
                return original(*args, **kwargs)
            except subprocess.CalledProcessError as error:
                stderr = error.stderr or b""
                if isinstance(stderr, bytes):
                    stderr = stderr.decode(errors="replace")
                if (
                    not command
                    or command[-1] != "migrate"
                    or "tuple concurrently updated" not in stderr
                    or attempt == 4
                ):
                    raise
                time.sleep(0.1 * 2**attempt)

    monkeypatch.setattr(subprocess, "run", guarded_run)


@pytest.fixture
def synthetic_archive():
    # Never accept a source DSN from the owner's environment.
    admin_dsn = "postgresql://postgres@127.0.0.1:54331/postgres"
    name = "tl_test_" + uuid.uuid4().hex
    dsn = make_conninfo(admin_dsn, dbname=name)
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        data = json.loads((RECORDINGS / "archive.json").read_text())
        shapes = {
            "messages": "id platform conversation_id native_id sender_id sender_name direction sent_at time_raw time_precision body edited_at reply_to attachments_json reactions_json metadata_json identity_quality first_imported_at last_seen_at",
            "conversations": "id platform native_id title kind guild_id metadata_json",
            "identities": "identity_id platform namespace native_id",
            "message_aliases": "platform native_id message_id",
        }
        with psycopg.connect(dsn) as conn:
            conn.execute("CREATE SCHEMA archive")
            for table, names in shapes.items():
                columns = names.split()
                definitions = [
                    sql.SQL("{} TEXT{}").format(
                        sql.Identifier(c),
                        sql.SQL(
                            " PRIMARY KEY"
                            if c in {"id", "identity_id"}
                            else " NOT NULL"
                            if table == "message_aliases"
                            and c in {"platform", "native_id"}
                            else ""
                        ),
                    )
                    for c in columns
                ]
                conn.execute(
                    sql.SQL("CREATE TABLE archive.{} ({})").format(
                        sql.Identifier(table), sql.SQL(",").join(definitions)
                    )
                )
                for row in data[table]:
                    conn.execute(
                        sql.SQL("INSERT INTO archive.{} ({}) VALUES ({})").format(
                            sql.Identifier(table),
                            sql.SQL(",").join(map(sql.Identifier, columns)),
                            sql.SQL(",").join(sql.Placeholder() for _ in columns),
                        ),
                        [row.get(c) for c in columns],
                    )
        yield dsn
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )
