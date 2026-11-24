import json
import os
import shutil
from pathlib import Path

import pytest
from throughline_sdk import Checkpoint
from throughline_sdk.contract import check_plugin

from throughline_lmstudio import LMStudio

RECORDED = Path(__file__).parent / "recorded" / "conversations"
SOUP = "1714557600000.conversation.json"


@pytest.fixture
def folder(tmp_path):
    target = tmp_path / "conversations"
    shutil.copytree(RECORDED, target)
    return target


def plugin(monkeypatch, folder, **settings):
    monkeypatch.setenv("LMSTUDIO_CONVERSATIONS_DIRS", str(folder))
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    return LMStudio()


def collect(p, cursor=None):
    items = list(p.records(cursor or {}, "sync"))
    return [i for i in items if isinstance(i, dict)], [i for i in items if isinstance(i, Checkpoint)]


def ids(messages):
    return {m["external_id"]: m for m in messages}


def test_lmstudio_maps_turns_versions_models_and_times(monkeypatch, folder):
    p = plugin(monkeypatch, folder)
    messages, _ = collect(p)
    found = ids(messages)
    question = found["lmstudio:1714557600000:0.0"]
    assert question["content"] == "Invented question about soup"
    assert question["happened_at"] == "2024-05-01T10:00:00+00:00"
    assert question["fields"]["author_kind"] == "owner" and question["fields"]["time_source"] == "created"
    answer = found["lmstudio:1714557600000:1.0"]
    assert answer["content"] == "Invented soup answer"  # The thinking block is left out.
    assert answer["fields"]["has_reasoning"] is True
    assert answer["fields"]["model"] == "invented-org/invented-7b-gguf"
    assert answer["author"] == {"system": "lmstudio", "kind": "agent", "value": "invented-org/invented-7b-gguf",
                                "display_name": "invented-org/invented-7b-gguf"}
    assert answer["fields"]["predicted_tokens"] == 42 and answer["fields"]["tokens_per_second"] == 33.5
    assert answer["happened_at"] == "2024-05-01T10:00:01+00:00"
    one, two = found["lmstudio:1714557600000:3.0"], found["lmstudio:1714557600000:3.1"]
    assert (one["fields"]["is_selected"], two["fields"]["is_selected"]) == (False, True)
    assert one["fields"]["version_count"] == 2 and two["content"] == "Invented bread answer two"
    assert one["thread"] == two["thread"] and one["thread"]["external_id"] == "lmstudio:1714557600000"
    assert one["thread"]["metadata"]["system_prompt"] == "You are an invented assistant."
    travel = [m for m in messages if m["thread"]["title"] == "Invented travel plan"]
    assert len(travel) == 4 and travel[2]["fields"]["file_count"] == 1
    assert travel[0]["thread"]["metadata"]["pinned"] is True
    folder_chat = [m for m in messages if m["fields"]["folder"] == "Invented Folder"]
    assert [m["content"] for m in folder_chat] == [
        "Invented old-shape question", "Invented old-shape answer", "Invented timed question", "Invented timed answer",
    ]
    assert folder_chat[2]["fields"]["time_source"] == "message"
    assert folder_chat[3]["happened_at"] == "2024-05-03T10:00:51+00:00"  # After the timed turn.
    assert folder_chat[1]["fields"]["model"] == "invented-org/invented-7b-gguf"  # lastUsedModel
    assert p.stats["unknown_shapes"]["unknown_message"] == 1


def test_lmstudio_change_detection_and_new_turns(monkeypatch, folder):
    p = plugin(monkeypatch, folder)
    messages, checkpoints = collect(p)
    cursor = checkpoints[-1].cursor
    assert collect(p, cursor)[0] == []
    # Touched but identical: not mapped again.
    os.utime(folder / SOUP, ns=(1, 1_800_000_000_000_000_000))
    again, after = collect(p, cursor)
    assert again == []
    cursor = after[-1].cursor
    # Two new turns: placed at the file's modification time when first seen.
    data = json.loads((folder / SOUP).read_text())
    data["messages"].append({"versions": [{"type": "singleStep", "role": "user",
                                           "content": [{"type": "text", "text": "Invented late question"}]}],
                             "currentlySelected": 0})
    data["messages"].append({"versions": [{"type": "singleStep", "role": "assistant",
                                           "content": [{"type": "text", "text": "Invented late answer"}]}],
                             "currentlySelected": 0})
    data["messages"][3]["currentlySelected"] = 0
    (folder / SOUP).write_text(json.dumps(data))
    os.utime(folder / SOUP, ns=(1, 1_717_243_200_000_000_000))  # 2024-06-01T12:00:00Z
    changed, _ = collect(p, cursor)
    found = ids(changed)
    assert len(changed) == 7
    assert found["lmstudio:1714557600000:4.0"]["happened_at"] == "2024-06-01T12:00:00+00:00"
    assert found["lmstudio:1714557600000:4.0"]["fields"]["time_source"] == "seen"
    assert found["lmstudio:1714557600000:5.0"]["happened_at"] == "2024-06-01T12:00:01+00:00"
    assert found["lmstudio:1714557600000:0.0"]["happened_at"] == "2024-05-01T10:00:00+00:00"
    assert found["lmstudio:1714557600000:3.0"]["fields"]["is_selected"] is True  # Re-selected


def test_lmstudio_skips_broken_files_and_checkpoints(monkeypatch, folder):
    (folder / "1799999999999.conversation.json").write_text('{"name": "Invented half writ')
    (folder / "1799999999998.conversation.json").write_text('["not", "a", "conversation"]')
    p = plugin(monkeypatch, folder, LMSTUDIO_BATCH_SIZE="2")
    items = list(p.records({}, "backfill"))
    assert sum(isinstance(i, Checkpoint) for i in items) == 4
    assert p.stats["unreadable"] == 1 and p.stats["unknown_shapes"]["unknown_conversation"] == 1


def test_lmstudio_without_created_uses_file_time(monkeypatch, folder):
    data = json.loads((folder / SOUP).read_text())
    del data["createdAt"]
    (folder / SOUP).write_text(json.dumps(data))
    os.utime(folder / SOUP, ns=(1, 1_717_243_200_000_000_000))
    messages, _ = collect(plugin(monkeypatch, folder))
    first = ids(messages)["lmstudio:1714557600000:0.0"]
    assert first["happened_at"] == "2024-06-01T12:00:00+00:00" and first["fields"]["time_source"] == "file"


def test_lmstudio_missing_folder_fails(monkeypatch, tmp_path):
    with pytest.raises(FileNotFoundError):
        collect(plugin(monkeypatch, tmp_path / "nowhere"))


def test_lmstudio_default_folders(monkeypatch, tmp_path):
    monkeypatch.delenv("LMSTUDIO_CONVERSATIONS_DIRS", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    shutil.copytree(RECORDED, tmp_path / ".cache" / "lm-studio" / "conversations")
    messages, _ = collect(LMStudio())
    assert len(messages) == 13


def test_lmstudio_contract():
    result = check_plugin(
        "throughline_lmstudio:LMStudio",
        {"LMSTUDIO_CONVERSATIONS_DIRS": str(RECORDED), "LMSTUDIO_BATCH_SIZE": "2"},
    )
    assert result["messages"] == 13
