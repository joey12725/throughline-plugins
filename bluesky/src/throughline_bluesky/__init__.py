"""Bluesky (AT Protocol): the owner's own posts, replies, reposts and likes.

Signs in with a handle (or DID) and an app password
(com.atproto.server.createSession, refreshed with refreshSession), finds the
owner's PDS from the DID document, and reads the owner's repository with
com.atproto.repo.listRecords for app.bsky.feed.post, app.bsky.feed.repost and
app.bsky.feed.like. The repository holds the full history, so a first run
backfills everything; later runs page newest-first until they reach the
newest record already delivered. Optionally, notifications (mentions,
replies and quotes of the owner) come from app.bsky.notification.
listNotifications. Liked and reposted posts are looked up with
app.bsky.feed.getPosts so their text is searchable.
"""

import logging
import time
from datetime import datetime, timezone

import httpx
from throughline_sdk import (
    Channel,
    Checkpoint,
    Declaration,
    Manifest,
    Plugin,
    Record,
    coverage,
    run,
)

from . import fake

__all__ = ["Bluesky", "main", "tid_time", "post_text"]

POSTS = "bluesky.posts"
LIKES = "bluesky.likes"
REPOSTS = "bluesky.reposts"
NOTIFICATIONS = "bluesky.notifications"
COLLECTIONS = [
    ("app.bsky.feed.post", POSTS),
    ("app.bsky.feed.repost", REPOSTS),
    ("app.bsky.feed.like", LIKES),
]
TID_ALPHABET = "234567abcdefghijklmnopqrstuvwxyz"
log = logging.getLogger("throughline_bluesky")


def fields(**values):
    return [dict(name=k, type=v[0], description=v[1]) for k, v in values.items()]


COMMON = dict(
    author_kind=("text", "owner (your own record) or other (someone else's)"),
    uri=("text", "AT URI of the record"),
    rkey=("text", "Record key in the repository"),
    cid=("text", "Content id of the record version"),
)

POST_FIELDS = fields(
    **COMMON,
    handle=("text", "Your handle when the post was read"),
    is_reply=("boolean", "The post replies to another post"),
    reply_parent_uri=("text", "AT URI of the post replied to"),
    reply_root_uri=("text", "AT URI of the first post of the thread"),
    quote_uri=("text", "AT URI of a quoted post or record"),
    embed_type=("text", "images, video, external, record, record_with_media, or empty"),
    image_count=("integer", "Images attached"),
    external_uri=("text", "Link card URL"),
    link_uris=("json", "URLs linked in the text"),
    mention_dids=("json", "DIDs mentioned in the text"),
    tags=("json", "Hashtags"),
    langs=("json", "Declared languages"),
)

SUBJECT_FIELDS = fields(
    **COMMON,
    subject_uri=("text", "AT URI of the liked or reposted record"),
    subject_cid=("text", "Content id of that record"),
    subject_author_did=("text", "DID of its author"),
    subject_author_handle=("text", "Handle of its author when looked up"),
    subject_text=("text", "Its text when looked up"),
    subject_available=(
        "boolean",
        "The subject was found by getPosts (false: deleted, blocked, not a post, or lookup off)",
    ),
    subject_created_at=("timestamp", "When the subject post was created"),
)

NOTIFICATION_FIELDS = fields(
    **COMMON,
    reason=("text", "mention, reply, quote (or like, repost, follow when configured)"),
    reason_subject=("text", "AT URI of your record the notification is about"),
    from_did=("text", "DID of the other account"),
    from_handle=("text", "Handle of the other account"),
    is_read=("boolean", "Seen in the app when read"),
    indexed_at=("timestamp", "When the AppView indexed it"),
    reply_parent_uri=("text", "AT URI of the post replied to"),
    reply_root_uri=("text", "AT URI of the first post of the thread"),
)

SETTINGS = {
    "identifier": Declaration(
        env="BLUESKY_IDENTIFIER", description="Your handle (alice.bsky.social) or DID"
    ),
    "service": Declaration(
        env="BLUESKY_SERVICE",
        required=False,
        default="https://bsky.social",
        description="Where to sign in (the PDS or entryway that holds your account)",
    ),
    "plc_url": Declaration(
        env="BLUESKY_PLC_URL",
        required=False,
        default="https://plc.directory",
        description="PLC directory used to resolve did:plc documents",
    ),
    "hydrate": Declaration(
        env="BLUESKY_HYDRATE",
        required=False,
        default="true",
        description="Look up the text of liked and reposted posts (getPosts)",
    ),
    "notifications": Declaration(
        env="BLUESKY_NOTIFICATIONS",
        required=False,
        default="false",
        description="Also read notifications (replies, mentions, quotes of you)",
    ),
    "notification_reasons": Declaration(
        env="BLUESKY_NOTIFICATION_REASONS",
        required=False,
        default="mention,reply,quote",
        description="Comma-separated notification reasons to keep",
    ),
    "page_size": Declaration(
        env="BLUESKY_PAGE_SIZE",
        required=False,
        default="100",
        description="Records per request (1-100); each page is one checkpoint",
    ),
    "fixture": Declaration(
        env="BLUESKY_FIXTURE",
        required=False,
        description="Tests only: JSON file served by an offline PDS instead of the network",
    ),
}


def truthy(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def tid_time(key):
    """The creation time encoded in a TID record key, or None."""
    if len(key) != 13 or any(c not in TID_ALPHABET for c in key):
        return None
    value = 0
    for char in key:
        value = value * 32 + TID_ALPHABET.index(char)
    micros = value >> 10
    try:
        return datetime.fromtimestamp(micros / 1e6, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def moment(value, key=None):
    """Offset-aware ISO time of a record: createdAt, else the TID's time."""
    if isinstance(value, str) and value:
        try:
            when = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            when = None
        if when is not None:
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return when.isoformat()
    when = tid_time(key) if key else None
    return when.isoformat() if when else None


def rkey_of(uri):
    return uri.rsplit("/", 1)[-1]


def did_of(uri):
    parts = uri.split("/")
    return parts[2] if len(parts) > 2 and uri.startswith("at://") else None


def facets(value):
    links, mentions, tags = [], [], []
    for facet in value.get("facets") or []:
        for feature in (facet or {}).get("features") or []:
            kind = (feature or {}).get("$type", "")
            if kind.endswith("#link") and feature.get("uri"):
                links.append(feature["uri"])
            elif kind.endswith("#mention") and feature.get("did"):
                mentions.append(feature["did"])
            elif kind.endswith("#tag") and feature.get("tag"):
                tags.append(feature["tag"])
    tags += [t for t in value.get("tags") or [] if isinstance(t, str)]
    return links, mentions, tags


def embed_parts(embed):
    """(embed_type, image alts, video alt, external, quoted uri)."""
    if not isinstance(embed, dict):
        return None, [], None, None, None
    kind = embed.get("$type", "")
    if kind == "app.bsky.embed.recordWithMedia":
        media = embed_parts(embed.get("media"))
        quoted = ((embed.get("record") or {}).get("record") or {}).get("uri")
        return "record_with_media", media[1], media[2], media[3], quoted
    if kind == "app.bsky.embed.images":
        alts = [(i or {}).get("alt") or "" for i in embed.get("images") or []]
        return "images", alts, None, None, None
    if kind == "app.bsky.embed.video":
        return "video", [], embed.get("alt") or "", None, None
    if kind == "app.bsky.embed.external":
        return "external", [], None, embed.get("external") or {}, None
    if kind == "app.bsky.embed.record":
        return "record", [], None, None, (embed.get("record") or {}).get("uri")
    return kind or None, [], None, None, None


def post_text(value):
    """Searchable text of a post record: its text plus what it embeds."""
    lines = [(value.get("text") or "").strip()]
    _, alts, video, external, quoted = embed_parts(value.get("embed"))
    for alt in alts:
        lines.append(f"[image: {alt}]" if alt else "[image]")
    if video is not None:
        lines.append(f"[video: {video}]" if video else "[video]")
    if external:
        title = (external.get("title") or "").strip()
        lines.append(f"[link: {title} {external.get('uri') or ''}]".replace("  ", " "))
    if quoted:
        lines.append(f"[quoting {quoted}]")
    text = "\n".join(line for line in lines if line).strip()
    return text or "[empty post]"


class AuthError(RuntimeError):
    pass


class SourceUnavailable(RuntimeError):
    pass


class Session:
    """XRPC calls against the owner's PDS with refresh on ExpiredToken."""

    def __init__(self, plugin, http):
        self.plugin = plugin
        self.http = http
        self.service = plugin.settings["service"].rstrip("/")
        self.access = self.refresh = None
        self.did = self.handle = None
        self.pds = None
        self.sleep = plugin.sleep

    def login(self):
        response = self.send(
            "POST",
            self.service + "/xrpc/com.atproto.server.createSession",
            json={
                "identifier": self.plugin.settings["identifier"],
                "password": self.plugin.secrets["app_password"],
            },
        )
        if response.status_code in (400, 401):
            raise AuthError(
                "Bluesky refused the sign-in (%s): check BLUESKY_IDENTIFIER and the app "
                "password" % self.error_name(response)
            )
        self.check(response)
        self.adopt(response.json())
        doc = response.json().get("didDoc") or self.resolve(self.did)
        self.pds = self.endpoint(doc) or self.service

    def adopt(self, body):
        self.access, self.refresh = body["accessJwt"], body["refreshJwt"]
        self.did, self.handle = body["did"], body.get("handle")

    def resolve(self, did):
        if did.startswith("did:plc:"):
            url = self.plugin.settings["plc_url"].rstrip("/") + "/" + did
        elif did.startswith("did:web:"):
            url = "https://" + did[len("did:web:") :].replace(":", "/") + "/.well-known/did.json"
        else:
            return None
        response = self.send("GET", url)
        return response.json() if response.status_code == 200 else None

    @staticmethod
    def endpoint(doc):
        for service in (doc or {}).get("service") or []:
            if str(service.get("id", "")).endswith("#atproto_pds") or service.get(
                "type"
            ) == "AtprotoPersonalDataServer":
                return str(service.get("serviceEndpoint") or "").rstrip("/") or None
        return None

    @staticmethod
    def error_name(response):
        try:
            return response.json().get("error") or f"HTTP {response.status_code}"
        except ValueError:
            return f"HTTP {response.status_code}"

    def send(self, method, url, **kwargs):
        for attempt in range(4):
            try:
                response = self.http.request(method, url, **kwargs)
            except httpx.TransportError as error:
                if attempt == 3:
                    raise SourceUnavailable(f"Bluesky unreachable ({type(error).__name__})") from None
                self.sleep(2**attempt)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == 3:
                    raise SourceUnavailable(f"Bluesky HTTP {response.status_code}")
                wait = 2**attempt
                reset = response.headers.get("ratelimit-reset", "")
                if response.status_code == 429 and reset.isdigit():
                    wait = min(300, max(1, int(reset) - int(time.time())))
                self.sleep(wait)
                continue
            return response
        raise SourceUnavailable("Bluesky retry limit reached")

    def check(self, response):
        if response.is_error:
            raise SourceUnavailable(f"Bluesky {self.error_name(response)}")

    def get(self, method, params):
        for attempt in range(2):
            response = self.send(
                "GET",
                f"{self.pds}/xrpc/{method}",
                params=params,
                headers={"Authorization": "Bearer " + self.access},
            )
            if response.status_code in (400, 401) and self.error_name(response) in (
                "ExpiredToken",
                "InvalidToken",
            ):
                if attempt == 0:
                    self.renew()
                    continue
            self.check(response)
            return response.json()
        raise AuthError("Bluesky session could not be renewed")

    def renew(self):
        response = self.send(
            "POST",
            f"{self.pds}/xrpc/com.atproto.server.refreshSession",
            headers={"Authorization": "Bearer " + self.refresh},
        )
        if response.is_error:
            self.login()  # The refresh token expired as well: sign in again.
            return
        self.adopt(response.json())


class Bluesky(Plugin):
    manifest = Manifest(
        name="bluesky",
        version="0.1.0",
        channels=[
            Channel(
                name=POSTS,
                description=(
                    "Your own Bluesky posts and replies from your repository, with image "
                    "alt text, link cards and quoted posts; threaded by the reply root"
                ),
                fields=POST_FIELDS,
                conversation_gap_s=6 * 3600,
            ),
            Channel(
                name=REPOSTS,
                description="Posts you reposted, with the reposted post's author and text",
                fields=SUBJECT_FIELDS,
                is_reference=True,
            ),
            Channel(
                name=LIKES,
                description="Posts you liked, with the liked post's author and text",
                fields=SUBJECT_FIELDS,
                is_reference=True,
            ),
            Channel(
                name=NOTIFICATIONS,
                description=(
                    "Other people's replies to, mentions and quotes of you on Bluesky "
                    "(only when BLUESKY_NOTIFICATIONS is on)"
                ),
                fields=NOTIFICATION_FIELDS,
                conversation_gap_s=6 * 3600,
            ),
        ],
        settings=SETTINGS,
        secrets={
            "app_password": Declaration(
                env="BLUESKY_APP_PASSWORD",
                description="App password (Settings > Privacy and security > App passwords)",
            )
        },
        run_mode="poll",
        default_interval_s=1800,
        link_types=["reply_to", "quotes", "reposted", "liked"],
    )

    def __init__(self, transport=None):
        super().__init__()
        self.transport = transport
        if transport is None and self.settings["fixture"]:
            self.transport = fake.from_file(self.settings["fixture"]).transport()
        self.page_size = min(100, max(1, int(self.settings["page_size"] or 100)))
        self.hydrate = truthy(self.settings["hydrate"])
        self.notifications = truthy(self.settings["notifications"])
        self.reasons = {
            r.strip() for r in (self.settings["notification_reasons"] or "").split(",") if r.strip()
        }
        self.sleep = time.sleep

    # -- mapping --------------------------------------------------------------

    def owner(self, session):
        return {
            "system": "bluesky",
            "kind": "account",
            "value": session.did,
            "display_name": session.handle,
        }

    def map_post(self, session, record):
        value = record.get("value") or {}
        uri = record["uri"]
        key = rkey_of(uri)
        reply = value.get("reply") or {}
        parent = (reply.get("parent") or {}).get("uri")
        root = (reply.get("root") or {}).get("uri") or parent
        embed_type, alts, _, external, quoted = embed_parts(value.get("embed"))
        links, mentions, tags = facets(value)
        text = post_text(value)
        message = dict(
            channel=POSTS,
            external_id=uri,
            thread={
                "external_id": root or uri,
                "title": None if root else text.splitlines()[0][:120],
            },
            happened_at=moment(value.get("createdAt"), key),
            author=self.owner(session),
            content=text,
            fields=dict(
                author_kind="owner",
                uri=uri,
                rkey=key,
                cid=record.get("cid"),
                handle=session.handle,
                is_reply=bool(parent),
                reply_parent_uri=parent,
                reply_root_uri=root,
                quote_uri=quoted,
                embed_type=embed_type,
                image_count=len(alts),
                external_uri=(external or {}).get("uri"),
                link_uris=links,
                mention_dids=mentions,
                tags=tags,
                langs=value.get("langs") or [],
            ),
            payload=record,
        )
        links_out = []
        if parent:
            links_out.append(("reply_to", parent))
        if quoted:
            links_out.append(("quotes", quoted))
        return message, links_out

    def map_subject(self, session, channel, record, post):
        value = record.get("value") or {}
        uri = record["uri"]
        key = rkey_of(uri)
        subject = value.get("subject") or {}
        target = subject.get("uri") or ""
        verb = "Liked" if channel == LIKES else "Reposted"
        author = (post or {}).get("author") or {}
        body = (post or {}).get("record") or {}
        handle = author.get("handle")
        if post:
            text = f"{verb} @{handle or did_of(target)}'s post: {post_text(body)}"
        else:
            text = f"{verb} {target or 'a record'} (text not available)"
        payload = dict(record)
        if post:
            payload["subject_post"] = post
        message = dict(
            channel=channel,
            external_id=uri,
            thread={
                "external_id": channel.split(".")[1],
                "title": "Bluesky " + channel.split(".")[1],
            },
            happened_at=moment(value.get("createdAt"), key),
            author=self.owner(session),
            content=text,
            fields=dict(
                author_kind="owner",
                uri=uri,
                rkey=key,
                cid=record.get("cid"),
                subject_uri=target or None,
                subject_cid=subject.get("cid"),
                subject_author_did=author.get("did") or did_of(target),
                subject_author_handle=handle,
                subject_text=(body.get("text") if post else None),
                subject_available=bool(post),
                subject_created_at=moment(body.get("createdAt")) if post else None,
            ),
            payload=payload,
        )
        return message, [("liked" if channel == LIKES else "reposted", target)] if target else []

    def map_notification(self, item):
        record = item.get("record") or {}
        author = item.get("author") or {}
        reason = item.get("reason") or "unknown"
        handle = author.get("handle") or author.get("did")
        reply = record.get("reply") or {}
        parent = (reply.get("parent") or {}).get("uri")
        root = (reply.get("root") or {}).get("uri") or parent
        if reason in ("reply", "mention", "quote"):
            verb = {"reply": "replied", "mention": "mentioned you", "quote": "quoted you"}[reason]
            text = f"@{handle} {verb}: {post_text(record)}"
        else:
            text = f"@{handle} {reason}" + (
                f" ({item.get('reasonSubject')})" if item.get("reasonSubject") else ""
            )
        uri = item["uri"]
        when = moment(record.get("createdAt")) or moment(item.get("indexedAt"))
        return dict(
            channel=NOTIFICATIONS,
            external_id=f"{reason}:{uri}",
            thread={"external_id": root or item.get("reasonSubject") or uri},
            happened_at=when,
            author={
                "system": "bluesky",
                "kind": "account",
                "value": author.get("did"),
                "display_name": author.get("displayName") or handle,
            },
            content=text,
            fields=dict(
                author_kind="other",
                uri=uri,
                rkey=rkey_of(uri),
                cid=item.get("cid"),
                reason=reason,
                reason_subject=item.get("reasonSubject"),
                from_did=author.get("did"),
                from_handle=author.get("handle"),
                is_read=item.get("isRead"),
                indexed_at=moment(item.get("indexedAt")),
                reply_parent_uri=parent,
                reply_root_uri=root,
            ),
            payload=item,
        )

    @staticmethod
    def link(channel, external_id, kind, target):
        return Record(
            kind="link",
            data={
                "type": kind,
                "from": {"channel": channel, "external_id": external_id},
                "to": {"kind": "bluesky_post", "external_ref": target},
                "method": "source",
            },
        )

    def lookup(self, session, uris):
        found = {}
        wanted = sorted({u for u in uris if u and "/app.bsky.feed.post/" in u})
        for start in range(0, len(wanted), 25):
            answer = session.get("app.bsky.feed.getPosts", [("uris", u) for u in wanted[start : start + 25]])
            for post in answer.get("posts") or []:
                found[post.get("uri")] = post
        return found

    # -- run ------------------------------------------------------------------

    def records(self, cursor, mode):
        state = {k: dict(v) for k, v in (cursor or {}).items() if isinstance(v, dict)}
        with httpx.Client(timeout=60, transport=self.transport, follow_redirects=True) as http:
            session = Session(self, http)
            session.login()
            if state.get("did") and state["did"].get("value") != session.did:
                state = {}  # A different account: start over.
            state["did"] = {"value": session.did}
            for collection, channel in COLLECTIONS:
                yield from self.collection(session, state, collection, channel)
            if self.notifications:
                yield from self.read_notifications(session, state)

    def collection(self, session, state, collection, channel):
        st = state.setdefault(collection, {})
        high, page = st.get("high"), st.get("page")
        pass_high = st.get("pass_high")
        while True:
            params = {"repo": session.did, "collection": collection, "limit": self.page_size}
            if page:
                params["cursor"] = page
            answer = session.get("com.atproto.repo.listRecords", params)
            rows = [r for r in answer.get("records") or [] if isinstance(r, dict) and r.get("uri")]
            fresh, done = [], False
            for row in rows:
                key = rkey_of(row["uri"])
                if high and key <= high:
                    done = True
                    break
                fresh.append(row)
                pass_high = max(pass_high, key) if pass_high else key
            posts = {}
            if fresh and channel != POSTS and self.hydrate:
                posts = self.lookup(
                    session, [((r.get("value") or {}).get("subject") or {}).get("uri") for r in fresh]
                )
            for row in fresh:
                if channel == POSTS:
                    message, links = self.map_post(session, row)
                else:
                    target = ((row.get("value") or {}).get("subject") or {}).get("uri")
                    message, links = self.map_subject(session, channel, row, posts.get(target))
                if not message["happened_at"]:
                    log.warning("skipping %s record without a usable time", collection)
                    continue
                yield message
                for kind, target in links:
                    yield self.link(channel, message["external_id"], kind, target)
            page = answer.get("cursor")
            if done or not page or not rows:
                if pass_high:
                    high = max(high, pass_high) if high else pass_high
                state[collection] = {"high": high}
                yield Checkpoint(cursor=state)
                return
            state[collection] = {"high": high, "pass_high": pass_high, "page": page}
            yield Checkpoint(cursor=state)

    def read_notifications(self, session, state):
        st = state.setdefault("notifications", {})
        high, page = st.get("high"), st.get("page")
        pass_high = st.get("pass_high")
        oldest = None
        while True:
            params = {"limit": self.page_size}
            if page:
                params["cursor"] = page
            answer = session.get("app.bsky.notification.listNotifications", params)
            rows = [n for n in answer.get("notifications") or [] if isinstance(n, dict) and n.get("uri")]
            done = False
            for item in rows:
                indexed = moment(item.get("indexedAt"))
                if not indexed:
                    continue
                if high and indexed < high:
                    done = True
                    break
                pass_high = max(pass_high, indexed) if pass_high else indexed
                oldest = min(oldest, indexed) if oldest else indexed
                if item.get("reason") not in self.reasons:
                    continue
                message = self.map_notification(item)
                if message["happened_at"]:
                    yield message
            page = answer.get("cursor")
            if done or not page or not rows:
                if not done and high and oldest and oldest > high:
                    # The AppView served no older notifications than these:
                    # everything between the last run and them is gone.
                    yield coverage(
                        channel=NOTIFICATIONS,
                        status="partial",
                        reasons=["source_cap"],
                        gaps=[{"from": high, "to": oldest, "reason": "source_cap"}],
                    )
                if pass_high:
                    high = max(high, pass_high) if high else pass_high
                state["notifications"] = {"high": high}
                yield Checkpoint(cursor=state)
                return
            state["notifications"] = {"high": high, "pass_high": pass_high, "page": page}
            yield Checkpoint(cursor=state)


def main(argv=None):
    run(Bluesky, argv)
