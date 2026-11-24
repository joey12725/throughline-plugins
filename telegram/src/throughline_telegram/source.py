"""A small synchronous facade over Telethon's asyncio TelegramClient.

The plugin only calls the methods below, so tests (and the offline
recording mode) pass any object with the same async methods:
`connect`, `is_user_authorized`, `get_me`, `iter_dialogs`, `iter_messages`,
`get_participants` and `disconnect`. Telethon itself is imported only when a
real session is used.
"""

import asyncio


class TelegramSource:
    def __init__(self, client, loop=None):
        self.client = client
        self.loop = loop or asyncio.new_event_loop()

    def _run(self, coro):
        return self.loop.run_until_complete(coro)

    def open(self):
        self._run(self.client.connect())
        if not self._run(self.client.is_user_authorized()):
            raise RuntimeError(
                "Telegram session is not authorized; run throughline-telegram-login "
                "and store the printed session string in TELEGRAM_SESSION"
            )
        return self

    def close(self):
        try:
            result = self.client.disconnect()
            if asyncio.iscoroutine(result):
                self._run(result)
        finally:
            self.loop.close()

    def me(self):
        return self._run(self.client.get_me())

    def dialogs(self):
        async def collect():
            return [d async for d in self.client.iter_dialogs()]

        return self._run(collect())

    def participants_count(self, entity):
        async def count():
            result = await self.client.get_participants(entity, limit=0)
            return getattr(result, "total", None)

        try:
            return self._run(count())
        except Exception:  # admin-only lists, forbidden chats
            return None

    def page(self, entity, min_id, limit):
        """Up to `limit` messages with id > min_id, oldest first."""

        async def collect():
            return [
                m
                async for m in self.client.iter_messages(
                    entity, limit=limit, min_id=min_id, reverse=True
                )
            ]

        return self._run(collect())

    def since(self, entity, offset_date, max_id, limit):
        """Messages sent at or after offset_date with id <= max_id, oldest first."""

        async def collect():
            found = []
            async for m in self.client.iter_messages(
                entity, offset_date=offset_date, reverse=True, limit=limit
            ):
                if m.id > max_id:
                    break
                found.append(m)
            return found

        return self._run(collect())


def telethon_client(api_id, api_hash, session):
    """A real TelegramClient on a StringSession (imports Telethon lazily)."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    client = TelegramClient(
        StringSession(session),
        int(api_id),
        api_hash,
        device_model="Throughline",
        app_version="throughline-telegram 0.1",
        receive_updates=False,
    )
    # Telethon sleeps through FLOOD_WAIT errors shorter than this.
    client.flood_sleep_threshold = 300
    return TelegramSource(client, loop)
