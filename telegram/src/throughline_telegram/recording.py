"""Offline stand-in for Telethon built from an invented JSON recording.

The classes carry the same names and attributes as the Telethon types the
plugin reads (User, Chat, Channel, Message, MessageReplyHeader, ...), so the
mapping code cannot tell them apart. Used by tests and TELEGRAM_RECORDING.
"""

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace


def _obj(_type_name, **values):
    cls = type(_type_name, (SimpleNamespace,), {})
    return cls(**values)


def User(**v):
    v.setdefault("bot", False)
    v.setdefault("is_self", False)
    for key in ("first_name", "last_name", "username", "phone"):
        v.setdefault(key, None)
    return _obj("User", **v)


def Chat(**v):
    v.setdefault("participants_count", None)
    return _obj("Chat", **v)


def Channel(**v):
    v.setdefault("broadcast", False)
    v.setdefault("megagroup", False)
    v.setdefault("participants_count", None)
    v.setdefault("username", None)
    return _obj("Channel", **v)


def PeerUser(user_id):
    return _obj("PeerUser", user_id=user_id)


def PeerChannel(channel_id):
    return _obj("PeerChannel", channel_id=channel_id)


def PeerChat(chat_id):
    return _obj("PeerChat", chat_id=chat_id)


PEERS = {"user": PeerUser, "channel": PeerChannel, "chat": PeerChat}
ENTITIES = {"User": User, "Chat": Chat, "Channel": Channel}


def peer(value):
    if not value:
        return None
    kind, ident = value.split(":")
    return PEERS[kind](int(ident))


def when(value):
    return datetime.fromisoformat(value) if value else None


def message(raw, senders, chat_id):
    reply = raw.get("reply_to")
    fwd = raw.get("fwd_from")
    media = raw.get("media")
    reactions = raw.get("reactions")
    return _obj(
        "MessageService" if raw.get("action") else "Message",
        id=raw["id"],
        chat_id=chat_id,
        date=when(raw["date"]),
        message=raw.get("text", ""),
        out=raw.get("out", False),
        sender_id=raw.get("sender_id"),
        sender=senders.get(raw.get("sender_id")),
        post=raw.get("post", False),
        reply_to=_obj(
            "MessageReplyHeader",
            reply_to_msg_id=reply.get("msg_id"),
            reply_to_peer_id=peer(reply.get("peer")),
            reply_to_top_id=reply.get("top_id"),
        )
        if reply
        else None,
        fwd_from=_obj(
            "MessageFwdHeader",
            from_id=peer(fwd.get("from")),
            from_name=fwd.get("from_name"),
            date=when(fwd.get("date")),
            channel_post=fwd.get("channel_post"),
        )
        if fwd
        else None,
        media=_obj(media["type"]) if media else None,
        file=_obj("File", **{k: v for k, v in media.items() if k != "type"})
        if media and media.get("type") != "MessageMediaGeo"
        else None,
        geo=_obj("GeoPoint", lat=media["lat"], long=media["long"])
        if media and media.get("type") == "MessageMediaGeo"
        else None,
        edit_date=when(raw.get("edit_date")),
        grouped_id=raw.get("grouped_id"),
        action=_obj(raw["action"], **raw.get("action_args", {}))
        if raw.get("action")
        else None,
        reactions=_obj(
            "MessageReactions",
            results=[
                _obj(
                    "ReactionCount",
                    reaction=_obj("ReactionEmoji", emoticon=r["emoji"]),
                    count=r["count"],
                )
                for r in reactions.get("results", [])
            ],
            recent_reactions=[
                _obj(
                    "MessagePeerReaction",
                    peer_id=peer(r["peer"]),
                    reaction=_obj("ReactionEmoji", emoticon=r["emoji"]),
                    date=when(r.get("date")),
                )
                for r in reactions.get("recent", [])
            ],
        )
        if reactions
        else None,
    )


class _Total(list):
    total = None


class RecordedClient:
    """Async API subset of telethon.TelegramClient over a JSON recording."""

    def __init__(self, data):
        self.data = data
        self.users = {u["id"]: User(**u) for u in data.get("users", [])}
        self.me = self.users[data["me"]]
        self.me.is_self = True

    @classmethod
    def load(cls, path):
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    async def connect(self):
        return None

    async def is_user_authorized(self):
        return True

    def disconnect(self):
        return None

    async def get_me(self):
        return self.me

    def _dialog(self, raw):
        spec = dict(raw["entity"])
        kind = spec.pop("type")
        if kind == "User":
            entity = self.users[spec["id"]]
        else:
            entity = ENTITIES[kind](**spec)
        senders = dict(self.users)
        if kind == "Channel":
            senders[None] = entity
        return SimpleNamespace(
            id=raw["id"],
            name=raw.get("name"),
            entity=entity,
            date=when(raw["messages"][-1]["date"]) if raw.get("messages") else None,
            _messages=[message(m, senders, raw["id"]) for m in raw.get("messages", [])],
            _participants=raw.get("participants"),
        )

    async def iter_dialogs(self):
        for raw in self.data.get("dialogs", []):
            yield self._dialog(raw)

    async def get_participants(self, entity, limit=None):
        for raw in self.data.get("dialogs", []):
            if raw["entity"].get("id") == getattr(entity, "id", None):
                result = _Total()
                result.total = raw.get("participants")
                return result
        raise ValueError("unknown entity")

    async def iter_messages(
        self, entity, limit=None, min_id=0, reverse=False, offset_date=None
    ):
        dialog = next(
            self._dialog(raw)
            for raw in self.data.get("dialogs", [])
            if raw["entity"].get("id") == getattr(entity, "id", None)
        )
        rows = sorted(dialog._messages, key=lambda m: m.id, reverse=not reverse)
        count = 0
        for m in rows:
            if m.id <= (min_id or 0):
                continue
            if offset_date is not None and reverse and m.date < offset_date:
                continue
            yield m
            count += 1
            if limit is not None and count >= limit:
                return
