import io
import shutil
from pathlib import Path

import pytest
from throughline_sdk import Checkpoint
from throughline_sdk.contract import check_plugin

from throughline_android_sms import AndroidSMS
from throughline_android_sms.backup import SurrogateFixer, iterate
from throughline_android_sms.mapping import addresses, normalize_address

RECORDED = Path(__file__).parent / "recorded"


@pytest.fixture
def folder(tmp_path, monkeypatch):
    target = tmp_path / "backups"
    shutil.copytree(RECORDED, target)
    monkeypatch.setenv("ANDROID_SMS_DIR", str(target))
    monkeypatch.setenv("ANDROID_SMS_BATCH_SIZE", "3")
    monkeypatch.setenv("ANDROID_SMS_OWNER_NUMBER", "+15555550100")
    monkeypatch.setenv("ANDROID_SMS_COUNTRY_CODE", "1")
    monkeypatch.delenv("ANDROID_SMS_OVERLAP_DAYS", raising=False)
    return target


def messages(items):
    return [i for i in items if isinstance(i, dict)]


def test_surrogate_pairs_are_repaired_across_chunk_boundaries():
    text = b'<a b="' + b"x" * 65530 + b'&#55357;&#56832;" c="&#55357;" />'
    fixed = b""
    stream = SurrogateFixer(io.BytesIO(text))
    while chunk := stream.read(65536):
        fixed += chunk
    assert b"&#128512;" in fixed and b"&#65533;" in fixed and b"&#55357;&#56832;" not in fixed


def test_stream_parser_reads_mms_parts_without_media(folder):
    records = list(iterate(folder / "sms-20261008130000.xml"))
    photo = next(r for r in records if r["tag"] == "mms" and r["text_only"] == "0")
    image = next(p for p in photo["parts"] if p["ct"] == "image/jpeg")
    assert "data" not in image and image["data_bytes"] > 0
    assert any(a["type"] == "137" for a in photo["addrs"])
    assert records[2]["body"] == "Invented smile \U0001f600 see you"


def test_address_normalization():
    assert normalize_address("(555) 555-0102", "1") == "+15555550102"
    assert normalize_address("(555) 555-0102") == "5555550102"
    assert addresses("+15555550102~+15555550101") == ["+15555550101", "+15555550102"]


def test_mapping_and_dedupe_across_overlapping_backups(folder):
    items = list(AndroidSMS().records({}, "backfill"))
    rows = messages(items)
    ids = [m["external_id"] for m in rows]
    assert len(ids) == len(set(ids))
    sms = [m for m in rows if m["channel"] == "android_sms.messages"]
    calls = [m for m in rows if m["channel"] == "android_sms.calls"]
    assert len(sms) == 12 and len(calls) == 6
    assert not any("draft" in m["content"] for m in sms)
    first = next(m for m in sms if m["content"].startswith("Invented: are you"))
    assert first["author"] == {
        "system": "phone",
        "kind": "number",
        "value": "+15555550101",
    }
    assert first["fields"]["direction"] == "incoming"
    assert first["happened_at"] == "2026-10-01T12:00:00+00:00"
    sent = next(m for m in sms if "lemonade" in m["content"])
    assert sent["fields"]["author_kind"] == "owner"
    assert sent["author"]["value"] == "+15555550100"
    group = next(m for m in sms if "group plan" in m["content"])
    assert group["fields"]["conversation_kind"] == "group_dm"
    assert group["fields"]["sender_address"] == "+15555550102"
    assert group["author"]["value"] == "+15555550102"
    assert group["thread"]["external_id"] == "android_sms:+15555550101~+15555550102"
    photo = next(m for m in sms if m["content"].startswith("[attachment"))
    assert photo["fields"]["attachments"][0]["name"] == "invented-photo.jpg"
    national = next(m for m in sms if "national format" in m["content"])
    assert national["thread"]["external_id"] == "android_sms:+15555550102"
    assert "read" not in first["payload"] and "contact_name" not in first["payload"]
    missed = next(m for m in calls if m["fields"]["call_type"] == "missed")
    assert missed["content"] == "Missed call from +15555550102"
    answered = next(m for m in calls if m["fields"]["duration_s"] == 65)
    assert answered["ended_at"] == "2026-10-01T12:02:05+00:00"
    assert len([i for i in items if isinstance(i, Checkpoint)]) >= 5


def test_sync_reads_only_new_files_and_skips_old_records(folder, monkeypatch):
    monkeypatch.setenv("ANDROID_SMS_OVERLAP_DAYS", "0")
    plugin = AndroidSMS()
    cursor = [i for i in plugin.records({}, "sync") if isinstance(i, Checkpoint)][-1].cursor
    assert messages(plugin.records(cursor, "sync")) == []
    newest = folder / "sms-20261008130000.xml"
    text = newest.read_text().replace(
        "</smses>",
        '  <sms protocol="0" address="+15555550101" date="1790900000000" type="1" '
        'subject="null" body="Invented brand new" read="0" date_sent="0" '
        'contact_name="Invented Alex" />\n</smses>',
    )
    (folder / "sms-20261015130000.xml").write_text(text)
    later = messages(plugin.records(cursor, "sync"))
    # The full repeat of older messages is skipped (no overlap window here).
    assert [m["content"] for m in later] == ["Invented last group message", "Invented brand new"]


def test_partial_backup_is_retried(folder, caplog):
    (folder / "sms-20261020130000.xml").write_text(
        "<?xml version='1.0'?>\n<smses count=\"1\">\n  <sms address=\"+1555\""
    )
    rows = messages(AndroidSMS().records({}, "backfill"))
    assert len(rows) == 18
    assert "unreadable backup" in caplog.text


def test_empty_folder_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("ANDROID_SMS_DIR", str(tmp_path))
    with pytest.raises(RuntimeError):
        list(AndroidSMS().records({}, "backfill"))


def test_contract(tmp_path):
    target = tmp_path / "backups"
    shutil.copytree(RECORDED, target)
    result = check_plugin(
        "throughline_android_sms:AndroidSMS",
        {
            "ANDROID_SMS_DIR": str(target),
            "ANDROID_SMS_BATCH_SIZE": "2",
            "ANDROID_SMS_OWNER_NUMBER": "+15555550100",
        },
    )
    assert result["messages"] == 18
