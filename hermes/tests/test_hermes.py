import copy
import json

import throughline_hermes
from throughline_sdk import Checkpoint, Record
from throughline_sdk.contract import check_plugin

from throughline_hermes import MAPPING_REVISION, Hermes


def plugin(env, source, **settings):
    env.setenv("HERMES_SOURCE", "export:" + str(source))
    for key, value in settings.items():
        env.setenv(key, value)
    return Hermes()


def messages(items):
    return [i for i in items if isinstance(i, dict)]


def links(items):
    return [i.data for i in items if isinstance(i, Record) and i.kind == "link"]


def final_cursor(items):
    return [i for i in items if isinstance(i, Checkpoint)][-1].cursor


def by_id(items):
    return {m["external_id"]: m for m in messages(items)}


def ext(data, session, number):
    return data.external(session, number)


def test_hermes_contract_kit(hermes_env, export_dir, data):
    result = check_plugin(
        "throughline_hermes:Hermes",
        {
            "HERMES_SOURCE": "export:" + str(export_dir),
            "HERMES_BATCH_SIZE": "4",
            "HERMES_OWNER_USER_IDS": "discord:" + data.OWNER_DISCORD,
        },
        # The fetcher drops these before export; the kit proves they never
        # reach the store, the wire or the logs.
        secrets=[data.PROMPT_CANARY, data.REASONING_CANARY],
    )
    total = len(data.RUN1_MESSAGES) + len(data.RUN2_MESSAGES)
    assert result["messages"] == total + len(data.RUN1_SESSIONS) + len(
        data.RUN2_SESSIONS
    )


def test_hermes_channels_threads_and_authors(hermes_env, export_dir, data):
    rows = by_id(plugin(hermes_env, export_dir).records({}, "backfill"))
    first = rows[ext(data, data.SESSION_A, 1)]
    assert first["channel"] == "hermes.messages"
    assert first["happened_at"] == "2024-03-01T10:00:05+00:00"
    assert "subject" not in first
    assert first["thread"] == {
        "external_id": data.SESSION_A,
        "title": "Invented garden plan",
        "metadata": {"kind": "hermes_session", "source": "discord"},
    }
    # The owner's own Discord chat: the owner identity, not the Discord id.
    assert first["author"] == {
        "system": "hermes",
        "kind": "owner",
        "value": "owner",
        "display_name": None,
    }
    assert first["fields"]["platform_message_id"] == "400000000000000004"
    reply = rows[ext(data, data.SESSION_A, 4)]
    assert reply["author"] == {
        "system": "hermes",
        "kind": "agent",
        "value": "hermes",
        "display_name": "Hermes",
    }
    assert reply["content"] == "Plant invented beans after the last frost."
    assert reply["fields"]["reasoning_present"] is True
    assert reply["fields"]["token_count"] == 42
    assert reply["fields"]["model"] == "invented/model-1"
    assert reply["fields"]["mapping_revision"] == MAPPING_REVISION
    # A friend on Telegram: the platform user id as an identifier.
    friend = rows[ext(data, data.SESSION_B, 7)]
    assert friend["author"] == {
        "system": "telegram",
        "kind": "user_id",
        "value": data.FRIEND_TELEGRAM,
        "display_name": "Invented Friend",
    }
    # CLI is an owner surface by default.
    assert rows[ext(data, data.SESSION_D, 12)]["author"]["kind"] == "owner"
    # Multimodal parts become readable text.
    assert (
        rows[ext(data, data.SESSION_A, 5)]["content"]
        == "Here is my invented garden\n[image]"
    )
    undone = rows[ext(data, data.SESSION_D, 13)]
    assert undone["fields"]["visibility"] == "rewound"
    # Cron sessions go to their own channel.
    cron = [m for m in rows.values() if m["thread"]["external_id"] == data.SESSION_C]
    assert {m["channel"] for m in cron} == {"hermes.automation"}
    assert all(m["fields"]["automated"] for m in cron)


def test_hermes_tool_calls_results_and_links(hermes_env, export_dir, data):
    items = list(plugin(hermes_env, export_dir).records({}, "backfill"))
    rows = by_id(items)
    call = rows[ext(data, data.SESSION_A, 2)]
    assert call["content"] == (
        "Let me look that up.\ntool web_search: query=invented bean planting"
    )
    assert call["fields"]["tool_names"] == ["web_search"]
    assert call["fields"]["tool_call_ids"] == ["call_a1"]
    assert call["fields"]["tool_calls"][0]["name"] == "web_search"
    result = rows[ext(data, data.SESSION_A, 3)]
    assert result["content"].startswith(
        "tool web_search result: Invented search result line."
    )
    assert len(result["content"]) < 600 and result["content"].endswith("...")
    assert result["fields"]["tool_output"] == data.LONG_TOOL_OUTPUT.strip()
    assert result["fields"]["tool_output_length"] == len(data.LONG_TOOL_OUTPUT)
    assert result["fields"]["tool_output_truncated"] is False
    assert result["fields"]["tool_call_id"] == "call_a1"
    # A call with no text still has content; two calls are listed in order.
    multi = rows[ext(data, data.SESSION_E, 18)]
    assert multi["content"] == (
        "tool read_file: path=/invented/notes.md\ntool memory: action=read"
    )
    assert (
        rows[ext(data, data.SESSION_E, 20)]["content"]
        == "tool memory result: (no output)"
    )
    found = {
        (link["from"]["external_id"], link["to"]["external_id"])
        for link in links(items)
    }
    assert found == {
        (ext(data, data.SESSION_A, 3), ext(data, data.SESSION_A, 2)),
        (ext(data, data.SESSION_C, 11), ext(data, data.SESSION_C, 10)),
        (ext(data, data.SESSION_E, 19), ext(data, data.SESSION_E, 18)),
        (ext(data, data.SESSION_E, 20), ext(data, data.SESSION_E, 18)),
    }
    assert all(
        link["type"] == "reply_to" and link["method"] == "source"
        for link in links(items)
    )
    cron_link = next(
        link for link in links(items) if link["from"]["external_id"].startswith("cron_")
    )
    assert (
        cron_link["from"]["channel"]
        == cron_link["to"]["channel"]
        == "hermes.automation"
    )


def test_hermes_session_records(hermes_env, export_dir, data):
    rows = by_id(plugin(hermes_env, export_dir).records({}, "backfill"))
    session = rows[data.SESSION_A]
    assert session["channel"] == "hermes.sessions"
    assert session["happened_at"] == "2024-03-01T10:00:00+00:00"
    # The second run's state wins: ended by compression.
    assert session["ended_at"] == "2024-03-05T12:00:00+00:00"
    fields = session["fields"]
    assert fields["end_reason"] == "compression" and fields["message_count"] == 8
    assert fields["owner_session"] is True and fields["automated"] is False
    assert fields["input_tokens"] == 1200 and fields["estimated_cost_usd"] == 0.0123
    assert fields["system_prompt_length"] > 0 and fields["system_prompt"] is None
    assert len(fields["system_prompt_sha256"]) == 64
    assert session["content"].splitlines()[0] == (
        "Hermes session on discord: Invented garden plan"
    )
    assert "End reason: compression" in session["content"]
    cron = rows[data.SESSION_C]
    assert cron["fields"]["automated"] is True and "Automated run" in cron["content"]
    assert rows[data.SESSION_B]["fields"]["user_name"] == "Invented Friend"
    assert rows[data.SESSION_B]["thread"]["external_id"] == "sessions:telegram"


def test_hermes_secrets_never_mapped(hermes_env, export_dir, data):
    items = list(plugin(hermes_env, export_dir).records({}, "backfill"))
    text = json.dumps([i if isinstance(i, dict) else i.model_dump() for i in items])
    assert data.PROMPT_CANARY not in text and data.REASONING_CANARY not in text


def test_hermes_deterministic_and_idempotent(hermes_env, export_dir, data):
    first = list(plugin(hermes_env, export_dir).records({}, "backfill"))
    second = list(plugin(hermes_env, export_dir).records({}, "backfill"))
    assert messages(first) == messages(second)
    assert links(first) == links(second)
    cursor = final_cursor(first)
    assert cursor["mapping_revision"] == MAPPING_REVISION and cursor["config"]
    # A rerun with the cursor maps nothing new.
    again = list(plugin(hermes_env, export_dir).records(copy.deepcopy(cursor), "sync"))
    assert messages(again) == []
    # A mapping setting change replays everything under the new setting.
    changed = list(
        plugin(hermes_env, export_dir, HERMES_AGENT_NAME="Nova").records(cursor, "sync")
    )
    assert len(messages(changed)) == len(messages(first))
    assert by_id(changed)[ext(data, data.SESSION_A, 4)]["author"]["value"] == "nova"


def test_hermes_export_resume_after_crash(hermes_env, export_dir):
    full = list(
        plugin(hermes_env, export_dir, HERMES_BATCH_SIZE="5").records({}, "backfill")
    )
    checkpoints = [i for i in full if isinstance(i, Checkpoint)]
    interim = checkpoints[1].cursor
    assert interim["resume"]["offset"] == 10
    resumed = list(
        plugin(hermes_env, export_dir, HERMES_BATCH_SIZE="5").records(
            interim, "backfill"
        )
    )
    assert messages(resumed) == messages(full)[10:]
    assert final_cursor(resumed) == final_cursor(full)


def test_hermes_batches_by_serialized_payload_bytes(
    hermes_env, export_dir, monkeypatch
):
    monkeypatch.setattr(throughline_hermes, "BATCH_BYTES", 4_000)
    items = list(
        plugin(hermes_env, export_dir, HERMES_BATCH_SIZE="500").records(
            {}, "backfill"
        )
    )
    interim = [i.cursor for i in items if isinstance(i, Checkpoint)][:-1]
    assert interim
    assert interim[0]["resume"]["offset"] < len(messages(items))


def test_hermes_new_export_run_is_incremental(hermes_env, tmp_path, store, data):
    db, conn = store
    folder = tmp_path / "ship"
    folder.mkdir()
    state = {}
    first, _ = data.fetch_run(db, state, mode="backfill")
    data.write_jsonl(folder / "a.jsonl", first)
    items = list(plugin(hermes_env, folder).records({}, "sync"))
    cursor = final_cursor(items)
    data.advance_store(conn)
    second, _ = data.fetch_run(db, state, at="2024-03-11T00:00:00")
    data.write_jsonl(folder / "b.jsonl", second)
    later = by_id(plugin(hermes_env, folder).records(cursor, "sync"))
    assert set(later) == {
        data.SESSION_A,
        data.SESSION_E,
        *(
            ext(data, r["session_id"], r["id"])
            for r in second
            if r["kind"] == "message"
        ),
    }


def test_hermes_live_snapshot_mode(hermes_env, store, data):
    db, conn = store
    hermes_env.setenv("HERMES_DB", str(db))
    hermes_env.setenv("HERMES_READER", "snapshot")
    hermes_env.setenv("HERMES_SOURCE", "live")
    first = Hermes()
    first.fetch_options = {"now": data.clock("2024-03-06T00:00:00")}
    items = list(first.records({}, "backfill"))
    assert len(messages(items)) == len(data.RUN1_MESSAGES) + len(data.RUN1_SESSIONS)
    cursor = final_cursor(items)
    assert cursor["fetch"]["after_id"] == len(data.RUN1_MESSAGES)
    data.advance_store(conn)
    second = Hermes()
    second.fetch_options = {"now": data.clock("2024-03-11T00:00:00")}
    later = list(second.records(cursor, "sync"))
    assert len(
        [m for m in messages(later) if m["channel"] != "hermes.sessions"]
    ) == len(data.RUN2_MESSAGES)
    # Interim checkpoints keep the old watermark: a crash refetches, never skips.
    second = Hermes()
    second.fetch_options = {"now": data.clock("2024-03-11T00:00:00")}
    hermes_env.setenv("HERMES_BATCH_SIZE", "2")
    second = Hermes()
    second.fetch_options = {"now": data.clock("2024-03-11T00:00:00")}
    replay = list(second.records(cursor, "sync"))
    interim = [i.cursor for i in replay if isinstance(i, Checkpoint)][:-1]
    assert interim and all(c["fetch"] == cursor["fetch"] for c in interim)


def test_hermes_uid_survives_rewrite(hermes_env, tmp_path, store, data):
    """Hermes deletes and re-inserts rows on rewrites; the uid keeps the record."""
    db, conn = store
    data.advance_store(conn)
    folder = tmp_path / "ship"
    folder.mkdir()
    state = {}
    first, _ = data.fetch_run(db, state, mode="backfill")
    data.write_jsonl(folder / "a.jsonl", first)
    items = list(plugin(hermes_env, folder).records({}, "backfill"))
    cursor = final_cursor(items)
    old = by_id(items)[ext(data, data.SESSION_E, 18)]
    columns = (
        "session_id",
        "role",
        "content",
        "tool_calls",
        "timestamp",
        "message_uid",
    )
    values = conn.execute(
        f"SELECT {', '.join(columns)} FROM messages WHERE id = 18"
    ).fetchone()
    row = dict(zip(columns, values))
    conn.execute("DELETE FROM messages WHERE id = 18")
    data.insert(conn, "messages", row)
    conn.commit()
    second, _ = data.fetch_run(db, state, at="2024-03-12T00:00:00")
    data.write_jsonl(folder / "b.jsonl", second)
    later = by_id(plugin(hermes_env, folder).records(cursor, "sync"))
    new = later[ext(data, data.SESSION_E, 18)]
    assert new["fields"]["message_id"] == 22 and old["fields"]["message_id"] == 18
    assert new["content"] == old["content"]


def test_hermes_many_tool_calls_bounded(hermes_env, data):
    from throughline_hermes.mapping import Settings, map_message

    calls = [
        {
            "id": f"c{n}",
            "function": {"name": "terminal", "arguments": '{"command": "ls"}'},
        }
        for n in range(60)
    ]
    record = {
        "kind": "message",
        "id": 7,
        "session_id": "s1",
        "payload": {
            "id": 7,
            "role": "assistant",
            "content": None,
            "timestamp": 1.7e9,
            "tool_calls": calls,
        },
    }
    message, link = map_message(record, {"source": "cli"}, Settings())
    assert message["fields"]["tool_call_count"] == 60
    assert len(message["fields"]["tool_calls"]) == 50
    assert message["content"].endswith("... and 10 more tool calls")
    assert link is None


def test_hermes_nested_tool_arguments_escape_nul(hermes_env):
    from throughline_hermes.mapping import Settings, map_message

    record = {
        "kind": "message",
        "id": 1,
        "session_id": "invented-session",
        "payload": {
            "id": 1,
            "role": "assistant",
            "content": "",
            "timestamp": 1.7e9,
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {
                        "name": "invented_tool",
                        "arguments": json.dumps({"value": "a\x00b"}),
                    },
                }
            ],
        },
    }
    message, _ = map_message(record, {}, Settings())
    assert "\x00" not in message["content"]
    assert "a\\u0000b" in message["content"]


def test_hermes_group_chat_is_not_the_owners_own_chat(hermes_env, data):
    from throughline_hermes.mapping import Settings, map_message

    settings = Settings(owner_user_ids=["discord:" + data.OWNER_DISCORD])
    record = {
        "kind": "message",
        "id": 3,
        "session_id": "g1",
        "payload": {"id": 3, "role": "user", "content": "hi all", "timestamp": 1.7e9},
    }
    dm = {"source": "discord", "user_id": data.OWNER_DISCORD, "chat_type": "dm"}
    group = dict(dm, chat_type="group")
    assert map_message(record, dm, settings)[0]["author"]["kind"] == "owner"
    author = map_message(record, group, settings)[0]["author"]
    assert author["system"] == "discord" and author["value"] == data.OWNER_DISCORD
