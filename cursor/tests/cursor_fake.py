"""Builds an invented Cursor `User` directory with every store shape the
plugin understands. All content is invented."""

import json
import sqlite3
from pathlib import Path

T0 = 1714557600000  # 2024-05-01T10:00:00Z in milliseconds


def kv(db, table, rows):
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.execute(f"CREATE TABLE IF NOT EXISTS {table} (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
    for key, value in rows.items():
        conn.execute(
            f"INSERT INTO {table} VALUES (?, ?)",
            (key, value if isinstance(value, (bytes, str)) else json.dumps(value)),
        )
    conn.commit()
    conn.close()


def v3_composer(cid, name, start_ms, turns, mode="agent", model="invented-model-large"):
    """A composer _v3: headers in composerData, bubbles in their own keys."""
    headers, bubbles = [], {}
    for index, (role, text) in enumerate(turns):
        bid = f"{cid}-b{index}"
        headers.append({"bubbleId": bid, "type": 1 if role == "user" else 2})
        bubble = {
            "_v": 2,
            "type": 1 if role == "user" else 2,
            "bubbleId": bid,
            "text": text,
            "createdAt": _iso(start_ms + index * 60000),
        }
        if role == "assistant":
            bubble["modelInfo"] = {"modelName": model}
            bubble["tokenCount"] = {"inputTokens": 100 + index, "outputTokens": 20 + index}
        bubbles[f"bubbleId:{cid}:{bid}"] = bubble
    data = {
        "_v": 3,
        "composerId": cid,
        "name": name,
        "createdAt": start_ms,
        "lastUpdatedAt": start_ms + len(turns) * 60000,
        "unifiedMode": mode,
        "modelConfig": {"modelName": model},
        "fullConversationHeadersOnly": headers,
    }
    return {f"composerData:{cid}": data, **bubbles}


def _iso(ms):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat().replace("+00:00", "Z")


def build(root, conversations=4, turns=4):
    """Write the invented stores under root; returns the User directory."""
    user = Path(root) / "User"
    rows = {}
    for n in range(conversations):
        cid = f"c0000000-0000-4000-8000-00000000000{n}"
        rows.update(
            v3_composer(
                cid,
                f"Invented composer {n}",
                T0 + n * 86400000,
                [
                    ("user" if i % 2 == 0 else "assistant", f"Invented message {i} of composer {n}")
                    for i in range(turns)
                ],
            )
        )
    # A richText-only user bubble, a tool call, a thinking bubble and a
    # header whose bubble is missing.
    cid = "c0000000-0000-4000-8000-0000000000aa"
    rows[f"composerData:{cid}"] = {
        "_v": 9,
        "composerId": cid,
        "name": "Invented rich text",
        "createdAt": T0 + 10 * 86400000,
        "lastUpdatedAt": T0 + 10 * 86400000 + 5000,
        "unifiedMode": "chat",
        "fullConversationHeadersOnly": [
            {"bubbleId": "r1", "type": 1},
            {"bubbleId": "r2", "type": 2},
            {"bubbleId": "r3", "type": 2},
            {"bubbleId": "r4", "type": 2},
            {"bubbleId": "gone", "type": 2},
        ],
    }
    rich = {"root": {"type": "root", "children": [
        {"type": "paragraph", "children": [{"type": "text", "text": "Invented rich "}, {"type": "text", "text": "prompt"}]},
        {"type": "paragraph", "children": [{"type": "text", "text": "second line"}]},
    ]}}
    rows[f"bubbleId:{cid}:r1"] = {"type": 1, "text": "", "richText": json.dumps(rich)}
    rows[f"bubbleId:{cid}:r2"] = {"type": 2, "text": "", "toolFormerData": {"name": "read_file"}}
    rows[f"bubbleId:{cid}:r3"] = {"type": 2, "text": "", "thinking": {"text": "Invented thought"}}
    rows[f"bubbleId:{cid}:r4"] = {
        "type": 2,
        "text": "Invented rich reply",
        "timingInfo": {"clientStartTime": T0 + 10 * 86400000 + 4000},
    }
    # An older composer with the conversation inline (_v 2).
    cid = "c0000000-0000-4000-8000-0000000000bb"
    rows[f"composerData:{cid}"] = {
        "_v": 2,
        "composerId": cid,
        "name": "Invented inline composer",
        "createdAt": T0 + 11 * 86400000,
        "lastUpdatedAt": T0 + 11 * 86400000 + 9000,
        "conversation": [
            {"type": 1, "bubbleId": "i1", "text": "Invented inline question"},
            {"type": 2, "bubbleId": "i2", "text": "Invented inline answer"},
        ],
    }
    # Shapes the plugin must skip and count, not crash on.
    rows["composerData:c0000000-0000-4000-8000-0000000000cc"] = {"_v": 99, "somethingNew": True}
    rows["composerData:c0000000-0000-4000-8000-0000000000dd"] = b"\xff\xfe not json"
    rows["composerData:c0000000-0000-4000-8000-0000000000ee"] = {
        "_v": 3, "composerId": "c0000000-0000-4000-8000-0000000000ee", "fullConversationHeadersOnly": []
    }
    rows["unrelatedKey"] = {"ignored": True}
    kv(user / "globalStorage" / "state.vscdb", "cursorDiskKV", rows)

    ws = user / "workspaceStorage" / "0123456789abcdef"
    ws.mkdir(parents=True)
    (ws / "workspace.json").write_text(json.dumps({"folder": "file:///home/invented/projects/orchard%20app"}))
    kv(
        ws / "state.vscdb",
        "ItemTable",
        {
            "composer.composerData": {
                "allComposers": [
                    {"composerId": "c0000000-0000-4000-8000-000000000000", "name": "Invented composer 0"},
                    {"composerId": "c0000000-0000-4000-8000-0000000000bb"},
                    "not a dict",
                ],
                "selectedComposerId": "c0000000-0000-4000-8000-000000000000",
            },
            "workbench.panel.aichat.view.aichat.chatdata": {
                "tabs": [
                    {
                        "tabId": "t0000000-0000-4000-8000-000000000001",
                        "chatTitle": "Invented legacy chat",
                        "lastSendTime": T0 - 86400000,
                        "bubbles": [
                            {"type": "user", "id": "u1", "text": "Invented legacy question"},
                            {"type": "ai", "id": "a1", "text": "Invented legacy answer", "modelType": "invented-legacy-model"},
                            {"type": "user", "id": "u2", "text": "Invented follow-up"},
                            {"type": "mystery", "id": "x"},
                        ],
                    },
                    {"noTabId": True},
                ]
            },
            "aiService.prompts": [
                {"text": "Invented untimed prompt", "commandType": 4},
                {"text": "Invented untimed prompt 2", "commandType": 4},
            ],
        },
    )
    ws2 = user / "workspaceStorage" / "fedcba9876543210"
    ws2.mkdir(parents=True)
    kv(ws2 / "state.vscdb", "ItemTable", {"some.other.key": {"x": 1}})
    return user
