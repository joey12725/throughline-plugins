"""A thin, read-only layer over an imaplib.IMAP4-like connection.

Only the calls below are used, so tests (and IMAP_FIXTURE) can pass any
object with the same methods: login, capability, list, status, select,
uid, logout. Every mailbox is opened read-only (EXAMINE) and bodies are
fetched with BODY.PEEK[], so the connector never changes flags.
"""

import base64
import re
from datetime import datetime


class ImapError(RuntimeError):
    """A command answered NO/BAD, or the connection failed."""


# --- response parsing -------------------------------------------------------


class _Lit(bytes):
    """A literal ({n} string) from a server response."""


def _stream(data):
    """Flatten imaplib response data into a list of text chunks and literals."""
    out = []
    for item in data or []:
        if item is None:
            continue
        if isinstance(item, tuple):
            head, literal = item[0], item[1] if len(item) > 1 else b""
            out.append(head if isinstance(head, bytes) else str(head).encode())
            out.append(_Lit(literal if isinstance(literal, bytes) else str(literal).encode()))
        else:
            out.append(item if isinstance(item, bytes) else str(item).encode())
            out.append(b"\n")  # Response boundary.
    return out


def tokens(data):
    """Tokens of IMAP response data: '(' ')' '\n' atoms (str), quoted strings
    (str), literals (_Lit bytes) and None for NIL. Section specifiers such as
    BODY[HEADER.FIELDS (DATE)] stay one atom."""
    result = []
    for chunk in _stream(data):
        if isinstance(chunk, _Lit):
            # The {n} marker that announced it was already dropped below.
            result.append(chunk)
            continue
        text = chunk.decode("utf-8", "replace")
        i, n = 0, len(text)
        while i < n:
            c = text[i]
            if c in " \r\t":
                i += 1
            elif c == "\n":
                result.append("\n")
                i += 1
            elif c in "()":
                result.append(c)
                i += 1
            elif c == '"':
                j, buf = i + 1, []
                while j < n and text[j] != '"':
                    if text[j] == "\\" and j + 1 < n:
                        j += 1
                    buf.append(text[j])
                    j += 1
                result.append(_Quoted("".join(buf)))
                i = j + 1
            elif c == "{":
                j = text.find("}", i)
                i = (j + 1) if j >= 0 else n  # Literal follows as its own chunk.
            else:
                j, depth = i, 0
                while j < n:
                    ch = text[j]
                    if ch == "[":
                        depth += 1
                    elif ch == "]":
                        depth -= 1
                    elif depth <= 0 and ch in ' ()"\r\n':
                        break
                    j += 1
                atom = text[i:j]
                result.append(None if atom.upper() == "NIL" else atom)
                i = j
    return result


class _Quoted(str):
    """A quoted string (never NIL, never an atom keyword)."""


def _parse_list(toks, i):
    """Parse a parenthesized list starting after '(' -> (list, next index)."""
    items = []
    while i < len(toks):
        tok = toks[i]
        if tok == "(" and not isinstance(tok, _Quoted):
            sub, i = _parse_list(toks, i + 1)
            items.append(sub)
            continue
        if tok == ")" and not isinstance(tok, _Quoted):
            return items, i + 1
        if tok == "\n":
            i += 1
            continue
        items.append(tok)
        i += 1
    return items, i


def parse_fetch(data):
    """{uid: {ITEM: value}} from a UID FETCH answer. Keys are upper case;
    literal values are bytes; FLAGS and X-GM-LABELS are lists."""
    toks = tokens(data)
    result = {}
    i = 0
    while i < len(toks):
        tok = toks[i]
        if tok == "(" and not isinstance(tok, _Quoted):
            items, i = _parse_list(toks, i + 1)
            attrs = {}
            for k in range(0, len(items) - 1, 2):
                key = items[k]
                if isinstance(key, str):
                    attrs[key.upper()] = items[k + 1]
            uid = attrs.get("UID")
            if uid is not None and str(uid).isdigit():
                # A server may split one message's attributes over responses.
                result.setdefault(int(uid), {}).update(attrs)
            continue
        i += 1
    return result


def parse_list_line(line):
    """(flags, delimiter, name) from one LIST response line (or tuple)."""
    toks = [t for t in tokens([line]) if t != "\n"]
    if not toks or toks[0] != "(":
        raise ValueError("unexpected LIST line")
    flags, i = _parse_list(toks, 1)
    delimiter = toks[i] if i < len(toks) else None
    name = toks[i + 1] if i + 1 < len(toks) else ""
    if isinstance(name, bytes):
        name = name.decode("utf-8", "replace")
    return [str(f) for f in flags], delimiter, str(name)


def parse_status(data):
    toks = [t for t in tokens(data) if t != "\n"]
    start = next((k for k, t in enumerate(toks) if t == "("), None)
    if start is None:
        return {}
    items, _ = _parse_list(toks, start + 1)
    return {
        str(items[k]).upper(): int(items[k + 1])
        for k in range(0, len(items) - 1, 2)
        if str(items[k + 1]).isdigit()
    }


def decode_utf7(name):
    """Mailbox names use modified UTF-7 (RFC 3501 5.1.3)."""

    def part(match):
        chunk = match.group(1)
        if not chunk:
            return "&"
        chunk = chunk.replace(",", "/")
        chunk += "=" * (-len(chunk) % 4)
        try:
            return base64.b64decode(chunk).decode("utf-16-be")
        except (ValueError, UnicodeDecodeError):
            return match.group(0)

    return re.sub(r"&([A-Za-z0-9+,]*)-", part, name)


def quote(name):
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


_MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def internal_date(value):
    """An aware datetime from an IMAP INTERNALDATE ("17-Jul-1996 02:44:25 -0700")."""
    if not value:
        return None
    try:
        return datetime.strptime(str(value).strip(), "%d-%b-%Y %H:%M:%S %z")
    except ValueError:
        return None


def search_date(day):
    """IMAP SEARCH date (locale independent): 01-Jan-2024."""
    return f"{day.day:02d}-{_MONTHS[day.month - 1]}-{day.year}"


def uid_set(uids):
    """Compact UID set: 1:3,7,9:10."""
    uids = sorted(set(uids))
    parts, start, prev = [], None, None
    for uid in uids:
        if start is None:
            start = prev = uid
        elif uid == prev + 1:
            prev = uid
        else:
            parts.append(f"{start}:{prev}" if start != prev else str(start))
            start = prev = uid
    if start is not None:
        parts.append(f"{start}:{prev}" if start != prev else str(start))
    return ",".join(parts)


# --- session ------------------------------------------------------------------


class Session:
    def __init__(self, conn):
        """`conn` must be logged in: servers announce extensions such as
        CONDSTORE and X-GM-EXT-1 only after authentication."""
        self.conn = conn
        self.selected = None
        caps = ()
        try:
            typ, data = conn.capability()
            if typ == "OK" and data:
                caps = b" ".join(d for d in data if isinstance(d, bytes)).split()
        except Exception:  # noqa: BLE001 - fall back to the greeting's list
            caps = ()
        if not caps:
            caps = getattr(conn, "capabilities", None) or ()
        self.capabilities = {
            (c.decode() if isinstance(c, bytes) else str(c)).upper() for c in caps
        }

    def _ok(self, answer, what):
        typ, data = answer
        if typ != "OK":
            detail = b" ".join(d for d in data or [] if isinstance(d, bytes))
            raise ImapError(f"{what} failed: {typ} {detail[:200].decode('utf-8', 'replace')}")
        return data

    @property
    def gmail(self):
        return "X-GM-EXT-1" in self.capabilities

    @property
    def condstore(self):
        return "CONDSTORE" in self.capabilities or "QRESYNC" in self.capabilities

    def folders(self):
        """[{name (wire name), display, flags, special}] for selectable folders."""
        data = self._ok(self.conn.list(), "LIST")
        result = []
        for line in data or []:
            if line is None:
                continue
            try:
                flags, _, name = parse_list_line(line)
            except ValueError:
                continue
            lowered = {f.lower() for f in flags}
            if "\\noselect" in lowered or "\\nonexistent" in lowered:
                continue
            special = next(
                (
                    f
                    for f in ("\\All", "\\Sent", "\\Drafts", "\\Junk", "\\Trash", "\\Archive", "\\Flagged")
                    if f.lower() in lowered
                ),
                None,
            )
            result.append(
                {"name": name, "display": decode_utf7(name), "flags": flags, "special": special}
            )
        return result

    def status(self, name):
        items = "(UIDVALIDITY UIDNEXT MESSAGES" + (" HIGHESTMODSEQ" if self.condstore else "") + ")"
        return parse_status(self._ok(self.conn.status(quote(name), items), "STATUS"))

    def examine(self, name):
        if self.selected != name:
            self._ok(self.conn.select(quote(name), readonly=True), "EXAMINE")
            self.selected = name

    def search(self, *criteria):
        data = self._ok(self.conn.uid("SEARCH", None, *criteria), "UID SEARCH")
        found = []
        for line in data or []:
            if isinstance(line, bytes):
                found.extend(int(x) for x in line.split() if x.isdigit())
        return sorted(set(found))

    def fetch(self, uids, items, modifier=None, chunk=200):
        result = {}
        uids = sorted(set(uids))
        for start in range(0, len(uids), chunk):
            part = uid_set(uids[start : start + chunk])
            args = [part, items] + ([modifier] if modifier else [])
            result.update(parse_fetch(self._ok(self.conn.uid("FETCH", *args), "UID FETCH")))
        return result

    def changed_since(self, last_uid, modseq):
        """UIDs <= last_uid whose flags changed after modseq (CONDSTORE)."""
        if last_uid < 1:
            return []
        data = self._ok(
            self.conn.uid("FETCH", f"1:{last_uid}", "(UID FLAGS)", f"(CHANGEDSINCE {modseq})"),
            "UID FETCH CHANGEDSINCE",
        )
        return sorted(uid for uid in parse_fetch(data) if uid <= last_uid)

    def close(self):
        try:
            self.conn.logout()
        except Exception:  # noqa: BLE001 - best effort on the way out
            pass
