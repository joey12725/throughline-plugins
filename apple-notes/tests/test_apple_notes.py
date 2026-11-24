import sys
from pathlib import Path

import pytest
from throughline_sdk import Checkpoint, Record
from throughline_sdk.contract import check_plugin

sys.path.insert(0, str(Path(__file__).parent))

from apple_notes_fake import body, standard  # noqa: E402

from throughline_apple_notes import NOTES, AppleNotes  # noqa: E402
from throughline_apple_notes.proto import DecodeError, note_proto, render  # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "NoteStore.sqlite"
    fake = standard(path)
    monkeypatch.setenv("APPLE_NOTES_DB", str(path))
    monkeypatch.setenv("APPLE_NOTES_CHECKPOINT_EVERY", "1")
    return fake


def run(cursor=None, now=None):
    plugin = AppleNotes()
    plugin.now = now
    items = list(plugin.records(cursor or {}, "sync"))
    rows = [i for i in items if isinstance(i, dict)]
    records = [i for i in items if isinstance(i, Record)]
    checkpoints = [i.cursor for i in items if isinstance(i, Checkpoint)]
    return rows, records, checkpoints


def test_notes_mapping(store):
    rows, records, checkpoints = run()
    by_id = {r["external_id"]: r for r in rows}
    assert list(by_id) == ["NOTE-0001", "NOTE-0002", "NOTE-0004", "NOTE-0005"]  # locked and deleted skipped
    grocery = by_id["NOTE-0001"]
    assert grocery["channel"] == NOTES
    assert grocery["content"] == "Invented Grocery List\n\n- [x] Apples\n- [ ] Invented pears 🍐\nSee example.com"
    assert grocery["fields"]["checklist_total"] == 2 and grocery["fields"]["checklist_done"] == 1
    assert grocery["fields"]["links"] == ["https://example.com/list"]
    assert grocery["fields"]["is_pinned"] is True
    assert grocery["fields"]["account"] == "iCloud"
    assert grocery["happened_at"] == "2024-12-24T00:28:20+00:00"
    soup = by_id["NOTE-0002"]
    assert soup["content"].split("\n\n", 1)[1] == (
        "## Ingredients\n- Invented carrots\n  - Invented leeks\n1. Step one\n2. Step two\nPhoto: [image]"
    )
    assert soup["fields"]["folder_path"] == "Notes/Invented Recipes"
    assert soup["fields"]["attachments"] == ["public.jpeg"]
    assert by_id["NOTE-0004"]["fields"]["in_trash"] is True
    links = [r.data for r in records if r.kind == "link"]
    assert {link["to"]["external_ref"] for link in links} == {
        "apple_notes:folder:FOLDER-NOTES-0000",
        "apple_notes:folder:FOLDER-RECIPES-0000",
        "apple_notes:folder:FOLDER-TRASH-0000",
    }
    detail = next(r.data["detail"] for r in records if r.kind == "coverage")
    assert detail["locked_skipped"] == 1
    assert len(checkpoints) == 5


def test_incremental_edit_and_purge(store):
    _, _, checkpoints = run()
    cursor = checkpoints[-1]
    assert run(cursor)[0] == []
    store.update("NOTE-0005", [("Invented Book Notes", {}), ("Chapter two", {})], 1_735_100_000)
    store.purge("NOTE-0001")
    rows, _, _ = run(cursor, now="2025-01-01T00:00:00+00:00")
    by_id = {r["external_id"]: r for r in rows}
    assert by_id["NOTE-0005"]["content"].endswith("Chapter two")
    assert by_id["NOTE-0001"]["fields"]["deleted"] is True
    assert by_id["NOTE-0001"]["thread"]["title"] == "Invented Grocery List"


def test_exclude_trash(store, monkeypatch):
    monkeypatch.setenv("APPLE_NOTES_INCLUDE_TRASH", "false")
    rows, _, _ = run()
    assert "NOTE-0004" not in {r["external_id"] for r in rows}


def test_missing_db_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("APPLE_NOTES_DB", str(tmp_path / "missing.sqlite"))
    with pytest.raises(FileNotFoundError):
        list(AppleNotes().records({}, "sync"))


def test_proto_decoder_edges():
    text, runs = note_proto(body([("Title", {"style": 0}), ("x", {})]))
    assert text == "Title\nx" and runs[0]["style"] == 0
    assert render(text, runs)[0] == "# Title\nx"
    with pytest.raises(DecodeError):
        note_proto(b"\x12\x05ab")  # truncated length-delimited field
    assert render("plain", [])[0] == "plain"


def test_contract_kit(tmp_path):
    path = tmp_path / "contract" / "NoteStore.sqlite"
    path.parent.mkdir()
    standard(path)
    result = check_plugin(
        "throughline_apple_notes:AppleNotes",
        {"APPLE_NOTES_DB": str(path), "APPLE_NOTES_CHECKPOINT_EVERY": "1"},
    )
    assert result["messages"] == 4
