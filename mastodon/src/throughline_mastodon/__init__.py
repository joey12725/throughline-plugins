"""Throughline connector for any Mastodon instance.

Polls the client API with a user access token: your own statuses (posts,
replies, reblogs), your favourites and bookmarks, and optionally your
notifications. Each stream is read newest-first back to the beginning once
(max_id paging), then forward from the newest item seen (min_id paging),
with a checkpoint after every page.
"""

import copy
from dataclasses import dataclass

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

from .client import MastodonClient, SourceError, fixture_transport
from .mapping import (
    MAPPING_REVISION,
    notification,
    own_status,
    qualified_acct,
    saved_status,
)

__all__ = ["Mastodon", "MAPPING_REVISION", "main"]


def fields(**values):
    return [dict(name=k, type=v[0], description=v[1]) for k, v in values.items()]


STATUS = dict(
    status_id=("text", "Status id on your instance"),
    uri=("text", "ActivityPub URI of the status (globally unique)"),
    url=("text", "Web URL of the status"),
    visibility=("text", "public, unlisted, private (followers) or direct"),
    author_kind=("text", "owner (posted by you) or other"),
    status_author=("text", "Author of the status as user@host"),
    is_reply=("boolean", "The status replies to another status"),
    in_reply_to_id=("text", "Instance id of the status it replies to"),
    spoiler_text=("text", "Content warning text, if any"),
    sensitive=("boolean", "Marked sensitive"),
    language=("text", "ISO 639 language code the author set"),
    tags=("json", "Hashtags (without #)"),
    mentions=("json", "Mentioned accounts as user@host"),
    links=("json", "Link targets found in the text"),
    media=("json", "Attachments: [{type, url, description}]"),
    media_count=("integer", "Number of media attachments"),
    replies_count=("integer", "Replies when fetched"),
    reblogs_count=("integer", "Boosts when fetched"),
    favourites_count=("integer", "Favourites when fetched"),
    edited_at=("timestamp", "Last edit, if edited"),
    mapping_revision=("integer", "Mapping revision that produced this record"),
)

STATUS_FIELDS = fields(
    **STATUS,
    is_reblog=("boolean", "A reblog (boost) of someone's status"),
    reblog_of_id=("text", "Instance id of the boosted status"),
    reblog_of_author=("text", "Author of the boosted status as user@host"),
    application=("text", "App the status was posted from, when the server says"),
)

SAVED_FIELDS = fields(
    **STATUS,
    time_basis=(
        "text",
        "Always status_created_at: happened_at is when the status was posted, "
        "since Mastodon does not expose when you favourited or bookmarked it",
    ),
)

NOTIFICATION_FIELDS = fields(
    notification_id=("text", "Notification id on your instance"),
    notification_type=(
        "text",
        "mention, status, reblog, favourite, follow, follow_request, poll, update, ...",
    ),
    author_kind=("text", "owner or other (who triggered the notification)"),
    status_author=("text", "Account that triggered it (or wrote the status) as user@host"),
    status_id=("text", "Instance id of the status involved, if any"),
    url=("text", "Web URL of the status involved"),
    visibility=("text", "Visibility of the status involved"),
    in_reply_to_id=("text", "Status the mentioning status replies to"),
    spoiler_text=("text", "Content warning of the status involved"),
    mapping_revision=("integer", "Mapping revision that produced this record"),
)

STREAMS = {
    "statuses": "mastodon.statuses",
    "favourites": "mastodon.favourites",
    "bookmarks": "mastodon.bookmarks",
    "notifications": "mastodon.notifications",
}


def truthy(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def id_key(value):
    """Mastodon ids are decimal strings of varying length: order numerically."""
    text = str(value)
    return (len(text), text)


@dataclass
class Context:
    host: str
    domain: str
    owner_id: str
    owner_acct: str
    direct_label: str | None


class Mastodon(Plugin):
    manifest = Manifest(
        name="mastodon",
        version="0.1.0",
        channels=[
            Channel(
                name="mastodon.statuses",
                description=(
                    "Your own Mastodon statuses: posts, replies and reblogs (boosts, "
                    "is_reblog). Text is HTML converted to plain text with content "
                    "warnings, polls and media descriptions"
                ),
                fields=STATUS_FIELDS,
                conversation_gap_s=3600,
            ),
            Channel(
                name="mastodon.favourites",
                description=(
                    "Statuses you favourited (liked). happened_at is the status's "
                    "post time, not the time you favourited it (time_basis)"
                ),
                fields=SAVED_FIELDS,
                is_reference=True,
                is_outside=True,
            ),
            Channel(
                name="mastodon.bookmarks",
                description=(
                    "Statuses you bookmarked. happened_at is the status's post time, "
                    "not the time you bookmarked it (time_basis)"
                ),
                fields=SAVED_FIELDS,
                is_reference=True,
                is_outside=True,
            ),
            Channel(
                name="mastodon.notifications",
                description=(
                    "Optional (MASTODON_NOTIFICATIONS): notifications such as "
                    "mentions and replies to you, filtered by MASTODON_NOTIFICATION_TYPES"
                ),
                fields=NOTIFICATION_FIELDS,
                conversation_gap_s=3600,
            ),
        ],
        settings={
            "base_url": Declaration(
                env="MASTODON_BASE_URL",
                description="Your instance, e.g. https://mastodon.example",
            ),
            "acct_domain": Declaration(
                env="MASTODON_ACCT_DOMAIN",
                description=(
                    "Domain in local accounts' user@domain handles when it differs "
                    "from the web host (LOCAL_DOMAIN vs WEB_DOMAIN setups)"
                ),
                required=False,
            ),
            "capture": Declaration(
                env="MASTODON_CAPTURE",
                description="Comma list of statuses,favourites,bookmarks",
                required=False,
                default="statuses,favourites,bookmarks",
            ),
            "notifications": Declaration(
                env="MASTODON_NOTIFICATIONS",
                description="true to capture notifications too",
                required=False,
                default="false",
            ),
            "notification_types": Declaration(
                env="MASTODON_NOTIFICATION_TYPES",
                description="Comma list of notification types (mention covers replies)",
                required=False,
                default="mention",
            ),
            "direct_label": Declaration(
                env="MASTODON_DIRECT_LABEL",
                description=(
                    "Access label for direct-visibility statuses; empty disables"
                ),
                required=False,
                default="third_party_confidence",
            ),
            "batch_size": Declaration(
                env="MASTODON_BATCH_SIZE",
                description="Items per API page (1-40)",
                required=False,
                default="40",
            ),
            "fixture": Declaration(
                env="MASTODON_FIXTURE",
                description="Recorded-response JSON file used instead of the network (tests)",
                required=False,
            ),
        },
        secrets={
            "token": Declaration(
                env="MASTODON_ACCESS_TOKEN",
                description="User access token with read scopes",
            )
        },
        default_interval_s=900,
        link_types=["reply_to", "reposted", "liked", "saved", "cites"],
    )

    transport = None  # tests may inject an httpx transport

    def client(self):
        transport = self.transport
        if transport is None and self.settings.get("fixture"):
            transport = fixture_transport(self.settings["fixture"])
        options = {}
        if transport is not None:
            options = {"transport": transport, "sleep": lambda _seconds: None}
        return MastodonClient(
            self.settings["base_url"], self.secrets["token"], **options
        )

    def batch_size(self):
        try:
            size = int(self.settings.get("batch_size") or 40)
        except ValueError:
            size = 40
        return max(1, min(size, 40))

    def streams(self):
        wanted = {
            s.strip().lower()
            for s in (self.settings.get("capture") or "").split(",")
            if s.strip()
        }
        unknown = wanted - {"statuses", "favourites", "bookmarks"}
        if unknown:
            raise ValueError("MASTODON_CAPTURE: unknown " + ", ".join(sorted(unknown)))
        names = [s for s in ("statuses", "favourites", "bookmarks") if s in wanted]
        if truthy(self.settings.get("notifications")):
            names.append("notifications")
        return names

    def records(self, cursor, mode):
        state = copy.deepcopy(cursor or {})
        api = self.client()
        try:
            yield from self._records(api, state)
        finally:
            api.close()

    def _records(self, api, state):
        me, _ = api.get("/api/v1/accounts/verify_credentials")
        owner_id = str(me["id"])
        domain = (self.settings.get("acct_domain") or api.host).strip().lower()
        ctx = Context(
            host=api.host,
            domain=domain,
            owner_id=owner_id,
            owner_acct=qualified_acct(me, domain),
            direct_label=(self.settings.get("direct_label") or "").strip() or None,
        )
        if state.get("account_id") not in (None, owner_id):
            # A token for a different account: start that account's history.
            state = {}
        state["account_id"] = owner_id
        state.setdefault("streams", {})
        names = self.streams()
        failed = []
        for name in names:
            try:
                yield from self.stream(api, ctx, state, name)
            except SourceError as error:
                failed.append(name)
                yield coverage(
                    STREAMS[name],
                    status="partial",
                    reasons=["source_error"],
                    detail={"error": str(error)[:300]},
                )
        if names and len(failed) == len(names):
            raise SourceError("every Mastodon stream failed: " + ", ".join(failed))

    def request(self, ctx, name):
        limit = self.batch_size()
        if name == "statuses":
            return f"/api/v1/accounts/{ctx.owner_id}/statuses", {"limit": limit}
        if name == "notifications":
            types = [
                t.strip()
                for t in (self.settings.get("notification_types") or "").split(",")
                if t.strip()
            ]
            params = {"limit": limit}
            if types:
                params["types[]"] = types
            return "/api/v1/notifications", params
        return f"/api/v1/{name}", {"limit": limit}

    def map(self, ctx, name, item):
        if name == "statuses":
            return own_status(ctx, item)
        if name == "notifications":
            return notification(ctx, item)
        return saved_status(ctx, item, STREAMS[name])

    def emit(self, ctx, state, name, items):
        """Messages of one page (oldest first), then its links, then a checkpoint."""
        links = []
        for item in reversed(items):
            message, item_links = self.map(ctx, name, item)
            yield message
            links.extend(item_links)
        for data in links:
            yield Record(kind="link", data=data)
        yield Checkpoint(cursor=copy.deepcopy(state))

    def stream(self, api, ctx, state, name):
        """Backfill (newest first via max_id) until exhausted, then forward
        (min_id) from the newest item seen. Statuses and notifications page
        by their own ids; favourites and bookmarks page by internal ids that
        only the Link header carries, so the cursor stores Link values."""
        path, base = self.request(ctx, name)
        own_ids = name in ("statuses", "notifications")
        s = state["streams"].setdefault(name, {})
        while not s.get("done"):
            params = dict(base)
            if s.get("older"):
                params["max_id"] = s["older"]
            items, pages = api.get(path, params)
            items = items or []
            if s.get("newest") is None:
                if not items:
                    return  # nothing yet; try the full history next run
                s["newest"] = (pages.get("prev") or {}).get("min_id") or (
                    max((str(i["id"]) for i in items), key=id_key) if own_ids else None
                )
                if s["newest"] is None:
                    raise SourceError(f"{path}: no Link header to page forward from")
            older = (pages.get("next") or {}).get("max_id")
            if older is None and own_ids and items:
                older = min((str(i["id"]) for i in items), key=id_key)
            if not items or older is None or older == s.get("older"):
                s["done"] = True
                s.pop("older", None)
            else:
                s["older"] = older
            yield from self.emit(ctx, state, name, items)
        while True:
            params = dict(base, min_id=s["newest"])
            items, pages = api.get(path, params)
            items = items or []
            if not items:
                return
            newest = (pages.get("prev") or {}).get("min_id")
            if newest is None and own_ids:
                newest = max((str(i["id"]) for i in items), key=id_key)
            if newest is None or newest == s["newest"]:
                # Cannot page further forward; keep the cursor and stop.
                yield from self.emit(ctx, state, name, items)
                return
            s["newest"] = newest
            yield from self.emit(ctx, state, name, items)


def main():
    run(Mastodon)
