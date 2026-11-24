"""Streaming reader for "SMS Backup & Restore" XML files (standard library only).

The app writes `sms-<timestamp>.xml` (root `<smses>`, children `<sms>` and
`<mms>`) and `calls-<timestamp>.xml` (root `<calls>`, children `<call>`).
Files can be hundreds of megabytes (MMS media is inlined as base64), so they
are parsed with iterparse and every top-level element is cleared after use.

The app encodes characters outside the Basic Multilingual Plane (most emoji)
as two numeric character references to UTF-16 surrogates, e.g.
`&#55357;&#56832;`, which XML parsers reject. `SurrogateFixer` rewrites those
pairs into the real character while streaming.
"""

import re
import xml.etree.ElementTree as ET
from pathlib import Path

CHUNK = 1 << 16
PAIR = re.compile(rb"&#(\d{5});&#(\d{5});")
LONE = re.compile(rb"&#(5[5-7]\d{3});")
TOP_LEVEL = {"sms", "mms", "call"}


def _pair(match):
    hi, lo = int(match.group(1)), int(match.group(2))
    if 0xD800 <= hi <= 0xDBFF and 0xDC00 <= lo <= 0xDFFF:
        code = 0x10000 + ((hi - 0xD800) << 10) + (lo - 0xDC00)
        return b"&#%d;" % code
    return match.group(0)


def _lone(match):
    value = int(match.group(1))
    return b"&#65533;" if 0xD800 <= value <= 0xDFFF else match.group(0)


class SurrogateFixer:
    """File-like wrapper that repairs surrogate-pair character references."""

    def __init__(self, raw):
        self.raw = raw
        self.carry = b""

    def read(self, size=-1):
        chunk = self.raw.read(CHUNK if size is None or size < 0 else max(size, 64))
        data = self.carry + chunk
        self.carry = b""
        if not data:
            return b""
        if chunk:
            # Hold back everything after the last character that can never
            # be inside a character reference, so a pair is never split.
            cut = max(data.rfind(c) for c in (b" ", b'"', b"'", b">", b"\n"))
            if cut >= 0:
                data, self.carry = data[: cut + 1], data[cut + 1 :]
        data = PAIR.sub(_pair, data)
        return LONE.sub(_lone, data)


def kind_of(path):
    """'messages' for <smses> files, 'calls' for <calls> files, else None."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(4096)
    except OSError:
        return None
    if b"<smses" in head:
        return "messages"
    if b"<calls" in head:
        return "calls"
    return None


def fingerprint(path):
    stat = Path(path).stat()
    return f"{Path(path).name}:{stat.st_size}:{stat.st_mtime_ns}"


def discover(directory):
    """Backup files in a folder, sorted by name (the app names them by time)."""
    found = []
    for path in sorted(Path(directory).expanduser().glob("*.xml")):
        if path.name.startswith(".") or not path.is_file():
            continue
        kind = kind_of(path)
        if kind:
            found.append((path, kind))
    return found


def clean(attrs):
    """Attributes with the app's literal "null" turned into None."""
    return {k: (None if v == "null" else v) for k, v in attrs.items()}


def element_record(elem):
    """A top-level element as a plain dict; MMS keeps parts and addrs."""
    record = {"tag": elem.tag, **clean(elem.attrib)}
    if elem.tag == "mms":
        parts, addrs = [], []
        for child in elem:
            if child.tag == "parts":
                for part in child:
                    item = clean(part.attrib)
                    data = item.pop("data", None)
                    if data:
                        # Media stays out of Throughline: only its size is kept.
                        item["data_bytes"] = len(data) * 3 // 4
                    parts.append(item)
            elif child.tag == "addrs":
                addrs.extend(clean(a.attrib) for a in child)
        record["parts"] = parts
        record["addrs"] = addrs
    return record


def iterate(path):
    """Yield every top-level record of one backup file, in file order."""
    with open(path, "rb") as raw:
        stream = SurrogateFixer(raw)
        root = None
        depth = 0
        for event, elem in ET.iterparse(stream, events=("start", "end")):
            if event == "start":
                if root is None:
                    root = elem
                depth += 1
                continue
            depth -= 1
            if depth == 1 and elem.tag in TOP_LEVEL:
                yield element_record(elem)
                elem.clear()
                root.clear()
