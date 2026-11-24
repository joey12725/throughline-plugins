"""Pure mapping from chat.db rows to Throughline messages."""

from .chatdb import apple_time, decode_attributed_body

CHANNEL = "imessage.messages"
OBJECT_REPLACEMENT = "￼"

TAPBACKS = {
    0: ("love", "❤️"),
    1: ("like", "\U0001f44d"),
    2: ("dislike", "\U0001f44e"),
    3: ("laugh", "\U0001f602"),
    4: ("emphasize", "‼️"),
    5: ("question", "❓"),
    6: ("emoji", None),
    7: ("sticker", None),
}
# chat.style: 43 group chat, 45 one-to-one.
GROUP_STYLE = 43


def iso(dt):
    return dt.isoformat() if dt else None


def message_id(guid):
    return "imessage:" + guid


def chat_thread_id(chat):
    return "imessage:chat:" + chat["guid"]


def author_for(handle, display_name=None):
    """phone or email identity for a Messages handle id."""
    if not handle:
        return None
    value = handle.strip()
    if value.lower().startswith(("e:", "p:")) and len(value) > 2:
        value = value[2:]
    if "@" in value:
        system, kind = "email", "email"
    elif value.lstrip("+").replace(" ", "").replace("-", "").isdigit():
        system, kind = "phone", "number"
    else:
        system, kind = "imessage", "handle"
    author = {"system": system, "kind": kind, "value": value}
    if display_name:
        author["display_name"] = display_name
    return author


def owner_handle(row, owner):
    return owner or row.get("destination_caller_id") or row.get("account") or None


def message_text(row):
    text = row.get("text")
    if text is None and row.get("attributedBody") is not None:
        text = decode_attributed_body(row["attributedBody"])
    if text is None:
        return None
    text = text.replace(OBJECT_REPLACEMENT, "").strip()
    return text or None


def reaction_details(rows):
    """Current tapbacks: the latest add/remove per (reactor, kind) wins."""
    state = {}
    for row in rows:
        kind = int(row["associated_message_type"] or 0)
        removing = kind >= 3000
        base = (kind - 3000) if removing else (kind - 2000)
        name, emoji = TAPBACKS.get(base, ("tapback", None))
        reactor = "owner" if row.get("is_from_me") else (row.get("handle") or "unknown")
        key = (reactor, name if name != "emoji" else "emoji")
        if removing:
            state.pop(key, None)
            continue
        entry = {"emoji": row.get("associated_message_emoji") or emoji or name}
        entry["kind"] = name
        if row.get("is_from_me"):
            entry["sender_kind"] = "owner"
        elif row.get("handle"):
            entry["sender_id"] = row["handle"]
        at = apple_time(row.get("date"))
        if at:
            entry["at"] = iso(at)
        state.pop(key, None)
        state[key] = entry
    return list(state.values())


def reaction_line(details):
    if not details:
        return None
    parts = []
    for d in details:
        who = "me" if d.get("sender_kind") == "owner" else d.get("sender_id", "someone")
        parts.append(f"{d['emoji']} {who}")
    return "Reactions: " + ", ".join(parts)


def attachment_meta(rows):
    out = []
    for a in rows or []:
        item = {
            k: a.get(k)
            for k in ("guid", "transfer_name", "filename", "mime_type", "uti", "total_bytes")
            if a.get(k) is not None
        }
        if a.get("is_sticker"):
            item["is_sticker"] = True
        out.append(item)
    return out


def event_text(row):
    item_type = row.get("item_type") or 0
    if item_type == 1:
        action = "added someone to" if (row.get("group_action_type") or 0) == 0 else "removed someone from"
        return f"[{action} the conversation]"
    if item_type == 2:
        return f"[renamed the conversation to {row.get('group_title') or 'nothing'}]"
    if item_type == 3:
        return "[left the conversation]" if (row.get("group_action_type") or 0) == 0 else "[changed the group photo]"
    if item_type in (4, 5, 6):
        return "[shared location or other system event]"
    return None


def event_kind(row):
    return {0: None, 1: "membership", 2: "rename", 3: "group_update"}.get(
        row.get("item_type") or 0, "system"
    )


def map_message(row, chat, attachments, tapbacks, owner=None):
    """One chat.db message row as a Throughline message dict."""
    at = apple_time(row["date"])
    if at is None:
        raise ValueError("message without date")
    from_me = bool(row.get("is_from_me"))
    edited = apple_time(row.get("date_edited"))
    retracted = apple_time(row.get("date_retracted"))
    text = message_text(row)
    files = attachment_meta(attachments)
    details = reaction_details(tapbacks)
    is_group = bool(chat) and (
        chat.get("style") == GROUP_STYLE or len(chat.get("participants") or []) > 1
    )
    if text:
        content = text
    elif retracted:
        content = "[unsent message]"
    elif files:
        content = " ".join(
            "[attachment: " + (f.get("transfer_name") or f.get("mime_type") or "file") + "]"
            for f in files
        )
    else:
        content = event_text(row) or "[empty message]"
    line = reaction_line(details)
    if line:
        content += "\n\n" + line
    if chat:
        thread_external = chat_thread_id(chat)
        title = chat.get("display_name") or (
            ", ".join(chat.get("participants") or []) or chat.get("chat_identifier")
        )
        thread = dict(
            external_id=thread_external,
            title=title,
            metadata={
                "kind": "group_dm" if is_group else "dm",
                "service": chat.get("service_name"),
                "chat_identifier": chat.get("chat_identifier"),
                "participants": chat.get("participants") or [],
            },
        )
    else:
        handle = row.get("handle") or "unknown"
        thread = dict(external_id="imessage:handle:" + handle, title=handle)
    if from_me:
        author = author_for(owner_handle(row, owner))
    else:
        author = author_for(row.get("handle"))
    reply = row.get("thread_originator_guid")
    fields = {
        "direction": "outgoing" if from_me else "incoming",
        "is_from_me": from_me,
        "author_kind": "owner" if from_me else "other",
        "service": row.get("service"),
        "conversation_kind": "group_dm" if is_group else "dm",
        "chat_guid": chat.get("guid") if chat else None,
        "participants": (chat or {}).get("participants") or [],
        "sender_handle": None if from_me else row.get("handle"),
        "reply_to": message_id(reply) if reply else None,
        "edited_at": iso(edited),
        "unsent_at": iso(retracted),
        "attachments": files,
        "reaction_details": details,
        "event_kind": event_kind(row),
        "app_bundle": row.get("balloon_bundle_id"),
        "send_effect": row.get("expressive_send_style_id"),
    }
    fields = {k: v for k, v in fields.items() if v not in (None, [], "")}
    fields["is_from_me"] = from_me
    payload = {
        k: v
        for k, v in row.items()
        if k not in ("attributedBody",) and not isinstance(v, (bytes, bytearray))
    }
    payload["decoded_text"] = text
    payload["attachments"] = attachments or []
    payload["tapbacks"] = [
        {k: v for k, v in t.items() if not isinstance(v, (bytes, bytearray))}
        for t in tapbacks
    ]
    return dict(
        channel=CHANNEL,
        external_id=message_id(row["guid"]),
        thread=thread,
        happened_at=iso(at),
        content=content,
        fields=fields,
        author=author,
        payload=payload,
    )


def reply_link(source, target):
    return {
        "type": "reply_to",
        "from": {"channel": CHANNEL, "external_id": source},
        "to": {"channel": CHANNEL, "external_id": target},
        "method": "source",
    }
