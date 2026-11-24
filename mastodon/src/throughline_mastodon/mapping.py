"""Mastodon API entities to Throughline messages and links.

Every id Mastodon returns is local to the instance that served it, so
external ids and status placeholders are prefixed with the instance host:
`<host>:<status id>`. The same status seen as your reblog target, a reply
parent, a favourite and a bookmark therefore resolves to one
`mastodon_status` entity.
"""

from datetime import datetime, timezone
from urllib.parse import urlsplit

from .text import html_to_text

MAPPING_REVISION = 1
STATUS_ENTITY = "mastodon_status"


def moment(value):
    """Offset-aware ISO text for a Mastodon timestamp ('...Z' or offset)."""
    if not value:
        return None
    when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.isoformat()


def qualified_acct(account, local_domain):
    """user@host for an account; local accounts get the instance's domain."""
    acct = (account or {}).get("acct") or (account or {}).get("username") or ""
    if not acct:
        return ""
    if "@" not in acct:
        acct = f"{acct}@{local_domain}"
    return acct.lower()


def author(account, local_domain):
    acct = qualified_acct(account, local_domain)
    if not acct:
        return None
    return {
        "system": "mastodon",
        "kind": "acct",
        "value": acct,
        "display_name": (account.get("display_name") or "").strip() or acct,
    }


def status_ref(host, status_id):
    return f"{host}:{status_id}"


def excerpt(text, limit=80):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def media_lines(status):
    lines, media = [], []
    for item in status.get("media_attachments") or []:
        kind = item.get("type") or "media"
        description = (item.get("description") or "").strip()
        lines.append(f"[{kind}: {description}]" if description else f"[{kind}]")
        media.append(
            {
                "type": kind,
                "url": item.get("url") or item.get("remote_url"),
                "description": description or None,
            }
        )
    return lines, media


def poll_line(status):
    poll = status.get("poll") or {}
    options = [o.get("title") for o in poll.get("options") or [] if o.get("title")]
    return ("Poll: " + " / ".join(options)) if options else None


def body(status):
    """Plain-text rendering of one status: CW, text, poll, media, card."""
    text, urls = html_to_text(status.get("content"))
    parts = []
    spoiler = (status.get("spoiler_text") or "").strip()
    if spoiler:
        parts.append(f"CW: {spoiler}")
    if text:
        parts.append(text)
    poll = poll_line(status)
    if poll:
        parts.append(poll)
    lines, media = media_lines(status)
    if lines:
        parts.append("\n".join(lines))
    card = status.get("card") or {}
    if card.get("url") and card["url"] not in urls and card.get("title"):
        parts.append(f"Link: {card['title']} ({card['url']})")
    return "\n\n".join(parts), urls, media


def status_fields(status, owner_id, local_domain):
    """Fields shared by every channel that carries a status."""
    text, urls, media = body(status)
    account = status.get("account") or {}
    return text, {
        "status_id": str(status.get("id")),
        "uri": status.get("uri"),
        "url": status.get("url") or status.get("uri"),
        "visibility": status.get("visibility"),
        "author_kind": "owner" if str(account.get("id")) == owner_id else "other",
        "status_author": qualified_acct(account, local_domain) or None,
        "is_reply": bool(status.get("in_reply_to_id")),
        "in_reply_to_id": _str(status.get("in_reply_to_id")),
        "spoiler_text": (status.get("spoiler_text") or "").strip() or None,
        "sensitive": bool(status.get("sensitive")),
        "language": status.get("language"),
        "tags": [t.get("name") for t in status.get("tags") or [] if t.get("name")],
        "mentions": [
            qualified_acct(m, local_domain)
            for m in status.get("mentions") or []
            if m.get("acct")
        ],
        "links": urls,
        "media": media,
        "media_count": len(media),
        "replies_count": _int(status.get("replies_count")),
        "reblogs_count": _int(status.get("reblogs_count")),
        "favourites_count": _int(status.get("favourites_count")),
        "edited_at": moment(status.get("edited_at")),
        "mapping_revision": MAPPING_REVISION,
    }


def _str(value):
    return None if value is None else str(value)


def _int(value):
    return value if type(value) is int else None


def link(link_type, message, to):
    return {
        "type": link_type,
        "from": {"channel": message["channel"], "external_id": message["external_id"]},
        "to": to,
        "method": "source",
    }


def placeholder(host, status, local_domain):
    acct = qualified_acct(status.get("account"), local_domain)
    if status.get("visibility") == "direct":
        # Entity names carry no access label: never copy private text there.
        text = "direct message"
    else:
        text, _, _ = body(status)
    name = f"@{acct}: {excerpt(text)}" if acct else excerpt(text)
    return {
        "kind": STATUS_ENTITY,
        "external_ref": status_ref(host, status["id"]),
        "name": name or status.get("uri") or status_ref(host, status["id"]),
    }


def reply_placeholder(host, status):
    parent = status_ref(host, status["in_reply_to_id"])
    return {"kind": STATUS_ENTITY, "external_ref": parent, "name": f"status {parent}"}


def access_label(status, direct_label):
    return direct_label if direct_label and status.get("visibility") == "direct" else None


def _finish(message, label):
    if label:
        message["access_label"] = label
    return message


def own_status(ctx, status):
    """One of your statuses (a post, reply or reblog) -> (message, links)."""
    host = ctx.host
    reblog = status.get("reblog")
    shown = reblog or status
    text, fields = status_fields(shown, ctx.owner_id, ctx.domain)
    fields.update(
        status_id=str(status["id"]),
        uri=status.get("uri"),
        url=status.get("url") or shown.get("url") or status.get("uri"),
        visibility=status.get("visibility"),
        author_kind="owner",
        is_reblog=bool(reblog),
        reblog_of_id=_str(reblog.get("id")) if reblog else None,
        reblog_of_author=fields["status_author"] if reblog else None,
        application=((status.get("application") or {}).get("name")),
    )
    fields["status_author"] = qualified_acct(status.get("account"), ctx.domain)
    if reblog:
        who = qualified_acct(reblog.get("account"), ctx.domain)
        text = f"Boosted @{who}:\n\n{text}" if text else f"Boosted @{who}"
    message = {
        "channel": "mastodon.statuses",
        "external_id": status_ref(host, status["id"]),
        "thread": {
            "external_id": f"{host}:{ctx.owner_id}:statuses",
            "title": f"Mastodon posts by @{ctx.owner_acct}",
        },
        "happened_at": moment(status["created_at"]),
        "author": author(status.get("account"), ctx.domain),
        "content": text or "(empty status)",
        "fields": fields,
        "payload": status,
    }
    links = []
    if reblog:
        links.append(link("reposted", message, placeholder(host, reblog, ctx.domain)))
    elif status.get("in_reply_to_id"):
        links.append(link("reply_to", message, reply_placeholder(host, status)))
    return _finish(message, access_label(shown, ctx.direct_label)), links


def saved_status(ctx, status, channel):
    """A favourited or bookmarked status -> (message, links).

    The API never says when you favourited or bookmarked something, so
    happened_at is the status's own created_at and time_basis says so."""
    host = ctx.host
    text, fields = status_fields(status, ctx.owner_id, ctx.domain)
    fields["time_basis"] = "status_created_at"
    kind = "favourites" if channel == "mastodon.favourites" else "bookmarks"
    message = {
        "channel": channel,
        "external_id": status_ref(host, status["id"]),
        "thread": {
            "external_id": f"{host}:{ctx.owner_id}:{kind}",
            "title": f"Mastodon {kind} of @{ctx.owner_acct}",
        },
        "happened_at": moment(status["created_at"]),
        "author": author(status.get("account"), ctx.domain),
        "content": text or "(empty status)",
        "fields": fields,
        "payload": status,
    }
    relation = "liked" if kind == "favourites" else "saved"
    links = [link(relation, message, placeholder(host, status, ctx.domain))]
    return _finish(message, access_label(status, ctx.direct_label)), links


VERBS = {
    "mention": "mentioned you",
    "status": "posted",
    "reblog": "boosted your post",
    "favourite": "favourited your post",
    "follow": "followed you",
    "follow_request": "requested to follow you",
    "poll": "a poll you voted in or created has ended",
    "update": "edited a post you interacted with",
    "quote": "quoted your post",
}


def notification(ctx, item):
    """A notification -> (message, links). Mentions carry the mentioning
    status's text; other types describe the action and quote your post."""
    host = ctx.host
    kind = item.get("type") or "unknown"
    account = item.get("account") or {}
    acct = qualified_acct(account, ctx.domain)
    status = item.get("status")
    verb = VERBS.get(kind, kind.replace("_", " "))
    fields = {
        "notification_id": str(item["id"]),
        "notification_type": kind,
        "author_kind": "owner" if str(account.get("id")) == ctx.owner_id else "other",
        "status_author": acct or None,
        "mapping_revision": MAPPING_REVISION,
    }
    text = ""
    label = None
    if status:
        text, sfields = status_fields(status, ctx.owner_id, ctx.domain)
        fields.update(
            status_id=sfields["status_id"],
            url=sfields["url"],
            visibility=sfields["visibility"],
            in_reply_to_id=sfields["in_reply_to_id"],
            spoiler_text=sfields["spoiler_text"],
        )
        fields["status_author"] = sfields["status_author"]
        label = access_label(status, ctx.direct_label)
    if kind in ("mention", "status") and text:
        content = text
    elif text:
        content = f"@{acct} {verb}:\n\n{text}"
    else:
        content = f"@{acct} {verb}" if acct else verb
    message = {
        "channel": "mastodon.notifications",
        "external_id": f"{host}:notification:{item['id']}",
        "thread": {
            "external_id": f"{host}:{ctx.owner_id}:notifications",
            "title": f"Mastodon notifications for @{ctx.owner_acct}",
        },
        "happened_at": moment(item["created_at"]),
        "author": author(account, ctx.domain),
        "content": content,
        "fields": fields,
        "payload": item,
    }
    if not message["author"]:
        del message["author"]
    links = []
    if status:
        to = placeholder(host, status, ctx.domain)
        if kind in ("mention", "status", "update"):
            if status.get("in_reply_to_id"):
                links.append(link("reply_to", message, reply_placeholder(host, status)))
            links.append(link("cites", message, to))
        elif kind == "favourite":
            links.append(link("liked", message, to))
        elif kind == "reblog":
            links.append(link("reposted", message, to))
        else:
            links.append(link("cites", message, to))
    return _finish(message, label), links


def host_of(url):
    return (urlsplit(url).hostname or "").lower()
