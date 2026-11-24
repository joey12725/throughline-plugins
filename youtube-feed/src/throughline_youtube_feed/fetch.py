#!/usr/bin/env python3
"""Standard-library-only YouTube subscription feed fetcher.

Part of throughline-youtube-feed, but self-contained: copy this one file to any
host with Python 3.12+ and run

    python fetch.py export --out feed.jsonl --channels subscriptions.opml

It reads public channel feeds (RSS, or the public channel videos page as a
fallback), optionally scores new uploads with an OpenAI-compatible model (the
"router") and optionally adds picks to a YouTube playlist (dry run unless
--apply). It writes raw records as JSON lines; the Throughline plugin maps them.

Every record has `kind`, a stable native `id` and the raw source data:

- `upload`: one feed entry (`transport` rss, page or prediction).
- `pick`: one scoring decision (`origin` router, predictions or flagged).
- `playlist_add`: a video added to a playlist by this tool.
- `feed_error`: a channel whose feed could not be read in this run.
- `feed_gap`: a full RSS feed whose oldest entry is newer than the newest one
  seen before: the uploads in between are past the feed's window (`from`, `to`).
- `run`: a summary of the run (not mapped to messages).

Credentials come only from environment variables (never argv), are sent only
in headers or POST bodies and are never written or printed:
YOUTUBE_FEED_ROUTER_API_KEY, YOUTUBE_FEED_OAUTH_TOKEN,
YOUTUBE_FEED_CLIENT_SECRET, YOUTUBE_FEED_REFRESH_TOKEN.
"""

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from pathlib import Path

VERSION = "0.1.0"
PREFIX = "YOUTUBE_FEED_"
CHANNEL_ID = re.compile(r"UC[0-9A-Za-z_-]{22}")
CHANNEL_IN_TEXT = re.compile(
    r"(?:channel_id=|/channel/|^|\s)(UC[0-9A-Za-z_-]{22})(?![0-9A-Za-z_-])"
)
VIDEO_ID = re.compile(r"[0-9A-Za-z_-]{11}")
BOM = chr(0xFEFF)
NS = {
    "a": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}

# Setting name -> default. Environment variable is PREFIX + NAME.upper().
DEFAULTS = {
    "channels": "",
    "channel_ids": "",
    "transport": "rss",
    "rss_url": "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}",
    "web_url": "https://www.youtube.com",
    "api_url": "https://www.googleapis.com/youtube/v3",
    "token_url": "https://oauth2.googleapis.com/token",
    "user_agent": "Mozilla/5.0 (compatible; throughline-youtube-feed/0.1)",
    "delay_s": "1.0",
    "timeout_s": "20",
    "retries": "4",
    "backoff_s": "2",
    "channels_per_run": "0",
    "requests_per_day": "5000",
    "details": "0",
    "details_per_run": "50",
    "check_shorts": "0",
    "seen_days": "120",
    "router": "0",
    "router_url": "",
    "router_model": "",
    "router_prompt": "",
    "router_json_mode": "0",
    "router_per_run": "25",
    "router_per_day": "200",
    "router_max_age_h": "72",
    "router_skip_shorts": "1",
    "router_max_tokens": "400",
    "thresholds": "",
    "cache_dir": "",
    "playlist": "",
    "playlist_apply": "0",
    "playlist_per_day": "20",
    "client_id": "",
    "import_scorer": "imported",
}
SECRETS = ("router_api_key", "oauth_token", "client_secret", "refresh_token")
THRESHOLDS = {"min_watch": 0.70, "min_progress": 50.0, "max_junk": 0.50}

DEFAULT_PROMPT = """You triage new uploads from a person's YouTube subscriptions.
For the video described in the user message, estimate:
- watch_probability: probability (0 to 1) that the person chooses to watch it;
- progress_if_watched: expected percentage (0 to 100) of the video watched if they start it;
- junk_food_probability: probability (0 to 1) that it is low-value filler or rage/clickbait;
- watch_later_probability: probability (0 to 1) that they would save it for later.
Answer with one JSON object with exactly those four numbers and a short "reason"
string (at most 30 words). No other text."""


class LimitReached(Exception):
    """A per-run or per-day limit stopped further calls of one kind."""


class FetchError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def utcnow():
    """Current time; tests replace this."""
    return datetime.now(UTC)


def iso(moment):
    return moment.isoformat() if moment else None


def parse_time(value):
    """An aware datetime from ISO text (or a date), else None."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment


def precision_of(value):
    """'day' for a bare date, otherwise 'exact'."""
    return "day" if isinstance(value, str) and len(value.strip()) == 10 else "exact"


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:24]


def truthy(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def config(env=None, **overrides):
    """Settings and secrets from environment variables (or a mapping)."""
    env = os.environ if env is None else env
    cfg = {k: env.get(PREFIX + k.upper(), d) for k, d in DEFAULTS.items()}
    for name in SECRETS:
        cfg[name] = env.get(PREFIX + name.upper()) or ""
    cfg.update({k: v for k, v in overrides.items() if v is not None})
    return cfg


LOOPBACK = ("localhost", "127.0.0.1", "::1")


def secure_url(cfg, name):
    """The URL setting `name`; credentials only go over HTTPS (or loopback)."""
    url = (cfg.get(name) or "").strip()
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https" or (
        parts.scheme == "http" and parts.hostname in LOOPBACK
    ):
        return url
    raise ValueError(
        f"{PREFIX}{name.upper()} must be an https URL (http only for localhost)"
    )


def number(cfg, name, kind=float):
    try:
        return kind(cfg.get(name) or DEFAULTS.get(name) or 0)
    except ValueError:
        raise ValueError(f"{PREFIX}{name.upper()} must be a number") from None


def thresholds(cfg):
    """Router thresholds: defaults, then a JSON object or file, then nothing else.

    Accepts min_watch/min_progress/max_junk or youtube-notify style names
    (watch_probability, progress_if_watched, junk_food_probability).
    """
    result = dict(THRESHOLDS)
    source = (cfg.get("thresholds") or "").strip()
    if not source:
        return result
    data = json.loads(
        source if source.startswith("{") else Path(source).read_text("utf-8")
    )
    aliases = {
        "min_watch": ("min_watch", "watch_probability", "min_watch_probability"),
        "min_progress": (
            "min_progress",
            "progress_if_watched",
            "min_progress_if_watched",
        ),
        "max_junk": ("max_junk", "junk_food_probability", "max_junk_food_probability"),
    }
    for key, names in aliases.items():
        for name in names:
            if isinstance(data.get(name), (int, float)):
                result[key] = float(data[name])
                break
    return result


# ---------------------------------------------------------------- state


def day_counts(state, now=None):
    """Today's (UTC) call counters; older days beyond a week are dropped."""
    today = (now or utcnow()).date()
    days = state.setdefault("days", {})
    for key in list(days):
        try:
            if (today - datetime.fromisoformat(key).date()).days > 7:
                del days[key]
        except ValueError:
            del days[key]
    return days.setdefault(today.isoformat(), {})


def take(state, name, limit, now=None):
    """Count one call of `name` against a per-day limit (0 = unlimited)."""
    counts = day_counts(state, now)
    if limit and counts.get(name, 0) >= limit:
        raise LimitReached(name)
    counts[name] = counts.get(name, 0) + 1


def prune(state, cfg, now=None):
    """Forget seen/scored entries older than seen_days (by first sight)."""
    cutoff = ((now or utcnow()) - timedelta(days=number(cfg, "seen_days"))).isoformat()
    for key in ("seen", "scored", "details", "added"):
        table = state.get(key) or {}
        for vid in [v for v, e in table.items() if (e or {}).get("at", "") < cutoff]:
            del table[vid]


def load_state(path):
    if path and Path(path).is_file():
        return json.loads(Path(path).read_text("utf-8"))
    return {}


def save_state(path, state):
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".state-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(state, handle, sort_keys=True)
    os.replace(tmp, target)


# ---------------------------------------------------------------- HTTP


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Http:
    """urllib with polite pacing, per-day budgets and retries on 429/5xx."""

    def __init__(self, cfg, state, sleep=time.sleep):
        self.cfg = cfg
        self.state = state
        self.sleep = sleep
        self.delay = number(cfg, "delay_s")
        self.timeout = number(cfg, "timeout_s")
        self.retries = number(cfg, "retries", int)
        self.backoff = number(cfg, "backoff_s")
        self.last = {}
        self.stats = {}
        self.follow = urllib.request.build_opener()
        self.nofollow = urllib.request.build_opener(_NoRedirect)

    def count(self, key, n=1):
        self.stats[key] = self.stats.get(key, 0) + n

    def pace(self, kind):
        wait = self.delay - (time.monotonic() - self.last.get(kind, -1e9))
        if kind == "youtube" and wait > 0:
            self.sleep(wait)
        self.last[kind] = time.monotonic()

    def call(
        self,
        method,
        url,
        *,
        headers=None,
        body=None,
        kind="youtube",
        follow=True,
    ):
        """Return (status, headers, bytes). Raises FetchError or LimitReached."""
        heads = {"User-Agent": self.cfg["user_agent"], "Accept-Language": "en-US,en"}
        heads.update(headers or {})
        safe = url.split("?", 1)[0]
        # urllib keeps Authorization (and re-sends POST bodies) across hosts on
        # redirects, so calls that may carry credentials never follow one.
        credentials = kind != "youtube"
        if credentials:
            follow = False
        for attempt in range(self.retries + 1):
            if kind == "youtube":
                take(self.state, "requests", number(self.cfg, "requests_per_day", int))
            self.pace(kind)
            self.count(kind + "_requests")
            request = urllib.request.Request(
                url, data=body, headers=heads, method=method
            )
            opener = self.follow if follow else self.nofollow
            retry_after = None
            try:
                with opener.open(request, timeout=self.timeout) as response:
                    return response.status, dict(response.headers), response.read()
            except urllib.error.HTTPError as error:
                if not follow and 300 <= error.code < 400:
                    if credentials:
                        raise FetchError(
                            f"HTTP {error.code} redirect refused from {safe}",
                            error.code,
                        ) from None
                    return error.code, dict(error.headers or {}), b""
                if error.code != 429 and error.code < 500:
                    raise FetchError(
                        f"HTTP {error.code} from {safe}", error.code
                    ) from None
                self.count("retryable_errors")
                retry_after = (error.headers or {}).get("Retry-After")
                last = FetchError(f"HTTP {error.code} from {safe}", error.code)
            except (
                urllib.error.URLError,
                TimeoutError,
                ConnectionError,
                OSError,
            ) as error:
                self.count("transport_errors")
                last = FetchError(f"{type(error).__name__} from {safe}")
            if attempt == self.retries:
                raise last
            wait = self.backoff * (2**attempt)
            if retry_after and str(retry_after).isdigit():
                wait = max(wait, int(retry_after))
            self.sleep(min(wait, 300))
        raise FetchError("unreachable")

    def json(self, method, url, *, payload=None, headers=None, kind="api"):
        heads = {"Accept": "application/json"}
        heads.update(headers or {})
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            heads["Content-Type"] = "application/json"
        _, _, data = self.call(method, url, headers=heads, body=body, kind=kind)
        return json.loads(data.decode("utf-8") or "{}")


# ---------------------------------------------------------------- channel lists


def channel_ids_in(text):
    return [m.group(1) for m in CHANNEL_IN_TEXT.finditer(text or "")]


def _json_channels(node, out):
    if isinstance(node, str):
        if CHANNEL_ID.fullmatch(node):
            out.append((node, None))
        return
    if isinstance(node, list):
        for item in node:
            _json_channels(item, out)
        return
    if not isinstance(node, dict):
        return
    snippet = node.get("snippet") if isinstance(node.get("snippet"), dict) else {}
    cid = None
    for key in ("channel_id", "channelId", "id"):
        value = node.get(key)
        if isinstance(value, str) and CHANNEL_ID.fullmatch(value):
            cid = value
            break
    resource = (
        snippet.get("resourceId") if isinstance(snippet.get("resourceId"), dict) else {}
    )
    if not cid and CHANNEL_ID.fullmatch(str(resource.get("channelId") or "")):
        cid = resource["channelId"]
    if not cid:
        for key in ("url", "channel_url", "xmlUrl"):
            found = channel_ids_in(str(node.get(key) or ""))
            if found:
                cid = found[0]
                break
    if cid:
        title = next(
            (
                v
                for v in (
                    node.get("title"),
                    node.get("name"),
                    node.get("channel_name"),
                    node.get("channel_title"),
                    snippet.get("title"),
                )
                if isinstance(v, str) and v.strip()
            ),
            None,
        )
        out.append((cid, title))
        return
    for key, value in node.items():
        if CHANNEL_ID.fullmatch(str(key)):
            title = value if isinstance(value, str) else None
            if isinstance(value, dict):
                title = (
                    value.get("title") or value.get("name") or value.get("channel_name")
                )
            out.append((key, title))
        else:
            _json_channels(value, out)


def parse_channel_list(text, name):
    """(channel_id, title) pairs from OPML/XML, CSV, JSON or plain text."""
    out = []
    suffix = Path(name).suffix.lower()
    stripped = text.lstrip(BOM).strip()
    if suffix in (".opml", ".xml") or stripped.startswith("<"):
        if "<!DOCTYPE" in stripped.upper() or "<!ENTITY" in stripped.upper():
            raise ValueError(f"{name}: DTDs are not accepted")
        root = ET.fromstring(stripped)
        for node in root.iter("outline"):
            found = channel_ids_in(node.get("xmlUrl") or node.get("htmlUrl") or "")
            if found:
                out.append((found[0], node.get("title") or node.get("text")))
    elif suffix == ".json" or stripped[:1] in ("[", "{"):
        data = json.loads(stripped)
        if isinstance(data, dict):
            keys = [k for k in ("channels", "subscriptions", "items") if k in data]
            if keys:
                data = [data[k] for k in keys]
            elif isinstance(data.get("data"), dict):
                data = data["data"]
        _json_channels(data, out)
    elif suffix == ".csv":
        rows = list(csv.reader(io.StringIO(stripped)))
        header = [c.lower() for c in rows[0]] if rows else []
        title_col = next((i for i, c in enumerate(header) if "title" in c), None)
        for row in rows:
            for cell in row:
                found = channel_ids_in(cell)
                if found:
                    title = (
                        row[title_col]
                        if title_col is not None and title_col < len(row)
                        else None
                    )
                    out.append((found[0], title))
                    break
    else:
        for line in stripped.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            found = channel_ids_in(line)
            if found:
                words = [w for w in line.split() if not channel_ids_in(w)]
                out.append((found[0], " ".join(words) or None))
    return out


def load_channels(cfg):
    """Configured channels in first-seen order, de-duplicated by channel ID."""
    pairs = []
    for raw in (cfg.get("channels") or "").split(os.pathsep):
        if raw.strip():
            path = Path(raw.strip())
            pairs.extend(parse_channel_list(path.read_text("utf-8-sig"), path.name))
    for raw in (cfg.get("channel_ids") or "").split(","):
        found = channel_ids_in(raw.strip())
        if found:
            pairs.append((found[0], None))
    channels = {}
    for cid, title in pairs:
        entry = channels.setdefault(cid, {"channel_id": cid, "title": None})
        if not entry["title"] and title:
            entry["title"] = title.strip()
    return list(channels.values())


# ---------------------------------------------------------------- feeds


def _text(node, path):
    found = node.find(path, NS) if node is not None else None
    return found.text.strip() if found is not None and found.text else None


def parse_feed(data):
    """Parse a channel's Atom feed into (feed info, [(raw, observed)])."""
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    upper = text[:2000].upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise ValueError("feed with a DTD refused")
    root = ET.fromstring(text)
    info = {
        "channel_id": _text(root, "yt:channelId"),
        "title": _text(root, "a:title"),
        "author": _text(root, "a:author/a:name"),
    }
    entries = []
    for entry in root.findall("a:entry", NS):
        link = entry.find("a:link[@rel='alternate']", NS)
        group = entry.find("media:group", NS)
        content = group.find("media:content", NS) if group is not None else None
        thumb = group.find("media:thumbnail", NS) if group is not None else None
        community = group.find("media:community", NS) if group is not None else None
        rating = (
            community.find("media:starRating", NS) if community is not None else None
        )
        stats = (
            community.find("media:statistics", NS) if community is not None else None
        )
        raw = {
            "entry_id": _text(entry, "a:id"),
            "video_id": _text(entry, "yt:videoId"),
            "channel_id": _text(entry, "yt:channelId") or info["channel_id"],
            "title": _text(entry, "a:title"),
            "link": link.get("href") if link is not None else None,
            "author": {
                "name": _text(entry, "a:author/a:name"),
                "uri": _text(entry, "a:author/a:uri"),
            },
            "published": _text(entry, "a:published"),
            "media": {
                "title": _text(group, "media:title"),
                "description": _text(group, "media:description"),
                "content": dict(content.attrib) if content is not None else None,
                "thumbnail": dict(thumb.attrib) if thumb is not None else None,
            },
        }
        # Volatile: they change between fetches and must not version a record.
        observed = {
            "updated": _text(entry, "a:updated"),
            "views": int(stats.get("views"))
            if stats is not None and (stats.get("views") or "").isdigit()
            else None,
            "rating": dict(rating.attrib) if rating is not None else None,
        }
        if raw["video_id"] and VIDEO_ID.fullmatch(raw["video_id"]):
            entries.append((raw, observed))
    return info, entries


def extract_json(html, markers):
    """The JSON object assigned after one of `markers` in a page, else None."""
    decoder = json.JSONDecoder()
    for marker in markers:
        start = html.find(marker)
        if start < 0:
            continue
        index = start + len(marker)
        while index < len(html) and html[index] in " \t\r\n":
            index += 1
        try:
            value, _ = decoder.raw_decode(html, index)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _runs(value):
    if isinstance(value, dict):
        if isinstance(value.get("simpleText"), str):
            return value["simpleText"]
        if isinstance(value.get("content"), str):
            return value["content"]
        runs = value.get("runs")
        if isinstance(runs, list):
            return "".join(r.get("text", "") for r in runs if isinstance(r, dict))
    return value if isinstance(value, str) else None


def _walk(node):
    stack = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            yield item
            stack.extend(reversed(list(item.values())))
        elif isinstance(item, list):
            stack.extend(reversed(item))


def parse_videos_page(html):
    """Videos of a public channel /videos page: (channel title, [raw])."""
    data = extract_json(
        html,
        ["var ytInitialData = ", 'window["ytInitialData"] = ', "ytInitialData = "],
    )
    if data is None:
        raise ValueError("no ytInitialData in channel page")
    meta = (data.get("metadata") or {}).get("channelMetadataRenderer") or {}
    videos, seen = [], set()
    for node in _walk(data):
        raw = None
        if isinstance(node.get("videoRenderer"), dict):
            v = node["videoRenderer"]
            raw = {
                "video_id": v.get("videoId"),
                "title": _runs(v.get("title")),
                "length_text": _runs(v.get("lengthText")),
                "published_text": _runs(v.get("publishedTimeText")),
                "live": any("LIVE" in json.dumps(b) for b in v.get("badges") or [])
                or bool(v.get("upcomingEventData")),
                "short": False,
            }
        elif isinstance(node.get("reelItemRenderer"), dict):
            v = node["reelItemRenderer"]
            raw = {
                "video_id": v.get("videoId"),
                "title": _runs(v.get("headline")),
                "short": True,
            }
        elif isinstance(node.get("lockupViewModel"), dict):
            v = node["lockupViewModel"]
            if "VIDEO" not in str(v.get("contentType", "VIDEO")):
                continue
            title = (
                (v.get("metadata") or {}).get("lockupMetadataViewModel") or {}
            ).get("title")
            raw = {
                "video_id": v.get("contentId"),
                "title": _runs(title),
                "short": False,
            }
        if raw and raw["video_id"] and VIDEO_ID.fullmatch(raw["video_id"]):
            if raw["video_id"] not in seen:
                seen.add(raw["video_id"])
                videos.append(raw)
    return meta.get("title"), videos


UNITS = {
    "second": 1,
    "minute": 60,
    "hour": 3600,
    "day": 86400,
    "week": 7 * 86400,
    "month": 30 * 86400,
    "year": 365 * 86400,
}


def relative_age(text):
    """Seconds in '3 hours ago' style text, else None."""
    match = re.search(
        r"(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago", text or ""
    )
    return int(match.group(1)) * UNITS[match.group(2)] if match else None


def duration_text(text):
    """Seconds in '1:02:03' style text, else None."""
    if not text or not re.fullmatch(r"\d+(:\d{1,2}){0,2}", text.strip()):
        return None
    seconds = 0
    for part in text.strip().split(":"):
        seconds = seconds * 60 + int(part)
    return seconds


def parse_watch_page(html):
    """Stable details from a public watch page (no view counts)."""
    player = extract_json(
        html, ["var ytInitialPlayerResponse = ", "ytInitialPlayerResponse = "]
    )
    if player is None:
        raise ValueError("no ytInitialPlayerResponse in watch page")
    details = player.get("videoDetails") or {}
    micro = (player.get("microformat") or {}).get("playerMicroformatRenderer") or {}
    length = str(details.get("lengthSeconds") or "")
    live = micro.get("liveBroadcastDetails") or {}
    return {
        "video_id": details.get("videoId"),
        "title": details.get("title"),
        "channel_id": details.get("channelId"),
        "author": details.get("author"),
        "length_seconds": int(length) if length.isdigit() else None,
        "description": details.get("shortDescription"),
        "is_live_content": details.get("isLiveContent"),
        "is_upcoming": details.get("isUpcoming"),
        "publish_date": micro.get("publishDate"),
        "upload_date": micro.get("uploadDate"),
        "category": micro.get("category"),
        "live_start": live.get("startTimestamp"),
        "live_end": live.get("endTimestamp"),
    }


# ---------------------------------------------------------------- normalization


def watch_url(vid):
    return f"https://www.youtube.com/watch?v={vid}"


def normalize_upload(record):
    """The normalized view of an upload record, whatever its transport."""
    raw = record.get("raw") or {}
    details = record.get("details") or {}
    transport = record.get("transport")
    vid = record.get("id")
    video = {
        "video_id": vid,
        "channel_id": record.get("channel_id"),
        "channel_title": None,
        "title": None,
        "description": None,
        "published_at": None,
        "time_precision": None,
        "duration_s": None,
        "is_short": None,
        "is_live": None,
        "url": watch_url(vid),
    }
    if transport == "rss":
        media = raw.get("media") or {}
        video.update(
            channel_id=raw.get("channel_id") or record.get("channel_id"),
            channel_title=(raw.get("author") or {}).get("name"),
            title=raw.get("title") or media.get("title"),
            description=media.get("description"),
            published_at=raw.get("published"),
            time_precision=precision_of(raw.get("published")),
        )
        if "/shorts/" in (raw.get("link") or ""):
            video["is_short"] = True
            video["url"] = raw["link"]
    elif transport == "page":
        video.update(
            channel_title=raw.get("channel_title"),
            title=raw.get("title"),
            published_at=record.get("estimated_published_at"),
            time_precision="approximate",
            duration_s=duration_text(raw.get("length_text")),
            is_short=raw.get("short"),
            is_live=raw.get("live") or None,
        )
        if raw.get("short"):
            video["url"] = f"https://www.youtube.com/shorts/{vid}"
    elif transport == "prediction":
        duration = raw.get("duration_seconds")
        video.update(
            channel_id=raw.get("channel_id") or record.get("channel_id"),
            channel_title=raw.get("channel_name"),
            title=raw.get("title"),
            published_at=raw.get("published_at"),
            time_precision=precision_of(raw.get("published_at"))
            if raw.get("published_at")
            else None,
            duration_s=int(duration) if isinstance(duration, (int, float)) else None,
            url=raw.get("url") or watch_url(vid),
        )
    if details:
        if details.get("length_seconds") is not None:
            video["duration_s"] = details["length_seconds"]
        video["description"] = video["description"] or details.get("description")
        video["title"] = video["title"] or details.get("title")
        video["channel_title"] = video["channel_title"] or details.get("author")
        video["channel_id"] = video["channel_id"] or details.get("channel_id")
        if details.get("is_live_content") or details.get("is_upcoming"):
            video["is_live"] = True
        exact = details.get("publish_date") or details.get("upload_date")
        if exact and video["time_precision"] in (None, "approximate"):
            video["published_at"] = iso(parse_time(exact))
            video["time_precision"] = precision_of(exact)
    if record.get("short_check") is not None:
        video["is_short"] = bool(record["short_check"])
        if video["is_short"]:
            video["url"] = f"https://www.youtube.com/shorts/{vid}"
    if video["published_at"]:
        moment = parse_time(video["published_at"])
        video["published_at"] = iso(moment) if moment else None
    return video


def upload_rank(record):
    """How complete an upload record is; higher replaces lower, never the reverse."""
    base = {"prediction": 0, "page": 4, "rss": 8}.get(record.get("transport"), 0)
    return (
        base
        + (2 if record.get("details") else 0)
        + (1 if record.get("short_check") is not None else 0)
    )


def stable(record):
    """The upload record without fetch-time or volatile fields."""
    return {k: v for k, v in record.items() if k not in ("fetched_at", "observed")}


def _number(value, scale_if_over=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        try:
            value = float(str(value).strip().rstrip("%"))
        except (TypeError, ValueError):
            return None
    value = float(value)
    if scale_if_over is not None and value > scale_if_over:
        value = value / 100.0
    return value


def verdict(scores, limits):
    w, p, j = (
        scores.get("watch_probability"),
        scores.get("progress_if_watched"),
        scores.get("junk_food_probability"),
    )
    if w is None or p is None or j is None:
        return "skip"
    return (
        "pick"
        if w >= limits["min_watch"]
        and p >= limits["min_progress"]
        and j < limits["max_junk"]
        else "skip"
    )


def prediction_record(line, scorer, origin="predictions"):
    """A pick record from a youtube-notify predictions/flagged line."""
    vid = line.get("video_id")
    if not isinstance(vid, str) or not VIDEO_ID.fullmatch(vid):
        return None
    return {
        "kind": "pick",
        "id": f"{vid}:{scorer}",
        "video_id": vid,
        "scorer": scorer,
        "origin": origin,
        "raw": line,
    }


def flagged_records(data, scorer):
    """Pick records from a youtube-notify flagged.json value (list or mapping)."""
    if isinstance(data, dict):
        items = data.get("flagged") or data.get("items") or list(data.values())
    else:
        items = data
    out = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict):
            record = prediction_record(item, scorer, origin="flagged")
            if record:
                out.append(record)
    return out


def pick_decision(records, limits=None):
    """One decision from all records of one pick ID (deterministic merge).

    The latest scoring wins; a flagged.json entry of that same scoring marks
    it flagged. Imported lines keep their own `flagged` verdict when present.
    """
    limits = limits or dict(THRESHOLDS)

    def scored(record):
        if record.get("origin") == "router":
            return record.get("decision", {}).get("scored_at") or ""
        return str(record.get("raw", {}).get("scored_at") or "")

    order = {"router": 2, "predictions": 1, "flagged": 0}
    ordered = sorted(
        records,
        key=lambda r: (scored(r), order.get(r.get("origin"), 0), digest(r.get("raw"))),
    )
    best = ordered[-1]
    same = [r for r in ordered if scored(r) == scored(best)]
    if best.get("origin") == "router":
        decision = dict(best["decision"])
    else:
        raw = best.get("raw") or {}
        scores = {
            "watch_probability": _number(raw.get("watch_probability"), 1.0),
            "progress_if_watched": _number(raw.get("progress_if_watched")),
            "junk_food_probability": _number(raw.get("junk_food_probability"), 1.0),
            "watch_later_probability": _number(raw.get("watch_later_probability"), 1.0),
        }
        flagged = any(
            r.get("origin") == "flagged" or (r.get("raw") or {}).get("flagged") is True
            for r in same
        )
        explicit = any("flagged" in (r.get("raw") or {}) for r in same) or any(
            r.get("origin") == "flagged" for r in same
        )
        is_junk = raw.get("is_junk_food")
        decision = {
            **scores,
            "is_junk_food": is_junk if isinstance(is_junk, bool) else None,
            "verdict": ("pick" if flagged else "skip")
            if explicit
            else verdict(scores, limits),
            "reason": raw.get("reason") if isinstance(raw.get("reason"), str) else None,
            "scored_at": iso(parse_time(raw.get("scored_at"))),
            "thresholds": None if explicit else limits,
            "video": {
                "video_id": best["video_id"],
                "channel_id": raw.get("channel_id"),
                "channel_title": raw.get("channel_name"),
                "title": raw.get("title"),
                "url": raw.get("url") or watch_url(best["video_id"]),
                "published_at": iso(parse_time(raw.get("published_at"))),
                "time_precision": precision_of(raw.get("published_at"))
                if parse_time(raw.get("published_at"))
                else None,
                "duration_s": int(raw["duration_seconds"])
                if isinstance(raw.get("duration_seconds"), (int, float))
                else None,
            },
        }
    decision.update(scorer=best.get("scorer"), origin=best.get("origin"))
    return decision


# ---------------------------------------------------------------- collection


# A channel's RSS feed holds only its newest uploads.
RSS_ENTRIES = 15


def feed_entries(cfg, http, channel, now):
    """Upload records of one channel through the configured transport."""
    cid = channel["channel_id"]
    transport = (cfg.get("transport") or "rss").lower()
    fetched = iso(now)
    if transport in ("rss", "auto"):
        try:
            url = cfg["rss_url"].format(channel_id=urllib.parse.quote(cid))
            _, _, body = http.call(
                "GET", url, headers={"Accept": "application/atom+xml"}
            )
            info, entries = parse_feed(body)
            records = []
            for raw, observed in entries:
                records.append(
                    {
                        "kind": "upload",
                        "id": raw["video_id"],
                        "channel_id": raw["channel_id"] or cid,
                        "transport": "rss",
                        "fetched_at": fetched,
                        "raw": raw,
                        "observed": observed,
                    }
                )
            return records
        except (FetchError, ValueError, ET.ParseError):
            if transport == "rss":
                raise
    url = cfg["web_url"].rstrip("/") + f"/channel/{urllib.parse.quote(cid)}/videos"
    _, _, body = http.call("GET", url, headers={"Accept": "text/html"})
    title, videos = parse_videos_page(body.decode("utf-8", "replace"))
    records = []
    for raw in videos:
        observed = {"published_text": raw.pop("published_text", None)}
        raw["channel_title"] = title or channel.get("title")
        records.append(
            {
                "kind": "upload",
                "id": raw["video_id"],
                "channel_id": cid,
                "transport": "page",
                "fetched_at": fetched,
                "raw": raw,
                "observed": observed,
            }
        )
    return records


def collect(cfg, state, http=None, log=None):
    """Fetch new or changed uploads of the configured channels.

    `state` (a JSON-able dict, mutated) holds the seen-set, rotation offset,
    channel health and daily counters, so repeated runs are incremental.
    Returns (records, stats).
    """
    now = utcnow()
    http = http or Http(cfg, state)
    channels = load_channels(cfg)
    stats = {"channels": len(channels), "channels_read": 0, "uploads": 0, "errors": 0}
    if not channels:
        return [], stats
    per_run = number(cfg, "channels_per_run", int) or len(channels)
    start = state.get("rotation", 0) % len(channels)
    batch = (channels[start:] + channels[:start])[:per_run]
    seen = state.setdefault("seen", {})
    health = state.setdefault("channels", {})
    horizon = now - timedelta(days=number(cfg, "seen_days"))
    records = []
    read = 0
    for channel in batch:
        cid = channel["channel_id"]
        try:
            entries = feed_entries(cfg, http, channel, now)
        except LimitReached:
            stats["limit"] = "requests_per_day"
            break
        except (FetchError, ValueError, ET.ParseError) as error:
            read += 1
            stats["errors"] += 1
            status = health.setdefault(cid, {})
            status["errors"] = status.get("errors", 0) + 1
            status["last_error"] = str(error)[:200]
            records.append(
                {
                    "kind": "feed_error",
                    "id": cid,
                    "channel_id": cid,
                    "fetched_at": iso(now),
                    "error": str(error)[:200],
                    "status": getattr(error, "status", None),
                }
            )
            continue
        read += 1
        status = health.setdefault(cid, {})
        known = bool(status.get("first_ok"))
        status.setdefault("first_ok", iso(now))
        status.update(last_ok=iso(now), errors=0)
        status.pop("last_error", None)
        published = [
            parse_time(normalize_upload(r)["published_at"])
            for r in entries
            if r["transport"] == "rss"
        ]
        published = [p for p in published if p]
        if published:
            newest = parse_time(status.get("newest"))
            if newest and len(entries) >= RSS_ENTRIES and min(published) > newest:
                records.append(
                    {
                        "kind": "feed_gap",
                        "id": cid,
                        "channel_id": cid,
                        "fetched_at": iso(now),
                        "from": iso(newest),
                        "to": iso(min(published)),
                        "reason": "source_cap",
                        "entries": len(entries),
                    }
                )
            status["newest"] = iso(max([*published, *([newest] if newest else [])]))
        for record in entries:
            vid = record["id"]
            previous = seen.get(vid)
            if record["transport"] == "page":
                # Fix a page item's estimated time at first sight so that the
                # same video always maps to the same record.
                if previous and previous.get("est"):
                    record["estimated_published_at"] = previous["est"]
                else:
                    age = relative_age(record["observed"].get("published_text"))
                    estimate = now - timedelta(seconds=age or 0)
                    record["estimated_published_at"] = iso(
                        estimate.replace(microsecond=0)
                    )
            fingerprint = digest(stable(record))
            if previous and previous.get("d") == fingerprint:
                continue
            published = parse_time(normalize_upload(record)["published_at"])
            if not previous and known and published and published < horizon:
                # Old item reappearing after its seen entry was pruned.
                continue
            entry = seen.setdefault(vid, {"at": iso(now)})
            entry["d"] = fingerprint
            if record.get("estimated_published_at"):
                entry["est"] = record["estimated_published_at"]
            records.append(record)
            stats["uploads"] += 1
    state["rotation"] = (start + read) % len(channels)
    stats["channels_read"] = read
    enrich(cfg, state, http, [r for r in records if r["kind"] == "upload"], stats)
    prune(state, cfg, now)
    stats.update({k: v for k, v in http.stats.items()})
    if log:
        log(stats)
    return records, stats


def enrich(cfg, state, http, uploads, stats):
    """Optional watch-page details and Shorts checks, once per video."""
    done = state.setdefault("details", {})
    now = iso(utcnow())
    if truthy(cfg.get("details")):
        limit = number(cfg, "details_per_run", int)
        for record in uploads:
            if limit <= 0:
                break
            if record["id"] in done or record.get("details"):
                continue
            url = (
                cfg["web_url"].rstrip("/")
                + "/watch?v="
                + urllib.parse.quote(record["id"])
            )
            try:
                _, _, body = http.call("GET", url, headers={"Accept": "text/html"})
                record["details"] = parse_watch_page(body.decode("utf-8", "replace"))
                stats["details"] = stats.get("details", 0) + 1
            except LimitReached:
                stats["limit"] = "requests_per_day"
                return
            except (FetchError, ValueError) as error:
                stats["details_errors"] = stats.get("details_errors", 0) + 1
                record["details_error"] = str(error)[:200]
            done[record["id"]] = {"at": now}
            limit -= 1
    if truthy(cfg.get("check_shorts")):
        for record in uploads:
            video = normalize_upload(record)
            if video["is_short"] is not None:
                continue
            url = (
                cfg["web_url"].rstrip("/")
                + "/shorts/"
                + urllib.parse.quote(record["id"])
            )
            try:
                status, _, _ = http.call("HEAD", url, follow=False)
            except LimitReached:
                stats["limit"] = "requests_per_day"
                return
            except FetchError:
                continue
            record["short_check"] = status == 200


# ---------------------------------------------------------------- router


def prompt_text(cfg):
    source = (cfg.get("router_prompt") or "").strip()
    return Path(source).read_text("utf-8") if source else DEFAULT_PROMPT


def parse_scores(text):
    """Scores from a model answer: the first JSON object in the text."""
    start = (text or "").find("{")
    if start < 0:
        raise ValueError("model answer has no JSON object")
    value, _ = json.JSONDecoder().raw_decode(text, start)
    if not isinstance(value, dict):
        raise ValueError("model answer is not an object")
    scores = {
        "watch_probability": _number(value.get("watch_probability"), 1.0),
        "progress_if_watched": _number(value.get("progress_if_watched")),
        "junk_food_probability": _number(value.get("junk_food_probability"), 1.0),
        "watch_later_probability": _number(value.get("watch_later_probability"), 1.0),
    }
    for key in (
        "watch_probability",
        "junk_food_probability",
        "watch_later_probability",
    ):
        if scores[key] is not None:
            scores[key] = min(1.0, max(0.0, scores[key]))
    if scores["progress_if_watched"] is not None:
        p = scores["progress_if_watched"]
        p = (
            p * 100
            if 0 < p <= 1 and "." in str(value.get("progress_if_watched"))
            else p
        )
        scores["progress_if_watched"] = min(100.0, max(0.0, p))
    missing = [
        k
        for k in ("watch_probability", "progress_if_watched", "junk_food_probability")
        if scores[k] is None
    ]
    if missing:
        raise ValueError("model answer lacks " + ", ".join(missing))
    reason = value.get("reason")
    return scores, reason[:500] if isinstance(reason, str) else None


def route(cfg, state, uploads, http=None, log=None):
    """Score new uploads with an OpenAI-compatible model. Returns (picks, stats).

    Off unless router=1 with router_url and router_model. Limits: per run,
    per day (UTC), maximum age, one attempt per video (two after an error).
    """
    stats = {"scored": 0, "router_errors": 0}
    if not truthy(cfg.get("router")):
        return [], stats
    if not cfg.get("router_url") or not cfg.get("router_model"):
        raise ValueError(
            "router needs YOUTUBE_FEED_ROUTER_URL and YOUTUBE_FEED_ROUTER_MODEL"
        )
    secure_url(cfg, "router_url")
    http = http or Http(cfg, state)
    now = utcnow()
    model = cfg["router_model"]
    prompt = prompt_text(cfg)
    prompt_sha = hashlib.sha256(prompt.encode()).hexdigest()[:16]
    limits = thresholds(cfg)
    max_age = timedelta(hours=number(cfg, "router_max_age_h"))
    scored = state.setdefault("scored", {})
    candidates = {}
    for record in uploads:
        if record.get("kind") != "upload":
            continue
        video = normalize_upload(record)
        published = parse_time(video["published_at"])
        mark = scored.get(record["id"]) or {}
        if mark and (not mark.get("error") or mark.get("attempts", 1) >= 2):
            continue
        if not published or now - published > max_age or video["is_live"]:
            continue
        if video["is_short"] and truthy(cfg.get("router_skip_shorts")):
            continue
        best = candidates.get(record["id"])
        if not best or upload_rank(record) > upload_rank(best[1]):
            candidates[record["id"]] = (video, record)
    ordered = sorted(
        candidates.values(),
        key=lambda x: (x[0]["published_at"], x[0]["video_id"]),
        reverse=True,
    )
    picks = []
    per_run = number(cfg, "router_per_run", int)
    cache = Path(cfg["cache_dir"]) if cfg.get("cache_dir") else None
    for video, record in ordered[: per_run if per_run > 0 else None]:
        vid = video["video_id"]
        key = hashlib.sha256(f"{model}\n{prompt_sha}\n{vid}".encode()).hexdigest()[:32]
        cached = cache / f"{key}.json" if cache else None
        answer = None
        if cached and cached.is_file():
            answer = json.loads(cached.read_text("utf-8"))
            stats["cache_hits"] = stats.get("cache_hits", 0) + 1
        else:
            try:
                take(state, "router", number(cfg, "router_per_day", int), now)
            except LimitReached:
                stats["limit"] = "router_per_day"
                break
            user = {
                k: video[k]
                for k in (
                    "title",
                    "channel_title",
                    "published_at",
                    "duration_s",
                    "is_short",
                    "url",
                )
            }
            user["description"] = (video.get("description") or "")[:1500]
            body = {
                "model": model,
                "temperature": 0,
                "max_tokens": number(cfg, "router_max_tokens", int),
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
                ],
            }
            if truthy(cfg.get("router_json_mode")):
                body["response_format"] = {"type": "json_object"}
            headers = {}
            if cfg.get("router_api_key"):
                headers["Authorization"] = "Bearer " + cfg["router_api_key"]
            try:
                response = http.json(
                    "POST",
                    cfg["router_url"].rstrip("/") + "/chat/completions",
                    payload=body,
                    headers=headers,
                    kind="router",
                )
                content = response["choices"][0]["message"]["content"]
                answer = {
                    "content": content,
                    "usage": response.get("usage"),
                    "model": response.get("model"),
                }
                parse_scores(content)
                if cached:
                    try:
                        cached.parent.mkdir(parents=True, exist_ok=True)
                        cached.write_text(json.dumps(answer), "utf-8")
                    except OSError:
                        stats["cache_errors"] = stats.get("cache_errors", 0) + 1
            except (FetchError, ValueError, KeyError, IndexError, TypeError) as error:
                stats["router_errors"] += 1
                attempts = (scored.get(vid) or {}).get("attempts", 0) + 1
                scored[vid] = {
                    "at": iso(now),
                    "error": str(error)[:200],
                    "attempts": attempts,
                }
                continue
        scores, reason = parse_scores(answer["content"])
        decision = {
            **scores,
            "is_junk_food": scores["junk_food_probability"] >= limits["max_junk"],
            "verdict": verdict(scores, limits),
            "reason": reason,
            "scored_at": iso(now),
            "thresholds": limits,
            "video": {
                k: video[k]
                for k in (
                    "video_id",
                    "channel_id",
                    "channel_title",
                    "title",
                    "url",
                    "published_at",
                    "time_precision",
                    "duration_s",
                )
            },
            "model": model,
            "prompt_sha": prompt_sha,
        }
        picks.append(
            {
                "kind": "pick",
                "id": f"{vid}:{model}",
                "video_id": vid,
                "scorer": model,
                "origin": "router",
                "decision": decision,
                "raw": answer,
            }
        )
        scored[vid] = {"at": iso(now), "verdict": decision["verdict"]}
        stats["scored"] += 1
    if log:
        log(stats)
    return picks, stats


# ---------------------------------------------------------------- playlist


def check_playlist(cfg, apply=None):
    """Raise ValueError for an unusable playlist setup, before any paid call."""
    apply = truthy(cfg.get("playlist_apply")) if apply is None else apply
    if not (cfg.get("playlist") or "").strip() or not apply:
        return
    secure_url(cfg, "api_url")
    if cfg.get("oauth_token"):
        return
    if not (
        cfg.get("client_id") and cfg.get("client_secret") and cfg.get("refresh_token")
    ):
        raise ValueError(
            "playlist writes need YOUTUBE_FEED_OAUTH_TOKEN or YOUTUBE_FEED_CLIENT_ID, "
            "YOUTUBE_FEED_CLIENT_SECRET and YOUTUBE_FEED_REFRESH_TOKEN"
        )
    secure_url(cfg, "token_url")


def access_token(cfg, http):
    check_playlist(cfg, apply=True)
    if cfg.get("oauth_token"):
        return cfg["oauth_token"]
    body = urllib.parse.urlencode(
        {
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "refresh_token": cfg["refresh_token"],
            "grant_type": "refresh_token",
        }
    ).encode()
    _, _, data = http.call(
        "POST",
        cfg["token_url"],
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=body,
        kind="api",
    )
    token = json.loads(data.decode("utf-8")).get("access_token")
    if not token:
        raise FetchError("token endpoint returned no access token")
    return token


def resolve_playlist(cfg, http, token, target):
    if target == "WL" or re.fullmatch(r"(PL|LL|FL|UU|OL)[0-9A-Za-z_-]{10,}", target):
        return target
    page, pages = None, set()
    while True:
        query = {"part": "snippet", "mine": "true", "maxResults": "50"}
        if page:
            query["pageToken"] = page
        data = http.json(
            "GET",
            cfg["api_url"].rstrip("/") + "/playlists?" + urllib.parse.urlencode(query),
            headers={"Authorization": "Bearer " + token},
        )
        for item in data.get("items") or []:
            if (item.get("snippet") or {}).get(
                "title", ""
            ).strip().casefold() == target.strip().casefold():
                return item["id"]
        page = data.get("nextPageToken")
        if not page or page in pages or len(pages) >= 100:
            raise ValueError("no playlist of yours has that title")
        pages.add(page)


def playlist(cfg, state, picks, http=None, apply=None, log=None):
    """Add recent picked videos to the configured playlist.

    Dry run unless `apply` (or playlist_apply=1): returns (records, stats)
    where stats["planned"] lists what would be added.
    """
    stats = {"planned": [], "added": 0, "playlist_errors": 0}
    target = (cfg.get("playlist") or "").strip()
    if not target:
        return [], stats
    apply = truthy(cfg.get("playlist_apply")) if apply is None else apply
    now = utcnow()
    max_age = timedelta(hours=number(cfg, "router_max_age_h"))
    added = state.setdefault("added", {})
    wanted = {}
    for record in picks:
        if record.get("kind") != "pick":
            continue
        decision = pick_decision([record], thresholds(cfg))
        when = parse_time(decision.get("scored_at"))
        vid = record["video_id"]
        if (
            decision["verdict"] != "pick"
            or vid in added
            or not when
            or now - when > max_age
        ):
            continue
        wanted.setdefault(vid, record["id"])
    stats["planned"] = sorted(wanted)
    if not apply or not wanted:
        return [], stats
    check_playlist(cfg, apply=True)
    http = http or Http(cfg, state)
    try:
        token = access_token(cfg, http)
        playlist_id = resolve_playlist(cfg, http, token, target)
    except (FetchError, ValueError, KeyError, TypeError) as error:
        # Picks already paid for must still be written; retried next run.
        stats["playlist_errors"] += 1
        stats["playlist_setup_error"] = str(error)[:200]
        return [], stats
    records = []
    for vid in sorted(wanted):
        try:
            take(state, "playlist", number(cfg, "playlist_per_day", int), now)
        except LimitReached:
            stats["limit"] = "playlist_per_day"
            break
        body = {
            "snippet": {
                "playlistId": playlist_id,
                "resourceId": {"kind": "youtube#video", "videoId": vid},
            }
        }
        try:
            item = http.json(
                "POST",
                cfg["api_url"].rstrip("/") + "/playlistItems?part=snippet",
                payload=body,
                headers={"Authorization": "Bearer " + token},
            )
        except FetchError as error:
            stats["playlist_errors"] += 1
            added[vid] = {"at": iso(now), "error": str(error)[:200]}
            continue
        added[vid] = {"at": iso(now), "playlist_id": playlist_id}
        records.append(
            {
                "kind": "playlist_add",
                "id": f"{vid}:{playlist_id}",
                "video_id": vid,
                "pick_id": wanted[vid],
                "playlist_id": playlist_id,
                "playlist": target,
                "added_at": iso(now),
                "item_id": item.get("id"),
            }
        )
        stats["added"] += 1
    if log:
        log(stats)
    return records, stats


# ---------------------------------------------------------------- files


def read_records(paths, scorer="imported"):
    """Records from export files, youtube-notify predictions JSONL or flagged JSON.

    A directory is read recursively (*.jsonl, *.jsonl.N, flagged*.json), in
    sorted order.
    """
    files = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            files.extend(
                sorted(
                    p
                    for p in path.rglob("*")
                    if p.is_file()
                    and (
                        re.search(r"\.jsonl(\.\d+)?$", p.name)
                        or (p.name.startswith("flagged") and p.suffix == ".json")
                    )
                )
            )
        else:
            files.append(path)
    records = []
    for path in files:
        text = path.read_text("utf-8-sig")
        if path.suffix == ".json":
            records.extend(flagged_records(json.loads(text), scorer))
            continue
        for number_, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except ValueError:
                raise ValueError(f"{path.name}:{number_}: not JSON") from None
            if not isinstance(value, dict):
                continue
            if "kind" in value:
                records.append(value)
            elif "watch_probability" in value:
                record = prediction_record(value, scorer)
                if record:
                    records.append(record)
    return records


def write_records(path, records, append=False):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = "".join(
        json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in records
    )
    if append:
        with target.open("a", encoding="utf-8") as handle:
            handle.write(lines)
        return
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".export-", suffix=".jsonl")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(lines)
    os.replace(tmp, target)


# ---------------------------------------------------------------- CLI


def _log(stats):
    print(json.dumps({"stats": stats}, sort_keys=True, default=str), file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="fetch.py",
        description="YouTube subscription feed fetcher for Throughline (stdlib only). "
        "Credentials are read from environment variables only.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--out", required=True, help="output JSONL file")
        p.add_argument(
            "--append", action="store_true", help="append instead of replacing"
        )
        p.add_argument("--state", help="state JSON file for incremental runs")

    export = sub.add_parser("export", help="fetch feeds (and optionally route and add)")
    common(export)
    export.add_argument(
        "--channels",
        action="append",
        help="channel list file (OPML, JSON, CSV or text); repeatable",
    )
    export.add_argument("--channel-id", action="append", help="channel ID; repeatable")
    export.add_argument("--transport", choices=["rss", "page", "auto"])
    export.add_argument("--channels-per-run", type=int)
    export.add_argument(
        "--details",
        action="store_true",
        help="fetch watch pages for duration/description",
    )
    export.add_argument("--check-shorts", action="store_true")
    export.add_argument(
        "--route", action="store_true", help="score new uploads (paid model calls)"
    )
    export.add_argument("--playlist", help="playlist ID, WL or title for picked videos")
    export.add_argument(
        "--apply", action="store_true", help="really add to the playlist"
    )
    export.add_argument(
        "--import-predictions", action="append", help="youtube-notify predictions.jsonl"
    )
    export.add_argument(
        "--import-flagged", action="append", help="youtube-notify flagged.json"
    )
    export.add_argument("--scorer", help="scorer name for imported predictions")

    router = sub.add_parser("route", help="score uploads from existing export files")
    common(router)
    router.add_argument("--in", dest="inputs", action="append", required=True)

    adder = sub.add_parser(
        "playlist", help="add picks from export files to the playlist"
    )
    common(adder)
    adder.add_argument("--in", dest="inputs", action="append", required=True)
    adder.add_argument("--playlist")
    adder.add_argument("--apply", action="store_true")

    notify = sub.add_parser("import-notify", help="convert youtube-notify files")
    notify.add_argument("--out", required=True)
    notify.add_argument("--append", action="store_true")
    notify.add_argument("--predictions", action="append", default=[])
    notify.add_argument("--flagged", action="append", default=[])
    notify.add_argument("--scorer")

    args = parser.parse_args(argv)
    overrides = {}
    if getattr(args, "channels", None):
        overrides["channels"] = os.pathsep.join(args.channels)
    if getattr(args, "channel_id", None):
        overrides["channel_ids"] = ",".join(args.channel_id)
    for name in ("transport", "playlist"):
        if getattr(args, name, None):
            overrides[name] = getattr(args, name)
    if getattr(args, "channels_per_run", None) is not None:
        overrides["channels_per_run"] = str(args.channels_per_run)
    if getattr(args, "details", False):
        overrides["details"] = "1"
    if getattr(args, "check_shorts", False):
        overrides["check_shorts"] = "1"
    if getattr(args, "route", False):
        overrides["router"] = "1"
    if getattr(args, "scorer", None):
        overrides["import_scorer"] = args.scorer
    cfg = config(**overrides)
    scorer = cfg["import_scorer"]

    if args.command == "import-notify":
        records = read_records(args.predictions, scorer)
        for path in args.flagged:
            records.extend(
                flagged_records(json.loads(Path(path).read_text("utf-8-sig")), scorer)
            )
        write_records(args.out, records, args.append)
        _log({"picks": len(records)})
        return 0

    state = load_state(args.state)
    http = Http(cfg, state)
    started = iso(utcnow())
    stats = {}
    if args.command in ("export", "playlist"):
        check_playlist(cfg, apply=getattr(args, "apply", False) or None)
    if args.command == "export":
        records, stats = collect(cfg, state, http)
        picks, route_stats = route(cfg, state, records, http)
        stats.update(route_stats)
        records.extend(picks)
        imported = read_records(args.import_predictions or [], scorer)
        for path in args.import_flagged or []:
            imported.extend(
                flagged_records(json.loads(Path(path).read_text("utf-8-sig")), scorer)
            )
        records.extend(imported)
        adds, add_stats = playlist(cfg, state, picks, http, apply=args.apply or None)
        records.extend(adds)
        stats.update(add_stats)
    elif args.command == "route":
        records, stats = route(cfg, state, read_records(args.inputs, scorer), http)
    else:
        picks = [
            r for r in read_records(args.inputs, scorer) if r.get("kind") == "pick"
        ]
        records, stats = playlist(cfg, state, picks, http, apply=args.apply or None)
    stats.update(http.stats)
    records.append(
        {
            "kind": "run",
            "id": started,
            "command": args.command,
            "version": VERSION,
            "started_at": started,
            "finished_at": iso(utcnow()),
            "stats": stats,
        }
    )
    write_records(args.out, records, args.append)
    save_state(args.state, state)
    _log(stats)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FetchError, ValueError, LimitReached, OSError) as error:
        print(f"fetch.py: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1) from None
