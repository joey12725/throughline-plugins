"""Map spooled tracker requests (and OwnTracks Recorder rows) to track points
and messages. The same functions serve every mode, so a point pushed live and
the same point read from the Recorder normalise identically."""

from datetime import datetime, timezone

SOURCE = "gps_push"
MAPPING_REVISION = 1

TRANSITIONS = "gps_push.transitions"
REGIONS = "gps_push.regions"

FAR_FUTURE_YEAR = 3000  # Overland/iOS reports an open visit's departure as 4001-01-01


def _float(value):
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None


def instant(value):
    """An aware UTC datetime from an epoch number or ISO-ish string, else None."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    number = _float(value)
    if number is not None:
        if number > 1e12:  # milliseconds
            number /= 1000
        try:
            return datetime.fromtimestamp(number, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1).replace(" ", "")
    try:
        at = datetime.fromisoformat(text)
    except ValueError:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at.astimezone(timezone.utc)


def valid_fix(lat, lon):
    return (
        lat is not None
        and lon is not None
        and -90 <= lat <= 90
        and -180 <= lon <= 180
        and not (lat == 0 and lon == 0)
    )


def device_key(user, device):
    user = (user or "").strip()
    device = (device or "").strip()
    if user and device:
        return f"{user}/{device}"
    return user or device or "unknown"


def point(at, lat, lon, accuracy):
    return {
        "at": at.isoformat(),
        "lat": lat,
        "lon": lon,
        "accuracy_m": accuracy,
        "source": SOURCE,
    }


def _clean(fields):
    return {k: v for k, v in fields.items() if v is not None}


# ------------------------------------------------------------------ OwnTracks


def owntracks_identity(record, item):
    user, device = record.get("user"), record.get("device")
    topic = item.get("topic")
    if isinstance(topic, str) and topic.count("/") >= 2:
        parts = topic.split("/")
        user = user or parts[1]
        device = device or parts[2]
    return device_key(user, device)


def owntracks_location(item):
    lat, lon = _float(item.get("lat")), _float(item.get("lon"))
    at = instant(item.get("tst"))
    if at is None or not valid_fix(lat, lon):
        return None
    return point(at, lat, lon, _float(item.get("acc")))


def owntracks_transition(item, device):
    at = instant(item.get("tst"))
    event = str(item.get("event") or "").lower()
    if at is None or event not in ("enter", "leave"):
        return None
    region = item.get("desc") or item.get("rid") or "unknown region"
    lat, lon, acc = _float(item.get("lat")), _float(item.get("lon")), _float(item.get("acc"))
    message = {
        "channel": TRANSITIONS,
        "external_id": f"owntracks:{device}:{int(at.timestamp())}:{event}:{item.get('rid') or region}",
        "thread": {"external_id": f"region:{region}", "title": f"Region {region}"},
        "happened_at": at.isoformat(),
        "content": f"{'Entered' if event == 'enter' else 'Left'} {region}",
        "fields": _clean(
            {
                "event": event,
                "region": str(region),
                "region_id": item.get("rid"),
                "device": device,
                "app": "owntracks",
                "trigger": item.get("t"),
                "accuracy_m": acc,
                "mapping_revision": MAPPING_REVISION,
            }
        ),
        "payload": item,
    }
    if valid_fix(lat, lon):
        message["location"] = _clean({"lat": lat, "lon": lon, "accuracy_m": acc})
    return message


def owntracks_waypoint(item, device):
    at = instant(item.get("tst"))
    lat, lon = _float(item.get("lat")), _float(item.get("lon"))
    desc = item.get("desc")
    if at is None or not desc:
        return None
    radius = _float(item.get("rad"))
    where = f"{radius:g} m around {lat:.5f}, {lon:.5f}" if valid_fix(lat, lon) and radius else "no geofence"
    return {
        "channel": REGIONS,
        "external_id": f"owntracks:{device}:{item.get('rid') or int(at.timestamp())}",
        "thread": {"external_id": f"regions:{device}", "title": f"Regions of {device}"},
        "happened_at": at.isoformat(),
        "content": f"Region {desc}: {where}",
        "fields": _clean(
            {
                "region": str(desc),
                "region_id": item.get("rid"),
                "radius_m": radius,
                "center_lat": lat if valid_fix(lat, lon) else None,
                "center_lon": lon if valid_fix(lat, lon) else None,
                "device": device,
                "app": "owntracks",
                "mapping_revision": MAPPING_REVISION,
            }
        ),
        "payload": item,
    }


def map_owntracks(record):
    body = record.get("body")
    items = body if isinstance(body, list) else [body]
    for item in items:
        if not isinstance(item, dict):
            continue
        device = owntracks_identity(record, item)
        kind = item.get("_type")
        if kind == "location":
            fix = owntracks_location(item)
            if fix:
                yield device, "track", fix
        elif kind == "transition":
            message = owntracks_transition(item, device)
            if message:
                yield device, "message", message
        elif kind == "waypoint":
            message = owntracks_waypoint(item, device)
            if message:
                yield device, "message", message
        elif kind == "waypoints":
            for waypoint in item.get("waypoints") or []:
                if isinstance(waypoint, dict):
                    message = owntracks_waypoint(waypoint, device)
                    if message:
                        yield device, "message", message
        # lwt, card, status, cmd, steps, beacon, ...: nothing to record


# ------------------------------------------------------------------- Overland


def map_overland(record):
    for feature in record["body"].get("locations") or []:
        if not isinstance(feature, dict):
            continue
        props = feature.get("properties") or {}
        coords = (feature.get("geometry") or {}).get("coordinates") or []
        lon = _float(coords[0]) if len(coords) > 0 else None
        lat = _float(coords[1]) if len(coords) > 1 else None
        device = device_key(record.get("user"), props.get("device_id") or record.get("device"))
        accuracy = _float(props.get("horizontal_accuracy"))
        if props.get("type") == "visit":
            message = overland_visit(props, lat, lon, accuracy, device, feature)
            if message:
                yield device, "message", message
            continue
        at = instant(props.get("timestamp"))
        if at is not None and valid_fix(lat, lon):
            yield device, "track", point(at, lat, lon, accuracy)


def overland_visit(props, lat, lon, accuracy, device, feature):
    arrived = instant(props.get("arrival_date"))
    departed = instant(props.get("departure_date"))
    if departed and departed.year >= FAR_FUTURE_YEAR:
        departed = None
    if arrived is None or arrived.year < 1971:
        # iOS reports an unknown arrival as the distant past; the departure
        # is then the only reliable time.
        if departed is None:
            return None
        arrived, event_at, label = None, departed, "Left a place"
    else:
        event_at, label = arrived, "Visit"
    parts = [label]
    if arrived:
        parts.append(f"arrived {arrived.strftime('%H:%M')} UTC")
    if departed:
        parts.append(f"departed {departed.strftime('%H:%M')} UTC")
    else:
        parts.append("still there")
    message = {
        "channel": TRANSITIONS,
        "external_id": f"overland:{device}:visit:{event_at.isoformat()}",
        "thread": {"external_id": f"visits:{device}", "title": f"Visits of {device}"},
        "happened_at": event_at.isoformat(),
        "ended_at": departed.isoformat() if departed and departed > event_at else None,
        "content": ", ".join(parts),
        "fields": _clean(
            {
                "event": "visit",
                "device": device,
                "app": "overland",
                "accuracy_m": accuracy,
                "arrived_at": arrived.isoformat() if arrived else None,
                "departed_at": departed.isoformat() if departed else None,
                "mapping_revision": MAPPING_REVISION,
            }
        ),
        "payload": feature,
    }
    if valid_fix(lat, lon):
        message["location"] = _clean({"lat": lat, "lon": lon, "accuracy_m": accuracy})
    return message


# ------------------------------------------------------------------ GPSLogger


def _first(values, *keys):
    for key in keys:
        if values.get(key) not in (None, ""):
            return values[key]
    return None


def map_gpslogger(record):
    values = record["body"] if isinstance(record.get("body"), dict) else {}
    lat = _float(_first(values, "lat", "latitude"))
    lon = _float(_first(values, "lon", "lng", "longitude"))
    at = instant(_first(values, "time", "timestamp", "tst", "date")) or instant(record.get("received_at"))
    device = device_key(record.get("user"), _first(values, "device", "ser", "aid", "profile") or record.get("device"))
    if at is not None and valid_fix(lat, lon):
        yield device, "track", point(at, lat, lon, _float(_first(values, "acc", "accuracy", "hdop_m")))


MAPPERS = {"owntracks": map_owntracks, "overland": map_overland, "gpslogger": map_gpslogger}


def map_record(record):
    """(device, 'track'|'message', data) for one spooled request."""
    if not isinstance(record, dict) or record.get("format") not in MAPPERS:
        return
    yield from MAPPERS[record["format"]](record)
