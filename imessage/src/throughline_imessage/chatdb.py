"""Read-only access to the macOS Messages database (~/Library/Messages/chat.db).

Standard library only. The database is opened with a `mode=ro` URI (falling
back to `immutable=1` when a read-only copy cannot take the WAL lock); it is
never written. Optional columns that only exist on newer macOS versions
(`date_edited`, `date_retracted`, `thread_originator_guid`,
`associated_message_emoji`, `destination_caller_id`, ...) are detected with
PRAGMA table_info, so the same reader works from macOS 10.13 onward.
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

# Columns read when present; ROWID, guid and date are required.
MESSAGE_COLUMNS = (
    "guid",
    "text",
    "attributedBody",
    "handle_id",
    "service",
    "account",
    "date",
    "is_from_me",
    "item_type",
    "group_title",
    "group_action_type",
    "associated_message_guid",
    "associated_message_type",
    "associated_message_emoji",
    "thread_originator_guid",
    "date_edited",
    "date_retracted",
    "cache_has_attachments",
    "destination_caller_id",
    "balloon_bundle_id",
    "expressive_send_style_id",
)
ATTACHMENT_COLUMNS = (
    "guid",
    "filename",
    "mime_type",
    "transfer_name",
    "total_bytes",
    "uti",
    "is_sticker",
)
# Tapbacks: 2000-2007 add a reaction, 3000-3007 remove the same kind.
TAPBACK_MIN, TAPBACK_MAX = 2000, 3999


def connect(path):
    uri = Path(path).expanduser().resolve().as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=30)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
    except sqlite3.OperationalError as error:
        if "unable to open" in str(error) and not Path(path).expanduser().exists():
            raise
        # Without Full Disk Access macOS refuses the open itself; on a
        # read-only copy the WAL lock cannot be taken, so read it immutable.
        conn = sqlite3.connect(uri + "&immutable=1", uri=True)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
    conn.row_factory = sqlite3.Row
    return conn


def apple_time(value):
    """Apple epoch time (nanoseconds on macOS 10.13+, seconds before) as UTC."""
    if value is None or value == 0:
        return None
    value = int(value)
    if abs(value) > 1e11:
        return APPLE_EPOCH + timedelta(microseconds=value // 1000)
    return APPLE_EPOCH + timedelta(seconds=value)


def apple_units(value):
    """Comparable integer for a stored Apple time (seconds scaled to ns)."""
    if not value:
        return 0
    value = int(value)
    return value if abs(value) > 1e11 else value * 1_000_000_000


def _length(data, pos):
    """A typedstream integer: one byte, or 0x81 + int16 / 0x82 + int32 (LE)."""
    tag = data[pos]
    if tag == 0x81:
        return int.from_bytes(data[pos + 1 : pos + 3], "little", signed=True), pos + 3
    if tag == 0x82:
        return int.from_bytes(data[pos + 1 : pos + 5], "little", signed=True), pos + 5
    return tag, pos + 1


def decode_attributed_body(blob):
    """Plain text of an `attributedBody` typedstream (NSAttributedString) blob.

    Newer macOS leaves `message.text` NULL and stores the text only in this
    archived NSAttributedString. The common case is a single NSString whose
    UTF-8 bytes follow the class name, a `+` (C string) type tag and a
    typedstream length. Returns None when the blob has no such string.
    """
    if not blob:
        return None
    data = bytes(blob)
    for marker in (b"NSString", b"NSMutableString"):
        start = data.find(marker)
        if start < 0:
            continue
        plus = data.find(b"+", start + len(marker), start + len(marker) + 12)
        if plus < 0:
            continue
        try:
            length, pos = _length(data, plus + 1)
        except IndexError:
            return None
        if length < 0 or pos + length > len(data):
            return None
        return data[pos : pos + length].decode("utf-8", errors="replace")
    return None


def tapback_target(value):
    """GUID a tapback refers to: 'p:0/GUID' (part 0 of GUID) or 'bp:GUID'."""
    if not value:
        return None
    if "/" in value:
        return value.split("/", 1)[1]
    if ":" in value:
        return value.split(":", 1)[1]
    return value


class ChatDB:
    def __init__(self, path):
        self.path = Path(path).expanduser()
        self.conn = connect(self.path)
        self.columns = self._columns("message")
        missing = {"guid", "date"} - self.columns
        if missing:
            raise ValueError("not a Messages database: message table lacks guid/date")
        self.attachment_columns = self._columns("attachment")
        self._chats = None

    def close(self):
        self.conn.close()

    def _columns(self, table):
        return {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}

    def _select(self):
        cols = ", ".join(
            f"m.{c}" if c in self.columns else f"NULL AS {c}" for c in MESSAGE_COLUMNS
        )
        return (
            f"SELECT m.ROWID AS rowid, {cols}, h.id AS handle,"
            " (SELECT MIN(chat_id) FROM chat_message_join j WHERE j.message_id=m.ROWID)"
            " AS chat_id FROM message m LEFT JOIN handle h ON h.ROWID=m.handle_id"
        )

    def max_rowid(self):
        return self.conn.execute("SELECT COALESCE(MAX(ROWID),0) FROM message").fetchone()[0]

    def change_high_water(self):
        parts = [c for c in ("date_edited", "date_retracted") if c in self.columns]
        if not parts:
            return 0
        row = self.conn.execute(
            "SELECT " + ", ".join(f"MAX({c})" for c in parts) + " FROM message"
        ).fetchone()
        return max(apple_units(v) for v in row)

    def messages_after(self, rowid, limit, upto=None):
        sql = self._select() + " WHERE m.ROWID > ?"
        args = [rowid]
        if upto is not None:
            sql += " AND m.ROWID <= ?"
            args.append(upto)
        sql += " ORDER BY m.ROWID LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.conn.execute(sql, args)]

    def changed_since(self, high_water, upto):
        """Messages at or below ROWID `upto` edited or unsent after high_water."""
        parts = [c for c in ("date_edited", "date_retracted") if c in self.columns]
        if not parts:
            return []
        # Stored values are ns (or seconds on old systems): compare both scales.
        where = " OR ".join(
            f"(m.{c} > ? OR (m.{c} > 0 AND m.{c} < 100000000000 AND m.{c} > ?))"
            for c in parts
        )
        args = []
        for _ in parts:
            args += [high_water, high_water // 1_000_000_000]
        rows = self.conn.execute(
            self._select() + f" WHERE m.ROWID <= ? AND ({where}) ORDER BY m.ROWID",
            [upto, *args],
        )
        return [dict(r) for r in rows]

    def message_by_guid(self, guid):
        row = self.conn.execute(self._select() + " WHERE m.guid = ?", (guid,)).fetchone()
        return dict(row) if row else None

    def tapbacks(self):
        """All tapback rows, grouped by the GUID of the message they target."""
        if "associated_message_type" not in self.columns:
            return {}
        emoji = (
            "m.associated_message_emoji"
            if "associated_message_emoji" in self.columns
            else "NULL"
        )
        rows = self.conn.execute(
            "SELECT m.ROWID AS rowid, m.guid, m.associated_message_guid,"
            f" m.associated_message_type, {emoji} AS associated_message_emoji,"
            " m.is_from_me, m.date, h.id AS handle FROM message m"
            " LEFT JOIN handle h ON h.ROWID=m.handle_id"
            " WHERE m.associated_message_type BETWEEN ? AND ? ORDER BY m.date, m.ROWID",
            (TAPBACK_MIN, TAPBACK_MAX),
        )
        grouped = {}
        for row in rows:
            row = dict(row)
            target = tapback_target(row["associated_message_guid"])
            if target:
                grouped.setdefault(target, []).append(row)
        return grouped

    def attachments(self, rowids):
        if not rowids or not self.attachment_columns:
            return {}
        cols = ", ".join(
            f"a.{c}" if c in self.attachment_columns else f"NULL AS {c}"
            for c in ATTACHMENT_COLUMNS
        )
        marks = ",".join("?" * len(rowids))
        found = {}
        for row in self.conn.execute(
            f"SELECT j.message_id, {cols} FROM message_attachment_join j"
            f" JOIN attachment a ON a.ROWID=j.attachment_id"
            f" WHERE j.message_id IN ({marks}) ORDER BY j.message_id, a.ROWID",
            list(rowids),
        ):
            row = dict(row)
            found.setdefault(row.pop("message_id"), []).append(row)
        return found

    def chats(self):
        """chat ROWID -> {guid, identifier, service, display_name, style, participants}."""
        if self._chats is None:
            chat_cols = self._columns("chat")
            pick = ", ".join(
                c if c in chat_cols else f"NULL AS {c}"
                for c in ("guid", "chat_identifier", "service_name", "display_name", "style")
            )
            chats = {
                r["ROWID"]: dict(r, participants=[])
                for r in self.conn.execute(f"SELECT ROWID, {pick} FROM chat")
            }
            for r in self.conn.execute(
                "SELECT j.chat_id, h.id FROM chat_handle_join j"
                " JOIN handle h ON h.ROWID=j.handle_id ORDER BY j.chat_id, h.id"
            ):
                if r[0] in chats:
                    chats[r[0]]["participants"].append(r[1])
            self._chats = chats
        return self._chats
