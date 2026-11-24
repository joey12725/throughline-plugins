"""Build a synthetic NoteStore.sqlite following the documented macOS schema.

All content is invented. Bodies are gzipped NoteStoreProto messages written
with a tiny protobuf encoder.
"""

import gzip
import sqlite3

APPLE_EPOCH_UNIX = 978307200


def varint(value):
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def field(number, value):
    if isinstance(value, int):
        return varint(number << 3) + varint(value)
    if isinstance(value, str):
        value = value.encode()
    return varint(number << 3 | 2) + varint(len(value)) + value


def run(length, style=None, indent=0, checked=None, link=None, uti=None):
    body = field(1, length)
    if style is not None or checked is not None:
        para = field(1, style if style is not None else 103)
        if indent:
            para += field(4, indent)
        if checked is not None:
            para += field(5, field(1, b"\x01\x02") + field(2, 1 if checked else 0))
        body += field(2, para)
    if link:
        body += field(9, link)
    if uti:
        body += field(12, field(1, "ATT-1") + field(2, uti))
    return field(5, body)


def utf16(text):
    return len(text.encode("utf-16-le")) // 2


def body(lines):
    """lines: [(text, run kwargs)] joined with newlines."""
    text = ""
    runs = b""
    for index, (line, options) in enumerate(lines):
        chunk = line + ("\n" if index < len(lines) - 1 else "")
        text += chunk
        runs += run(utf16(chunk), **options)
    note = field(2, text) + runs
    return gzip.compress(field(2, field(2, 1) + field(3, note)), mtime=0)


def stamp(unix):
    return unix - APPLE_EPOCH_UNIX


class FakeStore:
    def __init__(self, path):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.executescript(
            """
            CREATE TABLE Z_PRIMARYKEY (Z_ENT INTEGER PRIMARY KEY, Z_NAME VARCHAR, Z_SUPER INTEGER, Z_MAX INTEGER);
            INSERT INTO Z_PRIMARYKEY VALUES (12, 'ICNote', 0, 0), (15, 'ICFolder', 0, 0), (14, 'ICAccount', 0, 0);
            CREATE TABLE ZICCLOUDSYNCINGOBJECT (
              Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, ZIDENTIFIER VARCHAR,
              ZTITLE1 VARCHAR, ZTITLE2 VARCHAR, ZNAME VARCHAR, ZACCOUNTTYPE INTEGER,
              ZSNIPPET VARCHAR, ZFOLDER INTEGER, ZNOTEDATA INTEGER, ZFOLDERTYPE INTEGER,
              ZPARENT INTEGER, ZOWNER INTEGER, ZACCOUNT3 INTEGER, ZACCOUNT4 INTEGER,
              ZCREATIONDATE1 TIMESTAMP, ZCREATIONDATE3 TIMESTAMP, ZMODIFICATIONDATE1 TIMESTAMP,
              ZISPASSWORDPROTECTED INTEGER, ZMARKEDFORDELETION INTEGER, ZISPINNED INTEGER,
              ZSERVERSHAREDATA BLOB
            );
            CREATE TABLE ZICNOTEDATA (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, ZNOTE INTEGER, ZDATA BLOB);
            """
        )
        self.next_pk = 1

    def pk(self):
        self.next_pk += 1
        return self.next_pk

    def account(self, ident, name):
        pk = self.pk()
        self.conn.execute(
            "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, Z_ENT, ZIDENTIFIER, ZNAME, ZACCOUNTTYPE) VALUES (?,?,?,?,1)",
            (pk, 14, ident, name),
        )
        return pk

    def folder(self, ident, title, owner, parent=None, kind=0):
        pk = self.pk()
        self.conn.execute(
            "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, Z_ENT, ZIDENTIFIER, ZTITLE2, ZOWNER, ZPARENT, ZFOLDERTYPE) VALUES (?,?,?,?,?,?,?)",
            (pk, 15, ident, title, owner, parent, kind),
        )
        return pk

    def note(self, ident, title, folder, lines, created, modified, locked=0, pinned=0, deleted=0):
        pk = self.pk()
        data_pk = self.pk()
        self.conn.execute("INSERT INTO ZICNOTEDATA VALUES (?,?,?,?)", (data_pk, 19, pk, body(lines)))
        self.conn.execute(
            "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, Z_ENT, ZIDENTIFIER, ZTITLE1, ZSNIPPET, ZFOLDER, ZNOTEDATA,"
            " ZCREATIONDATE3, ZMODIFICATIONDATE1, ZISPASSWORDPROTECTED, ZMARKEDFORDELETION, ZISPINNED)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (pk, 12, ident, title, (lines[1][0] if len(lines) > 1 else ""), folder, data_pk,
             stamp(created), stamp(modified), locked, deleted, pinned),
        )
        self.conn.commit()
        return pk

    def update(self, ident, lines, modified):
        row = self.conn.execute("SELECT ZNOTEDATA FROM ZICCLOUDSYNCINGOBJECT WHERE ZIDENTIFIER=?", (ident,)).fetchone()
        self.conn.execute("UPDATE ZICNOTEDATA SET ZDATA=? WHERE Z_PK=?", (body(lines), row[0]))
        self.conn.execute("UPDATE ZICCLOUDSYNCINGOBJECT SET ZMODIFICATIONDATE1=? WHERE ZIDENTIFIER=?", (stamp(modified), ident))
        self.conn.commit()

    def purge(self, ident):
        self.conn.execute("DELETE FROM ZICCLOUDSYNCINGOBJECT WHERE ZIDENTIFIER=?", (ident,))
        self.conn.commit()


def standard(path):
    """The invented store used by the tests."""
    store = FakeStore(path)
    icloud = store.account("ACCOUNT-ICLOUD-0000", "iCloud")
    notes = store.folder("FOLDER-NOTES-0000", "Notes", icloud)
    recipes = store.folder("FOLDER-RECIPES-0000", "Invented Recipes", icloud, parent=notes)
    trash = store.folder("FOLDER-TRASH-0000", "Recently Deleted", icloud, kind=1)
    base = 1_735_000_000  # December 2024
    store.note("NOTE-0001", "Invented Grocery List", notes, [
        ("Invented Grocery List", {"style": 0}),
        ("Apples", {"style": 103, "checked": True}),
        ("Invented pears 🍐", {"style": 103, "checked": False}),
        ("See example.com", {"link": "https://example.com/list"}),
    ], base, base + 100, pinned=1)
    store.note("NOTE-0002", "Invented Soup", recipes, [
        ("Invented Soup", {"style": 0}),
        ("Ingredients", {"style": 1}),
        ("Invented carrots", {"style": 100}),
        ("Invented leeks", {"style": 100, "indent": 1}),
        ("Step one", {"style": 102}),
        ("Step two", {"style": 102}),
        ("Photo: ￼", {"uti": "public.jpeg"}),
    ], base + 200, base + 300)
    store.note("NOTE-0003", "Invented Secret", notes, [("Invented Secret", {}), ("hidden", {})], base, base + 400, locked=1)
    store.note("NOTE-0004", "Invented Old Idea", trash, [("Invented Old Idea", {}), ("Discarded invented idea", {})],
               base, base + 500)
    store.note("NOTE-0005", "Invented Book Notes", notes, [("Invented Book Notes", {}), ("Chapter one", {})],
               base + 600, base + 700)
    store.note("NOTE-0006", "Invented Gone", notes, [("Invented Gone", {})], base, base + 800, deleted=1)
    store.conn.commit()
    return store
