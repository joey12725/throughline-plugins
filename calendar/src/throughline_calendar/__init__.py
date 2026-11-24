"""Google Calendar through Composio: event occurrences and event lifecycle.

Two layers: `fetch.py` (standard library only) reads Composio and writes raw
JSONL records; this plugin maps those records to Throughline messages, either
from export files (CALENDAR_SOURCE=export:<file or directory>) or by running
the fetcher in-process (CALENDAR_SOURCE=live).
"""

import copy
import logging

from throughline_sdk import (
    Channel,
    Checkpoint,
    Declaration,
    Manifest,
    Plugin,
    Record,
    run,
)

from . import fetch, sources
from .mapping import CHANGES, EVENTS, MAPPING_REVISION, Mapper

__all__ = ["Calendar", "MAPPING_REVISION", "main"]


def fields(**values):
    return [dict(name=k, type=v[0], description=v[1]) for k, v in values.items()]


COMMON = dict(
    account=("text", "Composio connected account id (or MCP account name)"),
    calendar_id=("text", "Google calendar id"),
    calendar_name=("text", "Calendar name (summaryOverride, else summary)"),
    event_id=("text", "Google event id; an occurrence id for recurring instances"),
    series_id=("text", "recurringEventId of the series, if recurring"),
    ical_uid=("text", "iCalUID shared by every copy of the event"),
    title=("text", "Event title"),
    status=(
        "text",
        "confirmed, tentative, cancelled, or removed (vanished from a complete window)",
    ),
    all_day=("boolean", "All-day event; times are UTC midnight with day precision"),
    place=("text", "Location text as entered"),
    observed_at=(
        "timestamp",
        "Fetch time that observed a change the API does not timestamp",
    ),
    mapping_revision=("integer", "Mapping revision that produced this record"),
)

PLUGIN_SETTINGS = [
    (
        "source",
        "CALENDAR_SOURCE",
        "live",
        "live, or export:<JSONL file or directory of export files>",
    ),
    (
        "archive_dir",
        "CALENDAR_ARCHIVE_DIR",
        None,
        "Optional directory where live runs are kept as export files",
    ),
    (
        "links",
        "CALENDAR_LINKS",
        "true",
        "Link records to a calendar_event entity per iCalUID",
    ),
    (
        "batch_size",
        "CALENDAR_BATCH_SIZE",
        "500",
        "Messages per acknowledged batch and checkpoint",
    ),
]


class FetchFailed(RuntimeError):
    """A live run could read no calendar at all."""


class Calendar(Plugin):
    manifest = Manifest(
        name="calendar",
        version="0.1.0",
        channels=[
            Channel(
                name=EVENTS,
                description=(
                    "Event occurrences on Google calendars (recurring events expanded within the fetch "
                    "window); the record time and subject are when the event happens"
                ),
                fields=fields(
                    **COMMON,
                    start_time_zone=("text", "Time zone named on the event start"),
                    original_start=(
                        "timestamp",
                        "Original start of a moved or cancelled recurring instance",
                    ),
                    description=("text", "Event description as plain text"),
                    organizer_email=(
                        "text",
                        "Organizer email (also the author identifier)",
                    ),
                    organizer_name=("text", "Organizer display name"),
                    attendees=(
                        "json",
                        "Attendees: email, name, response, organizer, self, optional, resource",
                    ),
                    attendee_count=("integer", "Number of attendees listed"),
                    my_response=("text", "The calendar owner's response status"),
                    conference_url=("text", "Video meeting link"),
                    html_link=("text", "Google Calendar link"),
                    event_type=(
                        "text",
                        "default, outOfOffice, focusTime, workingLocation, birthday, ...",
                    ),
                    visibility=("text", "Event visibility"),
                    transparency=("text", "opaque (busy) or transparent (free)"),
                    event_created=("timestamp", "When the event was created (API)"),
                    event_updated=(
                        "timestamp",
                        "When the event was last modified (API)",
                    ),
                ),
            ),
            Channel(
                name=CHANGES,
                description=(
                    "Event lifecycle: created, changed, cancelled, removed, restored. The record time is "
                    "the API's created/updated time, or the fetch time for changes found by snapshot diff"
                ),
                fields=fields(
                    **COMMON,
                    change=("text", "created, changed, cancelled, removed or restored"),
                    changed_fields=(
                        "json",
                        "Fields that differ from the previous snapshot; null when unknown",
                    ),
                    event_start=("timestamp", "Event start after the change"),
                    event_end=("timestamp", "Event end after the change"),
                    recurrence=("json", "RRULE/EXDATE lines of a recurring series"),
                    creator_email=("text", "Event creator email"),
                    organizer_email=("text", "Organizer email"),
                    time_precision=(
                        "text",
                        "source (API timestamp) or observed (fetch time of a snapshot diff)",
                    ),
                    occurrence_count=(
                        "integer",
                        "Occurrences of a series that vanished together",
                    ),
                ),
            ),
        ],
        settings={
            key: Declaration(
                env=env, required=False, default=default, description=description
            )
            for key, env, default, description in PLUGIN_SETTINGS + fetch.SETTINGS
        },
        secrets={
            key: Declaration(env=env, required=False, description=description)
            for key, env, description in fetch.SECRETS
        },
        run_mode="poll",
        default_interval_s=1800,
        link_types=["part_of", "about"],
    )

    def __init__(self):
        super().__init__()
        self.kind, self.path = sources.source_spec(self.settings["source"])
        self.batch_size = int(self.settings["batch_size"])
        if self.batch_size < 1:
            raise ValueError("CALENDAR_BATCH_SIZE must be positive")
        self.with_links = fetch.truthy(self.settings["links"])
        fetch.config(self.fetch_settings())  # Fail early on a bad setting.
        self.dry_run = False
        self.stats = {}
        # Test seam for the live fetch: opener, sleep, now.
        self.fetch_options = {}

    def fetch_settings(self):
        return {key: self.settings[key] for key, _, _, _ in fetch.SETTINGS}

    def execute(self, client, mode, dry_run=False):
        self.dry_run = dry_run
        report = super().execute(client, mode, dry_run)
        if self.stats:
            report["calendar"] = self.stats
        return report

    def pending_runs(self, state, resume, mode):
        """(runs to map, resume offset) for this invocation."""
        last = state.get("last_run")
        if self.kind == "export":
            runs, skipped = sources.export_runs(self.path)
            upto = (resume or {}).get("upto")
            pending = [
                (key, records)
                for key, records in runs
                if (last is None or key > last) and (upto is None or key <= upto)
            ]
            self.stats.update(runs_in_export=len(runs), runs_without_header=skipped)
            return pending, ((resume or {}).get("offset", 0) if upto else 0)
        fetch_state = state.setdefault("fetch", {})
        key, records, report = sources.live_run(
            self.fetch_settings(),
            self.secrets,
            mode,
            fetch_state,
            log=lambda text: logging.warning("Calendar %s", text),
            **self.fetch_options,
        )
        self.stats["fetch"] = report
        if self.settings["archive_dir"] and not self.dry_run:
            sources.archive(self.settings["archive_dir"], records)
        passes = [r for r in records if r["kind"] in ("window", "changes")]
        if report.get("errors") and not passes:
            # Nothing usable at all (bad key, wrong account): fail the run so
            # the heartbeat reports it instead of a quiet success.
            raise FetchFailed(
                "calendar fetch failed for every calendar; see the warnings above"
            )
        if key is None or (last is not None and key <= last):
            return [], 0
        return [(key, records)], 0

    @staticmethod
    def with_day(base, state):
        """A copy of `base` carrying the per-day call counter of `state`."""
        spent = state.get("fetch") or {}
        result = copy.deepcopy(base)
        result.setdefault("fetch", {}).update(
            {k: spent[k] for k in ("day", "day_calls") if k in spent}
        )
        return result

    def records(self, cursor, mode):
        self.stats = {}
        state = copy.deepcopy(cursor or {})
        if state.get("mapping_revision") != MAPPING_REVISION:
            # A new mapping replays the inputs; only the call counter survives.
            kept = {
                k: v
                for k, v in (state.get("fetch") or {}).items()
                if k in ("day", "day_calls")
            }
            state = {"mapping_revision": MAPPING_REVISION, "fetch": kept}
        resume = state.pop("resume", None)
        base = copy.deepcopy(state)
        try:
            pending, offset = self.pending_runs(state, resume, mode)
        except FetchFailed:
            # Keep the calls a failed live run spent, so the per-day cap still
            # holds when every run fails; nothing else moves.
            yield Checkpoint(cursor=self.with_day(base, state))
            raise
        if self.kind == "live":
            # Interim checkpoints keep the spent calls; cursors move at the end.
            base = self.with_day(base, state)
        plan, links = {}, {}

        def emit(message, link):
            if message.get("subject") is None:
                message.pop("subject", None)
            key = (message["channel"], message["external_id"])
            plan[key] = message  # The last state of a record in this invocation wins.
            if link:
                links[key] = link

        mapper = Mapper(
            state,
            calendars=fetch.split_list(self.settings["calendars"]),
            exclude=fetch.split_list(self.settings["exclude_calendars"]),
            emit=emit,
        )
        for key, records in pending:
            mapper.process(records)
            state["last_run"] = key
        self.stats.update(runs_mapped=len(pending), **mapper.stats)
        items = list(plan.items())
        upto = pending[-1][0] if pending else None
        for start in range(offset, len(items), self.batch_size):
            chunk = items[start : start + self.batch_size]
            for _, message in chunk:
                yield message
            if self.with_links:
                for key, _ in chunk:
                    if key in links:
                        yield Record(kind="link", data=links[key])
            if start + self.batch_size < len(items):
                interim = copy.deepcopy(base)
                if self.kind == "export":
                    interim["resume"] = {
                        "upto": upto,
                        "offset": start + self.batch_size,
                    }
                yield Checkpoint(cursor=interim)
        yield Checkpoint(cursor=state)


def main(argv=None):
    run(Calendar, argv)
