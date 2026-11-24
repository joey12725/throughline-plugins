"""Takeout readers. No metadata lookups; unknown local formats fail explicitly."""

import csv
import io
import json
import re
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlparse

from dateutil.parser import parse


def timestamp(value, date_format=None):
    if not value:
        return None
    value = value.strip().replace("\u202f", " ").replace("\u00a0", " ")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        if date_format:
            dt = datetime.strptime(value, date_format)
        else:
            # Restrict free-form parsing to unambiguous English month/date + UTC
            # or numeric offset. Never infer the machine's timezone or day order.
            if not re.search(r"[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}", value):
                raise ValueError("Unsupported Takeout date; set YOUTUBE_DATE_FORMAT")
            if not re.search(r"(?:UTC|GMT|[+-]\d{2}:?\d{2})$", value):
                raise ValueError("Takeout date needs an explicit numeric offset or UTC")
            dt = parse(value)
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("Takeout date has no timezone; use a format including %z")
    return dt.isoformat()


def video_id(url):
    p = urlparse(url or "")
    if p.hostname == "youtu.be":
        return p.path.strip("/").split("/")[0] or None
    if p.hostname not in {
        "youtube.com",
        "www.youtube.com",
        "music.youtube.com",
        "m.youtube.com",
    }:
        return None
    if p.path.startswith(("/shorts/", "/embed/")):
        return p.path.split("/")[2]
    return parse_qs(p.query).get("v", [None])[0]


def channel_id(url):
    p = urlparse(url or "")
    return p.path.split("/")[2] if p.path.startswith("/channel/") else None


def watch_json(text, date_format=None):
    rows = json.loads(text)
    if not isinstance(rows, list):
        raise ValueError("watch-history.json must contain an array")
    for row in rows:
        url = row.get("titleUrl", "")
        # Search activity isn't a view. Deleted video views with a watch title
        # but no URL remain useful and are retained without a work link.
        if "/results?" in url or row.get("header", "").lower() == "search":
            continue
        title = row.get("title", "Unavailable video")
        if title.startswith("Watched "):
            title = title[8:]
        subs = row.get("subtitles") or []
        channel = subs[0] if subs else {}
        flags = {
            k: row[k]
            for k in ("is_short", "is_music", "is_ad")
            if isinstance(row.get(k), bool)
        }
        if "/shorts/" in url:
            flags["is_short"] = True
        if urlparse(url).hostname == "music.youtube.com":
            flags["is_music"] = True
        if any("google ads" in str(d).lower() for d in row.get("details", [])):
            flags["is_ad"] = True
        yield dict(
            video_id=video_id(url),
            title=title,
            channel=channel.get("name"),
            channel_id=channel_id(channel.get("url")),
            url=url or None,
            at=timestamp(row.get("time"), date_format),
            payload=row,
            **flags,
        )


class HistoryHTML(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.blocks = []
        self.depth = 0
        self.block = []
        self.text = text
        self.offsets = [0]
        for line in text.splitlines(keepends=True):
            self.offsets.append(self.offsets[-1] + len(line))

    def source_offset(self):
        line, col = self.getpos()
        return self.offsets[line - 1] + col

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "div" and (
            self.depth or "content-cell" in attrs.get("class", "").split()
        ):
            if not self.depth:
                self.start = self.source_offset()
            self.depth += 1
        if self.depth:
            self.block.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        if self.depth:
            self.block.append(f"</{tag}>")
            if tag == "div":
                self.depth -= 1
                if not self.depth:
                    self.blocks.append(
                        self.text[self.start : self.source_offset() + len("</div>")]
                    )
                    self.block = []

    def handle_data(self, data):
        if self.depth:
            from html import escape

            self.block.append(escape(data))


class Cell(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.parts = []
        self.active = None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.active = [dict(attrs).get("href", ""), ""]
        if tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "a" and self.active:
            self.links.append(self.active)
            self.active = None

    def handle_data(self, data):
        self.parts.append(data)
        if self.active is not None:
            self.active[1] += data


def watch_html(text, date_format=None):
    parser = HistoryHTML(text)
    parser.feed(text)
    for block in parser.blocks:
        cell = Cell()
        cell.feed(block)
        video = next((x for x in cell.links if video_id(x[0])), None)
        lines = [x.strip() for x in "".join(cell.parts).splitlines() if x.strip()]
        if not video and not any("Watched" in x for x in lines):
            continue
        at = None
        for line in reversed(lines):
            try:
                at = timestamp(line, date_format)
                break
            except (ValueError, OverflowError):
                pass
        if at is None:
            raise ValueError(
                "Cannot parse HTML watch time; prefer JSON or set YOUTUBE_DATE_FORMAT"
            )
        ch = next((x for x in cell.links if channel_id(x[0])), [None, None])
        yield dict(
            video_id=video_id(video[0]) if video else None,
            title=video[1] if video else "Unavailable video",
            channel=ch[1],
            channel_id=channel_id(ch[0]),
            url=video[0] if video else None,
            at=at,
            payload={"html": block},
        )


ALIASES = {
    "video id": "video_id",
    "video title": "title",
    "title": "title",
    "channel id": "channel_id",
    "channel title": "channel",
    "channel name": "channel",
    "channel url": "url",
    "video url": "url",
    "url": "url",
    "time added": "at",
    "added at": "at",
    "subscription date": "at",
    "position": "position",
}


def csv_rows(text, aliases=None, date_format=None):
    """Support metadata preambles, BOM, quoted commas and explicit translations.

    Yield normalized columns plus the untouched original header/value mapping.
    """
    aliases = {
        **ALIASES,
        **{k.casefold().strip(): v for k, v in (aliases or {}).items()},
    }
    rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
    header = None
    for row in rows:
        keys = [aliases.get(k.strip().casefold()) for k in row]
        if "video_id" in keys or ("channel_id" in keys and "channel" in keys):
            header = row
            continue
        if not header or not any(row):
            continue
        if len(row) != len(header):
            raise ValueError("Malformed Takeout CSV row")
        raw = dict(zip(header, row, strict=True))
        normalized = {
            aliases[k.strip().casefold()]: v.strip()
            for k, v in raw.items()
            if k.strip().casefold() in aliases
        }
        normalized["at"] = timestamp(normalized.get("at"), date_format)
        normalized["payload"] = raw
        yield normalized
    if header is None:
        raise ValueError(
            "Unknown Takeout CSV headers; set YOUTUBE_CSV_HEADERS with translations"
        )
