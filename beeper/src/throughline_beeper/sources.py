"""Read-only source adapters, with invented recordings replacing every source call."""

import json
import re
import time
from pathlib import Path
from urllib.parse import quote

import httpx
import psycopg
from psycopg.rows import dict_row

from .mapping import network


class SourceError(RuntimeError):
    """A Desktop API request failed after its retries.

    status is the HTTP status (None for a transport failure). Authentication
    failures (401, 403) stop the run; other failures cost only the chat whose
    history was being loaded (an explicit coverage gap, retried next run).
    """

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status

    @property
    def auth(self):
        return self.status in (401, 403)


class Desktop:
    def __init__(self, base_url, token, recording=None, transport=None):
        self.recording = (
            json.loads(Path(recording).read_text(encoding="utf-8"))
            if recording
            else None
        )
        self.http = httpx.Client(
            base_url=base_url.rstrip("/") + "/",
            timeout=60,
            headers={"Authorization": "Bearer " + (token or "")},
            transport=transport,
        )

    def close(self):
        self.http.close()

    def get(self, path, params=None, *, body=None):
        params = params or {}
        if self.recording is not None:
            for response in self.recording["responses"]:
                if response["path"] == path and response.get("params", {}) == (
                    params if body is None else body
                ):
                    if "error" in response:
                        # An invented failure: the source answered with an error.
                        raise SourceError(
                            f"Beeper request failed (HTTP {response['error']})",
                            response["error"],
                        )
                    return response["body"]
            raise ValueError("invented recording lacks requested page")
        for attempt in range(5):
            try:
                response = (
                    self.http.get(path, params=params)
                    if body is None
                    else self.http.post(path, json=body)
                )
            except httpx.TransportError:
                if attempt == 4:
                    raise SourceError("Beeper transport failed") from None
                time.sleep(min(2**attempt, 8))
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == 4:
                    raise SourceError(
                        "Beeper retry budget exhausted", response.status_code
                    )
                try:
                    delay = float(response.headers.get("Retry-After", 2**attempt))
                except ValueError:
                    delay = 2**attempt
                time.sleep(max(0, min(delay, 60)))
                continue
            if response.is_error:
                raise SourceError(
                    f"Beeper request failed (HTTP {response.status_code})",
                    response.status_code,
                )
            return response.json()

    def accounts(self):
        return self.get("v1/accounts")

    def download_asset(self, url):
        """Local file URL of an mxc:// or localmxc:// attachment, or None.

        Beeper Desktop downloads the asset to its own device and answers with
        a file URL there (POST /v1/assets/download). Read-only for the chat.
        """
        result = self.get("v1/assets/download", body={"url": url})
        if not isinstance(result, dict) or result.get("error"):
            return None
        return result.get("srcURL") or None

    def chats(self):
        params = {"limit": 200}
        seen = set()
        while True:
            page = self.get("v1/chats", params)
            yield from page["items"]
            if not page["hasMore"]:
                return
            cursor = page["oldestCursor"]
            if not cursor or cursor in seen:
                raise ValueError("Beeper chat pagination made no progress")
            seen.add(cursor)
            params = {"limit": 200, "cursor": cursor, "direction": "before"}

    def pages(self, chat_id, cursor=None, direction="before"):
        seen = set()
        while True:
            page = self.get(
                "v1/chats/" + quote(chat_id, safe="") + "/messages",
                {"cursor": cursor, "direction": direction} if cursor else {},
            )
            yield page
            if not page["hasMore"]:
                return
            next_cursor = page[
                "oldestCursor" if direction == "before" else "newestCursor"
            ]
            if not next_cursor or next_cursor == cursor or next_cursor in seen:
                raise ValueError("Beeper message pagination made no progress")
            seen.add(next_cursor)
            cursor = next_cursor


class Archive:
    def __init__(self, dsn=None, recording=None):
        self.fixture = (
            json.loads(Path(recording).read_text(encoding="utf-8"))
            if recording
            else None
        )
        self.conn = None
        if self.fixture is None:
            try:
                self.conn = psycopg.connect(
                    dsn,
                    row_factory=dict_row,
                    options="-c default_transaction_read_only=on",
                )
            except psycopg.Error:
                raise RuntimeError(
                    "archive connection failed; check BEEPER_ARCHIVE_DSN"
                ) from None

    def close(self):
        if self.conn:
            self.conn.close()

    def table(self, name):
        if self.fixture is not None:
            return self.fixture[name]
        # name is an internal constant, never user input.
        return self.conn.execute("SELECT * FROM archive." + name).fetchall()

    def networks(self):
        if self.fixture is not None:
            return {network(r.get("platform")) for r in self.fixture["messages"]}
        return {
            network(r["platform"])
            for r in self.conn.execute("SELECT DISTINCT platform FROM archive.messages")
        }

    @staticmethod
    def position(row):
        return [row.get("conversation_id") or "", row.get("sent_at") or "", row["id"]]

    def batches(self, after="", size=2000, *, links=False):
        if self.fixture is not None:
            rows = sorted(
                (
                    r
                    for r in self.fixture["messages"]
                    if (
                        self.position(r) > after
                        if isinstance(after, list)
                        else r["id"] > after
                    )
                ),
                key=self.position
                if not after or isinstance(after, list)
                else lambda r: r["id"],
            )
            for start in range(0, len(rows), size):
                yield rows[start : start + size]
            return
        # The optional expression index supports the composite seek. Without it,
        # PostgreSQL sorts once and the named cursor streams bounded batches.
        with self.conn.cursor(name="beeper_archive_stream") as cur:
            columns = "*"
            order = 'COALESCE(conversation_id, \'\') COLLATE "C", COALESCE(sent_at, \'\') COLLATE "C", id COLLATE "C"'
            where = f"({order}) > (%s,%s,%s)" if isinstance(after, list) else "id > %s"
            if after and isinstance(after, str):
                order = 'id COLLATE "C"'
            cur.execute(
                "SELECT "
                + columns
                + " FROM archive.messages WHERE "
                + where
                + " ORDER BY "
                + order,
                tuple(after) if isinstance(after, list) else (after,),
            )
            while rows := cur.fetchmany(size):
                yield rows

    CANDIDATE_COLUMNS = (
        "id",
        "platform",
        "sent_at",
        "first_imported_at",
        "body",
        "attachments_json",
    )
    LINK_PATTERN = r"(instagram\.com|instagr\.am|youtube\.com|youtu\.be)"

    def candidates(self, links=True, attachments=True, size=5000):
        """Rows that may hold enrichable links or attachments (a cheap filter).

        Only for planning which items to do first; the stream decides.
        """
        if not (links or attachments):
            return
        if self.fixture is not None:
            for row in self.fixture["messages"]:
                body = row.get("body") or ""
                listed = (row.get("attachments_json") or "").strip()
                if (links and re.search(self.LINK_PATTERN, body, re.IGNORECASE)) or (
                    attachments and listed not in ("", "[]", "null")
                ):
                    yield {k: row.get(k) for k in self.CANDIDATE_COLUMNS}
            return
        conditions = []
        params = []
        if links:
            conditions.append("COALESCE(body, '') ~* %s")
            params.append(self.LINK_PATTERN)
        if attachments:
            conditions.append(
                "COALESCE(attachments_json, '') NOT IN ('', '[]', 'null')"
            )
        # Column names and conditions are internal constants, never user input.
        with self.conn.cursor(name="beeper_enrichment_candidates") as cur:
            cur.execute(
                "SELECT "
                + ", ".join(self.CANDIDATE_COLUMNS)
                + " FROM archive.messages WHERE "
                + " OR ".join(conditions),
                tuple(params),
            )
            while rows := cur.fetchmany(size):
                yield from rows

    def first_dated(self):
        """Earliest dated row of each conversation that has an undated row.

        Undated rows sort first in a conversation, so this row is the next
        dated one after them in stream order; no dated row can precede them.
        Rows without a conversation_id are not a conversation: they share
        nothing but the missing key, so they get no neighbour.
        """
        if self.fixture is not None:
            wanted = {
                r["conversation_id"]
                for r in self.fixture["messages"]
                if not r.get("sent_at") and r.get("conversation_id")
            }
            result = {}
            for r in sorted(self.fixture["messages"], key=self.position):
                key = r.get("conversation_id") or ""
                if key in wanted and r.get("sent_at") and key not in result:
                    result[key] = (r["sent_at"], r["id"], "next")
            return result
        key = "COALESCE(conversation_id, '') COLLATE \"C\""
        return {
            r["conversation"]: (r["sent_at"], r["id"], "next")
            for r in self.conn.execute(
                "SELECT DISTINCT ON ("
                + key
                + ") COALESCE(conversation_id, '') AS conversation, sent_at, id"
                " FROM archive.messages WHERE COALESCE(sent_at, '') <> ''"
                " AND COALESCE(conversation_id, '') <> ''"
                " AND conversation_id IN"
                " (SELECT conversation_id FROM archive.messages"
                " WHERE COALESCE(sent_at, '') = '') ORDER BY "
                + key
                + ', sent_at COLLATE "C", id COLLATE "C"'
            )
        }

    def senders(self):
        """Per conversation: unambiguous sender name -> ID and ID -> name.

        Reactions often name the reactor without an ID (Meta exports) or the
        reverse. Only conversations that have reactions are read, and only
        names or IDs that map one-to-one inside the conversation resolve.
        """
        if self.fixture is not None:
            reacted = {
                r.get("conversation_id")
                for r in self.fixture["messages"]
                if (r.get("reactions_json") or "").strip()
                not in ("", "[]", "null", '""')
            }
            rows = {
                (r.get("conversation_id"), r.get("sender_id"), r.get("sender_name"))
                for r in self.fixture["messages"]
                if r.get("conversation_id") in reacted
                and r.get("sender_id")
                and r.get("sender_name")
            }
        else:
            rows = {
                (r["conversation_id"], r["sender_id"], r["sender_name"])
                for r in self.conn.execute(
                    "SELECT conversation_id, sender_id, sender_name FROM archive.messages"
                    " WHERE COALESCE(sender_id, '') <> '' AND COALESCE(sender_name, '') <> ''"
                    " AND conversation_id IN (SELECT conversation_id FROM archive.messages"
                    " WHERE COALESCE(reactions_json, '') NOT IN ('', '[]', 'null', '\"\"'))"
                    " GROUP BY 1, 2, 3"
                )
            }
        names, ids = {}, {}
        for conversation, sender, name in rows:
            names.setdefault(conversation, {}).setdefault(name, set()).add(sender)
            ids.setdefault(conversation, {}).setdefault(sender, set()).add(name)
        return {
            c: {
                "by_name": {
                    n: next(iter(s)) for n, s in names.get(c, {}).items() if len(s) == 1
                },
                "by_id": {
                    i: next(iter(s)) for i, s in ids.get(c, {}).items() if len(s) == 1
                },
            }
            for c in set(names) | set(ids)
        }

    def message_aliases(self):
        """{archive message id: [alias external ids]} from message_aliases.

        Only unambiguous same-network aliases count: an alias key (network,
        native id) naming more than one message, or a message on another
        network, is left out. A native id that already has the live
        external-id form (beeper:...) is kept as it is; any other native id
        takes the archive's own form <network>:<native id>. The archive
        message id stays the canonical external id; the core resolves an
        alias to it. At most 10 aliases per message (the core's limit).
        """
        ids, aliases = self.reply_targets()
        result = {}
        for (net, native), message_id in sorted(aliases.items()):
            if not native or ids.get(message_id) != net:
                continue
            alias = native if native.startswith("beeper:") else f"{net}:{native}"
            if alias != message_id and alias not in result.get(message_id, []):
                result.setdefault(message_id, []).append(alias)
        return {k: v[:10] for k, v in result.items()}

    def reply_targets(self):
        """Only unambiguous same-network aliases; never use native_id as a key."""
        if self.fixture is not None:
            ids = {
                r["id"]: network(r.get("platform")) for r in self.fixture["messages"]
            }
        else:
            ids = {
                r["id"]: network(r["platform"])
                for r in self.conn.execute("SELECT id,platform FROM archive.messages")
            }
        aliases = {}
        for row in self.table("message_aliases"):
            key = (network(row["platform"]), row["native_id"])
            aliases.setdefault(key, set()).add(row["message_id"])
        return ids, {k: next(iter(v)) for k, v in aliases.items() if len(v) == 1}
