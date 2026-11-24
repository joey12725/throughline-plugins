"""Deterministic mapping of raw fetch.py records to Throughline messages.

The same function maps a live window and an export file, so both modes give
identical messages for identical source documents.
"""

import hashlib
import json
import math
from datetime import date, datetime, timezone

# Raise when the shape of a message changes: every message carries it in the
# mapping_revision field, and export cursors re-send all buckets after a change.
MAPPING_REVISION = 1

DAILY = "health.oura.daily"
SLEEP = "health.oura.sleep"
SESSION = "health.oura.session"
WORKOUT = "health.oura.workout"
HEARTRATE = "health.oura.heartrate"
WEIGHT = "health.weight"
CHANNELS = (DAILY, SLEEP, SESSION, WORKOUT, HEARTRATE, WEIGHT)

KG_PER_LB = 0.45359237

COMMON = [
    ("provider", "text", "Source service: oura or weightgurus"),
    (
        "mapping_revision",
        "integer",
        "Revision of the plugin mapping that produced this record",
    ),
]
DAY = [
    (
        "day",
        "text",
        "Calendar day (YYYY-MM-DD) the record describes, as the source reports it",
    )
]


def _contributors(prefix, kind, names, what):
    return [
        (
            f"{prefix}_{n}",
            "integer",
            kind,
            ("contributors", n),
            f"{what} contributor: {n.replace('_', ' ')} (0-100)",
        )
        for n in names
    ]


# (field, type, daily collection, path into its document, description)
DAILY_METRICS = [
    ("sleep_score", "integer", "daily_sleep", ("score",), "Oura sleep score (0-100)"),
    *_contributors(
        "sleep_contrib",
        "daily_sleep",
        (
            "deep_sleep",
            "efficiency",
            "latency",
            "rem_sleep",
            "restfulness",
            "timing",
            "total_sleep",
        ),
        "Sleep score",
    ),
    (
        "readiness_score",
        "integer",
        "daily_readiness",
        ("score",),
        "Oura readiness score (0-100)",
    ),
    (
        "temperature_deviation",
        "number",
        "daily_readiness",
        ("temperature_deviation",),
        "Body temperature deviation from baseline, degrees C",
    ),
    (
        "temperature_trend_deviation",
        "number",
        "daily_readiness",
        ("temperature_trend_deviation",),
        "Temperature trend deviation, degrees C",
    ),
    *_contributors(
        "readiness_contrib",
        "daily_readiness",
        (
            "activity_balance",
            "body_temperature",
            "hrv_balance",
            "previous_day_activity",
            "previous_night",
            "recovery_index",
            "resting_heart_rate",
            "sleep_balance",
            "sleep_regularity",
        ),
        "Readiness score",
    ),
    (
        "activity_score",
        "integer",
        "daily_activity",
        ("score",),
        "Oura activity score (0-100)",
    ),
    ("steps", "integer", "daily_activity", ("steps",), "Steps"),
    (
        "active_calories",
        "integer",
        "daily_activity",
        ("active_calories",),
        "Active calories (kcal)",
    ),
    (
        "total_calories",
        "integer",
        "daily_activity",
        ("total_calories",),
        "Total calories (kcal)",
    ),
    (
        "target_calories",
        "integer",
        "daily_activity",
        ("target_calories",),
        "Activity target calories (kcal)",
    ),
    (
        "equivalent_walking_distance_m",
        "integer",
        "daily_activity",
        ("equivalent_walking_distance",),
        "Equivalent walking distance, metres",
    ),
    (
        "high_activity_s",
        "integer",
        "daily_activity",
        ("high_activity_time",),
        "High activity time, seconds",
    ),
    (
        "medium_activity_s",
        "integer",
        "daily_activity",
        ("medium_activity_time",),
        "Medium activity time, seconds",
    ),
    (
        "low_activity_s",
        "integer",
        "daily_activity",
        ("low_activity_time",),
        "Low activity time, seconds",
    ),
    (
        "sedentary_s",
        "integer",
        "daily_activity",
        ("sedentary_time",),
        "Sedentary time, seconds",
    ),
    (
        "resting_s",
        "integer",
        "daily_activity",
        ("resting_time",),
        "Resting time, seconds",
    ),
    (
        "non_wear_s",
        "integer",
        "daily_activity",
        ("non_wear_time",),
        "Ring not worn, seconds",
    ),
    (
        "high_activity_met_minutes",
        "integer",
        "daily_activity",
        ("high_activity_met_minutes",),
        "High activity MET minutes",
    ),
    (
        "medium_activity_met_minutes",
        "integer",
        "daily_activity",
        ("medium_activity_met_minutes",),
        "Medium activity MET minutes",
    ),
    (
        "low_activity_met_minutes",
        "integer",
        "daily_activity",
        ("low_activity_met_minutes",),
        "Low activity MET minutes",
    ),
    (
        "sedentary_met_minutes",
        "integer",
        "daily_activity",
        ("sedentary_met_minutes",),
        "Sedentary MET minutes",
    ),
    (
        "average_met_minutes",
        "number",
        "daily_activity",
        ("average_met_minutes",),
        "Average MET",
    ),
    (
        "inactivity_alerts",
        "integer",
        "daily_activity",
        ("inactivity_alerts",),
        "Inactivity alerts",
    ),
    (
        "meters_to_target",
        "integer",
        "daily_activity",
        ("meters_to_target",),
        "Metres left to the activity target",
    ),
    (
        "target_meters",
        "integer",
        "daily_activity",
        ("target_meters",),
        "Activity target, metres",
    ),
    *_contributors(
        "activity_contrib",
        "daily_activity",
        (
            "meet_daily_targets",
            "move_every_hour",
            "recovery_time",
            "stay_active",
            "training_frequency",
            "training_volume",
        ),
        "Activity score",
    ),
    (
        "stress_high_s",
        "integer",
        "daily_stress",
        ("stress_high",),
        "Time in high stress, seconds",
    ),
    (
        "recovery_high_s",
        "integer",
        "daily_stress",
        ("recovery_high",),
        "Time in high recovery, seconds",
    ),
    (
        "stress_day_summary",
        "text",
        "daily_stress",
        ("day_summary",),
        "Oura stress day summary (restored, normal, stressful)",
    ),
    (
        "spo2_average",
        "number",
        "daily_spo2",
        ("spo2_percentage", "average"),
        "Average blood oxygen during sleep, percent",
    ),
    (
        "breathing_disturbance_index",
        "integer",
        "daily_spo2",
        ("breathing_disturbance_index",),
        "Breathing disturbance index",
    ),
    (
        "resilience_level",
        "text",
        "daily_resilience",
        ("level",),
        "Oura resilience level",
    ),
    (
        "resilience_sleep_recovery",
        "number",
        "daily_resilience",
        ("contributors", "sleep_recovery"),
        "Resilience contributor: sleep recovery",
    ),
    (
        "resilience_daytime_recovery",
        "number",
        "daily_resilience",
        ("contributors", "daytime_recovery"),
        "Resilience contributor: daytime recovery",
    ),
    (
        "resilience_stress",
        "number",
        "daily_resilience",
        ("contributors", "stress"),
        "Resilience contributor: stress",
    ),
    (
        "vascular_age",
        "integer",
        "daily_cardiovascular_age",
        ("vascular_age",),
        "Oura cardiovascular age, years",
    ),
    ("vo2_max", "number", "vO2_max", ("vo2_max",), "Estimated VO2 max, ml/kg/min"),
]
DAILY_KINDS = tuple(dict.fromkeys(m[2] for m in DAILY_METRICS))
MAIN_SLEEP = [
    (
        "main_sleep_total_s",
        "integer",
        "Total sleep of the day's main sleep period, seconds",
    ),
    (
        "main_sleep_time_in_bed_s",
        "integer",
        "Time in bed of the main sleep period, seconds",
    ),
    (
        "main_sleep_efficiency",
        "integer",
        "Sleep efficiency of the main sleep period, percent",
    ),
    (
        "main_sleep_lowest_heart_rate",
        "integer",
        "Lowest heart rate in the main sleep period, bpm",
    ),
    ("main_sleep_average_hrv", "number", "Average HRV in the main sleep period, ms"),
    ("main_sleep_start", "timestamp", "Bedtime start of the main sleep period"),
    ("main_sleep_end", "timestamp", "Bedtime end of the main sleep period"),
    ("sleep_periods", "integer", "Sleep periods recorded for the day (naps included)"),
    ("daily_sources", "json", "Oura daily collections present for this day"),
]
SLEEP_FIELDS = [
    (
        "sleep_type",
        "text",
        "Oura sleep type: long_sleep, sleep, late_nap, rest or deleted",
    ),
    ("main_sleep", "boolean", "This is the day's main sleep period"),
    ("total_sleep_s", "integer", "Total sleep, seconds"),
    ("time_in_bed_s", "integer", "Time in bed, seconds"),
    ("deep_sleep_s", "integer", "Deep sleep, seconds"),
    ("light_sleep_s", "integer", "Light sleep, seconds"),
    ("rem_sleep_s", "integer", "REM sleep, seconds"),
    ("awake_s", "integer", "Awake time in bed, seconds"),
    ("latency_s", "integer", "Sleep latency, seconds"),
    ("efficiency", "integer", "Sleep efficiency, percent"),
    ("average_heart_rate", "number", "Average heart rate, bpm"),
    ("lowest_heart_rate", "integer", "Lowest heart rate, bpm"),
    ("average_hrv", "number", "Average heart rate variability, ms"),
    ("average_breath", "number", "Average breaths per minute"),
    ("restless_periods", "integer", "Restless periods"),
    ("readiness_score", "integer", "Readiness score computed for this period"),
    (
        "temperature_deviation",
        "number",
        "Temperature deviation for this period, degrees C",
    ),
    ("sleep_period", "integer", "Oura period index within the day"),
]
SESSION_FIELDS = [
    (
        "session_type",
        "text",
        "Oura session type (breathing, meditation, nap, relaxation, rest, body_status)",
    ),
    ("mood", "text", "Mood reported for the session"),
    ("duration_s", "integer", "Session length, seconds"),
    ("heart_rate_mean", "number", "Mean of the session heart rate samples, bpm"),
    ("hrv_mean", "number", "Mean of the session HRV samples, ms"),
]
WORKOUT_FIELDS = [
    ("activity", "text", "Workout activity"),
    ("intensity", "text", "Workout intensity (easy, moderate, hard)"),
    ("calories", "number", "Energy burned, kcal"),
    ("distance_m", "number", "Distance, metres"),
    ("duration_s", "integer", "Workout length, seconds"),
    ("label", "text", "User label"),
    (
        "workout_source",
        "text",
        "How the workout was recorded (manual, autodetected, confirmed, workout_heart_rate)",
    ),
]
HEARTRATE_FIELDS = [
    ("sample_count", "integer", "Heart rate samples this day"),
    ("bpm_min", "integer", "Lowest sample, bpm"),
    ("bpm_max", "integer", "Highest sample, bpm"),
    ("bpm_mean", "number", "Mean of all samples, bpm"),
    ("bpm_mean_awake", "number", "Mean of awake samples, bpm"),
    ("bpm_mean_rest", "number", "Mean of rest samples, bpm"),
    ("bpm_mean_sleep", "number", "Mean of sleep samples, bpm"),
    ("bpm_mean_workout", "number", "Mean of workout samples, bpm"),
    ("first_sample_at", "timestamp", "First sample time"),
    ("last_sample_at", "timestamp", "Last sample time"),
]
WEIGHT_FIELDS = [
    ("weight", "number", "Body weight in weight_unit"),
    ("weight_unit", "text", "Unit of weight (lb or kg)"),
    ("weight_kg", "number", "Body weight in kilograms"),
    ("bmi", "number", "Body mass index"),
    ("body_fat_pct", "number", "Body fat, percent"),
    ("muscle_mass_pct", "number", "Muscle mass, percent"),
    ("water_pct", "number", "Body water, percent"),
    (
        "deleted",
        "boolean",
        "The weigh-in was deleted in Weight Gurus (kept as a new version)",
    ),
    (
        "scale_source",
        "text",
        "How the reading arrived (for example a Wi-Fi or Bluetooth scale, or manual)",
    ),
    (
        "server_timestamp",
        "timestamp",
        "When Weight Gurus stored the latest operation for this weigh-in",
    ),
    ("operation_count", "integer", "Weight Gurus operations seen for this weigh-in"),
]


def declared(*groups):
    out = []
    for group in groups:
        for item in group:
            out.append({"name": item[0], "type": item[1], "description": item[-1]})
    return out


FIELDS = {
    DAILY: declared(COMMON, DAY, DAILY_METRICS, MAIN_SLEEP),
    SLEEP: declared(COMMON, DAY, SLEEP_FIELDS),
    SESSION: declared(COMMON, DAY, SESSION_FIELDS),
    WORKOUT: declared(COMMON, DAY, WORKOUT_FIELDS),
    HEARTRATE: declared(COMMON, DAY, HEARTRATE_FIELDS),
    WEIGHT: declared(COMMON, WEIGHT_FIELDS),
}


# ----------------------------------------------------------------- helpers


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:24]


def number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def integer(value):
    value = number(value)
    return int(round(value)) if value is not None else None


def coerce(kind, value):
    if kind == "integer":
        return integer(value)
    if kind == "number":
        return number(value)
    if kind == "text":
        return (
            str(value)
            if value is not None and not isinstance(value, (dict, list))
            else None
        )
    if kind == "boolean":
        return value if isinstance(value, bool) else None
    return value


def path(doc, keys):
    for key in keys:
        if not isinstance(doc, dict):
            return None
        doc = doc.get(key)
    return doc


def instant(value):
    """An offset-aware ISO timestamp string, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at


def iso(at):
    return at.isoformat() if at else None


def day_of(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def duration(seconds):
    seconds = integer(seconds)
    if seconds is None:
        return None
    hours, minutes = divmod(round(seconds / 60), 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def fmt(value, digits=1):
    value = number(value)
    if value is None:
        return None
    text = f"{value:,.{digits}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def thread(channel, at, title):
    month = at.strftime("%Y-%m")
    return {"external_id": f"{channel}:{month}", "title": f"{title} {month}"}


def subject_day(day):
    """The calendar day a record is about, as the core expects a day bound:
    00:00 UTC of that day with day precision and no end. (An end at the next
    midnight, or a local-offset midnight, would place the record on two days
    or on the wrong UTC day when the core buckets subjects by day.)"""
    return {
        "start": datetime.combine(day, datetime.min.time(), timezone.utc).isoformat(),
        "start_precision": "day",
    }


def latest(docs):
    """Deterministic pick among documents of one kind for one day."""
    return max(
        docs, key=lambda d: (str(d.get("timestamp") or ""), str(d.get("id") or ""))
    )


def message(
    channel,
    external_id,
    at,
    content,
    fields,
    payload,
    *,
    title,
    ended=None,
    subject=None,
):
    out = {
        "channel": channel,
        "external_id": external_id,
        "thread": thread(channel, at, title),
        "happened_at": at.isoformat(),
        "ended_at": iso(ended) if ended and ended >= at else None,
        "content": content,
        "fields": {k: v for k, v in fields.items() if v is not None},
        "payload": payload,
    }
    if subject:
        out["subject"] = subject
    return out


def base(provider, day=None):
    out = {"provider": provider, "mapping_revision": MAPPING_REVISION}
    if day is not None:
        out["day"] = day.isoformat()
    return out


# ------------------------------------------------------------------ merging


def merge(raws):
    """Last copy of every (kind, id): latest fetched_at, then latest position."""
    best = {}
    for position, raw in enumerate(raws):
        kind = raw.get("kind")
        if (
            not isinstance(kind, str)
            or kind.startswith("export.")
            or raw.get("id") is None
        ):
            continue
        key = (kind, str(raw["id"]))
        rank = (str(raw.get("fetched_at") or ""), position)
        if key not in best or rank >= best[key][0]:
            best[key] = (rank, raw)
    return [
        raw
        for _, raw in sorted(
            best.values(), key=lambda item: (item[1]["kind"], str(item[1]["id"]))
        )
    ]


DAY_KINDS = tuple("oura." + k for k in DAILY_KINDS + ("sleep", "session", "workout"))


def newest_fetch(raws):
    """Per Oura (kind, day), keep only documents of the newest fetch of that day.

    A fetch window reads every document of a collection for its days, so the
    newest fetch is authoritative: a document Oura regenerated under a new id
    replaces the old one instead of standing beside it. Live windows have one
    fetch time, so this only matters when several export files overlap.
    """
    newest = {}
    for raw in raws:
        if raw["kind"] in DAY_KINDS and isinstance(raw.get("payload"), dict):
            key = (raw["kind"], str(raw["payload"].get("day")))
            newest[key] = max(newest.get(key, ""), str(raw.get("fetched_at") or ""))
    return [
        raw
        for raw in raws
        if raw["kind"] not in DAY_KINDS
        or not isinstance(raw.get("payload"), dict)
        or str(raw.get("fetched_at") or "")
        == newest[(raw["kind"], str(raw["payload"].get("day")))]
    ]


# ------------------------------------------------------------------- Oura


def main_sleep(sessions):
    usable = [s for s in sessions if s.get("type") != "deleted"]
    if not usable:
        return None
    return max(
        usable,
        key=lambda s: (
            s.get("type") == "long_sleep",
            integer(s.get("total_sleep_duration")) or 0,
            str(s.get("id")),
        ),
    )


def daily_message(day, docs, sessions):
    offset = None
    for kind in ("daily_readiness", "daily_sleep", "daily_activity") + DAILY_KINDS:
        at = instant(docs[kind].get("timestamp")) if kind in docs else None
        if at is not None:
            offset = at.tzinfo
            break
    main = main_sleep(sessions)
    if offset is None and main and instant(main.get("bedtime_end")):
        offset = instant(main["bedtime_end"]).tzinfo
    offset = offset or timezone.utc
    at = datetime.combine(day, datetime.min.time(), offset)
    fields = base("oura", day)
    for name, kind, collection, keys, _ in DAILY_METRICS:
        if collection in docs:
            fields[name] = coerce(kind, path(docs[collection], keys))
    if main:
        fields.update(
            main_sleep_total_s=integer(main.get("total_sleep_duration")),
            main_sleep_time_in_bed_s=integer(main.get("time_in_bed")),
            main_sleep_efficiency=integer(main.get("efficiency")),
            main_sleep_lowest_heart_rate=integer(main.get("lowest_heart_rate")),
            main_sleep_average_hrv=number(main.get("average_hrv")),
            main_sleep_start=iso(instant(main.get("bedtime_start"))),
            main_sleep_end=iso(instant(main.get("bedtime_end"))),
        )
    fields["sleep_periods"] = (
        sum(1 for s in sessions if s.get("type") != "deleted") or None
    )
    fields["daily_sources"] = sorted(docs)
    parts = []
    for label, key in (
        ("sleep", "sleep_score"),
        ("readiness", "readiness_score"),
        ("activity", "activity_score"),
    ):
        if fields.get(key) is not None:
            parts.append(f"{label} {fields[key]}")
    if fields.get("main_sleep_total_s") is not None:
        parts.append("slept " + duration(fields["main_sleep_total_s"]))
    if fields.get("steps") is not None:
        parts.append(f"{fields['steps']:,} steps")
    if fields.get("stress_high_s") is not None:
        parts.append("high stress " + duration(fields["stress_high_s"]))
    if fields.get("stress_day_summary"):
        parts.append("stress " + fields["stress_day_summary"])
    if fields.get("spo2_average") is not None:
        parts.append(f"SpO2 {fmt(fields['spo2_average'])}%")
    if fields.get("temperature_deviation") is not None:
        parts.append(f"temperature {fields['temperature_deviation']:+.2f} C")
    if fields.get("resilience_level"):
        parts.append("resilience " + fields["resilience_level"])
    content = f"Oura {day.isoformat()}: " + (
        ", ".join(parts) if parts else "daily summary"
    )
    payload = {kind: docs[kind] for kind in sorted(docs)}
    return message(
        DAILY,
        f"oura:daily:{day.isoformat()}",
        at,
        content,
        fields,
        payload,
        title="Oura daily",
        subject=subject_day(day),
    )


def clock(at):
    return at.strftime("%H:%M") if at else None


def sleep_message(doc, main_id, native=None):
    day = day_of(doc.get("day"))
    start, end = instant(doc.get("bedtime_start")), instant(doc.get("bedtime_end"))
    if start is None:
        if day is None:
            return None
        start = datetime.combine(day, datetime.min.time(), timezone.utc)
    readiness = doc.get("readiness") if isinstance(doc.get("readiness"), dict) else {}
    fields = base("oura", day)
    fields.update(
        sleep_type=coerce("text", doc.get("type")),
        main_sleep=doc.get("id") == main_id,
        total_sleep_s=integer(doc.get("total_sleep_duration")),
        time_in_bed_s=integer(doc.get("time_in_bed")),
        deep_sleep_s=integer(doc.get("deep_sleep_duration")),
        light_sleep_s=integer(doc.get("light_sleep_duration")),
        rem_sleep_s=integer(doc.get("rem_sleep_duration")),
        awake_s=integer(doc.get("awake_time")),
        latency_s=integer(doc.get("latency")),
        efficiency=integer(doc.get("efficiency")),
        average_heart_rate=number(doc.get("average_heart_rate")),
        lowest_heart_rate=integer(doc.get("lowest_heart_rate")),
        average_hrv=number(doc.get("average_hrv")),
        average_breath=number(doc.get("average_breath")),
        restless_periods=integer(doc.get("restless_periods")),
        readiness_score=integer(readiness.get("score")),
        temperature_deviation=number(readiness.get("temperature_deviation")),
        sleep_period=integer(doc.get("period")),
    )
    label = (fields.get("sleep_type") or "sleep").replace("_", " ")
    head = (
        f"Slept {duration(fields['total_sleep_s'])}"
        if fields.get("total_sleep_s") is not None
        else "Sleep period"
    )
    span = f"{clock(start)}-{clock(end)}" if end else clock(start)
    content = f"{head} ({label}, {span})"
    stages = [
        f"{name} {duration(fields[key])}"
        for name, key in (
            ("deep", "deep_sleep_s"),
            ("REM", "rem_sleep_s"),
            ("light", "light_sleep_s"),
        )
        if fields.get(key) is not None
    ]
    extra = []
    if fields.get("efficiency") is not None:
        extra.append(f"efficiency {fields['efficiency']}%")
    if fields.get("lowest_heart_rate") is not None:
        extra.append(f"lowest HR {fields['lowest_heart_rate']}")
    if fields.get("average_hrv") is not None:
        extra.append(f"HRV {fmt(fields['average_hrv'], 0)} ms")
    if stages or extra:
        content += ": " + ", ".join(stages + extra)
    return message(
        SLEEP,
        f"oura:sleep:{doc.get('id') or native}",
        start,
        content,
        fields,
        doc,
        title="Oura sleep",
        ended=end,
    )


def mean(values):
    values = [v for v in (number(x) for x in values or []) if v is not None]
    return round(sum(values) / len(values), 2) if values else None


def samples(series):
    return series.get("items") if isinstance(series, dict) else None


def session_message(doc, native=None):
    start, end = instant(doc.get("start_datetime")), instant(doc.get("end_datetime"))
    day = day_of(doc.get("day"))
    if start is None:
        return None
    fields = base("oura", day)
    fields.update(
        session_type=coerce("text", doc.get("type")),
        mood=coerce("text", doc.get("mood")),
        duration_s=int((end - start).total_seconds()) if end and end >= start else None,
        heart_rate_mean=mean(samples(doc.get("heart_rate"))),
        hrv_mean=mean(samples(doc.get("heart_rate_variability"))),
    )
    parts = [(fields.get("session_type") or "session").replace("_", " ")]
    if fields.get("duration_s") is not None:
        parts.append(duration(fields["duration_s"]))
    if fields.get("heart_rate_mean") is not None:
        parts.append(f"mean HR {fmt(fields['heart_rate_mean'], 0)}")
    if fields.get("mood"):
        parts.append("mood " + fields["mood"])
    return message(
        SESSION,
        f"oura:session:{doc.get('id') or native}",
        start,
        "Oura session: " + ", ".join(parts),
        fields,
        doc,
        title="Oura sessions",
        ended=end,
    )


def workout_message(doc, native=None):
    start, end = instant(doc.get("start_datetime")), instant(doc.get("end_datetime"))
    day = day_of(doc.get("day"))
    if start is None:
        return None
    fields = base("oura", day)
    fields.update(
        activity=coerce("text", doc.get("activity")),
        intensity=coerce("text", doc.get("intensity")),
        calories=number(doc.get("calories")),
        distance_m=number(doc.get("distance")),
        duration_s=int((end - start).total_seconds()) if end and end >= start else None,
        label=coerce("text", doc.get("label")),
        workout_source=coerce("text", doc.get("source")),
    )
    parts = [
        (fields.get("label") or fields.get("activity") or "workout").replace("_", " ")
    ]
    if fields.get("duration_s") is not None:
        parts.append(duration(fields["duration_s"]))
    if fields.get("calories") is not None:
        parts.append(f"{fmt(fields['calories'], 0)} kcal")
    if fields.get("distance_m"):
        parts.append(f"{fmt(fields['distance_m'] / 1000, 2)} km")
    if fields.get("intensity"):
        parts.append(fields["intensity"] + " intensity")
    return message(
        WORKOUT,
        f"oura:workout:{doc.get('id') or native}",
        start,
        "Workout: " + ", ".join(parts),
        fields,
        doc,
        title="Oura workouts",
        ended=end,
    )


def heartrate_message(payload):
    day = day_of(payload.get("day"))
    items = [
        i
        for i in payload.get("items") or []
        if isinstance(i, dict)
        and number(i.get("bpm")) is not None
        and instant(i.get("timestamp"))
    ]
    if day is None or not items:
        return None
    times = sorted(instant(i["timestamp"]) for i in items)
    bpm = [number(i["bpm"]) for i in items]
    fields = base("oura", day)
    fields.update(
        sample_count=len(items),
        bpm_min=integer(min(bpm)),
        bpm_max=integer(max(bpm)),
        bpm_mean=mean(bpm),
        first_sample_at=times[0].isoformat(),
        last_sample_at=times[-1].isoformat(),
    )
    for source in ("awake", "rest", "sleep", "workout"):
        fields[f"bpm_mean_{source}"] = mean(
            [i["bpm"] for i in items if i.get("source") == source]
        )
    by_source = [
        f"{source} {fmt(fields[f'bpm_mean_{source}'], 0)}"
        for source in ("sleep", "rest", "awake", "workout")
        if fields.get(f"bpm_mean_{source}") is not None
    ]
    content = (
        f"Heart rate {day.isoformat()}: {len(items)} samples, {fields['bpm_min']}-{fields['bpm_max']} bpm, "
        f"mean {fmt(fields['bpm_mean'], 0)}"
        + (f" ({', '.join(by_source)})" if by_source else "")
    )
    return message(
        HEARTRATE,
        f"oura:heartrate:{day.isoformat()}",
        times[0],
        content,
        fields,
        payload,
        title="Oura heart rate",
        ended=times[-1],
        subject=subject_day(day),
    )


# ------------------------------------------------------------ Weight Gurus

SCALED = (
    ("bmi", "bmi"),
    ("bodyFat", "body_fat_pct"),
    ("muscleMass", "muscle_mass_pct"),
    ("water", "water_pct"),
)


def weight_message(entry, ops, unit):
    """One weigh-in from all operations that name its entryTimestamp.

    Weight Gurus stores weight, BMI and percentages as integers times ten. The
    newest operation decides deletion; values come from the newest non-delete
    operation (or the delete itself when nothing else was seen).
    """
    at = instant(entry)
    if at is None:
        return None
    ordered = sorted(
        ops,
        key=lambda o: (
            str(o.get("serverTimestamp") or ""),
            json.dumps(o, sort_keys=True),
        ),
    )
    newest = ordered[-1]
    deleted = str(newest.get("operationType") or "").lower() == "delete"
    values = next(
        (
            o
            for o in reversed(ordered)
            if str(o.get("operationType") or "").lower() != "delete"
        ),
        newest,
    )
    unit = str(values.get("unit") or unit or "lb").lower()
    unit = "kg" if unit.startswith("kg") else "lb"
    raw_weight = number(values.get("weight"))
    weight = round(raw_weight / 10, 2) if raw_weight is not None else None
    fields = base("weightgurus")
    fields.update(
        weight=weight,
        weight_unit=unit if weight is not None else None,
        weight_kg=(round(weight * KG_PER_LB, 2) if unit == "lb" else weight)
        if weight is not None
        else None,
        deleted=deleted,
        scale_source=coerce("text", values.get("source")),
        server_timestamp=iso(instant(newest.get("serverTimestamp"))),
        operation_count=len(ordered),
    )
    for key, name in SCALED:
        value = number(values.get(key))
        fields[name] = round(value / 10, 2) if value is not None and value > 0 else None
    parts = []
    if weight is not None:
        parts.append(f"{fmt(weight)} {unit}")
    for name, label, suffix in (
        ("body_fat_pct", "body fat", "%"),
        ("muscle_mass_pct", "muscle", "%"),
        ("water_pct", "water", "%"),
        ("bmi", "BMI", ""),
    ):
        if fields.get(name) is not None:
            parts.append(f"{label} {fmt(fields[name])}{suffix}")
    content = "Weigh-in: " + (", ".join(parts) if parts else "no readings")
    if deleted:
        content += " (deleted in Weight Gurus)"
    return message(
        WEIGHT,
        f"weightgurus:{entry}",
        at,
        content,
        fields,
        {"operations": ordered},
        title="Weigh-ins",
    )


# --------------------------------------------------------------- top level


def map_records(raws, *, unit="lb"):
    """Messages for a set of raw records, sorted by channel, time and id."""
    raws = newest_fetch(merge(raws))
    daily, sleeps, out = {}, {}, []
    weighins = {}
    for raw in raws:
        kind, payload = raw["kind"], raw.get("payload")
        if not isinstance(payload, dict):
            continue
        if kind.startswith("oura.") and kind[5:] in DAILY_KINDS:
            day = day_of(payload.get("day"))
            if day:
                daily.setdefault(day, {}).setdefault(kind[5:], []).append(payload)
        elif kind == "oura.sleep":
            day = day_of(payload.get("day"))
            sleeps.setdefault(day, []).append((payload, raw["id"]))
        elif kind == "oura.session":
            out.append(session_message(payload, raw["id"]))
        elif kind == "oura.workout":
            out.append(workout_message(payload, raw["id"]))
        elif kind == "oura.heartrate":
            out.append(heartrate_message(payload))
        elif kind == "weightgurus.operation":
            entry = payload.get("entryTimestamp")
            if isinstance(entry, str) and entry:
                weighins.setdefault(entry, []).append(payload)
    for day, pairs in sleeps.items():
        main = main_sleep([doc for doc, _ in pairs])
        main_id = main.get("id") if main else None
        out.extend(sleep_message(doc, main_id, native) for doc, native in pairs)
    for day, kinds in daily.items():
        docs = {kind: latest(items) for kind, items in kinds.items()}
        out.append(daily_message(day, docs, [doc for doc, _ in sleeps.get(day, [])]))
    for entry, ops in weighins.items():
        out.append(weight_message(entry, ops, unit))
    out = [m for m in out if m is not None]
    out.sort(key=lambda m: (m["channel"], instant(m["happened_at"]), m["external_id"]))
    return out
