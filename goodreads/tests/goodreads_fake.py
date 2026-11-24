"""A fake Goodreads shelf RSS endpoint over invented shelf data.

Renders recorded/goodreads_shelves.json as Goodreads list_rss XML, newest
added first, PAGE items per page; paged=False mimics a feed that ignores
the page parameter."""

import json
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.sax.saxutils import escape

import httpx

HERE = Path(__file__).resolve().parent
USER = "12345678"
KEY = "invented-goodreads-feed-key"
PAGE = 4


def load():
    return json.loads((HERE / "recorded" / "goodreads_shelves.json").read_text())


def item(book):
    cdata = lambda v: f"<![CDATA[{v}]]>"  # noqa: E731
    return f"""<item>
<guid>{cdata(f"https://www.goodreads.com/review/show/{book['review_id']}?utm_medium=api&utm_source=rss")}</guid>
<pubDate>{cdata(book['user_date_added'])}</pubDate>
<title>{escape(book['title'])}</title>
<link>{cdata(f"https://www.goodreads.com/review/show/{book['review_id']}?utm_medium=api&utm_source=rss")}</link>
<book_id>{book['book_id']}</book_id>
<book_image_url>{cdata("https://example.com/cover.jpg")}</book_image_url>
<book_description>{cdata("An invented description.")}</book_description>
<book id="{book['book_id']}"><num_pages>{book['num_pages']}</num_pages></book>
<author_name>{escape(book['author_name'])}</author_name>
<isbn>{book['isbn']}</isbn>
<user_name>{book['user_name']}</user_name>
<user_rating>{book['user_rating']}</user_rating>
<user_read_at>{cdata(book['user_read_at'])}</user_read_at>
<user_started_at>{cdata(book['user_started_at'])}</user_started_at>
<user_date_added>{cdata(book['user_date_added'])}</user_date_added>
<user_date_created>{cdata(book['user_date_added'])}</user_date_created>
<user_shelves>{escape(book['user_shelves'])}</user_shelves>
<user_review>{cdata(book['user_review'])}</user_review>
<average_rating>{book['average_rating']}</average_rating>
<book_published>{book['book_published']}</book_published>
<description>{cdata("<a href='https://example.com'>invented</a>")}</description>
</item>"""


def handler(books=None, paged=True, requests=None, key=None):
    books = load() if books is None else books

    def handle(request):
        params = dict(request.url.params)
        if requests is not None:
            requests.append(params)
        if request.url.path != f"/review/list_rss/{USER}":
            return httpx.Response(404, text="Not found")
        if key and params.get("key") != key:
            return httpx.Response(403, text="private")
        shelf = params.get("shelf", "all")
        rows = sorted(
            (b for b in books if shelf in ("all", b["shelf"])),
            key=lambda b: parsedate_to_datetime(b["user_date_added"]),
            reverse=True,
        )
        page = int(params.get("page", 1)) if paged else 1
        chunk = rows[(page - 1) * PAGE : page * PAGE]
        body = (
            '<?xml version="1.0"?><rss version="2.0"><channel>'
            f"<title>Invented Reader's bookshelf: {shelf}</title>"
            + "".join(item(b) for b in chunk)
            + "</channel></rss>"
        )
        return httpx.Response(200, content=body.encode(), headers={"Content-Type": "application/xml"})

    return handle


def transport():
    return httpx.MockTransport(handler())
