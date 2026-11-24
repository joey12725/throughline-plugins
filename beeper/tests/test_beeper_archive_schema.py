"""The published archive-schema.sql is what the archive source reads.

Loads the invented archive recording into a disposable tl_test_ database
created from archive-schema.sql and reads it back through the Postgres path.
"""

import json
import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from throughline_beeper.sources import Archive

HERE = Path(__file__).parent
SCHEMA = HERE.parent / "archive-schema.sql"
ADMIN = os.environ.get(
    "THROUGHLINE_TEST_DATABASE_URL", "postgresql://postgres@127.0.0.1:54331/postgres"
)


@pytest.fixture
def schema_dsn():
    name = "tl_test_" + uuid4().hex
    try:
        admin = psycopg.connect(ADMIN, autocommit=True)
    except psycopg.Error:
        pytest.skip("no test Postgres")
    with admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    dsn = make_conninfo(ADMIN, dbname=name)
    try:
        yield dsn
    finally:
        with psycopg.connect(ADMIN, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(name)
                )
            )


def test_archive_schema_matches_the_archive_source(schema_dsn):
    data = json.loads((HERE / "recorded" / "archive.json").read_text())
    with psycopg.connect(schema_dsn) as conn:
        conn.execute(SCHEMA.read_text())
        for table, rows in data.items():
            for row in rows:
                columns = list(row)
                conn.execute(
                    sql.SQL("INSERT INTO archive.{} ({}) VALUES ({})").format(
                        sql.Identifier(table),
                        sql.SQL(",").join(map(sql.Identifier, columns)),
                        sql.SQL(",").join(sql.Placeholder() * len(columns)),
                    ),
                    [
                        json.dumps(v) if isinstance(v, (dict, list)) else v
                        for v in row.values()
                    ],
                )
    live = Archive(dsn=schema_dsn)
    fixture = Archive(recording=HERE / "recorded" / "archive.json")
    try:
        assert live.networks() == fixture.networks()
        assert sum(map(len, live.batches())) == len(data["messages"])
        assert live.message_aliases() == fixture.message_aliases()
        assert len(live.table("conversations")) == len(data["conversations"])
    finally:
        live.close()
