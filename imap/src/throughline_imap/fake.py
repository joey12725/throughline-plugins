"""An in-memory, imaplib.IMAP4-compatible mailbox loaded from a JSON fixture.

Used by the tests and by IMAP_FIXTURE=<file> (contract kit), never against a
real server. It answers in the same shapes imaplib returns (tuples for
literals, trailing attribute chunks after a literal, quoted and literal
mailbox names) so the real parser is exercised.

Fixture format:
  {"capabilities": ["IMAP4REV1", "CONDSTORE", "X-GM-EXT-1"],
   "password": "optional expected password",
   "folders": [{"name": "INBOX", "flags": ["\\\\HasNoChildren"],
                "uidvalidity": 7, "messages": [
                  {"uid": 1, "flags": ["\\\\Seen"], "modseq": 3,
                   "internaldate": "05-Jan-2025 10:00:00 +0000",
                   "x_gm_thrid": "1001", "x_gm_msgid": "2001",
                   "x_gm_labels": ["\\\\Inbox"],
                   "raw": "From: ...\\r\\n\\r\\nbody" | "raw_lines": [...]}]}]}
"""

import imaplib
import json
import re
from datetime import datetime


def _unquote(name):
    name = name.strip()
    if len(name) >= 2 and name[0] == '"' and name[-1] == '"':
        return re.sub(r'\\(.)', r"\1", name[1:-1])
    return name


def _raw(message):
    if "raw_lines" in message:
        return "\r\n".join(message["raw_lines"]).encode("utf-8")
    return str(message.get("raw") or "").replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8")


def _flags(values):
    return "(" + " ".join(values) + ")"


def _qstr(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


class FakeIMAP:
    def __init__(self, spec):
        self.spec = spec
        self.full_capabilities = tuple(spec.get("capabilities") or ["IMAP4REV1"])
        self.logged_in = False
        self.folders = {f["name"]: f for f in spec.get("folders") or []}
        self.selected = None
        self.calls = []
        self.fail = dict(spec.get("fail") or {})  # {"select:Sent": "NO"}

    @classmethod
    def from_file(cls, path):
        with open(path, encoding="utf-8") as handle:
            return cls(json.load(handle))

    # imaplib surface -----------------------------------------------------------

    def login(self, user, password):
        self.calls.append(("LOGIN", user))
        expected = self.spec.get("password")
        if expected is not None and password != expected:
            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")
        self.logged_in = True
        return "OK", [b"Logged in"]

    @property
    def capabilities(self):
        # imaplib keeps the pre-login (greeting) list in this attribute.
        return ("IMAP4REV1", "AUTH=PLAIN")

    def capability(self):
        # Like real servers: extensions are announced only after LOGIN.
        caps = self.full_capabilities if self.logged_in else ("IMAP4REV1", "AUTH=PLAIN")
        return "OK", [" ".join(caps).encode()]

    def list(self, directory='""', pattern="*"):
        self.calls.append(("LIST",))
        lines = []
        for folder in self.folders.values():
            flags = _flags(folder.get("flags") or [])
            name = folder["name"]
            if " " in name and folder.get("literal_name"):
                lines.append((f'{flags} "/" {{{len(name.encode())}}}'.encode(), name.encode()))
            else:
                lines.append(f'{flags} "/" {_qstr(name)}'.encode())
        return "OK", lines

    def status(self, mailbox, items):
        name = _unquote(mailbox)
        self.calls.append(("STATUS", name))
        folder = self.folders.get(name)
        if folder is None or self.fail.get("status:" + name):
            return "NO", [b"Mailbox does not exist"]
        messages = folder.get("messages") or []
        values = {
            "MESSAGES": len(messages),
            "UIDNEXT": max([m["uid"] for m in messages] or [0]) + 1,
            "UIDVALIDITY": folder.get("uidvalidity", 1),
            "HIGHESTMODSEQ": max([m.get("modseq", 1) for m in messages] or [1]),
        }
        wanted = [w for w in items.strip("()").split() if w in values]
        body = " ".join(f"{w} {values[w]}" for w in wanted)
        return "OK", [f"{_qstr(name)} ({body})".encode()]

    def select(self, mailbox, readonly=False):
        name = _unquote(mailbox)
        self.calls.append(("SELECT", name, readonly))
        if name not in self.folders or self.fail.get("select:" + name):
            return "NO", [b"[NONEXISTENT] Unknown Mailbox"]
        self.selected = name
        return "OK", [str(len(self.folders[name].get("messages") or [])).encode()]

    def logout(self):
        self.calls.append(("LOGOUT",))
        return "BYE", [b"Logging out"]

    def uid(self, command, *args):
        command = command.upper()
        self.calls.append(("UID", command) + tuple(a for a in args if a is not None))
        if self.selected is None:
            return "BAD", [b"No mailbox selected"]
        messages = sorted(self.folders[self.selected].get("messages") or [], key=lambda m: m["uid"])
        if command == "SEARCH":
            return "OK", [" ".join(str(u) for u in self._search(messages, args[1:])).encode()]
        if command == "FETCH":
            return "OK", self._fetch(messages, *args)
        return "BAD", [b"unsupported"]

    # helpers -----------------------------------------------------------------

    def _search(self, messages, criteria):
        criteria = [c for c in criteria if c is not None]
        uids = [m["uid"] for m in messages]
        i = 0
        while i < len(criteria):
            word = str(criteria[i]).upper()
            if word == "ALL":
                i += 1
            elif word == "UID":
                low, _, high = str(criteria[i + 1]).partition(":")
                top = max(uids or [0])
                low = int(low)
                high = top if high in ("*", "") else int(high)
                if high == top and low > top and top:
                    low = top  # "n:*" always includes the highest UID.
                uids = [u for u in uids if low <= u <= high]
                i += 2
            elif word == "SINCE":
                day = datetime.strptime(str(criteria[i + 1]), "%d-%b-%Y").date()
                keep = {
                    m["uid"]
                    for m in messages
                    if datetime.strptime(m["internaldate"], "%d-%b-%Y %H:%M:%S %z").date() >= day
                }
                uids = [u for u in uids if u in keep]
                i += 2
            elif word == "HEADER":
                name, value = str(criteria[i + 1]), _unquote(str(criteria[i + 2])).lower()
                keep = {
                    m["uid"]
                    for m in messages
                    if value in _headers(_raw(m), [name]).decode("utf-8", "replace").lower()
                }
                uids = [u for u in uids if u in keep]
                i += 3
            else:
                raise ValueError("fake IMAP: unsupported SEARCH " + word)
        return uids

    def _wanted(self, spec):
        lowered = spec.upper()
        fields = None
        match = re.search(r"HEADER\.FIELDS \(([^)]*)\)", lowered)
        if match:
            fields = match.group(1).split()
        return lowered, fields

    def _fetch(self, messages, uidset, items, modifier=None):
        wanted = set()
        for part in str(uidset).split(","):
            low, _, high = part.partition(":")
            top = max([m["uid"] for m in messages] or [0])
            high = top if high == "*" else int(high or low)
            wanted.update(range(int(low), high + 1))
        since = None
        if modifier:
            match = re.search(r"CHANGEDSINCE (\d+)", str(modifier).upper())
            since = int(match.group(1)) if match else None
        spec, header_fields = self._wanted(items)
        gmail = "X-GM-EXT-1" in self.full_capabilities
        out = []
        for seq, message in enumerate(messages, 1):
            if message["uid"] not in wanted:
                continue
            if since is not None and message.get("modseq", 1) <= since:
                continue
            head = [f"UID {message['uid']}", f"FLAGS {_flags(message.get('flags') or [])}"]
            if since is not None or "MODSEQ" in spec:
                head.append(f"MODSEQ ({message.get('modseq', 1)})")
            if since is not None and "BODY" not in spec:
                out.append(f"{seq} ({' '.join(head)})".encode())
                continue
            head.append(f'INTERNALDATE "{message["internaldate"]}"')
            raw = _raw(message)
            head.append(f"RFC822.SIZE {len(raw)}")
            tail = []
            if gmail:
                labels = " ".join(
                    lab if lab.startswith("\\") else _qstr(lab) for lab in message.get("x_gm_labels") or []
                )
                tail = [
                    f"X-GM-THRID {message.get('x_gm_thrid', '0')}",
                    f"X-GM-MSGID {message.get('x_gm_msgid', '0')}",
                    f"X-GM-LABELS ({labels})",
                ]
            if header_fields is not None:
                literal = _headers(raw, header_fields)
                section = "BODY[HEADER.FIELDS (" + " ".join(header_fields) + ")]"
            elif "BODY.PEEK[]" in spec or "BODY[]" in spec:
                literal, section = raw, "BODY[]"
            else:
                out.append(f"{seq} ({' '.join(head + tail)})".encode())
                continue
            # Real servers may put attributes after the literal: do so here.
            out.append((f"{seq} ({' '.join(head)} {section} {{{len(literal)}}}".encode(), literal))
            out.append((" " + " ".join(tail) + ")").encode() if tail else b")")
        return out


def _headers(raw, names):
    head = raw.split(b"\r\n\r\n", 1)[0]
    lines, current = [], []
    for line in head.split(b"\r\n"):
        if line[:1] in (b" ", b"\t") and current:
            current.append(line)
            continue
        if current:
            lines.append(b"\r\n".join(current))
        current = [line]
    if current:
        lines.append(b"\r\n".join(current))
    wanted = {n.upper().encode() for n in names}
    kept = [line for line in lines if line.split(b":", 1)[0].strip().upper() in wanted]
    return b"\r\n".join(kept) + b"\r\n\r\n"

