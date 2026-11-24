"""Pure, total reaction normalization: never raises on odd source shapes.

Sources disagree on the reaction layout:

- Discord exports: ``{"emoji": {"name", "id"}, "count", "users": [{"id", "name"}]}``
- Meta (Instagram) exports: ``{"reaction", "actor", "timestamp"}``, or a bare
  emoji string that ``mapping.reactions`` already turned into ``{"emoji"}``
- Beeper Desktop API: ``{"id", "participantID", "reactionKey", "emoji": bool}``
- other bridges: ``{"emoji", "sender_id" | "senderID", "sender_name", ...}``

Every shape becomes a list of ``{emoji, sender_id?, sender_kind?,
sender_name?, at?, count?}`` in source order. Keys are present only when the
source supplied (or the conversation resolved) a value, so the stored field is
deterministic and compact.
"""

from datetime import datetime, timezone

ID_KEYS = (
    "participantID",
    "senderID",
    "sender_id",
    "userID",
    "user_id",
    "actorID",
    "actor_id",
    "authorID",
    "author_id",
    "reactorID",
    "reactor_id",
    "from_id",
)
NAME_KEYS = (
    "senderName",
    "sender_name",
    "actor",
    "actor_name",
    "reactor",
    "reactor_name",
    "author",
    "user",
    "from",
)
TIME_KEYS = (
    "timestamp",
    "timestamp_ms",
    "time",
    "created_at",
    "createdAt",
    "reacted_at",
    "reactedAt",
    "at",
    "date",
)
EMOJI_KEYS = ("reaction", "reactionKey", "key", "emoji_name", "value", "text")
LINE_ENTRIES = 50
LINE_BYTES = 4000


def text(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        value = " ".join(value.replace("\x00", "").split())
        return value or None
    return None


def emoji_of(reaction):
    emoji = reaction.get("emoji")
    if isinstance(emoji, dict):
        name = text(emoji.get("name"))
        if name and text(emoji.get("id")) and name.isascii():
            return ":" + name.strip(":") + ":"  # custom server emoji
        return name or text(emoji.get("code")) or text(emoji.get("id"))
    if isinstance(emoji, str) and text(emoji):
        return text(emoji)
    for key in EMOJI_KEYS:
        if text(reaction.get(key)):
            return text(reaction.get(key))
    return None


def person(value):
    """(id, name) from a nested user/actor value."""
    if isinstance(value, dict):
        return (
            text(value.get("id") or value.get("userID") or value.get("user_id")),
            text(
                value.get("nickname")
                or value.get("displayName")
                or value.get("display_name")
                or value.get("fullName")
                or value.get("name")
                or value.get("username")
            ),
        )
    return None, text(value)


def when(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            seconds = value / 1000 if value > 1e11 else value
            return datetime.fromtimestamp(seconds, timezone.utc).isoformat()
        if isinstance(value, str) and value.strip():
            raw = value.strip()
            if raw.lstrip("-").isdigit():
                return when(int(raw))
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.utcoffset() is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat()
    except (ValueError, OverflowError, OSError):
        return None
    return None


def normalize(raw, resolve=None):
    """raw is the decoded reactions list; resolve(id, name) -> (id, name, kind)."""
    result = []
    for reaction in raw if isinstance(raw, list) else []:
        if isinstance(reaction, str):
            reaction = {"emoji": reaction}
        if not isinstance(reaction, dict):
            continue
        emoji = emoji_of(reaction)
        if not emoji:
            continue
        at = None
        for key in TIME_KEYS:
            at = when(reaction.get(key))
            if at:
                break
        users = reaction.get("users")
        people = []
        if isinstance(users, list) and users:
            people = [person(u) for u in users]
        else:
            rid = next(
                (text(reaction[k]) for k in ID_KEYS if text(reaction.get(k))), None
            )
            name = None
            for key in NAME_KEYS:
                nested_id, name = person(reaction.get(key))
                rid = rid or nested_id
                if name:
                    break
            if rid or name:
                people = [(rid, name)]
        try:
            count = int(reaction.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        for rid, name in people:
            kind = None
            if resolve:
                try:
                    rid, name, kind = resolve(rid, name)
                except Exception:  # resolution is best effort, never fatal
                    kind = None
            entry = {"emoji": emoji}
            for key, value in (
                ("sender_id", rid),
                ("sender_kind", kind if rid else None),
                ("sender_name", name),
                ("at", at),
            ):
                if value:
                    entry[key] = value
            result.append(entry)
        anonymous = max(count - len(people), 0) if people else count
        if not people:
            entry = {"emoji": emoji}
            if at:
                entry["at"] = at
            if anonymous > 1:
                entry["count"] = anonymous
            result.append(entry)
        elif anonymous:
            result.append({"emoji": emoji, "count": anonymous})
    return result


def line(details):
    """Readable content line: ``reactions: ❤️ Alex, 👍 Sam, 👍 ×3``."""
    if not details:
        return ""
    shown = []
    for entry in details[:LINE_ENTRIES]:
        who = entry.get("sender_name") or entry.get("sender_id")
        item = entry["emoji"] + (" " + who if who else "")
        if entry.get("count", 1) > 1:
            item += " ×" + str(entry["count"])
        shown.append(item)
    rest = len(details) - len(shown)
    result = "reactions: " + ", ".join(shown) + (f", and {rest} more" if rest else "")
    encoded = result.encode("utf-8")
    if len(encoded) > LINE_BYTES:
        result = encoded[: LINE_BYTES - 5].decode("utf-8", "ignore") + " …"
    return result
