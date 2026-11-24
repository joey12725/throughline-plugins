"""Research streams: configured searches whose results become messages.

Two layers. `fetch.py` (standard library only) runs due searches against an
OpenAI-compatible endpoint, or imports results produced elsewhere, and writes
raw JSON Lines. This plugin maps raw records to messages, either live (calling
fetch in-process) or from export files: RESEARCH_SOURCE=live|export:<path>.
"""

import copy
import hashlib
import json
import logging
from pathlib import Path

from throughline_sdk import (
    Channel,
    Checkpoint,
    Declaration,
    Manifest,
    Plugin,
    Record,
    coverage,
    provenance,
    run,
)

from . import fetch, mapping
from .mapping import ITEMS, MAPPING_REVISION, RESULTS

log = logging.getLogger("throughline_research")


def fields(*rows):
    return [dict(name=n, type=t, description=d) for n, t, d in rows]


RESULT_FIELDS = fields(
    ("stream", "text", "Stream id"),
    ("stream_title", "text", "Stream title"),
    ("search_id", "text", "Search id within the stream"),
    ("search_label", "text", "Readable search label"),
    ("cluster", "text", "Interest group or cluster the search belongs to"),
    ("variant", "text", "Search variant (for example A anchored, B open)"),
    (
        "search_kind",
        "text",
        "Search kind from the stream (local-event, current-check, ...)",
    ),
    ("run_date", "text", "Run date YYYY-MM-DD in the stream's timezone"),
    ("window_start", "text", "First day the search asked about"),
    ("window_end", "text", "Last day the search asked about"),
    ("provider", "text", "openai (model call) or import (produced elsewhere)"),
    ("model", "text", "Model name when known"),
    ("import_path", "text", "Imported file path relative to the import directory"),
    ("prompt", "text", "Rendered prompt, when known"),
    ("prompt_hash", "text", "SHA-256 prefix of the rendered prompt"),
    ("status", "text", "ok, or empty when the provider found nothing"),
    (
        "cited_urls",
        "json",
        "Cited URLs: provider annotations first, then URLs in the text",
    ),
    ("citations", "json", "Cited URLs with titles"),
    ("citation_count", "integer", "Number of cited URLs"),
    ("item_count", "integer", "Structured items extracted into research.items"),
    ("prompt_tokens", "integer", "Provider-reported input tokens"),
    ("completion_tokens", "integer", "Provider-reported output tokens"),
    ("stream_revision", "text", "Hash of the stream config used for the run"),
    ("mapping_revision", "integer", "Mapping revision that produced this message"),
)
ITEM_FIELDS = fields(
    ("stream", "text", "Stream id"),
    ("search_id", "text", "Search that found the item"),
    ("result_id", "text", "External id of the research.results message"),
    ("run_date", "text", "Run date of the result"),
    ("item_type", "text", "Item type from the stream, for example event"),
    ("item_key", "text", "Stable key: canonical URL or title, start and venue"),
    ("position", "integer", "1-based position within the result"),
    ("title", "text", "Item title"),
    ("description", "text", "What the item is"),
    ("venue", "text", "Venue name"),
    ("address", "text", "Venue address or details"),
    ("city", "text", "City"),
    ("organizer", "text", "Hosting organization"),
    ("door_time", "text", "Door or set time as written"),
    ("price", "text", "Price as written"),
    ("audience", "text", "Audience or participation details"),
    ("source_tier", "text", "primary, ticketing or credible-secondary"),
    ("date_certainty", "text", "explicit or inferred"),
    ("notes", "text", "Notes"),
    ("canonical_url", "text", "Canonical item URL"),
    ("evidence_url", "text", "Evidence URL"),
    ("ticket_url", "text", "Ticket, RSVP or signup URL"),
    ("primary_confirmed", "boolean", "Confirmed by a primary source"),
    ("occurrence_start", "timestamp", "When the item happens (also its subject start)"),
    ("occurrence_end", "timestamp", "When the item ends"),
    ("date_precision", "text", "exact, or day when only a date was given"),
    ("details", "json", "Other labeled values, and unparsed date text"),
    ("mapping_revision", "integer", "Mapping revision that produced this message"),
)


def setting(name, default=None, description=""):
    return Declaration(
        env="RESEARCH_" + name.upper(),
        required=False,
        default=default,
        description=description,
    )


class Research(Plugin):
    manifest = Manifest(
        name="research",
        version="0.1.0",
        default_interval_s=3600,
        channels=[
            Channel(
                name=RESULTS,
                description=(
                    "Research stream results: one message per stream, search and run "
                    "date, with the findings as text and cited URLs as fields and links. "
                    "Model output: generated, never evidence on its own."
                ),
                record_kind="derived",
                is_outside=True,
                fields=RESULT_FIELDS,
            ),
            Channel(
                name=ITEMS,
                description=(
                    "Structured items (such as events) found inside research results. "
                    "Record time is when the result was produced; subject time is when "
                    "the item happens."
                ),
                record_kind="derived",
                is_outside=True,
                fields=ITEM_FIELDS,
            ),
        ],
        settings={
            "source": setting(
                "source",
                "live",
                "live, or export:<file or directory of .jsonl exports>",
            ),
            "streams": setting(
                "streams", None, "Stream config file or directory (live mode)"
            ),
            "since": setting("since", None, "Earliest run date to map, YYYY-MM-DD"),
            "until": setting("until", None, "Latest run date to map, YYYY-MM-DD"),
            "batch": setting("batch", "25", "Raw records per checkpoint"),
            "date": setting("date", None, "Run date override for live runs"),
            "cache_dir": setting(
                "cache_dir", None, "Per-call result cache; resumed runs reuse it"
            ),
            "allow_model_calls": setting(
                "allow_model_calls", "0", "1 enables paid model calls; off by default"
            ),
            "max_calls_per_run": setting(
                "max_calls_per_run", "10", "Model calls allowed in one run"
            ),
            "max_calls_per_day": setting(
                "max_calls_per_day", "80", "Model calls allowed per UTC day"
            ),
            "min_interval_s": setting(
                "min_interval_s", "1", "Minimum seconds between model calls"
            ),
            "timeout_s": setting("timeout_s", "150", "Model call timeout in seconds"),
            "base_url": Declaration(
                env="RESEARCH_OPENAI_BASE_URL",
                required=False,
                description="OpenAI-compatible base URL, for example https://host/v1",
            ),
            "model": Declaration(
                env="RESEARCH_OPENAI_MODEL",
                required=False,
                description="Model name (a stream provider may override it)",
            ),
            "web_search": Declaration(
                env="RESEARCH_OPENAI_WEB_SEARCH",
                required=False,
                description="Override web search mode: none, options, plugin or tool",
            ),
        },
        secrets={
            "api_key": Declaration(
                env="RESEARCH_OPENAI_API_KEY",
                required=False,
                description="API key for the OpenAI-compatible endpoint (header only)",
            )
        },
        link_types=["cites", "found_in", "about"],
    )

    def options(self):
        s = self.settings
        options = fetch.Options.from_env(
            {},
            allow_model_calls=fetch.truthy(s.get("allow_model_calls")),
            base_url=s.get("base_url") or None,
            model=s.get("model") or None,
            web_search=s.get("web_search") or None,
            max_calls_per_run=int(s.get("max_calls_per_run") or 10),
            max_calls_per_day=int(s.get("max_calls_per_day") or 80),
            min_interval_s=float(s.get("min_interval_s") or 1),
            timeout_s=float(s.get("timeout_s") or 150),
            cache_dir=s.get("cache_dir") or None,
            run_date=fetch.parse_day(s.get("date"), "RESEARCH_DATE"),
            since=fetch.parse_day(s.get("since"), "RESEARCH_SINCE"),
            until=fetch.parse_day(s.get("until"), "RESEARCH_UNTIL"),
            log=log.info,
        )
        options.api_key = self.secrets.get("api_key") or None
        return options

    def in_bounds(self, raw):
        options = self.bounds
        day = fetch.parse_day(raw.get("run_date"), "run_date")
        return day is not None and fetch.in_bounds(day, options)

    dry_run = False

    def execute(self, client, mode, dry_run=False):
        # A dry run starts from an empty cursor, so it would also start from an
        # empty call ledger; it must never spend on model calls.
        self.dry_run = dry_run
        try:
            return super().execute(client, mode, dry_run)
        finally:
            self.dry_run = False

    def map(self, raw):
        if not isinstance(raw, dict) or raw.get("kind") != fetch.RECORD_KIND:
            return
        if not raw.get("id") or not raw.get("completed_at") or not self.in_bounds(raw):
            return
        for kind, data in mapping.result_records(raw):
            if kind == "message":
                yield data
            elif kind == "link":
                yield Record(kind="link", data=data)
            else:
                yield provenance(data["derived"], data["sources"])

    def records(self, cursor, mode):
        self.bounds = fetch.Options(
            since=fetch.parse_day(self.settings.get("since"), "RESEARCH_SINCE"),
            until=fetch.parse_day(self.settings.get("until"), "RESEARCH_UNTIL"),
        )
        batch = int(self.settings.get("batch") or 25)
        if batch < 1:
            raise ValueError("RESEARCH_BATCH must be at least 1")
        source = (self.settings.get("source") or "live").strip()
        if source == "live":
            yield from self.live(cursor, mode, batch)
        elif source.startswith("export:"):
            yield from self.exported(cursor, Path(source[len("export:") :]), batch)
        else:
            raise ValueError("RESEARCH_SOURCE must be live or export:<path>")

    def live(self, cursor, mode, batch):
        if not self.settings.get("streams"):
            raise ValueError("RESEARCH_STREAMS is required in live mode")
        streams = fetch.load_streams(self.settings["streams"])
        state = copy.deepcopy(cursor.get("live") or {})
        stored = {"mapping_revision": MAPPING_REVISION, "live": state}
        options = self.options()
        if self.dry_run and options.allow_model_calls:
            log.info("dry run: model calls are off; imports still run")
            options.allow_model_calls = False
        count = 0
        try:
            for raw in fetch.collect(streams, options, state, mode=mode):
                yield from self.map(raw)
                count += 1
                if count % batch == 0:
                    yield Checkpoint(cursor=copy.deepcopy(stored))
        except fetch.FetchError:
            # Raised only by the provider call (401/403), after every earlier
            # result was mapped: keep those results and the call ledger so the
            # next run neither repeats nor forgets paid calls.
            yield Checkpoint(cursor=copy.deepcopy(stored))
            raise
        failures = options.call_failures
        if failures and len(failures) < options.calls_attempted:
            # Some calls failed (they stay due): report what this run missed.
            by_stream = {}
            for name in failures:
                by_stream.setdefault(name.split("/", 1)[0], []).append(name)
            for stream_id, searches in sorted(by_stream.items()):
                yield coverage(
                    channel=RESULTS,
                    scope_key=stream_id,
                    status="partial",
                    reasons=["source_error"],
                    detail={"failed_searches": searches},
                )
        yield Checkpoint(cursor=copy.deepcopy(stored))
        if failures and len(failures) == options.calls_attempted:
            # Every provider call failed: an outage, not a quiet run.
            raise fetch.FetchError(
                f"every provider call failed ({len(failures)}); they stay due"
            )

    def exported(self, cursor, path, batch):
        """Map export files in name order, resuming at the line after the last
        checkpoint. A trailing line without a newline (a file still being
        written) is left for the next run."""
        if path.is_dir():
            files = sorted(
                p for p in path.iterdir() if p.suffix == ".jsonl" and p.is_file()
            )
        elif path.is_file():
            files = [path]
        else:
            raise ValueError("RESEARCH_SOURCE export path does not exist")
        files_done = dict(cursor.get("files") or {})
        if cursor.get("mapping_revision") not in (None, MAPPING_REVISION):
            # A new mapping re-maps every export line once; unchanged output
            # is a no-op at ingest, changed output becomes one new version.
            files_done = {}
        state = {"mapping_revision": MAPPING_REVISION, "files": files_done}
        count = 0
        for file in files:
            entry = state["files"].get(file.name)
            if isinstance(entry, dict):
                done, expected = int(entry.get("lines") or 0), entry.get("sha256")
            else:
                done, expected = int(entry or 0), None
            with file.open("r", encoding="utf-8", newline="") as handle:
                lines = handle.readlines()
            # The cursor keeps a hash of the lines already mapped. A file that
            # was rewritten under the same name (a rerun export, a new backfill)
            # no longer matches it and is mapped again from the start; unchanged
            # lines are no-ops at ingest.
            prefix = hashlib.sha256()
            if done > len(lines):
                done = 0
            for line in lines[:done]:
                prefix.update(line.encode("utf-8"))
            if expected and prefix.hexdigest() != expected:
                done, prefix = 0, hashlib.sha256()
            for number in range(done, len(lines)):
                line = lines[number]
                if not line.endswith("\n"):
                    break
                prefix.update(line.encode("utf-8"))
                if line.strip():
                    try:
                        raw = json.loads(line)
                    except ValueError:
                        raise ValueError(
                            f"{file.name} line {number + 1} is not valid JSON"
                        ) from None
                    yield from self.map(raw)
                state["files"][file.name] = {
                    "lines": number + 1,
                    "sha256": prefix.hexdigest(),
                }
                count += 1
                if count % batch == 0:
                    yield Checkpoint(cursor=copy.deepcopy(state))
        yield Checkpoint(cursor=state)


def main():
    run(Research)
