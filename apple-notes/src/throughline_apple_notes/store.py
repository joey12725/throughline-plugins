"""Read Apple Notes' NoteStore.sqlite from a private copy.

Core Data column names drift between macOS releases (ZTITLE vs ZTITLE1,
ZCREATIONDATE1 vs ZCREATIONDATE3, ...), so every column is picked from a
list of known candidates present in the table.
"""

from __future__ import annotations

import datetime as dt
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path

APPLE_EPOCH = dt.datetime(2001, 1, 1, tzinfo=dt.UTC)
DEFAULT_DB = "~/Library/Group Containers/group.com.apple.notes/NoteStore.sqlite"
TRASH_FOLDER_TYPE = 1

CANDIDATES = {
    "note_title": ("ZTITLE1", "ZTITLE"),
    "folder_title": ("ZTITLE2", "ZTITLE"),
    "created": ("ZCREATIONDATE3", "ZCREATIONDATE1", "ZCREATIONDATE"),
    "modified": ("ZMODIFICATIONDATE1", "ZMODIFICATIONDATE"),
    "locked": ("ZISPASSWORDPROTECTED",),
    "deleted": ("ZMARKEDFORDELETION",),
    "pinned": ("ZISPINNED",),
    "snippet": ("ZSNIPPET",),
    "folder": ("ZFOLDER",),
    "folder_type": ("ZFOLDERTYPE",),
    "parent": ("ZPARENT",),
    "owner": ("ZOWNER",),
    "account_name": ("ZNAME",),
    "account_type": ("ZACCOUNTTYPE",),
    "shared": ("ZSERVERSHAREDATA", "ZSHARETYPE"),
}


def apple_time(value):
    if value is None:
        return None
    try:
        return (APPLE_EPOCH + dt.timedelta(seconds=float(value))).replace(microsecond=0)
    except (TypeError, ValueError, OverflowError):
        return None


@contextmanager
def snapshot(path):
    """Copy the database and its -wal/-shm into a temporary folder, so the
    live store is never opened, locked or written."""
    path = Path(path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(
            f"Apple Notes database not found at {path} (grant Full Disk Access, or set APPLE_NOTES_DB)"
        )
    with tempfile.TemporaryDirectory(prefix="tl-apple-notes-") as folder:
        target = Path(folder) / path.name
        shutil.copy2(path, target)
        for suffix in ("-wal", "-shm"):
            side = path.with_name(path.name + suffix)
            if side.is_file():
                shutil.copy2(side, target.with_name(target.name + suffix))
        conn = sqlite3.connect(target)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()


class Store:
    def __init__(self, conn):
        self.conn = conn
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "ZICCLOUDSYNCINGOBJECT" not in tables or "ZICNOTEDATA" not in tables:
            raise ValueError(
                "not a modern Apple Notes store (ZICCLOUDSYNCINGOBJECT/ZICNOTEDATA missing); "
                "notes from before macOS 10.11 are not supported"
            )
        self.columns = {r[1] for r in conn.execute("PRAGMA table_info(ZICCLOUDSYNCINGOBJECT)")}
        self.pick = {key: next((c for c in options if c in self.columns), None) for key, options in CANDIDATES.items()}
        self.entities = {}
        if "Z_PRIMARYKEY" in tables:
            self.entities = {r["Z_NAME"]: r["Z_ENT"] for r in conn.execute("SELECT Z_ENT, Z_NAME FROM Z_PRIMARYKEY")}
        self.accounts = self._accounts()
        self.folders = self._folders()

    def col(self, key, alias=None):
        name = self.pick.get(key)
        return f"{name} AS {alias or key}" if name else f"NULL AS {alias or key}"

    def _entity_filter(self, name, fallback):
        ent = self.entities.get(name)
        return f"Z_ENT = {int(ent)}" if ent is not None else fallback

    def _accounts(self):
        if not self.pick["account_name"]:
            return {}
        where = self._entity_filter("ICAccount", f"{self.pick['account_name']} IS NOT NULL")
        rows = self.conn.execute(
            f"SELECT Z_PK, ZIDENTIFIER, {self.col('account_name', 'name')}, {self.col('account_type', 'type')} "
            f"FROM ZICCLOUDSYNCINGOBJECT WHERE {where}"
        )
        return {r["Z_PK"]: {"id": r["ZIDENTIFIER"], "name": r["name"], "type": r["type"]} for r in rows}

    def _folders(self):
        if not self.pick["folder_title"]:
            return {}
        where = self._entity_filter("ICFolder", f"{self.pick['folder_title']} IS NOT NULL")
        rows = self.conn.execute(
            f"SELECT Z_PK, ZIDENTIFIER, {self.col('folder_title', 'title')}, {self.col('folder_type', 'type')}, "
            f"{self.col('parent')}, {self.col('owner')} FROM ZICCLOUDSYNCINGOBJECT WHERE {where}"
        )
        folders = {r["Z_PK"]: dict(r) for r in rows}
        for folder in folders.values():
            names, seen, current = [], set(), folder
            while current and current["Z_PK"] not in seen:
                seen.add(current["Z_PK"])
                names.append(current["title"] or "Untitled folder")
                current = folders.get(current["parent"])
            folder["path"] = "/".join(reversed(names))
        return folders

    def notes(self):
        """Every note row, oldest modification first, with its body blob."""
        where = self._entity_filter("ICNote", "n.ZNOTEDATA IS NOT NULL")
        cols = ", ".join(
            "n." + self.col(key) if self.pick.get(key) else self.col(key)
            for key in ("note_title", "created", "modified", "locked", "deleted", "pinned", "snippet", "folder", "shared")
        )
        accounts = [c for c in sorted(self.columns) if c.startswith("ZACCOUNT") and c[8:].isdigit()]
        account_expr = "COALESCE(" + ", ".join(f"n.{c}" for c in accounts) + ")" if accounts else "NULL"
        query = (
            f"SELECT n.Z_PK, n.ZIDENTIFIER, {cols}, {account_expr} AS account, d.ZDATA AS data "
            f"FROM ZICCLOUDSYNCINGOBJECT n LEFT JOIN ZICNOTEDATA d ON d.Z_PK = n.ZNOTEDATA "
            f"WHERE {where.replace('Z_ENT', 'n.Z_ENT')} AND n.ZIDENTIFIER IS NOT NULL "
            f"ORDER BY modified, n.ZIDENTIFIER"
        )
        for row in self.conn.execute(query):
            note = dict(row)
            folder = self.folders.get(note["folder"]) or {}
            account = self.accounts.get(folder.get("owner")) or self.accounts.get(note["account"]) or {}
            note["folder_row"] = folder
            note["account_row"] = account
            yield note
