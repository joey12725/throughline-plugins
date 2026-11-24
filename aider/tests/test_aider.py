import shutil
from pathlib import Path

import pytest
from throughline_sdk import Checkpoint
from throughline_sdk.contract import check_plugin

from throughline_aider import Aider
from throughline_aider.history import parse

RECORDED = Path(__file__).parent / "recorded"


@pytest.fixture
def roots(tmp_path):
    target = tmp_path / "roots"
    shutil.copytree(RECORDED, target)
    return target


def plugin(monkeypatch, roots, **settings):
    monkeypatch.setenv("AIDER_ROOTS", str(roots))
    monkeypatch.setenv("AIDER_TIMEZONE", "America/Winnipeg")
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    return Aider()


def collect(p, cursor=None):
    items = list(p.records(cursor or {}, "sync"))
    return [i for i in items if isinstance(i, dict)], [i for i in items if isinstance(i, Checkpoint)]


def test_aider_parses_sessions_roles_models_and_times(monkeypatch, roots):
    messages, checkpoints = collect(plugin(monkeypatch, roots))
    orchard = [m for m in messages if m["fields"]["repo"].endswith("orchard-app")]
    first = [m for m in orchard if m["fields"]["session_started_at"].startswith("2024-05-01")]
    assert [(m["fields"]["role"], m["content"].splitlines()[0]) for m in first] == [
        ("user", "/add invented_app.py"),
        ("tool", "Added invented_app.py to the chat"),
        ("user", "Add an invented greeting function"),
        ("assistant", "Here is the change to add the invented greeting:"),
        ("tool", "Applied edit to invented_app.py"),
        ("user", "Now call it from main"),
        ("assistant", "I added the call to `greet()` in `main`."),
        ("tool", "Applied edit to invented_app.py"),
    ]
    prompt = first[2]
    assert prompt["content"] == "Add an invented greeting function\nthat prints hello orchard"
    assert prompt["happened_at"] == "2024-05-01T10:01:05-05:00"
    assert prompt["fields"]["time_estimated"] is False
    assert prompt["fields"]["author_kind"] == "owner"
    assert prompt["author"]["kind"] == "owner"
    reply = first[3]
    assert reply["fields"]["author_kind"] == "agent"
    assert reply["fields"]["model"] == "invented-model-large"
    assert reply["author"] == {
        "system": "aider", "kind": "agent", "value": "invented-model-large", "display_name": "invented-model-large"
    }
    assert reply["happened_at"] == "2024-05-01T10:01:06-05:00" and reply["fields"]["time_estimated"]
    assert ">>>>>>> REPLACE" in reply["content"]
    assert first[0]["fields"]["is_command"] and not prompt["fields"]["is_command"]
    assert first[1]["fields"]["author_kind"] == "other"
    assert first[0]["fields"]["aider_version"] == "0.50.1"
    assert first[0]["thread"]["metadata"]["aider_version"] == "0.50.1"
    second = [m for m in orchard if m["fields"]["session_started_at"].startswith("2024-05-02")]
    assert second[1]["content"] == "The invented orchard module keeps a list of trees.\n\nIt has two functions."
    assert second[-1]["fields"]["model"] == "invented-model-other"
    third = [m for m in orchard if m["fields"]["session_started_at"].startswith("2024-05-03")]
    assert third[-2]["content"] == "Run them" and third[-2]["fields"]["time_estimated"]
    lantern = [m for m in messages if m["fields"]["repo"].endswith("lantern-cli")]
    assert len(lantern) == 5 and lantern[1]["fields"]["model"] == "invented-local-model"
    assert all(m["fields"]["time_estimated"] for m in lantern[1:])
    assert len({m["external_id"] for m in messages}) == len(messages)
    assert len({m["thread"]["external_id"] for m in messages}) == 4
    assert checkpoints[-1].cursor["files"]


def test_aider_cursor_rereads_only_the_last_session(monkeypatch, roots):
    p = plugin(monkeypatch, roots)
    messages, checkpoints = collect(p)
    again, after = collect(p, checkpoints[-1].cursor)
    # Only each file's last session is read again, with identical records.
    last_sessions = {m["thread"]["external_id"] for m in again}
    assert len(last_sessions) == 2
    assert all(m in messages for m in again)
    history = roots / "orchard-app" / ".aider.chat.history.md"
    with open(history, "a") as handle:
        handle.write("\n#### One more invented request  \n\nDone, invented.\n")
        handle.write("\n# aider chat started at 2024-05-04 08:00:00\n\n> Model: invented-model-large\n\n#### Hi again  \n")
    grown, _ = collect(p, after[-1].cursor)
    contents = [m["content"] for m in grown if m["fields"]["repo"].endswith("orchard-app")]
    assert contents == [
        "Add invented tests", "Added three invented tests.", "Run them",
        "They all pass in this invented example.", "One more invented request", "Done, invented.", "Hi again",
    ]
    old_ids = {m["external_id"] for m in messages}
    assert {m["external_id"] for m in grown if m["content"] == "Run them"} <= old_ids


def test_aider_truncated_file_is_read_again(monkeypatch, roots):
    p = plugin(monkeypatch, roots)
    _, checkpoints = collect(p)
    history = roots / "orchard-app" / ".aider.chat.history.md"
    history.write_text("\n# aider chat started at 2024-07-01 10:00:00\n\n#### Fresh invented start  \n")
    messages, _ = collect(p, checkpoints[-1].cursor)
    assert "Fresh invented start" in [m["content"] for m in messages]


def test_aider_without_tool_output_and_depth(monkeypatch, roots):
    messages, _ = collect(plugin(monkeypatch, roots, AIDER_INCLUDE_TOOL_OUTPUT="false"))
    assert {m["fields"]["role"] for m in messages} == {"user", "assistant"}
    shallow, _ = collect(plugin(monkeypatch, roots, AIDER_SCAN_DEPTH="1"))
    assert {Path(m["fields"]["repo"]).name for m in shallow} == {"orchard-app"}


def test_aider_parse_edge_cases():
    data = (
        b"stray line before any session\n"
        b"# aider chat started at 2024-01-01 00:00:00\n\n"
        b"#### one  \n\n#### two  \n\n"
        b"> tool a  \n\n> tool b  \n\n"
        b"reply line\n\n> not a tool, a quote?\n"
    )
    sessions, skipped = parse(data)
    assert skipped == 1
    blocks = [(b.role, b.text) for b in sessions[0].blocks]
    assert blocks == [
        ("user", "one"), ("user", "two"), ("tool", "tool a\ntool b"),
        ("assistant", "reply line"), ("tool", "not a tool, a quote?"),
    ]
    assert sessions[0].offset == len(b"stray line before any session\n")
    assert sessions[0].end == len(data)


def test_aider_missing_roots_fail(monkeypatch, tmp_path):
    p = plugin(monkeypatch, tmp_path / "nowhere")
    with pytest.raises(FileNotFoundError):
        collect(p)


def test_aider_contract():
    result = check_plugin(
        "throughline_aider:Aider",
        {"AIDER_ROOTS": str(RECORDED), "AIDER_TIMEZONE": "America/Winnipeg", "AIDER_BATCH_SIZE": "3"},
    )
    assert result["messages"] == 23
