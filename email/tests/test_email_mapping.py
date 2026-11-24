"""Mapping of raw Gmail records: MIME, headers, identities, threads, fields."""

import copy
import json

from email_fakes import b64, flattened, gmail_message, mailboxes
from throughline_email import Email, mapping


def record(message, account="alpha@example.invalid", **extra):
    base = {
        "kind": "gmail.message",
        "account": account,
        "account_ref": "ca_alpha",
        "id": message.get("id") or message.get("messageId"),
        "fetched_at": "2026-01-01T00:00:00Z",
        "labels": {},
        "raw": message,
    }
    base.update(extra)
    return base


def by_id(ident):
    for box in mailboxes().values():
        for message in box["messages"]:
            if message["id"] == ident:
                return message
    raise KeyError(ident)


def test_plain_message_identity_thread_and_fields():
    message = mapping.map_record(record(by_id("a1")))
    assert message["channel"] == "email.message"
    assert message["external_id"] == "alpha@example.invalid:a1"
    assert message["thread"]["external_id"] == "alpha@example.invalid:ta"
    assert message["thread"]["title"] == "Garden plans"
    assert message["happened_at"] == "2021-03-01T09:00:00+00:00"
    assert message["author"] == {
        "system": "email",
        "kind": "email",
        "value": "robin@example.invalid",
        "display_name": "Robin Example",
    }
    assert message["content"] == (
        "Subject: Garden plans\nFrom: Robin Example <robin@example.invalid>\n"
        "To: alpha@example.invalid\n\nShall we plant the invented tulips?\nRobin"
    )
    fields = message["fields"]
    assert fields["direction"] == "received" and fields["body_format"] == "plain"
    assert fields["to"] == [{"address": "alpha@example.invalid", "name": None}]
    assert fields["participants"] == ["alpha@example.invalid", "robin@example.invalid"]
    assert fields["rfc822_message_id"] == "<a1@mail.example.invalid>"
    assert fields["mapping_revision"] == mapping.MAPPING_REVISION
    assert fields["date_header"].startswith("Mon, 01 Mar 2021 09:00:00")
    stored = message["payload"]["message"]
    assert stored["id"] == "a1" and stored["thread_id"] == "ta"
    assert stored["headers"]["message-id"] == "<a1@mail.example.invalid>"
    assert stored["headers"]["from"] == "Robin Example <robin@example.invalid>"
    assert "payload" not in stored and "body_text" not in message["payload"]
    declared = {name for name, _, _ in mapping.FIELDS}
    assert set(fields) == declared


def test_reply_thread_title_and_sent_direction():
    message = mapping.map_record(record(by_id("a2")))
    assert message["thread"]["title"] == "Garden plans"
    assert message["fields"]["direction"] == "sent"
    assert message["author"]["value"] == "alpha@example.invalid"
    beta = mapping.map_record(record(by_id("b2"), account="beta@example.invalid"))
    assert beta["thread"]["title"] == "Book club"
    # Offsets are normalized to UTC from Gmail's internal date.
    assert beta["happened_at"] == "2021-02-28T14:00:00+00:00"


def test_html_only_encoded_words_labels_and_unread():
    message = mapping.map_record(record(by_id("a3"), labels={"Label_7": "Food"}))
    fields = message["fields"]
    assert fields["subject"] == "Menü update"
    assert fields["from_name"] == "Café Owner"
    assert fields["body_format"] == "html"
    assert message["content"].endswith("New menu\n\n- Soup\n- Bread & butter")
    assert "p{}" not in message["content"]
    assert fields["labels"] == ["INBOX", "UNREAD", "Food"]
    assert fields["label_ids"] == ["INBOX", "UNREAD", "Label_7"]
    assert fields["is_unread"] is True and fields["is_starred"] is False
    assert message["payload"]["message"]["labels"] == {"Label_7": "Food"}


def test_alternative_prefers_plain_and_lists_attachments():
    message = mapping.map_record(record(by_id("a4")))
    fields = message["fields"]
    assert fields["body_format"] == "plain"
    assert fields["cc"] == [
        {"address": "ledger@example.invalid", "name": "Ledger"},
        {"address": "other@example.invalid", "name": None},
    ]
    assert fields["attachment_count"] == 1
    assert fields["attachments"] == [
        {
            "filename": "invoice-42.pdf",
            "mime_type": "application/pdf",
            "size": 2048,
            "attachment_id": "att-a4-1",
            "content_id": None,
            "inline": False,
        }
    ]
    assert message["content"].endswith(
        "Invoice attached.\n\n[attachments: invoice-42.pdf]"
    )
    assert fields["is_starred"] and fields["is_important"]


def test_flattened_composio_shape():
    flat = flattened(by_id("b1"))
    message = mapping.map_record(record(flat, account="beta@example.invalid"))
    assert message["external_id"] == "beta@example.invalid:b1"
    assert message["happened_at"] == "2021-02-27T23:00:00+00:00"
    assert message["author"]["value"] == "sam@example.invalid"
    assert message["fields"]["subject"] == "Book club"
    assert message["content"].endswith("Next book is invented.")
    html = dict(flat, messageText="<div>Hi<br>there</div>", messageId="b9")
    mapped = mapping.map_record(record(html, account="beta@example.invalid"))
    assert mapped["fields"]["body_format"] == "html"
    assert mapped["content"].endswith("Hi\nthere")


def test_charset_single_part_snippet_and_truncation():
    latin = {
        "id": "x1",
        "threadId": "tx1",
        "internalDate": "1600000000000",
        "labelIds": ["INBOX"],
        "snippet": "It&#39;s fine",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": "a@example.invalid"},
                {"name": "Content-Type", "value": 'text/plain; charset="iso-8859-1"'},
            ],
            "body": {
                "data": __import__("base64")
                .urlsafe_b64encode("Grüße".encode("latin-1"))
                .decode()
            },
        },
    }
    message = mapping.map_record(record(latin))
    assert message["content"].endswith("Grüße")
    assert message["fields"]["snippet"] == "It's fine"
    assert message["fields"]["subject"] is None
    assert message["content"].startswith("Subject: (no subject)\n")
    assert message["thread"]["title"] == "(no subject)"
    long = gmail_message(
        "x2",
        "tx2",
        "2021-01-01T00:00:00+00:00",
        "a@example.invalid",
        "b@example.invalid",
        "Long",
        plain="word " * 100,
    )
    short = mapping.map_record(record(long), body_limit=20)
    assert short["fields"]["body_truncated"] is True
    assert short["fields"]["body_chars"] == len(("word " * 100).strip())
    assert short["content"].endswith("[body truncated]")
    empty = dict(latin, payload={"headers": latin["payload"]["headers"]}, snippet="")
    assert mapping.map_record(record(empty))["content"].endswith("[no text body]")


def test_nul_is_replaced_and_draft_direction():
    raw = gmail_message(
        "x3",
        "tx3",
        "2021-01-01T00:00:00+00:00",
        "a@example.invalid",
        "b@example.invalid",
        "Nul",
        plain="a\x00b",
        labels=("DRAFT",),
    )
    raw["snippet"] = "a\x00b"
    message = mapping.map_record(record(raw))
    assert "\x00" not in json.dumps(message)
    assert message["fields"]["direction"] == "draft"


def test_deterministic_and_never_mutates():
    raw = by_id("a4")
    original = copy.deepcopy(raw)
    first = mapping.map_record(record(raw))
    second = mapping.map_record(record(raw, fetched_at="2027-01-01T00:00:00Z"))
    assert first == second  # fetch time is not part of the message
    assert raw == original


def test_plugin_skips_malformed_and_other_kinds(monkeypatch):
    monkeypatch.setenv("EMAIL_SOURCE", "export:unused")
    plugin = Email()
    assert plugin.map({"kind": "gmail.profile", "raw": {}}) is None
    assert (
        plugin.map({"kind": "gmail.message", "account": "a", "raw": {"id": "m"}})
        is None
    )
    bad = {
        "kind": "gmail.message",
        "account": "a",
        "raw": {"payload": {"body": {"data": b64("x")}}},
    }
    assert plugin.map(bad) is None


def test_html_to_text_blocks_breaks_and_pre():
    assert mapping.html_to_text("<div>a</div><div>b</div>") == "a\nb"
    assert mapping.html_to_text("a<br>b<br><br>c") == "a\nb\n\nc"
    assert (
        mapping.html_to_text("<p>one\n   two</p>\n<p>three</p>") == "one two\n\nthree"
    )
    assert mapping.html_to_text("<pre>x\ny</pre>") == "x\ny"
    assert mapping.html_to_text("<table><tr><td>a</td><td>b</td></tr></table>") == "a b"
    assert (
        mapping.html_to_text("<script>var x;</script>Hi&nbsp;there &lt;3")
        == "Hi there <3"
    )


def test_field_names_avoid_reserved_columns():
    reserved = set(
        "id channel_id channel external_id version thread_id thread_no happened_at content "
        "fields lat lon person_id superseded_by happened_offset_min ended_at ingested_at "
        "author_handle author_identifier_id images loc_accuracy_m loc_method loc_reason "
        "access_label payload_hash tsv score conversation_id first_thread_no last_thread_no "
        "is_reference is_outside thread_external_id thread_title person_handle display_name "
        "kind record_kind record_id ref_id record_time is_current subject_from subject_to "
        "subject_from_precision subject_to_precision provenance support expansion generated "
        "unsupported claim heading_path char_start char_end".split()
    )
    assert not reserved & {name for name, _, _ in mapping.FIELDS}


def _huge_message():
    """A newsletter-sized fake: big HTML and plain parts, inline images and an
    attachment carried as base64, and a long Received header chain."""
    markup = "<html><head><style>" + "p{color:red}" * 2000 + "</style></head><body>"
    markup += "".join(
        f"<p>Paragraph {i} of the invented letter.</p>" for i in range(400)
    )
    markup += "</body></html>"
    plain = "\n\n".join(f"Paragraph {i} of the invented letter." for i in range(400))
    raw = gmail_message(
        "big1",
        "tbig",
        "2022-05-01T08:00:00+00:00",
        "News <news@example.invalid>",
        "alpha@example.invalid",
        "Weekly news",
        plain=plain,
        markup=markup,
        attachments=[("report.pdf", "application/pdf", 90_000)],
    )
    headers = raw["payload"]["headers"]
    headers += [
        {
            "name": "Received",
            "value": f"from relay{i}.example.invalid by mx " + "x" * 200,
        }
        for i in range(40)
    ]
    headers += [
        {"name": "List-Id", "value": "Weekly <weekly.example.invalid>"},
        {"name": "Reply-To", "value": "replies@example.invalid"},
        {"name": "DKIM-Signature", "value": "v=1; " + "k" * 2000},
    ]
    blob = b64("PNG" + "A" * 60_000)
    for number in range(3):
        raw["payload"]["parts"].append(
            {
                "partId": f"img{number}",
                "mimeType": "image/png",
                "filename": "",
                "headers": [{"name": "Content-ID", "value": f"<img{number}>"}],
                "body": {"size": 60_000, "data": blob, "attachmentId": f"inl{number}"},
            }
        )
    raw["payload"]["parts"][1]["body"]["data"] = b64("%PDF" + "B" * 90_000)
    raw["sizeEstimate"] = 400_000
    return raw


def test_large_payload_maps_to_small_stored_record():
    raw = _huge_message()
    before = len(json.dumps(record(raw)))
    message = mapping.map_record(
        record(raw, labels={"Label_1": "News", "Label_2": "X"})
    )
    after = len(json.dumps(message))
    assert before > 300_000
    assert after < 30_000, after
    stored = json.dumps(message["payload"])
    assert len(stored) < 3_000
    for absent in ("AAAA", "BBBB", "<p>", "<style", "Received", "DKIM", "kkkk"):
        assert absent not in stored
    assert "Received" not in json.dumps(message)
    headers = message["payload"]["message"]["headers"]
    assert set(headers) == {
        "from", "to", "subject", "date", "message-id", "list-id", "reply-to"
    }  # fmt: skip
    assert message["fields"]["list_id"] == "Weekly <weekly.example.invalid>"
    assert message["fields"]["size_estimate"] == 400_000
    assert message["payload"]["message"]["size_estimate"] == 400_000
    assert message["payload"]["message"]["labels"] == {}
    assert [a["filename"] for a in message["payload"]["message"]["attachments"]] == [
        "report.pdf", None, None, None
    ]  # fmt: skip
    assert set(message["payload"]["message"]["attachments"][0]) == {
        "filename", "mime_type", "size", "attachment_id"
    }  # fmt: skip
    assert message["fields"]["body_format"] == "plain"
    assert "Paragraph 399 of the invented letter." in message["content"]
    print(f"synthetic message: raw record {before} bytes, mapped {after} bytes")


def test_truncated_body_kept_in_full_and_rerun_is_identical():
    raw = _huge_message()
    first = mapping.map_record(record(raw), body_limit=1000)
    assert first["fields"]["body_truncated"] is True
    full = first["payload"]["body_text"]
    assert len(full) == first["fields"]["body_chars"] > 1000
    assert full.endswith("Paragraph 399 of the invented letter.")
    assert "Paragraph 399" not in first["content"]
    again = mapping.map_record(record(copy.deepcopy(raw)), body_limit=1000)
    assert json.dumps(first, sort_keys=True) == json.dumps(again, sort_keys=True)


def test_html_only_large_payload_stores_text_not_markup():
    raw = _huge_message()
    alternative = raw["payload"]["parts"][0]
    alternative["parts"] = [
        p for p in alternative["parts"] if p["mimeType"] == "text/html"
    ]
    message = mapping.map_record(record(raw))
    assert message["fields"]["body_format"] == "html"
    dumped = json.dumps(message)
    assert "<p>" not in dumped and "color:red" not in dumped
    assert "Paragraph 12 of the invented letter." in message["content"]
