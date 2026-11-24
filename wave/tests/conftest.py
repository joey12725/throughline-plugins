"""Invented Wave archive in a disposable tl_test_ database; never owner data."""

from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

RECORDED = Path(__file__).parent / "recorded"
# Deliberately fixed local test service. No owner database discovery.
ADMIN = "postgresql://postgres@127.0.0.1:54331/postgres"


@pytest.fixture
def archive_dsn():
    name = "tl_test_" + uuid4().hex
    with psycopg.connect(ADMIN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    dsn = make_conninfo(ADMIN, dbname=name)
    try:
        with psycopg.connect(dsn) as conn:
            conn.execute((RECORDED.parent.parent / "archive-schema.sql").read_text())
            for number in range(3):
                rid = f"fake-recording-{number}"
                conn.execute(
                    """INSERT INTO transcripts.recording
                    (id,source,source_id,title,started_at,started_at_from,duration_s,
                     device,source_platform,place,lat,lon,ignore_reason,metadata,imported_at,updated_at)
                    VALUES (%s,'wave',%s,%s,'2020-01-01T10:00:00+02:00','manual',60,
                    'invented-device','desktop','Invented orchard',12,34,%s,
                    '{"summary":"Invented orchard planning","type":"meeting"}',
                    '2020-01-02Z','2020-01-02Z')""",
                    (
                        rid,
                        rid,
                        f"Invented meeting {number}",
                        "synthetic ignore" if number == 2 else None,
                    ),
                )
                for turn in range(1, 5):
                    conn.execute(
                        """INSERT INTO transcripts.turn
                        (recording_id,turn_no,speaker,start_s,end_s,text,said_at,said_at_manual,
                         ignore_reason,metadata)
                        VALUES (%s,%s,%s,%s,%s,%s,
                        '2020-01-01T08:00:00Z'::timestamptz + make_interval(secs=>%s),
                        CASE WHEN %s=3 THEN '2020-01-01T08:00:45Z'::timestamptz END,%s,'{}')""",
                        (
                            rid,
                            turn,
                            "Voice A" if turn % 2 else "Voice B",
                            turn * 5,
                            turn * 5 + 2,
                            f"Invented turn {turn}",
                            turn * 5,
                            turn,
                            "synthetic ignored turn" if turn == 4 else None,
                        ),
                    )
                conn.execute(
                    """INSERT INTO transcripts.speaker VALUES
                    (%s,'Voice A','Voice A','wave','voice_profile','fake-profile-a','voice_match')""",
                    (rid,),
                )
        yield dsn
    finally:
        with psycopg.connect(ADMIN, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )
