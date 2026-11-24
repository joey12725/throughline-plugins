"""iCalendar text -> normalized occurrences and event units (see calmap).

Recurrence expansion (RRULE, RDATE, EXDATE, RECURRENCE-ID overrides, time
zones) is done by `recurring-ical-events` on top of `icalendar`.
"""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import icalendar
import recurring_ical_events

from . import calmap

# Volatile properties: DTSTAMP is the time the file was generated on many
# feeds, so keeping it would make every fetch a new version.
VOLATILE = {"DTSTAMP"}
CONFERENCE = ("X-GOOGLE-CONFERENCE", "X-MICROSOFT-SKYPETEAMSMEETINGURL", "X-MICROSOFT-ONLINEMEETINGURL")


def parse(text):
    try:
        return icalendar.Calendar.from_ical(text)
    except (ValueError, KeyError, IndexError) as error:
        raise ValueError(f"unreadable iCalendar data: {error}") from None


def calendar_name(cal):
    name = cal.get("X-WR-CALNAME")
    return str(name) if name else None


def _zone(name):
    try:
        return ZoneInfo(name) if name else UTC
    except (ZoneInfoNotFoundError, ValueError):
        return UTC


def _when(value, floating):
    """(aware datetime, all_day) for a DATE or DATE-TIME value."""
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            value = value.replace(tzinfo=floating)
        return value, False
    if isinstance(value, date):
        return datetime.combine(value, time(), UTC), True
    return None, False


def _value(component, name):
    prop = component.get(name)
    return getattr(prop, "dt", None) if prop is not None else None


def _text(component, name):
    value = component.get(name)
    if value is None:
        return None
    if isinstance(value, list):
        value = value[0]
    text = str(value).strip()
    return text or None


def _address(prop):
    value = str(prop or "").strip()
    if value.lower().startswith("mailto:"):
        value = value[7:]
    return value or None


def _list(component, name):
    value = component.get(name)
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def person(prop, owner=()):
    params = getattr(prop, "params", {}) or {}
    email = _address(prop)
    role = str(params.get("ROLE", "")).upper()
    cutype = str(params.get("CUTYPE", "")).upper()
    return {
        "email": email,
        "name": str(params["CN"]) if params.get("CN") else None,
        "response": calmap.response(params.get("PARTSTAT")),
        "optional": role == "OPT-PARTICIPANT" or None,
        "resource": cutype in ("RESOURCE", "ROOM") or None,
        "self": bool(email and email.lower() in owner) or None,
    }


def lines(component):
    """The component's content lines, unfolded, without volatile ones."""
    text = component.to_ical().decode("utf-8", "replace")
    text = text.replace("\r\n ", "").replace("\r\n\t", "")
    return [
        line
        for line in text.split("\r\n")
        if line and line.split(":", 1)[0].split(";", 1)[0].upper() not in VOLATILE
    ]


def recurrence_lines(component):
    return [line for line in lines(component) if line.split(":", 1)[0].split(";", 1)[0].upper() in ("RRULE", "RDATE", "EXDATE", "EXRULE")] or None


def _id_stamp(value):
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(UTC)
            return value.strftime("%Y%m%dT%H%M%SZ")
        return value.strftime("%Y%m%dT%H%M%S")
    return value.strftime("%Y%m%d")


def _common(component, floating, owner):
    organizer = component.get("ORGANIZER")
    attendees = [person(a, owner) for a in _list(component, "ATTENDEE")]
    status = (_text(component, "STATUS") or "confirmed").lower()
    created, _ = _when(_value(component, "CREATED"), UTC)
    updated, _ = _when(_value(component, "LAST-MODIFIED"), UTC)
    org = person(organizer, owner) if organizer is not None else None
    return {
        "ical_uid": _text(component, "UID"),
        "title": _text(component, "SUMMARY"),
        "status": status if status in ("confirmed", "tentative", "cancelled") else status,
        "place": _text(component, "LOCATION"),
        "description": _text(component, "DESCRIPTION"),
        "organizer": {"email": org["email"], "name": org["name"]} if org else None,
        "creator": None,
        "attendees": attendees,
        "created": created,
        "updated": updated,
    }


def events(cal, low, high, *, owner=(), floating=UTC, singles_anywhere=False):
    """(units, occurrences) of every VEVENT in `cal` within [low, high).

    units: {uid: unit dict}; occurrences: [occurrence dict]. With
    singles_anywhere, non-recurring events outside the window are included
    too (a known event that moved away from the window)."""
    owner = {o.lower() for o in owner}
    by_uid = {}
    for component in cal.walk("VEVENT"):
        uid = _text(component, "UID")
        if uid:
            by_uid.setdefault(uid, []).append(component)
    units, series = {}, set()
    for uid, components in sorted(by_uid.items()):
        masters = [c for c in components if c.get("RECURRENCE-ID") is None]
        master = masters[0] if masters else components[0]
        if any(c.get("RRULE") is not None or c.get("RDATE") is not None for c in masters) or any(
            c.get("RECURRENCE-ID") is not None for c in components
        ):
            series.add(uid)
        start, all_day = _when(_value(master, "DTSTART"), floating)
        end, _ = _when(_value(master, "DTEND"), floating)
        unit = _common(master, floating, owner)
        unit.update(
            unit_id=uid,
            start=start,
            end=end,
            all_day=all_day,
            recurrence=recurrence_lines(master),
            source=lines(master),
        )
        units[uid] = unit
    try:
        expanded = recurring_ical_events.of(cal, skip_bad_series=True).between(low, high)
    except Exception as error:  # noqa: BLE001 - the library raises many kinds
        raise ValueError(f"recurrence expansion failed: {error}") from None
    occurrences = [_occurrence(c, series, floating, owner) for c in expanded]
    if singles_anywhere:
        seen = {o["occurrence_id"] for o in occurrences}
        for uid, components in sorted(by_uid.items()):
            if uid in series:
                continue
            occ = _occurrence(components[0], series, floating, owner)
            if occ["occurrence_id"] not in seen and occ["start"] is not None:
                occurrences.append(occ)
    return units, [o for o in occurrences if o["start"] is not None]


def _occurrence(component, series, floating, owner):
    uid = _text(component, "UID")
    start_value = _value(component, "DTSTART")
    start, all_day = _when(start_value, floating)
    end, _ = _when(_value(component, "DTEND"), floating)
    if end is None and start is not None:
        duration = _value(component, "DURATION")
        if isinstance(duration, timedelta):
            end = start + duration
        elif all_day:
            end = start + timedelta(days=1)
    recurrence = _value(component, "RECURRENCE-ID")
    is_series = uid in series
    if is_series:
        occurrence_id = f"{uid}/{_id_stamp(recurrence if recurrence is not None else start_value)}"
    else:
        occurrence_id = uid
    original, _ = _when(recurrence, floating) if (is_series and recurrence is not None) else (None, False)
    if original is not None and start is not None and original == start:
        original = None  # An unmoved occurrence.
    params = getattr(component.get("DTSTART"), "params", {}) or {}
    occ = _common(component, floating, owner)
    me = next((a for a in occ["attendees"] if a.get("self")), {})
    occ.update(
        occurrence_id=occurrence_id,
        unit_id=uid,
        series_id=uid if is_series else None,
        start=start,
        end=end,
        all_day=all_day,
        time_zone=str(params["TZID"]) if params.get("TZID") else None,
        original_start=original,
        my_response=me.get("response"),
        conference_url=next((_text(component, k) for k in CONFERENCE if _text(component, k)), None),
        html_link=_text(component, "URL"),
        event_type=None,
        visibility=(_text(component, "CLASS") or "").lower() or None,
        transparency=(_text(component, "TRANSP") or "").lower() or None,
        source=lines(component),
    )
    return occ


def zone(name):
    return _zone(name)
