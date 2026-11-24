import json
from pathlib import Path

import httpx
import pytest
from throughline_sdk import Checkpoint
from throughline_sdk.contract import check_plugin

from throughline_openwebui import OpenWebUIPlugin
from throughline_openwebui.api import OpenWebUI

RECORDED = Path(__file__).parent / "recorded" / "openwebui.json"
CANARY = "sk-invented-canary-0b9f3c"
ROUTES = json.loads(RECORDED.read_text())["routes"]


class Server:
    """A MockTransport handler over the recorded routes that checks the key
    and records every request."""

    def __init__(self, routes=None, status=None):
        self.routes = dict(ROUTES if routes is None else routes)
        self.status = status or {}
        self.seen = []

    def __call__(self, request):
        assert request.headers["authorization"] == f"Bearer {CANARY}"
        key = request.url.path + (f"?{request.url.query.decode()}" if request.url.query else "")
        self.seen.append(key)
        if key in self.status:
            return httpx.Response(self.status[key])
        if key in self.routes:
            return httpx.Response(200, json=self.routes[key])
        return httpx.Response(404, json={"detail": "Not Found"})


def plugin(monkeypatch, server, **settings):
    monkeypatch.setenv("OPENWEBUI_URL", "https://chat.example.com")
    monkeypatch.setenv("OPENWEBUI_API_KEY", CANARY)
    monkeypatch.delenv("OPENWEBUI_FIXTURE", raising=False)
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    return OpenWebUIPlugin(transport=httpx.MockTransport(server))


def collect(p, cursor=None):
    items = list(p.records(cursor or {}, "sync"))
    return [i for i in items if isinstance(i, dict)], [i for i in items if isinstance(i, Checkpoint)]


def by_id(messages):
    return {m["fields"]["message_id"]: m for m in messages}


def test_openwebui_keeps_every_branch_and_marks_the_current_path(monkeypatch):
    messages, checkpoints = collect(plugin(monkeypatch, Server()))
    nodes = by_id(messages)
    assert set(nodes) == {"a1", "a2", "a3", "a4", "b1", "b2", "b3", "b4", "b5", "b6", "c1", "c2",
                          "d1", "d2", "e1", "e2", "g0", "g1", "g2"}
    assert {k for k, m in nodes.items() if m["fields"]["chat_id"] == "chat-b" and m["fields"]["is_current_path"]} == {"b5", "b6"}
    assert nodes["b3"]["fields"]["branch_index"] == 1 and nodes["b3"]["fields"]["sibling_count"] == 2
    assert nodes["b5"]["fields"]["branch_index"] == 1 and nodes["b5"]["fields"]["sibling_count"] == 2
    assert nodes["b6"]["fields"]["rating"] == 1
    reply = nodes["a2"]
    assert reply["fields"]["author_kind"] == "agent"
    assert reply["author"] == {"system": "openwebui", "kind": "agent", "value": "invented-model:7b",
                               "display_name": "Invented Model 7B"}
    assert reply["fields"]["input_tokens"] == 12 and reply["fields"]["output_tokens"] == 40
    assert reply["external_id"] == "chat-a:a2" and reply["thread"]["external_id"] == "chat-a"
    assert reply["happened_at"] == "2024-05-01T10:00:10+00:00"
    prompt = nodes["a1"]
    assert prompt["fields"]["author_kind"] == "owner" and prompt["author"]["kind"] == "owner"
    assert prompt["fields"]["model"] == "invented-model:7b"
    assert nodes["c1"]["fields"]["archived"] is True
    assert nodes["d1"]["fields"]["pinned"] is True and nodes["d1"]["fields"]["file_count"] == 1
    assert nodes["d2"]["content"] == "Invented upstream error" and nodes["d2"]["fields"]["has_error"]
    assert nodes["e2"]["fields"]["model"] == "invented-old-model" and nodes["e2"]["fields"]["is_current_path"]
    assert nodes["g1"]["content"] == "Invented multi-part\nquestion"
    assert nodes["g1"]["happened_at"] == "2024-05-06T10:00:01+00:00"  # ms timestamp
    assert nodes["g0"]["fields"]["role"] == "system"
    assert nodes["g2"]["fields"]["output_tokens"] == 55
    p_stats = checkpoints[-1].cursor
    assert p_stats["high_water"] == 1714557600 + 5 * 86400 + 90


def test_openwebui_incremental_stops_at_the_high_water_mark(monkeypatch):
    server = Server()
    p = plugin(monkeypatch, server)
    _, checkpoints = collect(p)
    server.seen.clear()
    again, _ = collect(p, checkpoints[-1].cursor)
    # Only the newest chat (at the high-water mark) is fetched again.
    assert {m["fields"]["chat_id"] for m in again} == {"chat-g"}
    assert "/api/v1/chats/?page=3" not in server.seen
    # A chat gains a regenerated answer: it is re-sent, with the new branch current.
    chat = json.loads(json.dumps(ROUTES["/api/v1/chats/chat-a"]))
    chat["updated_at"] = 1714557600 + 30 * 86400
    history = chat["chat"]["history"]
    history["messages"]["a3"]["childrenIds"].append("a5")
    history["messages"]["a5"] = {"id": "a5", "parentId": "a3", "childrenIds": [], "role": "assistant",
                                 "content": "Invented second basil answer", "timestamp": chat["updated_at"],
                                 "model": "invented-model:7b"}
    history["currentId"] = "a5"
    routes = dict(ROUTES)
    routes["/api/v1/chats/chat-a"] = chat
    listing = [dict(id="chat-a", title=chat["title"], updated_at=chat["updated_at"], created_at=chat["created_at"])]
    routes["/api/v1/chats/?page=1"] = listing + ROUTES["/api/v1/chats/?page=1"]
    p2 = plugin(monkeypatch, Server(routes))
    changed, _ = collect(p2, checkpoints[-1].cursor)
    nodes = by_id([m for m in changed if m["fields"]["chat_id"] == "chat-a"])
    assert nodes["a4"]["fields"]["is_current_path"] is False
    assert nodes["a5"]["fields"]["is_current_path"] is True and nodes["a5"]["fields"]["branch_index"] == 1


def test_openwebui_checkpoints_and_vanished_chats(monkeypatch):
    p = plugin(monkeypatch, Server(), OPENWEBUI_BATCH_SIZE="2")
    items = list(p.records({}, "backfill"))
    checkpoints = [i.cursor["high_water"] for i in items if isinstance(i, Checkpoint)]
    assert len(checkpoints) == 4 and checkpoints == sorted(checkpoints)
    assert p.stats["vanished_chats"] == 1


def test_openwebui_archived_toggle_and_old_servers(monkeypatch):
    routes = {k: v for k, v in ROUTES.items() if "pinned" not in k and "archived" not in k}
    p = plugin(monkeypatch, Server(routes), OPENWEBUI_INCLUDE_ARCHIVED="false")
    messages, _ = collect(p)
    assert not {"chat-c", "chat-d"} & {m["fields"]["chat_id"] for m in messages}
    p = plugin(monkeypatch, Server(routes), OPENWEBUI_INCLUDE_ARCHIVED="true")
    collect(p)
    assert p.stats["archived_unsupported"] is True


def test_openwebui_bad_key_and_unreachable_server_raise(monkeypatch):
    p = plugin(monkeypatch, Server(status={"/api/v1/chats/?page=1": 401}))
    with pytest.raises(PermissionError):
        collect(p)

    def down(request):
        raise httpx.ConnectError("invented outage")

    monkeypatch.setenv("OPENWEBUI_URL", "https://chat.example.com")
    monkeypatch.setenv("OPENWEBUI_API_KEY", CANARY)
    p = OpenWebUIPlugin(transport=httpx.MockTransport(down))
    p.api = lambda: OpenWebUI(p.url, CANARY, transport=httpx.MockTransport(down), sleep=lambda s: None)
    with pytest.raises(httpx.ConnectError):
        collect(p)


def test_openwebui_retries_server_errors(monkeypatch):
    calls = []

    def flaky(request):
        calls.append(1)
        return httpx.Response(503) if len(calls) < 3 else httpx.Response(200, json=[])

    api = OpenWebUI("https://chat.example.com", CANARY, transport=httpx.MockTransport(flaky), sleep=lambda s: None)
    assert api.get("/api/v1/chats/") == []
    assert len(calls) == 3


def test_openwebui_contract():
    result = check_plugin(
        "throughline_openwebui:OpenWebUIPlugin",
        {
            "OPENWEBUI_URL": "https://chat.example.com",
            "OPENWEBUI_API_KEY": CANARY,
            "OPENWEBUI_FIXTURE": str(RECORDED),
            "OPENWEBUI_BATCH_SIZE": "2",
        },
        secrets=[CANARY],
    )
    assert result["messages"] == 19
