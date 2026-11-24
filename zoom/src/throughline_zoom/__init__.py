"""Zoom cloud recording transcripts for Throughline (Server-to-Server OAuth).

Lists the account owner's cloud recordings in 30-day windows (Zoom's
maximum per request), downloads each TRANSCRIPT (WebVTT) file, and yields
one message per speaker turn plus one message per meeting. Turns link
part_of their meeting.
"""

import logging
from datetime import date, datetime, timedelta, timezone

import httpx
from throughline_sdk import (
    Channel,
    Checkpoint,
    Declaration,
    Manifest,
    Plugin,
    coverage,
    run,
)

from . import vtt
from .client import Zoom, ZoomError, fixture_transport, meeting_path_id
from .mapping import (
    MEETINGS,
    TRANSCRIPTS,
    UNKNOWN,
    People,
    meeting_message,
    parse_time,
    part_of,
    transcript_files,
    turn_messages,
)

__all__ = ["ZoomPlugin", "main", "meeting_path_id"]

log = logging.getLogger("throughline_zoom")
WINDOW_DAYS = 30
# A recording without a transcript yet is re-listed for this long.
PENDING_DAYS = 30
FRESH = timedelta(days=2)


def fields(spec):
    return [{"name": n, "type": t, "description": d} for n, t, d in spec]


class ZoomPlugin(Plugin):
    manifest = Manifest(
        name="zoom",
        version="0.1.0",
        default_interval_s=3600,
        link_types=["part_of"],
        channels=[
            Channel(
                name=TRANSCRIPTS,
                description="Zoom cloud recording transcript turns (consecutive cues of one speaker), threaded by meeting",
                conversation_whole_thread=True,
                fields=fields(
                    [
                        ("speaker_label", "text", "Speaker name as Zoom's transcript labels it"),
                        ("start_s", "number", "Turn start offset in the recording, seconds"),
                        ("end_s", "number", "Turn end offset in the recording, seconds"),
                        ("cue_count", "integer", "WebVTT cues merged into this turn"),
                        ("is_owner", "boolean", "Spoken by the account owner (host); null when the owner is unknown"),
                        ("author_kind", "text", "owner or other (human speech, machine transcribed)"),
                        ("meeting_id", "text", "Zoom numeric meeting id (shared by recurring instances)"),
                    ]
                ),
            ),
            Channel(
                name=MEETINGS,
                description="Zoom recorded meetings: topic, time span, host, speakers and optional participant report",
                fields=fields(
                    [
                        ("meeting_id", "text", "Zoom numeric meeting id (shared by recurring instances)"),
                        ("topic", "text", "Meeting topic"),
                        ("duration_min", "integer", "Meeting duration in minutes"),
                        ("timezone", "text", "Meeting time zone"),
                        ("host_id", "text", "Zoom user id of the host (the account owner)"),
                        ("host_email", "text", "Host email from /users/me when readable"),
                        ("recording_count", "integer", "Recording files Zoom lists"),
                        ("transcript_status", "text", "complete, processing, none or failed"),
                        ("turn_count", "integer", "Transcript turns imported"),
                        ("speakers", "json", "Distinct transcript speaker labels in order"),
                        ("participant_count", "integer", "Distinct participants in the report"),
                        ("participants", "json", "[{name, email}] from the participant report"),
                        ("participants_status", "text", "ok, disabled, not_found or unavailable"),
                    ]
                ),
            ),
        ],
        settings={
            "earliest": Declaration(
                env="ZOOM_EARLIEST",
                default="2020-01-01",
                description="Oldest meeting date (YYYY-MM-DD) a backfill walks back to",
            ),
            "overlap_hours": Declaration(
                env="ZOOM_OVERLAP_HOURS",
                default="72",
                description="Sync re-lists this far back: transcripts appear hours after a meeting",
            ),
            "participants": Declaration(
                env="ZOOM_PARTICIPANTS",
                default="0",
                description="1 reads the participant report (paid plan + report scope)",
            ),
            "owner_names": Declaration(
                env="ZOOM_OWNER_NAMES",
                required=False,
                description="Comma-separated extra speaker labels that are you",
            ),
            "access_label": Declaration(
                env="ZOOM_ACCESS_LABEL",
                required=False,
                description="Access label for every record (for example work)",
            ),
            "page_size": Declaration(env="ZOOM_PAGE_SIZE", default="300", description="Meetings per list page (max 300)"),
            "batch_size": Declaration(env="ZOOM_BATCH_SIZE", default="200", description="Turns per checkpointed batch"),
            "fixture": Declaration(
                env="ZOOM_FIXTURE",
                required=False,
                description="Recorded-response JSON file used instead of the network (tests)",
            ),
            "now": Declaration(
                env="ZOOM_NOW",
                required=False,
                description="Clock override, ISO instant (tests with recorded responses)",
            ),
        },
        secrets={
            "account_id": Declaration(env="ZOOM_ACCOUNT_ID", description="Server-to-Server OAuth account id"),
            "client_id": Declaration(env="ZOOM_CLIENT_ID", description="Server-to-Server OAuth client id"),
            "client_secret": Declaration(env="ZOOM_CLIENT_SECRET", description="Server-to-Server OAuth client secret"),
        },
    )

    transport = None  # tests may inject an httpx transport

    def client(self):
        transport = self.transport
        if transport is None and self.settings.get("fixture"):
            transport = fixture_transport(self.settings["fixture"])
        return Zoom(
            self.secrets["account_id"],
            self.secrets["client_id"],
            self.secrets["client_secret"],
            transport=transport,
        )

    def now(self):
        if self.settings.get("now"):
            return parse_time(self.settings["now"]).astimezone(timezone.utc)
        return datetime.now(timezone.utc)

    def records(self, cursor, mode):
        zoom = self.client()
        try:
            yield from self._records(zoom, dict(cursor or {}), mode)
        finally:
            zoom.close()

    def _start(self, cursor, mode, now, earliest):
        if mode == "backfill" or not cursor.get("complete"):
            return earliest
        lookback = timedelta(hours=float(self.settings["overlap_hours"]))
        # catch_up_since widens the lookback over an outage; apply its span
        # to this plugin's clock so a clock override stays consistent.
        span = datetime.now(timezone.utc) - self.catch_up_since(lookback)
        start = (now - span).date()
        for when in (cursor.get("pending") or {}).values():
            start = min(start, date.fromisoformat(when[:10]))
        return max(start, earliest)

    def _records(self, zoom, cursor, mode):
        now = self.now()
        earliest = date.fromisoformat(self.settings["earliest"])
        page_size = max(1, min(300, int(self.settings["page_size"])))
        batch = max(1, int(self.settings["batch_size"]))
        label = self.settings.get("access_label") or None
        extra = [n for n in (self.settings.get("owner_names") or "").split(",") if n.strip()]
        self.read_participants = self.settings.get("participants") in ("1", "true", "yes")
        self.participant_failures = []
        self.transcript_failures = []

        pending = {
            uuid: when
            for uuid, when in (cursor.get("pending") or {}).items()
            if date.fromisoformat(when[:10]) >= (now - timedelta(days=PENDING_DAYS)).date()
        }
        state = {
            "watermark": cursor.get("watermark"),
            "complete": bool(cursor.get("complete")),
            "pending": pending,
        }
        scan = cursor.get("scan")
        resumed = bool(scan)
        if not scan:
            start = self._start(cursor, mode, now, earliest)
            scan = {"start": start.isoformat(), "end": now.date().isoformat(), "token": None, "index": 0}

        try:
            me = zoom.me()
        except ZoomError as error:
            log.warning("owner profile unreadable (HTTP %s); owner marking uses ZOOM_OWNER_NAMES", error.status)
            me = None

        def checkpoint(**position):
            return Checkpoint(cursor=dict(state, pending=dict(state["pending"]), scan=dict(scan, **position)))

        window_start = date.fromisoformat(scan["start"])
        end = date.fromisoformat(scan["end"])
        token, index = scan.get("token"), int(scan.get("index") or 0)
        while window_start <= end:
            window_end = min(window_start + timedelta(days=WINDOW_DAYS - 1), end)
            scan["start"] = window_start.isoformat()
            while True:
                try:
                    page = zoom.recordings(window_start.isoformat(), window_end.isoformat(), page_size, token)
                except ZoomError as error:
                    # Zoom page tokens expire after 15 minutes: a resumed
                    # token restarts its window (replay is idempotent).
                    if resumed and token and error.status in (400, 404):
                        resumed, token, index = False, None, 0
                        continue
                    raise
                resumed = False
                for position, meeting in enumerate(page.get("meetings") or []):
                    if position < index:
                        continue
                    yield from self.meeting(zoom, meeting, me, extra, label, batch, state, now,
                                            lambda i=position, t=token: checkpoint(token=t, index=i))
                    yield checkpoint(token=token, index=position + 1)
                token, index = page.get("next_page_token") or None, 0
                if not token:
                    break
                yield checkpoint(token=token, index=0)
            window_start = window_end + timedelta(days=1)
            if window_start <= end:
                scan["start"] = window_start.isoformat()
                yield checkpoint(token=None, index=0)

        yield from self.coverage_records()
        state["complete"] = True
        yield Checkpoint(cursor=dict(state, pending=dict(state["pending"]), scan=None))

    def coverage_records(self):
        if self.participant_failures:
            yield coverage(
                channel=MEETINGS,
                scope_key="participants",
                status="partial",
                reasons=["participants_unavailable"],
                detail={"meetings": len(self.participant_failures),
                        "http_status": sorted(set(self.participant_failures))},
            )
        if self.transcript_failures:
            yield coverage(
                channel=TRANSCRIPTS,
                scope_key="transcripts",
                status="partial",
                reasons=["transcript_unavailable"],
                detail={"meetings": len(self.transcript_failures),
                        "statuses": sorted(set(self.transcript_failures))},
            )

    def participants(self, zoom, uuid):
        if not self.read_participants:
            return None, "disabled"
        try:
            return zoom.participants(uuid), "ok"
        except ZoomError as error:
            self.participant_failures.append(error.status)
            if error.status == 404 or error.code == 3001:
                return None, "not_found"
            # Plan or scope refusal: stop asking for the rest of this run.
            log.warning("participant report unavailable (HTTP %s); continuing without it", error.status)
            self.read_participants = False
            return None, "unavailable"

    def transcript(self, zoom, meeting, now):
        """(turns, status). Turns carry their source file for timing."""
        files = transcript_files(meeting)
        ready = [f for f in files if (f.get("status") or "completed") == "completed" and f.get("download_url")]
        if not ready:
            processing = any(
                (f.get("status") or "completed") != "completed" for f in meeting.get("recording_files") or []
            )
            if files or processing:
                return [], "processing"
            recent = now - parse_time(meeting["start_time"]) < FRESH
            return [], "processing" if recent else "none"
        out = []
        try:
            for file in ready:
                for turn in vtt.turns(vtt.cues(zoom.download_text(file["download_url"]))):
                    out.append(dict(turn, speaker=turn["speaker"] or UNKNOWN, file=file))
        except (ZoomError, ValueError, httpx.HTTPError) as error:
            log.warning("transcript download failed for one meeting: %s", type(error).__name__)
            self.transcript_failures.append(str(getattr(error, "status", type(error).__name__)))
            return [], "failed"
        return out, "complete"

    def meeting(self, zoom, meeting, me, extra, label, batch, state, now, redo):
        uuid = meeting["uuid"]
        turn_list, status = self.transcript(zoom, meeting, now)
        participants, participants_status = self.participants(zoom, uuid)
        people = People(me, extra, participants or (), meeting.get("host_id"))
        record = meeting_message(meeting, me, turn_list, status, participants, participants_status, label)
        if status in ("processing", "failed"):
            state["pending"][uuid] = meeting["start_time"]
        else:
            state["pending"].pop(uuid, None)
        if state["watermark"] is None or parse_time(record["happened_at"]) > parse_time(state["watermark"]):
            state["watermark"] = record["happened_at"]
        yield record
        turns = turn_messages(meeting, people, turn_list, label)
        for start in range(0, len(turns), batch):
            chunk = turns[start : start + batch]
            yield from chunk
            for turn in chunk:
                yield part_of(turn, uuid)
            if start + batch < len(turns):
                # Mid-meeting: a resume redoes this meeting (unchanged rows).
                yield redo()


def main():
    run(ZoomPlugin)
