import sqlite3
import sys
from pathlib import Path

import pytest
from throughline_sdk import Checkpoint
from throughline_sdk.contract import check_plugin

sys.path.insert(0, str(Path(__file__).parent))

import cursor_fake  # noqa: E402

from throughline_cursor import Cursor  # noqa: E402


@pytest.fixture
def user_dir(tmp_path):
    return cursor_fake.build(tmp_path)


def plugin(monkeypatch, user_dir, **settings):
    monkeypatch.setenv("CURSOR_USER_DIR", str(user_dir))
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    return Cursor()


def run_once(p, cursor=None):
    items = list(p.records(cursor or {}, "sync"))
    messages = [i for i in items if isinstance(i, dict)]
    checkpoints = [i for i in items if isinstance(i, Checkpoint)]
    return messages, checkpoints


def test_cursor_maps_composers_bubbles_and_legacy_stores(monkeypatch, user_dir):
    p = plugin(monkeypatch, user_dir)
    messages, checkpoints = run_once(p)
    by_id = {m["external_id"]: m for m in messages}
    first = by_id["composer:c0000000-0000-4000-8000-000000000000:c0000000-0000-4000-8000-000000000000-b0"]
    assert first["content"] == "Invented message 0 of composer 0"
    assert first["author"] == {"system": "cursor", "kind": "owner", "value": "owner", "display_name": None}
    assert first["fields"]["author_kind"] == "owner"
    assert first["fields"]["workspace"] == "/home/invented/projects/orchard app"
    assert first["happened_at"] == "2024-05-01T10:00:00.000+00:00"
    reply = by_id["composer:c0000000-0000-4000-8000-000000000000:c0000000-0000-4000-8000-000000000000-b1"]
    assert reply["fields"]["author_kind"] == "agent"
    assert reply["fields"]["model"] == "invented-model-large"
    assert reply["author"]["value"] == "invented-model-large"
    assert reply["fields"]["output_tokens"] == 21
    assert reply["thread"]["external_id"] == "composer:c0000000-0000-4000-8000-000000000000"
    assert reply["thread"]["title"] == "Invented composer 0"
    rich = [m for m in messages if m["fields"]["chat_id"].endswith("aa")]
    assert [m["content"] for m in rich] == ["Invented rich prompt\nsecond line", "Invented rich reply"]
    assert rich[0]["fields"]["time_estimated"] is True and rich[1]["fields"]["time_estimated"] is False
    inline = [m for m in messages if m["fields"]["source_format"] == "composer_inline_v2"]
    assert [m["fields"]["role"] for m in inline] == ["user", "assistant"]
    assert inline[0]["fields"]["workspace"] == "/home/invented/projects/orchard app"
    assert inline[1]["happened_at"] > inline[0]["happened_at"]
    legacy = [m for m in messages if m["fields"]["source_format"] == "legacy_chat"]
    assert [m["content"] for m in legacy] == [
        "Invented legacy question",
        "Invented legacy answer",
        "Invented follow-up",
    ]
    assert legacy[-1]["happened_at"] == "2024-04-30T10:00:00.000+00:00"
    assert legacy[1]["fields"]["model"] == "invented-legacy-model"
    assert p.stats["unknown_shapes"]["unknown_composer_shape"] == 1
    assert p.stats["unknown_shapes"]["unparseable_composer"] == 1
    assert p.stats["unknown_shapes"]["unknown_legacy_tab"] == 1
    assert p.stats["unknown_shapes"]["unknown_legacy_bubble_type"] == 1
    skipped = p.stats["skipped"]
    assert skipped["tool_call"] == 1 and skipped["thinking"] == 1 and skipped["missing_bubble"] == 1
    assert skipped["untimed_prompt"] == 2 and skipped["empty_conversation"] == 1
    assert len(messages) == 4 * 4 + 2 + 2 + 3
    assert checkpoints[-1].cursor["files"]
    for m in messages:
        assert m["happened_at"].endswith("+00:00")


def test_cursor_unchanged_stores_and_conversations_are_not_resent(monkeypatch, user_dir):
    p = plugin(monkeypatch, user_dir)
    _, checkpoints = run_once(p)
    again, _ = run_once(p, checkpoints[-1].cursor)
    assert again == [] and p.stats["unchanged_stores"]
    # A new reply in one composer: only that conversation is re-sent.
    db = user_dir / "globalStorage" / "state.vscdb"
    cid = "c0000000-0000-4000-8000-000000000001"
    rows = cursor_fake.v3_composer(
        cid,
        "Invented composer 1",
        cursor_fake.T0 + 86400000,
        [("user" if i % 2 == 0 else "assistant", f"Invented message {i} of composer 1") for i in range(5)],
    )
    cursor_fake.kv(db, "cursorDiskKV", rows)
    changed, _ = run_once(p, checkpoints[-1].cursor)
    assert {m["fields"]["chat_id"] for m in changed} == {cid}
    assert len(changed) == 5


def test_cursor_checkpoints_between_conversations(monkeypatch, user_dir):
    p = plugin(monkeypatch, user_dir, CURSOR_BATCH_SIZE="3")
    items = list(p.records({}, "backfill"))
    assert sum(isinstance(i, Checkpoint) for i in items) >= 5
    # Resuming from a mid-run checkpoint skips conversations already done.
    index = next(i for i, item in enumerate(items) if isinstance(item, Checkpoint))
    before = [i for i in items[:index] if isinstance(i, dict)]
    rest, _ = run_once(p, items[index].cursor)
    assert {m["external_id"] for m in rest}.isdisjoint({m["external_id"] for m in before})
    assert len(rest) + len(before) == len([i for i in items if isinstance(i, dict)])


def test_cursor_reads_a_copy_with_wal(monkeypatch, tmp_path):
    user = tmp_path / "User"
    db = user / "globalStorage" / "state.vscdb"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT UNIQUE, value BLOB)")
    import json

    for key, value in cursor_fake.v3_composer(
        "c0000000-0000-4000-8000-0000000000ff", "Invented WAL composer", cursor_fake.T0,
        [("user", "Invented WAL question"), ("assistant", "Invented WAL answer")],
    ).items():
        conn.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", (key, json.dumps(value)))
    conn.commit()  # Left in the WAL: the connection stays open.
    assert Path(str(db) + "-wal").stat().st_size > 0
    messages, _ = run_once(plugin(monkeypatch, user))
    conn.close()
    assert [m["content"] for m in messages] == ["Invented WAL question", "Invented WAL answer"]
    assert not list(db.parent.glob("*.tmp"))


def test_cursor_missing_directory_fails(monkeypatch, tmp_path):
    p = plugin(monkeypatch, tmp_path / "nowhere")
    with pytest.raises(FileNotFoundError):
        run_once(p)


def test_cursor_owner_identifier(monkeypatch, user_dir):
    p = plugin(
        monkeypatch, user_dir,
        CURSOR_OWNER_IDENTIFIER="email:address:owner@example.com", CURSOR_OWNER_NAME="Invented Owner",
    )
    messages, _ = run_once(p)
    owner = next(m for m in messages if m["fields"]["role"] == "user")
    assert owner["author"] == {
        "system": "email", "kind": "address", "value": "owner@example.com", "display_name": "Invented Owner"
    }
    monkeypatch.setenv("CURSOR_OWNER_IDENTIFIER", "bad")
    with pytest.raises(ValueError):
        Cursor()


def test_cursor_contract(tmp_path):
    user = cursor_fake.build(tmp_path, conversations=6, turns=4)
    result = check_plugin(
        "throughline_cursor:Cursor",
        {"CURSOR_USER_DIR": str(user), "CURSOR_BATCH_SIZE": "4"},
    )
    assert result["messages"] == 6 * 4 + 2 + 2 + 3
