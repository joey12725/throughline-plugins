"""Importer hygiene, legacy channels, coverage and the report.

Every source row is invented and lives in a disposable tl_test_ database.
"""

import contextlib
import json
import subprocess
import sys
from pathlib import Path

import psycopg
from throughline_sdk import Client
from throughline_sdk._server import core_server

from throughline_gbrain import (
    LEGACY,
    LEGACY_TIMELINE,
    PAGES,
    TIMELINE,
    GBrain,
    cited_first,
    is_legacy,
    normalize_type,
)

LEGACY_SQL = Path(__file__).with_name("legacy.sql")


def sql_rows(client, statement):
    return client.request("POST", "sql", {"statement": statement})["rows"]


def with_legacy(dsn):
    with psycopg.connect(dsn) as conn:
        conn.execute(LEGACY_SQL.read_text(encoding="utf-8"))


def plugin_token(env, plugin="gbrain"):
    output = subprocess.run(
        [
            sys.executable,
            "-m",
            "throughline",
            "token",
            "create",
            "invented-" + plugin,
            "--kind",
            "plugin",
            "--plugin",
            plugin,
            "--json",
        ],
        env=env,
        check=True,
        capture_output=True,
    ).stdout
    return json.loads(output)["token"]


@contextlib.contextmanager
def gbrain_core():
    """(owner client, plugin client, env) against a disposable core."""
    with core_server() as (_http, env):
        owner = Client(env["THROUGHLINE_URL"], env["THROUGHLINE_TOKEN"], "gbrain")
        plugin = Client(env["THROUGHLINE_URL"], plugin_token(env), "gbrain")
        try:
            yield owner, plugin, env
        finally:
            owner.close()
            plugin.close()


def coverage_rows(client):
    return {
        r["channel"]: r
        for r in sql_rows(
            client,
            "SELECT channel,scope_key,status,reasons,raw_detail,rows_delivered FROM api.coverage WHERE plugin='gbrain' AND channel_id IS NOT NULL",
        )
    }


def recall(client, query, **scope):
    answer = client.request("POST", "recall", {"query": query, "scope": scope})
    return {(r["channel"], r["external_id"]) for r in answer["records"]}


def message_id(client, channel, external_id):
    (row,) = sql_rows(
        client,
        f"SELECT id FROM api.messages WHERE channel='{channel}' AND external_id='{external_id}'",
    )
    return row["id"]


def test_legacy_marker_forms():
    marker = "source_copy"
    page = {"frontmatter": {}, "page_kind": "note", "type": "document"}
    assert not is_legacy(page, [], marker)
    assert is_legacy(dict(page, frontmatter={"source_copy": True}), [], marker)
    assert not is_legacy(dict(page, frontmatter={"source_copy": False}), [], marker)
    assert is_legacy(dict(page, frontmatter={"kind": "Source_Copy "}), [], marker)
    assert is_legacy(dict(page, frontmatter={"tags": ["x", "source_copy"]}), [], marker)
    assert is_legacy(dict(page, page_kind="source_copy"), [], marker)
    assert is_legacy(page, ["source_copy"], marker)
    assert not is_legacy(dict(page, page_kind="source_copy"), [], "")
    assert normalize_type("Mention!") == "mention"
    assert normalize_type("→").startswith("t_") and len(normalize_type("→")) == 14
    assert normalize_type("2nd degree") == "t_2nd_degree"


def test_soft_deleted_skipped_and_reported_as_filtered_coverage(
    source_dsn, monkeypatch
):
    monkeypatch.setenv("GBRAIN_DSN", source_dsn)
    with gbrain_core() as (owner, plugin, _env):
        result = GBrain().execute(plugin, "backfill")
        assert result["coverage"]["refused"] == 0
        rows = coverage_rows(owner)
        pages = rows[PAGES]
        assert pages["status"] == "complete"
        assert pages["reasons"] == ["filtered_by_policy"]
        assert pages["raw_detail"] == {"skipped_deleted": 1}
        assert pages["rows_delivered"] == 2
        assert rows[TIMELINE]["status"] == "complete"
        assert rows[TIMELINE]["reasons"] == []
        assert {LEGACY, LEGACY_TIMELINE} <= set(rows)
        # The deleted page was not imported.
        assert not sql_rows(
            owner, "SELECT id FROM api.messages WHERE external_id='fake-source:retired'"
        )
        # Each run reports again, even when nothing changed.
        GBrain().execute(plugin, "sync")
        assert coverage_rows(owner)[PAGES]["rows_delivered"] == 0
        assert coverage_rows(owner)[PAGES]["status"] == "complete"


def test_gbrain_coverage_quarantined_partial(source_dsn, monkeypatch):
    monkeypatch.setenv("GBRAIN_DSN", source_dsn)
    p = GBrain()
    data = p.source()
    data["page_versions"] = [
        dict(
            id=9,
            page_id=1,
            compiled_truth="Invented snapshot.",
            frontmatter={},
            snapshot_at="2019-02-01T00:00:00",  # no offset: quarantined
        )
    ]
    p.source = lambda: data
    with gbrain_core() as (owner, plugin, _env):
        result = p.execute(plugin, "backfill")
        assert result["quarantined"] == 1
        pages = coverage_rows(owner)[PAGES]
        assert pages["status"] == "partial"
        assert "quarantined_rows" in pages["reasons"]
        assert "filtered_by_policy" in pages["reasons"]
        assert pages["raw_detail"] == {"quarantined": 1, "skipped_deleted": 1}
        assert coverage_rows(owner)[TIMELINE]["status"] == "complete"


def test_legacy_marker_pages_to_reference_channel_out_of_default_recall(
    source_dsn, monkeypatch
):
    monkeypatch.setenv("GBRAIN_DSN", source_dsn)
    with_legacy(source_dsn)
    with gbrain_core() as (owner, plugin, _env):
        GBrain().execute(plugin, "backfill")
        channels = {
            r["external_id"]: r["channel"]
            for r in sql_rows(
                owner,
                "SELECT channel,external_id FROM api.messages WHERE channel LIKE 'gbrain.%' AND channel NOT LIKE '%timeline'",
            )
        }
        assert channels == {
            "fake-source:orchard": PAGES,
            "fake-source:garden": PAGES,
            "fake-source:invented-person": PAGES,
            "fake-source:legacy-note": LEGACY,
            "fake-source:legacy-flag": LEGACY,
        }
        described = owner.request(
            "GET", "describe", params={"subject": "channel:" + LEGACY}
        )
        assert described["record_kind"] == "reference"
        assert described["is_reference"] is True
        # No passages for reference text.
        note = message_id(owner, LEGACY, "fake-source:legacy-note")
        assert sql_rows(
            owner, f"SELECT count(*) AS n FROM api.passages_of({note})"
        ) == [{"n": 0}]
        default = recall(owner, "Quillberry")
        assert not any(channel == LEGACY for channel, _ in default)
        opted = recall(owner, "Quillberry", kinds=["reference"])
        assert (LEGACY, "fake-source:legacy-note") in opted
        assert (LEGACY, "fake-source:legacy-flag") in opted


def test_legacy_timeline_entries_out_of_default_recall(source_dsn, monkeypatch):
    monkeypatch.setenv("GBRAIN_DSN", source_dsn)
    with_legacy(source_dsn)
    with gbrain_core() as (owner, plugin, _env):
        GBrain().execute(plugin, "backfill")
        assert sql_rows(
            owner,
            "SELECT channel,external_id FROM api.messages WHERE channel LIKE 'gbrain.%timeline' ORDER BY external_id",
        ) == [
            {"channel": TIMELINE, "external_id": "1"},
            {"channel": LEGACY_TIMELINE, "external_id": "2"},
        ]
        described = owner.request(
            "GET", "describe", params={"subject": "channel:" + LEGACY_TIMELINE}
        )
        assert described["record_kind"] == "reference"
        assert not any(
            c == LEGACY_TIMELINE for c, _ in recall(owner, "Quillberry event")
        )
        assert (LEGACY_TIMELINE, "2") in recall(
            owner, "Quillberry event", kinds=["reference"]
        )
        # The 'about' link points at the legacy page record.
        assert sql_rows(
            owner,
            "SELECT b.channel,b.external_id FROM api.links l JOIN api.message_versions a ON a.id=l.from_id JOIN api.message_versions b ON b.id=l.to_id WHERE l.type='about' AND a.channel='gbrain.legacy_timeline'",
        ) == [{"channel": LEGACY, "external_id": "fake-source:legacy-flag"}]
        # The deleted legacy page's entry is filtered and counted.
        legacy = coverage_rows(owner)[LEGACY_TIMELINE]
        assert legacy["status"] == "complete"
        assert legacy["raw_detail"] == {"skipped_deleted": 1}
        assert coverage_rows(owner)[LEGACY]["raw_detail"] == {"skipped_deleted": 1}


def test_link_from_live_page_to_legacy_page_resolves(source_dsn, monkeypatch):
    monkeypatch.setenv("GBRAIN_DSN", source_dsn)
    with_legacy(source_dsn)
    with gbrain_core() as (owner, plugin, _env):
        result = GBrain().execute(plugin, "backfill")
        links = {
            (r["from_channel"], r["from_id"], r["to_channel"], r["to_id"])
            for r in sql_rows(
                owner,
                "SELECT a.channel AS from_channel,a.external_id AS from_id,b.channel AS to_channel,b.external_id AS to_id FROM api.links l JOIN api.message_versions a ON a.id=l.from_id JOIN api.message_versions b ON b.id=l.to_id WHERE l.method='source' AND l.type<>'about'",
            )
        }
        assert (
            PAGES,
            "fake-source:orchard",
            LEGACY,
            "fake-source:legacy-note",
        ) in links
        assert (
            LEGACY,
            "fake-source:legacy-note",
            PAGES,
            "fake-source:orchard",
        ) in links
        assert (
            LEGACY,
            "fake-source:legacy-flag",
            LEGACY,
            "fake-source:legacy-note",
        ) in links
        assert len(links) == 5  # with orchard -> garden from source.sql
        # garden names legacy-note as gbrain.pages:<id>: the citation resolves
        # to the legacy record. A cited page is delivered before the page
        # citing it, so it resolves in the same run.
        assert result["provenance"]["unresolved"] == 0
        garden = message_id(owner, PAGES, "fake-source:garden")
        cited = [{"channel": LEGACY, "external_id": "fake-source:legacy-note"}]
        assert (
            sql_rows(
                owner,
                f"SELECT channel,external_id FROM api.sources('message',{garden})",
            )
            == cited
        )
        # A repeated sync keeps it resolved.
        result = GBrain().execute(plugin, "sync")
        assert result["provenance"]["unresolved"] == 0
        assert (
            sql_rows(
                owner,
                f"SELECT channel,external_id FROM api.sources('message',{garden})",
            )
            == cited
        )


def test_cited_pages_delivered_before_citing_pages():
    pages = {
        1: {"frontmatter": {}, "source_uri": "throughline://gbrain.pages/s:c"},
        2: {"frontmatter": {}, "source_uri": None},
        3: {
            "frontmatter": {
                "throughline_sources": [
                    {"channel": "gbrain.pages", "external_id": "s:b"}
                ]
            },
            "source_uri": None,
        },
        # A cycle (4 <-> 5) is broken without looping.
        4: {"frontmatter": {}, "source_uri": "throughline://gbrain.legacy/s:e"},
        5: {"frontmatter": {}, "source_uri": "throughline://gbrain.pages/s:d"},
    }
    ids = {1: "s:a", 2: "s:b", 3: "s:c", 4: "s:d", 5: "s:e"}
    # An earlier revision of 2 cites 4: revisions count too.
    versions = [
        {
            "page_id": 2,
            "frontmatter": {
                "throughline_sources": [
                    {"channel": "gbrain.pages", "external_id": "s:d"}
                ]
            },
        }
    ]
    order = cited_first(pages, ids, versions)
    assert sorted(order) == [1, 2, 3, 4, 5]
    assert order.index(2) < order.index(3) < order.index(1)
    assert order.index(5) < order.index(4) < order.index(2)


def test_verify_counts_per_channel_incl_legacy(source_dsn, monkeypatch):
    monkeypatch.setenv("GBRAIN_DSN", source_dsn)
    with_legacy(source_dsn)
    with gbrain_core() as (owner, plugin, _env):
        # Imported before the legacy channel existed: everything in gbrain.pages.
        monkeypatch.setenv("GBRAIN_LEGACY_CHANNEL", "false")
        GBrain().execute(plugin, "backfill")
        pairs = GBrain().verify(owner)
        assert all(a == b for a, b in pairs)
        assert pairs.report["channels"][PAGES] == {"expected": 5, "actual": 5}
        assert pairs.report["channels"][LEGACY] == {"expected": 0, "actual": 0}
        # The legacy channel on: marked pages are imported into gbrain.legacy;
        # the earlier gbrain.pages records are never moved or deleted, and
        # verify shows the difference per channel.
        monkeypatch.setenv("GBRAIN_LEGACY_CHANNEL", "true")
        GBrain().execute(plugin, "sync")
        pairs = GBrain().verify(owner)
        assert pairs.report["channels"] == {
            PAGES: {"expected": 3, "actual": 5},
            TIMELINE: {"expected": 1, "actual": 2},
            LEGACY: {"expected": 2, "actual": 2},
            LEGACY_TIMELINE: {"expected": 1, "actual": 1},
        }
        assert any(a != b for a, b in pairs)


def test_verify_matches_with_legacy_channels(source_dsn, monkeypatch):
    monkeypatch.setenv("GBRAIN_DSN", source_dsn)
    with_legacy(source_dsn)
    with gbrain_core() as (owner, plugin, _env):
        GBrain().execute(plugin, "backfill")
        pairs = GBrain().verify(owner)
        assert all(a == b for a, b in pairs)
        assert pairs[0] == (5, 5)
        assert pairs.report["channels"] == {
            PAGES: {"expected": 3, "actual": 3},
            TIMELINE: {"expected": 1, "actual": 1},
            LEGACY: {"expected": 2, "actual": 2},
            LEGACY_TIMELINE: {"expected": 1, "actual": 1},
        }


def retire_type(owner, name):
    owner.request("POST", "link-types", {"type": name, "description": "Invented"})
    owner.request("POST", f"link-types/{name}/retire", {})


def test_link_types_raw_kept_in_evidence_and_batch_never_fails_on_type(
    source_dsn, monkeypatch
):
    monkeypatch.setenv("GBRAIN_DSN", source_dsn)
    with_legacy(source_dsn)
    with gbrain_core() as (owner, plugin, _env):
        retire_type(owner, "invented_retired_kind")
        GBrain().execute(plugin, "backfill")
        rows = sql_rows(
            owner,
            "SELECT type,type_as_written,evidence FROM api.links WHERE method='source' AND type<>'about' ORDER BY id",
        )
        raw = [r["evidence"]["source_record"]["link_type"] for r in rows]
        # orchard -> garden (source.sql), then the legacy fixture's links.
        assert raw == ["mentions", "mentions", "Mention", "→", "invented_retired_kind"]
        assert [r["type"] for r in rows] == [
            "mentions",
            "mentions",
            "mentions",
            normalize_type("→"),
            "invented_retired_kind",
        ]
        assert rows[2]["type_as_written"] == "Mention"
        assert rows[3]["type_as_written"] == "→"
        candidates = owner.request(
            "GET", "link-types", params={"status": "candidate", "with_counts": "true"}
        )["rows"]
        assert normalize_type("→") in {r["type"] for r in candidates}
        retired = owner.request(
            "GET", "link-types", params={"status": "retired", "with_counts": "true"}
        )["rows"]
        assert next(r for r in retired if r["type"] == "invented_retired_kind")[
            "retired_but_written"
        ]
        # A second run sends the same types again without failing.
        GBrain().execute(plugin, "sync")


def test_report_orphans_candidate_link_types_oversized_with_passage_counts(
    source_dsn, monkeypatch
):
    monkeypatch.setenv("GBRAIN_DSN", source_dsn)
    monkeypatch.setenv("GBRAIN_OVERSIZE_CHARS", "40")
    with_legacy(source_dsn)
    with gbrain_core() as (owner, plugin, env):
        retire_type(owner, "invented_retired_kind")
        owner.request(
            "POST",
            "identity/people",
            {"handle": "invented-someone", "display_name": "Invented Someone"},
        )
        GBrain().execute(plugin, "backfill")
        # Passages are split by the core's passage build (worker or CLI).
        subprocess.run(
            [sys.executable, "-m", "throughline", "lib", "passages", "build"],
            env=env,
            check=True,
            capture_output=True,
        )
        report = GBrain().report(owner)
        assert report["missing"] == {}
        assert report["orphan_pages"] == {
            "count": 1,
            "pages": [
                {
                    "external_id": "fake-source:invented-person",
                    "channel": PAGES,
                    "title": "Invented Person",
                }
            ],
        }
        types = report["link_types"]
        assert [(e["type"], e["raw_types"]) for e in types["active"]] == [
            ("mentions", ["mentions"])
        ]
        assert [(e["type"], e["raw_types"]) for e in types["folded"]] == [
            ("mentions", ["Mention"])
        ]
        assert [e["type"] for e in types["candidate"]] == [normalize_type("→")]
        assert [e["type"] for e in types["retired_but_written"]] == [
            "invented_retired_kind"
        ]
        assert types["distinct_source_types"] == 4
        assert types["distinct_types_after_registry"] == 3
        assert types["active"][0]["stored_active_links"] == 3
        oversized = {p["external_id"]: p for p in report["oversized_pages"]["pages"]}
        assert report["oversized_pages"]["threshold_chars"] == 40
        # Live pages have passages; legacy pages are reference text without.
        person = oversized["fake-source:invented-person"]
        assert person["characters"] > 40 and person["passages"] >= 1
        assert oversized["fake-source:legacy-note"]["passages"] == 0
        assert "fake-source:garden" not in oversized
        assert report["person_pages"] == {
            "pages": 1,
            "registry_people": 1,
            "without_registry_person": ["fake-source:invented-person"],
        }
        # The report reads and never writes the source.
        with psycopg.connect(source_dsn) as conn:
            assert conn.execute("SELECT count(*) FROM pages").fetchone()[0] == 7
