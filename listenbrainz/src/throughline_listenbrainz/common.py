"""Shared helpers: the media field vocabulary, time parsing and a small
HTTP client with an injectable transport.

The field names, types and meanings follow the unified `media.consumption`
channel (service, event_type, media_type, title, creator, duration_s,
progress, ...), so "what did I watch / listen to / read" reads the same
across every media source.
"""

import hashlib
import importlib
import logging
import time
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)

# Bump when the mapping of an unchanged source item changes.
MAPPING_REVISION = 1

MEDIA_FIELDS = {
    "service": ("text", "Source service"),
    "source_kind": ("text", "Service-specific record kind"),
    "event_type": (
        "text",
        "play, finished, progress, liked, hated, rated, reviewed, library_added, "
        "watchlist_add, shelved, saved or highlight",
    ),
    "media_type": (
        "text",
        "track, episode, movie, show, video, audiobook, book, article or podcast",
    ),
    "title": ("text", "Track, episode, movie, video, book or article title"),
    "creator": ("text", "Artists, author, director or channel, comma separated"),
    "creators": ("json", "Ordered creator names when the source lists them"),
    "album": ("text", "Album of a track"),
    "show": ("text", "TV show or podcast of an episode"),
    "season": ("integer", "Season number of a TV episode"),
    "episode": ("integer", "Episode number of a TV episode"),
    "year": ("integer", "Release or publication year when known"),
    "duration_s": ("number", "Runtime of the work in seconds, not time spent"),
    "position_s": ("number", "Last known playback position in seconds"),
    "progress": ("number", "Fraction of the work completed, 0 to 1"),
    "is_finished": ("boolean", "Source reports the work as finished"),
    "rating": ("number", "The owner's rating on a 0 to 10 scale"),
    "rating_raw": ("text", "The rating as the source shows it (e.g. 4.5/5, 8/10)"),
    "device": ("text", "Device name or model"),
    "client": ("text", "Player application or platform"),
    "account": ("text", "Source account or user name"),
    "library": ("text", "Server library name or ID"),
    "added_at": ("timestamp", "When the work was added to a library or list"),
    "native_id": ("text", "Primary source ID of the work"),
    "native_ids": ("json", "All source and cross-reference IDs (MusicBrainz, IMDb, TMDB, ISBN, ...)"),
    "url": ("text", "Link to the work or entry"),
    "detail": ("json", "Further service-specific values"),
    "timing_basis": ("text", "What happened_at means for this record"),
    "time_precision": ("text", "source_mark, day, progress or first_seen"),
    "source": ("text", "api or rss"),
    "author_kind": (
        "text",
        "owner (the owner's own action or words), agent (model-generated) or other "
        "(someone else's words, e.g. a quoted book author)",
    ),
    "mapping_revision": ("integer", "Mapping revision that produced this message"),
}


def field_list(names, extra=None):
    """Channel field declarations: shared names in order, then extras."""
    out = [
        {"name": n, "type": MEDIA_FIELDS[n][0], "description": MEDIA_FIELDS[n][1]}
        for n in names
    ]
    for name, (kind, description) in (extra or {}).items():
        out.append({"name": name, "type": kind, "description": description})
    return out


def fields_for(declared, **values):
    """Every declared field present (None when unknown), then the values."""
    fields = {f["name"]: None for f in declared}
    unknown = set(values) - set(fields)
    if unknown:
        raise KeyError("undeclared fields: " + ", ".join(sorted(unknown)))
    fields.update(values)
    if "mapping_revision" in fields:
        fields["mapping_revision"] = MAPPING_REVISION
    return fields


def compact(value):
    return {k: v for k, v in value.items() if v not in (None, "", [], {})}


def digest(*parts, length=16):
    text = "\x1f".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(text.encode()).hexdigest()[:length]


def parse_time(value):
    """ISO timestamp, date or unix seconds to an aware UTC datetime, or None."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    text = str(value).strip()
    if text.isdigit():
        return datetime.fromtimestamp(int(text), timezone.utc)
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        text += "T00:00:00+00:00"
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    if moment.year < 1971:
        return None
    return moment.astimezone(timezone.utc)


def iso(moment):
    return moment.astimezone(timezone.utc).isoformat() if moment else None


def number(value, scale=1):
    try:
        return float(value) / scale if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def integer(value):
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def link(kind, source, target):
    return {"type": kind, "from": source, "to": target, "method": "source"}


def load_transport(spec):
    """An httpx transport from "module:factory" (testing hook), else None."""
    if not spec:
        return None
    module, _, attr = spec.partition(":")
    return getattr(importlib.import_module(module), attr or "transport")()


class ApiError(RuntimeError):
    pass


class Http:
    """httpx client with bounded retries on transport errors, 429 and 5xx.

    Never logs request URLs or headers (they can carry credentials)."""

    def __init__(
        self,
        base_url,
        *,
        headers=None,
        transport=None,
        delay_s=0.0,
        retries=4,
        timeout=30.0,
        sleep=time.sleep,
    ):
        self.client = httpx.Client(
            base_url=base_url,
            headers=dict(headers or {}, **{"User-Agent": "throughline-connector/0.1"}),
            transport=transport,
            timeout=timeout,
            follow_redirects=True,
        )
        self.delay_s = float(delay_s or 0)
        self.retries = retries
        self.sleep = sleep
        self.requests = 0

    def close(self):
        self.client.close()

    def request(self, method, path, **kwargs):
        wait = 1.0
        for attempt in range(self.retries + 1):
            if self.delay_s and self.requests:
                self.sleep(self.delay_s)
            self.requests += 1
            try:
                response = self.client.request(method, path, **kwargs)
            except httpx.TransportError as error:
                if attempt == self.retries:
                    raise ApiError(f"{type(error).__name__} contacting {self.client.base_url.host}") from None
                self.sleep(wait)
                wait *= 2
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == self.retries:
                    raise ApiError(f"HTTP {response.status_code} from {self.client.base_url.host}")
                retry_after = response.headers.get("Retry-After") or response.headers.get(
                    "X-RateLimit-Reset-In"
                )
                pause = float(retry_after) if (retry_after or "").isdigit() else wait
                self.sleep(min(pause, 300))
                wait *= 2
                continue
            if response.headers.get("X-RateLimit-Remaining") == "0":
                # ListenBrainz-style budget headers: wait out the window
                # before the next request instead of hitting a 429.
                reset = response.headers.get("X-RateLimit-Reset-In") or ""
                if reset.isdigit():
                    self.sleep(min(float(reset), 300))
            return response
        raise AssertionError("unreachable")

    def get_json(self, path, params=None, headers=None):
        response = self.request("GET", path, params=params, headers=headers)
        if response.status_code >= 400:
            raise ApiError(f"HTTP {response.status_code} for {path}")
        return response.json()
