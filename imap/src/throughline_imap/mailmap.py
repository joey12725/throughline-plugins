"""Provider-neutral email mapping.

The same file ships in throughline_imap and throughline_msgraph so mail from
any provider has the same field names, thread modelling and author shape as
the Gmail connector (plugins/email): one message per email, author
`{system: "email", kind: "email", value: <address>}`, content
"Subject/From/To lines + body", HTML converted to text, attachments as
metadata only.

Input is a normalized dict built by the provider module (see `map_mail`);
the output is one Throughline message dict. Pure and deterministic.
"""

import hashlib
import html
import json
import re
from email.errors import HeaderParseError
from email.header import decode_header, make_header
from email.utils import getaddresses
from html.parser import HTMLParser

DEFAULT_BODY_LIMIT = 50_000
SNIPPET_CHARS = 200

# Field names shared with plugins/email where the meaning is the same.
COMMON_FIELDS = [
    ("account", "text", "Mailbox address the message was read from"),
    ("provider", "text", "Mail provider / protocol"),
    ("rfc822_message_id", "text", "Message-ID header without angle brackets"),
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
    ("folders", "json", "Folders the message was found in when fetched"),
    ("labels", "json", "Folder names plus provider labels/categories"),
    ("snippet", "text", "First characters of the body text"),
    ("direction", "text", "received, sent or draft"),
    (
        "author_kind",
        "text",
        "owner (sent by one of the owner addresses) or other",
    ),
    ("is_unread", "boolean", "Not marked as read when fetched"),
    ("is_starred", "boolean", "Flagged / starred when fetched"),
    ("is_important", "boolean", "Marked important / high importance when fetched"),
    ("date_header", "text", "Date header as sent; happened_at is the server's received time"),
    ("body_format", "text", "plain, html (converted to text), snippet or none"),
    ("body_chars", "integer", "Characters in the full body text"),
    ("body_truncated", "boolean", "Content holds only the first BODY_LIMIT characters"),
    ("attachment_count", "integer", "Attachments (metadata only, never content)"),
    ("attachments", "json", "[{filename, mime_type, size, content_id, inline}]"),
    ("size_estimate", "integer", "Size of the raw message in bytes, when known"),
    (
        "thread_source",
        "text",
        "How the thread was chosen: provider (native thread id), references "
        "(root of References/In-Reply-To) or message (a thread of its own)",
    ),
    ("mapping_revision", "integer", "Mapping revision that produced this version"),
]

_PREFIX = re.compile(r"^\s*((re|fwd?|aw|wg|sv|antw)\s*(\[\d+\])?\s*:\s*)+", re.I)
_MSGID = re.compile(r"<([^<>\s]+)>")


def header_text(value):
    """A header as readable text (RFC 2047 decoded, unfolded)."""
    if value is None:
        return None
    value = re.sub(r"\r?\n[ \t]+", " ", str(value))
    if "=?" not in value:
        return value
    try:
        return str(make_header(decode_header(value)))
    except (HeaderParseError, UnicodeError, LookupError, ValueError):
        return value


def addresses(value):
    """[{address, name}] from an address header (or a list of headers)."""
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        value = ", ".join(str(v) for v in value if v)
    result = []
    for name, address in getaddresses([str(value)]):
        address = address.strip().lower()
        if not address or "@" not in address:
            continue
        name = header_text(name).strip().strip('"') if name else None
        result.append({"address": address, "name": name or None})
    return result


def message_id(value):
    """The first <id> of a Message-ID style header, without brackets."""
    if not value:
        return None
    text = str(value).strip()
    found = _MSGID.search(text)
    if found:
        return found.group(1)
    text = text.strip("<> \t\r\n")
    return text if text and " " not in text else None


def references(value):
    return _MSGID.findall(str(value or ""))


def thread_root(own_id, in_reply_to, refs):
    """Root of the reply chain: the first References id, else In-Reply-To."""
    chain = references(refs)
    if chain:
        return chain[0], "references"
    parent = message_id(in_reply_to)
    if parent:
        return parent, "references"
    return own_id, "message"


def fallback_key(date, sender, to, subject, size=None):
    """A stable key for a message without a Message-ID header."""
    text = json.dumps(
        [str(date or ""), str(sender or ""), str(to or ""), str(subject or ""), size],
        ensure_ascii=False,
    )
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


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


def thread_title(subject):
    title = _PREFIX.sub("", subject or "").strip()
    return title or "(no subject)"


def _display(entry):
    return f"{entry['name']} <{entry['address']}>" if entry.get("name") else entry["address"]


def _clean(value):
    """Postgres text and jsonb refuse NUL."""
    if isinstance(value, str):
        return value.replace("\x00", "�")
    if isinstance(value, dict):
        return {_clean(k): _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def body_text(plain, markup, snippet=None):
    """(text, format) preferring text/plain, then HTML converted to text."""
    if plain and plain.strip():
        return tidy(plain), "plain"
    if markup and markup.strip():
        return html_to_text(markup), "html"
    if snippet and snippet.strip():
        return snippet.strip(), "snippet"
    return "", "none"


def map_mail(
    mail,
    *,
    channel,
    revision,
    owner=(),
    body_limit=DEFAULT_BODY_LIMIT,
    extra_fields=None,
    extra_payload=None,
):
    """One Throughline message for a normalized mail dict.

    Keys of `mail`: account, provider, key (stable record key: the Message-ID
    or a fallback hash), message_id, in_reply_to, references, list_id,
    subject, from/to/cc/bcc/reply_to ([{address, name}]), date_header,
    happened_at (aware datetime), folders, labels, is_unread, is_starred,
    is_important, is_draft, in_sent, plain, html, snippet, attachments,
    size, thread_id (provider-native thread id or None), headers (dict kept
    in the payload).
    """
    account = mail["account"]
    owner = {a.lower() for a in owner if a}
    sender = mail.get("from") or []
    first = sender[0] if sender else None
    to, cc, bcc = mail.get("to") or [], mail.get("cc") or [], mail.get("bcc") or []
    reply_to = mail.get("reply_to") or []
    subject = (mail.get("subject") or "").strip() or None

    body, body_format = body_text(mail.get("plain"), mail.get("html"), mail.get("snippet"))
    snippet = mail.get("snippet")
    if not snippet and body:
        snippet = re.sub(r"\s+", " ", body)[:SNIPPET_CHARS].strip() or None
    snippet = html.unescape(snippet) if snippet else None
    limit = max(0, int(body_limit))
    shown = body[:limit] if limit else body
    truncated = len(shown) < len(body)

    from_owner = bool(first and first["address"] in owner)
    if mail.get("is_draft"):
        direction = "draft"
    elif mail.get("in_sent") or from_owner:
        direction = "sent"
    else:
        direction = "received"
    author_kind = "owner" if (from_owner or direction != "received") else "other"

    if mail.get("thread_id"):
        thread_key, thread_source = mail["thread_id"], "provider"
    else:
        root, thread_source = thread_root(
            mail.get("message_id"), mail.get("in_reply_to"), mail.get("references")
        )
        thread_key = "mid:" + (root or mail["key"])
    participants = sorted({a["address"] for a in sender + to + cc + bcc + reply_to})
    attachments = mail.get("attachments") or []

    lines = [f"Subject: {subject or '(no subject)'}"]
    if first:
        lines.append("From: " + _display(first))
    if to:
        shown_to = ", ".join(_display(a) for a in to[:20])
        lines.append("To: " + shown_to + (f" and {len(to) - 20} more" if len(to) > 20 else ""))
    content = "\n".join(lines) + "\n\n" + (shown.strip() or "[no text body]")
    if truncated:
        content += "\n\n[body truncated]"
    if attachments:
        content += (
            "\n\n[attachments: "
            + ", ".join(a.get("filename") or a.get("mime_type") or "file" for a in attachments)
            + "]"
        )
    folders = list(mail.get("folders") or [])
    labels = list(dict.fromkeys(folders + list(mail.get("labels") or [])))
    fields = dict(
        account=account,
        provider=mail["provider"],
        rfc822_message_id=mail.get("message_id"),
        in_reply_to=message_id(mail.get("in_reply_to")),
        references=" ".join(references(mail.get("references"))) or None,
        list_id=mail.get("list_id"),
        subject=subject,
        from_address=first["address"] if first else None,
        from_name=first["name"] if first else None,
        to=to,
        cc=cc,
        bcc=bcc,
        reply_to=reply_to,
        participants=participants,
        folders=folders,
        labels=labels,
        snippet=snippet,
        direction=direction,
        author_kind=author_kind,
        is_unread=bool(mail.get("is_unread")),
        is_starred=bool(mail.get("is_starred")),
        is_important=bool(mail.get("is_important")),
        date_header=mail.get("date_header"),
        body_format=body_format,
        body_chars=len(body),
        body_truncated=truncated,
        attachment_count=len(attachments),
        attachments=attachments,
        size_estimate=mail.get("size"),
        thread_source=thread_source,
        mapping_revision=revision,
    )
    fields.update(extra_fields or {})
    payload = {
        "account": account,
        "provider": mail["provider"],
        "message": {
            "key": mail["key"],
            "headers": mail.get("headers") or {},
            "folders": folders,
            "labels": labels,
            "attachments": attachments,
            "body_format": body_format,
            **(extra_payload or {}),
        },
    }
    if truncated:
        payload["body_text"] = body
    message = dict(
        channel=channel,
        external_id=f"{account}:{mail['key']}",
        thread=dict(
            external_id=f"{account}:{thread_key}",
            title=thread_title(subject),
            metadata={"account": account, "provider": mail["provider"]},
        ),
        happened_at=mail["happened_at"].isoformat(),
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
        payload=payload,
    )
    return _clean(message)
