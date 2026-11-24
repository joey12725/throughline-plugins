"""When a recording started, with an honest precision.

Order of evidence (first usable wins):

1. `com.apple.quicktime.creationdate` tag (iPhone Voice Memos, many phone
   recorders): local time *with* an offset. precision exact.
2. A date and time in the file name (`2025-03-04 10-15-00`, `REC_20250304_101500`,
   `20250304101500`, `2025-03-04T10.15`): the recorder's local clock, read in
   the configured zone. precision exact.
3. `creation_time` tag (MP4/M4A/OGG): UTC; can be the time of a later
   conversion, so it ranks below the file name. precision exact.
4. A date alone in the file name: precision day.
5. The file's modification time minus the duration (recorders usually write
   the file when the recording stops): precision approximate.
"""

import re
from datetime import datetime, timedelta, timezone

EARLIEST = datetime(1990, 1, 1, tzinfo=timezone.utc)

FULL = re.compile(
    r"(?<!\d)(?P<y>(?:19|20)\d{2})[-_.]?(?P<m>[01]\d)[-_.]?(?P<d>[0-3]\d)"
    r"[ T_.-]{0,3}(?P<H>[0-2]\d)[-_.:h]?(?P<M>[0-5]\d)(?:[-_.:m]?(?P<S>[0-5]\d))?(?!\d)"
)
DAY = re.compile(r"(?<!\d)(?P<y>(?:19|20)\d{2})[-_.]?(?P<m>[01]\d)[-_.]?(?P<d>[0-3]\d)(?!\d)")


def parse_tag(value):
    """An aware datetime from a metadata tag, or None."""
    text = (value or "").strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    # 2025-03-04T10:15:00-0800 -> -08:00
    text = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", text)
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when if when >= EARLIEST else None


def from_name(stem, zone):
    """(aware datetime, precision) from a file name, or (None, None)."""
    for match in FULL.finditer(stem):
        g = match.groupdict()
        try:
            when = datetime(
                int(g["y"]), int(g["m"]), int(g["d"]), int(g["H"]), int(g["M"]), int(g["S"] or 0)
            )
        except ValueError:
            continue
        return when.replace(tzinfo=zone), "exact"
    for match in DAY.finditer(stem):
        try:
            when = datetime(int(match["y"]), int(match["m"]), int(match["d"]))
        except ValueError:
            continue
        return when.replace(tzinfo=zone), "day"
    return None, None


def recording_start(stem, tags, mtime, duration, zone):
    """(aware datetime, time_source, precision)."""
    tagged = parse_tag(tags.get("com.apple.quicktime.creationdate"))
    if tagged:
        return tagged, "quicktime_creationdate", "exact"
    named, precision = from_name(stem, zone)
    if named and precision == "exact":
        return named, "file_name", "exact"
    created = parse_tag(tags.get("creation_time"))
    if created:
        return created.astimezone(zone), "creation_time", "exact"
    if named:
        return named, "file_name", precision
    modified = datetime.fromtimestamp(mtime, tz=timezone.utc).astimezone(zone)
    if duration:
        return modified - timedelta(seconds=duration), "mtime_minus_duration", "approximate"
    return modified, "mtime", "approximate"
