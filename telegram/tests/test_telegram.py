import json
from pathlib import Path

import pytest
from throughline_sdk import Checkpoint, Record
from throughline_sdk.contract import check_plugin

from throughline_telegram import Telegram
from throughline_telegram.recording import RecordedClient
from throughline_telegram.source import TelegramSource

RECORDED = Path(__file__).parent / "recorded" / "dialogs.json"
PREFIX = "telegram:1000001:"


@pytest.fixture
def env(monkeypatch, tmp_path):
    path = tmp_path / "dialogs.json"
    path.write_text(RECORDED.read_text())
    monkeypatch.setenv("TELEGRAM_RECORDING", str(path))
    monkeypatch.setenv("TELEGRAM_BATCH_SIZE", "2")
    for key in (
        "TELEGRAM_INCLUDE_CHANNELS",
        "TELEGRAM_INCLUDE_BOTS",
        "TELEGRAM_MAX_GROUP_SIZE",
        "TELEGRAM_INCLUDE",
        "TELEGRAM_EXCLUDE",
        "TELEGRAM_RESCAN_DAYS",
        "TELEGRAM_SESSION",
    ):
        monkeypatch.delenv(key, raising=False)
    return path


def rows(items):
    return {m["external_id"]: m for m in items if isinstance(m, dict)}


def test_default_filters_keep_private_chats_and_small_groups(env):
    items = list(Telegram().records({}, "backfill"))
    found = rows(items)
    dialogs = {m["fields"]["dialog_id"] for m in found.values()}
    assert dialogs == {"1000002", "-4000001", "-1009000001", "1000001"}
    assert len(found) == 17
    checkpoints = [i for i in items if isinstance(i, Checkpoint)]
    assert checkpoints[-1].cursor == {
        "dialogs": {"1000002": 17, "-4000001": 26, "-1009000001": 103, "1000001": 41}
    }


def test_mapping(env):
    items = list(Telegram().records({}, "backfill"))
    found = rows(items)
    hi = found[PREFIX + "1000002:11"]
    assert hi["author"] == {
        "system": "telegram",
        "kind": "user_id",
        "value": "1000002",
        "display_name": "Invented Alex",
    }
    assert hi["thread"]["external_id"] == PREFIX + "1000002"
    assert hi["thread"]["metadata"]["kind"] == "dm"
    assert hi["happened_at"] == "2026-09-01T10:00:00+00:00"
    reply = found[PREFIX + "1000002:12"]
    assert reply["fields"]["author_kind"] == "owner"
    assert reply["fields"]["reply_to"] == PREFIX + "1000002:11"
    assert found[PREFIX + "1000002:13"]["content"] == "[photo]"
    forward = found[PREFIX + "1000002:14"]["fields"]["forwarded_from"]
    assert forward == {"from_id": "channel:7000001", "date": "2026-08-31T08:00:00+00:00", "channel_post": 55}
    edited = found[PREFIX + "1000002:15"]
    assert edited["fields"]["edited_at"] == "2026-09-01T10:05:00+00:00"
    assert edited["fields"]["reaction_details"][0]["sender_id"] == "user:1000002"
    assert found[PREFIX + "1000002:16"]["content"] == "[voice message 0:12]"
    doc = found[PREFIX + "1000002:17"]
    assert doc["content"].endswith("[document: invented-itinerary.pdf]")
    assert doc["fields"]["media"]["mime_type"] == "application/pdf"
    created = found[PREFIX + "-4000001:21"]
    assert created["content"] == "[ChatCreate: Invented Book Club]"
    assert created["fields"]["service_action"] == "MessageActionChatCreate"
    assert found[PREFIX + "-4000001:22"]["fields"]["reaction_details"] == [{"emoji": "\U0001f525", "count": 2}]
    assert found[PREFIX + "-4000001:25"]["fields"]["album_id"] == "99001"
    assert found[PREFIX + "-4000001:26"]["fields"]["media"]["lat"] == 40.0
    assert "reply_to" not in found[PREFIX + "-1009000001:103"]["fields"]
    links = [i.data for i in items if isinstance(i, Record) and i.kind == "link"]
    assert len(links) == 3


def test_options_include_channels_bots_and_explicit_lists(env, monkeypatch):
    monkeypatch.setenv("TELEGRAM_INCLUDE_CHANNELS", "1")
    monkeypatch.setenv("TELEGRAM_INCLUDE_BOTS", "1")
    monkeypatch.setenv("TELEGRAM_EXCLUDE", "@invented_alex")
    monkeypatch.setenv("TELEGRAM_INCLUDE", "-1008000001")
    found = rows(Telegram().records({}, "backfill"))
    dialogs = {m["fields"]["dialog_id"] for m in found.values()}
    assert dialogs == {"-4000001", "-1009000001", "1000001", "-1007000001", "-1008000001", "1000004"}
    post = found[PREFIX + "-1007000001:55"]
    assert post["fields"]["conversation_kind"] == "channel"
    assert post["author"] is None
    assert found[PREFIX + "1000004:31"]["fields"]["sender_is_bot"] is True


def test_incremental_and_edit_rescan(env, monkeypatch):
    monkeypatch.setenv("TELEGRAM_RESCAN_DAYS", "100000")
    plugin = Telegram()
    cursor = [i for i in plugin.records({}, "sync") if isinstance(i, Checkpoint)][-1].cursor
    data = json.loads(env.read_text())
    alex = data["dialogs"][0]["messages"]
    alex[0]["text"] = "Invented hi, edited"
    alex[0]["edit_date"] = "2026-09-06T00:00:00+00:00"
    alex.append({"id": 18, "date": "2026-09-06T00:01:00+00:00", "sender_id": 1000002, "text": "Invented new one"})
    env.write_text(json.dumps(data))
    later = rows(plugin.records(cursor, "sync"))
    assert later[PREFIX + "1000002:11"]["content"] == "Invented hi, edited"
    assert later[PREFIX + "1000002:18"]["content"] == "Invented new one"
    monkeypatch.setenv("TELEGRAM_RESCAN_DAYS", "0")
    only_new = rows(Telegram().records(cursor, "sync"))
    assert set(only_new) == {PREFIX + "1000002:18"}


class FakeTelethon:
    """Hand-built Telethon look-alike: unauthorized session."""

    async def connect(self):
        return None

    async def is_user_authorized(self):
        return False

    def disconnect(self):
        return None


def test_unauthorized_session_raises():
    with pytest.raises(RuntimeError, match="throughline-telegram-login"):
        TelegramSource(FakeTelethon()).open()


def test_source_pages_and_since_window():
    source = TelegramSource(RecordedClient.load(RECORDED)).open()
    try:
        dialog = next(d for d in source.dialogs() if d.id == 1000002)
        assert [m.id for m in source.page(dialog.entity, 12, 2)] == [13, 14]
        from datetime import datetime, timezone

        start = datetime(2026, 9, 1, 10, 3, tzinfo=timezone.utc)
        assert [m.id for m in source.since(dialog.entity, start, 16, 100)] == [14, 15, 16]
    finally:
        source.close()


def test_missing_credentials(monkeypatch):
    monkeypatch.delenv("TELEGRAM_RECORDING", raising=False)
    monkeypatch.delenv("TELEGRAM_API_ID", raising=False)
    with pytest.raises(ValueError, match="TELEGRAM_API_ID"):
        list(Telegram().records({}, "backfill"))


def test_contract(tmp_path):
    session = "1InventedStringSessionCanary0000000000"
    result = check_plugin(
        "throughline_telegram:Telegram",
        {
            "TELEGRAM_RECORDING": str(RECORDED),
            "TELEGRAM_BATCH_SIZE": "2",
            "TELEGRAM_SESSION": session,
            "TELEGRAM_API_HASH": "invented0hash0canary",
        },
        secrets=[session, "invented0hash0canary"],
    )
    assert result["messages"] == 17
