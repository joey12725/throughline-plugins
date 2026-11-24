"""Generate the invented Mastodon recorded-response fixture:

    python tests/recorded/generate.py tests/recorded/mastodon.json
"""
import json
import sys

HOST = "social.example"
BASE = f"https://{HOST}"
OWNER = {"id": "100", "username": "inventedowl", "acct": "inventedowl",
         "display_name": "Invented Owl", "url": f"{BASE}/@inventedowl"}
FERN = {"id": "200", "username": "fern", "acct": "fern@other.example",
        "display_name": "Invented Fern", "url": "https://other.example/@fern"}
MOSS = {"id": "300", "username": "moss", "acct": "moss",
        "display_name": "Invented Moss", "url": f"{BASE}/@moss"}
BATCH = 3


def status(sid, account, at, html, **extra):
    acct = account["acct"]
    user_host = acct.split("@")[1] if "@" in acct else HOST
    s = {
        "id": sid, "created_at": at, "in_reply_to_id": None,
        "in_reply_to_account_id": None, "sensitive": False, "spoiler_text": "",
        "visibility": "public", "language": "en",
        "uri": f"https://{user_host}/users/{account['username']}/statuses/{sid}",
        "url": f"https://{user_host}/@{account['username']}/{sid}",
        "replies_count": 0, "reblogs_count": 0, "favourites_count": 0,
        "edited_at": None, "content": html, "reblog": None,
        "application": {"name": "Invented App"}, "account": account,
        "media_attachments": [], "mentions": [], "tags": [], "emojis": [],
        "card": None, "poll": None,
    }
    s.update(extra)
    return s


fern_post = status("111000000000000050", FERN, "2023-03-01T09:00:00.000Z",
                   "<p>Invented fern thoughts about moss gardens.</p>")
moss_post = status("111000000000000060", MOSS, "2023-03-02T10:00:00.000Z",
                   "<p>Invented moss watering schedule<br>water on Tuesdays</p>")

statuses = [  # oldest first
    status("111000000000000101", OWNER, "2023-04-01T08:00:00.000Z",
           '<p>Hello invented world! Reading <a href="https://example.com/garden/article-about-owls" rel="nofollow noopener" target="_blank"><span class="invisible">https://</span><span class="ellipsis">example.com/garden/arti</span><span class="invisible">cle-about-owls</span></a> <a href="https://social.example/tags/owls" class="mention hashtag" rel="tag">#<span>owls</span></a></p>',
           tags=[{"name": "owls", "url": f"{BASE}/tags/owls"}]),
    status("111000000000000102", OWNER, "2023-04-01T09:30:00.000Z",
           '<p><span class="h-card"><a href="https://other.example/@fern" class="u-url mention">@<span>fern</span></a></span> I agree, moss gardens are invented &amp; lovely.</p>',
           in_reply_to_id=fern_post["id"], in_reply_to_account_id=FERN["id"],
           mentions=[{"id": "200", "username": "fern", "acct": "fern@other.example", "url": FERN["url"]}]),
    status("111000000000000103", OWNER, "2023-04-02T12:00:00.000Z", "",
           reblog=moss_post, uri=f"{BASE}/users/inventedowl/statuses/111000000000000103/activity",
           url=None, application=None),
    status("111000000000000104", OWNER, "2023-04-03T18:00:00.000Z",
           "<p>Invented spoiler: the owl was a cat.</p>",
           sensitive=True, spoiler_text="invented plot twist",
           media_attachments=[{"id": "1", "type": "image", "url": f"{BASE}/media/invented1.png",
                               "description": "An invented drawing of an owl in a hat"},
                              {"id": "2", "type": "video", "url": f"{BASE}/media/invented2.mp4",
                               "description": None}]),
    status("111000000000000105", OWNER, "2023-04-04T07:15:00.000Z",
           "<p>Which invented bird is best?</p>",
           poll={"id": "9", "options": [{"title": "Owl"}, {"title": "Heron"}, {"title": "Wren"}]}),
    status("111000000000000106", OWNER, "2023-04-05T21:00:00.000Z",
           '<p><span class="h-card"><a href="https://social.example/@moss" class="u-url mention">@<span>moss</span></a></span> invented private note about the meetup</p>',
           visibility="direct",
           mentions=[{"id": "300", "username": "moss", "acct": "moss", "url": MOSS["url"]}]),
    status("111000000000000107", OWNER, "2023-04-06T10:00:00.000Z",
           "<p>First invented line</p><p>Second invented paragraph with <a href=\"https://example.com/x\">a link</a></p><ul><li>one</li><li>two</li></ul>",
           edited_at="2023-04-06T10:05:00.000Z", replies_count=2, reblogs_count=1, favourites_count=4,
           card={"url": "https://example.com/card", "title": "Invented card title"}),
]

favourites = [  # (internal id, status), favourited oldest first
    ("5001", fern_post),
    ("5002", moss_post),
    ("5003", status("111000000000000070", FERN, "2023-05-01T10:00:00.000Z",
                    "<p>Invented fern post number two</p>")),
    ("5004", status("111000000000000071", MOSS, "2023-05-02T11:00:00.000Z",
                    "<p>Invented moss joke</p>", visibility="unlisted")),
    ("5005", statuses[0]),
]
bookmarks = [
    ("7001", status("111000000000000080", FERN, "2023-06-01T10:00:00.000Z",
                    '<p>Invented recipe <a href="https://example.com/recipe">https://example.com/recipe</a></p>')),
    ("7002", fern_post),
    ("7003", status("111000000000000081", MOSS, "2023-06-03T10:00:00.000Z",
                    "<p>Invented reading list</p>", language=None)),
    ("7004", status("111000000000000082", FERN, "2023-06-04T10:00:00.000Z",
                    "", media_attachments=[{"id": "3", "type": "image", "url": "https://other.example/m.png", "description": ""}])),
]

mention_status = status("111000000000000120", FERN, "2023-07-01T09:00:00.000Z",
                        '<p><span class="h-card"><a href="https://social.example/@inventedowl" class="u-url mention">@<span>inventedowl</span></a></span> invented reply to your post</p>',
                        in_reply_to_id="111000000000000101", in_reply_to_account_id="100")
dm_status = status("111000000000000121", MOSS, "2023-07-02T09:00:00.000Z",
                   '<p><span class="h-card"><a href="https://social.example/@inventedowl" class="u-url mention">@<span>inventedowl</span></a></span> invented direct message</p>',
                   visibility="direct")
notifications = [
    {"id": "9001", "type": "mention", "created_at": "2023-07-01T09:00:01.000Z", "account": FERN, "status": mention_status},
    {"id": "9002", "type": "favourite", "created_at": "2023-07-01T12:00:00.000Z", "account": MOSS, "status": statuses[0]},
    {"id": "9003", "type": "follow", "created_at": "2023-07-02T08:00:00.000Z", "account": FERN},
    {"id": "9004", "type": "mention", "created_at": "2023-07-02T09:00:01.000Z", "account": MOSS, "status": dm_status},
    {"id": "9005", "type": "reblog", "created_at": "2023-07-03T09:00:00.000Z", "account": FERN, "status": statuses[4]},
]

entries = [{"path": "/api/v1/accounts/verify_credentials", "json": OWNER}]


def link(path, nxt, prev, extra=""):
    parts = []
    if nxt:
        parts.append(f'<{BASE}{path}?limit={BATCH}{extra}&max_id={nxt}>; rel="next"')
    if prev:
        parts.append(f'<{BASE}{path}?limit={BATCH}{extra}&min_id={prev}>; rel="prev"')
    return {"Link": ", ".join(parts)} if parts else {}


def paged(path, rows, extra_params=None, extra_q=""):
    """rows: [(page id, item)] oldest first. Newest-first pages of BATCH."""
    newest_first = list(reversed(rows))
    pages = [newest_first[i:i + BATCH] for i in range(0, len(newest_first), BATCH)]
    max_id = None
    for page in pages:
        params = dict(extra_params or {}, limit=str(BATCH), min_id=None,
                      max_id=max_id if max_id else None)
        entries.append({"path": path, "params": params, "json": [i for _, i in page],
                        "headers": link(path, page[-1][0], page[0][0], extra_q)})
        max_id = page[-1][0]
    entries.append({"path": path, "params": dict(extra_params or {}, max_id=max_id), "json": []})
    entries.append({"path": path, "params": dict(extra_params or {}, min_id=newest_first[0][0]), "json": []})


paged(f"/api/v1/accounts/{OWNER['id']}/statuses", [(s["id"], s) for s in statuses])
paged("/api/v1/favourites", favourites)
paged("/api/v1/bookmarks", bookmarks)
types = ["mention", "favourite", "follow", "reblog"]
paged("/api/v1/notifications", [(n["id"], n) for n in notifications],
      {"types[]": types}, "".join(f"&types[]={t}" for t in types))

json.dump(entries, open(sys.argv[1], "w"), indent=1)
