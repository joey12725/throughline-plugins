"""v1.1 mapping: revision time, subject time, record kinds, citations, schema.

Every source row is invented and lives in a disposable tl_test_ database.
"""

import contextlib
import copy
import json
import types

import psycopg
import pytest
from psycopg import sql
from throughline_sdk import Channel, Checkpoint, Client, Manifest, Record
from throughline_sdk._server import core_server

import throughline_gbrain
from throughline_gbrain import (
    CONTENT_BUDGET,
    REQUIRED_SCHEMA,
    TRUNCATED,
    GBrain,
    SchemaMismatch,
    digest,
    read_source,
)

PAGES = "fake-source:orchard"
SNAPSHOTS = (
    "INSERT INTO page_versions VALUES"
    " (1,1,'Seedling.','{}','2019-01-01T08:00:00Z'),"
    " (2,1,'Sapling.','{}','2019-02-01T09:30:00Z')"
)


def sql_rows(client, statement):
    return client.request("POST", "sql", {"statement": statement})["rows"]


def expected(p, data):
    p.source = lambda: data
    p.client = type("Cursor", (), {"cursor": lambda self, key: {}})()
    return list(p.records({}, "backfill"))


def messages(records, channel=None):
    return [
        r
        for r in records
        if isinstance(r, dict) and (channel is None or r["channel"] == channel)
    ]


@contextlib.contextmanager
def gbrain_core():
    with core_server() as (http, env):
        client = Client(env["THROUGHLINE_URL"], env["THROUGHLINE_TOKEN"], "gbrain")
        try:
            yield http, env, client
        finally:
            client.close()


def test_brn_5_each_snapshot_is_a_version_with_its_revision_time(
    source_dsn, monkeypatch
):
    monkeypatch.setenv("GBRAIN_DSN", source_dsn)
    with psycopg.connect(source_dsn) as conn:
        conn.execute(SNAPSHOTS)
    with gbrain_core() as (_, _env, client):
        GBrain().execute(client, "backfill")
        rows = sql_rows(
            client,
            f"SELECT version,content,happened_at,fields FROM api.message_versions WHERE external_id='{PAGES}' ORDER BY version",
        )
        assert [r["content"].split("\n\n")[1] for r in rows] == [
            "Seedling.",
            "Sapling.",
            "Apple trees.",
        ]
        times = [throughline_gbrain.utc(r["happened_at"]) for r in rows]
        # Snapshot time for a snapshot, update time for the live page; never
        # the effective date (2020-01-01) or creation time.
        assert times == [
            "2019-01-01T08:00:00+00:00",
            "2019-02-01T09:30:00+00:00",
            "2021-01-01T00:00:00+00:00",
        ]
        assert [throughline_gbrain.utc(r["fields"]["updated_at"]) for r in rows] == (
            times
        )
        assert all(a == b for a, b in GBrain().verify(client))


def test_brn_5_creation_time_only_when_revision_time_missing(source_dsn, monkeypatch):
    monkeypatch.setenv("GBRAIN_DSN", source_dsn)
    p = GBrain()
    data = p.source()
    data["pages"][0]["updated_at"] = None
    data["page_versions"] = [
        dict(
            id=1,
            page_id=1,
            compiled_truth="Seedling.",
            frontmatter={},
            snapshot_at=None,
        ),
        dict(
            id=2,
            page_id=1,
            compiled_truth="Sapling.",
            frontmatter={},
            snapshot_at="2019-02-01T00:00:00",  # no offset: unusable
        ),
    ]
    p.quarantined = 0
    orchard = [
        r
        for r in messages(expected(p, data), "gbrain.pages")
        if r["external_id"] == PAGES
    ]
    created = data["pages"][0]["created_at"]
    assert [r["happened_at"] for r in orchard] == [created, created]
    assert [
        r["payload"]["version"] and r["payload"]["version"]["id"] for r in orchard
    ] == [1, None]
    assert p.quarantined == 1


def test_msg_9_effective_date_is_subject_time(source_dsn, monkeypatch):
    monkeypatch.setenv("GBRAIN_DSN", source_dsn)
    p = GBrain()
    data = p.source()

    def subjects():
        return {
            r["external_id"]: r.get("subject")
            for r in messages(expected(p, copy.deepcopy(data)), "gbrain.pages")
        }

    # A date held at 00:00 UTC is a day; garden has no effective date.
    assert subjects() == {
        PAGES: {"start": "2020-01-01T00:00:00+00:00", "start_precision": "day"},
        "fake-source:garden": None,
    }
    data["pages"][0]["effective_date"] = "2020-01-01T10:30:00+02:00"
    assert subjects()[PAGES] == {
        "start": "2020-01-01T10:30:00+02:00",
        "start_precision": "exact",
    }
    data["pages"][0]["effective_date"] = "2020-03-04"
    assert subjects()[PAGES] == {
        "start": "2020-03-04T00:00:00+00:00",
        "start_precision": "day",
    }
    # An unusable effective date quarantines the page; nothing is guessed.
    data["pages"][0]["effective_date"] = "2020-03-04T10:00:00"
    p.quarantined = 0
    assert PAGES not in subjects()
    assert p.quarantined == 1
    # A source column of type date is read as a day as well.
    with psycopg.connect(source_dsn) as conn:
        conn.execute(
            "ALTER TABLE pages ALTER COLUMN effective_date TYPE date USING effective_date::date"
        )
        conn.execute("UPDATE pages SET effective_date='2020-05-06' WHERE id=2")
    with gbrain_core() as (_, _env, client):
        GBrain().execute(client, "backfill")
        rows = {
            r["external_id"]: r
            for r in sql_rows(
                client,
                "SELECT external_id,record_time,subject_from,subject_from_precision,subject_to FROM api.records WHERE channel='gbrain.pages'",
            )
        }
        assert throughline_gbrain.utc(rows["fake-source:garden"]["subject_from"]) == (
            "2020-05-06T00:00:00+00:00"
        )
        assert rows["fake-source:garden"]["subject_from_precision"] == "day"
        assert rows["fake-source:garden"]["subject_to"] is None
        # Record time stays the revision time, not the subject time.
        assert throughline_gbrain.utc(rows["fake-source:garden"]["record_time"]) == (
            "2021-01-01T00:00:00+00:00"
        )
        assert all(a == b for a, b in GBrain().verify(client))


def test_brn_5_kind_page_revision_and_records_asof(source_dsn, monkeypatch):
    monkeypatch.setenv("GBRAIN_DSN", source_dsn)
    with psycopg.connect(source_dsn) as conn:
        conn.execute(SNAPSHOTS)
    with gbrain_core() as (http, _env, client):
        GBrain().execute(client, "backfill")
        channels = {
            c["name"]: c
            for c in http.get("channels").json()
            if c["name"].startswith("gbrain.")
        }
        assert channels["gbrain.pages"]["record_kind"] == "page_revision"
        assert channels["gbrain.pages"]["is_reference"] is True
        assert channels["gbrain.pages"]["passage_text_paths"] == [
            "/page/title",
            "/version/compiled_truth|/page/compiled_truth",
            "/page/timeline",
        ]
        assert channels["gbrain.pages"]["provenance_link_types"] == ["cites"]
        assert channels["gbrain.timeline"]["record_kind"] == "derived"
        kinds = {
            (r["channel"], r["kind"], r["generated"])
            for r in sql_rows(
                client,
                "SELECT channel,kind,generated FROM api.records WHERE channel LIKE 'gbrain.%'",
            )
        }
        assert kinds == {
            ("gbrain.pages", "page_revision", True),
            ("gbrain.timeline", "derived", True),
        }

        def asof(at):
            rows = sql_rows(
                client,
                f"SELECT content FROM api.records_asof('{at}') WHERE external_id='{PAGES}'",
            )
            return [r["content"].split("\n\n")[1] for r in rows]

        assert asof("2018-12-31T00:00:00Z") == []
        assert asof("2019-01-15T00:00:00Z") == ["Seedling."]
        assert asof("2019-06-01T00:00:00Z") == ["Sapling."]
        assert asof("2022-01-01T00:00:00Z") == ["Apple trees."]


def test_msg_10_pages_and_timeline_not_applicable(source_dsn, monkeypatch):
    monkeypatch.setenv("GBRAIN_DSN", source_dsn)
    with psycopg.connect(source_dsn) as conn:
        conn.execute(SNAPSHOTS)
    with gbrain_core() as (_, _env, client):
        GBrain().execute(client, "backfill")
        rows = sql_rows(
            client,
            "SELECT channel,lat,lon,loc_reason FROM api.record_versions WHERE channel LIKE 'gbrain.%'",
        )
        assert {r["channel"] for r in rows} == {"gbrain.pages", "gbrain.timeline"}
        assert all(
            r["lat"] is None
            and r["lon"] is None
            and r["loc_reason"] == "not_applicable"
            for r in rows
        )


def test_msg_9_timeline_day_precision(source_dsn, monkeypatch):
    monkeypatch.setenv("GBRAIN_DSN", source_dsn)
    with gbrain_core() as (_, _env, client):
        GBrain().execute(client, "backfill")
        (row,) = sql_rows(
            client,
            "SELECT record_time,subject_from,subject_to,subject_from_precision,subject_to_precision FROM api.records WHERE channel='gbrain.timeline'",
        )
        assert throughline_gbrain.utc(row["record_time"]) == "2020-01-01T00:00:00+00:00"
        assert (
            throughline_gbrain.utc(row["subject_from"]) == "2020-01-01T00:00:00+00:00"
        )
        assert row["subject_from_precision"] == "day"
        assert row["subject_to"] is None and row["subject_to_precision"] is None


def old_mapping(records):
    """The same stream as mapping revision 2 sent it: record time was the
    effective date (or creation time), and nothing carried a subject."""
    old, cursors = [], {}
    for r in records:
        if isinstance(r, Checkpoint):
            if r.key.startswith("page_"):
                cursors[r.key] = dict(r.cursor)
            continue
        if not isinstance(r, dict):
            continue
        r = copy.deepcopy(r)
        r.pop("subject", None)
        if r["channel"] == "gbrain.pages":
            page = r["payload"]["page"]
            r["happened_at"] = page["effective_date"] or page["created_at"]
        old.append(r)
    return old, cursors


def test_msg_2_second_sync_after_bump_idempotent(source_dsn, monkeypatch):
    monkeypatch.setenv("GBRAIN_DSN", source_dsn)
    with psycopg.connect(source_dsn) as conn:
        conn.execute(SNAPSHOTS)
    p = GBrain()
    data = p.source()
    records = expected(p, data)
    old, cursors = old_mapping(records)
    with gbrain_core() as (_, _env, client):
        # A destination imported by mapping revision 2.
        client.register(GBrain.manifest)
        for message in old:
            client.ingest([message])
        for page in data["pages"]:
            key = "page_" + digest(f"{page['source_id']}:{page['slug']}")
            if key not in cursors:
                continue
            aliases = [
                a["alias_norm"]
                for a in data["page_aliases"]
                if (a["source_id"], a["slug"]) == (page["source_id"], page["slug"])
            ] + [
                a["alias_slug"]
                for a in data["slug_aliases"]
                if (a["source_id"], a["canonical_slug"])
                == (page["source_id"], page["slug"])
            ]
            tags = sorted(t["tag"] for t in data["tags"] if t["page_id"] == page["id"])
            cursors[key]["live"] = digest([2, page, aliases, tags])
            client.checkpoint(cursors[key], key)
        count = "SELECT channel,external_id,count(*) AS n FROM api.message_versions WHERE channel LIKE 'gbrain.%' GROUP BY channel,external_id"
        before = {
            (r["channel"], r["external_id"]): r["n"] for r in sql_rows(client, count)
        }
        written = []
        real_ingest = client.ingest

        def counting(batch, **kwargs):
            results = real_ingest(batch, **kwargs)
            written.append(sum(r["inserted"] + r["new_versions"] for r in results))
            return results

        client.ingest = counting
        GBrain().execute(client, "sync")
        after = {
            (r["channel"], r["external_id"]): r["n"] for r in sql_rows(client, count)
        }
        # Exactly one deliberate new version of every live page and entry.
        assert after == {key: n + 1 for key, n in before.items()}
        assert sum(written) == len(before)
        rows = sql_rows(
            client,
            f"SELECT version,happened_at FROM api.message_versions WHERE external_id='{PAGES}' ORDER BY version",
        )
        # Snapshots imported under the old mapping keep their old record time.
        assert [throughline_gbrain.utc(r["happened_at"]) for r in rows] == [
            "2020-01-01T00:00:00+00:00",
            "2020-01-01T00:00:00+00:00",
            "2020-01-01T00:00:00+00:00",
            "2021-01-01T00:00:00+00:00",
        ]
        pairs = GBrain().verify(client)
        assert all(a == b for a, b in pairs[:5])
        # verify reports the snapshots whose record time is not their revision time.
        assert pairs[5] == (set(), {1, 2})
        written.clear()
        GBrain().execute(client, "sync")
        assert sum(written) == 0
        assert {
            (r["channel"], r["external_id"]): r["n"] for r in sql_rows(client, count)
        } == after


def provenance_route(http):
    response = http.post(
        "provenance",
        json={
            "derived": {"channel": "gbrain.pages", "external_id": "fake-absent"},
            "sources": [],
            "relation": "cites",
        },
    )
    if response.status_code in (404, 405):
        pytest.skip("POST /v1/provenance (package w2-lineage) is not present")


def cited_world(source_dsn, env):
    """A second synthetic channel the GBrain source cites by Throughline key."""
    notes = Client(env["THROUGHLINE_URL"], env["THROUGHLINE_TOKEN"], "fakenotes")
    try:
        notes.register(
            Manifest(
                name="fakenotes",
                version="1",
                channels=[Channel(name="fake.notes", description="Invented notes")],
            )
        )
        notes.ingest(
            [
                {
                    "channel": "fake.notes",
                    "external_id": "note/1",
                    "thread": {"external_id": "fake-notes"},
                    "happened_at": "2019-12-01T00:00:00+00:00",
                    "content": "Invented note about the orchard.",
                    "payload": {"invented": True},
                }
            ]
        )
    finally:
        notes.close()
    with psycopg.connect(source_dsn) as conn:
        conn.execute(
            "UPDATE pages SET frontmatter=%s WHERE id=1",
            (
                json.dumps(
                    {
                        "throughline_sources": [
                            {"channel": "fake.notes", "external_id": "note/1"},
                            {"channel": "fake.notes", "external_id": "never-imported"},
                        ],
                        "source": "notes/orchard.md",
                    }
                ),
            ),
        )
        conn.execute(
            "UPDATE pages SET source_uri='throughline://fake.notes/note%2F1' WHERE id=2"
        )
        conn.execute(
            "UPDATE timeline_entries SET source='throughline://fake.notes/note%2F1' WHERE id=1"
        )
        # An uncited page: a file path is not a Throughline record.
        conn.execute(
            "INSERT INTO pages (id,source_id,slug,type,page_kind,title,compiled_truth,timeline,frontmatter,created_at,updated_at,source_uri) VALUES"
            " (4,'fake-source','meadow','place','note','Invented meadow','Grass.','','{}','2020-04-01Z','2021-01-01Z','file:///invented/meadow.md')"
        )


def message_id(client, channel, external_id):
    return sql_rows(
        client,
        f"SELECT id FROM api.messages WHERE channel='{channel}' AND external_id='{external_id}'",
    )[0]["id"]


def test_brn_6_frontmatter_and_uri_citations_become_provenance(source_dsn, monkeypatch):
    monkeypatch.setenv("GBRAIN_DSN", source_dsn)
    with gbrain_core() as (http, env, client):
        provenance_route(http)
        cited_world(source_dsn, env)
        result = GBrain().execute(client, "backfill")
        # orchard (two citations), garden (URI) and the timeline entry (URI).
        assert result["provenance"]["records"] == 3
        assert result["provenance"]["unresolved"] == 1
        for channel, external_id in (
            ("gbrain.pages", PAGES),
            ("gbrain.pages", "fake-source:garden"),
            ("gbrain.timeline", "1"),
        ):
            sources = sql_rows(
                client,
                f"SELECT channel,external_id FROM api.sources('message',{message_id(client, channel, external_id)})",
            )
            assert sources == [{"channel": "fake.notes", "external_id": "note/1"}]
        assert not sql_rows(
            client,
            f"SELECT id FROM api.sources('message',{message_id(client, 'gbrain.pages', 'fake-source:meadow')})",
        )
        # Other citation forms stay in fields and payload only.
        (meadow,) = sql_rows(
            client,
            "SELECT m.fields,r.unsupported FROM api.messages m JOIN api.records r ON r.message_id=m.id WHERE m.external_id='fake-source:meadow'",
        )
        assert meadow["fields"]["source_uri"] == "file:///invented/meadow.md"
        assert meadow["unsupported"] is True
        pairs = GBrain().verify(client)
        assert all(a == b for a, b in pairs)
        assert pairs.report["pages_with_sources"] == 2
        assert pairs.report["pages_without_sources"] == 1
        assert pairs.report["unresolved_citations"] == 1
        # A repeated sync re-sends the citations of every current revision
        # (the core keeps existing rows) and writes no version.
        versions = "SELECT count(*) AS n FROM api.message_versions WHERE channel LIKE 'gbrain.%'"
        count = sql_rows(client, versions)
        again = GBrain().execute(client, "sync")
        assert again["provenance"] == {"records": 3, "written": 0, "unresolved": 1}
        assert sql_rows(client, versions) == count
        assert all(a == b for a, b in GBrain().verify(client))
        # A cited record imported after the page resolves on the next sync,
        # although the page itself is unchanged.
        notes = Client(env["THROUGHLINE_URL"], env["THROUGHLINE_TOKEN"], "fakenotes")
        try:
            notes.ingest(
                [
                    {
                        "channel": "fake.notes",
                        "external_id": "never-imported",
                        "thread": {"external_id": "fake-notes"},
                        "happened_at": "2019-12-02T00:00:00+00:00",
                        "content": "Invented late note.",
                        "payload": {"invented": True},
                    }
                ]
            )
        finally:
            notes.close()
        late = GBrain().execute(client, "sync")
        assert late["provenance"] == {"records": 3, "written": 1, "unresolved": 0}
        assert sql_rows(client, versions) == count
        assert sql_rows(
            client,
            f"SELECT channel,external_id FROM api.sources('message',{message_id(client, 'gbrain.pages', PAGES)}) ORDER BY external_id",
        ) == [
            {"channel": "fake.notes", "external_id": "never-imported"},
            {"channel": "fake.notes", "external_id": "note/1"},
        ]
        pairs = GBrain().verify(client)
        assert all(a == b for a, b in pairs)
        assert pairs.report["unresolved_citations"] == 0


def test_acl_9_cited_page_hidden_when_source_hidden(source_dsn, monkeypatch):
    from throughline import access
    from throughline.db import connect

    monkeypatch.setenv("GBRAIN_DSN", source_dsn)
    with gbrain_core() as (http, env, client):
        provenance_route(http)
        cited_world(source_dsn, env)
        GBrain().execute(client, "backfill")
        monkeypatch.setenv("THROUGHLINE_DATABASE_URL", env["THROUGHLINE_DATABASE_URL"])
        access.register_asker("fake-guest")
        access.grant("fake-guest", channel="gbrain.pages")

        def visible():
            with connect("reader") as c:
                for name, value in (
                    ("scope_mode", "asker"),
                    ("asker", "fake-guest"),
                    ("unknown_asker", "deny"),
                ):
                    c.execute(
                        sql.SQL("SET LOCAL throughline.{}={}").format(
                            sql.SQL(name), sql.Literal(value)
                        )
                    )
                return {
                    r["external_id"]
                    for r in c.execute(
                        "SELECT external_id FROM api.messages WHERE channel='gbrain.pages'"
                    ).fetchall()
                }

        # Pages citing a hidden note are hidden; the uncited page follows its
        # own channel grant only.
        assert visible() == {"fake-source:meadow"}
        access.grant("fake-guest", channel="fake.notes")
        assert visible() == {PAGES, "fake-source:garden", "fake-source:meadow"}


class Recording:
    """A source connection that records every statement it is given."""

    def __init__(self, conn, log):
        self.conn, self.log = conn, log

    def __enter__(self):
        self.conn.__enter__()
        return self

    def __exit__(self, *exc):
        return self.conn.__exit__(*exc)

    def execute(self, statement, params=None):
        text = (
            statement.as_string(self.conn)
            if isinstance(statement, sql.Composable)
            else statement
        )
        self.log.append(text)
        result = self.conn.execute(statement, params)
        if text.startswith("SET TRANSACTION"):
            read_only = self.conn.execute("SHOW transaction_read_only").fetchone()
            self.log.append("read_only=" + read_only["transaction_read_only"])
        return result


def recording(monkeypatch):
    """Record the adapter's source statements; other connections are untouched."""
    log = []
    real = psycopg.connect
    monkeypatch.setattr(
        throughline_gbrain,
        "psycopg",
        types.SimpleNamespace(connect=lambda *a, **k: Recording(real(*a, **k), log)),
    )
    return log


def test_brn_7_schema_check_names_missing_column_and_tolerates_extras(
    source_dsn, monkeypatch
):
    with psycopg.connect(source_dsn) as conn:
        conn.execute("ALTER TABLE pages ADD COLUMN invented_extra text DEFAULT 'x'")
        conn.execute("CREATE TABLE public.invented_extra_table (id int)")
    log = recording(monkeypatch)
    data = read_source(source_dsn)
    # The check runs first, inside the read-only snapshot transaction.
    assert log[0].startswith(
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
    )
    assert log[1] == "read_only=on"
    assert "information_schema.columns" in log[2]
    assert all("invented_extra" not in json.dumps(r) for r in data["pages"])
    assert set(data["pages"][0]) == set(REQUIRED_SCHEMA["pages"])
    assert data["schema_check"]["compatible"] is True
    assert data["schema_check"]["pinned_upstream"] is None
    with psycopg.connect(source_dsn) as conn:
        conn.execute("ALTER TABLE pages DROP COLUMN title")
        conn.execute("ALTER TABLE page_versions DROP COLUMN snapshot_at")
        conn.execute("DROP TABLE sources")
    log.clear()
    with pytest.raises(SchemaMismatch) as error:
        read_source(source_dsn)
    message = str(error.value)
    for name in (
        "pages.title",
        "page_versions.snapshot_at",
        "sources.id",
        "sources.name",
    ):
        assert name in message
    assert "pages.slug" not in message
    # No data query ran against the incompatible source.
    assert not [s for s in log if "FROM public." in s or 'FROM "public"' in s]


def source_state(dsn):
    with psycopg.connect(dsn) as conn:
        return {
            table: conn.execute(
                sql.SQL(
                    "SELECT md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), '')) FROM public.{} t"
                ).format(sql.Identifier(table))
            ).fetchone()[0]
            for table in REQUIRED_SCHEMA
        }


def test_brn_7_source_never_written(source_dsn, monkeypatch):
    monkeypatch.setenv("GBRAIN_DSN", source_dsn)
    with psycopg.connect(source_dsn) as conn:
        conn.execute(SNAPSHOTS)
    before = source_state(source_dsn)
    log = recording(monkeypatch)
    with gbrain_core() as (_, _env, client):
        GBrain().execute(client, "backfill")
        GBrain().execute(client, "sync")
        pairs = GBrain().verify(client)
        assert all(a == b for a, b in pairs)
        assert pairs.report["pinned_upstream"] is None
        assert pairs.report["schema_check"]["compatible"] is True
    assert source_state(source_dsn) == before
    assert log
    for statement in log:
        assert statement.startswith(
            (
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY",
                "SELECT",
                "read_only=on",
            )
        ), statement
    assert log.count("read_only=on") == log.count(
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
    )


def resolve(payload, entries):
    """The passage text of a payload, by the channel's passage_text_paths."""

    def pointer(value, path):
        for part in path.split("/")[1:]:
            part = part.replace("~1", "/").replace("~0", "~")
            if isinstance(value, dict) and part in value:
                value = value[part]
            elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
                value = value[int(part)]
            else:
                return None
        return value

    parts = []
    for entry in entries:
        for path in entry.split("|"):
            found = pointer(payload, path)
            if isinstance(found, str) and found.strip():
                parts.append(found)
                break
    return "\n\n".join(parts)


def test_passage_text_paths_resolve_full_text(source_dsn, monkeypatch):
    monkeypatch.setenv("GBRAIN_DSN", source_dsn)
    p = GBrain()
    data = p.source()
    huge = "Invented orchard paragraph. " * 12_000  # 336,000 bytes
    assert len(huge.encode()) > CONTENT_BUDGET
    data["pages"][0]["compiled_truth"] = huge
    data["page_versions"] = [
        dict(
            id=1,
            page_id=1,
            compiled_truth="Invented snapshot body.",
            frontmatter={},
            snapshot_at="2019-01-01T00:00:00+00:00",
        )
    ]
    (paths,) = [
        c.passage_text_paths
        for c in GBrain.manifest.channels
        if c.name == "gbrain.pages"
    ]
    snapshot, live = [
        r
        for r in messages(expected(p, data), "gbrain.pages")
        if r["external_id"] == PAGES
    ]
    assert live["content"].endswith(TRUNCATED)
    full = resolve(live["payload"], paths)
    assert full == "\n\n".join(["Invented orchard", huge, "A tree was planted."])
    assert len(full.encode()) > CONTENT_BUDGET
    # A snapshot's passage text is its own body, not the current page.
    assert resolve(snapshot["payload"], paths) == "\n\n".join(
        ["Invented orchard", "Invented snapshot body.", "A tree was planted."]
    )


def test_provenance_records_follow_their_revision(source_dsn, monkeypatch):
    monkeypatch.setenv("GBRAIN_DSN", source_dsn)
    p = GBrain()
    data = p.source()
    data["pages"][0]["frontmatter"] = {
        "throughline_sources": [
            {"channel": "fake.notes", "external_id": "b"},
            {"channel": "fake.notes", "external_id": "a"},
            {"channel": "fake.notes", "external_id": "a"},
            {"channel": "", "external_id": "dropped"},
            "fake.notes/not-a-record",
        ]
    }
    data["pages"][1]["source_uri"] = "https://invented.example/garden"
    data["timeline_entries"][0]["source"] = "throughline://fake.notes/c"
    records = expected(p, data)
    cited = [
        (i, r)
        for i, r in enumerate(records)
        if isinstance(r, Record) and r.kind == "provenance"
    ]
    assert [r.data["derived"] for _, r in cited] == [
        {"channel": "gbrain.pages", "external_id": PAGES},
        {"channel": "gbrain.timeline", "external_id": "1"},
    ]
    assert cited[0][1].data["sources"] == [
        {"channel": "fake.notes", "external_id": "a"},
        {"channel": "fake.notes", "external_id": "b"},
    ]
    assert cited[1][1].data["sources"] == [
        {"channel": "fake.notes", "external_id": "c"}
    ]
    for index, record in cited:
        before = records[index - 1]
        assert isinstance(before, dict)
        assert (before["channel"], before["external_id"]) == (
            record.data["derived"]["channel"],
            record.data["derived"]["external_id"],
        )


def test_brn_6_citations_split_at_the_core_limit_and_skip_nul(source_dsn, monkeypatch):
    monkeypatch.setenv("GBRAIN_DSN", source_dsn)
    p = GBrain()
    data = p.source()
    many = [
        {"channel": "fake.notes", "external_id": f"n{n:05d}"}
        for n in range(throughline_gbrain.MAX_CITATIONS + 5)
    ]
    data["pages"][0]["frontmatter"] = {"throughline_sources": many}
    # (A NUL in frontmatter quarantines the whole page; the citation reader
    # drops such a key on its own as well.)
    assert not throughline_gbrain.frontmatter_citations(
        {"throughline_sources": [{"channel": "fake.notes", "external_id": chr(0)}]}
    )
    # Percent-decoding must not produce a key the core cannot store.
    data["pages"][1]["source_uri"] = "throughline://fake.notes/bad%00"
    data["timeline_entries"][0]["source"] = "throughline://fake.notes/also%00bad"
    cited = [
        r for r in expected(p, data) if isinstance(r, Record) and r.kind == "provenance"
    ]
    assert [len(r.data["sources"]) for r in cited] == [
        throughline_gbrain.MAX_CITATIONS,
        5,
    ]
    assert {r.data["derived"]["external_id"] for r in cited} == {PAGES}
    assert [s for r in cited for s in r.data["sources"]] == many
