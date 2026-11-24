"""Browser history (Chromium family, Firefox, Safari) and bookmarks."""

from __future__ import annotations

import base64
import datetime as dt
import gzip
import json
import logging
import os
import re
import socket
from pathlib import Path
from zoneinfo import ZoneInfo

from throughline_sdk import (
    Channel,
    Checkpoint,
    Declaration,
    Manifest,
    Plugin,
    coverage,
    run,
)

from . import readers
from .privacy import DEFAULT_EXCLUDE, DEFAULT_REDACT, Privacy, domain
from .profiles import BROWSERS, discover, explicit

MAPPING_REVISION = 1
VISITS = "browser_history.visits"
BOOKMARKS = "browser_history.bookmarks"
LOG = logging.getLogger("throughline_browser_history")
TITLES = {"chrome": "Chrome", "chromium": "Chromium", "brave": "Brave", "edge": "Edge", "vivaldi": "Vivaldi",
          "firefox": "Firefox", "librewolf": "LibreWolf", "safari": "Safari"}


def fields(*items):
    return [dict(name=name, type=kind, description=description) for name, kind, description in items]


def setting(env, default, description):
    return Declaration(env=env, required=False, default=default, description=description)


def flag(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def words(value):
    return [part.strip() for part in re.split(r"[,\s]+", value or "") if part.strip()]


def pack(value):
    raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    return base64.b64encode(gzip.compress(raw, mtime=0)).decode("ascii")


def unpack(text):
    return json.loads(gzip.decompress(base64.b64decode(text))) if text else {}


class BrowserHistory(Plugin):
    manifest = Manifest(
        name="browser_history",
        version="0.1.0",
        run_mode="poll",
        default_interval_s=900,
        channels=[
            Channel(
                name=VISITS,
                description=(
                    "Pages visited in Chrome, Chromium, Brave, Edge, Vivaldi, Firefox, LibreWolf and Safari on "
                    "this device: one record per visit, title and URL as content; threads are browser-profile days"
                ),
                record_kind="life_event",
                embed=False,
                fields=fields(
                    ("url", "text", "Visited URL (userinfo dropped, sensitive parameters redacted)"),
                    ("title", "text", "Page title known to the browser"),
                    ("domain", "text", "Host name without a leading www."),
                    ("browser", "text", "chrome, chromium, brave, edge, vivaldi, firefox, librewolf or safari"),
                    ("profile", "text", "Stable profile key: browser/profile-folder"),
                    ("profile_name", "text", "Profile display name"),
                    ("device", "text", "Device label (BROWSER_HISTORY_DEVICE, default host name)"),
                    ("transition", "text", "How the page was reached: link, typed, reload, form_submit, ..."),
                    ("typed", "boolean", "Typed or chosen from the address bar"),
                    ("forward_back", "boolean", "Reached with back/forward (Chromium)"),
                    ("redirect", "boolean", "This visit is the target of a redirect"),
                    ("duration_s", "number", "Time the page was open, seconds (Chromium only)"),
                    ("referrer_url", "text", "URL of the visit this one came from"),
                    ("from_visit", "text", "External id of the visit this one came from"),
                    ("synced", "boolean", "Visit was synced from another device (kept only when enabled)"),
                    ("author_kind", "text", "owner: the owner's own browsing"),
                    ("mapping_revision", "integer", "Mapping revision that produced this record"),
                ),
            ),
            Channel(
                name=BOOKMARKS,
                description="Browser bookmarks (Chromium family and Firefox), versioned when edited and when removed",
                is_reference=True,
                record_kind="reference",
                fields=fields(
                    ("url", "text", "Bookmarked URL (sensitive parameters redacted)"),
                    ("title", "text", "Bookmark title"),
                    ("domain", "text", "Host name without a leading www."),
                    ("folder", "text", "Folder path, / separated"),
                    ("browser", "text", "Browser the bookmark lives in"),
                    ("date_added", "timestamp", "When the bookmark was created"),
                    ("removed", "boolean", "The bookmark was deleted (tombstone version)"),
                    ("author_kind", "text", "owner"),
                    ("mapping_revision", "integer", "Mapping revision that produced this record"),
                ),
            ),
        ],
        settings={
            "profiles": setting("BROWSER_HISTORY_PROFILES", "",
                                "Explicit browser:path items (profile folder or history file), comma separated; disables discovery"),
            "browsers": setting("BROWSER_HISTORY_BROWSERS", "", "Discover only these browsers (default: all of " + ", ".join(BROWSERS) + ")"),
            "home": setting("BROWSER_HISTORY_HOME", "", "Home folder searched by discovery (default: the current user's)"),
            "platform": setting("BROWSER_HISTORY_PLATFORM", "", "linux, darwin or windows (default: this system)"),
            "device": setting("BROWSER_HISTORY_DEVICE", "", "Stable device label used in visit ids (default: host name)"),
            "timezone": setting("BROWSER_HISTORY_TZ", "", "IANA zone for day threads (default: system)"),
            "exclude_domains": setting("BROWSER_HISTORY_EXCLUDE_DOMAINS", "",
                                       "Domains never recorded (suffix match, * globs allowed), comma separated"),
            "default_excludes": setting("BROWSER_HISTORY_DEFAULT_EXCLUDES", "true", "Also exclude " + ", ".join(DEFAULT_EXCLUDE)),
            "schemes": setting("BROWSER_HISTORY_SCHEMES", "http,https", "URL schemes recorded (file:, chrome:, about: ... are skipped)"),
            "redact_params": setting("BROWSER_HISTORY_REDACT_PARAMS", "", "Extra query/fragment parameter names to redact"),
            "strip_query": setting("BROWSER_HISTORY_STRIP_QUERY", "false", "Drop every query string and fragment"),
            "include_synced": setting("BROWSER_HISTORY_INCLUDE_SYNCED", "false",
                                      "Keep visits synced from other devices (off: each device reports its own)"),
            "include_redirects": setting("BROWSER_HISTORY_INCLUDE_REDIRECTS", "false", "Keep visits that only redirected onwards"),
            "bookmarks": setting("BROWSER_HISTORY_BOOKMARKS", "true", "Read bookmarks too"),
            "checkpoint_every": setting("BROWSER_HISTORY_CHECKPOINT_EVERY", "500", "Visits per checkpoint"),
        },
        link_types=[],
    )

    def __init__(self):
        super().__init__()
        s = self.settings
        exclude = words(s.get("exclude_domains"))
        if flag(s.get("default_excludes")):
            exclude = list(DEFAULT_EXCLUDE) + exclude
        self.privacy = Privacy(
            schemes=words(s.get("schemes")) or ["http", "https"],
            exclude=exclude,
            redact=list(DEFAULT_REDACT) + words(s.get("redact_params")),
            strip_query=flag(s.get("strip_query")),
        )
        device = (s.get("device") or "").strip() or socket.gethostname().split(".")[0]
        self.device = re.sub(r"[^A-Za-z0-9_.-]+", "-", device).strip("-").lower() or "device"
        zone = (s.get("timezone") or "").strip()
        self.tz = ZoneInfo(zone) if zone else dt.datetime.now().astimezone().tzinfo
        self.include_synced = flag(s.get("include_synced"))
        self.include_redirects = flag(s.get("include_redirects"))
        self.with_bookmarks = flag(s.get("bookmarks"))
        try:
            self.every = int(s.get("checkpoint_every") or 500)
        except ValueError:
            raise ValueError("BROWSER_HISTORY_CHECKPOINT_EVERY must be a positive integer") from None
        if self.every < 1:
            raise ValueError("BROWSER_HISTORY_CHECKPOINT_EVERY must be a positive integer")
        self.now = None

    def profiles(self):
        if (self.settings.get("profiles") or "").strip():
            return explicit(self.settings["profiles"])
        home = Path(self.settings.get("home") or Path.home()).expanduser()
        wanted = [b.lower() for b in words(self.settings.get("browsers"))] or None
        unknown = set(wanted or []) - set(BROWSERS)
        if unknown:
            raise ValueError("unknown browser in BROWSER_HISTORY_BROWSERS: " + ", ".join(sorted(unknown)))
        return discover(home, self.settings.get("platform") or None, os.environ, wanted)

    # --- run ----------------------------------------------------------------

    def records(self, cursor, mode):
        """Cursor: {"v": 1, "p": {profile key: {"id": newest visit id read,
        "t": newest raw visit time, "bs": bookmark signature, "bm": packed
        {guid: title}}}}."""
        state = json.loads(json.dumps((cursor or {}).get("p") or {}))
        profiles = self.profiles()
        if not profiles:
            raise RuntimeError("no browser profiles found (set BROWSER_HISTORY_PROFILES or BROWSER_HISTORY_HOME)")
        failures = 0
        for profile in profiles:
            entry = state.setdefault(profile.key, {})
            try:
                yield from self.read_profile(profile, entry, state)
            except (OSError, ValueError, readers.sqlite3.Error) as error:
                failures += 1
                LOG.warning("browser profile %s could not be read: %s", profile.key, type(error).__name__)
                for channel in (VISITS, BOOKMARKS):
                    yield coverage(channel, scope_key=profile.key, status="partial", reasons=["source_error"],
                                   detail={"error": type(error).__name__})
        if failures == len(profiles):
            raise RuntimeError("no browser profile could be read")
        yield Checkpoint(cursor={"v": 1, "p": state})

    def read_profile(self, profile, entry, state):
        if not profile.history.is_file():
            raise FileNotFoundError(profile.key)
        _, _, _, reader = readers.VISIT_TABLES[profile.family]
        counts = {"visits": 0, "filtered": 0, "subframes": 0, "redirect_sources": 0, "synced_skipped": 0}
        with readers.snapshot(profile.history) as conn:
            max_id, min_raw, _ = readers.visit_bounds(conn, profile.family)
            last_id = int(entry.get("id") or 0)
            last_t = entry.get("t")
            recovery = bool(last_id and (max_id or 0) < last_id and last_t is not None)
            gaps = []
            if last_t is not None and min_raw is not None and min_raw > last_t:
                # The browser expired history newer than our high-water mark.
                gaps.append({"from": readers.to_time(profile.family, last_t).isoformat(),
                             "to": readers.to_time(profile.family, min_raw).isoformat(), "reason": "source_cap"})
            newest = None
            since = 0
            visits = reader(conn, last_id, last_t if recovery else None)
            for visit in visits:
                if not recovery:
                    entry["id"] = visit["id"]
                if last_t is None or visit["raw_time"] > last_t:
                    last_t = visit["raw_time"]
                    entry["t"] = last_t
                if visit["subframe"]:
                    counts["subframes"] += 1
                    continue
                if visit["redirect_source"] and not self.include_redirects:
                    counts["redirect_sources"] += 1
                    continue
                if visit["synced"] and not self.include_synced:
                    counts["synced_skipped"] += 1
                    continue
                if not self.privacy.allowed(visit["url"]):
                    counts["filtered"] += 1
                    continue
                message = self.map_visit(profile, visit)
                newest = max(newest or message["happened_at"], message["happened_at"])
                yield message
                counts["visits"] += 1
                since += 1
                if since >= self.every and not recovery:
                    since = 0
                    yield Checkpoint(cursor={"v": 1, "p": state})
            if recovery:
                entry["id"] = max_id or 0
            yield coverage(VISITS, scope_key=profile.key, status="partial" if gaps else "complete",
                           reasons=["source_cap"] if gaps else (), gaps=gaps, rows_delivered=counts["visits"],
                           high_water=newest, verified=not gaps, detail={**counts, "recovered_cleared_history": recovery})
            if self.with_bookmarks and profile.family in ("chromium", "firefox"):
                yield from self.read_bookmarks(profile, entry, conn)
        yield Checkpoint(cursor={"v": 1, "p": state})

    def read_bookmarks(self, profile, entry, conn):
        if profile.family == "chromium":
            path = profile.bookmarks
            if not path.is_file():
                return
            signature = readers.chromium_signature(path)
            if signature == entry.get("bs"):
                return
            items = list(readers.chromium_bookmarks(path))
        else:
            if "moz_bookmarks" not in readers.tables(conn):
                return
            signature = readers.firefox_signature(conn)
            if signature == entry.get("bs"):
                return
            items = list(readers.firefox_bookmarks(conn))
        before = unpack(entry.get("bm"))
        current = {}
        delivered = 0
        for item in items:
            if not item["guid"] or not item["at"] or not self.privacy.allowed(item["url"]):
                continue
            if item["guid"] in current:
                continue
            message = self.map_bookmark(profile, item)
            current[item["guid"]] = message["fields"]["title"][:120]
            yield message
            delivered += 1
        observed = self.now or dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()
        removed = sorted(set(before) - set(current))
        for guid in removed:
            yield self.removed_bookmark(profile, guid, before[guid], observed)
        entry["bs"] = signature
        entry["bm"] = pack(current)
        yield coverage(BOOKMARKS, scope_key=profile.key, status="complete", rows_delivered=delivered + len(removed),
                       verified=True, detail={"bookmarks": len(current), "removed": len(removed)})

    # --- mapping ------------------------------------------------------------

    def visit_id(self, profile, ident, raw_time):
        return f"{self.device}:{profile.key}:{ident}:{raw_time}"

    def map_visit(self, profile, visit):
        url = self.privacy.clean(visit["url"])
        title = (visit["title"] or "").strip() or None
        at = visit["at"].astimezone(dt.UTC)
        day = visit["at"].astimezone(self.tz).date().isoformat()
        referrer = visit["referrer"]
        referrer = self.privacy.clean(referrer) if referrer and self.privacy.allowed(referrer) else None
        from_visit = (
            self.visit_id(profile, visit["from_id"], visit["from_raw_time"])
            if visit["from_id"] and visit["from_raw_time"] is not None else None
        )
        browser = TITLES.get(profile.browser, profile.browser)
        values = {
            "url": url,
            "title": title,
            "domain": domain(url),
            "browser": profile.browser,
            "profile": profile.key,
            "profile_name": profile.name,
            "device": self.device,
            "transition": visit["transition"],
            "typed": visit["typed"],
            "forward_back": visit["forward_back"],
            "redirect": visit["redirect"],
            "duration_s": visit["duration_s"],
            "referrer_url": referrer,
            "from_visit": from_visit,
            "synced": visit["synced"],
            "author_kind": "owner",
            "mapping_revision": MAPPING_REVISION,
        }
        thread = f"{self.device}:{profile.key}:{day}"
        return {
            "channel": VISITS,
            "external_id": self.visit_id(profile, visit["id"], visit["raw_time"]),
            "thread": {"external_id": thread, "title": f"{browser} {profile.name} · {day}"},
            "happened_at": at.isoformat(),
            "content": f"{title}\n{url}" if title else url,
            "fields": values,
            "payload": {
                "visit": {"id": visit["id"], "raw_time": visit["raw_time"], "url": url, "title": title,
                          "transition": visit["transition"], "referrer": referrer, **visit["raw"]},
                "profile": {"browser": profile.browser, "key": profile.key, "name": profile.name, "device": self.device},
            },
        }

    def map_bookmark(self, profile, item):
        url = self.privacy.clean(item["url"])
        title = (item["title"] or "").strip() or url
        values = {
            "url": url,
            "title": title,
            "domain": domain(url),
            "folder": item["folder"] or None,
            "browser": profile.browser,
            "date_added": item["at"].astimezone(dt.UTC).isoformat(),
            "removed": False,
            "author_kind": "owner",
            "mapping_revision": MAPPING_REVISION,
        }
        ident = f"{profile.browser}:{item['guid']}"
        where = f"\nFolder: {item['folder']}" if item["folder"] else ""
        return {
            "channel": BOOKMARKS,
            "external_id": ident,
            "thread": {"external_id": f"{profile.browser}:bookmarks", "title": f"{TITLES.get(profile.browser, profile.browser)} bookmarks"},
            "happened_at": values["date_added"],
            "content": f"{title}\n{url}{where}",
            "fields": values,
            "payload": {"bookmark": {"guid": item["guid"], "url": url, "title": title, "folder": item["folder"],
                                     "date_added": values["date_added"]}},
        }

    def removed_bookmark(self, profile, guid, title, observed):
        title = title or "Bookmark"
        return {
            "channel": BOOKMARKS,
            "external_id": f"{profile.browser}:{guid}",
            "thread": {"external_id": f"{profile.browser}:bookmarks", "title": f"{TITLES.get(profile.browser, profile.browser)} bookmarks"},
            "happened_at": observed,
            "content": f"{title}\n\nRemoved from {TITLES.get(profile.browser, profile.browser)} bookmarks.",
            "fields": {"title": title, "browser": profile.browser, "removed": True, "author_kind": "owner",
                       "mapping_revision": MAPPING_REVISION},
            "payload": {"bookmark": {"guid": guid, "removed": True, "observed_at": observed}},
        }


def main(argv=None):
    run(BrowserHistory, argv)


__all__ = ["BOOKMARKS", "BrowserHistory", "MAPPING_REVISION", "VISITS", "main"]
