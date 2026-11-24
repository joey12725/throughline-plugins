"""Minimal protobuf reader for Apple Notes bodies (no protobuf dependency).

ZICNOTEDATA.ZDATA is a gzipped NoteStoreProto:

    NoteStoreProto { Document document = 2; }
    Document       { int32 version = 2; Note note = 3; }
    Note           { string note_text = 2; repeated AttributeRun attribute_run = 5; }
    AttributeRun   { int32 length = 1; ParagraphStyle paragraph_style = 2;
                     string link = 9; AttachmentInfo attachment_info = 12; ... }
    ParagraphStyle { int32 style_type = 1; int32 indent_amount = 4; Checklist checklist = 5; }
    Checklist      { bytes uuid = 1; int32 done = 2; }
    AttachmentInfo { string attachment_identifier = 1; string type_uti = 2; }

Run lengths count UTF-16 code units (NSString lengths).
"""

from __future__ import annotations

import gzip
import zlib

TITLE, HEADING, SUBHEADING, MONOSPACED = 0, 1, 2, 4
DOTTED, DASHED, NUMBERED, CHECKBOX = 100, 101, 102, 103
OBJECT = "￼"


class DecodeError(ValueError):
    pass


def varint(buf, pos):
    result = shift = 0
    while True:
        if pos >= len(buf):
            raise DecodeError("truncated varint")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise DecodeError("varint too long")


def fields(buf):
    """Yield (field number, wire type, value): int for varint/fixed, bytes
    for length-delimited. Groups (wire types 3/4) are rejected."""
    pos = 0
    while pos < len(buf):
        key, pos = varint(buf, pos)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = varint(buf, pos)
        elif wire == 1:
            value, pos = int.from_bytes(buf[pos : pos + 8], "little"), pos + 8
        elif wire == 2:
            size, pos = varint(buf, pos)
            if pos + size > len(buf):
                raise DecodeError("truncated field")
            value, pos = bytes(buf[pos : pos + size]), pos + size
        elif wire == 5:
            value, pos = int.from_bytes(buf[pos : pos + 4], "little"), pos + 4
        else:
            raise DecodeError(f"unsupported wire type {wire}")
        if pos > len(buf):
            raise DecodeError("truncated field")
        yield number, wire, value


def first(buf, number, wire=2):
    for n, w, value in fields(buf):
        if n == number and w == wire:
            return value
    return None


def inflate(data):
    data = bytes(data or b"")
    if data[:2] == b"\x1f\x8b":
        return gzip.decompress(data)
    try:
        return zlib.decompress(data)
    except zlib.error:
        return data


def note_proto(data):
    """(text, runs) from a ZDATA blob. Each run is a dict with length,
    style, indent, checked (None/False/True), link, attachment_uti."""
    raw = inflate(data)
    document = first(raw, 2)
    note = first(document, 3) if document is not None else None
    if note is None:
        raise DecodeError("no note in protobuf")
    text = ""
    runs = []
    for number, wire, value in fields(note):
        if number == 2 and wire == 2:
            text = value.decode("utf-8", errors="replace")
        elif number == 5 and wire == 2:
            runs.append(run_of(value))
    return text, runs


def run_of(buf):
    run = {"length": 0, "style": None, "indent": 0, "checked": None, "link": None, "attachment_uti": None}
    for number, wire, value in fields(buf):
        if number == 1 and wire == 0:
            run["length"] = value
        elif number == 2 and wire == 2:
            for n, w, v in fields(value):
                if n == 1 and w == 0:
                    run["style"] = v
                elif n == 4 and w == 0:
                    run["indent"] = v
                elif n == 5 and w == 2:
                    done = first(v, 2, 0)
                    run["checked"] = bool(done)
        elif number == 9 and wire == 2:
            run["link"] = value.decode("utf-8", errors="replace")
        elif number == 12 and wire == 2:
            uti = first(value, 2)
            run["attachment_uti"] = uti.decode("utf-8", errors="replace") if uti else "unknown"
    return run


def units(char):
    return 2 if ord(char) > 0xFFFF else 1


def styled_chars(text, runs):
    """Per character: the run covering it (or None past the last run)."""
    result = []
    run_index, left = 0, runs[0]["length"] if runs else 0
    for char in text:
        while run_index < len(runs) and left <= 0:
            run_index += 1
            left = runs[run_index]["length"] if run_index < len(runs) else 0
        result.append(runs[run_index] if run_index < len(runs) else None)
        left -= units(char)
    return result


def attachment_label(uti):
    uti = (uti or "").lower()
    if "table" in uti:
        return "[table]"
    if uti.startswith("public.") and any(k in uti for k in ("jpeg", "png", "heic", "image", "tiff", "gif")):
        return "[image]"
    if "drawing" in uti or "sketch" in uti:
        return "[drawing]"
    if "url" in uti:
        return "[link preview]"
    if "pdf" in uti:
        return "[pdf]"
    if "audio" in uti:
        return "[audio]"
    return "[attachment]"


def render(text, runs):
    """Readable Markdown-ish text plus a summary of what the body holds."""
    styles = styled_chars(text, runs)
    lines, line, line_style = [], [], None
    links, attachments, checks = [], [], [0, 0]
    number = 0

    def close():
        nonlocal number
        body = "".join(line)
        run = line_style or {}
        style = run.get("style")
        indent = "  " * int(run.get("indent") or 0)
        if style == NUMBERED:
            number += 1
        else:
            number = 0
        if style in (TITLE, HEADING, SUBHEADING) and body.strip():
            prefix = {TITLE: "# ", HEADING: "## ", SUBHEADING: "### "}[style]
        elif style in (DOTTED, DASHED):
            prefix = indent + "- "
        elif style == NUMBERED:
            prefix = indent + f"{number}. "
        elif style == CHECKBOX:
            checks[0] += 1
            checks[1] += bool(run.get("checked"))
            prefix = indent + ("- [x] " if run.get("checked") else "- [ ] ")
        elif style == MONOSPACED:
            prefix = "    "
        else:
            prefix = ""
        lines.append(prefix + body if body or prefix.strip() else body)

    for char, run in zip(text, styles, strict=True):
        if line_style is None and char != "\n":
            line_style = run
        if char == "\n":
            if line_style is None:
                line_style = run
            close()
            line, line_style = [], None
            continue
        if char == OBJECT:
            uti = (run or {}).get("attachment_uti")
            attachments.append(uti or "unknown")
            line.append(attachment_label(uti))
            continue
        line.append(char)
        link = (run or {}).get("link")
        if link and link not in links:
            links.append(link)
    if line:
        close()
    return "\n".join(lines).strip("\n"), {
        "links": links,
        "attachments": attachments,
        "checklist_total": checks[0],
        "checklist_done": checks[1],
    }
