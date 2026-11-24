"""Goodreads: the owner's shelves from the public shelf RSS feeds.

Goodreads retired its public API in 2020, but every shelf still has an RSS
feed (https://www.goodreads.com/review/list_rss/<user_id>?shelf=read) with
the book, author, ISBN, the owner's rating, review, read and added dates.
Books become reference entries (`goodreads.books`) and the owner's reading
becomes events (`goodreads.reading`: finished, started, want to read).

The feeds are paged; a feed that ignores paging or stops short is detected
(a repeated page) and, when it no longer reaches the newest book delivered
before, the missing window is reported as a source_cap coverage gap.
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

BOOKS = "goodreads.books"
READING = "goodreads.reading"
BASE = "https://www.goodreads.com/review/list_rss/"
SHARED = [
    "service", "source_kind", "event_type", "media_type", "title", "creator",
    "creators", "year", "is_finished", "rating", "rating_raw", "account",
    "added_at", "native_id", "native_ids", "url", "detail", "timing_basis",
    "time_precision", "source", "author_kind", "mapping_revision",
]
EXTRA = {
    "shelf": ("text", "Exclusive shelf: read, currently-reading or to-read"),
    "shelves": ("json", "All the owner's shelves for the book, including custom ones"),
    "read_at": ("timestamp", "Date the owner finished the book (latest read only)"),
    "started_at": ("timestamp", "Date the owner started the book, when the feed has it"),
    "review": ("text", "The owner's review text, when written"),
    "num_pages": ("integer", "Page count of the edition"),
    "average_rating": ("number", "Goodreads community average rating (1-5)"),
    "isbn": ("text", "ISBN of the edition"),
}
BOOK_FIELDS = field_list(SHARED, EXTRA)
READING_FIELDS = field_list(SHARED, EXTRA)
LINK_TYPES = ["consumed", "saved", "created_by"]
EVENTS = {
    "read": ("finished", "Finished reading"),
    "currently-reading": ("progress", "Started reading"),
    "to-read": ("watchlist_add", "Added to want-to-read:"),
}


def text(item, tag):
    node = item.find(tag)
    value = (node.text or "").strip() if node is not None else ""
    return value or None


def when(value):
    try:
        return parsedate_to_datetime(value) if value else None
    except (TypeError, ValueError):
        return None


def plain(value):
    if not value:
        return None
    value = re.sub(r"<br\s*/?>", "\n", value)
    return html.unescape(re.sub(r"<[^>]+>", "", value)).strip() or None


def parse_feed(data):
    root = ET.fromstring(data)
    channel = root.find("channel")
    if channel is None:
        raise ApiError("not an RSS feed")
    return channel.findall("item")


def review_id(item):
    for value in (text(item, "guid"), text(item, "link")):
        match = re.search(r"/review/show/(\d+)", value or "")
        if match:
            return match.group(1)
    return None


class Book:
    """One shelf RSS item, parsed."""

    def __init__(self, item, shelf):
        self.item = item
        self.shelf = shelf
        self.book_id = text(item, "book_id")
        self.review_id = review_id(item)
        self.added = when(text(item, "user_date_added")) or when(text(item, "pubDate"))
        self.read_at = when(text(item, "user_read_at"))
        self.started_at = when(text(item, "user_started_at"))
        self.payload = {
            child.tag: (child.text or "").strip()
            for child in item
            if child.tag not in ("book_description", "description")
            and not child.tag.endswith("image_url")
        }
        pages = item.find("book/num_pages")
        if pages is not None and pages.text:
            self.payload["num_pages"] = pages.text.strip()

    @property
    def key(self):
        return self.book_id or self.review_id


def book_fields(declared, book, user, **values):
    item = book.item
    author = text(item, "author_name")
    rating = integer(text(item, "user_rating")) or None
    shelves = [s.strip() for s in (text(item, "user_shelves") or "").split(",") if s.strip()]
    if book.shelf not in shelves:
        shelves.insert(0, book.shelf)
    isbn = text(item, "isbn")
    pages = item.find("book/num_pages")
    return fields_for(
        declared,
        service="goodreads",
        media_type="book",
        title=text(item, "title") or "Unknown book",
        creator=author,
        creators=[author] if author else None,
        year=integer(text(item, "book_published")),
        is_finished=book.shelf == "read",
        rating=rating * 2 if rating else None,
        rating_raw=f"{rating}/5" if rating else None,
        account=user,
        added_at=iso(book.added),
        native_id=f"goodreads:book:{book.book_id}" if book.book_id else None,
        native_ids=compact(
            {"goodreads_book_id": book.book_id, "goodreads_review_id": book.review_id, "isbn": isbn}
        )
        or None,
        url=f"https://www.goodreads.com/book/show/{book.book_id}" if book.book_id else text(item, "link"),
        source="rss",
        author_kind="owner",
        mapping_revision=1,
        shelf=book.shelf,
        shelves=shelves,
        read_at=iso(book.read_at),
        started_at=iso(book.started_at),
        review=plain(text(item, "user_review")),
        num_pages=integer(pages.text) if pages is not None else None,
        average_rating=number(text(item, "average_rating")),
        isbn=isbn,
        **values,
    )


def owner(user, item):
    return {
        "system": "goodreads",
        "kind": "user_id",
        "value": user,
        "display_name": text(item, "user_name") or user,
    }


def work_ref(book, fields):
    return {
        "kind": "work",
        "external_ref": f"goodreads:book:{book.key}",
        "name": fields["title"],
    }


def map_book(book, user):
    """(books message, reading message, links) for one shelf item."""
    if not book.key or book.added is None:
        return None
    fields = book_fields(
        BOOK_FIELDS, book, user,
        source_kind="shelf_entry",
        event_type="shelved",
        timing_basis="When the book was first added to the owner's shelves",
        time_precision="source_mark",
    )
    by = f" by {fields['creator']}" if fields["creator"] else ""
    year = f" ({fields['year']})" if fields["year"] else ""
    rated = f", rated {fields['rating_raw']}" if fields["rating_raw"] else ""
    entry = {
        "channel": BOOKS,
        "external_id": f"goodreads:{user}:book:{book.key}",
        "thread": {"external_id": f"goodreads:{user}:books", "title": "Goodreads books"},
        "happened_at": book.added.isoformat(),
        "author": owner(user, book.item),
        "content": f"{fields['title']}{by}{year} - on {', '.join(fields['shelves'])}{rated}",
        "fields": fields,
        "payload": book.payload,
    }
    event_type, verb = EVENTS[book.shelf]
    if book.shelf == "read" and book.read_at:
        at, basis, precision = book.read_at, "Date read (Goodreads keeps the latest read only)", "day"
    elif book.shelf == "currently-reading" and book.started_at:
        at, basis, precision = book.started_at, "Date started", "day"
    else:
        at = book.added
        basis = {
            "read": "No read date recorded: when the book was added to the shelves",
            "currently-reading": "No start date in the feed: when the book was added to the shelves",
            "to-read": "When the book was added to the shelves",
        }[book.shelf]
        precision = "source_mark"
    event_fields = book_fields(
        READING_FIELDS, book, user,
        source_kind="read" if book.shelf == "read" else "shelf",
        event_type=event_type,
        timing_basis=basis,
        time_precision=precision,
    )
    content = f"{verb} {fields['title']}{by}{rated}"
    if event_fields["review"]:
        content += "\n\n" + event_fields["review"]
    event = {
        "channel": READING,
        "external_id": f"goodreads:{user}:{event_type}:{book.review_id or book.key}",
        "thread": {"external_id": f"goodreads:{user}:book:{book.key}", "title": fields["title"]},
        "happened_at": at.isoformat(),
        "author": owner(user, book.item),
        "content": content,
        "fields": event_fields,
        "payload": book.payload,
    }
    work = work_ref(book, fields)
    links = [link("saved", {"channel": BOOKS, "external_id": entry["external_id"]}, work)]
    if book.shelf == "read":
        links.append(link("consumed", {"channel": READING, "external_id": event["external_id"]}, work))
    if fields["creator"]:
        links.append(
            link(
                "created_by",
                work,
                {
                    "kind": "author",
                    "external_ref": "goodreads:author:" + fields["creator"].casefold(),
                    "name": fields["creator"],
                },
            )
        )
    return entry, event, links


class Goodreads(Plugin):
    manifest = Manifest(
        name="goodreads",
        version="0.1.0",
        channels=[
            Channel(
                name=BOOKS,
                description="Books on the owner's Goodreads shelves (read, currently reading, want to read)",
                fields=BOOK_FIELDS,
                is_reference=True,
                record_kind="reference",
            ),
            Channel(
                name=READING,
                description=(
                    "The owner's reading on Goodreads: books finished (with rating and "
                    "review), started and added to want-to-read"
                ),
                fields=READING_FIELDS,
                record_kind="life_event",
                conversation_gap_s=86400,
            ),
        ],
        settings={
            "user_id": Declaration(
                env="GOODREADS_USER_ID",
                description="Numeric Goodreads user ID (from the profile URL)",
            ),
            "shelves": Declaration(
                env="GOODREADS_SHELVES", required=False, default="read,currently-reading,to-read",
                description="Comma-separated exclusive shelves to read",
            ),
            "feed_url": Declaration(
                env="GOODREADS_FEED_URL", required=False, default=BASE,
                description="Base of the shelf RSS URL (the user ID is appended)",
            ),
            "max_pages": Declaration(
                env="GOODREADS_MAX_PAGES", required=False, default="50",
                description="Most RSS pages read per shelf per run",
            ),
            "feed_cap": Declaration(
                env="GOODREADS_FEED_CAP", required=False, default="100",
                description="Items per RSS page; a full unpaged feed may hide older books",
            ),
            "batch_size": Declaration(
                env="GOODREADS_BATCH_SIZE", required=False, default="100",
                description="Books per checkpoint",
            ),
            "delay_s": Declaration(
                env="GOODREADS_REQUEST_DELAY_S", required=False, default="1",
                description="Pause between feed requests",
            ),
            "test_transport": Declaration(
                env="GOODREADS_TEST_TRANSPORT", required=False,
                description="Testing only: module:factory returning an httpx transport",
            ),
        },
        secrets={
            "key": Declaration(
                env="GOODREADS_FEED_KEY", required=False,
                description="The key= value from a private profile's RSS link (optional)",
            ),
        },
        run_mode="poll",
        default_interval_s=6 * 3600,
        link_types=LINK_TYPES,
    )
    transport = None

    def shelf(self, http, user, shelf, mode, high_water):
        """All books of one shelf feed (newest pages first), and whether the
        feed stopped short of the previous high-water mark."""
        books, seen = [], set()
        max_pages = max(1, int(self.settings.get("max_pages") or 50))
        cap = int(self.settings.get("feed_cap") or 100)
        params = {"shelf": shelf}
        if self.secrets.get("key"):
            params["key"] = self.secrets["key"]
        capped = False
        for page in range(1, max_pages + 1):
            response = http.request("GET", user, params=dict(params, page=page))
            if response.status_code == 404:
                raise ApiError("Goodreads feed not found: check GOODREADS_USER_ID")
            if response.status_code >= 400:
                raise ApiError(f"HTTP {response.status_code} from Goodreads")
            items = [Book(i, shelf) for i in parse_feed(response.content)]
            fresh = [b for b in items if (b.review_id or b.key) not in seen]
            if not fresh:
                # Empty page, or a feed that ignores paging and repeats page 1.
                capped = page == 2 and len(items) >= cap
                break
            seen.update(b.review_id or b.key for b in fresh)
            books.extend(fresh)
            if len(items) < cap:
                break
            oldest = min((b.added for b in fresh if b.added), default=None)
            if mode != "backfill" and high_water and oldest and iso(oldest) < high_water:
                break
        else:
            capped = True
        return books, capped

    def records(self, cursor, mode):
        user = self.settings["user_id"].strip()
        shelves = [s.strip() for s in (self.settings.get("shelves") or "").split(",") if s.strip()]
        unknown = set(shelves) - set(EVENTS)
        if unknown:
            raise ValueError("GOODREADS_SHELVES supports read, currently-reading, to-read")
        batch = max(1, int(self.settings.get("batch_size") or 100))
        http = Http(
            self.settings.get("feed_url") or BASE,
            transport=self.transport or load_transport(self.settings.get("test_transport")),
            delay_s=float(self.settings.get("delay_s") or 0),
        )
        cursor = dict(cursor)
        try:
            for shelf in shelves:
                marks = dict(cursor.get("high_water") or {})
                high_water = marks.get(shelf)
                books, capped = self.shelf(http, user, shelf, mode, high_water)
                mapped = [m for m in (map_book(b, user) for b in books) if m]
                mapped.sort(key=lambda m: (m[0]["happened_at"], m[0]["external_id"]))
                if capped and mapped:
                    oldest = min(iso(b.added) for b in books if b.added)
                    if high_water and oldest > high_water:
                        yield coverage(
                            channel=READING,
                            scope_key=shelf,
                            status="partial",
                            reasons=["source_cap"],
                            gaps=[{"from": high_water, "to": oldest, "reason": "source_cap"}],
                            detail={"source": "rss", "shelf": shelf, "items": len(books)},
                        )
                    elif not high_water:
                        yield coverage(
                            channel=READING,
                            scope_key=shelf,
                            status="best_effort",
                            reasons=["source_cap"],
                            detail={
                                "source": "rss",
                                "shelf": shelf,
                                "note": "the feed stopped before the start of the shelf",
                                "oldest_available": oldest,
                            },
                        )
                newest = high_water
                for index, (entry, event, links) in enumerate(mapped, 1):
                    yield entry
                    yield event
                    for data in links:
                        yield Record(kind="link", data=data)
                    added = entry["fields"]["added_at"]
                    newest = max(newest or "", added)
                    if index % batch == 0 or index == len(mapped):
                        marks[shelf] = newest
                        cursor["high_water"] = dict(marks)
                        yield Checkpoint(cursor=dict(cursor))
        finally:
            http.close()


def main():
    run(Goodreads)
