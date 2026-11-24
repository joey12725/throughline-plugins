import copy
import json
from pathlib import Path

import pytest
from throughline_sdk import Checkpoint
from throughline_beeper import Beeper
from throughline_beeper.mapping import archive_message
from throughline_beeper.sources import Archive, Desktop

FIXTURE = Path(__file__).parent / "recorded" / "archive.json"


@pytest.mark.parametrize(
    "change",
    [
        {"attachments_json": '[{"fileName":"a\\u0000b.png"}]', "body": ""},
        {"reactions_json": '[{"emoji":"x\\u0000"}]'},
        {"sent_at": "2020-01-01T10:00:00"},
        {"edited_at": "2020-01-01 10:00:00"},
        {"sent_at": "", "first_imported_at": "2024-01-01 00:00:00"},
        {"attachments_json": '[{"fileName":'},
        {"attachments_json": "null", "body": ""},
        {"attachments_json": '{"fileName":"x"}', "body": ""},
        {"body": " ".join(f"fakeword{i}" for i in range(400_000))},
    ],
)
def test_plg_4_bad_archive_row_cannot_pin_cursor(change, monkeypatch, tmp_path, caplog):
    data = json.loads(FIXTURE.read_text())
    base = data["messages"][0]
    data["messages"] = [
        dict(
            base,
            id=f"discord:{i:03}",
            conversation_id="fake",
            sent_at=f"2020-01-0{i}T00:00:00Z",
            reply_to="",
        )
        for i in range(1, 7)
    ]
    data["messages"][2].update(change)
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(data))
    monkeypatch.setenv("BEEPER_SOURCE", "archive")
    monkeypatch.setenv("BEEPER_RECORDING", str(path))
    monkeypatch.setenv("BEEPER_BATCH_SIZE", "2")
    plugin = Beeper()
    records = list(plugin.records({}, "backfill"))
    assert {r["external_id"] for r in records if isinstance(r, dict)} >= {
        "discord:004",
        "discord:005",
        "discord:006",
    }
    assert records[-1].cursor["archive"]["done"]
    if "\\u0000" in json.dumps(change) or len(change.get("body", "")) > 100_000:
        assert plugin.quarantined == 1

    if plugin.quarantined:
        assert "discord:003" in caplog.text
        assert "fakeword" not in caplog.text
    # Every acknowledged checkpoint can resume through the bad row.
    for item in records:
        if isinstance(item, Checkpoint):
            assert list(plugin.records(item.cursor, "backfill"))[-1].cursor["archive"][
                "done"
            ]


def test_msg_3_time_raw_precedes_import_time():
    row = json.loads(FIXTURE.read_text())["messages"][0]
    row.update(
        sent_at="",
        time_raw="Mar 3, 2019, 4:15 PM",
        first_imported_at="2026-05-01T10:00:00Z",
    )
    result = archive_message(row)
    assert result["happened_at"].startswith("2019-03-03T16:15:00")
    assert result["fields"]["time_precision"] == "timezone_unknown"


def test_plg_4_composite_order_empty_duplicate_and_resume(tmp_path):
    data = json.loads(FIXTURE.read_text())
    base = data["messages"][0]
    data["messages"] = [
        dict(base, id=i, conversation_id=c, sent_at=t)
        for i, c, t in [
            ("z", "a", "2020-01-01Z"),
            ("a", "a", "2020-01-02Z"),
            ("b", "a", "2020-01-01Z"),
            ("c", "a", ""),
            ("d", "b", ""),
        ]
    ]
    path = tmp_path / "order.json"
    path.write_text(json.dumps(data))
    source = Archive(recording=path)
    rows = [r for batch in source.batches(size=2) for r in batch]
    assert [r["id"] for r in rows] == ["c", "b", "z", "a", "d"]
    for i, row in enumerate(rows):
        assert [
            r for batch in source.batches(source.position(row), 2) for r in batch
        ] == rows[i + 1 :]


@pytest.mark.parametrize("empty_first", [False, True])
def test_plg_2_live_reply_sync_bounded_and_empty_chat_backfills(
    monkeypatch, empty_first
):
    import throughline_beeper

    class Fake:
        pages = Desktop.pages
        rows = []
        calls = []

        def __init__(self, *args):
            pass

        def close(self):
            pass

        def accounts(self):
            return [{"accountID": "fake-account", "network": "discord"}]

        def chats(self):
            return [{"id": "fake-chat", "accountID": "fake-account", "type": "group"}]

        def get(self, path, params):
            self.calls.append(copy.deepcopy(params))
            direction = params.get("direction", "before")
            cursor = params.get("cursor")
            candidates = [
                r
                for r in self.rows
                if cursor is None
                or (
                    int(r["id"]) > int(cursor)
                    if direction == "after"
                    else int(r["id"]) < int(cursor)
                )
            ]
            items = candidates[:20] if direction == "after" else candidates[-20:]
            return {
                "items": items,
                "hasMore": len(candidates) > 20,
                "newestCursor": items[-1]["id"] if items else None,
                "oldestCursor": items[0]["id"] if items else None,
            }

    def row(i):
        return dict(
            id=str(i),
            accountID="fake-account",
            chatID="fake-chat",
            timestamp="2020-01-01T00:00:00Z",
            text="Invented",
            linkedMessageID="1" if i else None,
        )

    monkeypatch.setattr(throughline_beeper, "Desktop", Fake)
    monkeypatch.setenv("BEEPER_SOURCE", "live")
    monkeypatch.setenv("BEEPER_ACCESS_TOKEN", "fake-token")
    plugin = Beeper()
    Fake.rows = [] if empty_first else [row(i) for i in range(5000)]
    first = list(plugin.records({}, "sync"))
    Fake.calls.clear()
    Fake.rows = [row(i) for i in range(50 if empty_first else 5001)]
    second = list(plugin.records(first[-1].cursor, "sync"))
    messages = [r for r in second if isinstance(r, dict)]
    assert len(messages) == (50 if empty_first else 1)
    assert len(Fake.calls) == (3 if empty_first else 1)


class FakeDesktop:
    """Invented Desktop API: chats_rows maps a chat id to rows, oldest first."""

    pages = Desktop.pages
    chats_rows = {}
    calls = []

    def __init__(self, *args):
        pass

    def close(self):
        pass

    def accounts(self):
        return [{"accountID": "fake-account", "network": "discord"}]

    def chats(self):
        return [
            {"id": c, "accountID": "fake-account", "type": "group"}
            for c in self.chats_rows
        ]

    def get(self, path, params):
        from urllib.parse import unquote

        self.calls.append(copy.deepcopy(params))
        rows = self.chats_rows[unquote(path.split("/")[2])]
        direction = params.get("direction", "before")
        cursor = params.get("cursor")
        candidates = [
            r
            for r in rows
            if cursor is None
            or (
                int(r["id"]) > int(cursor)
                if direction == "after"
                else int(r["id"]) < int(cursor)
            )
        ]
        items = candidates[:20] if direction == "after" else candidates[-20:]
        return {
            "items": items,
            "hasMore": len(candidates) > 20,
            "newestCursor": items[-1]["id"] if items else None,
            "oldestCursor": items[0]["id"] if items else None,
        }


def live_row(chat, i, reply=None, text="Invented"):
    return dict(
        id=str(i),
        accountID="fake-account",
        chatID=chat,
        timestamp="2020-01-01T00:00:00Z",
        text=text,
        linkedMessageID=reply,
    )


def live_plugin(monkeypatch, chats):
    import throughline_beeper

    FakeDesktop.chats_rows = chats
    FakeDesktop.calls = []
    monkeypatch.setattr(throughline_beeper, "Desktop", FakeDesktop)
    monkeypatch.setenv("BEEPER_SOURCE", "live")
    monkeypatch.setenv("BEEPER_ACCESS_TOKEN", "fake-token")
    monkeypatch.delenv("BEEPER_RECORDING", raising=False)
    return Beeper()


def link_pairs(records):
    return [
        (r.data["from"]["external_id"], r.data["to"]["external_id"])
        for r in records
        if getattr(r, "kind", None) == "link"
    ]


def test_live_missing_reply_target_never_blocks_sync(monkeypatch, retry_core_role_race):
    """A reply to a deleted, unbridged or quarantined message drops only its link."""
    from throughline_sdk import Client
    from throughline_sdk._server import core_server

    chats = {
        "fake-a": [
            live_row("fake-a", 1),
            live_row("fake-a", 2, reply="1"),
            live_row("fake-a", 3, reply="999"),  # never returned by the source
            live_row("fake-a", 4, text="bad\x00row"),  # quarantined by the plugin
            live_row("fake-a", 5, reply="4"),
            live_row("fake-a", 6, reply="2"),
        ],
        "fake-b": [live_row("fake-b", 1), live_row("fake-b", 2, reply="1")],
    }
    plugin = live_plugin(monkeypatch, chats)

    def ext(chat, i):
        return f"beeper:fake-account:{chat}:{i}"

    with core_server() as (_, env):
        client = Client(env["THROUGHLINE_URL"], env["THROUGHLINE_TOKEN"], "beeper")

        def links():
            statement = (
                "SELECT f.external_id src, t.external_id dst FROM api.links l"
                " JOIN api.messages f ON f.id=l.from_id"
                " JOIN api.messages t ON t.id=l.to_id"
                " WHERE l.type='reply_to' AND l.status='active'"
            )
            rows = client.request("POST", "sql", {"statement": statement})["rows"]
            return sorted((r["src"], r["dst"]) for r in rows)

        try:
            result = plugin.execute(client, "sync")
            assert result["unlinked"] == 2
            assert result["quarantined"] == 1
            # Valid links of the same chat survive, and the later chat syncs.
            assert links() == [
                (ext("fake-a", 2), ext("fake-a", 1)),
                (ext("fake-a", 6), ext("fake-a", 2)),
                (ext("fake-b", 2), ext("fake-b", 1)),
            ]
            cursor = client.cursor()
            assert all("replies" not in s for s in cursor["live"].values())
            assert cursor["live"]["fake-b"]["complete"]
            # The dropped links are not replayed, and a new one still works.
            chats["fake-a"].append(live_row("fake-a", 7, reply="998"))
            chats["fake-b"].append(live_row("fake-b", 3, reply="2"))
            again = plugin.execute(client, "sync")
            assert again["unlinked"] == 1
            assert sum(again["counts"].values()) == 2
            assert (ext("fake-b", 3), ext("fake-b", 2)) in links()
            assert plugin.execute(client, "sync")["unlinked"] == 0
        finally:
            client.close()


def test_live_link_errors_other_than_missing_message_still_stop(monkeypatch):
    import httpx

    plugin = live_plugin(
        monkeypatch, {"fake-a": [live_row("fake-a", 1), live_row("fake-a", 2, "1")]}
    )

    class Refusing:
        def cursor(self):
            return {}

        def register(self, manifest):
            pass

        def ingest(self, messages, *, links=None, dry_run=False):
            if links:
                request = httpx.Request("POST", "http://core.invalid/v1/ingest")
                response = httpx.Response(
                    400, json={"detail": "unsupported link"}, request=request
                )
                raise httpx.HTTPStatusError("x", request=request, response=response)

        def checkpoint(self, cursor):
            # The cursor that would forget the pending reply is never written.
            assert cursor["live"]["fake-a"]["replies"]

    with pytest.raises(httpx.HTTPStatusError):
        plugin.execute(Refusing(), "sync")


def test_live_pending_replies_in_cursor_stay_bounded(monkeypatch):
    """Backfill of a reply-heavy chat must not carry every reply in each cursor."""
    rows = [live_row("fake-a", i, reply=str(i - 1) if i else None) for i in range(400)]
    rows[200]["linkedMessageID"] = "absent"
    plugin = live_plugin(monkeypatch, {"fake-a": rows})
    records = list(plugin.records({}, "backfill"))
    sizes = [
        len(r.cursor["live"]["fake-a"].get("replies", []))
        for r in records
        if isinstance(r, Checkpoint)
    ]
    assert len(sizes) == 21 and max(sizes) <= 41 and sizes[-1] == 0
    assert len(link_pairs(records)) == len(set(link_pairs(records))) == 399
    # A link is only sent after a checkpoint acknowledged both of its ends,
    # apart from the leftovers flushed at the end of the chat.
    acknowledged, page = set(), set()
    for r in records:
        if isinstance(r, dict):
            page.add(r["external_id"])
        elif isinstance(r, Checkpoint):
            acknowledged |= page
            page.clear()
        elif r.kind == "link" and not r.data["to"]["external_id"].endswith(":absent"):
            assert r.data["from"]["external_id"] in acknowledged
            assert r.data["to"]["external_id"] in acknowledged
    # Resuming from any checkpoint never loses a pending reply.
    for index, item in enumerate(records):
        if isinstance(item, Checkpoint):
            resumed = list(plugin.records(item.cursor, "backfill"))
            assert len(set(link_pairs(records[:index] + resumed))) == 399


def test_msg_3_undated_rows_take_time_from_conversation_neighbour(
    monkeypatch, tmp_path, synthetic_archive
):
    import psycopg

    data = json.loads(FIXTURE.read_text())
    base = dict(
        data["messages"][0],
        reply_to="",
        time_raw="",
        time_precision="unknown",
        first_imported_at="2026-05-01T10:00:00Z",
    )
    data["messages"] = [
        dict(base, id=i, conversation_id=c, sent_at=t)
        for i, c, t in [
            ("m1", "fake-dated", ""),
            ("m2", "fake-dated", "2019-03-04T09:00:00+02:00"),
            ("m3", "fake-dated", "2019-03-03T08:00:00+02:00"),
            ("m4", "fake-dated", ""),
            ("m5", "fake-undated", ""),
            ("m6", "fake-other", "2018-01-01T00:00:00Z"),
        ]
    ]
    path = tmp_path / "undated.json"
    path.write_text(json.dumps(data))
    monkeypatch.setenv("BEEPER_SOURCE", "archive")
    monkeypatch.setenv("BEEPER_BATCH_SIZE", "1")

    def check(records):
        got = {r["external_id"]: r for r in records if isinstance(r, dict)}
        for mid in ("m1", "m4"):
            assert got[mid]["happened_at"] == "2019-03-03T08:00:00+02:00"
            assert got[mid]["fields"]["time_precision"] == "inferred"
            assert got[mid]["fields"]["time_inferred_from"] == {
                "method": "next_in_conversation",
                "message": "m3",
                "sent_at": "2019-03-03T08:00:00+02:00",
            }
            assert not got[mid]["payload"]["sent_at"]
        # No dated row anywhere in the conversation: import time, flagged.
        assert got["m5"]["happened_at"].startswith("2026-05-01T10:00:00")
        assert got["m5"]["fields"]["time_precision"] == "unknown"
        assert got["m5"]["fields"]["time_inferred_from"] == {
            "method": "first_imported_at"
        }
        assert "time_inferred_from" not in got["m3"]["fields"]
        assert got["m3"]["fields"]["time_precision"] == "unknown"

    monkeypatch.setenv("BEEPER_RECORDING", str(path))
    plugin = Beeper()
    full = list(plugin.records({}, "backfill"))
    check(full)
    # Resuming after the first undated row still finds the neighbour.
    first = next(r for r in full if isinstance(r, Checkpoint))
    resumed = [
        r for r in plugin.records(first.cursor, "backfill") if isinstance(r, dict)
    ]
    assert resumed[0]["external_id"] == "m4"
    assert resumed[0]["fields"]["time_precision"] == "inferred"

    # The SQL path gives the same answer as the fixture path.
    with psycopg.connect(synthetic_archive) as conn:
        columns = [
            d.name
            for d in conn.execute("SELECT * FROM archive.messages LIMIT 0").description
        ]
        conn.execute("DELETE FROM archive.messages")
        for row in data["messages"]:
            conn.execute(
                "INSERT INTO archive.messages VALUES ("
                + ",".join(["%s"] * len(columns))
                + ")",
                [row.get(c) for c in columns],
            )
        conn.execute(
            "UPDATE archive.messages SET sent_at=NULL, conversation_id=NULL WHERE id='m5'"
        )
    monkeypatch.delenv("BEEPER_RECORDING")
    monkeypatch.setenv("BEEPER_ARCHIVE_DSN", synthetic_archive)
    check(list(Beeper().records({}, "backfill")))


def test_dated_messages_keep_their_pre_inference_fields():
    """A null time_inferred_from would re-version every already-imported message."""
    from throughline_beeper.mapping import live_message

    row = dict(
        json.loads(FIXTURE.read_text())["messages"][0],
        sent_at="2020-01-01T10:00:00+00:00",
        time_precision="exact",
    )
    archived = archive_message(row)["fields"]
    assert sorted(archived) == [
        "attachments",
        "conversation_kind",
        "direction",
        "edited_at",
        "guild_id",
        "identity_quality",
        "reactions",
        "reply_to",
        "time_precision",
    ]
    assert archived["time_precision"] == "exact"
    live = live_message(
        {
            "id": "1",
            "accountID": "fake-account",
            "chatID": "fake-chat",
            "timestamp": "2020-01-01T10:00:00+00:00",
            "text": "synthetic",
            "senderID": "@fake:example.invalid",
            "isSender": False,
        },
        {"id": "fake-chat", "type": "single", "title": "Fake"},
        {"network": "signal"},
    )["fields"]
    assert live == {
        "direction": "incoming",
        "time_precision": "exact",
        "reply_to": None,
        "edited_at": None,
        "attachments": [],
        "reactions": [],
        "identity_quality": "beeper_id",
        "guild_id": None,
        "conversation_kind": "dm",
    }


def test_undated_row_without_conversation_gets_no_unrelated_neighbour(
    monkeypatch, tmp_path, synthetic_archive
):
    import psycopg

    data = json.loads(FIXTURE.read_text())
    base = dict(
        data["messages"][0],
        reply_to="",
        time_raw="",
        time_precision="unknown",
        first_imported_at="2026-05-01T10:00:00Z",
    )
    data["messages"] = [
        dict(base, id=i, conversation_id=c, sent_at=t)
        for i, c, t in [
            ("n1", "", ""),
            ("n2", "", "2011-01-01T00:00:00Z"),
            ("n3", None, ""),
            ("n4", None, "2012-01-01T00:00:00Z"),
        ]
    ]
    path = tmp_path / "loose.json"
    path.write_text(json.dumps(data))
    monkeypatch.setenv("BEEPER_SOURCE", "archive")

    def check(source, records):
        assert source.first_dated() == {}
        got = {r["external_id"]: r for r in records if isinstance(r, dict)}
        for mid in ("n1", "n3"):
            assert got[mid]["happened_at"].startswith("2026-05-01T10:00:00")
            assert got[mid]["fields"]["time_precision"] == "unknown"
            assert got[mid]["fields"]["time_inferred_from"] == {
                "method": "first_imported_at"
            }

    monkeypatch.setenv("BEEPER_RECORDING", str(path))
    check(Archive("", str(path)), list(Beeper().records({}, "backfill")))

    with psycopg.connect(synthetic_archive) as conn:
        columns = [
            d.name
            for d in conn.execute("SELECT * FROM archive.messages LIMIT 0").description
        ]
        conn.execute("DELETE FROM archive.messages")
        for row in data["messages"]:
            conn.execute(
                "INSERT INTO archive.messages VALUES ("
                + ",".join(["%s"] * len(columns))
                + ")",
                [row.get(c) for c in columns],
            )
    monkeypatch.delenv("BEEPER_RECORDING")
    monkeypatch.setenv("BEEPER_ARCHIVE_DSN", synthetic_archive)
    check(Archive(synthetic_archive, None), list(Beeper().records({}, "backfill")))


def test_plg_4_bare_string_reaction_is_kept_not_quarantined():
    row = json.loads(FIXTURE.read_text())["messages"][0]
    row.update(reactions_json=json.dumps("\u2764\ufe0f"))
    assert archive_message(row)["fields"]["reactions"] == [{"emoji": "\u2764\ufe0f"}]
    row.update(reactions_json=json.dumps(""))
    assert archive_message(row)["fields"]["reactions"] == []
    row.update(reactions_json=json.dumps({"emoji": "x"}))
    with pytest.raises(ValueError):
        archive_message(row)
