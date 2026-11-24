"""Pure mapping from Telethon objects (or look-alikes) to Throughline messages.

Only attributes are read, never Telethon classes, so the module works with the
real library and with the offline recording alike.
"""

from datetime import timezone

CHANNEL = "telegram.messages"

MEDIA_LABELS = {
    "MessageMediaPhoto": "photo",
    "MessageMediaDocument": "document",
    "MessageMediaGeo": "location",
    "MessageMediaGeoLive": "live location",
    "MessageMediaVenue": "venue",
    "MessageMediaContact": "contact",
    "MessageMediaPoll": "poll",
    "MessageMediaWebPage": "link preview",
    "MessageMediaDice": "dice",
    "MessageMediaGame": "game",
    "MessageMediaInvoice": "invoice",
    "MessageMediaStory": "story",
    "MessageMediaGiveaway": "giveaway",
}


def type_name(value):
    return type(value).__name__ if value is not None else None


def entity_kind(entity):
    """dm, bot, group_dm (basic group or supergroup) or channel (broadcast)."""
    name = type_name(entity)
    if name == "User":
        return "bot" if getattr(entity, "bot", False) else "dm"
    if name in ("Chat", "ChatForbidden"):
        return "group_dm"
    if name in ("Channel", "ChannelForbidden"):
        return "channel" if getattr(entity, "broadcast", False) else "group_dm"
    return "unknown"


def display_name(entity):
    if entity is None:
        return None
    if type_name(entity) == "User":
        parts = [getattr(entity, "first_name", None), getattr(entity, "last_name", None)]
        name = " ".join(p for p in parts if p)
        return name or getattr(entity, "username", None)
    return getattr(entity, "title", None)


def peer_ref(peer):
    """A Peer* object as 'user:ID' / 'chat:ID' / 'channel:ID'."""
    if peer is None:
        return None
    for attr, kind in (("user_id", "user"), ("channel_id", "channel"), ("chat_id", "chat")):
        value = getattr(peer, attr, None)
        if value is not None:
            return f"{kind}:{value}"
    return None


def iso(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def thread_id(account, dialog_id):
    return f"telegram:{account}:{dialog_id}"


def message_id(account, dialog_id, msg_id):
    return f"telegram:{account}:{dialog_id}:{msg_id}"


def reaction_text(reaction):
    return getattr(reaction, "emoticon", None) or (
        "custom:" + str(getattr(reaction, "document_id", ""))
        if getattr(reaction, "document_id", None)
        else type_name(reaction)
    )


def reaction_details(reactions):
    if reactions is None:
        return []
    details = []
    recent = getattr(reactions, "recent_reactions", None) or []
    for r in recent:
        item = {"emoji": reaction_text(r.reaction)}
        ref = peer_ref(getattr(r, "peer_id", None))
        if ref:
            item["sender_id"] = ref
        if getattr(r, "date", None):
            item["at"] = iso(r.date)
        details.append(item)
    if not details:
        for r in getattr(reactions, "results", None) or []:
            details.append({"emoji": reaction_text(r.reaction), "count": int(r.count)})
    return details


def media_meta(message):
    media = getattr(message, "media", None)
    if media is None:
        return None
    kind = type_name(media)
    meta = {"type": MEDIA_LABELS.get(kind, kind)}
    file = getattr(message, "file", None)
    if file is not None:
        for key in ("name", "mime_type", "size", "duration", "width", "height", "emoji"):
            value = getattr(file, key, None)
            if value not in (None, ""):
                meta[key] = value
        mime = meta.get("mime_type") or ""
        if mime.startswith("audio/ogg") or getattr(file, "voice", False):
            meta["type"] = "voice message"
        elif mime == "application/x-tgsticker" or meta.get("emoji"):
            meta["type"] = "sticker"
        elif mime.startswith("video/") and meta["type"] == "document":
            meta["type"] = "video"
    geo = getattr(message, "geo", None)
    if geo is not None and getattr(geo, "lat", None) is not None:
        meta["lat"], meta["lon"] = geo.lat, geo.long
    return meta


def media_label(meta):
    label = meta["type"]
    if meta.get("name"):
        label += ": " + meta["name"]
    elif meta.get("emoji"):
        label += " " + meta["emoji"]
    if meta.get("duration"):
        seconds = int(meta["duration"])
        label += f" {seconds // 60}:{seconds % 60:02d}"
    return f"[{label}]"


def action_label(action):
    name = type_name(action) or "action"
    if name.startswith("MessageAction"):
        name = name[len("MessageAction") :]
    title = getattr(action, "title", None)
    return f"[{name}: {title}]" if title else f"[{name}]"


def forward_meta(fwd):
    if fwd is None:
        return None
    meta = {
        "from_id": peer_ref(getattr(fwd, "from_id", None)),
        "from_name": getattr(fwd, "from_name", None),
        "date": iso(getattr(fwd, "date", None)),
        "channel_post": getattr(fwd, "channel_post", None),
    }
    return {k: v for k, v in meta.items() if v is not None}


def payload_of(message):
    """A JSON-safe snapshot of the attributes this plugin reads."""
    reply = getattr(message, "reply_to", None)
    return {
        "id": message.id,
        "date": iso(message.date),
        "message": getattr(message, "message", None) or "",
        "out": bool(getattr(message, "out", False)),
        "sender_id": getattr(message, "sender_id", None),
        "post": bool(getattr(message, "post", False)),
        "reply_to_msg_id": getattr(reply, "reply_to_msg_id", None) if reply else None,
        "reply_to_peer": peer_ref(getattr(reply, "reply_to_peer_id", None)) if reply else None,
        "reply_to_top_id": getattr(reply, "reply_to_top_id", None) if reply else None,
        "fwd_from": forward_meta(getattr(message, "fwd_from", None)),
        "media": media_meta(message),
        "edit_date": iso(getattr(message, "edit_date", None)),
        "grouped_id": getattr(message, "grouped_id", None),
        "action": type_name(getattr(message, "action", None)),
        "action_title": getattr(getattr(message, "action", None), "title", None),
        "reactions": reaction_details(getattr(message, "reactions", None)),
        "type": type_name(message),
    }


def map_message(message, dialog, account):
    entity = dialog.entity
    kind = entity_kind(entity)
    sender = getattr(message, "sender", None)
    sender_id = getattr(message, "sender_id", None)
    out = bool(getattr(message, "out", False))
    text = (getattr(message, "message", None) or "").strip()
    media = media_meta(message)
    action = getattr(message, "action", None)
    if action is not None:
        content = action_label(action) + ((" " + text) if text else "")
    elif text and media and media["type"] != "link preview":
        content = text + "\n" + media_label(media)
    else:
        content = text or (media_label(media) if media else "[empty message]")
    reply = getattr(message, "reply_to", None)
    reply_to = None
    if reply is not None and getattr(reply, "reply_to_msg_id", None):
        reply_peer = getattr(reply, "reply_to_peer_id", None)
        # Replies to another chat keep the id in the payload but cannot link.
        if reply_peer is None:
            reply_to = message_id(account, dialog.id, reply.reply_to_msg_id)
    details = reaction_details(getattr(message, "reactions", None))
    if details:
        content += "\n\nReactions: " + ", ".join(
            d["emoji"] + (f" x{d['count']}" if d.get("count") else "") for d in details
        )
    author_kind = "owner" if out else "other"
    is_bot = sender is not None and type_name(sender) == "User" and bool(
        getattr(sender, "bot", False)
    )
    author = None
    if sender_id is not None:
        author = {
            "system": "telegram",
            "kind": "user_id" if (sender is None or type_name(sender) == "User") else "chat_id",
            "value": str(sender_id),
        }
        name = display_name(sender)
        if name:
            author["display_name"] = name
    fields = {
        "direction": "outgoing" if out else "incoming",
        "author_kind": author_kind,
        "conversation_kind": kind,
        "dialog_id": str(dialog.id),
        "telegram_message_id": int(message.id),
        "sender_is_bot": True if is_bot else None,
        "sender_username": getattr(sender, "username", None) if sender is not None else None,
        "reply_to": reply_to,
        "forwarded_from": forward_meta(getattr(message, "fwd_from", None)) or None,
        "edited_at": iso(getattr(message, "edit_date", None)),
        "media": media,
        "album_id": str(message.grouped_id) if getattr(message, "grouped_id", None) else None,
        "service_action": type_name(action),
        "reaction_details": details or None,
    }
    title = display_name(entity) or getattr(dialog, "name", None)
    return dict(
        channel=CHANNEL,
        external_id=message_id(account, dialog.id, message.id),
        thread=dict(
            external_id=thread_id(account, dialog.id),
            title=title,
            metadata={
                "kind": kind,
                "title": title,
                "username": getattr(entity, "username", None),
            },
        ),
        happened_at=iso(message.date),
        content=content,
        fields={k: v for k, v in fields.items() if v not in (None, "", [])},
        author=author,
        payload=payload_of(message),
    )


def reply_link(source, target):
    return {
        "type": "reply_to",
        "from": {"channel": CHANNEL, "external_id": source},
        "to": {"channel": CHANNEL, "external_id": target},
        "method": "source",
    }
