"""Deterministic mapping of Health Auto Export documents to Throughline
messages, shaped like the `health` (Oura/scale) connector: one channel per
kind of record, threads per channel (and metric) and month, `provider`,
`mapping_revision` and `day` fields, typed metrics, no author (all of it is
the owner's own data), and the unchanged source object as the payload.
"""

import math
from datetime import datetime, timezone

MAPPING_REVISION = 1
PROVIDER = "health_auto_export"
TRACK_SOURCE = "apple_health"

METRICS = "apple_health.metrics"
HEART_RATE = "apple_health.heart_rate"
SLEEP = "apple_health.sleep"
WEIGHT = "apple_health.weight"
WORKOUTS = "apple_health.workouts"
CHANNELS = (METRICS, HEART_RATE, SLEEP, WEIGHT, WORKOUTS)

KG_PER_LB = 0.45359237
WEIGHT_METRICS = {"weight_body_mass", "body_mass", "weight"}
HEART_RATE_METRICS = {"heart_rate"}
SLEEP_METRICS = {"sleep_analysis"}

COMMON = [
    ("provider", "text", "Source app: health_auto_export"),
    ("mapping_revision", "integer", "Revision of the plugin mapping that produced this record"),
    ("day", "text", "Calendar day (YYYY-MM-DD) of the record in the offset the app reported"),
    ("source", "text", "Apple Health source (device or app) that recorded the sample"),
]
METRIC_FIELDS = [
    ("metric", "text", "Health Auto Export metric name, e.g. step_count, active_energy, heart_rate_variability"),
    ("value", "number", "Sample value (qty), or the average for min/avg/max metrics"),
    ("units", "text", "Unit of value as the app reports it"),
    ("value_min", "number", "Minimum, for metrics reported as Min/Avg/Max"),
    ("value_max", "number", "Maximum, for metrics reported as Min/Avg/Max"),
    ("systolic", "number", "Blood pressure systolic, mmHg"),
    ("diastolic", "number", "Blood pressure diastolic, mmHg"),
]
HEART_RATE_FIELDS = [
    ("bpm_min", "number", "Lowest heart rate in the sample interval, bpm"),
    ("bpm_max", "number", "Highest heart rate in the sample interval, bpm"),
    ("bpm_mean", "number", "Average heart rate in the sample interval, bpm"),
    ("units", "text", "Unit as the app reports it (count/min)"),
]
SLEEP_FIELDS = [
    ("aggregated", "boolean", "A nightly summary (true) or one sleep stage segment (false)"),
    ("stage", "text", "Stage of a segment: Core, Deep, REM, Awake, In Bed, Asleep"),
    ("total_sleep_s", "integer", "Total sleep, seconds"),
    ("asleep_s", "integer", "Unspecified asleep time, seconds"),
    ("core_sleep_s", "integer", "Core (light) sleep, seconds"),
    ("deep_sleep_s", "integer", "Deep sleep, seconds"),
    ("rem_sleep_s", "integer", "REM sleep, seconds"),
    ("awake_s", "integer", "Awake time, seconds"),
    ("time_in_bed_s", "integer", "Time in bed, seconds"),
    ("duration_s", "integer", "Length of a segment, seconds"),
    ("sleep_start", "timestamp", "Sleep start"),
    ("sleep_end", "timestamp", "Sleep end"),
    ("in_bed_start", "timestamp", "In-bed start"),
    ("in_bed_end", "timestamp", "In-bed end"),
]
WEIGHT_FIELDS = [
    ("weight", "number", "Body weight in weight_unit"),
    ("weight_unit", "text", "Unit of weight (lb or kg)"),
    ("weight_kg", "number", "Body weight in kilograms"),
]
WORKOUT_FIELDS = [
    ("activity", "text", "Workout type, e.g. Outdoor Run"),
    ("workout_id", "text", "Health Auto Export workout id"),
    ("duration_s", "integer", "Workout length, seconds"),
    ("calories", "number", "Active energy burned, kcal"),
    ("distance_m", "number", "Distance, metres"),
    ("heart_rate_mean", "number", "Average heart rate, bpm"),
    ("heart_rate_max", "number", "Maximum heart rate, bpm"),
    ("heart_rate_min", "number", "Minimum heart rate, bpm"),
    ("elevation_gain_m", "number", "Elevation climbed, metres"),
    ("steps", "integer", "Steps during the workout"),
    ("environment", "text", "Indoor or Outdoor"),
    ("temperature", "number", "Temperature reported for the workout, in its own unit"),
    ("humidity", "number", "Humidity reported for the workout, percent"),
    ("route_points", "integer", "GPS route points recorded (the route itself is not kept in the payload)"),
]


def declared(*groups):
    return [{"name": n, "type": t, "description": d} for group in groups for n, t, d in group]


FIELDS = {
    METRICS: declared(COMMON, METRIC_FIELDS),
    HEART_RATE: declared(COMMON, HEART_RATE_FIELDS),
    SLEEP: declared(COMMON, SLEEP_FIELDS),
    WEIGHT: declared(COMMON, WEIGHT_FIELDS),
    WORKOUTS: declared(COMMON, WORKOUT_FIELDS),
}


# ------------------------------------------------------------------ helpers


def number(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, dict):
        return number(value.get("qty"))
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def integer(value):
    value = number(value)
    return int(round(value)) if value is not None else None


def instant(value):
    """An aware datetime, keeping the reported offset. Health Auto Export
    writes 'yyyy-MM-dd HH:mm:ss Z' (e.g. 2024-03-01 07:30:00 -0800)."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    for pattern in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M %z"):
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            pass
    try:
        at = datetime.fromisoformat(text)
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def iso(at):
    return at.isoformat() if at else None


def utc_key(at):
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt(value, digits=1):
    if value is None:
        return None
    text = f"{value:,.{digits}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def duration(seconds):
    if seconds is None:
        return None
    hours, minutes = divmod(round(seconds / 60), 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def hours_to_s(value):
    value = number(value)
    return int(round(value * 3600)) if value is not None else None


def label(metric):
    return metric.replace("_", " ").capitalize()


def clean(fields):
    return {k: v for k, v in fields.items() if v is not None}


def base(at, source):
    return {
        "provider": PROVIDER,
        "mapping_revision": MAPPING_REVISION,
        "day": at.date().isoformat(),
        "source": source or None,
    }


def thread(key, at, title):
    month = at.strftime("%Y-%m")
    return {"external_id": f"{key}:{month}", "title": f"{title} {month}"}


def message(channel, external_id, at, content, fields, payload, *, thread_key, title, ended=None):
    return {
        "channel": channel,
        "external_id": external_id,
        "thread": thread(thread_key, at, title),
        "happened_at": at.isoformat(),
        "ended_at": ended.isoformat() if ended and ended > at else None,
        "content": content,
        "fields": clean(fields),
        "payload": payload,
    }


def sample_id(metric, at, source, extra=None):
    key = f"{metric}:{utc_key(at)}:{source or ''}"
    return f"{key}:{extra}" if extra else key


# ------------------------------------------------------------------ metrics


def metric_messages(metric):
    name = str(metric.get("name") or "").strip()
    units = metric.get("units")
    if not name:
        return
    for sample in metric.get("data") or []:
        if not isinstance(sample, dict):
            continue
        if name in SLEEP_METRICS:
            out = sleep_message(sample, units)
        elif name in HEART_RATE_METRICS:
            out = heart_rate_message(sample, units)
        elif name in WEIGHT_METRICS:
            out = weight_message(name, sample, units)
        else:
            out = generic_message(name, sample, units)
        if out:
            yield out


def _payload(name, units, sample):
    return {"metric": name, "units": units, "sample": sample}


def generic_message(name, sample, units):
    at = instant(sample.get("date") or sample.get("startDate"))
    if at is None:
        return None
    source = sample.get("source")
    fields = base(at, source)
    value = number(sample.get("qty"))
    low, avg, high = number(sample.get("Min")), number(sample.get("Avg")), number(sample.get("Max"))
    systolic, diastolic = number(sample.get("systolic")), number(sample.get("diastolic"))
    if value is None:
        value = avg
    if value is None and systolic is None:
        return None
    fields.update(metric=name, value=value, units=units, value_min=low, value_max=high, systolic=systolic, diastolic=diastolic)
    if systolic is not None:
        text = f"{label(name)}: {fmt(systolic, 0)}/{fmt(diastolic, 0)} {units or 'mmHg'}"
    else:
        text = f"{label(name)}: {fmt(value, 2)}" + (f" {units}" if units else "")
        if low is not None and high is not None:
            text += f" (min {fmt(low, 2)}, max {fmt(high, 2)})"
    if source:
        text += f" [{source}]"
    ended = instant(sample.get("endDate"))
    return message(
        METRICS, sample_id(name, at, source), at, text, fields, _payload(name, units, sample),
        thread_key=name, title=label(name), ended=ended,
    )


def heart_rate_message(sample, units):
    at = instant(sample.get("date") or sample.get("startDate"))
    if at is None:
        return None
    source = sample.get("source")
    low, avg, high = number(sample.get("Min")), number(sample.get("Avg")), number(sample.get("Max"))
    if avg is None:
        avg = number(sample.get("qty"))
    if avg is None and low is None and high is None:
        return None
    fields = base(at, source)
    fields.update(bpm_min=low, bpm_max=high, bpm_mean=avg, units=units)
    if low is not None and high is not None and low != high:
        text = f"Heart rate {fmt(low, 0)}-{fmt(high, 0)} bpm, mean {fmt(avg, 0)}"
    else:
        text = f"Heart rate {fmt(avg, 0)} bpm"
    if source:
        text += f" [{source}]"
    return message(
        HEART_RATE, sample_id("heart_rate", at, source), at, text, fields, _payload("heart_rate", units, sample),
        thread_key="heart_rate", title="Heart rate",
    )


def weight_message(name, sample, units):
    at = instant(sample.get("date"))
    value = number(sample.get("qty"))
    if at is None or value is None:
        return None
    source = sample.get("source")
    unit = "kg" if str(units or "kg").lower().startswith("kg") else "lb"
    fields = base(at, source)
    fields.update(
        weight=round(value, 2),
        weight_unit=unit,
        weight_kg=round(value * KG_PER_LB, 2) if unit == "lb" else round(value, 2),
    )
    text = f"Weigh-in: {fmt(value)} {unit}" + (f" [{source}]" if source else "")
    return message(
        WEIGHT, sample_id(name, at, source), at, text, fields, _payload(name, units, sample),
        thread_key="weight", title="Weigh-ins",
    )


STAGES = (
    ("deep", "deep_sleep_s", "deep"),
    ("rem", "rem_sleep_s", "REM"),
    ("core", "core_sleep_s", "core"),
    ("asleep", "asleep_s", "asleep"),
    ("awake", "awake_s", "awake"),
)


def sleep_message(sample, units):
    if "value" in sample and sample.get("startDate"):
        return sleep_segment(sample, units)
    day = instant(sample.get("date"))
    start = instant(sample.get("sleepStart")) or instant(sample.get("inBedStart"))
    end = instant(sample.get("sleepEnd")) or instant(sample.get("inBedEnd"))
    at = start or day
    if at is None:
        return None
    source = sample.get("source")
    fields = base(day or at, source)
    total = hours_to_s(sample.get("totalSleep"))
    if total is None:
        total = hours_to_s(sample.get("asleep"))
    fields.update(
        aggregated=True,
        total_sleep_s=total,
        time_in_bed_s=hours_to_s(sample.get("inBed")),
        sleep_start=iso(start),
        sleep_end=iso(end),
        in_bed_start=iso(instant(sample.get("inBedStart"))),
        in_bed_end=iso(instant(sample.get("inBedEnd"))),
    )
    for key, name, _ in STAGES:
        fields[name] = hours_to_s(sample.get(key))
    parts = [f"{fname} {duration(fields[name])}" for key, name, fname in STAGES if fields.get(name)]
    text = f"Slept {duration(total)}" if total is not None else "Sleep"
    if start and end:
        text += f" ({start.strftime('%H:%M')}-{end.strftime('%H:%M')})"
    if parts:
        text += ": " + ", ".join(parts)
    if fields.get("time_in_bed_s"):
        text += f"; in bed {duration(fields['time_in_bed_s'])}"
    if source:
        text += f" [{source}]"
    return message(
        SLEEP, sample_id("sleep_analysis", day or at, source), at, text, fields,
        _payload("sleep_analysis", units, sample), thread_key="sleep", title="Sleep", ended=end,
    )


def sleep_segment(sample, units):
    start, end = instant(sample.get("startDate")), instant(sample.get("endDate"))
    if start is None:
        return None
    stage = str(sample.get("value") or "Asleep")
    source = sample.get("source")
    seconds = int((end - start).total_seconds()) if end and end > start else hours_to_s(sample.get("qty"))
    fields = base(start, source)
    fields.update(aggregated=False, stage=stage, duration_s=seconds)
    text = f"Sleep stage {stage}: {duration(seconds)}" + (f" [{source}]" if source else "")
    return message(
        SLEEP, sample_id("sleep_analysis", start, source, stage), start, text, fields,
        _payload("sleep_analysis", units, sample), thread_key="sleep", title="Sleep", ended=end,
    )


# ----------------------------------------------------------------- workouts

DISTANCE_M = {"m": 1, "km": 1000, "mi": 1609.344, "yd": 0.9144, "ft": 0.3048}


def quantity(value):
    """(qty, units) from {qty, units}, a number, or a list of {qty} samples."""
    if isinstance(value, dict):
        return number(value.get("qty")), value.get("units")
    if isinstance(value, list):
        values = [number(v.get("qty")) for v in value if isinstance(v, dict)]
        values = [v for v in values if v is not None]
        units = next((v.get("units") for v in value if isinstance(v, dict) and v.get("units")), None)
        return (sum(values) if values else None), units
    return number(value), None


def energy_kcal(value):
    qty, units = quantity(value)
    if qty is None:
        return None
    return round(qty / 4.184, 1) if str(units or "kcal").lower() == "kj" else round(qty, 1)


def distance_m(value):
    qty, units = quantity(value)
    if qty is None:
        return None
    return round(qty * DISTANCE_M.get(str(units or "km").lower(), 1000), 1)


def route_point(p):
    lat = number(p.get("latitude", p.get("lat")))
    lon = number(p.get("longitude", p.get("lon")))
    at = instant(p.get("timestamp"))
    if at is None or lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    if lat == 0 and lon == 0:
        return None
    accuracy = number(p.get("horizontalAccuracy", p.get("hAcc")))
    return {"at": at.astimezone(timezone.utc).isoformat(), "lat": lat, "lon": lon, "accuracy_m": accuracy, "source": TRACK_SOURCE}


def workout_message(workout):
    start, end = instant(workout.get("start")), instant(workout.get("end"))
    if start is None:
        return None, []
    activity = str(workout.get("name") or "Workout")
    route = [p for p in workout.get("route") or [] if isinstance(p, dict)]
    points = [p for p in (route_point(p) for p in route) if p]
    heart = workout.get("heartRate") if isinstance(workout.get("heartRate"), dict) else {}
    seconds = number(workout.get("duration"))
    if seconds is None and end and end > start:
        seconds = (end - start).total_seconds()
    elevation, elevation_units = quantity(workout.get("elevationUp") or workout.get("elevation"))
    if elevation is not None and str(elevation_units or "m").lower() == "ft":
        elevation *= 0.3048
    environment = workout.get("location")
    if environment is None and isinstance(workout.get("isIndoor"), bool):
        environment = "Indoor" if workout["isIndoor"] else "Outdoor"
    fields = base(start, workout.get("source"))
    fields.update(
        activity=activity,
        workout_id=str(workout["id"]) if workout.get("id") else None,
        duration_s=int(round(seconds)) if seconds is not None else None,
        calories=energy_kcal(workout.get("activeEnergyBurned") or workout.get("activeEnergy")),
        distance_m=distance_m(workout.get("distance")),
        heart_rate_mean=number(heart.get("avg")) or number(workout.get("avgHeartRate")),
        heart_rate_max=number(heart.get("max")) or number(workout.get("maxHeartRate")),
        heart_rate_min=number(heart.get("min")),
        elevation_gain_m=round(elevation, 1) if elevation is not None else None,
        steps=integer(quantity(workout.get("stepCount"))[0]),
        environment=str(environment) if environment else None,
        temperature=number(workout.get("temperature")),
        humidity=number(workout.get("humidity")),
        route_points=len(route) or None,
    )
    parts = [activity]
    if fields.get("duration_s") is not None:
        parts.append(duration(fields["duration_s"]))
    if fields.get("calories") is not None:
        parts.append(f"{fmt(fields['calories'], 0)} kcal")
    if fields.get("distance_m"):
        parts.append(f"{fmt(fields['distance_m'] / 1000, 2)} km")
    if fields.get("heart_rate_mean"):
        parts.append(f"avg HR {fmt(fields['heart_rate_mean'], 0)}")
    payload = {k: v for k, v in workout.items() if k != "route"}
    identity = workout.get("id") or f"{activity}:{utc_key(start)}"
    out = message(
        WORKOUTS, f"workout:{identity}", start, "Workout: " + ", ".join(parts), fields, payload,
        thread_key="workouts", title="Workouts", ended=end,
    )
    if points:
        first = points[0]
        out["location"] = clean({"lat": first["lat"], "lon": first["lon"], "accuracy_m": first["accuracy_m"]})
    return out, points


def map_document(record, *, metrics_filter=None):
    """(messages, route points) of one spooled Health Auto Export request."""
    body = record.get("body") if isinstance(record, dict) else None
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return [], []
    messages, points = [], []
    for metric in data.get("metrics") or []:
        if not isinstance(metric, dict):
            continue
        if metrics_filter and not metrics_filter(str(metric.get("name") or "")):
            continue
        messages.extend(metric_messages(metric))
    for workout in data.get("workouts") or []:
        if isinstance(workout, dict):
            out, route = workout_message(workout)
            if out:
                messages.append(out)
                points.extend(route)
    return messages, points
