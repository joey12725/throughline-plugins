"""Letterboxd: the owner's film diary from the public RSS feed.

Letterboxd has no public API for personal data, but every member has an RSS
feed (https://letterboxd.com/<user>/rss/) with their most recent diary
entries: film, watched date, rating, like, rewatch flag and review text.
The feed only holds the newest entries (about 50). Every run re-reads it
(edited reviews arrive as new versions) and, when the feed no longer
reaches back to the newest entry delivered before, reports the window in
between as a source_cap coverage gap.
"""

import html
import logging
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

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

from .common import (
    ApiError,
    Http,
    compact,
    field_list,
    fields_for,
    integer,
    iso,
    link,
    load_transport,
    number,
)

log = logging.getLogger(__name__)

DIARY = "letterboxd.diary"
NS = {
    "letterboxd": "https://letterboxd.com",
    "tmdb": "https://themoviedb.org",
    "dc": "http://purl.org/dc/elements/1.1/",
}
SHARED = [
    "service", "source_kind", "event_type", "media_type", "title", "year",
    "rating", "rating_raw", "account", "native_id", "native_ids", "url", "detail",
    "timing_basis", "time_precision", "source", "author_kind", "mapping_revision",
]
EXTRA = {
    "watched_date": ("text", "Diary watched date (YYYY-MM-DD) as logged"),
    "logged_at": ("timestamp", "When the diary entry or review was published"),
    "is_rewatch": ("boolean", "The owner marked the entry as a rewatch"),
    "liked": ("boolean", "The owner liked the film"),
    "review": ("text", "The owner's review text, when written"),
    "has_spoilers": ("boolean", "The review is marked as containing spoilers"),
}
DIARY_FIELDS = field_list(SHARED, EXTRA)
LINK_TYPES = ["consumed", "liked"]


def text(item, tag):
    node = item.find(tag, NS)
    value = (node.text or "").strip() if node is not None else ""
    return value or None


def yes(value):
    return {"yes": True, "no": False}.get((value or "").strip().lower())


def plain(description):
    """Review text from the item HTML: drop the poster and the boilerplate
    "Watched on ..." line Letterboxd writes for entries without a review."""
    if not description:
        return None
    body = re.sub(r"<img[^>]*>", "", description)
    paragraphs = [
        html.unescape(re.sub(r"<[^>]+>", "", p)).strip()
        for p in re.split(r"</p>\s*|<br\s*/?>", body)
    ]
    kept = [
        p
        for p in paragraphs
        if p
        and not re.fullmatch(r"(Watched|Rewatched|Added) on .*\.", p)
        and p != "This review may contain spoilers."
    ]
    return "\n\n".join(kept) or None


def stars(rating):
    full = int(rating)
    return "★" * full + ("½" if rating - full >= 0.5 else "")


def parse_feed(data):
    root = ET.fromstring(data)
    channel = root.find("channel")
    if channel is None:
        raise ApiError("not an RSS feed")
    return channel.findall("item")


def map_item(item, user):
    """One RSS <item> to (message, links), or None for non-diary items
    (lists, plain likes) that have no film or date."""
    guid = text(item, "guid") or ""
    film = text(item, "letterboxd:filmTitle")
    published = text(item, "pubDate")
    if not film or not published or "letterboxd-list" in guid:
        return None
    logged = parsedate_to_datetime(published)
    watched = text(item, "letterboxd:watchedDate")
    year = integer(text(item, "letterboxd:filmYear"))
    rating = number(text(item, "letterboxd:memberRating"))
    liked = yes(text(item, "letterboxd:memberLike"))
    rewatch = yes(text(item, "letterboxd:rewatch"))
    description = text(item, "description") or ""
    review = plain(description) if "letterboxd-review" in guid else None
    tmdb = text(item, "tmdb:movieId") or text(item, "tmdb:tvId")
    url = text(item, "link")
    if watched:
        # A diary date has no time of day: noon in the zone of the log time.
        at = logged.replace(
            year=int(watched[:4]), month=int(watched[5:7]), day=int(watched[8:10]),
            hour=12, minute=0, second=0, microsecond=0,
        )
        precision, basis = "day", "Diary watched date (no time of day); noon in the logging time zone"
    else:
        at, precision, basis = logged, "source_mark", "When the entry was published (no watched date)"
    fields = fields_for(
        DIARY_FIELDS,
        service="letterboxd",
        source_kind="review" if "letterboxd-review" in guid else "diary",
        event_type="play" if watched else "reviewed",
        media_type="movie",
        title=film,
        year=year,
        rating=rating * 2 if rating is not None else None,
        rating_raw=f"{rating:g}/5" if rating is not None else None,
        account=user,
        native_id=f"tmdb:movie:{tmdb}" if tmdb else url,
        native_ids=compact({"tmdb": tmdb, "letterboxd_guid": guid or None}) or None,
        url=url,
        timing_basis=basis,
        time_precision=precision,
        source="rss",
        author_kind="owner",
        mapping_revision=1,
        watched_date=watched,
        logged_at=iso(logged),
        is_rewatch=rewatch,
        liked=liked,
        review=review,
        has_spoilers=("(contains spoilers)" in (text(item, "title") or "")) or None,
    )
    verb = "Rewatched" if rewatch else "Watched"
    head = f"{verb} {film}" + (f" ({year})" if year else "")
    extras = []
    if rating is not None:
        extras.append(f"rated {stars(rating)} ({rating:g}/5)")
    if liked:
        extras.append("liked")
    content = head + (" - " + ", ".join(extras) if extras else "")
    if not watched:
        content = "Reviewed " + head.split(" ", 1)[1] + (" - " + ", ".join(extras) if extras else "")
    if review:
        content += "\n\n" + review
    day = at.date().isoformat()
    key = guid or url
    message = {
        "channel": DIARY,
        "external_id": f"letterboxd:{user.casefold()}:{key}",
        "thread": {
            "external_id": f"letterboxd:{user.casefold()}:day:{day}",
            "title": f"Letterboxd {day}",
        },
        "happened_at": at.isoformat(),
        "author": {
            "system": "letterboxd",
            "kind": "account",
            "value": user,
            "display_name": text(item, "dc:creator") or user,
        },
        "content": content,
        "fields": fields,
        "payload": {
            "guid": guid,
            "title": text(item, "title"),
            "link": url,
            "pubDate": published,
            "watchedDate": watched,
            "rewatch": text(item, "letterboxd:rewatch"),
            "filmTitle": film,
            "filmYear": text(item, "letterboxd:filmYear"),
            "memberRating": text(item, "letterboxd:memberRating"),
            "memberLike": text(item, "letterboxd:memberLike"),
            "tmdb": tmdb,
            "description": description,
        },
    }
    source = {"channel": DIARY, "external_id": message["external_id"]}
    work = {
        "kind": "work",
        "external_ref": f"tmdb:movie:{tmdb}" if tmdb else f"letterboxd:film:{url}",
        "name": film + (f" ({year})" if year else ""),
    }
    links = [link("consumed", source, work)] if watched else []
    if liked:
        links.append(link("liked", source, work))
    return message, logged, links


class Letterboxd(Plugin):
    manifest = Manifest(
        name="letterboxd",
        version="0.1.0",
        channels=[
            Channel(
                name=DIARY,
                description=(
                    "Films the owner logged on Letterboxd: watched date, rating, like, "
                    "rewatch and review text (from the public RSS feed)"
                ),
                fields=DIARY_FIELDS,
                record_kind="life_event",
                conversation_gap_s=6 * 3600,
            )
        ],
        settings={
            "user": Declaration(env="LETTERBOXD_USER", description="Letterboxd user name"),
            "feed_url": Declaration(
                env="LETTERBOXD_FEED_URL", required=False,
                description="Override the feed URL (default https://letterboxd.com/<user>/rss/)",
            ),
            "feed_cap": Declaration(
                env="LETTERBOXD_FEED_CAP", required=False, default="50",
                description="Entries the feed holds at most; a full feed may hide older entries",
            ),
            "batch_size": Declaration(
                env="LETTERBOXD_BATCH_SIZE", required=False, default="50",
                description="Entries per checkpoint",
            ),
            "test_transport": Declaration(
                env="LETTERBOXD_TEST_TRANSPORT", required=False,
                description="Testing only: module:factory returning an httpx transport",
            ),
        },
        run_mode="poll",
        default_interval_s=3600,
        link_types=LINK_TYPES,
    )
    transport = None

    def fetch(self, user):
        url = self.settings.get("feed_url") or f"https://letterboxd.com/{user}/rss/"
        http = Http(
            url,
            transport=self.transport or load_transport(self.settings.get("test_transport")),
            headers={"Accept": "application/rss+xml, application/xml"},
        )
        try:
            response = http.request("GET", "")
            if response.status_code == 404:
                raise ApiError("Letterboxd feed not found: check LETTERBOXD_USER")
            if response.status_code >= 400:
                raise ApiError(f"HTTP {response.status_code} from Letterboxd")
            return response.content
        finally:
            http.close()

    def records(self, cursor, mode):
        user = self.settings["user"].strip()
        items = parse_feed(self.fetch(user))
        mapped = [m for m in (map_item(i, user) for i in items) if m]
        mapped.sort(key=lambda m: (m[1], m[0]["external_id"]))
        cap = int(self.settings.get("feed_cap") or 50)
        batch = max(1, int(self.settings.get("batch_size") or 50))
        high_water = cursor.get("high_water")
        if mapped and len(items) >= cap:
            oldest = mapped[0][1]
            if high_water and iso(oldest) > high_water:
                # The feed is full and starts after the newest entry we had:
                # anything logged in between has scrolled out of the feed.
                yield coverage(
                    channel=DIARY,
                    scope_key=user.casefold(),
                    status="partial",
                    reasons=["source_cap"],
                    gaps=[{"from": high_water, "to": iso(oldest), "reason": "source_cap"}],
                    detail={"source": "rss", "feed_items": len(items)},
                )
            elif not high_water:
                yield coverage(
                    channel=DIARY,
                    scope_key=user.casefold(),
                    status="best_effort",
                    reasons=["source_cap"],
                    detail={
                        "source": "rss",
                        "note": "the RSS feed holds only the newest entries; "
                        "diary entries logged before this are not available",
                        "oldest_available": iso(oldest),
                    },
                )
        newest = high_water
        for index, (message, logged, links) in enumerate(mapped, 1):
            yield message
            for data in links:
                yield Record(kind="link", data=data)
            newest = max(newest or "", iso(logged))
            if index % batch == 0 or index == len(mapped):
                yield Checkpoint(cursor=dict(cursor, high_water=newest))


def main():
    run(Letterboxd)
