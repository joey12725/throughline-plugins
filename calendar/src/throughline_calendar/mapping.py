"""Raw calendar records -> Throughline messages.

Pure and deterministic: the output depends only on the raw records and the
mapping state (kept in the plugin cursor), never on the clock or on how the
runs were split between invocations. Every timestamp comes from the source or
from the fetch run's `fetched_at`.
"""

import hashlib
import json
import re
from datetime import UTC, date, datetime, time, timedelta
from html.parser import HTMLParser

# Bump when the normalized shape changes: the cursor then replays the inputs
# and every record whose mapping changed gets one new version.
MAPPING_REVISION = 1

EVENTS = "calendar.events"
CHANGES = "calendar.changes"
DESCRIPTION_LIMIT = 4000
ATTENDEE_LIMIT = 25
LISTED_DATES = 10
# Google stamps `updated` a few seconds after `created` for a new event.
CHANGE_GRACE = timedelta(seconds=60)
TRACKED = (
    "title",
    "event_start",
    "event_end",
    "place",
    "description",
    "attendees",
    "responses",
    "recurrence",
)


def digest(value, size=24):
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:size]


def moment(value):
    """An aware datetime from an RFC 3339 / ISO string, keeping its offset."""
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else parsed.replace(tzinfo=UTC)


def iso(value):
    return value.isoformat() if value is not None else None


def _part(part):
    if not isinstance(part, dict):
        return None, False
    if part.get("dateTime"):
        return moment(part["dateTime"]), False
    if part.get("date"):
        try:
            day = date.fromisoformat(part["date"])
        except ValueError:
            return None, False
        # All-day events have no zone of their own: UTC midnight, day precision.
        return datetime.combine(day, time(), UTC), True
    return None, False


def times(item):
    """(start, end, all_day). Tombstones fall back to originalStartTime."""
    start, all_day = _part(item.get("start"))
    end, _ = _part(item.get("end"))
    if start is None:
        start, all_day = _part(item.get("originalStartTime"))
    if start is not None and end is not None and end < start:
        end = None
    return start, end, all_day


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

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.href = None

    def handle_starttag(self, tag, attrs):
        if tag in self.BREAKS:
            self.parts.append("\n")
        if tag == "a":
            self.href = dict(attrs).get("href")
            self.anchor = len(self.parts)

    def handle_endtag(self, tag):
        if tag == "a" and self.href:
            label = "".join(self.parts[self.anchor :]).strip()
            if self.href not in label:
                self.parts.append(f" ({self.href})")
            self.href = None
        elif tag in self.BREAKS:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def plain_text(value):
    """Event descriptions may be HTML; return readable text."""
    if not value:
        return None
    text = value
    if re.search(r"<[a-zA-Z/][^>]*>", value):
        parser = _Text()
        parser.feed(value)
        parser.close()
        text = "".join(parser.parts)
    text = "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text or None


def person(entry):
    if not isinstance(entry, dict):
        return None
    result = {
        "email": entry.get("email"),
        "name": entry.get("displayName"),
        "response": entry.get("responseStatus"),
        "organizer": entry.get("organizer") or None,
        "self": entry.get("self") or None,
        "optional": entry.get("optional") or None,
        "resource": entry.get("resource") or None,
    }
    return {k: v for k, v in result.items() if v is not None}


def author_of(entry):
    """An email identifier for a person; calendar addresses are not people."""
    entry = person(entry) or {}
    email = (entry.get("email") or "").strip()
    if not email or "@" not in email or email.lower().endswith("calendar.google.com"):
        return None
    return {
        "system": "email",
        "kind": "email",
        "value": email,
        "display_name": entry.get("name"),
    }


def conference_url(item):
    if item.get("hangoutLink"):
        return item["hangoutLink"]
    for entry in (item.get("conferenceData") or {}).get("entryPoints") or []:
        if (
            isinstance(entry, dict)
            and entry.get("entryPointType") == "video"
            and entry.get("uri")
        ):
            return entry["uri"]
    return None


def tracked(item):
    """Short digests of the fields a change record reports on."""
    start, end, _ = times(item)
    attendees = [person(a) or {} for a in item.get("attendees") or []]
    values = {
        "title": item.get("summary"),
        "event_start": iso(start),
        "event_end": iso(end),
        "place": item.get("location"),
        "description": item.get("description"),
        "attendees": sorted((a.get("email") or "").lower() for a in attendees),
        "responses": sorted(
            [(a.get("email") or "").lower(), a.get("response")] for a in attendees
        ),
        "recurrence": item.get("recurrence"),
    }
    return {key: digest(value, 8) for key, value in values.items()}


class Mapper:
    """Applies runs of raw records to the mapping state and collects messages.

    State (all JSON):
      calendars  "account\\tcalendar" -> {name, tz}
      snap       "account\\tcalendar" -> {occurrence id: [digest, start, end,
                 all_day, status, title, series id, entity ref]}
      units      "account\\tcalendar\\tevent id" -> {c: created recorded,
                 u: last updated, s: status, t: title, f: tracked digests, r: ref}
    """

    def __init__(self, state, *, calendars=(), exclude=(), emit=None):
        self.state = state
        self.calendars = state.setdefault("calendars", {})
        self.snap = state.setdefault("snap", {})
        self.units = state.setdefault("units", {})
        self.include = set(calendars or ())
        self.exclude = set(exclude or ())
        self.emit = emit  # emit(message, link_or_None)
        self.stats = {}

    def count(self, key):
        self.stats[key] = self.stats.get(key, 0) + 1

    def selected(self, calendar_id):
        if self.include and calendar_id not in self.include:
            return False
        return calendar_id not in self.exclude

    # --- helpers -------------------------------------------------------------

    def calendar_name(self, account, cid):
        return (self.calendars.get(account + "\t" + cid) or {}).get("name") or cid

    def thread(self, account, cid):
        return {
            "external_id": f"{account}:{cid}",
            "title": self.calendar_name(account, cid),
            "metadata": {"kind": "calendar", "account": account, "calendar_id": cid},
        }

    def ref(self, account, cid, item, previous=None):
        """Entity ref of the event: its iCalUID, else what is already known."""
        if item.get("iCalUID"):
            return "ical:" + item["iCalUID"]
        if previous:
            return previous
        series = item.get("recurringEventId")
        known = (
            (self.units.get(f"{account}\t{cid}\t{series}") or {}).get("r")
            if series
            else None
        )
        return known or f"gcal:{account}:{cid}:{series or item.get('id')}"

    @staticmethod
    def link(message, link_type, ref, name):
        return {
            "type": link_type,
            "from": {
                "channel": message["channel"],
                "external_id": message["external_id"],
            },
            "to": {"kind": "calendar_event", "external_ref": ref, "name": name},
            "method": "source",
        }

    def series_title(self, account, cid, series):
        if not series:
            return None
        return (self.units.get(f"{account}\t{cid}\t{series}") or {}).get("t")

    # --- runs ------------------------------------------------------------------

    def process(self, records):
        """Apply one fetch run (its records in fetch order)."""
        header = next((r for r in records if r.get("kind") == "run"), None)
        if header is None or not moment(header.get("fetched_at")):
            raise ValueError("an export run has no run header with fetched_at")
        observed = moment(header["fetched_at"]).astimezone(UTC)
        seen = {}
        windows = []
        for record in records:
            kind = record.get("kind")
            if kind in ("run", "run_end", "changes"):
                continue
            cid = record.get("calendar_id") if kind != "calendar" else record.get("id")
            account = record.get("account")
            if not account or not cid:
                self.count("skipped_malformed")
                continue
            if kind == "calendar":
                payload = record.get("payload") or {}
                self.calendars[account + "\t" + cid] = {
                    "name": payload.get("summaryOverride")
                    or payload.get("summary")
                    or cid,
                    "tz": payload.get("timeZone"),
                }
                continue
            if not self.selected(cid):
                self.count("skipped_calendar")
                continue
            touched = seen.setdefault(account + "\t" + cid, set())
            if kind == "occurrence":
                touched.add(record["id"])
                self.occurrence(account, cid, record.get("payload") or {}, observed)
            elif kind == "event":
                self.event(account, cid, record.get("payload") or {}, observed, touched)
            elif kind == "window":
                windows.append((account, cid, record))
            else:
                self.count("skipped_kind")
        # Vanished occurrences are judged at the end of the run, after the
        # change pass had a chance to show where a moved event went.
        for account, cid, record in windows:
            self.window(account, cid, record, seen[account + "\t" + cid], observed)

    # --- occurrences -------------------------------------------------------------

    def occurrence(self, account, cid, item, observed):
        if not item.get("id"):
            self.count("skipped_malformed")
            return
        self.refresh(account, cid, item)
        if item.get("recurringEventId"):
            self.series_instance(account, cid, item)
        else:
            self.unit(account, cid, item, observed)

    def refresh(self, account, cid, item):
        """Map one occurrence; emit it when it differs from the snapshot."""
        key = account + "\t" + cid
        entries = self.snap.setdefault(key, {})
        previous = entries.get(item["id"])
        message = self.occurrence_message(account, cid, item, previous)
        if message is None:
            self.count("skipped_no_time")
            return
        signature = digest(message)
        if previous is None or previous[0] != signature:
            ref = self.ref(account, cid, item, previous[7] if previous else None)
            title = message["fields"]["title"]
            self.emit(message, self.link(message, "part_of", ref, title))
            fields = message["fields"]
            entries[item["id"]] = [
                signature,
                message["happened_at"],
                message["ended_at"],
                fields["all_day"],
                fields["status"],
                title,
                fields["series_id"],
                ref,
            ]

    def occurrence_message(self, account, cid, item, previous):
        start, end, all_day = times(item)
        if start is None and previous:
            start, end, all_day = moment(previous[1]), moment(previous[2]), previous[3]
        if start is None:
            return None
        status = item.get("status") or "confirmed"
        series = item.get("recurringEventId")
        title = item.get("summary")
        if not title and status == "cancelled":
            title = (previous[5] if previous else None) or self.series_title(
                account, cid, series
            )
        title = title or "(no title)"
        calendar = self.calendar_name(account, cid)
        people = [p for p in (person(a) for a in item.get("attendees") or []) if p]
        organizer = person(item.get("organizer")) or {}
        me = next((p for p in people if p.get("self")), {})
        description = plain_text(item.get("description"))
        lines = [
            ("Cancelled: " if status == "cancelled" else "") + title,
            "When: " + when(start, end, all_day),
        ]
        if item.get("location"):
            lines.append("Where: " + item["location"])
        lines.append("Calendar: " + calendar)
        if people:
            shown = [
                (
                    f"{p['name']} <{p['email']}>"
                    if p.get("name") and p.get("email")
                    else p.get("name") or p.get("email") or "?"
                )
                + (f" ({p['response']})" if p.get("response") else "")
                for p in people[:ATTENDEE_LIMIT]
            ]
            extra = len(people) - ATTENDEE_LIMIT
            lines.append(
                "With: "
                + ", ".join(shown)
                + (f", and {extra} more" if extra > 0 else "")
            )
        if status not in ("confirmed", "cancelled"):
            lines.append("Status: " + status)
        if description:
            lines.append("")
            lines.append(description[:DESCRIPTION_LIMIT])
        original = (
            moment((item.get("originalStartTime") or {}).get("dateTime"))
            or (_part(item.get("originalStartTime"))[0])
        )
        fields = {
            "account": account,
            "calendar_id": cid,
            "calendar_name": calendar,
            "event_id": item["id"],
            "series_id": series,
            "ical_uid": item.get("iCalUID"),
            "title": title,
            "status": status,
            "all_day": all_day,
            "start_time_zone": (item.get("start") or {}).get("timeZone"),
            "original_start": iso(original),
            "place": item.get("location"),
            "description": description,
            "organizer_email": organizer.get("email"),
            "organizer_name": organizer.get("name"),
            "attendees": people or None,
            "attendee_count": len(people),
            "my_response": me.get("response"),
            "conference_url": conference_url(item),
            "html_link": item.get("htmlLink"),
            "event_type": item.get("eventType"),
            "visibility": item.get("visibility"),
            "transparency": item.get("transparency"),
            "event_created": iso(moment(item.get("created"))),
            "event_updated": iso(moment(item.get("updated"))),
            "observed_at": None,
            "mapping_revision": MAPPING_REVISION,
        }
        return {
            "channel": EVENTS,
            "external_id": f"{account}:{cid}:{item['id']}",
            "thread": self.thread(account, cid),
            "happened_at": iso(start),
            "ended_at": iso(end),
            "subject": subject(start, end, all_day),
            "author": author_of(item.get("organizer")),
            "content": "\n".join(lines),
            "fields": fields,
            "payload": {"source": item, "account": account, "calendar_id": cid},
        }

    def window(self, account, cid, record, seen, observed):
        """A complete occurrence pass: what vanished without a cancellation."""
        low, high = moment(record.get("time_min")), moment(record.get("time_max"))
        if low is None or high is None:
            self.count("skipped_malformed")
            return
        key = account + "\t" + cid
        entries = self.snap.get(key, {})
        if not seen and any(
            entry[4] not in ("cancelled", "removed")
            and moment(entry[1]) < high
            and (moment(entry[2]) or moment(entry[1])) > low
            for entry in entries.values()
        ):
            # A complete pass with nothing at all in it (not even tombstones)
            # while live occurrences are known there is far likelier a
            # misread answer than an emptied calendar: judge nothing.
            self.count("skipped_empty_window")
            return
        vanished_series = {}
        for event_id in sorted(entries):
            entry = entries[event_id]
            start, end = moment(entry[1]), moment(entry[2])
            end = end or start
            if end <= low and event_id not in seen:
                del entries[event_id]  # Left the window; nothing more to compare.
                continue
            if event_id in seen or entry[4] in ("cancelled", "removed"):
                continue
            if not (start < high and end > low):
                continue
            message = self.removed_message(
                account, cid, event_id, entry, observed, low, high
            )
            self.emit(message, self.link(message, "part_of", entry[7], entry[5]))
            entries[event_id] = [digest(message), *entry[1:4], "removed", *entry[5:]]
            if entry[6]:
                vanished_series.setdefault(entry[6], []).append(entry)
            else:
                self.unit_removed(account, cid, event_id, entry, observed)
        for series in sorted(vanished_series):
            self.series_removed(account, cid, series, vanished_series[series], observed)

    def removed_message(self, account, cid, event_id, entry, observed, low, high):
        start, end, all_day, title = (
            moment(entry[1]),
            moment(entry[2]),
            entry[3],
            entry[5],
        )
        calendar = self.calendar_name(account, cid)
        return {
            "channel": EVENTS,
            "external_id": f"{account}:{cid}:{event_id}",
            "thread": self.thread(account, cid),
            "happened_at": iso(start),
            "ended_at": iso(end),
            "subject": subject(start, end, all_day),
            "author": None,
            "content": "\n".join(
                [
                    "Removed from calendar: " + title,
                    "When: " + when(start, end, all_day),
                    "Calendar: " + calendar,
                ]
            ),
            "fields": {
                "account": account,
                "calendar_id": cid,
                "calendar_name": calendar,
                "event_id": event_id,
                "series_id": entry[6],
                "title": title,
                "status": "removed",
                "all_day": all_day,
                "observed_at": iso(observed),
                "mapping_revision": MAPPING_REVISION,
            },
            "payload": {
                "source": None,
                "account": account,
                "calendar_id": cid,
                "removed": {
                    "observed_at": iso(observed),
                    "window": [iso(low), iso(high)],
                },
            },
        }

    # --- lifecycle ------------------------------------------------------------------

    def change(
        self,
        account,
        cid,
        event_id,
        change,
        at,
        precision,
        item,
        *,
        observed,
        title,
        ref,
        changed=None,
        extra=None,
        occurrences=None,
    ):
        start, end, all_day = times(item) if item else (None, None, False)
        series = (item or {}).get("recurringEventId")
        noun = "occurrence of " if series else "event: "
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
        calendar = self.calendar_name(account, cid)
        lines = [head]
        if start is not None:
            lines.append("When: " + when(start, end, all_day))
        if item and item.get("location"):
            lines.append("Where: " + item["location"])
        lines.append("Calendar: " + calendar)
        if item and item.get("recurrence"):
            lines.append("Repeats: " + "; ".join(str(r) for r in item["recurrence"]))
        lines.extend(extra or [])
        stamp = iso(at.astimezone(UTC))
        external = f"{account}:{cid}:{event_id}:{change}"
        if change != "created":
            external += (":observed:" if precision == "observed" else ":") + stamp
        organizer = person((item or {}).get("organizer")) or {}
        creator = person((item or {}).get("creator")) or {}
        message = {
            "channel": CHANGES,
            "external_id": external,
            "thread": {
                "external_id": f"{account}:{cid}:{series or event_id}",
                "title": title,
                "metadata": {
                    "kind": "calendar_event",
                    "account": account,
                    "calendar_id": cid,
                },
            },
            "happened_at": iso(at),
            "ended_at": None,
            "subject": subject(start, end, all_day),
            "author": author_of(item.get("creator"))
            if (item and change == "created")
            else None,
            "content": "\n".join(lines),
            "fields": {
                "account": account,
                "calendar_id": cid,
                "calendar_name": calendar,
                "event_id": event_id,
                "series_id": series,
                "ical_uid": (item or {}).get("iCalUID"),
                "change": change,
                "changed_fields": changed,
                "title": title,
                "status": (item or {}).get("status"),
                "event_start": iso(start),
                "event_end": iso(end),
                "all_day": all_day if start is not None else None,
                "place": (item or {}).get("location"),
                "recurrence": (item or {}).get("recurrence"),
                "creator_email": creator.get("email"),
                "organizer_email": organizer.get("email"),
                "time_precision": precision,
                "observed_at": iso(observed) if precision == "observed" else None,
                "occurrence_count": occurrences,
                "mapping_revision": MAPPING_REVISION,
            },
            "payload": {"source": item, "account": account, "calendar_id": cid},
        }
        if precision == "observed":
            message["payload"]["observed_at"] = iso(observed)
        self.emit(message, self.link(message, "about", ref, title))
        self.count(change)
        return message

    def _unit(self, account, cid, event_id):
        key = f"{account}\t{cid}\t{event_id}"
        if key not in self.units:
            self.units[key] = {
                "c": False,
                "u": None,
                "s": None,
                "t": None,
                "f": None,
                "r": None,
            }
        return self.units[key]

    def event(self, account, cid, item, observed, touched):
        if not item.get("id"):
            self.count("skipped_malformed")
            return
        if item["id"] in self.snap.get(account + "\t" + cid, {}):
            # The same resource as a known occurrence (a single event or an
            # exception): it may have moved outside the window, not vanished.
            touched.add(item["id"])
            self.refresh(account, cid, item)
        self.unit(account, cid, item, observed)

    def unit(self, account, cid, item, observed):
        """Lifecycle of one event resource (master, single event or exception)."""
        event_id = item["id"]
        unit = self._unit(account, cid, event_id)
        status = item.get("status") or "confirmed"
        created, updated = moment(item.get("created")), moment(item.get("updated"))
        title = (
            item.get("summary")
            or unit["t"]
            or self.series_title(account, cid, item.get("recurringEventId"))
            or "(no title)"
        )
        unit["r"] = unit["r"] or self.ref(account, cid, item)
        common = dict(observed=observed, title=title, ref=unit["r"])
        if item.get("recurringEventId"):
            # An exception (a moved or cancelled instance) carries the series'
            # `created`; the series already has its one `created` record.
            unit["c"] = True
        if not unit["c"] and created is not None:
            self.change(
                account, cid, event_id, "created", created, "source", item, **common
            )
            unit["c"] = True
        changed_at = (
            updated if updated is not None and iso(updated) != unit["u"] else None
        )
        at, precision = (changed_at, "source") if changed_at else (observed, "observed")
        if status == "cancelled":
            if unit["s"] != "cancelled":
                self.change(
                    account, cid, event_id, "cancelled", at, precision, item, **common
                )
            unit.update(s="cancelled", u=iso(updated) or unit["u"], t=title)
            return
        fields = tracked(item)
        if unit["s"] in ("cancelled", "removed"):
            self.change(
                account, cid, event_id, "restored", at, precision, item, **common
            )
        elif unit["f"] is None:
            # First full sighting: only that it changed after creation is known.
            if created and updated and updated - created > CHANGE_GRACE:
                self.change(
                    account, cid, event_id, "changed", updated, "source", item, **common
                )
        else:
            changed = [key for key in TRACKED if fields.get(key) != unit["f"].get(key)]
            if changed:
                self.change(
                    account,
                    cid,
                    event_id,
                    "changed",
                    at,
                    precision,
                    item,
                    changed=changed,
                    **common,
                )
        unit.update(s=status, u=iso(updated) or unit["u"], t=title, f=fields)

    def series_instance(self, account, cid, item):
        """An occurrence of a recurring series: record that the series exists."""
        series = item["recurringEventId"]
        unit = self._unit(account, cid, series)
        unit["r"] = unit["r"] or self.ref(account, cid, item)
        if unit["t"] is None and item.get("summary"):
            unit["t"] = item["summary"]
        created = moment(item.get("created"))
        if unit["c"] or created is None or item.get("status") == "cancelled":
            return
        title = unit["t"] or "(no title)"
        # The series start is unknown here, so no subject period.
        stub = {
            k: item[k]
            for k in ("iCalUID", "creator", "organizer", "summary")
            if k in item
        }
        self.change(
            account,
            cid,
            series,
            "created",
            created,
            "source",
            stub,
            observed=None,
            title=title,
            ref=unit["r"],
        )
        unit["c"] = True

    def unit_removed(self, account, cid, event_id, entry, observed):
        unit = self._unit(account, cid, event_id)
        if unit["s"] in ("cancelled", "removed"):
            return
        item = {
            "start": _as_part(entry[1], entry[3]),
            "end": _as_part(entry[2], entry[3]),
        }
        self.change(
            account,
            cid,
            event_id,
            "removed",
            observed,
            "observed",
            item,
            observed=observed,
            title=entry[5],
            ref=entry[7],
        )
        unit["s"] = "removed"

    def series_removed(self, account, cid, series, entries, observed):
        dates = sorted(entry[1][:10] for entry in entries)
        listed = ", ".join(dates[:LISTED_DATES]) + (
            f", and {len(dates) - LISTED_DATES} more"
            if len(dates) > LISTED_DATES
            else ""
        )
        title = self.series_title(account, cid, series) or entries[0][5]
        self.change(
            account,
            cid,
            series,
            "removed",
            observed,
            "observed",
            {"recurringEventId": series},
            observed=observed,
            title=title,
            ref=entries[0][7],
            extra=[f"{len(entries)} occurrence(s) gone: {listed}"],
            occurrences=len(entries),
        )


def _as_part(value, all_day):
    if value is None:
        return None
    return {"date": value[:10]} if all_day else {"dateTime": value}
