"""Pure mapping of SMS Backup & Restore records to Throughline messages."""

import hashlib
from datetime import datetime, timedelta, timezone

MESSAGES = "android_sms.messages"
CALLS = "android_sms.calls"

SMS_BOX = {"1": "inbox", "2": "sent", "3": "draft", "4": "outbox", "5": "failed", "6": "queued"}
MMS_BOX = {"1": "inbox", "2": "sent", "3": "draft", "4": "outbox"}
CALL_TYPES = {
    "1": "incoming",
    "2": "outgoing",
    "3": "missed",
    "4": "voicemail",
    "5": "rejected",
    "6": "blocked",
    "7": "answered_externally",
}
# MMS addr types: 137 from, 151 to, 130 cc, 129 bcc.
MMS_FROM = "137"
# Values that change between backups of the same record; kept out of payloads
# so overlapping backups map to identical records.
VOLATILE = {
    "readable_date",
    "contact_name",
    "read",
    "seen",
    "locked",
    "status",
    "read_status",
    "st",
    "retr_st",
    "resp_st",
    "d_rpt",
    "rr",
    "sim_slot",
    "sim_imsi",
    "new",
    "is_read",
}


def normalize_address(value, country_code=None):
    if not value:
        return ""
    value = value.strip()
    if "@" in value:
        return value.lower()
    cleaned = "".join(c for c in value if c.isdigit() or c == "+")
    if not cleaned or not any(c.isdigit() for c in cleaned):
        return value
    if country_code and not cleaned.startswith("+") and len(cleaned) == 10:
        cleaned = "+" + country_code + cleaned
    return cleaned


def addresses(value, country_code=None):
    """The app joins group MMS participants with '~'."""
    parts = [normalize_address(p, country_code) for p in (value or "").split("~")]
    return sorted({p for p in parts if p and p != "insert-address-token"})


def when(ms):
    value = int(ms)
    if value < 100_000_000_000:  # seconds (some MMS rows)
        value *= 1000
    return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=value)


def millis(record):
    value = int(record.get("date") or 0)
    return value * 1000 if 0 < value < 100_000_000_000 else value


def digest(*parts):
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()[:32]


def author_for(address, display_name=None):
    if not address:
        return None
    if "@" in address:
        author = {"system": "email", "kind": "email", "value": address}
    elif any(c.isdigit() for c in address) and all(c.isdigit() or c == "+" for c in address):
        author = {"system": "phone", "kind": "number", "value": address}
    else:
        author = {"system": "android_sms", "kind": "address", "value": address}
    if display_name:
        author["display_name"] = display_name
    return author


def stable_payload(record):
    out = {k: v for k, v in record.items() if k not in VOLATILE}
    if "parts" in out:
        out["parts"] = [{k: v for k, v in p.items()} for p in out["parts"]]
    return out


def contact(record):
    """Contact names are not imported: the app writes the phone's current
    contact name into every backup, so overlapping backups of one message can
    disagree, and a record must map identically from any backup. People are
    resolved from the phone number instead."""
    return None


def thread_for(participants):
    key = "~".join(participants) or "unknown"
    return {
        "external_id": "android_sms:" + key,
        "title": ", ".join(participants) or "unknown",
        "metadata": {
            "kind": "group_dm" if len(participants) > 1 else "dm",
            "participants": participants,
        },
    }


def map_sms(record, owner=None, country_code=None):
    participants = addresses(record.get("address"), country_code)
    box = SMS_BOX.get(record.get("type") or "", "unknown")
    outgoing = box in ("sent", "outbox", "failed", "queued")
    body = record.get("body") or ""
    other = participants[0] if participants else None
    external_id = "android_sms:sms:" + digest(
        millis(record), "~".join(participants), "out" if outgoing else "in", body
    )
    fields = {
        "direction": "outgoing" if outgoing else "incoming",
        "author_kind": "owner" if outgoing else "other",
        "message_kind": "sms",
        "box": box,
        "conversation_kind": "group_dm" if len(participants) > 1 else "dm",
        "participants": participants,
        "sender_address": None if outgoing else other,
        "subject": record.get("subject") or None,
        "sent_at": when(record["date_sent"]).isoformat()
        if (record.get("date_sent") or "0") not in ("0", "")
        else None,
    }
    return dict(
        channel=MESSAGES,
        external_id=external_id,
        thread=thread_for(participants),
        happened_at=when(record["date"]).isoformat(),
        content=body.strip() or "[empty SMS]",
        fields={k: v for k, v in fields.items() if v not in (None, "", [])},
        author=author_for(owner) if outgoing else author_for(other, contact(record)),
        payload=stable_payload(record),
    )


def map_mms(record, owner=None, country_code=None):
    participants = addresses(record.get("address"), country_code)
    box = MMS_BOX.get(record.get("msg_box") or "", "unknown")
    outgoing = box in ("sent", "outbox")
    texts, files = [], []
    for part in record.get("parts") or []:
        ct = (part.get("ct") or "").lower()
        if ct == "application/smil":
            continue
        if ct.startswith("text/plain"):
            if part.get("text"):
                texts.append(part["text"])
            continue
        files.append(
            {
                k: part.get(k)
                for k in ("ct", "name", "fn", "cl", "data_bytes")
                if part.get(k) not in (None, "")
            }
        )
    sender = None
    if not outgoing:
        sender = next(
            (
                normalize_address(a.get("address"), country_code)
                for a in record.get("addrs") or []
                if a.get("type") == MMS_FROM
            ),
            None,
        )
    body = "\n".join(texts).strip()
    external_id = "android_sms:mms:" + digest(
        millis(record), "~".join(participants), "out" if outgoing else "in", body
    )
    if body:
        content = body
    elif files:
        content = " ".join(
            "[attachment: " + (f.get("name") or f.get("cl") or f.get("ct") or "file") + "]"
            for f in files
        )
    else:
        content = "[empty MMS]"
    fields = {
        "direction": "outgoing" if outgoing else "incoming",
        "author_kind": "owner" if outgoing else "other",
        "message_kind": "mms",
        "box": box,
        "conversation_kind": "group_dm" if len(participants) > 1 else "dm",
        "participants": participants,
        "sender_address": sender,
        "subject": record.get("sub") or None,
        "attachments": files,
    }
    return dict(
        channel=MESSAGES,
        external_id=external_id,
        thread=thread_for(participants),
        happened_at=when(record["date"]).isoformat(),
        content=content,
        fields={k: v for k, v in fields.items() if v not in (None, "", [])},
        author=author_for(owner) if outgoing else author_for(sender, None if len(participants) > 1 else contact(record)),
        payload=stable_payload(record),
    )


def map_call(record, owner=None, country_code=None):
    number = normalize_address(record.get("number"), country_code) or "unknown"
    call_type = CALL_TYPES.get(record.get("type") or "", "unknown")
    duration = int(record.get("duration") or 0)
    start = when(record["date"])
    name = contact(record)
    who = f"{name} ({number})" if name else number
    verb = {
        "incoming": "Incoming call from",
        "outgoing": "Outgoing call to",
        "missed": "Missed call from",
        "voicemail": "Voicemail from",
        "rejected": "Rejected call from",
        "blocked": "Blocked call from",
        "answered_externally": "Call answered on another device from",
    }.get(call_type, "Call with")
    content = f"{verb} {who}"
    if duration:
        content += f", {duration // 60}m {duration % 60:02d}s"
    message = dict(
        channel=CALLS,
        external_id="android_sms:call:" + digest(millis(record), number, call_type),
        thread={"external_id": "android_sms:call:" + number, "title": number},
        happened_at=start.isoformat(),
        content=content,
        fields={
            "direction": "outgoing" if call_type == "outgoing" else "incoming",
            "call_type": call_type,
            "duration_s": duration,
            "number": number,
            "author_kind": "owner" if call_type == "outgoing" else "other",
        },
        author=author_for(owner) if call_type == "outgoing" else author_for(number, name),
        payload=stable_payload(record),
    )
    if duration:
        message["ended_at"] = (start + timedelta(seconds=duration)).isoformat()
    return message


def map_record(record, owner=None, country_code=None):
    tag = record["tag"]
    if tag == "sms":
        if record.get("type") == "3":
            return None  # drafts were never sent
        return map_sms(record, owner, country_code)
    if tag == "mms":
        if record.get("msg_box") == "3":
            return None
        return map_mms(record, owner, country_code)
    if tag == "call":
        return map_call(record, owner, country_code)
    return None
