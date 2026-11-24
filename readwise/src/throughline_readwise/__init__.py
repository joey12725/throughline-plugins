"""Readwise: the owner's highlights and notes (export API v2) and the
documents they save to Reader (Reader API v3).

Both APIs page with an opaque ``pageCursor`` and filter with
``updatedAfter``, but pages are not ordered by update time. The high-water
mark (newest ``updated_at`` seen) therefore only advances when a pass has
read its last page; until then the cursor holds the pass's ``updatedAfter``
and the next ``pageCursor``, so a crash resumes mid-pass.

An incremental export page only carries the highlights that changed, so the
books it names are re-read in full (``ids=``) before mapping. That keeps a
book's entry, its highlight count and its document-note date identical to a
full export.
"""

import logging
from datetime import timedelta

from throughline_sdk import Channel, Checkpoint, Declaration, Manifest, Plugin, Record, run

from .common import ApiError, Http, iso, load_transport, parse_time
from .mapping import (
    BOOK_FIELDS,
    BOOKS,
    HIGHLIGHT_FIELDS,
    HIGHLIGHTS,
    LINK_TYPES,
    READER,
    READER_FIELDS,
    map_book,
    map_document,
)

log = logging.getLogger(__name__)

API = "https://readwise.io/api/"
# Re-read updates this far behind the high-water mark: an item updated on an
# already-read page while a pass runs can be older than the newest item seen.
OVERLAP = timedelta(minutes=10)


def flag(value, default=True):
    if value in (None, ""):
        return default
    return str(value).strip().lower() not in ("false", "0", "no", "off")


class ReadwiseApi:
    def __init__(self, token, transport=None, delay_s=3.0):
        self.http = Http(
            API, headers={"Authorization": f"Token {token}"}, transport=transport, delay_s=delay_s
        )

    def get(self, path, params):
        response = self.http.request("GET", path, params={k: v for k, v in params.items() if v})
        if response.status_code in (401, 403):
            raise ApiError(f"Readwise rejected the token (HTTP {response.status_code}); check READWISE_TOKEN")
        if response.status_code >= 400:
            raise ApiError(f"HTTP {response.status_code} from Readwise {path}")
        return response.json()

    def export(self, updated_after=None, page=None, ids=None):
        return self.get("v2/export/", {"updatedAfter": updated_after, "pageCursor": page, "ids": ids})

    def books(self, ids):
        """Every highlight of these books (all pages)."""
        out, page = [], None
        while True:
            data = self.export(page=page, ids=",".join(str(i) for i in ids))
            out.extend(data.get("results") or [])
            page = data.get("nextPageCursor")
            if not page:
                return out

    def reader(self, updated_after=None, page=None):
        return self.get("v3/list/", {"updatedAfter": updated_after, "pageCursor": page})


def newest(values, start=None):
    best = parse_time(start)
    for value in values:
        moment = parse_time(value)
        if moment and (best is None or moment > best):
            best = moment
    return iso(best)


def chunks(items, size):
    for start in range(0, len(items), size):
        yield items[start : start + size]


class Readwise(Plugin):
    manifest = Manifest(
        name="readwise",
        version="0.1.0",
        channels=[
            Channel(
                name=HIGHLIGHTS,
                description=(
                    "Passages the owner highlighted (author's words, author_kind other, "
                    "selected_by owner) and the owner's own notes on them and on whole "
                    "books (entry_type note, author_kind owner); one thread per book"
                ),
                fields=HIGHLIGHT_FIELDS,
                conversation_gap_s=3600,
                record_kind="life_event",
                embed=True,
            ),
            Channel(
                name=BOOKS,
                description="Books, articles, tweets and podcasts the owner has highlights from",
                fields=BOOK_FIELDS,
                is_reference=True,
                record_kind="reference",
                embed=False,
            ),
            Channel(
                name=READER,
                description="Documents the owner saved to Readwise Reader, with reading progress",
                fields=READER_FIELDS,
                conversation_gap_s=3600,
                record_kind="life_event",
                embed=False,
            ),
        ],
        settings={
            "reader": Declaration(
                env="READWISE_READER", required=False, default="true",
                description="Also import Reader documents (true/false; needs a Reader subscription)",
            ),
            "reader_feed": Declaration(
                env="READWISE_READER_FEED", required=False, default="false",
                description="Include unsaved RSS/email items in Reader's feed (true/false)",
            ),
            "page_size": Declaration(
                env="READWISE_PAGE_SIZE", required=False, default="100",
                description="Books or documents per checkpointed batch",
            ),
            "delay_s": Declaration(
                env="READWISE_REQUEST_DELAY_S", required=False, default="3",
                description="Pause between API requests (export and Reader list allow 20/min)",
            ),
            "owner_identifier": Declaration(
                env="READWISE_OWNER_IDENTIFIER", required=False, default="readwise:account:owner",
                description="system:kind:value author of the owner's notes",
            ),
            "owner_name": Declaration(
                env="READWISE_OWNER_NAME", required=False,
                description="Display name on the owner's notes",
            ),
            "test_transport": Declaration(
                env="READWISE_TEST_TRANSPORT", required=False,
                description="Testing only: module:factory returning an httpx transport",
            ),
        },
        secrets={
            "token": Declaration(
                env="READWISE_TOKEN", description="Readwise access token (readwise.io/access_token)"
            ),
        },
        run_mode="poll",
        default_interval_s=3600,
        link_types=LINK_TYPES,
    )
    transport = None

    def map_settings(self):
        settings = {
            "owner_identifier": self.settings.get("owner_identifier") or "readwise:account:owner",
            "owner_name": self.settings.get("owner_name"),
        }
        if len(settings["owner_identifier"].split(":", 2)) != 3:
            raise ValueError("READWISE_OWNER_IDENTIFIER must look like system:kind:value")
        return settings

    def api(self):
        return ReadwiseApi(
            self.secrets["token"],
            transport=self.transport or load_transport(self.settings.get("test_transport")),
            delay_s=float(self.settings.get("delay_s") or 0),
        )

    def batch_size(self):
        return max(1, int(self.settings.get("page_size") or 100))

    def records(self, cursor, mode):
        settings = self.map_settings()
        api = self.api()
        try:
            cursor = yield from self.export(api, dict(cursor), settings)
            if flag(self.settings.get("reader")):
                yield from self.reader(api, cursor, settings)
        finally:
            api.http.close()

    def passes(self, cursor, key, fetch, map_page):
        """Shared pass/resume logic for both endpoints.

        cursor[key] = {"since": high-water of the last complete pass} or, mid-pass,
        {"since", "pass_since", "page", "pass_max"}. Within a page, batches
        checkpoint the page's own start state, so a crash re-reads that page."""
        state = dict(cursor.get(key) or {})
        if "page" in state:
            pass_since, page, pass_max = state.get("pass_since"), state["page"], state.get("pass_max")
        else:
            pass_since, page, pass_max = state.get("since"), None, state.get("since")
        since = state.get("since")
        updated_after = iso(parse_time(pass_since) - OVERLAP) if pass_since else None
        while True:
            data = fetch(updated_after, page)
            results = data.get("results") or []
            here = dict(cursor, **{key: compact_state(since, pass_since, page, pass_max)})
            stamps = []
            for batch in chunks(results, self.batch_size()):
                for item in map_page(batch, updated_after, stamps):
                    yield item
                yield Checkpoint(cursor=here)
            pass_max = newest(stamps, pass_max)
            page = data.get("nextPageCursor")
            if not page:
                cursor = dict(cursor, **{key: {"since": pass_max}})
                yield Checkpoint(cursor=cursor)
                return cursor
            cursor = dict(cursor, **{key: compact_state(since, pass_since, page, pass_max)})
            yield Checkpoint(cursor=cursor)

    def export(self, api, cursor, settings):
        def map_page(books, updated_after, stamps):
            for book in books:
                stamps.extend(h.get("updated_at") for h in book.get("highlights") or [])
            if updated_after:
                # Incremental pages list only changed highlights: re-read the books.
                books = api.books([b["user_book_id"] for b in books])
            for book in books:
                messages, links = map_book(book, settings)
                yield from messages
                for data in links:
                    yield Record(kind="link", data=data)

        return (yield from self.passes(cursor, "export", api.export, map_page))

    def reader(self, api, cursor, settings):
        feed = flag(self.settings.get("reader_feed"), default=False)

        def map_page(docs, updated_after, stamps):
            for doc in docs:
                stamps.append(doc.get("updated_at"))
                if doc.get("location") == "feed" and not feed:
                    continue
                mapped = map_document(doc, settings)
                if mapped is None:
                    continue
                message, links = mapped
                yield message
                for data in links:
                    yield Record(kind="link", data=data)

        return (yield from self.passes(cursor, "reader", api.reader, map_page))


def compact_state(since, pass_since, page, pass_max):
    return {"since": since, "pass_since": pass_since, "page": page, "pass_max": pass_max}


def main():
    run(Readwise)
