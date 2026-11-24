import json
import shutil
from pathlib import Path

import httpx
import pytest
from throughline_sdk import Checkpoint, Record
from throughline_sdk.contract import check_plugin

from throughline_signal import Signal
from throughline_signal.spool import read_lines

RECORDED = Path(__file__).parent / "recorded"
ACC = "+15555550100"
ME = "00000000-0000-4000-8000-000000000100"
ALEX = "00000000-0000-4000-8000-000000000101"
SAM = "00000000-0000-4000-8000-000000000102"
GID = "SW52ZW50ZWRHcm91cElkMDAwMDAwMDAwMDAwMDAwMDA="
T0 = 1791619200000  # 2026-10-10T08:00:00Z


def t(minutes):
    return T0 + minutes * 60000


def key(author, minutes):
    return f"signal:{ACC}:{author}:{t(minutes)}"


@pytest.fixture
def state(tmp_path, monkeypatch):
    spool = tmp_path / "state" / "spool"
    shutil.copytree(RECORDED, spool)
    monkeypatch.setenv("SIGNAL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SIGNAL_RPC_URL", "none")
    monkeypatch.setenv("SIGNAL_ACCOUNT", ACC)
    monkeypatch.setenv("SIGNAL_BATCH_SIZE", "3")
    monkeypatch.delenv("SIGNAL_SPOOL_DIR", raising=False)
    return tmp_path / "state"


def rows(items):
    return {m["external_id"]: m for m in items if isinstance(m, dict)}


def test_mapping_folds_reactions_edits_and_deletes(state):
    items = list(Signal().records({}, "backfill"))
    found = rows(items)
    assert len(found) == 12
    hi = found[key(ALEX, 0)]
    assert hi["author"] == {
        "system": "phone",
        "kind": "number",
        "value": "+15555550101",
        "display_name": "Invented Alex",
    }
    assert hi["thread"]["external_id"] == f"signal:{ACC}:dm:{ALEX}"
    assert hi["happened_at"] == "2026-10-10T08:00:00+00:00"
    mine = found[key(ME, 1)]
    assert mine["fields"]["author_kind"] == "owner"
    assert mine["author"]["value"] == ACC
    assert mine["thread"]["external_id"] == f"signal:{ACC}:dm:{ALEX}"
    reply = found[key(ALEX, 2)]
    assert reply["fields"]["reply_to"] == key(ME, 1)
    group = found[key(SAM, 4)]
    assert group["thread"]["external_id"] == f"signal:{ACC}:group:{GID}"
    assert group["author"] == {"system": "signal", "kind": "uuid", "value": SAM, "display_name": "Invented Sam"}
    assert group["fields"]["reaction_details"][0]["sender_kind"] == "owner"
    photo = found[key(ALEX, 5)]
    assert photo["content"] == "[attachment: invented-sunset.jpg]"
    assert "reaction_details" not in photo["fields"]  # added then removed
    edited = found[key(ALEX, 8)]
    assert edited["content"] == "Invented typo message (fixed)"
    assert edited["fields"]["edit_count"] == 1
    assert found[key(SAM, 11)]["fields"]["deleted_at"].startswith("2026-10-10T08:12")
    assert found[key(ALEX, 13)]["content"] == "[sticker \U0001f389]"
    assert found[key(SAM, 16)]["fields"]["attachments"][0]["voiceNote"] is True
    assert found[key(ME, 17)]["thread"]["external_id"] == f"signal:{ACC}:dm:{SAM}"
    links = [i.data for i in items if isinstance(i, Record) and i.kind == "link"]
    assert [link["to"]["external_id"] for link in links] == [key(ME, 1)]
    assert len([i for i in items if isinstance(i, Checkpoint)]) >= 6


def test_late_modification_reemits_target_once(state):
    plugin = Signal()
    cursor = [i for i in plugin.records({}, "sync") if isinstance(i, Checkpoint)][-1].cursor
    assert rows(plugin.records(cursor, "sync")) == {}
    late = {
        "account": ACC,
        "envelope": {
            "source": "+15555550101",
            "sourceNumber": "+15555550101",
            "sourceUuid": ALEX,
            "sourceName": "Invented Alex",
            "sourceDevice": 1,
            "timestamp": t(30),
            "dataMessage": {
                "timestamp": t(30),
                "reaction": {"emoji": "\U0001f525", "targetAuthorUuid": ME, "targetSentTimestamp": t(1), "isRemove": False},
            },
        },
    }
    with open(state / "spool" / "signal-2026-11.jsonl", "a") as handle:
        handle.write(json.dumps(late) + "\n")
        handle.write('{"partial line still being written')
    later = rows(plugin.records(cursor, "sync"))
    assert list(later) == [key(ME, 1)]
    assert later[key(ME, 1)]["fields"]["reaction_details"][0]["emoji"] == "\U0001f525"


def test_rpc_receive_is_spooled_before_mapping(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNAL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SIGNAL_RPC_URL", "http://signal-cli.invalid:8080")
    monkeypatch.setenv("SIGNAL_ACCOUNT", ACC)
    monkeypatch.setenv("SIGNAL_MULTI_ACCOUNT", "1")
    monkeypatch.delenv("SIGNAL_SPOOL_DIR", raising=False)
    lines = [json.loads(line) for line in (RECORDED / "signal-2026-10.jsonl").read_text().splitlines()]
    queue = [lines[:6], lines[6:], []]
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        assert request.url.path == "/api/v1/rpc"
        assert body["params"]["account"] == ACC
        if body["method"] == "receive":
            result = [{"envelope": item["envelope"]} for item in queue.pop(0)]
        else:
            result = [{"id": GID, "name": "Invented Climbing Group"}]
        return httpx.Response(200, json={"jsonrpc": "2.0", "result": result, "id": body["id"]})

    plugin = Signal()
    plugin.transport = httpx.MockTransport(handler)
    found = rows(plugin.records({}, "sync"))
    assert [c["method"] for c in calls] == ["receive", "receive", "receive", "listGroups"]
    spooled = [item for path in sorted((tmp_path / "state" / "spool").glob("*.jsonl")) for _, item in read_lines(path)]
    assert len(spooled) == 8  # receipt and typing envelopes are not kept
    assert all(item["account"] == ACC for item in spooled)
    assert found[key(SAM, 4)]["thread"]["title"] == "Invented Climbing Group"


def test_rpc_error_fails_the_run(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNAL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIGNAL_RPC_URL", "http://signal-cli.invalid:8080")
    plugin = Signal()
    plugin.transport = httpx.MockTransport(
        lambda r: httpx.Response(200, json={"jsonrpc": "2.0", "error": {"code": -1, "message": "Invented failure"}, "id": "x"})
    )
    with pytest.raises(RuntimeError, match="Invented failure"):
        list(plugin.records({}, "sync"))


def test_contract(tmp_path):
    spool = tmp_path / "spool"
    shutil.copytree(RECORDED, spool)
    result = check_plugin(
        "throughline_signal:Signal",
        {
            "SIGNAL_RPC_URL": "none",
            "SIGNAL_STATE_DIR": str(tmp_path / "state"),
            "SIGNAL_SPOOL_DIR": str(spool),
            "SIGNAL_ACCOUNT": ACC,
            "SIGNAL_BATCH_SIZE": "2",
        },
    )
    assert result["messages"] == 12
