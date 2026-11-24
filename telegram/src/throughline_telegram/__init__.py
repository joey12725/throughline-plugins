"""Telegram private chats and groups through the MTProto user API (Telethon).

Logs in as you (a user session, not a bot), so it sees what your Telegram
apps see. Backfill walks every included dialog's full history oldest first
with a per-dialog high-water message id; later runs fetch only newer messages
and re-read a recent window so edits and reactions become new versions.
Broadcast channels and large groups are skipped unless enabled.
"""

import logging
from datetime import timedelta

from throughline_sdk import Channel, Checkpoint, Declaration, Manifest, Plugin, Record, run

from .mapping import CHANNEL, entity_kind, map_message, reply_link
from .source import TelegramSource, telethon_client

__all__ = ["Telegram", "main", "login"]
log = logging.getLogger("throughline_telegram")

FIELDS = [
    ("direction", "text", "incoming or outgoing relative to you"),
    ("author_kind", "text", "owner (you sent it) or other"),
    ("conversation_kind", "text", "dm, bot, group_dm (group or supergroup) or channel (broadcast)"),
    ("dialog_id", "text", "Telegram dialog (peer) id, marked form"),
    ("telegram_message_id", "integer", "Message id within the dialog"),
    ("sender_is_bot", "boolean", "The sender is a Telegram bot account"),
    ("sender_username", "text", "Sender's @username when public"),
    ("reply_to", "text", "External ID of the replied-to message in the same chat"),
    ("forwarded_from", "json", "{from_id, from_name, date, channel_post} of a forwarded message"),
    ("edited_at", "timestamp", "Last edit time"),
    ("media", "json", "{type, name, mime_type, size, duration, width, height} metadata only; files are not downloaded"),
    ("album_id", "text", "Grouped media (album) id shared by its messages"),
    ("service_action", "text", "MessageAction* type of a service message (joins, renames, calls)"),
    ("reaction_details", "json", "[{emoji, sender_id?, at?}] recent reactors, or [{emoji, count}] totals"),
]


def id_set(value):
    return {v.strip().lstrip("@").lower() for v in (value or "").split(",") if v.strip()}


class Telegram(Plugin):
    manifest = Manifest(
        name="telegram",
        version="0.1.0",
        channels=[
            Channel(
                name=CHANNEL,
                description="Telegram messages from your private chats and groups, one thread per dialog",
                fields=[dict(name=n, type=t, description=d) for n, t, d in FIELDS],
            )
        ],
        settings={
            "api_id": Declaration(
                env="TELEGRAM_API_ID",
                required=False,
                description="App api_id from https://my.telegram.org (API development tools)",
            ),
            "recording": Declaration(
                env="TELEGRAM_RECORDING",
                required=False,
                description="Invented JSON recording used instead of Telegram (offline tests only)",
            ),
            "include_channels": Declaration(
                env="TELEGRAM_INCLUDE_CHANNELS",
                default="0",
                description="1 also imports broadcast channels you follow",
            ),
            "include_bots": Declaration(
                env="TELEGRAM_INCLUDE_BOTS",
                default="0",
                description="1 also imports chats with bots",
            ),
            "max_group_size": Declaration(
                env="TELEGRAM_MAX_GROUP_SIZE",
                default="50",
                description="Skip groups with more members than this (0 = no limit)",
            ),
            "include": Declaration(
                env="TELEGRAM_INCLUDE",
                required=False,
                description="Comma-separated dialog ids or @usernames always imported, overriding the filters",
            ),
            "exclude": Declaration(
                env="TELEGRAM_EXCLUDE",
                required=False,
                description="Comma-separated dialog ids or @usernames never imported",
            ),
            "rescan_days": Declaration(
                env="TELEGRAM_RESCAN_DAYS",
                default="3",
                description="Re-read this many recent days of active dialogs each run to pick up edits and reactions",
            ),
            "batch_size": Declaration(
                env="TELEGRAM_BATCH_SIZE",
                default="500",
                description="Messages fetched per request and checkpoint",
            ),
        },
        secrets={
            "api_hash": Declaration(
                env="TELEGRAM_API_HASH",
                required=False,
                description="App api_hash from https://my.telegram.org",
            ),
            "session": Declaration(
                env="TELEGRAM_SESSION",
                required=False,
                description="Telethon StringSession printed by throughline-telegram-login",
            ),
        },
        default_interval_s=900,
        link_types=["reply_to"],
    )

    def source(self):
        if self.settings.get("recording"):
            from .recording import RecordedClient

            return TelegramSource(RecordedClient.load(self.settings["recording"]))
        missing = [
            env
            for env, value in (
                ("TELEGRAM_API_ID", self.settings.get("api_id")),
                ("TELEGRAM_API_HASH", self.secrets.get("api_hash")),
                ("TELEGRAM_SESSION", self.secrets.get("session")),
            )
            if not value
        ]
        if missing:
            raise ValueError("missing environment variable " + ", ".join(missing))
        return telethon_client(
            self.settings["api_id"], self.secrets["api_hash"], self.secrets["session"]
        )

    def wanted(self, source, dialog):
        entity = dialog.entity
        keys = {str(dialog.id), str(getattr(entity, "id", ""))}
        username = getattr(entity, "username", None)
        if username:
            keys.add(username.lower())
        if keys & id_set(self.settings.get("exclude")):
            return False
        if keys & id_set(self.settings.get("include")):
            return True
        if getattr(entity, "is_self", False):
            return True  # Saved Messages
        kind = entity_kind(entity)
        if kind == "dm":
            return True
        if kind == "bot":
            return self.settings.get("include_bots") == "1"
        if kind == "channel":
            return self.settings.get("include_channels") == "1"
        if kind != "group_dm":
            return False
        limit = int(self.settings.get("max_group_size") or 0)
        if limit <= 0:
            return True
        count = getattr(entity, "participants_count", None)
        if count is None:
            count = source.participants_count(entity)
        if count is None:
            # Unknown size: basic groups are small by construction.
            return type(entity).__name__ == "Chat"
        return count <= limit

    def records(self, cursor, mode):
        source = self.source().open()
        try:
            yield from self._records(source, dict(cursor or {}))
        finally:
            source.close()

    def _records(self, source, cursor):
        batch = max(1, min(int(self.settings["batch_size"] or 500), 3000))
        me = source.me()
        account = str(me.id)
        state = dict(cursor.get("dialogs") or {})
        rescan_days = float(self.settings.get("rescan_days") or 0)
        since = self.catch_up_since(timedelta(days=rescan_days)) if rescan_days > 0 else None
        dialogs = [d for d in source.dialogs() if self.wanted(source, d)]
        dialogs.sort(key=lambda d: str(d.id))
        for dialog in dialogs:
            key = str(dialog.id)
            high = int(state.get(key) or 0)
            emitted = set()

            def emit(messages):
                for message in messages:
                    if message.id in emitted:
                        continue
                    emitted.add(message.id)
                    row = map_message(message, dialog, account)
                    yield row
                    target = row["fields"].get("reply_to")
                    if target:
                        yield Record(kind="link", data=reply_link(row["external_id"], target))

            if high and since is not None and (dialog.date is None or dialog.date >= since):
                yield from emit(source.since(dialog.entity, since, high, 3000))
            while True:
                page = source.page(dialog.entity, high, batch)
                if not page:
                    break
                yield from emit(page)
                high = max(high, max(m.id for m in page))
                state[key] = high
                yield Checkpoint(cursor={"dialogs": dict(state)})
                if len(page) < batch:
                    break
        yield Checkpoint(cursor={"dialogs": dict(state)})


def main():
    run(Telegram)


def login():
    """Interactive login: prints a StringSession for TELEGRAM_SESSION."""
    import getpass
    import os

    from telethon.sessions import StringSession
    from telethon.sync import TelegramClient

    api_id = os.getenv("TELEGRAM_API_ID") or input("api_id (from my.telegram.org): ").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH") or getpass.getpass("api_hash: ").strip()
    with TelegramClient(StringSession(), int(api_id), api_hash) as client:
        session = client.session.save()
        name = getattr(client.get_me(), "first_name", "") or "your account"
    print(
        f"\nLogged in as {name}. Store this as the TELEGRAM_SESSION secret; it grants full "
        "access to your Telegram account, so keep it private and revoke it under "
        "Settings > Devices if it leaks:\n"
    )
    print(session)

