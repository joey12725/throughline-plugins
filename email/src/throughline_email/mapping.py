"""Pure, deterministic mapping of raw Gmail records to Throughline messages.

The input is one fetch.py record (kind "gmail.message"). Both message shapes
Composio returns are read: the Gmail API shape (id, threadId, internalDate,
payload with headers and MIME parts) and the flattened shape (messageId,
messageTimestamp, sender, to, subject, messageText, attachmentList).
Records are never mutated.
"""

import base64
import binascii
import codecs
import html
import re
from datetime import UTC, datetime, timedelta
from email.errors import HeaderParseError
from email.header import decode_header, make_header
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser

# 1: first mapping.
# 2: the payload is a compact summary of the message (selected headers, label
#    names, attachment metadata, the full body text only when content was
#    truncated), never the raw Composio message; adds list_id and size_estimate.
MAPPING_REVISION = 2
CHANNEL = "email.message"
DEFAULT_BODY_LIMIT = 50_000

FIELDS = [
    ("account", "text", "Mailbox address the message was read from"),
    ("provider", "text", "Mail provider (gmail)"),
    ("gmail_message_id", "text", "Gmail message id, unique within the mailbox"),
    ("gmail_thread_id", "text", "Gmail thread id, unique within the mailbox"),
    ("gmail_history_id", "text", "Gmail history id of the fetched message state"),
    ("rfc822_message_id", "text", "Message-ID header"),
    ("in_reply_to", "text", "In-Reply-To header"),
    ("references", "text", "References header"),
    ("list_id", "text", "List-Id header (mailing lists)"),
    ("subject", "text", "Decoded subject"),
    ("from_address", "text", "Sender address, lower case"),
    ("from_name", "text", "Sender display name"),
    ("to", "json", "To recipients: [{address, name}]"),
    ("cc", "json", "Cc recipients: [{address, name}]"),
    ("bcc", "json", "Bcc recipients (sent mail only): [{address, name}]"),
    ("reply_to", "json", "Reply-To addresses: [{address, name}]"),
    ("participants", "json", "Every distinct address on the message, sorted"),
    ("labels", "json", "Label names (system labels keep their ids)"),
    ("label_ids", "json", "Gmail label ids"),
    ("snippet", "text", "Gmail snippet"),
    ("direction", "text", "received, sent or draft"),
    ("is_unread", "boolean", "UNREAD label present when fetched"),
    ("is_starred", "boolean", "STARRED label present when fetched"),
    ("is_important", "boolean", "IMPORTANT label present when fetched"),
    (
        "date_header",
        "text",
        "Date header as sent; happened_at is Gmail's internal date",
    ),
    ("body_format", "text", "plain, html (converted to text), snippet or none"),
    ("body_chars", "integer", "Characters in the full body text"),
    (
        "body_truncated",
        "boolean",
        "Content holds only the first EMAIL_BODY_LIMIT characters",
    ),
    ("attachment_count", "integer", "Attachments (metadata only, never content)"),
    (
        "attachments",
        "json",
        "[{filename, mime_type, size, attachment_id, content_id, inline}]",
    ),
    ("size_estimate", "integer", "Gmail size estimate of the raw message in bytes"),
    ("partial", "boolean", "Source lists were truncated by the MCP transport"),
    ("mapping_revision", "integer", "Mapping revision that produced this version"),
]

# Headers kept (as sent) in the stored payload; nothing else of the raw
# message is stored.
KEPT_HEADERS = (
    "from", "to", "cc", "bcc", "reply-to", "subject", "date", "message-id",
    "in-reply-to", "references", "list-id",
)  # fmt: skip

_PREFIX = re.compile(r"^\s*((re|fwd?|aw|wg|sv|antw)\s*(\[\d+\])?\s*:\s*)+", re.I)


def record_key(record):
    """(account, message id) of a fetch record, the identity used for dedupe."""
    raw = record.get("raw") if isinstance(record.get("raw"), dict) else {}
    account = str(record.get("account") or record.get("account_ref") or "unknown")
    ident = raw.get("id") or raw.get("messageId") or record.get("id")
    return account.strip().lower(), str(ident or "")


def header_text(value):
    if value is None:
        return None
    value = re.sub(r"\r?\n[ \t]+", " ", str(value))
    try:
        return str(make_header(decode_header(value)))
    except (HeaderParseError, UnicodeError, LookupError, ValueError):
        return value


def addresses(value):
    """[{address, name}] from an address header; names RFC 2047 decoded."""
    if not value:
        return []
    if isinstance(value, list):
        value = ", ".join(str(v) for v in value)
    result = []
    for name, address in getaddresses([str(value)]):
        address = address.strip().lower()
        if not address:
            continue
        name = header_text(name).strip() if name else None
        result.append({"address": address, "name": name or None})
    return result


def _b64(data):
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (binascii.Error, ValueError):
        return b""


def _headers(part):
    result = {}
    for item in part.get("headers") or []:
        if isinstance(item, dict) and item.get("name"):
            result.setdefault(str(item["name"]).lower(), []).append(
                str(item.get("value") or "")
            )
    return result


def _charset(part):
    for value in _headers(part).get("content-type", []):
        match = re.search(r'charset\s*=\s*"?([^";\s]+)', value, re.I)
        if match:
            try:
                return codecs.lookup(match.group(1)).name
            except LookupError:
                return "utf-8"
    return "utf-8"


def _walk(part):
    if not isinstance(part, dict):
        return
    yield part
    for child in part.get("parts") or []:
        yield from _walk(child)


class _Text(HTMLParser):
    BLOCK = {
        "p", "div", "br", "li", "tr", "table", "h1", "h2", "h3", "h4", "h5",
        "h6", "blockquote", "hr", "section", "article", "header", "footer",
        "ul", "ol", "pre", "dl", "dt", "dd",
    }  # fmt: skip
    PARAGRAPH = {
        "p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "table", "ul",
        "ol", "pre", "hr",
    }  # fmt: skip
    SKIP = {"script", "style", "head", "template", "noscript"}
    # Markers: a block edge is one line break, a paragraph edge a blank line;
    # a <br> is a literal newline (two in a row make a blank line).
    LINE, PARA = "\x02", "\x01"

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.skip = 0
        self.pre = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag == "br":
            self.out.append("\n")
        elif tag == "li":
            self.out.append(self.LINE + "- ")
        elif tag in ("td", "th"):
            self.out.append(" ")
        elif tag in self.PARAGRAPH:
            self.out.append(self.PARA)
        elif tag in self.BLOCK:
            self.out.append(self.LINE)
        if tag == "pre":
            self.pre += 1

    def handle_startendtag(self, tag, attrs):
        if tag not in self.SKIP:
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.PARAGRAPH:
            self.out.append(self.PARA)
        elif tag in self.BLOCK and tag not in ("br", "li"):
            self.out.append(self.LINE)
        if tag == "pre":
            self.pre = max(0, self.pre - 1)

    def handle_data(self, data):
        if not self.skip:
            # Source line breaks are only whitespace in HTML, except in <pre>.
            self.out.append(data if self.pre else re.sub(r"\s+", " ", data))


def _breaks(match):
    run = match.group(0)
    return "\n\n" if _Text.PARA in run or run.count("\n") >= 2 else "\n"


def tidy(text):
    lines = [
        re.sub(r"[ \t\f\v ]+", " ", line).strip()
        for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ]
    result, blank = [], False
    for line in lines:
        if not line:
            blank = bool(result)
            continue
        if blank:
            result.append("")
        result.append(line)
        blank = False
    return "\n".join(result)


def html_to_text(markup):
    parser = _Text()
    try:
        parser.feed(markup)
        parser.close()
    except (AssertionError, ValueError):  # pragma: no cover - lenient parser
        pass
    text = re.sub(
        r"[ \t\n\x01\x02]*[\n\x01\x02][ \t\n\x01\x02]*", _breaks, "".join(parser.out)
    )
    return tidy(text)


def looks_html(text):
    return bool(re.search(r"<(html|body|div|p|br|table|span|a)\b", text, re.I))


def _attachment(part):
    body = part.get("body") if isinstance(part.get("body"), dict) else {}
    headers = _headers(part)
    disposition = (headers.get("content-disposition") or [""])[0].lower()
    content_id = (headers.get("content-id") or [None])[0]
    size = body.get("size")
    return {
        "filename": part.get("filename") or None,
        "mime_type": part.get("mimeType") or None,
        "size": size if isinstance(size, int) else None,
        "attachment_id": body.get("attachmentId") or None,
        "content_id": content_id.strip("<> ") if content_id else None,
        "inline": disposition.startswith("inline"),
    }


def extract(payload):
    """(plain text, html text, attachments) from a Gmail payload part tree."""
    plain, markup, attachments = [], [], []
    for part in _walk(payload):
        mime = str(part.get("mimeType") or "").lower()
        if mime.startswith("multipart/"):
            continue
        body = part.get("body") if isinstance(part.get("body"), dict) else {}
        if part.get("filename") or (
            body.get("attachmentId") and mime not in ("text/plain", "text/html")
        ):
            attachments.append(_attachment(part))
            continue
        data = body.get("data")
        if not data or mime not in ("text/plain", "text/html"):
            if body.get("attachmentId"):
                attachments.append(_attachment(part))
            continue
        try:
            text = _b64(data).decode(_charset(part), "replace")
        except LookupError:  # pragma: no cover - _charset checked the codec
            text = _b64(data).decode("utf-8", "replace")
        (plain if mime == "text/plain" else markup).append(text)
    return "\n\n".join(plain), "\n\n".join(markup), attachments


def message_time(raw, headers):
    value = raw.get("internalDate")
    if value not in (None, ""):
        try:
            return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=int(value))
        except (TypeError, ValueError, OverflowError):
            pass
    value = raw.get("messageTimestamp")
    if isinstance(value, str) and value:
        try:
            at = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return (
                at if at.utcoffset() is not None else at.replace(tzinfo=UTC)
            ).astimezone(UTC)
        except ValueError:
            pass
    for value in headers.get("date", []):
        try:
            at = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            continue
        if at is not None and at.utcoffset() is not None:
            return at.astimezone(UTC)
    raise ValueError("email record has no usable timestamp")


def thread_title(subject):
    title = _PREFIX.sub("", subject or "").strip()
    return title or "(no subject)"


def _display(entry):
    return (
        f"{entry['name']} <{entry['address']}>"
        if entry.get("name")
        else entry["address"]
    )


def _clean(value):
    """Postgres text and jsonb refuse NUL; replace it only where present."""
    if isinstance(value, str):
        return value.replace("\x00", "�") if "\x00" in value else value
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def _has_nul(value):
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(_has_nul(k) or _has_nul(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_has_nul(v) for v in value)
    return False


# Keys of the flattened Composio shape that stand in for a missing header.
_FLAT_HEADERS = {
    "from": ("sender", "from"),
    "to": ("to",),
    "cc": ("cc",),
    "bcc": ("bcc",),
    "reply-to": ("replyTo", "reply_to"),
    "subject": ("subject",),
}


def map_record(record, body_limit=DEFAULT_BODY_LIMIT):
    """One Throughline message for a gmail.message record."""
    raw = record.get("raw")
    if not isinstance(raw, dict):
        raise ValueError("email record has no raw message object")
    account, ident = record_key(record)
    if not ident:
        raise ValueError("email record has no message id")
    thread = str(raw.get("threadId") or record.get("thread_id") or ident)
    payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
    headers = _headers(payload)

    def header(name, *alternatives):
        values = headers.get(name)
        if values:
            return (
                ", ".join(values)
                if name in ("to", "cc", "bcc", "reply-to")
                else values[0]
            )
        for key in alternatives:
            value = raw.get(key)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, list) and value:
                return ", ".join(str(v) for v in value)
        return None

    preview = raw.get("preview") if isinstance(raw.get("preview"), dict) else {}
    subject = header_text(header("subject", "subject")) or header_text(
        preview.get("subject")
    )
    subject = subject.strip() if subject else None
    sender = addresses(header("from", "sender", "from"))
    to = addresses(header("to", "to"))
    cc = addresses(header("cc", "cc"))
    bcc = addresses(header("bcc", "bcc"))
    reply_to = addresses(header("reply-to", "replyTo", "reply_to"))
    ids = [str(v) for v in raw.get("labelIds") or [] if v is not None]
    names = record.get("labels") if isinstance(record.get("labels"), dict) else {}
    labels = [str(names.get(i, i)) for i in ids]

    plain, markup, attachments = extract(payload) if payload else ("", "", [])
    if not attachments:
        for item in raw.get("attachmentList") or []:
            if isinstance(item, dict):
                attachments.append(
                    {
                        "filename": item.get("filename") or item.get("name") or None,
                        "mime_type": item.get("mimeType")
                        or item.get("mime_type")
                        or None,
                        "size": item.get("size")
                        if isinstance(item.get("size"), int)
                        else None,
                        "attachment_id": item.get("attachmentId")
                        or item.get("attachment_id")
                        or None,
                        "content_id": None,
                        "inline": False,
                    }
                )
    snippet = raw.get("snippet") or preview.get("body") or None
    snippet = html.unescape(str(snippet)).strip() if snippet else None
    if plain.strip():
        body, body_format = tidy(plain), "plain"
    elif markup.strip():
        body, body_format = html_to_text(markup), "html"
    elif isinstance(raw.get("messageText"), str) and raw["messageText"].strip():
        text = raw["messageText"]
        body, body_format = (
            (html_to_text(text), "html") if looks_html(text) else (tidy(text), "plain")
        )
    elif snippet:
        body, body_format = snippet, "snippet"
    else:
        body, body_format = "", "none"
    body = body.replace("\x00", "�")
    limit = max(0, int(body_limit))
    shown = body[:limit] if limit else body
    truncated = len(shown) < len(body)

    first = sender[0] if sender else None
    if "DRAFT" in ids:
        direction = "draft"
    elif "SENT" in ids or (first and first["address"] == account):
        direction = "sent"
    else:
        direction = "received"
    participants = sorted({a["address"] for a in sender + to + cc + bcc + reply_to})

    lines = [f"Subject: {subject or '(no subject)'}"]
    if sender:
        lines.append("From: " + _display(sender[0]))
    if to:
        shown_to = ", ".join(_display(a) for a in to[:20])
        lines.append(
            "To: " + shown_to + (f" and {len(to) - 20} more" if len(to) > 20 else "")
        )
    content = "\n".join(lines) + "\n\n" + (shown.strip() or "[no text body]")
    if truncated:
        content += "\n\n[body truncated]"
    if attachments:
        content += (
            "\n\n[attachments: "
            + ", ".join(a["filename"] or a["mime_type"] or "file" for a in attachments)
            + "]"
        )

    def one(name):
        value = headers.get(name)
        return value[0].strip() if value else None

    size = raw.get("sizeEstimate")
    size = size if isinstance(size, int) and not isinstance(size, bool) else None

    fields = dict(
        account=account,
        provider="gmail",
        gmail_message_id=ident,
        gmail_thread_id=thread,
        gmail_history_id=str(raw["historyId"])
        if raw.get("historyId") not in (None, "")
        else None,
        rfc822_message_id=one("message-id"),
        in_reply_to=one("in-reply-to"),
        references=one("references"),
        list_id=one("list-id"),
        subject=subject,
        from_address=first["address"] if first else None,
        from_name=first["name"] if first else None,
        to=to,
        cc=cc,
        bcc=bcc,
        reply_to=reply_to,
        participants=participants,
        labels=labels,
        label_ids=ids,
        snippet=snippet,
        direction=direction,
        is_unread="UNREAD" in ids,
        is_starred="STARRED" in ids,
        is_important="IMPORTANT" in ids,
        date_header=one("date"),
        body_format=body_format,
        body_chars=len(body),
        body_truncated=truncated,
        attachment_count=len(attachments),
        attachments=attachments,
        size_estimate=size,
        partial=bool(record.get("truncated")),
        mapping_revision=MAPPING_REVISION,
    )
    kept = {}
    for name in KEPT_HEADERS:
        value = header(name, *_FLAT_HEADERS.get(name, ()))
        if value is not None:
            kept[name] = value
    source = {
        "account": account,
        "message": {
            "id": ident,
            "thread_id": thread,
            "history_id": fields["gmail_history_id"],
            "internal_date": str(raw["internalDate"])
            if raw.get("internalDate") not in (None, "")
            else raw.get("messageTimestamp") or None,
            "label_ids": ids,
            "labels": {i: str(names[i]) for i in ids if i in names},
            "snippet": snippet,
            "size_estimate": size,
            "headers": kept,
            "attachments": [
                {
                    "filename": a["filename"],
                    "mime_type": a["mime_type"],
                    "size": a["size"],
                    "attachment_id": a["attachment_id"],
                }
                for a in attachments
            ],
            "body_format": body_format,
        },
    }
    if truncated:
        # The full readable text, only when content holds a prefix of it.
        source["body_text"] = body
    if record.get("truncated"):
        source["truncated"] = True
    message = dict(
        channel=CHANNEL,
        external_id=f"{account}:{ident}",
        thread=dict(
            external_id=f"{account}:{thread}",
            title=thread_title(subject),
            metadata={"account": account, "provider": "gmail"},
        ),
        happened_at=message_time(raw, headers).isoformat(),
        content=content,
        fields=fields,
        author={
            "system": "email",
            "kind": "email",
            "value": first["address"],
            "display_name": first["name"],
        }
        if first
        else None,
        images=[],
        payload=source,
    )
    return _clean(message) if _has_nul(message) else message
