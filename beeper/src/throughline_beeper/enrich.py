"""Optional, bounded, resumable enrichment of chat messages.

Document attachments become Markdown/CSV text (with page and figure images),
Instagram links become transcripts, frames and images, YouTube links become
transcripts with one frame per minute. Every outcome, success or failure, is
cached by attachment ID / URL hash / video ID; message content and fields
are a pure function of the message and that cache. A rerun with the same
inputs therefore does no repeated work and creates no new versions, and a run
with enrichment switched off still renders what earlier runs cached instead of
reverting those messages.

Pacing (see throughline_media.pacing): each kind has a per-run limit and a
rolling 24-hour limit kept in the same cache file, and by default only
messages sent at or after the time the kind was first enabled are enriched
(the backlog needs ``backlog``). Items held back by either rule are left
untouched (no field, no version) and done on a later run, newest first.
"""

import hashlib
import json
import logging
import os
import re
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from urllib.request import url2pathname

import httpx
from throughline_media import ffmpeg as av
from throughline_media import youtube
from throughline_media.errors import BudgetExhausted
from throughline_media.pacing import (
    Pacing,
    endpoint_allowance,
    instant,
    parse_limit,
    parse_since,
)
from throughline_media.settings import truthy
from throughline_media.transcribe import OpenAICompatible, daily_minutes
from throughline_media.ytdlp import YtDlp, classify_youtube

from . import convert as converters
from . import media
from .assets import Cache, LocalAssetStore, StoreError, default_state_dir

KINDS = ("attachments", "instagram", "youtube")
MINUTES = "transcribe_minutes"  # Deferral reason: the endpoint's minutes.
# kind -> (setting, environment variable, default) of the rolling daily limit.
DAILY = {
    "attachments": ("attachments_per_day", "BEEPER_ATTACHMENTS_PER_DAY", "500"),
    "instagram": ("instagram_per_day", "BEEPER_INSTAGRAM_PER_DAY", "20"),
    "youtube": ("youtube_per_day", "BEEPER_YOUTUBE_PER_DAY", "20"),
}
# Live messages waiting for an allowance; the oldest beyond this are dropped.
QUEUE_MAX = 20_000
EARLIEST = datetime.min.replace(tzinfo=UTC)
MAX_ATTEMPTS = 3
TEXT_FIELD_MAX = 1_000_000
CIRCUIT_BREAKER = 5
INSTAGRAM = re.compile(
    # With a scheme, or bare when not glued to a longer host or path.
    r"(?:\bhttps?://|(?<![\w.@/:-]))(?:www\.|m\.)?(?:instagram\.com|instagr\.am)/"
    r"(?:(?!reels?/|p/|tv/)[A-Za-z0-9_.]{1,30}/)?(reels?|p|tv)/([A-Za-z0-9_-]{5,64})"
    r"(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
# Path words that follow /reel(s)/, /p/ or /tv/ but are pages, not posts.
NOT_SHORTCODES = {"audio", "explore", "liked_by", "tagged", "saved", "embed"}
LABELS = {"reel": "reel", "reels": "reel", "p": "post", "tv": "video"}
UNAVAILABLE = {
    "private": "unavailable (private)",
    "dead": "unavailable (removed or not found)",
    "unavailable": "unavailable (private, removed or login required)",
}


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def instagram_links(text):
    """[(canonical URL, kind, shortcode)] in order of first appearance."""
    seen, result = set(), []
    for match in INSTAGRAM.finditer(text or ""):
        kind = match.group(1).lower()
        kind = "reel" if kind == "reels" else kind
        code = match.group(2)
        if code.lower() in NOT_SHORTCODES:
            continue
        url = f"https://www.instagram.com/{kind}/{code}/"
        if url not in seen:
            seen.add(url)
            result.append((url, kind, code))
    return result


def name_of(att):
    for key in ("fileName", "filename", "file_name", "title", "name"):
        if isinstance(att.get(key), str) and att[key].strip():
            return converters.safe_name(att[key])
    for key in ("url", "srcURL", "src"):
        if isinstance(att.get(key), str):
            tail = urlsplit(att[key]).path.rsplit("/", 1)[-1]
            if tail:
                return converters.safe_name(tail)
    return "file"


def mime_of(att):
    for key in (
        "mimeType",
        "mime_type",
        "content_type",
        "contentType",
        "original_content_type",
    ):
        if isinstance(att.get(key), str) and att[key]:
            return att[key]
    return None


def size_of(att):
    for key in ("fileSizeBytes", "fileSize", "size", "file_size"):
        value = att.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value)
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def urls_of(att):
    result = []
    for key in ("srcURL", "url", "proxy_url", "proxyURL", "src"):
        value = att.get(key)
        if isinstance(value, str) and value.strip() and value.strip() not in result:
            result.append(value.strip())
    return result


def attachment_key(net, external_id, index, att):
    ident = att.get("id")
    if isinstance(ident, (str, int)) and not isinstance(ident, bool) and str(ident):
        basis = ["id", net, str(ident)]
    else:
        urls = urls_of(att)
        if urls:
            parts = urlsplit(urls[0])
            # Discord CDN signatures live in the query and change on refresh.
            basis = ["url", urlunsplit(parts._replace(query="", fragment=""))]
        else:
            basis = ["message", external_id, index, name_of(att)]
    return "att:" + digest(basis)


class Unavailable(Exception):
    def __init__(self, status, reason):
        super().__init__(reason)
        self.status = status


class Transient(Exception):
    pass


def clip(text, limit):
    """Text cut to at most `limit` UTF-8 bytes at a character boundary."""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[: max(limit, 0)].decode("utf-8", "ignore")


def size(text):
    return len(text.encode("utf-8"))


class Enricher:
    def __init__(
        self,
        settings,
        secrets=None,
        *,
        dry_run=False,
        desktop=None,
        live=False,
        transport=None,
        instagram=None,
        transcriber=None,
        store=None,
        youtube_fetcher=None,
    ):
        self.settings, self.secrets = settings, secrets or {}
        self.daily = {
            kind: parse_limit(
                default if settings.get(name) is None else settings[name], env
            )
            for kind, (name, env, default) in DAILY.items()
        }
        # A daily limit of 0 switches the kind off.
        self.attachments_on = (
            truthy(settings.get("attachments"))
            and not dry_run
            and self.daily["attachments"] != 0
        )
        self.instagram_on = (
            truthy(settings.get("instagram_links"))
            and not dry_run
            and self.daily["instagram"] != 0
        )
        self.youtube_on = (
            truthy(settings.get("youtube_links"))
            and not dry_run
            and self.daily["youtube"] != 0
        )
        state = Path(settings.get("state_dir") or default_state_dir())
        self.store = store or LocalAssetStore(
            settings.get("asset_dir") or state / "assets",
            settings.get("asset_base_url"),
        )
        working = self.attachments_on or self.instagram_on or self.youtube_on
        self.cache = Cache(state / "enrichment.sqlite3", readonly=not working)
        self.pacing = Pacing(self.cache.conn, writable=working)
        # The endpoint's daily minutes: one pool shared with other plugins.
        self.minutes = endpoint_allowance(
            settings, daily_minutes(settings), writable=working
        )
        self.backlog = truthy(settings.get("backlog"))
        explicit = parse_since(settings.get("enrich_since"), "BEEPER_ENRICH_SINCE")
        self.since = {
            kind: self.pacing.since(kind, explicit)
            for kind in KINDS
            if self.enabled(kind)
        }
        self.planned = None  # {kind: keys} once plan() reserved the allowance.
        self.deferred = {kind: set() for kind in (*KINDS, MINUTES)}
        self.backlogged = {kind: set() for kind in KINDS}
        self.exhausted = set()  # Kinds whose daily allowance ran out this run.
        self.last = {"deferred": False, "changed": False}
        self.queue_ids = set()
        if working:
            self.cache.conn.execute(
                "CREATE TABLE IF NOT EXISTS revisit (external_id TEXT PRIMARY KEY,"
                " at REAL NOT NULL, chat_id TEXT NOT NULL, row TEXT NOT NULL)"
            )
            self.cache.conn.execute(
                "CREATE TABLE IF NOT EXISTS revisit_chats (chat_id TEXT PRIMARY KEY,"
                " chat TEXT NOT NULL, account TEXT NOT NULL)"
            )
            self.cache.conn.commit()
            self.queue_ids = {
                r[0] for r in self.cache.conn.execute("SELECT external_id FROM revisit")
            }
        self.limits = {
            "attachments": int(settings.get("attachment_limit") or 200),
            "instagram": int(settings.get("instagram_limit") or 25),
            "youtube": int(settings.get("youtube_limit") or 10),
        }
        self.max_bytes = int(settings.get("attachment_max_bytes") or 25 * 1024 * 1024)
        self.max_frames = int(settings.get("instagram_max_frames") or 200)
        self.desktop, self.live = desktop, live
        self.transport = transport
        self._http = None
        self._instagram = instagram
        self._transcriber = transcriber
        self._youtube_fetcher = youtube_fetcher
        self._youtube = None
        self.used = Counter()
        self.stats = Counter()
        self.transient_streak = 0

    @property
    def active(self):
        return self.cache.conn is not None

    def close(self):
        self.cache.close()
        self.minutes.close()
        if self._http is not None:
            self._http.close()

    @property
    def http(self):
        if self._http is None:
            self._http = httpx.Client(
                timeout=60,
                follow_redirects=True,
                headers={"User-Agent": "throughline-beeper"},
                transport=self.transport,
            )
        return self._http

    @property
    def instagram(self):
        if self._instagram is None:
            self._instagram = media.YtDlp(
                cookies=self.settings.get("ytdlp_cookies"),
                cookies_from_browser=self.settings.get("ytdlp_cookies_from_browser"),
                max_bytes=int(
                    self.settings.get("instagram_max_bytes") or 200 * 1024 * 1024
                ),
                max_seconds=int(self.settings.get("instagram_max_seconds") or 1800),
            )
        return self._instagram

    @property
    def transcriber(self):
        if self._transcriber is None:
            self._transcriber = media.transcriber_from(self.settings, self.secrets)
        if (
            isinstance(self._transcriber, OpenAICompatible)
            and self._transcriber.daily is None
            and self.pacing.writable
        ):
            self._transcriber.daily = self.minutes  # Minutes in any 24 hours.
        return self._transcriber

    @property
    def youtube(self):
        """The shared per-video processor: transcript plus frames."""
        if self._youtube is None:
            settings = self.settings
            fetcher = self._youtube_fetcher or youtube.YouTubeFetcher(
                YtDlp(
                    cookies=settings.get("ytdlp_cookies"),
                    cookies_from_browser=settings.get("ytdlp_cookies_from_browser"),
                    classify=classify_youtube,
                )
            )
            langs = [
                lang.strip()
                for lang in (settings.get("youtube_caption_langs") or "en").split(",")
                if lang.strip()
            ]
            self._youtube = youtube.Processor(
                fetcher,
                self.store,
                self.transcriber,
                transcripts=True,
                frames=True,
                frame_every=int(settings.get("youtube_frame_seconds") or 60),
                source=(settings.get("youtube_transcript_source") or "auto").lower(),
                max_seconds=int(settings.get("youtube_max_seconds") or 7200),
                caption_langs=langs,
                max_bytes=int(
                    settings.get("youtube_max_bytes") or youtube.MAX_DOWNLOAD_BYTES
                ),
            )
        return self._youtube

    # Entry point ---------------------------------------------------------------

    def __call__(self, net, external_id, body, attachments, budget, at=None):
        """{sections, fields} for one message, or None when nothing applies.

        `at` is the message's time (ISO); without it the new-inbound rule
        does not apply. Afterwards `last` says whether this call did new work
        (`changed`) and whether something was held back by a limit
        (`deferred`), which live sync uses to revisit the message later.
        """
        self.last = {"deferred": False, "changed": False}
        if not self.active:
            return None
        timed = at is not None
        when = instant(at)
        entries = []
        for index, att in enumerate(attachments or []):
            if not isinstance(att, dict):
                continue
            name = name_of(att)
            kind = converters.candidate(name, mime_of(att))
            if kind is None:
                continue
            key = attachment_key(net, external_id, index, att)
            result = self.cache.get(key)
            if (
                self.attachments_on
                and self.attachment_needs_work(result)
                and self.admit("attachments", key, when, timed)
            ):
                # Outcomes known without a download cost nothing from the limit.
                fresh = self.precheck(att, name, kind)
                if fresh is None and self.may("attachments", key):
                    self.used["attachments"] += 1
                    fresh = self.guarded(
                        self.process_attachment, att, name, kind, result
                    )
                    fresh = {
                        "name": name,
                        "kind": kind,
                        "revision": converters.REVISION,
                        **fresh,
                    }
                if fresh is not None:
                    result = fresh
                    self.cache.put(key, result)
                    self.last["changed"] = True
                    self.stats["attachments_" + result["status"]] += 1
            if result and not self.pending(result):
                entries.append(("attachment_files", self.attachment_view(result)))
        for url, kind, code in instagram_links(body):
            key = "ig:" + digest(url)
            result = self.cache.get(key)
            if (
                self.instagram_on
                and self.link_needs_work(result)
                and self.admit("instagram", key, when, timed)
            ):
                if self.minutes_blocked():
                    self.defer(MINUTES, key)  # A reel would wait for its transcript.
                elif self.may("instagram", key):
                    result = {
                        "url": url,
                        "kind": kind,
                        "shortcode": code,
                        **self.guarded(self.process_link, url, kind, code, result),
                    }
                    self.used["instagram"] += 1
                    self.cache.put(key, result)
                    self.last["changed"] = True
                    self.stats["instagram_" + result["status"]] += 1
            elif (
                self.instagram_on
                and self.link_needs_transcript(result)
                and self.admit("instagram", key, when, timed)
                and self.may("instagram", key, fetch=False)
            ):
                fresh = self.retranscribe(result)
                self.used["instagram"] += 1
                if self.waits_for_minutes(fresh) and not self.pending(result):
                    # Already shown: it stays as it is until the minutes free up
                    # (no field or version change); then it is redone.
                    self.defer(MINUTES, key)
                elif fresh != result:
                    result = fresh
                    self.cache.put(key, result)
                    self.last["changed"] = True
            if result and self.instagram_on and self.waits_for_minutes(result):
                self.defer(MINUTES, key)
            if result and not self.pending(result):
                entries.append(("instagram_links", self.link_view(result)))
        for url, kind, video in youtube.youtube_links(body):
            key = "yt:" + video
            result = self.cache.get(key)
            if (
                self.youtube_on
                and self.youtube.wanted_parts(result)
                and self.admit("youtube", key, when, timed)
            ):
                if not self.youtube.todo(result):
                    self.defer(MINUTES, key)  # The transcript waits for minutes.
                elif self.may("youtube", key):
                    self.used["youtube"] += 1
                    fresh = self.guarded(self.youtube.process, video, result)
                    if fresh is not None and fresh != result:
                        result = fresh
                        self.cache.put(key, result)
                        self.last["changed"] = True
                        self.stats["youtube_" + result["status"]] += 1
                    if self.youtube.transient_streak >= CIRCUIT_BREAKER:
                        logging.warning(
                            "Beeper YouTube paused for this run after repeated failures"
                        )
                        self.youtube_on = False
            if self.youtube_on and result and result.get("status") == "deferred":
                self.defer(MINUTES, key)
            if result and not youtube.pending(result):
                entries.append(
                    ("youtube_links", self.youtube_view(result, url, kind, video))
                )
        if not entries:
            return None
        return self.render(entries, budget)

    @staticmethod
    def pending(result):
        """A transient failure that will be retried is not shown yet.

        Its attempt count changes on every retry; rendering it would give the
        message a new version per attempt. Only final outcomes are rendered.
        A post whose transcript waits for the transcription allowance is not
        final either.
        """
        if result.get("status") == "error" and result.get("attempts", 0) < MAX_ATTEMPTS:
            return True
        return Enricher.waits_for_minutes(result)

    @staticmethod
    def waits_for_minutes(result):
        return any(
            item.get("transcript_status") == "deferred"
            for item in result.get("items") or []
            if isinstance(item, dict)
        )

    # Pacing --------------------------------------------------------------------

    def enabled(self, what):
        return {
            "attachments": self.attachments_on,
            "instagram": self.instagram_on,
            "youtube": self.youtube_on,
        }[what]

    @property
    def working(self):
        return self.pacing.writable and any(self.enabled(k) for k in KINDS)

    def admit(self, what, key, when, timed=True):
        """New inbound only: items older than the kind's enable time wait.

        They are counted (not marked) and done only with the backlog switch.
        """
        if self.backlog or not timed:
            return True
        since = self.since.get(what)
        if since is not None and when is not None and when >= since:
            return True
        self.backlogged[what].add(key)
        return False

    def defer(self, what, key):
        self.deferred[what].add(key)
        self.last["deferred"] = True
        return False

    def may(self, what, key=None, fetch=True):
        """Per-run limit, this run's plan, then the rolling daily limit.

        A network fetch is counted against the daily limit when attempted.
        """
        if not self.enabled(what):
            return False
        if fetch and self.planned is not None and key not in self.planned[what]:
            return self.defer(what, key)
        if self.used[what] >= self.limits[what]:
            return self.defer(what, key)
        if fetch:
            if what in self.exhausted or not self.pacing.take(what, self.daily[what]):
                self.exhausted.add(what)
                return self.defer(what, key)
        return True

    def minutes_blocked(self):
        """The transcription endpoint has no minutes left (this run or day)."""
        transcriber = self.transcriber
        remaining = transcriber.remaining_s()
        return transcriber.name != "none" and remaining is not None and remaining <= 0

    def has_allowance(self):
        return any(
            self.enabled(k)
            and self.used[k] < self.limits[k]
            and k not in self.exhausted
            for k in KINDS
        )

    def due(self, net, external_id, body, attachments):
        """(kind, key) of this message's items that need a network fetch."""
        if self.attachments_on:
            for index, att in enumerate(attachments or []):
                if not isinstance(att, dict):
                    continue
                name = name_of(att)
                kind = converters.candidate(name, mime_of(att))
                if kind is None:
                    continue
                key = attachment_key(net, external_id, index, att)
                if (
                    self.attachment_needs_work(self.cache.get(key))
                    and self.precheck(att, name, kind) is None
                ):
                    yield "attachments", key
        if self.instagram_on:
            for url, _, _ in instagram_links(body):
                key = "ig:" + digest(url)
                if self.link_needs_work(self.cache.get(key)):
                    yield "instagram", key
        if self.youtube_on:
            for _, _, video in youtube.youtube_links(body):
                key = "yt:" + video
                if self.youtube.todo(self.cache.get(key)):
                    yield "youtube", key

    def plan(self, candidates):
        """Reserve this run's allowance for the newest due items first.

        `candidates` yields (time, network, external ID, body, attachments).
        Used where every message is streamed on every run (the archive) and
        stream order is not time order; without a plan, items are taken in
        stream order.
        """
        if not self.working:
            return
        due = []
        for at, net, external_id, body, attachments in candidates:
            when = instant(at)
            for what, key in self.due(net, external_id, body, attachments):
                if self.admit(what, key, when):
                    due.append((when or EARLIEST, what, key))
        due.sort(key=lambda item: item[0], reverse=True)
        room = {}
        for kind in KINDS:
            left = self.pacing.remaining(kind, self.daily[kind])
            room[kind] = self.limits[kind] - self.used[kind]
            if left is not None:
                room[kind] = min(room[kind], int(left))
        planned = {kind: set() for kind in KINDS}
        for _, what, key in due:
            if key not in planned[what] and len(planned[what]) < room[what]:
                planned[what].add(key)
        self.planned = planned

    def report(self):
        """Deferred and backlog counts, and when each daily window frees up."""
        out = {}
        deferred = {k: len(v) for k, v in self.deferred.items() if v}
        if deferred:
            out["deferred"] = deferred
        until = {}
        for kind in deferred:
            if kind == MINUTES:
                moment = self.minutes.frees_at(60)
            else:
                moment = self.pacing.frees_at(kind, self.daily[kind])
            if moment:
                until[kind] = moment
        if until:
            out["deferred_until"] = until
        backlog = {k: len(v) for k, v in self.backlogged.items() if v}
        if backlog:
            out["backlog_skipped"] = backlog
        return out

    # Live revisits -------------------------------------------------------------

    def remember(self, external_id, at, row, chat, account):
        """Queue a live message whose enrichment a limit held back.

        Live sync never reads an old message again; the queue (in the cache
        file) lets a later run finish it, newest first.
        """
        if not self.pacing.writable:
            return
        conn = self.cache.conn
        stamp = (instant(at) or EARLIEST).timestamp() if at else 0.0
        conn.execute(
            "INSERT INTO revisit_chats (chat_id, chat, account) VALUES (?, ?, ?)"
            " ON CONFLICT (chat_id) DO UPDATE SET chat=excluded.chat,"
            " account=excluded.account",
            (str(chat.get("id")), json.dumps(chat), json.dumps(account)),
        )
        conn.execute(
            "INSERT INTO revisit (external_id, at, chat_id, row) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (external_id) DO UPDATE SET at=excluded.at,"
            " chat_id=excluded.chat_id, row=excluded.row",
            (external_id, stamp, str(chat.get("id")), json.dumps(row)),
        )
        self.queue_ids.add(external_id)
        if len(self.queue_ids) > QUEUE_MAX:
            dropped = [
                r[0]
                for r in conn.execute(
                    "SELECT external_id FROM revisit ORDER BY at LIMIT ?",
                    (len(self.queue_ids) - QUEUE_MAX,),
                )
            ]
            conn.executemany(
                "DELETE FROM revisit WHERE external_id=?", [(i,) for i in dropped]
            )
            self.queue_ids.difference_update(dropped)
        conn.commit()

    def forget(self, external_ids):
        gone = [i for i in external_ids if i in self.queue_ids]
        if not gone or not self.pacing.writable:
            return
        self.cache.conn.executemany(
            "DELETE FROM revisit WHERE external_id=?", [(i,) for i in gone]
        )
        self.cache.conn.commit()
        self.queue_ids.difference_update(gone)

    def queued(self):
        """[(external ID, row, chat, account)] newest first."""
        if not self.pacing.writable or not self.queue_ids:
            return []
        rows = self.cache.conn.execute(
            "SELECT r.external_id, r.row, c.chat, c.account FROM revisit r"
            " JOIN revisit_chats c ON c.chat_id = r.chat_id"
            " ORDER BY r.at DESC, r.external_id"
        ).fetchall()
        return [(i, json.loads(r), json.loads(c), json.loads(a)) for i, r, c, a in rows]

    def guarded(self, function, *args):
        try:
            return function(*args)
        except StoreError:
            raise  # Out of disk or permissions: stop the run, cache nothing.
        except Exception as error:  # an enrichment bug never drops the message
            logging.warning("Beeper enrichment failed: %s", type(error).__name__)
            return {"status": "failed", "error": type(error).__name__}

    # Attachments ---------------------------------------------------------------

    @staticmethod
    def attachment_needs_work(result):
        if result is None:
            return True
        if result.get("status") == "error":
            return result.get("attempts", 0) < MAX_ATTEMPTS
        if result.get("status") in ("converted", "failed"):
            return result.get("revision") != converters.REVISION
        return False

    def precheck(self, att, name, kind):
        base = {"name": name, "kind": kind, "revision": converters.REVISION}
        declared = size_of(att)
        if declared is not None and declared > self.max_bytes:
            return dict(base, status="too_large", bytes=declared)
        if not urls_of(att):
            return dict(
                base,
                status="no_source",
                error="the source keeps no file for this attachment",
            )
        return None

    def process_attachment(self, att, name, kind, previous):
        base = {"name": name, "kind": kind, "revision": converters.REVISION}
        try:
            data = self.fetch(att)
        except Unavailable as error:
            return dict(base, status=error.status, error=str(error))
        except Transient as error:
            attempts = (previous or {}).get("attempts", 0) + 1
            return dict(base, status="error", error=str(error), attempts=attempts)
        actual = converters.sniff(data, kind)
        if actual is None:
            return dict(
                base, status="unsupported", error="content is not a supported document"
            )
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else actual
        original = self.store.put(data, ext if ext in converters.EXTENSIONS else actual)
        base.update(
            kind=actual,
            file=original["link"],
            sha256=original["sha256"],
            bytes=len(data),
        )
        try:
            converted = converters.convert(name, actual, data, self.store)
        except converters.Encrypted as error:
            return dict(base, status="encrypted", error=str(error))
        except converters.ConversionError as error:
            return dict(base, status="failed", error=str(error))
        except StoreError:
            raise
        except Exception as error:
            logging.warning("Beeper conversion failed: %s", type(error).__name__)
            return dict(base, status="failed", error=type(error).__name__)
        return dict(base, status="converted", **converted)

    def fetch(self, att):
        urls = urls_of(att)
        if not urls:
            raise Unavailable(
                "no_source", "the source keeps no file for this attachment"
            )
        failure = None
        for url in urls:
            try:
                return self.read(url)
            except Transient as error:
                failure = error
            except Unavailable as error:
                if not isinstance(failure, Transient):
                    failure = error
        raise failure

    def read(self, url):
        scheme = urlsplit(url).scheme.lower()
        if scheme in ("mxc", "localmxc"):
            if self.desktop is None:
                raise Unavailable(
                    "no_source", "Matrix asset needs the Beeper Desktop API"
                )
            try:
                local = self.desktop.download_asset(url)
            except (RuntimeError, httpx.HTTPError):
                raise Transient("Desktop asset download failed") from None
            if not local:
                raise Unavailable("unavailable", "Desktop could not download the asset")
            url, scheme = local, urlsplit(local).scheme.lower()
        if scheme == "file" or len(scheme) == 1 or (not scheme and os.path.isabs(url)):
            if not self.live:
                raise Unavailable(
                    "no_source", "local paths are trusted only from the Desktop API"
                )
            path = (
                Path(url2pathname(urlsplit(url).path))
                if scheme == "file"
                else Path(url)
            )
            try:
                if path.stat().st_size > self.max_bytes:
                    raise Unavailable("too_large", "file exceeds the size cap")
                return path.read_bytes()
            except OSError:
                raise Unavailable("unavailable", "local file missing") from None
        if scheme not in ("http", "https"):
            raise Unavailable("no_source", "unsupported URL scheme")
        try:
            with self.http.stream("GET", url) as response:
                if response.status_code in (401, 403, 404, 410):
                    raise Unavailable(
                        "unavailable",
                        f"expired or removed (HTTP {response.status_code})",
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    raise Transient(f"HTTP {response.status_code}")
                if response.is_error:
                    raise Unavailable("unavailable", f"HTTP {response.status_code}")
                chunks, total = [], 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > self.max_bytes:
                        raise Unavailable("too_large", "file exceeds the size cap")
                    chunks.append(chunk)
                return b"".join(chunks)
        except httpx.HTTPError:
            raise Transient("download failed") from None

    def attachment_view(self, result):
        field = {
            k: result[k]
            for k in (
                "name",
                "kind",
                "status",
                "file",
                "sha256",
                "bytes",
                "pages",
                "renderer",
                "error",
                "attempts",
            )
            if result.get(k) is not None
        }
        if result.get("assets"):
            field["assets"] = result["assets"]
        sections = result.get("sections") or []
        return {"field": field, "sections": sections}

    # Instagram -----------------------------------------------------------------

    @staticmethod
    def link_needs_work(result):
        if result is None:
            return True
        return (
            result.get("status") == "error" and result.get("attempts", 0) < MAX_ATTEMPTS
        )

    def link_needs_transcript(self, result):
        if not result or result.get("status") not in ("video", "images"):
            return False
        if not self.instagram_on:
            return False
        if self.waits_for_minutes(result):
            # Waiting for minutes; without a transcriber any more, it is final.
            return self.transcriber.name == "none" or not self.minutes_blocked()
        pending = any(
            i.get("audio")
            and (
                i.get("transcript_status") == "unavailable"
                or (i.get("transcript_status") == "over_budget" and self.fits_now(i))
            )
            for i in result.get("items", [])
        )
        return pending and self.transcriber.name != "none"

    def process_link(self, url, kind, code, previous):
        base = {"url": url, "kind": kind, "shortcode": code}
        attempts = (previous or {}).get("attempts", 0) + 1
        try:
            with tempfile.TemporaryDirectory(prefix="beeper-ig-") as tmp:
                fetched = self.instagram.fetch(url, tmp)
                items = [
                    self.store_item(index, item, Path(tmp))
                    for index, item in enumerate(fetched, 1)
                ]
        except media.MediaUnavailable as error:
            self.transient_streak = 0
            return dict(base, status=error.status, error=str(error))
        except media.MediaTransient as error:
            self.transient_streak += 1
            if self.transient_streak >= CIRCUIT_BREAKER:
                logging.warning(
                    "Beeper Instagram paused for this run after repeated failures"
                )
                self.instagram_on = False
            if attempts >= MAX_ATTEMPTS:
                return dict(
                    base, status="unavailable", error=str(error), attempts=attempts
                )
            return dict(base, status="error", error=str(error), attempts=attempts)
        self.transient_streak = 0
        status = "video" if any(i["type"] == "video" for i in items) else "images"
        return dict(base, status=status, items=items)

    def store_item(self, index, item, tmp):
        if item["type"] == "image":
            if item.get("skipped"):
                return {"type": "image", "index": index, "skipped": item["skipped"]}
            path = Path(item["path"])
            asset = self.store.put(path.read_bytes(), path.suffix.lstrip(".") or "jpg")
            return {
                "type": "image",
                "index": index,
                "link": asset["link"],
                "sha256": asset["sha256"],
            }
        out = {"type": "video", "index": index}
        if item.get("duration"):
            out["duration_s"] = round(float(item["duration"]), 3)
        if item.get("skipped"):
            out["skipped"] = item["skipped"]
            return out
        framedir = tmp / f"frames{index}"
        framedir.mkdir()
        out["frames"] = []
        for at, path in media.frames(item["path"], framedir, limit=self.max_frames):
            asset = self.store.put(path.read_bytes(), "jpg")
            out["frames"].append({"at_s": at, "link": asset["link"]})
        sound = media.audio(item["path"], framedir)
        if sound is None:
            out["transcript_status"] = "no_audio"
            return out
        asset = self.store.put(sound.read_bytes(), sound.suffix.lstrip("."))
        out["audio"] = asset["link"]
        out.update(self.transcribe(sound))
        return out

    def transcribe(self, path):
        transcriber = self.transcriber
        seconds = self.too_long_to_transcribe(path)
        if seconds is not None:
            # Could never fit the allowance: final until the allowance is raised.
            return {
                "transcript_status": "over_budget",
                "transcriber": transcriber.name,
                "duration_s": seconds,
            }
        try:
            text = transcriber.transcribe(path)
        except BudgetExhausted:
            # The minutes (this run or day) ran out: not shown until done.
            return {"transcript_status": "deferred", "transcriber": transcriber.name}
        except media.MediaTransient:
            text = None
        except Exception as error:
            logging.warning("Beeper transcription failed: %s", type(error).__name__)
            return {"transcript_status": "failed", "transcriber": transcriber.name}
        if text is None:
            return {"transcript_status": "unavailable", "transcriber": transcriber.name}
        text = converters.collapse(converters.clean(text))
        return {
            "transcript_status": "ok" if text else "empty",
            "transcriber": transcriber.name,
            "transcript": text,
        }

    def capacity_s(self):
        """Most audio seconds one item may take (per run or day), or None."""
        capacity = getattr(self.transcriber, "capacity_s", None)
        return capacity() if capacity else None

    def too_long_to_transcribe(self, path):
        """The audio's seconds when it exceeds the allowance's capacity, else None."""
        capacity = self.capacity_s()
        if capacity is None:
            return None
        seconds = av.duration(path)
        if seconds and seconds > capacity:
            return round(seconds, 3)
        return None

    def fits_now(self, item):
        """An over-allowance item is due again once the allowance was raised."""
        capacity = self.capacity_s()
        return capacity is None or capacity >= (item.get("duration_s") or 0)

    def retranscribe(self, result):
        result = json.loads(json.dumps(result))
        for item in result.get("items", []):
            status = item.get("transcript_status")
            if status == "over_budget" and not self.fits_now(item):
                continue
            if status not in ("unavailable", "deferred", "over_budget"):
                continue
            if not item.get("audio"):
                continue
            path = self.store.path(item["audio"])
            if path is not None:
                item.update(self.transcribe(path))
            elif status == "deferred":
                item["transcript_status"] = "unavailable"  # The audio is gone.
        return result

    def link_view(self, result):
        label = f"instagram {LABELS.get(result.get('kind'), 'post')} {result.get('shortcode', '')}".strip()
        field = {
            k: result[k]
            for k in ("url", "kind", "shortcode", "status", "error", "attempts")
            if result.get(k) is not None
        }
        status = result.get("status")
        if status in UNAVAILABLE:
            return {
                "field": field,
                "sections": [
                    {
                        "header": f"attachment {label}: {UNAVAILABLE[status]}",
                        "parts": [],
                    }
                ],
            }
        if status not in ("video", "images"):
            return {"field": field, "sections": []}  # retried later; nothing to show
        items = result.get("items", [])
        many = len(items) > 1
        sections, views = [], []
        for item in items:
            view = {k: v for k, v in item.items() if k != "transcript"}
            views.append(view)
            name = f"{label} item {item['index']}" if many else label
            if item.get("skipped"):
                reason = (
                    "longer than the duration cap"
                    if item["skipped"] == "too_long"
                    else "larger than the size cap"
                )
                sections.append(
                    {
                        "header": f"attachment {name}: {item['type']} not processed ({reason})",
                        "parts": [],
                    }
                )
                continue
            if item["type"] == "image":
                sections.append(
                    {
                        "header": None,
                        "parts": [
                            [
                                "link",
                                f"attachment {label} image {item['index']}: {item['link']}",
                            ]
                        ],
                    }
                )
                continue
            transcript = {
                "ok": item.get("transcript"),
                "empty": "(no speech)",
                "no_audio": "(no audio)",
                "failed": "transcript unavailable (transcription failed)",
                "over_budget": "transcript unavailable (longer than the transcription minutes allowed per run or day)",
            }.get(item.get("transcript_status"), "transcript unavailable")
            parts = [["text", transcript or "transcript unavailable"]]
            if item.get("audio"):
                parts.append(["link", f"attachment {name} audio: {item['audio']}"])
            for frame in item.get("frames", []):
                minutes, seconds = divmod(int(frame["at_s"]), 60)
                parts.append(
                    [
                        "link",
                        f"attachment {name} frame {minutes}:{seconds:02} image: {frame['link']}",
                    ]
                )
            sections.append(
                {"header": f"attachment {name}: video transcript", "parts": parts}
            )
        field["items"] = views
        return {"field": field, "sections": sections}

    # YouTube -------------------------------------------------------------------

    @staticmethod
    def youtube_view(result, url, kind, video):
        label = f"youtube {video}"
        meta = result.get("meta") or {}
        field = {"url": url, "kind": kind, "video_id": video}
        if meta.get("title"):
            field["title"] = meta["title"]
        if meta.get("duration"):
            field["duration_s"] = meta["duration"]
        field["status"] = result.get("status")
        if result.get("error"):
            field["error"] = result["error"]
        status = result.get("status")
        header = None
        if status in youtube.UNAVAILABLE:
            header = f"{label}: {youtube.UNAVAILABLE[status]}"
        elif status == "too_long":
            header = f"{label}: not processed (longer than the duration cap)"
        elif status != "ok":
            header = f"{label}: not processed (enrichment failed)"
        if header:
            return {"field": field, "sections": [{"header": header, "parts": []}]}
        part = result.get("transcript") or {}
        for name in (
            "source",
            "caption_kind",
            "language",
            "transcriber",
            "model",
            "timestamps",
        ):
            if part.get(name) is not None:
                key = "transcript_source" if name == "source" else name
                field[key] = part[name]
        if part.get("status"):
            field["transcript_status"] = part["status"]
        cues = youtube.cues_of(result)
        frames = youtube.frames_of(result)
        lines = [
            f"[{youtube.clock(w['start'])}] {w['text']}"
            for w in youtube.windows(cues, [], meta.get("duration"))
            if w["text"]
        ]
        text = "\n".join(lines) or (
            "transcript unavailable ("
            + youtube.TRANSCRIPT_STATUS.get(part.get("status"), "not requested")
            + ")"
        )
        field["transcript"] = text[:TEXT_FIELD_MAX]
        if frames:
            field["frames"] = [{"at_s": f["at_s"], "link": f["link"]} for f in frames]
        parts = [["text", text]]
        for frame in frames:
            parts.append(
                [
                    "link",
                    f"{label} frame {youtube.clock(frame['at_s'])}: {frame['link']}",
                ]
            )
        return {
            "field": field,
            "sections": [{"header": f"{label}: transcript", "parts": parts}],
        }

    # Rendering -----------------------------------------------------------------

    def render(self, entries, budget):
        """Fit every entry's text into `budget` UTF-8 bytes; links always stay.

        Text is shared fairly between entries; a cut entry gets a marker and
        its full text in the field.
        """
        fixed, wants = 0, []
        for _, view in entries:
            want = 0
            for section in view["sections"]:
                if section["header"]:
                    fixed += size(section["header"]) + 1
                for kind, value in section["parts"]:
                    if kind == "link":
                        fixed += size(value) + 1
                    else:
                        want += size(value) + 1
                fixed += 2
            wants.append(want)
        allowance = self.share(wants, budget - fixed - 200 * len(entries))
        sections_out, fields = [], {}
        for (field_name, view), want, allowed in zip(
            entries, wants, allowance or [0] * len(entries)
        ):
            field = dict(view["field"])
            remaining, full = allowed, []
            for section in view["sections"]:
                lines = [section["header"]] if section["header"] else []
                full.extend(lines)
                for kind, value in section["parts"]:
                    full.append(value)
                    if kind == "link":
                        lines.append(value)
                    elif remaining > 0:
                        shown = clip(value, remaining - 1)
                        if shown:
                            lines.append(shown)
                            remaining -= size(shown) + 1
                if lines:
                    sections_out.append("\n".join(lines))
            if want > allowed:
                sections_out.append(
                    f"[truncated: {allowed:,} of {want:,} bytes of text shown;"
                    f" full text in field {field_name}]"
                )
                text = "\n".join(full)
                field["truncated"] = True
                field["text"] = text[:TEXT_FIELD_MAX]
                if len(text) > TEXT_FIELD_MAX:
                    field["text_truncated"] = True
            fields.setdefault(field_name, []).append(field)
        if allowance is None:
            sections_out = []  # Not even the asset lines fit; fields keep all.
        return {"sections": sections_out, "fields": fields}

    @staticmethod
    def share(wants, available):
        """Water-filling: small entries get everything, large ones split the rest."""
        if available < 0:
            return None
        allowance = [0] * len(wants)
        pending = sorted(range(len(wants)), key=lambda i: wants[i])
        while pending:
            fair = available // len(pending)
            index = pending.pop(0)
            allowance[index] = min(wants[index], fair)
            available -= allowance[index]
        return allowance
