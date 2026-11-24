"""The email plugin: export and live sources, cursors, and the SDK contract kit."""

import json
import subprocess
import sys

import pytest
from email_fakes import API_KEY, gmail_message, live_env
from throughline_email import Email, fetch
from throughline_sdk import Checkpoint
from throughline_sdk.contract import check_plugin


def messages(items):
    return [x for x in items if isinstance(x, dict)]


def keys(items):
    return [m["external_id"] for m in messages(items)]


def write_export(path, fake, state=None, **extra):
    """Run the real fetcher in-process and write its records as an export."""
    fetcher = fetch.Fetcher(fetch.Config.from_env(live_env(fake, **extra)), state)
    final = None
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for event, item in fetcher.run():
            if event == "record":
                handle.write(json.dumps(item, sort_keys=True) + "\n")
            else:
                final = item
    return final


def plugin(monkeypatch, **env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Email()


def test_manifest_shape():
    manifest = Email.manifest
    assert manifest.name == "email"
    (channel,) = manifest.channels
    assert channel.name == "email.message" and channel.conversation_whole_thread
    assert not channel.is_reference
    assert {"api_key", "mcp_url", "mcp_token"} == set(manifest.secrets)
    assert manifest.settings["source"].env == "EMAIL_SOURCE"
    assert all(not d.required for d in manifest.settings.values())
    dumped = manifest.model_dump_json()
    assert API_KEY not in dumped


def test_source_setting_is_validated(monkeypatch):
    with pytest.raises(ValueError, match="EMAIL_SOURCE"):
        list(plugin(monkeypatch, EMAIL_SOURCE="imap:x").records({}, "backfill"))
    with pytest.raises(ValueError, match="does not exist"):
        list(
            plugin(monkeypatch, EMAIL_SOURCE="export:/no/such/place").records(
                {}, "backfill"
            )
        )


def test_live_backfill_then_incremental(monkeypatch, composio):
    email = plugin(monkeypatch, **live_env(composio))
    items = list(email.records({}, "backfill"))
    assert keys(items) == [
        "alpha@example.invalid:a4",
        "alpha@example.invalid:a3",
        "alpha@example.invalid:a2",
        "alpha@example.invalid:a1",
        "beta@example.invalid:b3",
        "beta@example.invalid:b2",
        "beta@example.invalid:b1",
    ]
    checkpoints = [x for x in items if isinstance(x, Checkpoint)]
    assert len(checkpoints) >= 6
    cursor = checkpoints[-1].cursor
    assert set(cursor) == {"live"}
    idle = list(email.records(cursor, "sync"))
    assert messages(idle) == []
    assert idle[-1].cursor["live"]["accounts"] == cursor["live"]["accounts"]
    composio.relabel("ca_alpha", "a2", ["SENT", "STARRED"])
    items = list(email.records(cursor, "sync"))
    (changed,) = messages(items)
    assert changed["external_id"] == "alpha@example.invalid:a2"
    assert changed["fields"]["is_starred"] is True
    # Resuming from any checkpoint of the backfill never loses a message.
    all_items = list(email.records({}, "backfill"))
    for index, item in enumerate(all_items):
        if isinstance(item, Checkpoint):
            before = keys(all_items[:index])
            after = keys(email.records(item.cursor, "backfill"))
            assert set(before) | set(after) >= set(keys(all_items))


def test_export_file_directory_dedupe_and_new_files(monkeypatch, composio, tmp_path):
    folder = tmp_path / "exports"
    folder.mkdir()
    state = write_export(folder / "2021-03-10.jsonl", composio)
    email = plugin(monkeypatch, EMAIL_SOURCE=f"export:{folder}", EMAIL_EXPORT_BATCH="2")
    first = list(email.records({}, "backfill"))
    assert len(messages(first)) == 7
    cursor = first[-1].cursor
    assert list(cursor["export"]["files"]) == ["2021-03-10.jsonl"]
    assert "pending" not in cursor["export"]
    assert messages(email.records(cursor, "sync")) == []
    # A later export: one new message and one relabelled message.
    composio.add(
        "ca_beta",
        gmail_message(
            "b4", "tx", "2021-03-08T08:00:00+00:00", "Sam Sample <sam@example.invalid>",
            "beta@example.invalid", "Re: Book club", plain="Chapter two.",
        ),
    )  # fmt: skip
    composio.relabel("ca_alpha", "a1", ["INBOX", "STARRED"])
    (folder / "nested").mkdir()
    write_export(folder / "nested" / "2021-03-11.jsonl", composio, state)
    later = list(email.records(cursor, "sync"))
    assert sorted(keys(later)) == [
        "alpha@example.invalid:a1",
        "beta@example.invalid:b4",
    ]
    assert next(m for m in messages(later) if m["external_id"].endswith("a1"))[
        "fields"
    ]["is_starred"]
    final = later[-1].cursor
    assert sorted(final["export"]["files"]) == [
        "2021-03-10.jsonl",
        "nested/2021-03-11.jsonl",
    ]
    # A fresh import of both files gives each message once, at its latest state.
    fresh = messages(email.records({}, "backfill"))
    assert len(fresh) == 8
    a1 = next(m for m in fresh if m["external_id"].endswith(":a1"))
    assert a1["fields"]["is_starred"] is True
    assert messages(email.records(final, "sync")) == []
    # A single file works as a source too.
    single = plugin(monkeypatch, EMAIL_SOURCE=f"export:{folder / '2021-03-10.jsonl'}")
    assert len(messages(single.records({}, "backfill"))) == 7


def test_export_resume_from_every_checkpoint(monkeypatch, composio, tmp_path):
    path = tmp_path / "e.jsonl"
    write_export(path, composio)
    email = plugin(monkeypatch, EMAIL_SOURCE=f"export:{path}", EMAIL_EXPORT_BATCH="2")
    items = list(email.records({}, "backfill"))
    all_keys = keys(items)
    for index, item in enumerate(items[:-1]):
        if isinstance(item, Checkpoint):
            done = keys(items[:index])
            assert keys(email.records(item.cursor, "backfill")) == all_keys[len(done) :]


def test_export_rejects_malformed_lines(monkeypatch, tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text('{"kind": "export.meta"}\nnot json\n', encoding="utf-8")
    email = plugin(monkeypatch, EMAIL_SOURCE=f"export:{path}")
    with pytest.raises(ValueError, match="line 2 is not JSON"):
        list(email.records({}, "backfill"))


def test_export_reads_gzip(monkeypatch, composio, tmp_path):
    import gzip

    plain = tmp_path / "e.jsonl"
    write_export(plain, composio, EMAIL_ACCOUNTS="ca_beta")
    packed = tmp_path / "packed"
    packed.mkdir()
    with gzip.open(packed / "e.jsonl.gz", "wt", encoding="utf-8") as handle:
        handle.write(plain.read_text(encoding="utf-8"))
    email = plugin(monkeypatch, EMAIL_SOURCE=f"export:{packed}")
    assert len(messages(email.records({}, "backfill"))) == 3


def test_manifest_command_runs():
    output = subprocess.check_output(
        [
            sys.executable,
            "-c",
            "from throughline_email import main; main()",
            "manifest",
        ],
        text=True,
    )
    assert json.loads(output)["channels"][0]["name"] == "email.message"


def test_contract_export(composio, tmp_path):
    folder = tmp_path / "exports"
    folder.mkdir()
    write_export(folder / "one.jsonl", composio)
    result = check_plugin(
        "throughline_email:Email",
        {
            "EMAIL_SOURCE": f"export:{folder}",
            "EMAIL_EXPORT_BATCH": "2",
            "COMPOSIO_API_KEY": "fake-export-canary",
        },
        secrets=["fake-export-canary"],
    )
    assert result["messages"] == 7


def test_contract_live(composio):
    env = live_env(composio)
    result = check_plugin("throughline_email:Email", env, secrets=[API_KEY])
    assert result["messages"] == 7


def test_export_latest_fetch_wins_by_instant_not_text(monkeypatch, tmp_path):
    from throughline_email import fetched_rank

    # Text order would put "...00Z" after "...00.500000Z"; the instant does not.
    assert fetched_rank({"fetched_at": "2021-03-10T12:00:00Z"}) < fetched_rank(
        {"fetched_at": "2021-03-10T12:00:00.500000Z"}
    )
    assert fetched_rank({"fetched_at": "2021-03-10T13:00:00+01:00"}) == fetched_rank(
        {"fetched_at": "2021-03-10T12:00:00Z"}
    )
    assert fetched_rank({"fetched_at": "garbage"}) < fetched_rank(
        {"fetched_at": "2000-01-01T00:00:00Z"}
    )
    old = gmail_message(
        "m1", "t1", "2021-03-01T08:00:00+00:00", "x@example.invalid",
        "box@example.invalid", "Hello", plain="Hi.", labels=["INBOX"],
    )  # fmt: skip
    new = json.loads(json.dumps(old))
    new["labelIds"] = ["INBOX", "STARRED"]

    def line(raw, at):
        return json.dumps(
            {"kind": "gmail.message", "account": "box@example.invalid",
             "account_ref": "ca_box", "id": "m1", "thread_id": "t1",
             "fetched_at": at, "labels": {}, "raw": raw}
        )  # fmt: skip

    # Text order ranks "12:00:01Z" above "12:00:01.500000Z" (Z sorts after .)
    # and file order favours b.jsonl; only the instant picks the newer state.
    newer, older = "2021-03-10T12:00:01.500000Z", "2021-03-10T12:00:01Z"
    (tmp_path / "a.jsonl").write_text(line(new, newer) + "\n")
    (tmp_path / "b.jsonl").write_text(line(old, older) + "\n")
    email = plugin(monkeypatch, EMAIL_SOURCE=f"export:{tmp_path}")
    [message] = messages(email.records({}, "backfill"))
    assert message["fields"]["is_starred"] is True
