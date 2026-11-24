"""Text-only Wave archive and official API connector."""

import json
import hashlib
import logging
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

import httpx
import psycopg
from psycopg.rows import dict_row
from throughline_sdk import (
    Channel,
    Checkpoint,
    Declaration,
    Manifest,
    Plugin,
    Record,
    coverage,
    run,
)


TURNS = "transcripts.wave"
RECORDINGS = "recordings.wave"
# Requests per UTC day the API path spends at most (Wave's daily budget).
DAILY_REQUESTS = 8900


# Everything one malformed source row can raise while being mapped. The archive
# and API paths must agree, or a bad row pins the cursor in one of them.
BAD_ROW = (ValueError, TypeError, KeyError, AttributeError, OverflowError)


def json_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    return value


def instant(value):
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise ValueError("Wave timestamp must include a UTC offset")
    return result


def location(row, fallback=None):
    for candidate in (row, fallback or {}):
        if candidate.get("lat") is not None and candidate.get("lon") is not None:
            return {"lat": float(candidate["lat"]), "lon": float(candidate["lon"])}
    return None


def validate_message(message):
    encoded = json.dumps(message, allow_nan=False)

    # JSON escapes an actual NUL once; literal backslash-u strings twice.
    def nul(value):
        if isinstance(value, str):
            return "\x00" in value
        if isinstance(value, dict):
            return any(nul(k) or nul(v) for k, v in value.items())
        if isinstance(value, list):
            return any(nul(v) for v in value)
        return False

    if nul(message) or len(message["content"].encode()) > 100_000:
        raise ValueError("unsafe source row")
    if message.get("location"):
        loc = message["location"]
        if not (-90 <= loc["lat"] <= 90 and -180 <= loc["lon"] <= 180):
            raise ValueError("invalid source location")
    return encoded


def recording_message(recording, turn_count, payload=None):
    at = instant(recording["started_at"])
    duration = recording.get("duration_s")
    title = recording.get("title") or "Untitled Wave recording"
    summary = (recording.get("metadata") or {}).get("summary")
    result = dict(
        channel="recordings.wave",
        external_id=recording["id"],
        thread={"external_id": recording["id"], "title": title},
        happened_at=at.isoformat(),
        ended_at=(at + timedelta(seconds=float(duration))).isoformat()
        if duration is not None and float(duration) >= 0
        else None,
        content=title + ("\n\n" + str(summary) if summary else ""),
        fields={
            "time_precision": recording.get("time_precision", "exact"),
            "duration_s": json_value(duration),
            "device": recording.get("device"),
            "source_platform": recording.get("source_platform"),
            "recording_type": (recording.get("metadata") or {}).get("type"),
            "turn_count": turn_count,
            "ignore_reason": recording.get("ignore_reason"),
        },
        location=location(recording),
        payload=json_value(payload if payload is not None else recording),
    )

    validate_message(result)
    return result


def turn_message(recording, turn, speaker=None, payload=None):
    at = instant(turn.get("said_at_manual") or turn["said_at"])
    start, end = turn.get("start_s"), turn.get("end_s")
    label = turn.get("speaker") or "Unknown speaker"
    speaker = speaker or {}
    author = {
        "system": "wave",
        "kind": "speaker_label",
        "value": f"{recording['id']}:{label}",
        "display_name": label,
    }
    if (speaker.get("system"), speaker.get("kind")) == (
        "wave",
        "voice_profile",
    ) and speaker.get("value"):
        author = {"system": "wave", "kind": "voice_profile", "value": speaker["value"]}
    result = dict(
        channel="transcripts.wave",
        external_id=f"{recording['id']}:{turn['turn_no']}",
        thread={
            "external_id": recording["id"],
            "title": recording.get("title") or "Untitled Wave recording",
        },
        happened_at=at.isoformat(),
        ended_at=(at + timedelta(seconds=float(end) - float(start))).isoformat()
        if start is not None and end is not None and float(end) >= float(start)
        else None,
        author=author,
        content=turn["text"] if turn["text"].strip() else "[Empty transcript turn]",
        fields={
            "speaker_label": turn.get("speaker"),
            "start_s": json_value(start),
            "end_s": json_value(end),
            "speaker_provenance": speaker.get("provenance"),
            "ignore_reason": turn.get("ignore_reason"),
            "place": turn.get("place")
            if turn.get("place") is not None
            else recording.get("place"),
        },
        location=location(turn, recording),
        payload=json_value(
            payload
            if payload is not None
            else {"turn": turn, "speaker": speaker, "recording": recording}
        ),
    )

    validate_message(result)
    return result


def part_of(message, recording_id):
    return Record(
        kind="link",
        data={
            "type": "part_of",
            "method": "source",
            "from": {
                "channel": "transcripts.wave",
                "external_id": message["external_id"],
            },
            "to": {"channel": "recordings.wave", "external_id": recording_id},
        },
    )


def fields(spec):
    return [
        {"name": name, "type": kind, "description": description}
        for name, kind, description in spec
    ]


class Wave(Plugin):
    manifest = Manifest(
        name="wave",
        version="0.1.0",
        default_interval_s=3600,
        link_types=["part_of"],
        channels=[
            Channel(
                name="transcripts.wave",
                description="Wave speaker turns threaded by whole recording; text only",
                conversation_whole_thread=True,
                fields=fields(
                    [
                        (
                            "speaker_label",
                            "text",
                            "Speaker label within this recording",
                        ),
                        ("start_s", "number", "Start offset in recording seconds"),
                        ("end_s", "number", "End offset in recording seconds"),
                        (
                            "speaker_provenance",
                            "text",
                            "Source evidence for the speaker mapping",
                        ),
                        (
                            "ignore_reason",
                            "text",
                            "Source ignore flag; retained so arrival rules can skip it",
                        ),
                        (
                            "place",
                            "text",
                            "Turn place, falling back to recording place",
                        ),
                    ]
                ),
            ),
            Channel(
                name="recordings.wave",
                description="Wave recording titles and summaries with full recording intervals",
                fields=fields(
                    [
                        (
                            "time_precision",
                            "text",
                            "exact or first_turn when start was absent",
                        ),
                        ("duration_s", "number", "Recording duration in seconds"),
                        ("device", "text", "Source recording device"),
                        ("source_platform", "text", "Source platform"),
                        ("recording_type", "text", "Wave recording type"),
                        ("turn_count", "integer", "Number of source transcript turns"),
                        (
                            "ignore_reason",
                            "text",
                            "Source ignore flag; recording is still retained",
                        ),
                    ]
                ),
            ),
        ],
        settings={
            "source": Declaration(
                env="WAVE_SOURCE", default="api", description="api or archive"
            ),
            "fixture": Declaration(
                env="WAVE_API_FIXTURE",
                required=False,
                description="Invented API response map for offline tests",
            ),
            "overlap_hours": Declaration(
                env="WAVE_API_OVERLAP_HOURS",
                default="168",
                description="How far behind the newest imported session each API sync lists again",
            ),
        },
        secrets={
            "dsn": Declaration(
                env="WAVE_ARCHIVE_DSN",
                required=False,
                description="Read-only archive Postgres DSN",
            ),
            "token": Declaration(
                env="WAVE_API_TOKEN",
                required=False,
                description="Wave scoped API bearer token",
            ),
        },
    )

    def execute(self, client, mode, dry_run=False):
        self.quarantined = 0
        result = super().execute(client, mode, dry_run)
        result["quarantined"] = self.quarantined
        logging.warning("Wave quarantined=%d", self.quarantined)
        return result

    def quarantine(self, key, channel=TURNS):
        self.quarantined = getattr(self, "quarantined", 0) + 1
        self.quarantined_by[channel] += 1
        logging.warning("Wave quarantined id=%s", key)

    def records(self, cursor, mode):
        source = self.settings["source"]
        if cursor and cursor.get("source") != source:
            raise ValueError(
                "Source changed; reset the wave cursor or finish the previous import first"
            )
        self.quarantined_by = Counter()
        self.delivered = Counter()
        self.budget_stop = None
        if source == "archive":
            stream = self.archive(cursor)
        elif source == "api":
            stream = self.api(cursor, mode)
        else:
            raise ValueError("WAVE_SOURCE must be archive or api")
        # The coverage reports go after every message and before the final
        # checkpoint, so a resumed run still ends on its cursor.
        held = None
        for item in stream:
            if held is not None:
                yield held
                held = None
            if isinstance(item, Checkpoint):
                held = item
                continue
            if isinstance(item, dict):
                self.delivered[item["channel"]] += 1
            yield item
        yield from self.coverage_records(source)
        if held is not None:
            yield held
        # Full reconciliation discovers edits and late turns on the next poll.
        if source == "archive":
            yield Checkpoint(cursor={})

    def coverage_records(self, source):
        """Coverage of both channels after this run (scope key archive or api).

        archive: a full reconciliation of the recording set, so complete
        (verified) unless rows were quarantined (partial, quarantined_rows).
        api: Wave lists a session only once it is processed, and each sync
        lists again over the overlap window, so a sync is best_effort with
        listing_lag; when the daily request budget stops it, partial with
        rate_limited (no window, so no gap: the next sync resumes the cursor).
        """
        for channel in (RECORDINGS, TURNS):
            quarantined = self.quarantined_by[channel]
            reasons, detail = [], {}
            if quarantined:
                reasons.append("quarantined_rows")
                detail["quarantined"] = quarantined
            if source == "archive":
                status = "partial" if quarantined else "complete"
                verified = not quarantined
            elif self.budget_stop is not None:
                status, verified = "partial", False
                reasons.insert(0, "rate_limited")
                detail.update(self.budget_stop)
            else:
                status, verified = "best_effort", False
                reasons.insert(0, "listing_lag")
                detail["overlap_hours"] = float(self.settings["overlap_hours"])
            yield coverage(
                channel,
                scope_key=source,
                status=status,
                reasons=reasons,
                rows_delivered=self.delivered[channel],
                verified=verified,
                detail=detail or None,
            )

    def budget_stopped(self, budget):
        self.budget_stop = {"requests": budget["requests"], "limit": DAILY_REQUESTS}

    def archive(self, cursor):
        if not self.secrets["dsn"]:
            raise ValueError("Set WAVE_ARCHIVE_DSN")
        with psycopg.connect(self.secrets["dsn"], row_factory=dict_row) as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            conn.execute("SET LOCAL TIME ZONE '+00:00'")
            after = cursor.get("after")
            active = cursor.get("recording")
            last_turn = cursor.get("turn_no")
            while True:
                if active:
                    recording = conn.execute(
                        "SELECT * FROM transcripts.recording WHERE source='wave' AND id=%s",
                        (active,),
                    ).fetchone()
                    if recording is None:
                        raise ValueError(
                            "Resumable archive recording disappeared; reset cursor to rescan"
                        )
                else:
                    recording = conn.execute(
                        'SELECT * FROM transcripts.recording WHERE source=\'wave\' AND (%s::text IS NULL OR id COLLATE "C" > %s COLLATE "C") ORDER BY id COLLATE "C" LIMIT 1',
                        (after, after),
                    ).fetchone()
                if recording is None:
                    break
                rid = recording["id"]
                count = conn.execute(
                    "SELECT count(*) n FROM transcripts.turn WHERE recording_id=%s",
                    (rid,),
                ).fetchone()["n"]
                original_recording = recording
                if recording.get("started_at") is None:
                    first = conn.execute(
                        "SELECT min(COALESCE(said_at_manual,said_at)) AS at FROM transcripts.turn WHERE recording_id=%s",
                        (rid,),
                    ).fetchone()["at"]
                    recording = {
                        **recording,
                        "started_at": first,
                        "time_precision": "first_turn",
                    }
                try:
                    parent = recording_message(recording, count, original_recording)
                except BAD_ROW:
                    self.quarantine(rid, RECORDINGS)
                    after, active, last_turn = rid, None, None
                    yield Checkpoint(cursor={"source": "archive", "after": after})
                    continue
                yield parent
                while True:
                    rows = conn.execute(
                        "SELECT to_jsonb(t) AS turn, to_jsonb(s) AS speaker FROM transcripts.turn t LEFT JOIN transcripts.speaker s ON s.recording_id=t.recording_id AND s.label=t.speaker WHERE t.recording_id=%s AND (%s::integer IS NULL OR t.turn_no>%s) ORDER BY t.turn_no LIMIT 200",
                        (rid, last_turn, last_turn),
                    ).fetchall()
                    if not rows:
                        break
                    linked = []
                    for row in rows:
                        try:
                            message = turn_message(
                                original_recording, row["turn"], row["speaker"]
                            )
                        except BAD_ROW:
                            self.quarantine(f"{rid}:{row['turn']['turn_no']}")
                        else:
                            yield message
                            linked.append(message)
                        last_turn = row["turn"]["turn_no"]
                    # Keep messages consecutive so the SDK can ingest this
                    # bounded source batch in one request, then batch its
                    # links after every endpoint is durable.
                    for message in linked:
                        yield part_of(message, rid)
                    # Checkpoint the bounded source batch, rather than every
                    # turn. A lost acknowledgement replays at most 200
                    # idempotent rows, while a multi-day catch-up does not
                    # force one HTTP cursor write per transcript turn.
                    yield Checkpoint(
                        cursor={
                            "source": "archive",
                            "after": after,
                            "recording": rid,
                            "turn_no": last_turn,
                        }
                    )
                after, active, last_turn = rid, None, None
                yield Checkpoint(cursor={"source": "archive", "after": after})

    def api(self, cursor, mode="sync"):
        today = datetime.now(timezone.utc).date().isoformat()
        budget = dict(cursor.get("budget", {}))
        if budget.get("day") != today:
            budget = {"day": today, "requests": 0}
        since = (
            cursor.get("since")
            if mode == "sync" or cursor.get("page_snapshot")
            else None
        )
        high_water = cursor.get("high_water", since)
        # Wave lists a session only once it has finished processing, which can
        # be after a newer session moved the watermark. Each sync therefore
        # lists again from the watermark minus an overlap, and skips sessions
        # whose list row is unchanged since they were imported.
        overlap = timedelta(hours=float(self.settings["overlap_hours"]))
        if overlap < timedelta(0):
            raise ValueError("WAVE_API_OVERLAP_HOURS must not be negative")
        seen = {
            # Cursors written before the overlap held {id: signature} for the
            # sessions at the watermark only.
            sid: value if isinstance(value, list) else [value, high_water]
            for sid, value in cursor.get("seen", cursor.get("boundary", {})).items()
        }

        # Sessions quarantined as a whole: {id: [signature, attempts, retry_at]}.
        # Each failure doubles the wait (1 h up to 24 h), so a session that never
        # becomes importable cannot spend the daily budget; a changed list row
        # is retried at once. Entries no longer listed expire two days after
        # their retry time.
        failed = {
            sid: list(value)
            for sid, value in cursor.get("failed", {}).items()
            if instant(value[2]) > datetime.now(timezone.utc) - timedelta(days=2)
        }

        def forget():
            if high_water is not None:
                floor = instant(high_water) - overlap
                for sid in [
                    k for k, v in seen.items() if v[1] is None or instant(v[1]) < floor
                ]:
                    del seen[sid]

        def state(**extra):
            return {
                "source": "api",
                "since": since,
                "high_water": high_water,
                "budget": dict(budget),
                "seen": {k: list(v) for k, v in seen.items()},
                "failed": {k: list(v) for k, v in failed.items()},
                **extra,
            }

        if budget["requests"] >= DAILY_REQUESTS:
            self.budget_stopped(budget)
            yield Checkpoint(
                cursor=state(
                    **{
                        k: v
                        for k, v in cursor.items()
                        if k in {"page", "index", "page_snapshot"}
                    }
                )
            )
            return
        fixture = self.settings["fixture"]
        if not fixture and not self.secrets["token"]:
            raise ValueError("Set WAVE_API_TOKEN")
        responses = (
            json.loads(Path(fixture).read_text(encoding="utf-8")) if fixture else None
        )
        with httpx.Client(
            base_url="https://api.wave.co/v1/",
            timeout=60,
            headers={"Authorization": "Bearer " + (self.secrets["token"] or "fixture")},
        ) as client:
            last_request = None

            def get(path, params=None):
                nonlocal last_request
                if responses is not None:
                    key = path + (
                        "?cursor=" + params["cursor"]
                        if params and params.get("cursor")
                        else ""
                    )
                    result = responses[key]
                    if path == "sessions" and params and params.get("since"):
                        result = {
                            **result,
                            "sessions": [
                                item
                                for item in result["sessions"]
                                if instant(
                                    responses["sessions/" + item["id"]]["timestamp"]
                                )
                                > instant(params["since"])
                            ],
                        }
                    return result
                for attempt in range(5):
                    # Below the documented 60 requests/minute cap. No audio routes.
                    if last_request is not None:
                        time.sleep(max(0, 1.05 - (time.monotonic() - last_request)))
                    last_request = time.monotonic()
                    try:
                        response = client.get(path, params=params)
                    except httpx.TransportError:
                        if attempt == 4:
                            raise ValueError("Wave API transport failed") from None
                        time.sleep(2**attempt)
                        continue
                    if response.status_code == 429 or response.status_code >= 500:
                        if attempt < 4:
                            time.sleep(
                                min(
                                    60,
                                    max(
                                        2**attempt,
                                        float(response.headers.get("Retry-After", "0")),
                                    ),
                                )
                            )
                            continue
                    if response.is_error:
                        raise ValueError(
                            f"Wave API request failed (HTTP {response.status_code})"
                        )
                    return response.json()
                raise ValueError("Wave API retry limit reached")

            page_cursor = cursor.get("page")
            offset = cursor.get("index", 0)
            saved_page = cursor.get("page_snapshot")
            while True:
                if not saved_page:
                    if budget["requests"] >= DAILY_REQUESTS:
                        self.budget_stopped(budget)
                        yield Checkpoint(cursor=state(page=page_cursor, index=offset))
                        return
                    # Reserve all five retry attempts before the request. A crash
                    # cannot erase quota consumption from the durable cursor.
                    budget["requests"] += 5
                    yield Checkpoint(cursor=state(page=page_cursor, index=offset))
                page = saved_page or get(
                    "sessions",
                    {
                        "limit": 100,
                        **(
                            {
                                "since": (instant(since) - overlap)
                                .astimezone(timezone.utc)
                                .strftime("%Y-%m-%dT%H:%M:%SZ")
                            }
                            if since
                            else {}
                        ),
                        **({"cursor": page_cursor} if page_cursor else {}),
                    },
                )
                # Persist the bounded source page, so resume does not skip a
                # session when new recordings shift the server's newest page.
                page = {
                    "sessions": page["sessions"],
                    "has_more": page.get("has_more", False),
                    "next_cursor": page.get("next_cursor"),
                }
                saved_page = None
                for index, item in enumerate(page["sessions"]):
                    if index < offset:
                        continue
                    if budget["requests"] >= DAILY_REQUESTS:
                        self.budget_stopped(budget)
                        yield Checkpoint(
                            cursor=state(
                                page=page_cursor, index=index, page_snapshot=page
                            )
                        )
                        return
                    sid = item["id"]
                    signature = hashlib.sha256(
                        json.dumps(item, sort_keys=True).encode()
                    ).hexdigest()
                    waiting = failed.get(sid)
                    if since and (
                        seen.get(sid, [None])[0] == signature
                        or (
                            waiting
                            and waiting[0] == signature
                            and instant(waiting[2]) > datetime.now(timezone.utc)
                        )
                    ):
                        yield Checkpoint(
                            cursor=state(
                                page=page_cursor, index=index + 1, page_snapshot=page
                            )
                        )
                        continue
                    budget["requests"] += 10
                    yield Checkpoint(
                        cursor=state(page=page_cursor, index=index, page_snapshot=page)
                    )
                    detail = get("sessions/" + quote(sid, safe=""))
                    transcript = get("sessions/" + quote(sid, safe="") + "/transcript")
                    try:
                        segments = transcript.get("segments")
                        if segments is None:
                            raise ValueError(
                                "Wave API transcript has no timed segments; refusing to invent turn times"
                            )
                        recording = {
                            "id": sid,
                            "title": detail.get("title"),
                            "started_at": detail["timestamp"],
                            "duration_s": detail.get("duration_seconds"),
                            "source_platform": detail.get("platform"),
                            "metadata": {
                                "summary": detail.get("summary"),
                                "type": detail.get("type"),
                            },
                        }
                        stamp = detail["timestamp"]
                        if high_water is None or instant(stamp) > instant(high_water):
                            high_water = stamp
                        yield recording_message(recording, len(segments), detail)
                        for number, segment in enumerate(segments, 1):
                            try:
                                turn = {
                                    "turn_no": number,
                                    "speaker": segment.get("speaker"),
                                    "start_s": segment["start"],
                                    "end_s": segment.get("end"),
                                    "text": segment["text"],
                                    "said_at": (
                                        instant(detail["timestamp"])
                                        + timedelta(seconds=segment["start"])
                                    ).isoformat(),
                                }
                                message = turn_message(
                                    recording,
                                    turn,
                                    payload={
                                        "session": detail,
                                        "transcript": transcript,
                                        "segment": segment,
                                    },
                                )
                                yield message
                                yield part_of(message, sid)
                            except BAD_ROW:
                                self.quarantine(f"{sid}:{number}")
                        # Only a session that imported is remembered, so one
                        # quarantined here is fetched again by later polls,
                        # with the backoff kept in "failed".
                        seen[sid] = [signature, stamp]
                        failed.pop(sid, None)
                        forget()
                    except BAD_ROW:
                        self.quarantine(sid, RECORDINGS)
                        attempts = (
                            waiting[1] + 1
                            if waiting and waiting[0] == signature
                            else 1
                        )
                        failed[sid] = [
                            signature,
                            attempts,
                            (
                                datetime.now(timezone.utc)
                                + timedelta(hours=min(24, 2 ** (attempts - 1)))
                            ).isoformat(),
                        ]
                    yield Checkpoint(
                        cursor=state(
                            page=page_cursor, index=index + 1, page_snapshot=page
                        )
                    )
                if not page.get("has_more"):
                    break
                next_cursor = page.get("next_cursor")
                if not next_cursor or next_cursor == page_cursor:
                    raise ValueError("Wave API pagination did not advance")
                page_cursor, offset = next_cursor, 0
                yield Checkpoint(cursor=state(page=page_cursor, index=0))
            yield Checkpoint(cursor=state(since=high_water))


def main():
    run(Wave)
