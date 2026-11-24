"""Granola meetings as a Throughline source.

Two live readers produce the same normalized meetings (see mapping.py):

- ``api`` (official, documented): Granola's public API with a personal or
  workspace API key, polled incrementally by ``updated_after``.
- ``cache`` (undocumented, legacy): the desktop app's plaintext local cache
  (``cache-v3.json`` and similar), re-read on every run because the app keeps
  rewriting it. Current Granola releases encrypt the cache, so this mode only
  works with older app versions.

Channels: granola.transcript (one record per transcript segment, the primary
channel), granola.meetings (one reference record per meeting),
granola.my_notes (what you typed) and granola.summaries (Granola's AI notes,
record_kind derived, with provenance to the transcript and your notes).
"""

import copy
from datetime import timedelta

from throughline_sdk import (
    Channel,
    Checkpoint,
    Declaration,
    Manifest,
    Plugin,
    coverage,
    run,
)

from . import api as api_mod
from . import cache as cache_mod
from .mapping import (
    MAPPING_REVISION,
    MEETINGS,
    MY_NOTES,
    SUMMARIES,
    TRANSCRIPT,
    Mapper,
    fingerprint,
)
from .text import moment

__all__ = ["Granola", "MAPPING_REVISION", "main"]


def fields(**values):
    return [dict(name=k, type=v[0], description=v[1]) for k, v in values.items()]


COMMON = dict(
    meeting_id=("text", "Granola document id (cache) or note id (API, not_...)"),
    source_mode=("text", "cache or api: which reader produced the record"),
)

TRANSCRIPT_FIELDS = fields(
    **COMMON,
    speaker_label=(
        "text",
        "Anonymous speaker label within this meeting (Others, Speaker A, ...) when the "
        "speaker is not you and could not be matched to an attendee",
    ),
    speaker_name=("text", "Speaker name, when Granola identified one or it was inferred"),
    speaker_source=(
        "text",
        "Audio source: microphone (your mic), system or speaker (the other side's audio)",
    ),
    speaker_attribution=("text", "me (the note-taker), them (others) or unknown"),
    speaker_inferred=(
        "boolean",
        "The author was inferred: a one-to-one meeting's other attendee for 'them' audio",
    ),
    author_kind=("text", "owner (you said it) or other"),
    start_s=("number", "Seconds from the first transcript segment of the meeting"),
    end_s=("number", "End, in seconds from the first transcript segment"),
    segment_id=("text", "Segment id in the source (cache) or position (API)"),
)

MEETING_FIELDS = fields(
    **COMMON,
    meeting_title=("text", "Note title, else the calendar event title"),
    calendar_event_id=("text", "Calendar event id"),
    calendar_title=("text", "Calendar event title"),
    scheduled_start=("timestamp", "Scheduled start of the calendar event"),
    scheduled_end=("timestamp", "Scheduled end of the calendar event"),
    organizer_email=("text", "Calendar organizer e-mail"),
    note_owner_email=("text", "E-mail of the note-taker (the note's owner)"),
    is_own_note=("boolean", "The note is yours (not a note shared with you)"),
    attendee_emails=("json", "Attendee e-mail addresses"),
    attendees=("json", "Attendees: email, name, organizer, self"),
    attendee_count=("integer", "Number of attendees"),
    folders=("json", "Granola folders the note is in"),
    transcript_status=(
        "text",
        "ok, missing (no transcript in the source) or disabled (an admin turned off "
        "transcript access over the API)",
    ),
    utterance_count=("integer", "Transcript segments delivered for the meeting"),
    web_url=("text", "Link to the note in Granola (API mode)"),
    note_created_at=("timestamp", "When the note was created"),
    note_updated_at=("timestamp", "When the note was last updated"),
)

MY_NOTES_FIELDS = fields(
    **COMMON,
    meeting_title=("text", "Meeting title"),
    author_kind=("text", "owner (your notes) or other (a note shared with you)"),
    note_format=("text", "markdown or plain"),
    note_updated_at=("timestamp", "When the note was last updated"),
)

SUMMARY_FIELDS = fields(
    **COMMON,
    meeting_title=("text", "Meeting title"),
    author_kind=("text", "Always agent: written by Granola's AI"),
    panel_id=("text", "Panel id (cache) or summary (API)"),
    panel_title=("text", "Panel or template title, e.g. Summary"),
    template=("text", "Granola template slug of the panel, when known"),
    source_count=("integer", "Transcript segments and typed notes the summary cites"),
)

SETTINGS = [
    ("source", "GRANOLA_SOURCE", "auto",
     "auto (api when GRANOLA_API_KEY or GRANOLA_FIXTURE is set, else cache), api or cache"),
    ("cache_path", "GRANOLA_CACHE_PATH", None,
     "Cache file or folder (default ~/Library/Application Support/Granola, newest cache-v*.json)"),
    ("fixture", "GRANOLA_FIXTURE", None, "Tests: recorded API responses (JSON) instead of the network"),
    ("api_url", "GRANOLA_API_URL", api_mod.BASE_URL, "API base URL"),
    ("owner_email", "GRANOLA_OWNER_EMAIL", None,
     "Your e-mail; notes owned by someone else are then marked as not yours"),
    ("owner_name", "GRANOLA_OWNER_NAME", None, "Your display name"),
    ("access_label", "GRANOLA_ACCESS_LABEL", None,
     "Access label for every record (e.g. work), gating it for non-owner askers"),
    ("batch_size", "GRANOLA_BATCH_SIZE", "20", "Meetings per checkpoint"),
    ("request_pause_s", "GRANOLA_REQUEST_PAUSE_S", "0.25",
     "Seconds between API requests (the API allows 5/s sustained)"),
]


class Granola(Plugin):
    manifest = Manifest(
        name="granola",
        version="0.1.0",
        channels=[
            Channel(
                name=TRANSCRIPT,
                description=(
                    "Granola meeting transcripts: one record per transcript segment, "
                    "threaded by meeting (a whole recording); your microphone is you "
                    "(author_kind owner), the other side's audio is the other attendees"
                ),
                fields=TRANSCRIPT_FIELDS,
                conversation_whole_thread=True,
                conversation_gap_s=1800,
            ),
            Channel(
                name=MEETINGS,
                description=(
                    "One reference record per Granola meeting note: title, attendees, "
                    "calendar event, scheduled and recorded times, folders"
                ),
                fields=MEETING_FIELDS,
                is_reference=True,
            ),
            Channel(
                name=MY_NOTES,
                description="The notes you typed yourself in Granola during a meeting",
                fields=MY_NOTES_FIELDS,
            ),
            Channel(
                name=SUMMARIES,
                description=(
                    "AI-generated Granola meeting notes and summaries (author_kind agent), "
                    "derived from the meeting's transcript and your typed notes"
                ),
                fields=SUMMARY_FIELDS,
                record_kind="derived",
            ),
        ],
        settings={
            key: Declaration(env=env, required=False, default=default, description=description)
            for key, env, default, description in SETTINGS
        },
        secrets={
            "api_key": Declaration(
                env="GRANOLA_API_KEY",
                required=False,
                description="Granola API key (grn_...), Settings > API in the Granola app",
            )
        },
        run_mode="poll",
        default_interval_s=900,
        link_types=[],
    )

    def __init__(self, transport=None):
        super().__init__()
        source = (self.settings["source"] or "auto").strip().lower()
        if source == "auto":
            source = "api" if (self.secrets["api_key"] or self.settings["fixture"]) else "cache"
        if source not in ("api", "cache"):
            raise ValueError("GRANOLA_SOURCE must be auto, api or cache")
        self.mode = source
        self.transport = transport  # test seam: an httpx transport
        try:
            self.batch_size = max(1, int(self.settings["batch_size"] or 20))
            self.pause_s = max(0.0, float(self.settings["request_pause_s"] or 0))
        except ValueError:
            raise ValueError("GRANOLA_BATCH_SIZE and GRANOLA_REQUEST_PAUSE_S must be numbers") from None
        self.mapper = Mapper(
            self.settings["owner_email"], self.settings["owner_name"], self.settings["access_label"]
        )
        self.stats = {}

    def config(self):
        return fingerprint([
            MAPPING_REVISION, self.mode, self.settings["owner_email"], self.settings["owner_name"],
            self.settings["access_label"],
        ])

    def execute(self, client, mode, dry_run=False):
        report = super().execute(client, mode, dry_run)
        if self.stats:
            report["granola"] = self.stats
        return report

    def records(self, cursor, mode):
        state = copy.deepcopy(cursor or {})
        if state.get("config") != self.config():
            state = {"config": self.config()}
        self.stats = {"mode": self.mode, "meetings": 0, "skipped_unchanged": 0}
        if self.mode == "cache":
            yield from self.from_cache(state)
        else:
            yield from self.from_api(state)

    # -- cache -----------------------------------------------------------
    def from_cache(self, state):
        path = cache_mod.find_cache(self.settings["cache_path"])
        meetings = cache_mod.meetings(cache_mod.load_state(path))
        done = state.setdefault("done", {})
        pending = 0
        for meeting in sorted(meetings, key=self.order):
            stamp = fingerprint(meeting)
            if done.get(meeting["id"]) == stamp:
                self.stats["skipped_unchanged"] += 1
                continue
            yield from self.mapper.messages(meeting)
            done[meeting["id"]] = stamp
            self.stats["meetings"] += 1
            pending += 1
            if pending >= self.batch_size:
                pending = 0
                yield Checkpoint(cursor=copy.deepcopy(state))
        yield Checkpoint(cursor=copy.deepcopy(state))

    def order(self, meeting):
        start, _ = self.mapper.span(meeting)
        return (moment(start).timestamp() if start and moment(start) else 0.0, meeting["id"])

    # -- api -------------------------------------------------------------
    def client(self):
        transport = self.transport
        if transport is None and self.settings["fixture"]:
            transport = api_mod.fixture_transport(self.settings["fixture"])
        if transport is None and not self.secrets["api_key"]:
            raise ValueError("GRANOLA_API_KEY is required for GRANOLA_SOURCE=api")
        return api_mod.Api(
            self.secrets["api_key"] or "fixture",
            transport=transport,
            base_url=self.settings["api_url"] or api_mod.BASE_URL,
            pause_s=0 if transport is not None else self.pause_s,
        )

    def from_api(self, state):
        client = self.client()
        high = state.get("high_water")  # [updated_at, note id] of the last note delivered
        after = None
        if high:
            after = (moment(high[0]) - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        listed = []
        for note in client.list_notes(updated_after=after):
            when = moment(note.get("updated_at"))
            if not when or note.get("deleted_at"):
                continue
            key = (when, note["id"])
            if high and key <= (moment(high[0]), high[1]):
                continue
            listed.append((key, note))
        listed.sort(key=lambda pair: pair[0])
        pending, disabled = 0, 0
        for (when, note_id), summary in listed:
            note = client.note(note_id)
            if note is not None:
                meeting = api_mod.meeting_of(note)
                disabled += meeting["transcript_status"] == "disabled"
                yield from self.mapper.messages(meeting)
                self.stats["meetings"] += 1
            state["high_water"] = [summary["updated_at"], note_id]
            pending += 1
            if pending >= self.batch_size:
                pending = 0
                yield Checkpoint(cursor=copy.deepcopy(state))
        self.stats["requests"] = client.requests
        if disabled:
            yield coverage(
                channel=TRANSCRIPT,
                status="partial",
                reasons=["transcript_access_disabled"],
                detail={"notes_without_transcript": disabled},
            )
        yield Checkpoint(cursor=copy.deepcopy(state))


def main(argv=None):
    run(Granola, argv)
