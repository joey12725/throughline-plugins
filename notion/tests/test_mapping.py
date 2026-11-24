import json
import subprocess
import sys

import pytest
from throughline_notion import DATABASES, PAGES, Notion
from throughline_sdk import Checkpoint, Record
from throughline_sdk.contract import check_plugin


def plugin(monkeypatch, recording, **settings):
    monkeypatch.setenv("NOTION_SOURCE", f"export:{recording}")
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    return Notion()


def messages(items):
    return [item for item in items if isinstance(item, dict)]


def test_manifest_and_mapping(monkeypatch, recording):
    manifest = Notion.manifest
    assert manifest.name == "notion" and manifest.default_interval_s == 3600
    assert {channel.name for channel in manifest.channels} == {PAGES, DATABASES}
    assert all(channel.record_kind == "page_revision" for channel in manifest.channels)
    items = list(plugin(monkeypatch, recording, NOTION_CHECKPOINT_EVERY="1").records({}, "backfill"))
    rows = messages(items)
    assert [(row["channel"], row["external_id"]) for row in rows] == [
        (DATABASES, "db-projects"),
        (PAGES, "page-beta"),
        (PAGES, "page-alpha"),
        (PAGES, "page-gone"),
    ]
    alpha = rows[2]
    assert alpha["thread"] == {"external_id": "page-alpha", "title": "Invented Alpha"}
    assert alpha["author"] == {
        "system": "notion",
        "kind": "account",
        "value": "user-sam",
        "display_name": "Sam Sample",
    }
    assert alpha["fields"]["database_id"] == "db-projects"
    assert alpha["fields"]["properties_text"] == "Name: Invented Alpha\nStatus: Done"
    assert "Revised invented outline" in alpha["content"]
    missing = rows[-1]
    assert missing["fields"]["missing"] is True
    assert "may be deleted, trashed, or no longer shared" in missing["content"]
    links = [item for item in items if isinstance(item, Record) and item.kind == "link"]
    assert len(links) == 1
    assert {link.data["to"]["external_id"] for link in links} == {"db-projects"}
    checkpoints = [item for item in items if isinstance(item, Checkpoint)]
    assert len(checkpoints) >= 4


def test_export_replay_and_resume(monkeypatch, recording):
    first = list(plugin(monkeypatch, recording, NOTION_CHECKPOINT_EVERY="2").records({}, "backfill"))
    cursor = [item.cursor for item in first if isinstance(item, Checkpoint)][-1]
    assert messages(Notion().records(cursor, "sync")) == []
    interim = next(item.cursor for item in first if isinstance(item, Checkpoint) and item.cursor["export"].get("pending"))
    resumed = list(Notion().records(interim, "backfill"))
    assert [row["external_id"] for row in messages(resumed)] == ["page-alpha", "page-gone"]


def test_source_validation(monkeypatch):
    monkeypatch.setenv("NOTION_SOURCE", "notion:invalid")
    with pytest.raises(ValueError, match="NOTION_SOURCE"):
        list(Notion().records({}, "backfill"))


def test_manifest_command():
    output = subprocess.check_output(
        [sys.executable, "-c", "from throughline_notion import main; main()", "manifest"],
        text=True,
    )
    assert {channel["name"] for channel in json.loads(output)["channels"]} == {PAGES, DATABASES}


def test_contract_kit(recording):
    result = check_plugin(
        "throughline_notion:Notion",
        {
            "NOTION_SOURCE": f"export:{recording}",
            "NOTION_CHECKPOINT_EVERY": "1",
            "COMPOSIO_MCP_TOKEN": "invented-secret-canary",
        },
        secrets=["invented-secret-canary"],
    )
    assert result["messages"] == 4
