"""iMessage, SMS and RCS from the macOS Messages database as a Throughline source.

Runs on the Mac that holds ~/Library/Messages/chat.db (the terminal or
service running it needs Full Disk Access). The database is opened read-only.
Cursor: the highest message ROWID delivered and the newest edit/unsend time
seen, so later edits, unsends and tapbacks on older messages produce new
versions of those messages.
"""

import sqlite3

from throughline_sdk import Channel, Checkpoint, Declaration, Manifest, Plugin, Record, run

from .chatdb import ChatDB, TAPBACK_MAX, TAPBACK_MIN, tapback_target
from .mapping import CHANNEL, map_message, message_id, reply_link

__all__ = ["IMessage", "main"]

FIELDS = [
    ("direction", "text", "incoming or outgoing relative to the owner"),
    ("is_from_me", "boolean", "The owner sent this message (message.is_from_me)"),
    ("author_kind", "text", "owner (sent by you) or other (sent by someone else)"),
    ("service", "text", "iMessage, SMS or RCS"),
    ("conversation_kind", "text", "dm or group_dm"),
    ("chat_guid", "text", "Messages chat GUID"),
    ("participants", "json", "Handles (phone numbers / emails) in the chat, excluding you"),
    ("sender_handle", "text", "Handle of the sender for incoming messages"),
    ("reply_to", "text", "External ID of the message this is an inline reply to"),
    ("edited_at", "timestamp", "When the message was last edited (macOS 13+)"),
    ("unsent_at", "timestamp", "When the sender unsent the message (macOS 13+)"),
    (
        "attachments",
        "json",
        "[{guid, transfer_name, filename, mime_type, uti, total_bytes, is_sticker?}] metadata only",
    ),
    (
        "reaction_details",
        "json",
        "Current tapbacks: [{emoji, kind, sender_id? | sender_kind: owner, at}]",
    ),
    ("event_kind", "text", "membership, rename, group_update or system for non-text events"),
    ("app_bundle", "text", "iMessage app balloon bundle id (stickers, Apple Pay, games)"),
    ("send_effect", "text", "Expressive send style (bubble/screen effect) id"),
]

DEFAULT_DB = "~/Library/Messages/chat.db"


class IMessage(Plugin):
    manifest = Manifest(
        name="imessage",
        version="0.1.0",
        channels=[
            Channel(
                name=CHANNEL,
                description="iMessage, SMS and RCS messages from the macOS Messages app, one thread per chat",
                fields=[dict(name=n, type=t, description=d) for n, t, d in FIELDS],
            )
        ],
        settings={
            "db": Declaration(
                env="IMESSAGE_DB",
                default=DEFAULT_DB,
                description="Path to chat.db (read-only)",
            ),
            "owner_handle": Declaration(
                env="IMESSAGE_OWNER_HANDLE",
                required=False,
                description="Your phone number or Apple ID email, used as the author of your own messages when chat.db does not record it",
            ),
            "batch_size": Declaration(
                env="IMESSAGE_BATCH_SIZE",
                default="1000",
                description="Messages read per checkpointed batch",
            ),
        },
        default_interval_s=300,
        link_types=["reply_to"],
    )

    def open(self):
        try:
            return ChatDB(self.settings["db"])
        except sqlite3.OperationalError as error:
            raise RuntimeError(
                f"cannot open {self.settings['db']} read-only ({error}); on macOS grant "
                "Full Disk Access to the app running this plugin"
            ) from None

    def records(self, cursor, mode):
        db = self.open()
        try:
            yield from self._records(db, dict(cursor or {}))
        finally:
            db.close()

    def _records(self, db, cursor):
        batch = max(1, int(self.settings["batch_size"] or 1000))
        owner = self.settings.get("owner_handle") or None
        start = int(cursor.get("rowid") or 0)
        changed_hw = cursor.get("changed_hw")
        snapshot_hw = db.change_high_water()
        upto = db.max_rowid()
        chats = db.chats()
        tapbacks = db.tapbacks()
        emitted = set()

        def emit(rows):
            files = db.attachments([r["rowid"] for r in rows])
            for row in rows:
                kind = row.get("associated_message_type") or 0
                if TAPBACK_MIN <= kind <= TAPBACK_MAX:
                    continue
                key = message_id(row["guid"])
                if key in emitted:
                    continue
                emitted.add(key)
                message = map_message(
                    row,
                    chats.get(row.get("chat_id")),
                    files.get(row["rowid"], []),
                    tapbacks.get(row["guid"], []),
                    owner,
                )
                yield message
                reply = row.get("thread_originator_guid")
                if reply:
                    yield Record(kind="link", data=reply_link(key, message_id(reply)))

        # Edits and unsends of messages delivered in earlier runs.
        if changed_hw is not None and start:
            yield from emit(db.changed_since(int(changed_hw), start))
        position = start
        while position < upto:
            rows = db.messages_after(position, batch, upto)
            if not rows:
                break
            plain, retargets = [], []
            for row in rows:
                kind = row.get("associated_message_type") or 0
                if TAPBACK_MIN <= kind <= TAPBACK_MAX:
                    # A tapback is a field of its target, never its own message.
                    target = tapback_target(row.get("associated_message_guid"))
                    if target:
                        retargets.append(target)
                else:
                    plain.append(row)
            yield from emit(plain)
            # Targets delivered before this run get a new version with the
            # tapback; targets inside this run already carry it.
            old = []
            for guid in dict.fromkeys(retargets):
                if message_id(guid) in emitted:
                    continue
                target = db.message_by_guid(guid)
                if target and target["rowid"] <= start:
                    old.append(target)
            yield from emit(old)
            position = rows[-1]["rowid"]
            yield Checkpoint(
                cursor={
                    "rowid": position,
                    "changed_hw": changed_hw if changed_hw is not None else snapshot_hw,
                }
            )
        yield Checkpoint(cursor={"rowid": max(position, start), "changed_hw": snapshot_hw})


def main():
    run(IMessage)
