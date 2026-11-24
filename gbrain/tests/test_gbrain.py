import copy
from pathlib import Path

import psycopg
import pytest
from throughline_sdk import Client
from throughline_sdk._server import core_server
from throughline_sdk.contract import check_plugin
from throughline_gbrain import GBrain


def test_plg_6_contract(source_dsn):
    # The recorded fixture must cover every declared channel, so the invented
    # legacy bulk pages (gbrain.legacy, gbrain.legacy_timeline) are loaded too.
    with psycopg.connect(source_dsn) as conn:
        conn.execute(Path(__file__).with_name("legacy.sql").read_text(encoding="utf-8"))
    result = check_plugin(
        "throughline_gbrain:GBrain", {"GBRAIN_DSN": source_dsn}, secrets=[source_dsn]
    )
    # orchard, garden, invented-person, legacy-note, legacy-flag and one
    # timeline entry per channel (deleted pages stay skipped).
    assert result["messages"] == 7


def test_brn_4_msg_2_history_resume_and_counts(source_dsn, monkeypatch):
    monkeypatch.setenv("GBRAIN_DSN", source_dsn)
    with psycopg.connect(source_dsn) as conn:
        conn.execute(
            "INSERT INTO page_versions VALUES (1,1,'Seedling.','{}','2019-01-01Z'), (2,1,'Sapling.','{}','2019-02-01Z'), (3,1,'Sapling.','{}','2019-03-01Z')"
        )
    with core_server() as (http, env):
        client = Client(env["THROUGHLINE_URL"], env["THROUGHLINE_TOKEN"], "gbrain")
        try:
            real_checkpoint = client.checkpoint
            calls = 0

            def interrupt(cursor, key="default"):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RuntimeError(
                        "invented interruption after acknowledged version"
                    )
                return real_checkpoint(cursor, key)

            client.checkpoint = interrupt
            with pytest.raises(RuntimeError):
                GBrain().execute(client, "backfill")
            client.checkpoint = real_checkpoint
            GBrain().execute(client, "backfill")
            statement = "SELECT version,content FROM api.message_versions WHERE channel='gbrain.pages' AND external_id='fake-source:orchard' ORDER BY version"
            before = client.request("POST", "sql", {"statement": statement})["rows"]
            assert len(before) == 3
            assert "Seedling" in before[0]["content"]
            assert "Sapling" in before[1]["content"]
            assert "Apple trees" in before[2]["content"]
            GBrain().execute(client, "sync")
            client.checkpoint({})
            GBrain().execute(client, "backfill")
            assert (
                client.request("POST", "sql", {"statement": statement})["rows"]
                == before
            )
            assert GBrain().verify(client)[:2] == ((2, 2), (2, 2))
            assert all(a == b for a, b in GBrain().verify(client))
            rows = http.post(
                "sql",
                json={
                    "statement": "SELECT lat,lon,is_reference FROM api.messages WHERE channel='gbrain.pages'"
                },
            ).json()["rows"]
            assert all(
                r["lat"] is None and r["lon"] is None and r["is_reference"]
                for r in rows
            )
            monkeypatch.setenv("GBRAIN_INCLUDE_DELETED", "true")
            GBrain().execute(client, "sync")
            assert GBrain().verify(client)[:2] == ((3, 3), (2, 2))
            assert all(a == b for a, b in GBrain().verify(client))
        finally:
            client.close()


def test_msg_6_fields_and_timeline(source_dsn, monkeypatch):
    monkeypatch.setenv("GBRAIN_DSN", source_dsn)
    plugin = GBrain()
    plugin.dry_run = True
    records = list(plugin.records({}, "backfill"))
    pages = [
        r for r in records if isinstance(r, dict) and r["channel"] == "gbrain.pages"
    ]
    page = copy.deepcopy(pages[0])
    assert page["fields"]["aliases"] == ["fruit trees", "old-orchard"]
    assert page["fields"]["tags"] == ["invented"]
    assert page["payload"]["page"]["id"] == 1
    assert page["happened_at"].startswith("2021-01-01")
    assert page["subject"] == {
        "start": "2020-01-01T00:00:00+00:00",
        "start_precision": "day",
    }
    assert "A tree was planted" in page["content"]
    assert any(
        getattr(r, "kind", None) == "link" and r.data["type"] == "about"
        for r in records
    )
