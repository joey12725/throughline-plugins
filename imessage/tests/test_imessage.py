import json
import sqlite3
from pathlib import Path

import pytest
from throughline_sdk import Checkpoint, Record
from throughline_sdk.contract import check_plugin

from throughline_imessage import IMessage
from throughline_imessage.chatdb import (
    apple_time,
    decode_attributed_body,
    tapback_target,
)
from throughline_imessage.mapping import author_for

RECORDED = Path(__file__).parent / "recorded" / "chat.json"
BASE = 794048400  # 2026-03-01T09:00:00Z in seconds after 2001-01-01
NS = 1_000_000_000

SCHEMA = """
CREATE TABLE handle (ROWID INTEGER PRIMARY KEY AUTOINCREMENT UNIQUE, id TEXT NOT NULL,
  country TEXT, service TEXT NOT NULL, uncanonicalized_id TEXT, person_centric_id TEXT);
CREATE TABLE chat (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT UNIQUE NOT NULL,
  style INTEGER, state INTEGER, account_id TEXT, properties BLOB, chat_identifier TEXT,
  service_name TEXT, room_name TEXT, account_login TEXT, is_archived INTEGER DEFAULT 0,
  last_addressed_handle TEXT, display_name TEXT, group_id TEXT);
CREATE TABLE message (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT UNIQUE NOT NULL,
  text TEXT, replace INTEGER DEFAULT 0, service_center TEXT, handle_id INTEGER DEFAULT 0,
  subject TEXT, country TEXT, attributedBody BLOB, version INTEGER DEFAULT 0,
  type INTEGER DEFAULT 0, service TEXT, account TEXT, account_guid TEXT, error INTEGER DEFAULT 0,
  date INTEGER, date_read INTEGER, date_delivered INTEGER, is_delivered INTEGER DEFAULT 0,
  is_finished INTEGER DEFAULT 0, is_from_me INTEGER DEFAULT 0, is_read INTEGER DEFAULT 0,
  cache_has_attachments INTEGER DEFAULT 0, item_type INTEGER DEFAULT 0,
  other_handle INTEGER DEFAULT 0, group_title TEXT, group_action_type INTEGER DEFAULT 0,
  balloon_bundle_id TEXT, payload_data BLOB, expressive_send_style_id TEXT,
  associated_message_guid TEXT, associated_message_type INTEGER DEFAULT 0,
  associated_message_emoji TEXT, thread_originator_guid TEXT, date_edited INTEGER DEFAULT 0,
  date_retracted INTEGER DEFAULT 0, destination_caller_id TEXT, message_summary_info BLOB);
CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT UNIQUE NOT NULL,
  created_date INTEGER DEFAULT 0, filename TEXT, uti TEXT, mime_type TEXT,
  transfer_name TEXT, total_bytes INTEGER DEFAULT 0, is_sticker INTEGER DEFAULT 0);
CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER, UNIQUE(chat_id, handle_id));
CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER, message_date INTEGER DEFAULT 0,
  PRIMARY KEY (chat_id, message_id));
CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER,
  UNIQUE(message_id, attachment_id));
"""


def typedstream(text):
    """An NSAttributedString typedstream blob like the ones Messages writes."""
    data = text.encode()
    n = len(data)
    if n < 0x80:
        length = bytes([n])
    elif n < 0x8000:
        length = b"\x81" + n.to_bytes(2, "little")
    else:
        length = b"\x82" + n.to_bytes(4, "little")
    return (
        b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString"
        b"\x00\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+"
        + length
        + data
        + b"\x86\x84\x02iI\x01\x82\x92\x84\x84\x84\x0cNSDictionary\x00\x94\x84\x01i\x01"
        b"\x92\x84\x96\x96\x1d__kIMMessagePartAttributeName\x86\x92\x84\x84\x84\x08"
        b"NSNumber\x00\x84\x84\x07NSValue\x00\x94\x84\x01*\x84\x99\x99\x00\x86\x86\x86"
    )


def build_db(path, data=None, upto=None):
    data = data or json.loads(RECORDED.read_text())
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    for h in data["handles"]:
        conn.execute(
            "INSERT INTO handle(ROWID,id,service) VALUES (?,?,?)",
            (h["rowid"], h["id"], h["service"]),
        )
    for c in data["chats"]:
        conn.execute(
            "INSERT INTO chat(ROWID,guid,style,chat_identifier,service_name,display_name)"
            " VALUES (?,?,?,?,?,?)",
            (c["rowid"], c["guid"], c["style"], c["chat_identifier"], c["service_name"], c["display_name"]),
        )
        for h in c["handles"]:
            conn.execute("INSERT INTO chat_handle_join VALUES (?,?)", (c["rowid"], h))
    for a in data["attachments"]:
        conn.execute(
            "INSERT INTO attachment(ROWID,guid,filename,mime_type,transfer_name,total_bytes,uti,is_sticker)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (a["rowid"], a["guid"], a["filename"], a["mime_type"], a["transfer_name"], a["total_bytes"], a["uti"], a["is_sticker"]),
        )
    for m in data["messages"][:upto]:
        add_message(conn, m)
    conn.commit()
    conn.close()
    return path


def add_message(conn, m):
    when = (BASE + m["t"]) * NS
    cur = conn.execute(
        "INSERT INTO message(guid,text,attributedBody,handle_id,service,account,date,is_from_me,"
        "item_type,group_title,associated_message_guid,associated_message_type,"
        "associated_message_emoji,thread_originator_guid,date_edited,date_retracted,"
        "cache_has_attachments,destination_caller_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            m["guid"],
            m.get("text"),
            typedstream(m["body_only"]) if m.get("body_only") else None,
            m.get("handle", 0),
            m.get("service", "iMessage"),
            "E:invented.owner@example.com",
            when,
            m.get("from_me", 0),
            m.get("item_type", 0),
            m.get("group_title"),
            m.get("target"),
            m.get("tapback", 0),
            m.get("emoji"),
            m.get("reply_to"),
            (BASE + m["edited"]) * NS if m.get("edited") else 0,
            (BASE + m["unsent"]) * NS if m.get("unsent") else 0,
            1 if m.get("attachments") else 0,
            "invented.owner@example.com" if m.get("from_me") else None,
        ),
    )
    conn.execute("INSERT INTO chat_message_join VALUES (?,?,?)", (m["chat"], cur.lastrowid, when))
    for a in m.get("attachments", []):
        conn.execute("INSERT INTO message_attachment_join VALUES (?,?)", (cur.lastrowid, a))


@pytest.fixture
def chatdb(tmp_path, monkeypatch):
    path = build_db(tmp_path / "chat.db")
    monkeypatch.setenv("IMESSAGE_DB", str(path))
    monkeypatch.setenv("IMESSAGE_BATCH_SIZE", "4")
    monkeypatch.delenv("IMESSAGE_OWNER_HANDLE", raising=False)
    return path


def messages(items):
    return [i for i in items if isinstance(i, dict)]


def by_id(items):
    return {m["external_id"]: m for m in messages(items)}


def test_attributed_body_decoder_short_long_and_garbage():
    assert decode_attributed_body(typedstream("Invented hi")) == "Invented hi"
    long = "Invented " * 40
    assert decode_attributed_body(typedstream(long)) == long
    assert decode_attributed_body(typedstream("Invented emoji \U0001f3d4")) == "Invented emoji \U0001f3d4"
    assert decode_attributed_body(b"no string here") is None
    assert decode_attributed_body(None) is None


def test_apple_time_nanoseconds_and_seconds():
    assert apple_time(BASE * NS).isoformat() == "2026-03-01T09:00:00+00:00"
    assert apple_time(BASE).isoformat() == "2026-03-01T09:00:00+00:00"
    assert apple_time(0) is None


def test_tapback_target_and_authors():
    assert tapback_target("p:0/ABC") == "ABC"
    assert tapback_target("bp:ABC") == "ABC"
    assert author_for("+15555550101") == {"system": "phone", "kind": "number", "value": "+15555550101"}
    assert author_for("E:a@example.com")["system"] == "email"
    assert author_for("urn:biz:invented")["system"] == "imessage"


def test_mapping(chatdb):
    items = list(IMessage().records({}, "backfill"))
    rows = by_id(items)
    # Tapbacks never become messages.
    assert "imessage:M-0004" not in rows and "imessage:M-0014" not in rows
    assert len(rows) == 16
    first = rows["imessage:M-0001"]
    assert first["channel"] == "imessage.messages"
    assert first["happened_at"] == "2026-03-01T09:00:00+00:00"
    assert first["author"] == {"system": "phone", "kind": "number", "value": "+15555550101"}
    assert first["fields"]["direction"] == "incoming"
    assert first["fields"]["is_from_me"] is False
    assert first["thread"]["metadata"]["kind"] == "dm"
    mine = rows["imessage:M-0002"]
    assert mine["content"].startswith("Yes! Invented plan")  # attributedBody only
    assert mine["fields"]["author_kind"] == "owner"
    assert mine["author"]["value"] == "invented.owner@example.com"
    assert mine["fields"]["reaction_details"][0]["emoji"] == "❤️"
    assert "Reactions:" in mine["content"]
    photo = rows["imessage:M-0003"]
    assert photo["content"] == "[attachment: invented-trail.jpg]"
    assert photo["fields"]["attachments"][0]["mime_type"] == "image/jpeg"
    group = rows["imessage:M-0005"]
    assert group["thread"]["title"] == "Invented Hiking Club"
    assert group["fields"]["conversation_kind"] == "group_dm"
    assert group["fields"]["participants"] == ["+15555550102", "invented.friend@example.com"]
    kinds = sorted(d["kind"] for d in group["fields"]["reaction_details"])
    assert kinds == ["emoji", "laugh"]
    assert any(d.get("sender_kind") == "owner" for d in group["fields"]["reaction_details"])
    assert "reaction_details" not in rows["imessage:M-0006"]["fields"]  # like removed
    assert len(rows["imessage:M-0006"]["content"]) > 127
    assert rows["imessage:M-0007"]["fields"]["reply_to"] == "imessage:M-0005"
    assert rows["imessage:M-0008"]["content"].startswith("[renamed the conversation")
    assert rows["imessage:M-0009"]["fields"]["edited_at"].startswith("2026-03-01T09:10")
    assert rows["imessage:M-0010"]["content"] == "[unsent message]"
    assert rows["imessage:M-0016"]["fields"]["service"] == "SMS"
    links = [i for i in items if isinstance(i, Record) and i.kind == "link"]
    assert [link.data["to"]["external_id"] for link in links] == ["imessage:M-0005"]
    checkpoints = [i for i in items if isinstance(i, Checkpoint)]
    assert len(checkpoints) >= 5


def test_incremental_tapback_and_edit_on_old_message(chatdb):
    plugin = IMessage()
    items = list(plugin.records({}, "sync"))
    cursor = [i for i in items if isinstance(i, Checkpoint)][-1].cursor
    assert list(m for m in messages(plugin.records(cursor, "sync"))) == []
    conn = sqlite3.connect(chatdb)
    add_message(conn, {"guid": "M-0100", "chat": 1, "handle": 1, "t": 2000, "tapback": 2001, "target": "p:0/M-0018"})
    add_message(conn, {"guid": "M-0101", "chat": 1, "handle": 1, "t": 2060, "text": "Invented new message"})
    conn.execute(
        "UPDATE message SET text=?, date_edited=? WHERE guid='M-0001'",
        ("Invented hello, edited", (BASE + 2100) * NS),
    )
    conn.commit()
    conn.close()
    later = by_id(plugin.records(cursor, "sync"))
    assert set(later) == {"imessage:M-0001", "imessage:M-0018", "imessage:M-0101"}
    assert later["imessage:M-0001"]["content"] == "Invented hello, edited"
    assert later["imessage:M-0018"]["fields"]["reaction_details"][0]["kind"] == "like"


def test_missing_database_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("IMESSAGE_DB", str(tmp_path / "missing.db"))
    with pytest.raises(RuntimeError, match="Full Disk Access"):
        list(IMessage().records({}, "backfill"))


def test_contract(tmp_path):
    path = build_db(tmp_path / "chat.db")
    result = check_plugin(
        "throughline_imessage:IMessage",
        {"IMESSAGE_DB": str(path), "IMESSAGE_BATCH_SIZE": "3"},
    )
    assert result["messages"] == 16
