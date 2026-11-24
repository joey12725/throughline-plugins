"""Deterministic mapping of raw fetch.py records to Throughline messages.

Pure functions of one raw record (plus the first-seen time the caller keeps),
so reruns of the same input produce identical messages.
"""

import hashlib
import json
import re
from datetime import datetime, timezone

# Bump when the mapping of a raw record changes: every item is re-emitted once
# (a new version) while its first-seen time is kept.
MAPPING_REVISION = 1

SITE = "https://www.reddit.com"
CHANNELS = {
    "saved": "reddit.saved",
    "upvoted": "reddit.votes",
    "downvoted": "reddit.votes",
    "submitted": "reddit.posts",
    "comments": "reddit.comments",
}
# Engagement counters change on every fetch. They are recorded as captured
# with the version that is stored, but a change in them alone is not a reason
# to write a new version.
VOLATILE = frozenset({"reddit_score", "num_comments", "upvote_ratio"})
FULLNAME = re.compile(r"^t[1-6]_[a-z0-9]{1,16}$")
MISSING_AUTHORS = {"", "[deleted]", "[removed]"}

FIELDS = {
    "item_type": ("text", "post or comment"),
    "fullname": ("text", "Reddit fullname of the item (t3_ post, t1_ comment)"),
    "subreddit": ("text", "Subreddit name without the r/ prefix"),
    "permalink": ("text", "Absolute reddit.com URL of the item"),
    "title": ("text", "Post title; for a comment, the title of its post"),
    "item_author": ("text", "Reddit username of the item's author, if not deleted"),
    "link_url": ("text", "Target URL of a link post"),
    "domain": ("text", "Domain of a link post (self.<subreddit> for text posts)"),
    "flair": ("text", "Post link flair text"),
    "reddit_score": (
        "integer",
        "Score when this version was captured (not updated on later fetches)",
    ),
    "num_comments": ("integer", "Comment count when this version was captured"),
    "upvote_ratio": ("number", "Upvote ratio when this version was captured"),
    "over_18": ("boolean", "Marked NSFW"),
    "created_at": ("timestamp", "When the item was posted on Reddit"),
    "edited_at": ("timestamp", "When the author last edited the item, if known"),
    "post_fullname": ("text", "Fullname of the post (the comment's post, or itself)"),
    "parent_fullname": ("text", "Fullname a comment replies to"),
    "vote": ("integer", "1 for an upvote, -1 for a downvote (reddit.votes only)"),
    "listing": (
        "text",
        "Source listing: saved, upvoted, downvoted, submitted or comments",
    ),
    "source": ("text", "Transport that fetched the item: oauth or session"),
    "account": ("text", "Reddit account whose listing held the item"),
    "time_precision": (
        "text",
        "source: happened_at is the item's creation time; first_seen: the time "
        "the action was first observed (Reddit does not timestamp saves or votes)",
    ),
    "mapping_revision": (
        "integer",
        "Revision of the mapping that produced this record",
    ),
}


def field_declarations():
    return [
        {"name": name, "type": kind, "description": text}
        for name, (kind, text) in FIELDS.items()
    ]


def utc(moment):
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def from_epoch(value):
    if isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(float(value), timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def integer(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def absolute(permalink):
    if not isinstance(permalink, str) or not permalink:
        return None
    return permalink if permalink.startswith("http") else SITE + permalink


def view_of(record):
    """Uniform view of a raw item: a Reddit listing child (t1 or t3)."""
    raw = record.get("raw")
    if not isinstance(raw, dict) or not isinstance(raw.get("data"), dict):
        return None
    data, kind = raw["data"], raw.get("kind")
    if kind not in ("t1", "t3"):
        return None
    comment = kind == "t1"
    edited = data.get("edited")
    link_url = None if comment or data.get("is_self") else text(data.get("url"))
    return {
        "item_type": "comment" if comment else "post",
        "subreddit": text(data.get("subreddit")) or text(record.get("subreddit")),
        "title": text(data.get("link_title" if comment else "title")),
        "body": text(data.get("body" if comment else "selftext")),
        "author": text(data.get("author")),
        "permalink": absolute(data.get("permalink")),
        "link_url": link_url,
        "domain": None if comment else text(data.get("domain")),
        "flair": None if comment else text(data.get("link_flair_text")),
        "score": integer(data.get("score")),
        "num_comments": integer(data.get("num_comments")),
        "upvote_ratio": number(data.get("upvote_ratio")),
        "over_18": data.get("over_18")
        if isinstance(data.get("over_18"), bool)
        else None,
        "created": from_epoch(data.get("created_utc")),
        "edited": from_epoch(edited) if edited not in (None, False) else None,
        "post": text(data.get("link_id")) if comment else record.get("id"),
        "parent": text(data.get("parent_id")) if comment else None,
    }


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:24]


class Item:
    """One raw record mapped; ``build(first_seen)`` makes the message."""

    def __init__(self, record, view, channel):
        self.record = record
        self.view = view
        self.channel = channel
        self.listing = record.get("listing")
        self.external_id = record["id"]
        self.key = f"{channel}|{self.external_id}"
        self.observed_at = parse_iso(record.get("observed_at"))
        probe, _ = self.build(None)
        self.digest = digest(
            [
                MAPPING_REVISION,
                {
                    k: v
                    for k, v in probe.items()
                    if k not in ("happened_at", "payload", "fields")
                },
                {k: v for k, v in probe["fields"].items() if k not in VOLATILE},
            ]
        )

    @property
    def first_seen_based(self):
        return self.channel in ("reddit.saved", "reddit.votes")

    def content(self):
        v, listing = self.view, self.listing
        sub = f"r/{v['subreddit']}" if v["subreddit"] else "Reddit"
        by = f" by u/{v['author']}" if v["author"] else ""
        title = v["title"] or "(untitled)"
        body = v["body"]
        if v["item_type"] == "comment":
            on = f' on "{v["title"]}"' if v["title"] else ""
            head = {
                "saved": f"Saved comment{by} in {sub}{on}",
                "upvoted": f"Upvoted comment{by} in {sub}{on}",
                "downvoted": f"Downvoted comment{by} in {sub}{on}",
                "comments": f"Commented in {sub}{on}",
                "submitted": f"Commented in {sub}{on}",
            }.get(listing, f"Comment{by} in {sub}{on}")
            return head + ":\n\n" + (body or "(no text)")
        head = {
            "saved": f"Saved post in {sub}{by}: {title}",
            "upvoted": f"Upvoted post in {sub}{by}: {title}",
            "downvoted": f"Downvoted post in {sub}{by}: {title}",
            "submitted": f"Posted in {sub}: {title}",
            "comments": f"Posted in {sub}: {title}",
        }.get(listing, f"{sub}: {title}")
        parts = [head]
        if body:
            parts.append(body)
        if v["link_url"]:
            parts.append(v["link_url"])
        return "\n\n".join(parts)

    def thread(self):
        v = self.view
        sub = v["subreddit"]
        if self.channel == "reddit.saved":
            return {"external_id": "saved", "title": "Saved on Reddit"}
        if self.channel == "reddit.votes":
            return {"external_id": "votes", "title": "Reddit votes"}
        if self.channel == "reddit.comments" and v["post"]:
            return {
                "external_id": f"post:{v['post']}",
                "title": v["title"] or (f"r/{sub}" if sub else v["post"]),
            }
        if sub:
            return {"external_id": f"r/{sub.lower()}", "title": f"r/{sub}"}
        return {"external_id": "unknown", "title": "Reddit"}

    def build(self, first_seen):
        v = self.view
        created = v["created"]
        first_seen_based = self.first_seen_based
        happened = first_seen if first_seen_based else created
        fields = {
            "item_type": v["item_type"],
            "fullname": self.external_id,
            "subreddit": v["subreddit"],
            "permalink": v["permalink"],
            "title": v["title"],
            "item_author": v["author"],
            "link_url": v["link_url"],
            "domain": v["domain"],
            "flair": v["flair"],
            "reddit_score": v["score"],
            "num_comments": v["num_comments"],
            "upvote_ratio": v["upvote_ratio"],
            "over_18": v["over_18"],
            "created_at": utc(created) if created else None,
            "edited_at": utc(v["edited"]) if v["edited"] else None,
            "post_fullname": v["post"],
            "parent_fullname": v["parent"],
            "vote": {"upvoted": 1, "downvoted": -1}.get(self.listing)
            if self.channel == "reddit.votes"
            else None,
            "listing": self.listing,
            "source": text(self.record.get("source")),
            "account": text(self.record.get("account")),
            "time_precision": "first_seen" if first_seen_based else "source",
            "mapping_revision": MAPPING_REVISION,
        }
        message = {
            "channel": self.channel,
            "external_id": self.external_id,
            "thread": self.thread(),
            "happened_at": utc(happened) if happened else None,
            "content": self.content(),
            "fields": fields,
            "payload": {
                "listing": self.listing,
                "source": self.record.get("source"),
                "item": self.record.get("raw"),
            },
        }
        author = v["author"]
        if not first_seen_based and author and author not in MISSING_AUTHORS:
            message["author"] = {
                "system": "reddit",
                "kind": "username",
                "value": author,
                "display_name": f"u/{author}",
            }
        if first_seen_based and created:
            message["subject"] = {"start": utc(created)}
        return message, self.links(message)

    def links(self, message):
        v = self.view
        ref = {"channel": self.channel, "external_id": self.external_id}
        kind = v["item_type"]
        entity = {
            "kind": kind,
            "external_ref": f"reddit:{self.external_id}",
            "name": (
                v["title"]
                if kind == "post"
                else f"Comment by u/{v['author'] or 'unknown'} on {v['title'] or v['post']}"
            )
            or self.external_id,
        }
        entity["name"] = entity["name"][:200]
        link_type = {"reddit.saved": "saved", "reddit.votes": "voted"}.get(
            self.channel, "represents"
        )
        links = [{"type": link_type, "from": ref, "to": entity, "method": "source"}]
        if v["subreddit"]:
            community = {
                "kind": "community",
                "external_ref": f"reddit:r/{v['subreddit'].lower()}",
                "name": f"r/{v['subreddit']}",
            }
            links.append(
                {
                    "type": "posted_in",
                    "from": entity,
                    "to": community,
                    "method": "source",
                }
            )
        if kind == "comment" and v["post"] and FULLNAME.fullmatch(v["post"]):
            post = {
                "kind": "post",
                "external_ref": f"reddit:{v['post']}",
                "name": (v["title"] or v["post"])[:200],
            }
            links.append(
                {"type": "reply_to", "from": entity, "to": post, "method": "source"}
            )
        return links


def item_of(record):
    """Map one raw record; None for summaries, unknown kinds and unusable rows."""
    if not isinstance(record, dict) or record.get("kind") != "reddit.item":
        return None
    channel = CHANNELS.get(record.get("listing"))
    if (
        channel is None
        or not isinstance(record.get("id"), str)
        or not FULLNAME.fullmatch(record["id"])
        or parse_iso(record.get("observed_at")) is None
    ):
        return None
    view = view_of(record)
    if view is None or (
        view["created"] is None and channel not in ("reddit.saved", "reddit.votes")
    ):
        return None
    return Item(record, view, channel)
