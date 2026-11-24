"""Pure, deterministic mapping; source records are never mutated."""

import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from urllib.parse import quote

from . import reactions as reaction_rules

# Conservative full-text indexing budget for one message's content.
CONTENT_LIMIT = 100_000


def network(value):
    value = (value or "unknown").lower().strip()
    aliases = {
        "google messages": "sms",
        "googlemessages": "sms",
        "discordgo": "discord",
        "instagramgo": "instagram",
        "slackgo": "slack",
        "imessage": "imessage",
    }
    value = aliases.get(value, value)
    value = re.sub(r"[^a-z0-9_]+", "_", value).strip("_") or "unknown"
    return value if value[0].isalpha() else "network_" + value


def decoded(value, default):
    try:
        result = (
            json.loads(value) if isinstance(value, str) and value.strip() else value
        )
    except (ValueError, TypeError):
        raise ValueError("malformed source JSON") from None
    if not isinstance(result, list) or any(not isinstance(v, dict) for v in result):
        if result is None or result == "":
            return default
        raise ValueError("source JSON must be an array of objects")
    return result


def reactions(value):
    """Reactions as a list of objects.

    Some archive rows (Instagram) store the reaction as a bare JSON string,
    the emoji text itself, instead of an array of reaction objects. That is
    still a reaction, not a reason to drop the message.
    """
    try:
        bare = json.loads(value) if isinstance(value, str) and value.strip() else None
    except (ValueError, TypeError):
        bare = None
    if isinstance(bare, str):
        return [{"emoji": bare}] if bare else []
    return decoded(value, [])


def timestamp(value):
    dt = datetime.fromisoformat(value)
    if dt.utcoffset() is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def content(body, attachments):
    if body and body.strip():
        return body
    return (
        " ".join(
            "[attachment: "
            + str(
                a.get("fileName")
                or a.get("filename")
                or a.get("title")
                or a.get("type")
                or "file"
            )
            + "]"
            for a in attachments
        )
        or "[empty chat message]"
    )


def message(
    net,
    external_id,
    thread_id,
    title,
    kind,
    at,
    body,
    sender,
    name,
    namespace,
    payload,
    enrich=None,
    **fields,
):
    fields["conversation_kind"] = kind
    base = content(body, fields["attachments"])
    reaction_line = reaction_rules.line(fields.get("reaction_details"))
    sections = []
    if enrich is not None:
        budget = CONTENT_LIMIT - size(base) - (size(reaction_line) + 2) - 2
        when = timestamp(at)  # New-inbound-only compares this with the enable time.
        try:
            extra = enrich(
                net, external_id, body or "", fields["attachments"], budget, at=when
            )
        except (sqlite3.Error, OSError):
            raise  # A broken cache or store must stop the run, not revert content.
        except Exception as error:
            logging.warning(
                "Beeper enrichment skipped id=%s: %s", external_id, type(error).__name__
            )
            extra = None
        if extra:
            sections = extra["sections"]
            fields.update(extra["fields"])
    text = compose(base, sections, reaction_line)
    if size(text) > CONTENT_LIMIT and sections:
        text = compose(base, [], reaction_line)  # The fields keep the full results.
    if size(text) > CONTENT_LIMIT:
        text = base
    result = dict(
        channel="chat." + net,
        external_id=external_id,
        thread=dict(
            external_id=thread_id, title=title, metadata={"kind": kind, "title": title}
        ),
        happened_at=timestamp(at),
        content=text,
        fields=fields,
        author={"system": net, "kind": namespace, "value": sender, "display_name": name}
        if sender
        else None,
        images=[],
        payload=payload,
    )

    # Reject unsafe values before batching; raw JSON text remains untouched.
    def has_nul(value):
        if isinstance(value, str):
            return "\x00" in value
        if isinstance(value, dict):
            return any(has_nul(k) or has_nul(v) for k, v in value.items())
        if isinstance(value, list):
            return any(has_nul(v) for v in value)
        return False

    if has_nul(result):
        raise ValueError("NUL in source row")
    if size(result["content"]) > CONTENT_LIMIT:
        raise ValueError("content exceeds safe full-text indexing budget")
    return result


def size(text):
    return len(text.encode("utf-8"))


def compose(base, sections, reaction_line):
    """Body (or attachment labels), enrichment sections, then reactions."""
    return "\n\n".join([base, *sections] + ([reaction_line] if reaction_line else []))


def unmangle(name):
    """UTF-8 text that was mis-decoded as Latin-1, repaired; else unchanged."""
    try:
        fixed = name.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name
    return fixed


def archive_resolver(senders, kind_of):
    """resolve(id, name) for reactors of one archive conversation."""
    by_name = (senders or {}).get("by_name", {})
    by_id = (senders or {}).get("by_id", {})
    # Meta exports often carry UTF-8 names decoded as Latin-1 ("JosÃ©") in
    # one place and repaired in another; match on the repaired form too.
    repaired = {}
    for known, sender in by_name.items():
        repaired.setdefault(unmangle(known), set()).add(sender)

    def resolve(rid, name):
        if not rid and name:
            rid = by_name.get(name)
            if rid is None:
                found = repaired.get(unmangle(name), set())
                rid = next(iter(found)) if len(found) == 1 else None
        name = name or by_id.get(rid)
        return rid, name, kind_of(rid) if rid else None

    return resolve


def live_resolver(chat):
    participants = (chat.get("participants") or {}).get("items") or []
    names = {
        p.get("id"): p.get("fullName") or p.get("username")
        for p in participants
        if isinstance(p, dict) and p.get("id")
    }

    def resolve(rid, name):
        return rid, name or names.get(rid), "beeper_user_id" if rid else None

    return resolve


def reaction_fields(raw, resolve):
    """Present only when there are reactions: absent keys keep old hashes."""
    details = reaction_rules.normalize(raw, resolve)
    return {"reaction_details": details} if details else {}


def default_namespace(net):
    return "phone" if net == "sms" else "username" if net == "snapchat" else "user_id"


def archive_message(
    row, conversation=None, namespace=None, neighbour=None, resolve=None, enrich=None
):
    """neighbour is (sent_at, id, relation) of a dated row in the same conversation.

    resolve(id, name) -> (id, name, kind) completes reactors; enrich is an
    optional Enricher.
    """
    conversation = conversation or {}
    net = network(row.get("platform"))
    raw_reactions = reactions(row.get("reactions_json"))
    if resolve is None:

        def resolve(rid, name):
            return rid, name, default_namespace(net) if rid else None

    kind = conversation.get("kind") or "unknown"
    if kind not in {"dm", "group_dm", "channel"}:
        kind = "unknown"
    at = row.get("sent_at")
    precision = row.get("time_precision") or "unknown"
    inferred = None
    if not at and row.get("time_raw"):
        raw = row["time_raw"]
        for fmt in (None, "%b %d, %Y, %I:%M %p"):
            try:
                at = (
                    datetime.fromisoformat(raw)
                    if fmt is None
                    else datetime.strptime(raw, fmt)
                ).isoformat()
                precision = "displayed_block"
                inferred = {"method": "time_raw"}
                break
            except ValueError:
                pass
    if not at and neighbour:
        # The source recorded no time at all. The nearest dated row of the same
        # conversation bounds it far better than the archive's import time.
        try:
            at = timestamp(neighbour[0])
            precision = "inferred"
            inferred = {
                "method": neighbour[2] + "_in_conversation",
                "message": neighbour[1],
                "sent_at": neighbour[0],
            }
        except (ValueError, TypeError):
            at = None
    if not at:
        at = row.get("first_imported_at")
        precision = "unknown"
        inferred = {"method": "first_imported_at"}
    elif precision != "inferred" and datetime.fromisoformat(at).utcoffset() is None:
        precision = "timezone_unknown"
    if not at:
        raise ValueError("archive message lacks sent_at and first_imported_at")
    return message(
        net,
        row["id"],
        row.get("conversation_id") or "archive:unknown",
        conversation.get("title"),
        kind,
        at,
        row.get("body"),
        row.get("sender_id"),
        row.get("sender_name"),
        namespace or default_namespace(net),
        row,
        enrich=enrich,
        direction=row.get("direction") or "unknown",
        time_precision=precision,
        # Present only when the time was inferred: a null key would change the
        # payload hash, and so the stored version, of every dated message.
        **({"time_inferred_from": inferred} if inferred else {}),
        reply_to=row.get("reply_to") or None,
        edited_at=timestamp(row["edited_at"]) if row.get("edited_at") else None,
        attachments=decoded(row.get("attachments_json"), []),
        reactions=raw_reactions,
        **reaction_fields(raw_reactions, resolve),
        identity_quality=row.get("identity_quality") or "unknown",
        guild_id=conversation.get("guild_id") or None,
    )


def live_id(account, chat, mid):
    return "beeper:" + ":".join(quote(str(v), safe="") for v in (account, chat, mid))


def live_message(row, chat, account, enrich=None):
    raw_reactions = row.get("reactions") or []
    if not isinstance(raw_reactions, list):
        raw_reactions = [raw_reactions]
    net = network(
        account.get("network")
        or chat.get("network")
        or account.get("bridge", {}).get("type")
    )
    kind = "dm" if chat["type"] == "single" else "group_dm"
    reply = row.get("linkedMessageID")
    return message(
        net,
        live_id(row["accountID"], row["chatID"], row["id"]),
        "beeper:" + chat["id"],
        chat.get("title"),
        kind,
        row["timestamp"],
        row.get("text"),
        row.get("senderID"),
        row.get("senderName"),
        "beeper_user_id",
        row,
        enrich=enrich,
        direction="outgoing"
        if row.get("isSender") is True
        else "incoming"
        if row.get("isSender") is False
        else "unknown",
        time_precision="exact",
        reply_to=live_id(row["accountID"], row["chatID"], reply) if reply else None,
        edited_at=timestamp(row["editedTimestamp"])
        if row.get("editedTimestamp")
        else None,
        attachments=row.get("attachments") or [],
        reactions=row.get("reactions") or [],
        **reaction_fields(raw_reactions, live_resolver(chat)),
        identity_quality="beeper_id",
        guild_id=None,
    )


def reply_link(channel, source, target):
    return {
        "type": "reply_to",
        "from": {"channel": channel, "external_id": source},
        "to": {"channel": channel, "external_id": target},
        "method": "source",
    }
