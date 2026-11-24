import copy
import json
from pathlib import Path

import pytest
from throughline_sdk import Checkpoint, Record
from throughline_sdk.contract import check_plugin

from throughline_imap import Imap, choose_folders
from throughline_imap.fake import FakeIMAP
from throughline_imap.protocol import ImapError, decode_utf7, parse_fetch, parse_list_line, uid_set

FIXTURE = Path(__file__).parent / "recorded" / "mailbox.json"
PASSWORD = "invented-canary-imap-password"
ME = "invented.owner@example.com"


def spec():
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("IMAP_USERNAME", ME)
    monkeypatch.setenv("IMAP_PASSWORD", PASSWORD)
    monkeypatch.setenv("IMAP_HOST", "imap.example.com")
    monkeypatch.delenv("IMAP_FIXTURE", raising=False)
    _ENV.append(monkeypatch)
    yield monkeypatch
    _ENV.clear()


_ENV = []


def plugin(fake, **settings):
    for key, value in settings.items():
        _ENV[-1].setenv(key, value)
    instance = Imap()
    instance.connector = lambda: fake
    return instance


def run(instance, cursor=None):
    items = list(instance.records(cursor or {}, "sync"))
    messages = [i for i in items if isinstance(i, dict)]
    checkpoints = [i for i in items if isinstance(i, Checkpoint)]
    return messages, checkpoints, items


def by_id(messages):
    return {m["external_id"].split(":", 1)[1]: m for m in messages}


# --- protocol ------------------------------------------------------------------


def test_parse_fetch_handles_literals_and_trailing_attributes():
    data = [
        (b'1 (UID 5 FLAGS (\\Seen $Invented) BODY[HEADER.FIELDS (MESSAGE-ID DATE)] {24}', b"Message-ID: <a@b.c>\r\n\r\n"),
        b' X-GM-THRID 1234 X-GM-LABELS (\\Inbox "Invented label"))',
        b'2 (UID 9 FLAGS () INTERNALDATE "06-Jan-2025 09:15:00 +0000" RFC822.SIZE 10)',
    ]
    parsed = parse_fetch(data)
    assert parsed[5]["FLAGS"] == ["\\Seen", "$Invented"]
    assert parsed[5]["BODY[HEADER.FIELDS (MESSAGE-ID DATE)]"] == b"Message-ID: <a@b.c>\r\n\r\n"
    assert parsed[5]["X-GM-LABELS"] == ["\\Inbox", "Invented label"]
    assert parsed[5]["X-GM-THRID"] == "1234"
    assert parsed[9]["INTERNALDATE"] == "06-Jan-2025 09:15:00 +0000"
    assert parsed[9]["FLAGS"] == []


def test_list_lines_names_and_utf7():
    assert parse_list_line(b'(\\HasNoChildren \\Sent) "/" "Sent Items"') == (["\\HasNoChildren", "\\Sent"], "/", "Sent Items")
    assert parse_list_line((b'(\\HasNoChildren) "/" {16}', b"Invented Archive"))[2] == "Invented Archive"
    assert parse_list_line(b'(\\Noselect) NIL INBOX')[1] is None
    assert decode_utf7("Entw&APw-rfe") == "Entwürfe"
    assert decode_utf7("A&-B") == "A&B"
    assert uid_set([1, 2, 3, 7, 9, 10]) == "1:3,7,9:10"


def test_folder_choice_prefers_all_mail_then_inbox_and_sent():
    listing = [
        {"name": "INBOX", "display": "INBOX", "special": None},
        {"name": "Sent Items", "display": "Sent Items", "special": None},
        {"name": "Junk", "display": "Junk", "special": "\\Junk"},
    ]
    assert [f["name"] for f in choose_folders(listing)] == ["INBOX", "Sent Items"]
    gmail = listing + [{"name": "[Gmail]/All Mail", "display": "[Gmail]/All Mail", "special": "\\All"}]
    assert [f["name"] for f in choose_folders(gmail)] == ["[Gmail]/All Mail"]
    assert [f["name"] for f in choose_folders(gmail, ["inbox", "\\Junk"])] == ["INBOX", "Junk"]
    assert [f["name"] for f in choose_folders(listing, (), ["sent items"])] == ["INBOX"]


# --- mapping -------------------------------------------------------------------


def test_backfill_maps_threads_dedupe_and_bodies(env):
    fake = FakeIMAP(spec())
    messages, checkpoints, _ = run(plugin(fake))
    rows = by_id(messages)
    assert len(messages) == 8  # 6 inbox + 3 sent, the note to self only once
    note = rows["self-1@example.com"]
    assert note["fields"]["folders"] == ["INBOX", "Sent"]
    assert note["fields"]["imap_uids"] == {"INBOX": 7, "Sent": 12}
    assert note["fields"]["direction"] == "sent" and note["fields"]["author_kind"] == "owner"

    # References root threads the whole picnic conversation together.
    picnic = [rows[k] for k in ("picnic-1@example.com", "picnic-2@example.com", "picnic-3@example.com")]
    assert {m["thread"]["external_id"] for m in picnic} == {ME + ":mid:picnic-1@example.com"}
    assert picnic[0]["thread"]["title"] == "Invented picnic plans"
    assert picnic[0]["fields"]["thread_source"] == "message"
    assert picnic[1]["fields"]["thread_source"] == "references"
    assert picnic[1]["fields"]["direction"] == "sent"
    assert picnic[0]["fields"]["is_unread"] is True
    assert picnic[2]["fields"]["is_starred"] is True
    assert picnic[2]["fields"]["body_format"] == "html"
    assert "Great, see you Saturday!" in picnic[2]["content"]
    assert "- invented cheese" in picnic[2]["content"]
    assert picnic[0]["author"] == {
        "system": "email",
        "kind": "email",
        "value": "friend@example.com",
        "display_name": "Invented Friend",
    }
    assert picnic[0]["happened_at"] == "2025-01-06T09:15:00+00:00"
    assert picnic[0]["fields"]["date_header"] == "Mon, 06 Jan 2025 10:14:58 +0100"

    order = rows["order-77@shop.example.com"]
    assert order["fields"]["subject"] == "Ihre Bestellung – Invented"
    assert order["fields"]["from_name"] == "Invented Bäckerei"
    assert order["fields"]["list_id"] == "Invented offers <offers.shop.example.com>"
    assert order["fields"]["attachments"] == [
        {"filename": "invented-receipt.pdf", "mime_type": "application/pdf", "size": 16, "content_id": None, "inline": False}
    ]
    assert "[attachments: invented-receipt.pdf]" in order["content"]

    hashed = [k for k in rows if k.startswith("sha256:")]
    assert len(hashed) == 1 and rows[hashed[0]]["fields"]["rfc822_message_id"] is None

    question = rows["q-1@example.com"]
    assert question["fields"]["bcc"] == [{"address": "other@example.net", "name": None}]
    assert question["fields"]["participants"] == ["invented.owner@example.com", "other@example.net", "shop@example.com"]

    final = checkpoints[-1].cursor
    assert final["folders"]["INBOX"] == {"uidvalidity": 1700000001, "uid": 8, "modseq": 18}
    assert "resume" not in final
    assert PASSWORD not in json.dumps(messages)


def test_read_only_access(env):
    fake = FakeIMAP(spec())
    run(plugin(fake))
    selects = [c for c in fake.calls if c[0] == "SELECT"]
    assert selects and all(c[2] is True for c in selects)
    fetches = [c for c in fake.calls if c[:2] == ("UID", "FETCH")]
    assert all("PEEK" in c[3] for c in fetches if "BODY" in c[3])
    assert not any(c[1] in ("STORE", "COPY", "MOVE", "EXPUNGE") for c in fake.calls if c[0] == "UID")


def test_incremental_new_mail_flag_changes_and_uidvalidity(env):
    data = spec()
    fake = FakeIMAP(copy.deepcopy(data))
    _, checkpoints, _ = run(plugin(fake))
    cursor = checkpoints[-1].cursor

    # Nothing new: nothing emitted.
    again, _, _ = run(plugin(FakeIMAP(copy.deepcopy(data))), cursor)
    assert again == []

    # A new message and a flag change (CONDSTORE) on an old one.
    changed = copy.deepcopy(data)
    inbox = changed["folders"][0]["messages"]
    inbox[0]["flags"] = ["\\Seen"]
    inbox[0]["modseq"] = 40
    inbox.append(
        {
            "uid": 9,
            "flags": [],
            "modseq": 41,
            "internaldate": "12-Jan-2025 10:00:00 +0000",
            "raw_lines": ["From: friend@example.com", "To: " + ME, "Subject: Invented later", "Message-ID: <later@example.com>", "", "Later."],
        }
    )
    messages, checkpoints, _ = run(plugin(FakeIMAP(changed)), cursor)
    rows = by_id(messages)
    assert set(rows) == {"picnic-1@example.com", "later@example.com"}
    assert rows["picnic-1@example.com"]["fields"]["is_unread"] is False
    assert checkpoints[-1].cursor["folders"]["INBOX"]["uid"] == 9

    # UIDVALIDITY changed: the folder is read again, ids stay the same.
    reset = copy.deepcopy(changed)
    reset["folders"][0]["uidvalidity"] = 99
    messages, _, _ = run(plugin(FakeIMAP(reset)), checkpoints[-1].cursor)
    assert len(messages) == 7 and "later@example.com" in by_id(messages)


def test_crash_resume_skips_acknowledged_batches(env):
    data = spec()
    first, checkpoints, items = run(plugin(FakeIMAP(copy.deepcopy(data)), IMAP_BATCH_SIZE="2"))
    interim = [c for c in checkpoints if "resume" in c.cursor]
    assert len(interim) == 3
    resumed, _, _ = run(plugin(FakeIMAP(copy.deepcopy(data)), IMAP_BATCH_SIZE="2"), interim[1].cursor)
    assert [m["external_id"] for m in resumed] == [m["external_id"] for m in first][4:]
    assert resumed == first[4:]


def test_since_and_max_messages_bound_the_first_run(env):
    messages, checkpoints, _ = run(plugin(FakeIMAP(spec()), IMAP_SINCE="2025-01-09", IMAP_MAX_MESSAGES="0"))
    assert {m["external_id"].split(":", 1)[1] for m in messages} == {"self-1@example.com", "picnic-4@example.com", "q-1@example.com"}
    assert checkpoints[-1].cursor["folders"]["INBOX"]["uid"] == 8
    messages, _, _ = run(plugin(FakeIMAP(spec()), IMAP_SINCE="", IMAP_MAX_MESSAGES="1"))
    assert {m["external_id"].split(":", 1)[1] for m in messages} == {"picnic-4@example.com", "q-1@example.com"}


def test_folder_failure_is_partial_and_total_failure_raises(env):
    data = spec()
    data["fail"] = {"select:Sent": "NO"}
    messages, _, items = run(plugin(FakeIMAP(data)))
    assert len(messages) == 6
    reports = [i for i in items if isinstance(i, Record) and i.kind == "coverage"]
    assert reports[0].data["status"] == "partial" and reports[0].data["reasons"] == ["source_error"]
    assert "Sent" not in [c for c in items if isinstance(c, Checkpoint)][-1].cursor["folders"]

    data["fail"] = {"select:Sent": "NO", "select:INBOX": "NO"}
    with pytest.raises(ImapError):
        run(plugin(FakeIMAP(data)))


def test_login_failure_raises_without_the_password(env):
    instance = plugin(FakeIMAP(spec()))
    instance.secrets["password"] = "wrong-invented"
    with pytest.raises(ImapError) as error:
        run(instance)
    assert "wrong-invented" not in str(error.value)


def gmail_spec():
    raw = ["From: Invented Friend <friend@example.com>", "To: " + ME, "Subject: Invented hike", "Message-ID: <hike@example.com>", "", "Hike on Sunday?"]
    reply = ["From: " + ME, "To: friend@example.com", "Subject: Re: Invented hike", "Message-ID: <hike-2@example.com>", "", "Sure."]
    return {
        "capabilities": ["IMAP4REV1", "X-GM-EXT-1"],
        "folders": [
            {"name": "INBOX", "flags": [], "uidvalidity": 1, "messages": []},
            {"name": "[Gmail]/All Mail", "flags": ["\\All", "\\HasNoChildren"], "uidvalidity": 11, "messages": [
                {"uid": 1, "flags": ["\\Seen"], "internaldate": "02-Feb-2025 10:00:00 +0000", "x_gm_thrid": "1844674407370955", "x_gm_msgid": "1844674407370955", "x_gm_labels": ["\\Inbox", "\\Important", "Invented trips"], "raw_lines": raw},
                {"uid": 2, "flags": ["\\Seen"], "internaldate": "02-Feb-2025 11:00:00 +0000", "x_gm_thrid": "1844674407370955", "x_gm_msgid": "1844674407370999", "x_gm_labels": ["\\Sent"], "raw_lines": reply},
            ]},
            {"name": "[Gmail]/Sent Mail", "flags": ["\\Sent"], "uidvalidity": 12, "messages": []},
        ],
    }


def test_gmail_all_mail_thread_ids_and_labels(env):
    messages, _, _ = run(plugin(FakeIMAP(gmail_spec())))
    rows = by_id(messages)
    hike, reply = rows["hike@example.com"], rows["hike-2@example.com"]
    assert hike["fields"]["folders"] == ["[Gmail]/All Mail"]
    assert hike["thread"]["external_id"] == reply["thread"]["external_id"] == ME + ":gm:" + format(1844674407370955, "x")
    assert hike["fields"]["thread_source"] == "provider"
    assert hike["fields"]["gmail_thread_id"] == format(1844674407370955, "x")
    assert hike["fields"]["labels"] == ["[Gmail]/All Mail", "\\Inbox", "\\Important", "Invented trips"]
    assert hike["fields"]["is_important"] is True
    assert reply["fields"]["direction"] == "sent"


def test_contract_kit():
    result = check_plugin(
        "throughline_imap:Imap",
        {
            "IMAP_FIXTURE": str(FIXTURE),
            "IMAP_USERNAME": ME,
            "IMAP_PASSWORD": PASSWORD,
            "IMAP_BATCH_SIZE": "2",
        },
        secrets=[PASSWORD],
    )
    assert result["messages"] == 8


def test_parse_fetch_merges_split_responses():
    data = [b"1 (UID 5 FLAGS (\\Seen))", b'1 (UID 5 INTERNALDATE "06-Jan-2025 09:15:00 +0000")']
    assert parse_fetch(data)[5] == {"UID": "5", "FLAGS": ["\\Seen"], "INTERNALDATE": "06-Jan-2025 09:15:00 +0000"}


def test_capabilities_are_read_after_login(env):
    fake = FakeIMAP(spec())
    assert "CONDSTORE" not in fake.capabilities  # The greeting list, as imaplib keeps it.
    instance = plugin(fake)
    session = instance.connect()
    assert session.condstore


def test_later_copy_keeps_folders_seen_in_earlier_runs(env):
    raw = ["From: " + ME, "To: " + ME, "Subject: note", "Message-ID: <self-9@example.com>", "", "hi"]
    data = {"capabilities": ["IMAP4REV1"], "folders": [
        {"name": "INBOX", "flags": [], "uidvalidity": 1, "messages": []},
        {"name": "Sent", "flags": ["\\Sent"], "uidvalidity": 1, "messages": [
            {"uid": 1, "flags": ["\\Seen"], "internaldate": "06-Jan-2025 10:00:00 +0000", "raw_lines": raw}]},
    ]}  # fmt: skip
    first, checkpoints, _ = run(plugin(FakeIMAP(copy.deepcopy(data))))
    assert first[0]["fields"]["folders"] == ["Sent"]
    data["folders"][0]["messages"].append({"uid": 1, "flags": [], "internaldate": "06-Jan-2025 10:00:05 +0000", "raw_lines": raw})
    later, _, _ = run(plugin(FakeIMAP(copy.deepcopy(data))), checkpoints[-1].cursor)
    replay, _, _ = run(plugin(FakeIMAP(copy.deepcopy(data))))
    assert later == replay
    assert later[0]["fields"]["folders"] == ["INBOX", "Sent"] and later[0]["fields"]["direction"] == "sent"
