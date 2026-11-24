"""CalDAV calendars (iCloud, Fastmail, Nextcloud, Radicale, ...) and ICS feeds.

Two channels shaped like the Google Calendar connector (plugins/calendar):
`caldav.events` (one record per occurrence; recurring series expanded inside
the window) and `caldav.changes` (created, changed, cancelled, removed,
restored). Change detection per calendar: the collection ctag / sync-token
skips unchanged calendars; resource etags limit downloads to changed events;
plain ICS feeds use ETag / Last-Modified conditional GETs.
"""

import copy
import hashlib
import logging
from datetime import UTC, date, datetime, time, timedelta
from urllib.parse import urlsplit

from throughline_sdk import Channel, Checkpoint, Declaration, Manifest, Plugin, Record, coverage, run

from . import calmap, ics
from .dav import DavClient, DavError

LOG = logging.getLogger("throughline_caldav")
EVENTS = "caldav.events"
CHANGES = "caldav.changes"
# 1: first mapping.
MAPPING_REVISION = 1

SETTINGS = [
    ("url", "CALDAV_URL", None, "CalDAV server, principal, calendar home or calendar URL (e.g. https://caldav.icloud.com/)"),
    ("username", "CALDAV_USERNAME", None, "CalDAV login name"),
    ("account", "CALDAV_ACCOUNT", None, "Account label used in ids (default: username@host)"),
    ("calendars", "CALDAV_CALENDARS", None, "Comma-separated calendar names or paths to include (default: all)"),
    ("exclude", "CALDAV_EXCLUDE_CALENDARS", None, "Comma-separated calendar names or paths to skip"),
    ("ics_names", "CALDAV_ICS_NAMES", None, "Optional comma-separated names for the ICS feeds, in the same order"),
    ("owner", "CALDAV_OWNER_ADDRESSES", None, "Comma-separated addresses that are you (sets my_response)"),
    ("past_days", "CALDAV_PAST_DAYS", "30", "Rolling window: days before today (UTC)"),
    ("future_days", "CALDAV_FUTURE_DAYS", "90", "Rolling window: days after today"),
    ("window_start", "CALDAV_WINDOW_START", None, "Fixed window start YYYY-MM-DD (overrides the rolling window)"),
    ("window_end", "CALDAV_WINDOW_END", None, "Fixed window end YYYY-MM-DD, exclusive"),
    ("default_tz", "CALDAV_DEFAULT_TZ", "UTC", "Zone for floating (zone-less) times"),
    ("links", "CALDAV_LINKS", "true", "Link records to a calendar_event entity per iCalendar UID"),
    ("batch_size", "CALDAV_BATCH_SIZE", "200", "Messages per acknowledged batch and checkpoint"),
    ("timeout", "CALDAV_TIMEOUT_S", "60", "HTTP timeout in seconds"),
    ("fixture", "CALDAV_FIXTURE", None, "Tests only: recorded HTTP answers (JSON) instead of the network"),
]


def _split(value):
    return [v.strip() for v in str(value or "").split(",") if v.strip()]


def _truthy(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _day(value):
    return datetime.combine(date.fromisoformat(value), time(), UTC)


def redact(values):
    """Add values to the runner's log redaction (the SDK redacts each declared
    secret as a whole; a list of URLs must also be redacted one by one)."""
    values = {v for v in values if v}
    for handler in logging.getLogger().handlers:
        for item in handler.filters:
            if hasattr(item, "clean") and isinstance(getattr(item, "values", None), list):
                item.values = sorted(set(item.values) | values, key=len, reverse=True)


class FetchFailed(RuntimeError):
    """No calendar or feed could be read at all."""


class CalDav(Plugin):
    manifest = Manifest(
        name="caldav",
        version="0.1.0",
        channels=[
            Channel(
                name=EVENTS,
                description=(
                    "Event occurrences from CalDAV calendars and ICS feeds (recurring events "
                    "expanded within the window); the record time and subject are when the event happens"
                ),
                fields=calmap.EVENT_FIELDS,
            ),
            Channel(
                name=CHANGES,
                description=(
                    "Event lifecycle: created, changed, cancelled, removed, restored. The record time is "
                    "the event's CREATED/LAST-MODIFIED time, or the fetch time for changes found by snapshot diff"
                ),
                fields=calmap.CHANGE_FIELDS,
            ),
        ],
        settings={
            key: Declaration(env=env, required=False, default=default, description=text)
            for key, env, default, text in SETTINGS
        },
        secrets={
            "password": Declaration(env="CALDAV_PASSWORD", required=False, description="CalDAV password or app-specific password"),
            "ics_urls": Declaration(
                env="CALDAV_ICS_URLS",
                required=False,
                description="Comma-separated ICS subscription URLs (secret: they usually embed a private token)",
            ),
        },
        run_mode="poll",
        default_interval_s=1800,
        link_types=["part_of", "about"],
    )

    # Test seams.
    transport = None
    clock = None

    def __init__(self):
        super().__init__()
        s = self.settings
        self.batch_size = int(s["batch_size"])
        if self.batch_size < 1:
            raise ValueError("CALDAV_BATCH_SIZE must be positive")
        self.owner = [o.lower() for o in _split(s.get("owner"))]
        # Each feed URL is a secret on its own: never logged, never in a record.
        for index, url in enumerate(_split(self.secrets.get("ics_urls"))):
            self.secrets[f"ics_url_{index}"] = url
            if url.startswith("webcal://"):
                self.secrets[f"ics_url_{index}_https"] = "https://" + url[len("webcal://"):]
        redact(v for k, v in self.secrets.items() if k.startswith("ics_url_"))
        self.floating = ics.zone(s.get("default_tz"))
        if bool(s.get("window_start")) != bool(s.get("window_end")):
            raise ValueError("set both CALDAV_WINDOW_START and CALDAV_WINDOW_END, or neither")
        username = s.get("username") or ""
        host = urlsplit(s.get("url") or "").hostname or ""
        self.account = (s.get("account") or (f"{username}@{host}" if username else host) or "caldav").lower()

    def now(self):
        return (self.clock or (lambda: datetime.now(UTC)))().astimezone(UTC).replace(microsecond=0)

    def window(self, now):
        s = self.settings
        if s.get("window_start"):
            return _day(s["window_start"]), _day(s["window_end"])
        today = datetime.combine(now.date(), time(), UTC)
        return today - timedelta(days=int(s["past_days"])), today + timedelta(days=int(s["future_days"]))

    def client(self):
        transport = self.transport
        if transport is None and self.settings.get("fixture"):
            from .fixture import transport_from_file

            transport = transport_from_file(self.settings["fixture"])
        return DavClient(
            self.settings.get("username"),
            self.secrets.get("password"),
            transport=transport,
            timeout=float(self.settings["timeout"]),
        )

    # --- fetch --------------------------------------------------------------------

    def selected(self, calendar):
        include = _split(self.settings.get("calendars"))
        exclude = _split(self.settings.get("exclude"))
        names = {calendar["name"], calendar["href"], calendar["href"].rstrip("/")}
        if include and not names & set(include):
            return False
        return not names & set(exclude)

    def fetch(self, client, sources, low, high):
        """[(scope dict, fetched dict)] for every calendar and feed that
        changed, plus the scopes that failed."""
        window = [calmap.iso(low), calmap.iso(high)]
        fetched, failed, attempted = [], [], 0
        if self.settings.get("url"):
            attempted += 1
            try:
                calendars = [c for c in client.discover(self.settings["url"]) if self.selected(c)]
            except DavError as error:
                LOG.warning("CalDAV discovery failed: %s", error)
                failed.append(self.account)
                calendars = []
            if calendars:
                # Found calendars: their reads, not the discovery, decide
                # whether this source could be read at all.
                attempted -= 1
            for calendar in calendars:
                attempted += 1
                key = self.account + "\t" + calendar["href"]
                known = sources.get(key) or {}
                try:
                    item = self.fetch_calendar(client, calendar, known, window, low, high)
                except DavError as error:
                    LOG.warning("CalDAV calendar %s skipped: %s", calendar["name"], error)
                    failed.append(f"{self.account}:{calendar['href']}")
                    continue
                if item is not None:
                    fetched.append(item)
        names = _split(self.settings.get("ics_names"))
        for index, url in enumerate(_split(self.secrets.get("ics_urls"))):
            attempted += 1
            feed_id = "ics-" + hashlib.sha256(url.encode()).hexdigest()[:12]
            key = "ics\t" + feed_id
            known = sources.get(key) or {}
            same_window = known.get("window") == window
            try:
                status, text, etag, modified = client.feed(
                    url,
                    known.get("http_etag") if same_window else None,
                    known.get("last_modified") if same_window else None,
                    label=f"ICS feed {feed_id}",
                )
            except DavError as error:
                LOG.warning("%s", error)
                failed.append("ics:" + feed_id)
                continue
            if status == 304:
                continue
            fetched.append(
                {
                    "account": "ics",
                    "calendar_id": feed_id,
                    "name": names[index] if index < len(names) else None,
                    "complete": True,
                    "objects": {"feed": [etag, text]},
                    "deleted": [],
                    "state": {"window": window, "http_etag": etag, "last_modified": modified},
                }
            )
        if attempted and len(failed) >= attempted:
            raise FetchFailed("no calendar or feed could be read; see the warnings above")
        return fetched, failed

    def fetch_calendar(self, client, calendar, known, window, low, high):
        tag = calendar.get("sync_token") or calendar.get("ctag")
        same_window = known.get("window") == window
        if same_window and tag and known.get("tag") == tag:
            return None  # Unchanged since the last complete read.
        etags = {href: entry[0] for href, entry in (known.get("etags") or {}).items()}
        if same_window and etags:
            listing, whole = client.etags(calendar["url"], low, high)
            changed = sorted(h for h, e in listing.items() if etags.get(h) != e)
            objects = client.multiget(calendar["url"], changed)[0] if changed else {}
            complete = False
        else:
            objects, whole = client.objects(calendar["url"], low, high)
            listing = {h: e for h, (e, _) in objects.items()}
            complete = whole
        # Only a whole listing tells what left the window; only an explicit
        # 404 tells that a resource is gone (anything else stays as known).
        vanished = sorted(set(etags) - set(listing)) if whole else []
        moved, gone = client.multiget(calendar["url"], vanished) if vanished else ({}, set())
        objects.update(moved)
        return {
            "account": self.account,
            "calendar_id": calendar["href"],
            "name": calendar["name"],
            "complete": complete,
            "objects": {h: [e, text] for h, (e, text) in sorted(objects.items())},
            "moved": sorted(moved),
            "deleted": sorted(gone),
            "listing": listing,
            # A truncated answer keeps no tag, so the next run reads again.
            "state": {"window": window, "tag": tag if whole else None},
        }

    # --- run ------------------------------------------------------------------------

    def records(self, cursor, mode):
        if not self.settings.get("url") and not self.secrets.get("ics_urls"):
            raise ValueError("set CALDAV_URL and/or CALDAV_ICS_URLS")
        state = copy.deepcopy(cursor or {})
        if state.get("mapping_revision") != MAPPING_REVISION:
            state = {"mapping_revision": MAPPING_REVISION}
        resume = state.pop("resume", None)
        base = copy.deepcopy(state)
        now = self.now()
        low, high = self.window(now)
        sources = state.setdefault("sources", {})
        client = self.client()
        try:
            fetched, failed = self.fetch(client, sources, low, high)
        finally:
            client.close()
        signature = calmap.digest([base, [calmap.iso(low), calmap.iso(high)], fetched], 64)
        offset = 0
        observed = now
        if resume and resume.get("signature") == signature:
            offset = resume.get("offset", 0)
            observed = datetime.fromisoformat(resume["observed"])
        plan, links = {}, {}

        def emit(message, link):
            key = (message["channel"], message["external_id"])
            plan[key] = message  # The last state of a record in this run wins.
            if link:
                links[key] = link

        mapper = calmap.Mapper(state, events=EVENTS, changes=CHANGES, revision=MAPPING_REVISION, emit=emit)
        for item in fetched:
            self.apply(mapper, sources, item, low, high, observed)
        items = list(plan.items())
        with_links = _truthy(self.settings["links"])
        for start in range(offset, len(items), self.batch_size):
            chunk = items[start : start + self.batch_size]
            for _, message in chunk:
                yield message
            if with_links:
                for key, _ in chunk:
                    if key in links:
                        yield Record(kind="link", data=links[key])
            if start + self.batch_size < len(items):
                interim = copy.deepcopy(base)
                interim["resume"] = {"signature": signature, "offset": start + self.batch_size, "observed": observed.isoformat()}
                yield Checkpoint(cursor=interim)
        for scope in failed:
            for channel in (EVENTS, CHANGES):
                yield coverage(channel, scope_key=scope, status="partial", reasons=["source_error"])
        yield Checkpoint(cursor=state)

    def apply(self, mapper, sources, item, low, high, observed):
        account, cid = item["account"], item["calendar_id"]
        key = account + "\t" + cid
        known = sources.get(key) or {}
        resources = dict(known.get("etags") or {})
        if item.get("name") or not mapper.calendars.get(key):
            mapper.set_name(account, cid, item.get("name"))
        mapper.prune(account, cid, low)
        produced, units_seen, parsed, broken = set(), {}, 0, []
        for href, (etag, text) in item["objects"].items():
            try:
                cal = ics.parse(text)
                units, occurrences = ics.events(
                    cal, low, high, owner=self.owner, floating=self.floating,
                    singles_anywhere=href in item.get("moved", ()),
                )  # fmt: skip
            except (ValueError, TypeError, AttributeError, KeyError) as error:
                LOG.warning("calendar object skipped: %s", error)
                broken.append(href)
                continue
            parsed += 1
            if item["account"] == "ics" and not item.get("name"):
                mapper.set_name(account, cid, ics.calendar_name(cal) or cid)
            for unit in units.values():
                mapper.unit(account, cid, unit, observed)
                units_seen[unit["unit_id"]] = href
            for occ in occurrences:
                mapper.occurrence(account, cid, occ)
                produced.add(occ["occurrence_id"])
            resources[href] = [etag, sorted(units)]
        complete = item["complete"]
        if complete and (broken or (item["objects"] and not parsed)):
            # Something in a full read could not be parsed (an HTML error page
            # served as 200, a broken object): judge nothing as removed, keep
            # the stored state so the next run reads everything again.
            mapper.count("skipped_unreadable")
            mapper.prune_units(account, cid)
            return
        if complete and not units_seen and mapper.live_ids(account, cid, low=low, high=high):
            # A complete answer with no event in it while live occurrences are
            # known is far likelier a misread answer than an emptied calendar.
            mapper.count("skipped_empty_window")
            return
        # What vanished: whole units first, then single occurrences.
        gone_units = set()
        for href in item.get("deleted", ()):
            gone_units.update((resources.pop(href, None) or [None, []])[1])
        if complete and item["account"] == "ics":
            old = {u for _, entry in (known.get("etags") or {}).items() for u in entry[1]}
            gone_units.update(old - set(units_seen))
        for unit_id in sorted(gone_units - set(units_seen)):
            live = mapper.live_ids(account, cid, unit_id, low, high)
            if live:
                mapper.unit_deleted(account, cid, unit_id, observed)
                mapper.remove(account, cid, live, observed, "deleted")
        if complete:
            missing = [i for i in mapper.live_ids(account, cid, low=low, high=high) if i not in produced]
        else:
            missing = [
                i
                for unit_id in units_seen
                for i in mapper.live_ids(account, cid, unit_id, low, high)
                if i not in produced
            ]
        mapper.remove(account, cid, missing, observed, "not in source")
        if complete and item["account"] != "ics":
            resources = {h: e for h, e in resources.items() if h in item["objects"]}
        mapper.prune_units(account, cid)
        sources[key] = dict(item["state"], etags=resources)


def main():
    run(CalDav)
