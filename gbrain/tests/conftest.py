"""Synthetic source tables in a disposable database; never inspect owner data."""

import os
import subprocess
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo


@pytest.fixture(autouse=True)
def retry_disposable_role_race(monkeypatch):
    """Core's database-local migration lock cannot serialize cluster role DDL."""
    original = subprocess.run

    def run(*args, **kwargs):
        command = args[0] if args else kwargs.get("args", [])
        env = kwargs.get("env", {})
        settings = conninfo_to_dict(env.get("THROUGHLINE_DATABASE_URL", ""))
        isolated_migration = (
            isinstance(command, list)
            and command[-3:] == ["-m", "throughline", "migrate"]
            and settings.get("host") == "127.0.0.1"
            and settings.get("dbname", "").startswith("tl_test_")
        )
        for attempt in range(8):
            try:
                return original(*args, **kwargs)
            except subprocess.CalledProcessError as error:
                stderr = error.stderr or b""
                if isinstance(stderr, str):
                    stderr = stderr.encode()
                if (
                    not isolated_migration
                    or b"tuple concurrently updated" not in stderr
                    or attempt == 7
                ):
                    raise

    monkeypatch.setattr(subprocess, "run", run)


@pytest.fixture
def source_dsn():
    admin = os.getenv(
        "THROUGHLINE_TEST_DATABASE_URL",
        "postgresql://postgres@127.0.0.1:54331/postgres",
    )
    settings = conninfo_to_dict(admin)
    if (
        settings.get("host") != "127.0.0.1"
        or settings.get("hostaddr") not in (None, "127.0.0.1")
        or settings.get("dbname") != "postgres"
    ):
        raise ValueError("tests require local administrative postgres")
    name = "tl_test_" + uuid.uuid4().hex
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    dsn = make_conninfo(**dict(settings, dbname=name))
    try:
        with psycopg.connect(dsn) as conn:
            conn.execute(
                Path(__file__).with_name("source.sql").read_text(encoding="utf-8")
            )
        yield dsn
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )
