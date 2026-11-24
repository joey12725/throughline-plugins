import copy
import json
import subprocess
from pathlib import Path

import httpx
import psycopg
import pytest
from throughline_sdk import Checkpoint
from throughline_sdk.contract import check_plugin
from throughline_beeper import Beeper
from throughline_beeper.mapping import archive_message, live_id, live_message, network
from throughline_beeper.sources import Archive, Desktop

RECORDINGS = Path(__file__).parent / "recorded"


def configured(monkeypatch, source, recording=True):
    monkeypatch.setenv("BEEPER_SOURCE", source)
    monkeypatch.setenv("BEEPER_BATCH_SIZE", "4")
    if recording:
        monkeypatch.setenv("BEEPER_RECORDING", str(RECORDINGS / (source + ".json")))
    else:
        monkeypatch.delenv("BEEPER_RECORDING", raising=False)
    return Beeper()


def row():
    return json.loads((RECORDINGS / "archive.json").read_text())["messages"][0]


def test_msg_1_msg_6_archive_text_json_and_attachment_only():
    original = row()
    mapped = archive_message(
        original,
        {"kind": "channel", "title": "Invented", "guild_id": "fake-guild"},
        "web_sender_fbid",
    )
    assert mapped["content"] == "[attachment: fake-photo.jpg]"
    assert mapped["payload"] == original
    assert mapped["fields"]["attachments"] == json.loads(original["attachments_json"])
    assert mapped["author"]["kind"] == "web_sender_fbid"
    assert mapped["fields"]["guild_id"] == "fake-guild"
    assert mapped["thread"]["metadata"]["kind"] == "channel"
    assert mapped["images"] == []


@pytest.mark.parametrize("body", [None, "", "   "])
def test_msg_1_empty_body_without_attachments(body):
    original = row()
    original.update(body=body, attachments_json="", sender_id="")
    result = archive_message(original)
    assert result["content"] == "[empty chat message]"
    assert result["author"] is None


def test_msg_2_native_id_is_never_a_key():
    a, b = row(), row()
    a.update(id="discord:fake-a", native_id="same")
    b.update(id="discord:fake-b", native_id="same")
    assert archive_message(a)["external_id"] != archive_message(b)["external_id"]
    a["native_id"] = ""
    assert archive_message(a)["external_id"] == a["id"]


def test_msg_3_missing_sent_at_offset_and_unknown_direction():
    original = row()
    original.update(
        sent_at="", direction="unknown", first_imported_at="2020-01-01T12:00:00-05:00"
    )
    mapped = archive_message(original)
    assert mapped["happened_at"].endswith("-05:00")
    assert mapped["fields"]["time_precision"] == "unknown"
    assert mapped["fields"]["direction"] == "unknown"
    original["first_imported_at"] = ""
    with pytest.raises(ValueError, match="lacks"):
        archive_message(original)


def test_msg_5_edited_reactions_identity_defaults_and_precision():
    original = row()
    original.update(
        platform="sms",
        time_precision="displayed_block",
        edited_at="2020-02-02T12:00:00Z",
        reactions_json='[{"emoji":"fake"}]',
    )
    mapped = archive_message(original)
    assert mapped["author"]["kind"] == "phone"
    assert mapped["fields"]["time_precision"] == "displayed_block"
    assert mapped["fields"]["edited_at"] == "2020-02-02T12:00:00+00:00"
    assert mapped["fields"]["reactions"] == [{"emoji": "fake"}]


def test_msg_1_live_shape_and_collision_safe_ids():
    data = json.loads((RECORDINGS / "live.json").read_text())
    page = data["responses"][0]["body"]
    original = copy.deepcopy(page["items"][0])
    original.update(text="", attachments=[{"filename": "fake.bin"}])
    mapped = live_message(
        original, {"id": "fake-chat-discord", "type": "group"}, {"network": "Discord"}
    )
    assert mapped["content"] == "[attachment: fake.bin]"
    assert mapped["fields"]["conversation_kind"] == "group_dm"
    assert mapped["payload"] == original
    assert mapped["fields"]["reply_to"] == live_id(
        original["accountID"], original["chatID"], original["linkedMessageID"]
    )
    assert live_id("a:b", "c", "d") != live_id("a", "b:c", "d")
    assert network("Google Messages") == "sms"
    assert network("New Network!") == "new_network"


@pytest.mark.parametrize("source", ["live", "archive"])
def test_plg_6_contract_both_sources(source, retry_core_role_race):
    try:
        check_plugin(
            "throughline_beeper:Beeper",
            {
                "BEEPER_SOURCE": source,
                "BEEPER_RECORDING": str(RECORDINGS / (source + ".json")),
                "BEEPER_BATCH_SIZE": "4",
                "BEEPER_ACCESS_TOKEN": "fake-contract-canary-beeper",
            },
            secrets=["fake-contract-canary-beeper"],
        )
    except subprocess.CalledProcessError as error:
        # The kit normally hides captured migration stderr. Surface only the
        # tail from its disposable synthetic database, never its environment.
        stderr = error.stderr or b""
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        pytest.fail(
            "disposable core subprocess failed: " + stderr[-2000:], pytrace=False
        )


def test_plg_4_real_postgres_stream_resume_and_plg_5_aliases(
    monkeypatch, synthetic_archive
):
    monkeypatch.setenv("BEEPER_ARCHIVE_DSN", synthetic_archive)
    plugin = configured(monkeypatch, "archive", False)
    full = list(plugin.records({}, "backfill"))
    messages = [x for x in full if isinstance(x, dict)]
    assert len(messages) == 30
    for index, item in enumerate(full):
        if isinstance(item, Checkpoint):
            resumed = list(plugin.records(item.cursor, "backfill"))
            prefix = [x for x in full[:index] if isinstance(x, dict)]
            suffix = [x for x in resumed if isinstance(x, dict)]
            if not item.cursor["archive"].get("done"):
                assert prefix + suffix == messages
    source = Archive(synthetic_archive)
    try:
        assert [
            r["id"] for b in source.batches("discord:fake-2", 2) for r in b
        ] == sorted(
            r["external_id"] for r in messages if r["external_id"] > "discord:fake-2"
        )
    finally:
        source.close()
    with psycopg.connect(synthetic_archive) as conn:
        conn.execute(
            "INSERT INTO archive.message_aliases VALUES ('discord','fake-alias','discord:fake-5')"
        )
        conn.execute(
            "UPDATE archive.messages SET reply_to='fake-alias' WHERE id='discord:fake-0'"
        )
    links = [
        x.data
        for x in plugin.records({}, "sync")
        if hasattr(x, "kind") and x.kind == "link"
    ]
    assert any(x["to"]["external_id"] == "discord:fake-5" for x in links)


def test_plg_4_live_resume_at_every_checkpoint(monkeypatch):
    plugin = configured(monkeypatch, "live")
    full = list(plugin.records({}, "backfill"))
    expected = {x["external_id"] for x in full if isinstance(x, dict)}
    for index, item in enumerate(full):
        if isinstance(item, Checkpoint):
            resumed = list(plugin.records(item.cursor, "backfill"))
            actual = {
                x["external_id"] for x in full[:index] + resumed if isinstance(x, dict)
            }
            assert actual == expected


def test_plg_2_incremental_uses_opaque_after_cursor(monkeypatch):
    plugin = configured(monkeypatch, "live")
    last = list(plugin.records({}, "backfill"))[-1]
    result = list(plugin.records(last.cursor, "sync"))
    assert not any(isinstance(x, dict) for x in result)


def test_plg_4_incremental_multi_page_resume(monkeypatch, tmp_path):
    plugin = configured(monkeypatch, "live")
    last = list(plugin.records({}, "backfill"))[-1]
    data = json.loads((RECORDINGS / "live.json").read_text())
    response = next(
        r
        for r in data["responses"]
        if r["path"] == "v1/chats/fake-chat-discord/messages"
        and r["params"] == {"cursor": "fake-head", "direction": "after"}
    )
    new_row = dict(
        next(
            r["body"]["items"][0]
            for r in data["responses"]
            if r["path"] == response["path"] and not r["params"]
        )
    )
    new_row.update(
        id="fake-new-1", timestamp="2020-03-01T00:00:00Z", linkedMessageID=None
    )
    response["body"] = dict(
        items=[new_row],
        hasMore=True,
        newestCursor="fake-after-1",
        oldestCursor="fake-head",
    )
    data["responses"].append(
        dict(
            path=response["path"],
            params={"cursor": "fake-after-1", "direction": "after"},
            body=dict(
                items=[dict(new_row, id="fake-new-2")],
                hasMore=False,
                newestCursor="fake-after-2",
                oldestCursor="fake-after-1",
            ),
        )
    )
    path = tmp_path / "invented-incremental.json"
    path.write_text(json.dumps(data))
    monkeypatch.setenv("BEEPER_RECORDING", str(path))
    plugin = Beeper()
    result = list(plugin.records(last.cursor, "sync"))
    messages = [r for r in result if isinstance(r, dict)]
    assert len(messages) == 2
    first_ack = next(r for r in result if isinstance(r, Checkpoint))
    resumed = [
        r for r in plugin.records(first_ack.cursor, "sync") if isinstance(r, dict)
    ]
    assert resumed == messages[1:]


def test_plg_5_unresolved_ambiguous_cross_network_replies(
    monkeypatch, synthetic_archive
):
    monkeypatch.setenv("BEEPER_ARCHIVE_DSN", synthetic_archive)
    plugin = configured(monkeypatch, "archive", False)
    with psycopg.connect(synthetic_archive) as conn:
        conn.execute(
            "INSERT INTO archive.message_aliases VALUES ('discord','fake-ambiguous','discord:fake-4'),('discord','fake-ambiguous','discord:fake-5')"
        )
        conn.execute(
            "UPDATE archive.messages SET reply_to='fake-ambiguous' WHERE id='discord:fake-0'"
        )
        conn.execute(
            "UPDATE archive.messages SET reply_to='sms:fake-5' WHERE id='discord:fake-1'"
        )
    result = list(plugin.records({}, "backfill"))
    assert not any(
        r.data["from"]["external_id"] in {"discord:fake-0", "discord:fake-1"}
        for r in result
        if getattr(r, "kind", None) == "link"
    )
    assert all(
        r["fields"]["reply_to"] is None
        for r in result
        if isinstance(r, dict)
        and r["external_id"] in {"discord:fake-0", "discord:fake-1"}
    )


def test_plg_4_rejected_batch_never_checkpoints(monkeypatch):
    plugin = configured(monkeypatch, "archive")

    class RejectedClient:
        def cursor(self):
            return {}

        def register(self, manifest):
            pass

        def ingest(self, *args, **kwargs):
            raise ValueError("invented rejected batch")

        def checkpoint(self, cursor):
            pytest.fail("checkpoint advanced after rejection")

    with pytest.raises(ValueError, match="rejected"):
        plugin.execute(RejectedClient(), "backfill")


def test_msg_5_dynamic_network_registration(monkeypatch, tmp_path):
    data = json.loads((RECORDINGS / "archive.json").read_text())
    min(data["messages"], key=Archive.position)["platform"] = "Invented New Network"
    path = tmp_path / "invented-network.json"
    path.write_text(json.dumps(data))
    plugin = configured(monkeypatch, "archive")
    plugin.settings["recording"] = str(path)
    first = next(plugin.records({}, "backfill"))
    assert first["channel"] == "chat.invented_new_network"
    assert first["channel"] in {c.name for c in plugin.manifest.channels}


def test_plg_6_retry_and_auth_without_secret_in_errors(monkeypatch):
    monkeypatch.setattr("throughline_beeper.sources.time.sleep", lambda _: None)
    requests = []

    def handler(request):
        requests.append(request)
        return (
            httpx.Response(429, headers={"Retry-After": "0"})
            if len(requests) < 3
            else httpx.Response(200, json=[])
        )

    source = Desktop(
        "http://fake.invalid",
        "fake-secret-token",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert source.accounts() == []
        assert len(requests) == 3
        assert requests[0].headers["Authorization"] == "Bearer fake-secret-token"
    finally:
        source.close()
