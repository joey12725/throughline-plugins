import os
import shutil
from pathlib import Path

import pytest
from throughline_sdk import Checkpoint, Record
from throughline_sdk.contract import check_plugin

from throughline_markdown_notes import MarkdownNotes
from throughline_markdown_notes.parse import parse_note
from throughline_markdown_notes.vault import Root, daily_date, moment_regex, pack, unpack

RECORDED = Path(__file__).parent / "recorded"
BASE_NS = 1_741_000_000 * 10**9  # 2025-03-03, a historical mtime


@pytest.fixture
def vaults(tmp_path):
    root = tmp_path / "vaults"
    shutil.copytree(RECORDED, root)
    for number, path in enumerate(sorted(p for p in root.rglob("*") if p.is_file())):
        stamp = BASE_NS + number * 3600 * 10**9
        os.utime(path, ns=(stamp, stamp))
    return root


def roots(vaults):
    return f"vault={vaults / 'obsidian'},graph={vaults / 'logseq'},plain={vaults / 'plain'}"


@pytest.fixture
def env(monkeypatch, vaults):
    monkeypatch.setenv("MARKDOWN_NOTES_ROOTS", roots(vaults))
    monkeypatch.setenv("MARKDOWN_NOTES_TZ", "UTC")
    monkeypatch.setenv("MARKDOWN_NOTES_CHECKPOINT_EVERY", "2")
    return monkeypatch


def run(cursor=None, now=None):
    plugin = MarkdownNotes()
    plugin.now = now
    items = list(plugin.records(cursor or {}, "sync"))
    rows = [i for i in items if isinstance(i, dict)]
    links = [i.data for i in items if isinstance(i, Record) and i.kind == "link"]
    checkpoints = [i.cursor for i in items if isinstance(i, Checkpoint)]
    return rows, links, checkpoints, items


def test_notes_mapping(env):
    rows, links, checkpoints, _ = run()
    by_id = {r["external_id"]: r for r in rows}
    assert set(by_id) == {
        "vault:Daily/2025-03-14.md",
        "vault:Projects/Orchard Plan.md",
        "vault:Seed List.md",
        "graph:journals/2025_03_15.md",
        "graph:pages/Garden Ideas.md",
        "graph:pages/projects___greenhouse.md",
        "plain:readme.md",
        "plain:sub/child.md",
    }  # .obsidian, .trash, logseq/bak and .txt are ignored
    daily = by_id["vault:Daily/2025-03-14.md"]
    assert daily["fields"]["is_daily"] is True
    assert daily["subject"] == {
        "start": "2025-03-14T00:00:00+00:00",
        "end": "2025-03-14T00:00:00+00:00",
        "start_precision": "day",
        "end_precision": "day",
    }
    assert daily["fields"]["tags"] == ["journal", "invented", "focused"]
    assert daily["fields"]["vault_format"] == "obsidian"
    assert daily["happened_at"].endswith("+00:00")
    plan = by_id["vault:Projects/Orchard Plan.md"]
    assert plan["fields"]["title"] == "Invented Orchard Plan"
    assert plan["fields"]["aliases"] == ["Orchard"]
    assert plan["fields"]["tags"] == ["project/orchard", "active"]
    assert plan["fields"]["created"] == "2025-01-02T00:00:00+00:00"
    assert plan["subject"] == {"start": "2025-04-01T00:00:00+00:00", "start_precision": "day"}
    assert "Not A Link" not in plan["fields"]["link_targets"]
    assert plan["content"].startswith("Invented Orchard Plan\n\nPlant invented")
    assert plan["payload"]["content"]["title"] == "Invented Orchard Plan"
    journal = by_id["graph:journals/2025_03_15.md"]
    assert journal["fields"]["is_daily"] and journal["fields"]["daily_date"] == "2025-03-15"
    assert journal["fields"]["tags"] == ["weekend plans"]
    garden = by_id["graph:pages/Garden Ideas.md"]
    assert garden["fields"]["aliases"] == ["Garden"]
    assert garden["fields"]["tags"] == ["ideas", "outdoors"]
    assert by_id["graph:pages/projects___greenhouse.md"]["fields"]["title"] == "projects/greenhouse"
    child = by_id["plain:sub/child.md"]
    assert child["fields"]["frontmatter_error"] is True and child["fields"]["vault_format"] == "markdown"
    assert all(r["fields"]["author_kind"] == "owner" for r in rows)

    note_links = {(link["from"]["external_id"], link["to"]["external_id"], link["evidence"]["via"])
                  for link in links if link["type"] == "links"}
    assert note_links == {
        ("vault:Daily/2025-03-14.md", "vault:Projects/Orchard Plan.md", "wikilink"),
        ("vault:Daily/2025-03-14.md", "vault:Seed List.md", "embed"),
        ("vault:Projects/Orchard Plan.md", "vault:Seed List.md", "markdown"),
        ("vault:Projects/Orchard Plan.md", "vault:Daily/2025-03-14.md", "wikilink"),
        ("graph:journals/2025_03_15.md", "graph:pages/Garden Ideas.md", "wikilink"),
        ("graph:pages/Garden Ideas.md", "graph:pages/projects___greenhouse.md", "wikilink"),
        ("plain:readme.md", "plain:sub/child.md", "markdown"),
    }
    tags = {link["to"]["external_ref"] for link in links if link["type"] == "about"}
    assert {"tag:focused", "tag:weekend plans", "tag:project/orchard"} <= tags
    assert len(checkpoints) >= 4
    assert set(checkpoints[-1]) == {"v", "roots"}


def test_unchanged_touch_edit_delete_rename(env, vaults):
    _, _, checkpoints, _ = run()
    cursor = checkpoints[-1]
    assert run(cursor)[0] == []  # nothing changed
    seed = vaults / "obsidian" / "Seed List.md"
    os.utime(seed, ns=(BASE_NS + 10**12, BASE_NS + 10**12))  # touched, same bytes
    rows, _, checkpoints, _ = run(cursor)
    assert rows == []
    cursor = checkpoints[-1]
    plan = vaults / "obsidian" / "Projects" / "Orchard Plan.md"
    plan.write_text(plan.read_text() + "\nInvented new paragraph.\n")
    os.utime(plan, ns=(BASE_NS + 2 * 10**12,) * 2)
    seed.rename(vaults / "obsidian" / "Seeds.md")
    rows, links, checkpoints, _ = run(cursor, now="2025-06-01T00:00:00+00:00")
    by_id = {r["external_id"]: r for r in rows}
    assert "Invented new paragraph" in by_id["vault:Projects/Orchard Plan.md"]["content"]
    gone = by_id["vault:Seed List.md"]
    assert gone["fields"]["deleted"] is True
    assert gone["fields"]["renamed_to"] == "vault:Seeds.md"
    assert gone["happened_at"] == "2025-06-01T00:00:00+00:00"
    assert "vault:Seeds.md" in by_id
    assert all(link["to"].get("external_id") != "vault:Seed List.md" for link in links)


def test_crash_before_links_replays_links(env):
    _, _, checkpoints, items = run()
    # The checkpoint before the link phase of the first root still lists
    # pending notes; resuming from it must re-send their links.
    interim = next(c for c in checkpoints if MarkdownNotes.load(c).get("vault", {}).get("p"))
    rows, links, _, _ = run(interim)
    assert any(link["type"] == "links" and link["from"]["external_id"].startswith("vault:") for link in links)


def test_empty_root_records_no_deletions(env, vaults, tmp_path):
    _, _, checkpoints, _ = run()
    shutil.rmtree(vaults / "plain")
    (vaults / "plain").mkdir()
    rows, _, _, items = run(checkpoints[-1])
    assert rows == []
    partial = [i.data for i in items if isinstance(i, Record) and i.kind == "coverage" and i.data["status"] == "partial"]
    assert partial and partial[0]["scope_key"] == "plain"


def test_all_roots_missing_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("MARKDOWN_NOTES_ROOTS", str(tmp_path / "nowhere"))
    with pytest.raises(RuntimeError):
        list(MarkdownNotes().records({}, "sync"))


def test_large_file_skipped(env, vaults):
    env.setenv("MARKDOWN_NOTES_MAX_BYTES", "60")
    rows, _, _, items = run()
    assert "vault:Projects/Orchard Plan.md" not in {r["external_id"] for r in rows}
    detail = [i.data["detail"] for i in items if isinstance(i, Record) and i.kind == "coverage"]
    assert sum(d["skipped_large"] for d in detail) >= 1


def test_parse_edge_cases():
    note = parse_note("---\ntags: a b\naliases: Solo\n---\nText #tag-one, #123 `#code` http://x.example.com/#frag [[A#Head|alias]] [[B^block]]")
    assert note["tags"] == ["a", "b", "tag-one"]
    assert note["aliases"] == ["Solo"]
    assert [t for t, _ in note["links"]] == ["A", "B"]
    assert parse_note("no frontmatter")["frontmatter"] == {}


def test_daily_formats(tmp_path):
    root = Root("x", tmp_path)
    root.daily = [("Journal", moment_regex("YYYY/MM/YYYY-MM-DD dddd")), ("", moment_regex("MMMM Do, YYYY"))]
    assert str(daily_date(root, "Journal/2024/02/2024-02-29 Thursday.md")) == "2024-02-29"
    assert str(daily_date(root, "March 3rd, 2024.md")) == "2024-03-03"
    assert daily_date(root, "Journal/2024/02/2024-03-29 Friday.md") is None  # month mismatch
    assert str(daily_date(root, "anything/2023_12_31.md")) == "2023-12-31"


def test_pack_roundtrip():
    value = {"f": {f"n{i}.md": [i, i, "abc"] for i in range(100)}}
    assert unpack(pack(value)) == value


def test_contract_kit(vaults):
    result = check_plugin(
        "throughline_markdown_notes:MarkdownNotes",
        {"MARKDOWN_NOTES_ROOTS": roots(vaults), "MARKDOWN_NOTES_TZ": "UTC", "MARKDOWN_NOTES_CHECKPOINT_EVERY": "1"},
    )
    assert result["messages"] == 8
