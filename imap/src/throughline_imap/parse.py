"""Raw RFC 822 bytes -> the normalized mail dict that mailmap.map_mail reads."""

from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime

from . import mailmap

# Headers kept (decoded, as sent) in the stored payload.
KEPT_HEADERS = (
    "from", "to", "cc", "bcc", "reply-to", "subject", "date", "message-id",
    "in-reply-to", "references", "list-id",
)  # fmt: skip
# Fetched in the planning pass; enough for the record key.
KEY_HEADERS = ("MESSAGE-ID", "DATE", "FROM", "TO", "SUBJECT")


def _parse(raw):
    return BytesParser(policy=policy.default).parsebytes(raw)


def _header(msg, name):
    try:
        values = msg.get_all(name) or []
        return [str(v) for v in values]
    except Exception:  # noqa: BLE001 - malformed header: read it raw
        values = msg.get_all(name, failobj=[]) if hasattr(msg, "get_all") else []
        return [str(getattr(v, "_value", v)) for v in values]


def _one(msg, name):
    values = _header(msg, name)
    return mailmap.header_text(values[0]).strip() if values else None


def record_key(raw_headers):
    """(key, message id) from a message's headers (or the whole message)."""
    msg = _parse(raw_headers)
    mid = mailmap.message_id(_one(msg, "message-id"))
    if mid:
        return mid, mid
    return (
        mailmap.fallback_key(
            _one(msg, "date"), _one(msg, "from"), _one(msg, "to"), _one(msg, "subject")
        ),
        None,
    )


def _text(part):
    try:
        return part.get_content()
    except Exception:  # noqa: BLE001 - unknown charset or broken encoding
        data = part.get_payload(decode=True) or b""
        return data.decode("utf-8", "replace")


def _attachment(part):
    try:
        data = part.get_payload(decode=True)
    except Exception:  # noqa: BLE001
        data = None
    content_id = part.get("content-id")
    try:
        filename = part.get_filename()
    except Exception:  # noqa: BLE001
        filename = None
    return {
        "filename": filename or None,
        "mime_type": part.get_content_type(),
        "size": len(data) if isinstance(data, bytes) else None,
        "content_id": str(content_id).strip("<> ") if content_id else None,
        "inline": (part.get_content_disposition() or "") == "inline",
    }


def _walk(part, plain, markup, attachments):
    if part.is_multipart():
        for child in part.iter_parts():
            _walk(child, plain, markup, attachments)
        return
    ctype = part.get_content_type()
    disposition = part.get_content_disposition()
    if ctype == "message/rfc822" or disposition == "attachment" or part.get_filename():
        attachments.append(_attachment(part))
        return
    if ctype == "text/plain":
        plain.append(_text(part))
    elif ctype == "text/html":
        markup.append(_text(part))
    else:
        attachments.append(_attachment(part))


def received_time(internal, msg):
    if internal is not None:
        return internal
    for value in _header(msg, "date"):
        try:
            at = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            continue
        if at is not None and at.utcoffset() is not None:
            return at
    raise ValueError("message has neither INTERNALDATE nor a usable Date header")


def normalize(raw, *, account, key, internal=None, size=None):
    """The provider-neutral part of a mail dict (flags and folders are added
    by the caller)."""
    msg = _parse(raw)
    plain, markup, attachments = [], [], []
    _walk(msg, plain, markup, attachments)
    headers = {}
    for name in KEPT_HEADERS:
        values = _header(msg, name)
        if values:
            headers[name] = mailmap.header_text(", ".join(values))
    return {
        "account": account,
        "provider": "imap",
        "key": key,
        "message_id": mailmap.message_id(_one(msg, "message-id")),
        "in_reply_to": _one(msg, "in-reply-to"),
        "references": " ".join(_header(msg, "references")) or None,
        "list_id": _one(msg, "list-id"),
        "subject": _one(msg, "subject"),
        "from": mailmap.addresses(_header(msg, "from")),
        "to": mailmap.addresses(_header(msg, "to")),
        "cc": mailmap.addresses(_header(msg, "cc")),
        "bcc": mailmap.addresses(_header(msg, "bcc")),
        "reply_to": mailmap.addresses(_header(msg, "reply-to")),
        "date_header": _one(msg, "date"),
        "happened_at": received_time(internal, msg),
        "plain": "\n\n".join(plain),
        "html": "\n\n".join(markup),
        "attachments": attachments,
        "size": size,
        "headers": headers,
    }
