"""Map spooled signal-cli envelopes to Throughline messages.

A Signal message is identified by its author and its sent timestamp
(milliseconds), which is also how reactions, edits, deletes and quotes refer
to it. Reactions, edits and remote deletes are folded into the message they
target; the whole spool is indexed first, so a message always maps to the
same final state no matter where a run starts.
"""

from datetime import datetime, timezone

CHANNEL = "signal.messages"


def iso_ms(ms):
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()


def person_id(uuid=None, number=None, legacy=None):
    return uuid or number or legacy or None


def message_key(account, author, sent_ms):
    return f"signal:{account}:{author}:{int(sent_ms)}"


class Event:
    """One spooled envelope, classified."""

    __slots__ = ("kind", "key", "target", "data", "item", "envelope", "account", "sync", "at")

    def __init__(self, kind, key, target, data, item, sync=False):
        self.kind = kind  # message | reaction | edit | delete
        self.key = key
        self.target = target
        self.data = data
        self.item = item
        self.envelope = item.get("envelope") or {}
        self.account = item.get("account")
        self.sync = sync
        self.at = int(self.envelope.get("timestamp") or 0)


def classify(item, default_account=None):
    """The Event of a spool line, or None for receipts, typing and the like."""
    envelope = item.get("envelope") or {}
    account = item.get("account") or default_account or "unknown"
    item = dict(item, account=account)
    source = person_id(
        envelope.get("sourceUuid"), envelope.get("sourceNumber"), envelope.get("source")
    )
    if not source:
        return None
    sync = False
    data = envelope.get("dataMessage")
    edit = envelope.get("editMessage")
    sent = (envelope.get("syncMessage") or {}).get("sentMessage")
    if sent:
        sync = True
        if sent.get("editMessage"):
            edit, data = sent["editMessage"], None
        else:
            data = sent
    if edit:
        inner = edit.get("dataMessage") or {}
        target = message_key(account, source, edit.get("targetSentTimestamp") or 0)
        return Event("edit", None, target, inner, item, sync)
    if not data or not data.get("timestamp"):
        return None
    reaction = data.get("reaction")
    if reaction:
        author = person_id(
            reaction.get("targetAuthorUuid"),
            reaction.get("targetAuthorNumber"),
            reaction.get("targetAuthor"),
        )
        if not author or not reaction.get("targetSentTimestamp"):
            return None
        target = message_key(account, author, reaction["targetSentTimestamp"])
        return Event("reaction", None, target, reaction, item, sync)
    delete = data.get("remoteDelete")
    if delete:
        target = message_key(account, source, delete.get("timestamp") or 0)
        return Event("delete", None, target, delete, item, sync)
    if not any(
        data.get(k) for k in ("message", "attachments", "sticker", "contacts", "payment")
    ):
        return None  # profile key updates, group updates without text, etc.
    return Event("message", message_key(account, source, data["timestamp"]), None, data, item, sync)


class Index:
    """Every base message and the modifications targeting it."""

    def __init__(self):
        self.base = {}
        self.mods = {}

    def add(self, event):
        if event.kind == "message":
            self.base.setdefault(event.key, event)
        else:
            self.mods.setdefault(event.target, []).append(event)


def author_for(uuid, number, name):
    if number:
        author = {"system": "phone", "kind": "number", "value": number}
    elif uuid:
        author = {"system": "signal", "kind": "uuid", "value": uuid}
    else:
        return None
    if name:
        author["display_name"] = name
    return author


def attachments(data):
    out = []
    for a in data.get("attachments") or []:
        out.append(
            {
                k: a.get(k)
                for k in ("contentType", "filename", "size", "width", "height", "caption", "id", "voiceNote")
                if a.get(k) not in (None, "", False)
            }
        )
    return out


def reaction_state(events):
    """Latest reaction per reactor wins; a removal clears it."""
    state = {}
    for event in sorted(events, key=lambda e: e.at):
        env = event.envelope
        reactor = person_id(env.get("sourceUuid"), env.get("sourceNumber"), env.get("source"))
        if event.data.get("isRemove"):
            state.pop(reactor, None)
            continue
        entry = {"emoji": event.data.get("emoji") or "?", "sender_id": reactor, "at": iso_ms(event.at)}
        if event.sync:
            entry["sender_kind"] = "owner"
        elif env.get("sourceName"):
            entry["sender_name"] = env["sourceName"]
        state.pop(reactor, None)
        state[reactor] = entry
    return list(state.values())


def thread_of(event, account, groups):
    data = event.data
    group = data.get("groupInfo") or {}
    gid = group.get("groupId")
    env = event.envelope
    if gid:
        title = groups.get(gid) or group.get("groupName") or "Signal group"
        return (
            {
                "external_id": f"signal:{account}:group:{gid}",
                "title": title,
                "metadata": {"kind": "group_dm", "group_id": gid, "title": title},
            },
            "group_dm",
        )
    if event.sync:
        other = person_id(
            data.get("destinationUuid"), data.get("destinationNumber"), data.get("destination")
        )
        title = data.get("destinationNumber") or data.get("destination") or other
    else:
        other = person_id(env.get("sourceUuid"), env.get("sourceNumber"), env.get("source"))
        title = env.get("sourceName") or env.get("sourceNumber") or other
    other = other or "unknown"
    return (
        {
            "external_id": f"signal:{account}:dm:{other}",
            "title": title,
            "metadata": {"kind": "dm", "peer": other},
        },
        "dm",
    )


def build(key, index, groups=None, owner_number=None):
    """The current state of message `key`: base + edits + reactions + delete."""
    base = index.base[key]
    account = base.account
    env = base.envelope
    mods = index.mods.get(key, [])
    # Only the author may edit or delete; reactions may come from anyone.
    author = person_id(env.get("sourceUuid"), env.get("sourceNumber"), env.get("source"))

    def by_author(e):
        envelope = e.envelope
        return person_id(
            envelope.get("sourceUuid"), envelope.get("sourceNumber"), envelope.get("source")
        ) == author

    edits = sorted((e for e in mods if e.kind == "edit" and by_author(e)), key=lambda e: e.at)
    deletes = sorted((e for e in mods if e.kind == "delete" and by_author(e)), key=lambda e: e.at)
    reactions = reaction_state([e for e in mods if e.kind == "reaction"])
    current = edits[-1].data if edits else base.data
    thread, kind = thread_of(base, account, groups or {})
    files = attachments(current)
    text = (current.get("message") or "").strip()
    sticker = current.get("sticker")
    if text:
        content = text
    elif files:
        content = " ".join(
            "[attachment: " + (f.get("filename") or f.get("contentType") or "file") + "]" for f in files
        )
    elif sticker:
        content = "[sticker" + (" " + sticker["emoji"] if sticker.get("emoji") else "") + "]"
    else:
        content = "[empty message]"
    if reactions:
        content += "\n\nReactions: " + ", ".join(
            r["emoji"] + " " + ("me" if r.get("sender_kind") == "owner" else r.get("sender_name") or r["sender_id"])
            for r in reactions
        )
    quote = base.data.get("quote") or {}
    reply_to = None
    if quote.get("id"):
        quoted = person_id(quote.get("authorUuid"), quote.get("authorNumber"), quote.get("author"))
        if quoted:
            reply_to = message_key(account, quoted, quote["id"])
    own = base.sync
    if own:
        number = owner_number or env.get("sourceNumber") or (
            account if str(account).startswith("+") else None
        )
        author_obj = author_for(env.get("sourceUuid"), number, None)
    else:
        author_obj = author_for(env.get("sourceUuid"), env.get("sourceNumber"), env.get("sourceName"))
    fields = {
        "direction": "outgoing" if own else "incoming",
        "author_kind": "owner" if own else "other",
        "conversation_kind": kind,
        "group_id": (base.data.get("groupInfo") or {}).get("groupId"),
        "sender_uuid": env.get("sourceUuid"),
        "sender_device": env.get("sourceDevice"),
        "reply_to": reply_to,
        "quote_text": quote.get("text") or None,
        "edited_at": iso_ms(edits[-1].data.get("timestamp") or edits[-1].at) if edits else None,
        "edit_count": len(edits) or None,
        "deleted_at": iso_ms(deletes[0].at) if deletes else None,
        "attachments": files,
        "sticker": sticker,
        "reaction_details": reactions,
        "expires_in_s": current.get("expiresInSeconds") or None,
        "view_once": True if current.get("viewOnce") else None,
        "mentions": current.get("mentions") or None,
    }
    payload = {
        "envelope": env,
        "account": account,
        "edits": [e.envelope for e in edits],
        "reactions": [e.envelope for e in sorted(mods, key=lambda e: e.at) if e.kind == "reaction"],
        "deletes": [e.envelope for e in deletes],
    }
    return dict(
        channel=CHANNEL,
        external_id=key,
        thread=thread,
        happened_at=iso_ms(base.data["timestamp"]),
        content=content,
        fields={k: v for k, v in fields.items() if v not in (None, "", [])},
        author=author_obj,
        payload=payload,
    )


def reply_link(source, target):
    return {
        "type": "reply_to",
        "from": {"channel": CHANNEL, "external_id": source},
        "to": {"channel": CHANNEL, "external_id": target},
        "method": "source",
    }
