"""Provider-neutral calendar mapping: occurrences and event lifecycle.

The same file ships in throughline_caldav and throughline_msgraph so events
from any provider look like the Google Calendar connector (plugins/calendar):
`<name>.events` (one record per occurrence, recurring series expanded inside
the window, record time = start) and `<name>.changes` (created, changed,
cancelled, removed, restored), the same field names, the organizer as an
email author and a `calendar_event` entity per iCalendar UID (`ical:<uid>`),
so copies of one meeting on Google, iCloud and Outlook share an entity.

Pure and deterministic: output depends only on the normalized input and the
mapping state kept in the plugin cursor; `observed` (the fetch time) is used
only for changes the source does not timestamp.
"""

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser

DESCRIPTION_LIMIT = 4000
ATTENDEE_LIMIT = 25
LISTED_DATES = 10
CHANGE_GRACE = timedelta(seconds=60)
TRACKED = (
    "title", "event_start", "event_end", "place", "description", "attendees",
    "responses", "recurrence",
)  # fmt: skip


def _f(**values):
    return [dict(name=k, type=v[0], description=v[1]) for k, v in values.items()]


COMMON = dict(
    account=("text", "Account the calendar belongs to"),
    calendar_id=("text", "Calendar id (collection path, feed id or provider id)"),
    calendar_name=("text", "Calendar display name"),
    event_id=("text", "Event id; an occurrence id for recurring instances"),
    series_id=("text", "Id of the recurring series, if recurring"),
    ical_uid=("text", "iCalendar UID shared by every copy of the event"),
    title=("text", "Event title"),
    status=("text", "confirmed, tentative, cancelled, or removed (vanished from the source)"),
    all_day=("boolean", "All-day event; times are UTC midnight with day precision"),
    place=("text", "Location text as entered"),
    observed_at=("timestamp", "Fetch time that observed a change the source does not timestamp"),
    mapping_revision=("integer", "Mapping revision that produced this record"),
)
EVENT_FIELDS = _f(
    **COMMON,
    start_time_zone=("text", "Time zone named on the event start"),
    original_start=("timestamp", "Original start of a moved or cancelled recurring instance"),
    description=("text", "Event description as plain text"),
    organizer_email=("text", "Organizer email (also the author identifier)"),
    organizer_name=("text", "Organizer display name"),
    attendees=("json", "Attendees: email, name, response, organizer, self, optional, resource"),
    attendee_count=("integer", "Number of attendees listed"),
    my_response=("text", "The calendar owner's response status"),
    conference_url=("text", "Video meeting link"),
    html_link=("text", "Link to the event in the provider's web app"),
    event_type=("text", "Provider event type, when it has one"),
    visibility=("text", "Event visibility / classification"),
    transparency=("text", "opaque (busy) or transparent (free)"),
    event_created=("timestamp", "When the event was created (source)"),
    event_updated=("timestamp", "When the event was last modified (source)"),
)
CHANGE_FIELDS = _f(
    **COMMON,
    change=("text", "created, changed, cancelled, removed or restored"),
    changed_fields=("json", "Fields that differ from the previous snapshot; null when unknown"),
    event_start=("timestamp", "Event start after the change"),
    event_end=("timestamp", "Event end after the change"),
    recurrence=("json", "RRULE/EXDATE/RDATE lines of a recurring series"),
    creator_email=("text", "Event creator email"),
    organizer_email=("text", "Organizer email"),
    time_precision=("text", "source (source timestamp) or observed (fetch time of a snapshot diff)"),
    occurrence_count=("integer", "Occurrences of a series that vanished together"),
)

# Response words normalized to Google's vocabulary.
RESPONSES = {
    "accepted": "accepted", "declined": "declined", "tentative": "tentative",
    "tentativelyaccepted": "tentative", "needs-action": "needsAction",
    "needsaction": "needsAction", "notresponded": "needsAction", "none": None,
    "organizer": "accepted", "delegated": "delegated",
}  # fmt: skip


def response(value):
    if not value:
        return None
    return RESPONSES.get(str(value).strip().lower().replace("_", ""), str(value))


def digest(value, size=24):
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:size]


def moment(value):
    if isinstance(value, datetime):
        return value if value.utcoffset() is not None else value.replace(tzinfo=UTC)
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else parsed.replace(tzinfo=UTC)


def iso(value):
    return value.isoformat() if value is not None else None


def subject(start, end, all_day):
    if start is None:
        return None
    precision = "day" if all_day else "exact"
    result = {"start": iso(start), "start_precision": precision}
    if end is not None:
        last = end - timedelta(days=1) if all_day else end
        result["end"] = iso(max(last, start))
        result["end_precision"] = precision
    return result


def _offset(value):
    text = value.strftime("%z")
    return "UTC" if text in ("+0000", "-0000") else f"UTC{text[:3]}:{text[3:]}"


def when(start, end, all_day):
    if start is None:
        return "unknown time"
    if all_day:
        last = (end - timedelta(days=1)) if end else start
        if last <= start:
            return f"{start:%Y-%m-%d} (all day)"
        return f"{start:%Y-%m-%d} to {last:%Y-%m-%d} (all day)"
    if end is None:
        return f"{start:%Y-%m-%d %H:%M} ({_offset(start)})"
    local_end = end.astimezone(start.tzinfo)
    if local_end.date() == start.date():
        return f"{start:%Y-%m-%d %H:%M} to {local_end:%H:%M} ({_offset(start)})"
    return f"{start:%Y-%m-%d %H:%M} to {local_end:%Y-%m-%d %H:%M} ({_offset(start)})"


class _Text(HTMLParser):
    BREAKS = {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "ul", "ol"}
    SKIP = {"style", "script", "head"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.href = None
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        if tag in self.BREAKS:
            self.parts.append("\n")
        if tag == "a":
            self.href = dict(attrs).get("href")
            self.anchor = len(self.parts)

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        if tag == "a" and self.href:
            label = "".join(self.parts[self.anchor :]).strip()
            if self.href not in label:
                self.parts.append(f" ({self.href})")
            self.href = None
        elif tag in self.BREAKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def plain_text(value):
    """Descriptions may be HTML (Outlook, Google); return readable text."""
    if not value:
        return None
    text = str(value)
    if re.search(r"<[a-zA-Z/][^>]*>", text):
        parser = _Text()
        parser.feed(text)
        parser.close()
        text = "".join(parser.parts)
    text = text.replace("\r\n", "\n").replace("\xa0", " ")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text or None


def author_of(entry):
    """An email identifier for a person; calendar addresses are not people."""
    email = ((entry or {}).get("email") or "").strip()
    if not email or "@" not in email or email.lower().endswith("calendar.google.com"):
        return None
    return {"system": "email", "kind": "email", "value": email, "display_name": (entry or {}).get("name")}


def _people(values):
    return [
        {k: v for k, v in p.items() if v not in (None, False, "")}
        for p in values or []
        if isinstance(p, dict) and (p.get("email") or p.get("name"))
    ]


def tracked(unit):
    attendees = _people(unit.get("attendees"))
    values = {
        "title": unit.get("title"),
        "event_start": iso(unit.get("start")),
        "event_end": iso(unit.get("end")),
        "place": unit.get("place"),
        "description": unit.get("description"),
        "attendees": sorted((a.get("email") or "").lower() for a in attendees),
        "responses": sorted([(a.get("email") or "").lower(), a.get("response")] for a in attendees),
        "recurrence": unit.get("recurrence"),
    }
    return {key: digest(value, 8) for key, value in values.items()}


class Mapper:
    """Applies normalized occurrences and event units to the mapping state.

    State (JSON, in the plugin cursor):
      calendars  "account\\tcalendar" -> name
      snap       "account\\tcalendar" -> {occurrence id: [digest, start, end,
                 all_day, status, title, series id, unit id, entity ref]}
      units      "account\\tcalendar\\tunit id" -> {c: created recorded,
                 u: last updated, s: status, t: title, f: tracked digests, r: ref}

    Occurrence dicts: occurrence_id, unit_id, series_id, ical_uid, title,
    status, start, end, all_day, time_zone, original_start, place,
    description, organizer {email, name}, attendees, my_response,
    conference_url, html_link, event_type, visibility, transparency, created,
    updated, source (raw, JSON-able).
    Unit dicts (a single event or a series master): unit_id, ical_uid, title,
    status, start, end, all_day, place, description, attendees, recurrence,
    creator, organizer, created, updated, source.
    """

    def __init__(self, state, *, events, changes, revision, emit):
        self.calendars = state.setdefault("calendars", {})
        self.snap = state.setdefault("snap", {})
        self.units = state.setdefault("units", {})
        self.events, self.changes, self.revision = events, changes, revision
        self.emit = emit  # emit(message, link or None)
        self.stats = {}
        self.touched = set()  # Units seen in this run (never pruned).

    def count(self, key):
        self.stats[key] = self.stats.get(key, 0) + 1

    # --- helpers -------------------------------------------------------------

    def name(self, account, cid):
        return self.calendars.get(account + "\t" + cid) or cid

    def set_name(self, account, cid, name):
        self.calendars[account + "\t" + cid] = name or cid

    def thread(self, account, cid):
        return {
            "external_id": f"{account}:{cid}",
            "title": self.name(account, cid),
            "metadata": {"kind": "calendar", "account": account, "calendar_id": cid},
        }

    @staticmethod
    def ref(account, cid, item):
        if item.get("ical_uid"):
            return "ical:" + item["ical_uid"]
        return f"cal:{account}:{cid}:{item.get('series_id') or item.get('unit_id') or item.get('occurrence_id')}"

    @staticmethod
    def link(message, link_type, ref, name):
        return {
            "type": link_type,
            "from": {"channel": message["channel"], "external_id": message["external_id"]},
            "to": {"kind": "calendar_event", "external_ref": ref, "name": name},
            "method": "source",
        }

    def entries(self, account, cid):
        return self.snap.setdefault(account + "\t" + cid, {})

    def _unit(self, account, cid, unit_id):
        key = f"{account}\t{cid}\t{unit_id}"
        self.touched.add(key)
        if key not in self.units:
            self.units[key] = {"c": False, "u": None, "s": None, "t": None, "f": None, "r": None}
        return self.units[key]

    # --- occurrences -----------------------------------------------------------

    def occurrence(self, account, cid, occ):
        entries = self.entries(account, cid)
        previous = entries.get(occ["occurrence_id"])
        if occ.get("start") is None:
            self.count("skipped_no_time")
            return
        message = self.occurrence_message(account, cid, occ)
        signature = digest(message)
        if previous is None or previous[0] != signature:
            ref = self.ref(account, cid, occ)
            self.emit(message, self.link(message, "part_of", ref, message["fields"]["title"]))
            entries[occ["occurrence_id"]] = [
                signature,
                message["happened_at"],
                message["ended_at"],
                occ["all_day"],
                message["fields"]["status"],
                message["fields"]["title"],
                occ.get("series_id"),
                occ.get("unit_id"),
                ref,
            ]

    def occurrence_message(self, account, cid, occ):
        start, end, all_day = occ["start"], occ.get("end"), bool(occ.get("all_day"))
        status = occ.get("status") or "confirmed"
        title = occ.get("title") or "(no title)"
        calendar = self.name(account, cid)
        people = _people(occ.get("attendees"))
        organizer = occ.get("organizer") or {}
        description = plain_text(occ.get("description"))
        lines = [("Cancelled: " if status == "cancelled" else "") + title, "When: " + when(start, end, all_day)]
        if occ.get("place"):
            lines.append("Where: " + occ["place"])
        lines.append("Calendar: " + calendar)
        if people:
            shown = [
                (f"{p['name']} <{p['email']}>" if p.get("name") and p.get("email") else p.get("name") or p.get("email"))
                + (f" ({p['response']})" if p.get("response") else "")
                for p in people[:ATTENDEE_LIMIT]
            ]
            extra = len(people) - ATTENDEE_LIMIT
            lines.append("With: " + ", ".join(shown) + (f", and {extra} more" if extra > 0 else ""))
        if status not in ("confirmed", "cancelled"):
            lines.append("Status: " + status)
        if description:
            lines += ["", description[:DESCRIPTION_LIMIT]]
        fields = {
            "account": account,
            "calendar_id": cid,
            "calendar_name": calendar,
            "event_id": occ["occurrence_id"],
            "series_id": occ.get("series_id"),
            "ical_uid": occ.get("ical_uid"),
            "title": title,
            "status": status,
            "all_day": all_day,
            "start_time_zone": occ.get("time_zone"),
            "original_start": iso(occ.get("original_start")),
            "place": occ.get("place"),
            "description": description,
            "organizer_email": organizer.get("email"),
            "organizer_name": organizer.get("name"),
            "attendees": people or None,
            "attendee_count": len(people),
            "my_response": occ.get("my_response"),
            "conference_url": occ.get("conference_url"),
            "html_link": occ.get("html_link"),
            "event_type": occ.get("event_type"),
            "visibility": occ.get("visibility"),
            "transparency": occ.get("transparency"),
            "event_created": iso(occ.get("created")),
            "event_updated": iso(occ.get("updated")),
            "observed_at": None,
            "mapping_revision": self.revision,
        }
        return {
            "channel": self.events,
            "external_id": f"{account}:{cid}:{occ['occurrence_id']}",
            "thread": self.thread(account, cid),
            "happened_at": iso(start),
            "ended_at": iso(end),
            "subject": subject(start, end, all_day),
            "author": author_of(organizer),
            "content": "\n".join(lines),
            "fields": fields,
            "payload": {"source": occ.get("source"), "account": account, "calendar_id": cid},
        }

    def live_ids(self, account, cid, unit_id=None, low=None, high=None):
        """Known occurrences (not cancelled/removed), optionally of one unit
        and overlapping [low, high)."""
        result = []
        for occ_id, entry in self.entries(account, cid).items():
            if entry[4] in ("cancelled", "removed"):
                continue
            if unit_id is not None and entry[7] != unit_id:
                continue
            if low is not None and high is not None:
                start = moment(entry[1])
                end = moment(entry[2]) or start
                if not (start < high and end > low):
                    continue
            result.append(occ_id)
        return sorted(result)

    def prune(self, account, cid, low):
        """Forget occurrences that ended before the window start."""
        entries = self.entries(account, cid)
        for occ_id in list(entries):
            entry = entries[occ_id]
            end = moment(entry[2]) or moment(entry[1])
            if end is not None and end <= low:
                del entries[occ_id]

    def prune_units(self, account, cid):
        """Forget events with no known occurrence left (the cursor stays
        bounded by the window). One that comes back is a first sighting
        again; its `created` record has a stable id, so it is not duplicated."""
        prefix = f"{account}\t{cid}\t"
        live = set()
        for occ_id, entry in self.entries(account, cid).items():
            live.update(x for x in (occ_id, entry[6], entry[7]) if x)
        for key in [k for k in self.units if k.startswith(prefix)]:
            if key[len(prefix):] not in live and key not in self.touched:
                del self.units[key]

    def remove(self, account, cid, occ_ids, observed, why):
        """Occurrences gone from the source without a cancellation."""
        entries = self.entries(account, cid)
        series, singles = {}, []
        for occ_id in sorted(set(occ_ids)):
            entry = entries.get(occ_id)
            if entry is None or entry[4] in ("cancelled", "removed"):
                continue
            message = self.removed_message(account, cid, occ_id, entry, observed, why)
            self.emit(message, self.link(message, "part_of", entry[8], entry[5]))
            entries[occ_id] = [digest(message), *entry[1:4], "removed", *entry[5:]]
            if entry[6]:
                series.setdefault(entry[6], []).append(entry)
            else:
                singles.append((occ_id, entry))
        for occ_id, entry in singles:
            self.unit_removed(account, cid, entry[7] or occ_id, entry, observed)
        for series_id in sorted(series):
            self.series_removed(account, cid, series_id, series[series_id], observed)

    def removed_message(self, account, cid, occ_id, entry, observed, why):
        start, end, all_day, title = moment(entry[1]), moment(entry[2]), entry[3], entry[5]
        calendar = self.name(account, cid)
        return {
            "channel": self.events,
            "external_id": f"{account}:{cid}:{occ_id}",
            "thread": self.thread(account, cid),
            "happened_at": iso(start),
            "ended_at": iso(end),
            "subject": subject(start, end, all_day),
            "author": None,
            "content": "\n".join(
                ["Removed from calendar: " + title, "When: " + when(start, end, all_day), "Calendar: " + calendar]
            ),
            "fields": {
                "account": account,
                "calendar_id": cid,
                "calendar_name": calendar,
                "event_id": occ_id,
                "series_id": entry[6],
                "title": title,
                "status": "removed",
                "all_day": all_day,
                "observed_at": iso(observed),
                "mapping_revision": self.revision,
            },
            "payload": {
                "source": None,
                "account": account,
                "calendar_id": cid,
                "removed": {"observed_at": iso(observed), "why": why},
            },
        }

    # --- lifecycle ---------------------------------------------------------------

    def change(self, account, cid, unit_id, change, at, precision, item, *, observed, title, ref,
               changed=None, extra=None, occurrences=None, series_id=None):  # fmt: skip
        item = item or {}
        start, end, all_day = item.get("start"), item.get("end"), bool(item.get("all_day"))
        noun = "occurrence of " if series_id else "event: "
        if change == "removed" and occurrences is not None:
            head = f"Occurrences of {title} no longer on calendar"
        else:
            head = {
                "created": f"Created {noun}",
                "changed": f"Changed {noun}",
                "cancelled": f"Cancelled {noun}",
                "removed": "No longer on calendar: ",
                "restored": "Back on calendar: ",
            }[change] + title
        if change == "changed":
            head += f" ({', '.join(changed)})" if changed else " (details not recorded)"
        calendar = self.name(account, cid)
        lines = [head]
        if start is not None:
            lines.append("When: " + when(start, end, all_day))
        if item.get("place"):
            lines.append("Where: " + item["place"])
        lines.append("Calendar: " + calendar)
        if item.get("recurrence"):
            lines.append("Repeats: " + "; ".join(str(r) for r in item["recurrence"]))
        lines.extend(extra or [])
        stamp = iso(at.astimezone(UTC))
        external = f"{account}:{cid}:{unit_id}:{change}"
        if change == "changed" and changed is None:
            # A first sighting knows only that a change happened: its own id,
            # so it never overwrites a diff-based record of the same change
            # (after a cursor reset) with changed_fields null.
            external += ":first-seen:" + stamp
        elif change != "created":
            external += (":observed:" if precision == "observed" else ":") + stamp
        organizer = item.get("organizer") or {}
        creator = item.get("creator") or {}
        message = {
            "channel": self.changes,
            "external_id": external,
            "thread": {
                "external_id": f"{account}:{cid}:{series_id or unit_id}",
                "title": title,
                "metadata": {"kind": "calendar_event", "account": account, "calendar_id": cid},
            },
            "happened_at": iso(at),
            "ended_at": None,
            "subject": subject(start, end, all_day),
            "author": author_of(creator or organizer) if change == "created" else None,
            "content": "\n".join(lines),
            "fields": {
                "account": account,
                "calendar_id": cid,
                "calendar_name": calendar,
                "event_id": unit_id,
                "series_id": series_id,
                "ical_uid": item.get("ical_uid"),
                "change": change,
                "changed_fields": changed,
                "title": title,
                "status": item.get("status"),
                "event_start": iso(start),
                "event_end": iso(end),
                "all_day": all_day if start is not None else None,
                "place": item.get("place"),
                "recurrence": item.get("recurrence"),
                "creator_email": creator.get("email"),
                "organizer_email": organizer.get("email"),
                "time_precision": precision,
                "observed_at": iso(observed) if precision == "observed" else None,
                "occurrence_count": occurrences,
                "mapping_revision": self.revision,
            },
            "payload": {"source": item.get("source"), "account": account, "calendar_id": cid},
        }
        if precision == "observed":
            message["payload"]["observed_at"] = iso(observed)
        if message["subject"] is None:
            del message["subject"]
        self.emit(message, self.link(message, "about", ref, title))
        self.count(change)
        return message

    def unit(self, account, cid, item, observed):
        """Lifecycle of one event (a single event, an exception or a series master)."""
        unit_id = item["unit_id"]
        unit = self._unit(account, cid, unit_id)
        status = item.get("status") or "confirmed"
        created, updated = item.get("created"), item.get("updated")
        title = item.get("title") or unit["t"] or "(no title)"
        unit["r"] = unit["r"] or self.ref(account, cid, item)
        common = dict(observed=observed, title=title, ref=unit["r"], series_id=item.get("series_id"))
        if item.get("series_id"):
            # An exception (a moved or cancelled instance) carries the series'
            # creation time; the series has its own `created` record.
            unit["c"] = True
        if not unit["c"] and created is not None:
            self.change(account, cid, unit_id, "created", created, "source", item, **common)
            unit["c"] = True
        changed_at = updated if updated is not None and iso(updated) != unit["u"] else None
        at, precision = (changed_at, "source") if changed_at else (observed, "observed")
        if status == "cancelled":
            if unit["s"] != "cancelled":
                self.change(account, cid, unit_id, "cancelled", at, precision, item, **common)
            unit.update(s="cancelled", u=iso(updated) or unit["u"], t=title)
            return
        fields = tracked(item)
        if unit["s"] in ("cancelled", "removed"):
            self.change(account, cid, unit_id, "restored", at, precision, item, **common)
        elif unit["f"] is None:
            # First sighting: only that it changed after creation is known.
            if created and updated and updated - created > CHANGE_GRACE:
                self.change(account, cid, unit_id, "changed", updated, "source", item, **common)
        else:
            changed = [key for key in TRACKED if fields.get(key) != unit["f"].get(key)]
            if changed:
                self.change(account, cid, unit_id, "changed", at, precision, item, changed=changed, **common)
        unit.update(s=status, u=iso(updated) or unit["u"], t=title, f=fields)

    def series_seen(self, account, cid, series_id, occ):
        """An occurrence of a series whose master is not delivered (Graph):
        record once that the series was created."""
        unit = self._unit(account, cid, series_id)
        unit["r"] = unit["r"] or self.ref(account, cid, occ)
        if unit["t"] is None and occ.get("title"):
            unit["t"] = occ["title"]
        created = occ.get("created")
        if unit["c"] or created is None or occ.get("status") == "cancelled":
            return
        stub = {k: occ.get(k) for k in ("ical_uid", "organizer", "creator", "source")}
        self.change(account, cid, series_id, "created", created, "source", stub,
                    observed=None, title=unit["t"] or "(no title)", ref=unit["r"])  # fmt: skip
        unit["c"] = True

    def unit_removed(self, account, cid, unit_id, entry, observed):
        unit = self._unit(account, cid, unit_id)
        if unit["s"] in ("cancelled", "removed"):
            return
        item = {"start": moment(entry[1]), "end": moment(entry[2]), "all_day": entry[3]}
        self.change(account, cid, unit_id, "removed", observed, "observed", item,
                    observed=observed, title=entry[5], ref=entry[8])  # fmt: skip
        unit["s"] = "removed"

    def unit_deleted(self, account, cid, unit_id, observed):
        """A whole event or series is gone (resource deleted)."""
        unit = self.units.get(f"{account}\t{cid}\t{unit_id}")
        if not unit or unit["s"] in ("cancelled", "removed"):
            return
        self.change(account, cid, unit_id, "removed", observed, "observed", None,
                    observed=observed, title=unit["t"] or "(no title)", ref=unit["r"] or f"cal:{account}:{cid}:{unit_id}")  # fmt: skip
        unit["s"] = "removed"

    def series_removed(self, account, cid, series_id, entries, observed):
        dates = sorted(entry[1][:10] for entry in entries)
        listed = ", ".join(dates[:LISTED_DATES]) + (
            f", and {len(dates) - LISTED_DATES} more" if len(dates) > LISTED_DATES else ""
        )
        unit = self.units.get(f"{account}\t{cid}\t{series_id}") or {}
        if unit.get("s") == "removed":
            return  # The whole series is gone; unit_deleted said so.
        title = unit.get("t") or entries[0][5]
        self.change(account, cid, series_id, "removed", observed, "observed", None,
                    observed=observed, title=title, ref=entries[0][8],
                    extra=[f"{len(entries)} occurrence(s) gone: {listed}"],
                    occurrences=len(entries))  # fmt: skip
