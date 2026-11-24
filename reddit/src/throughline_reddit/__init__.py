"""Reddit: the owner's account listings (saved, voted, submitted, comments).

Raw records come from ``fetch.py`` (standard library only), either run in this
process (``REDDIT_SOURCE=live``) or earlier on another host and shipped here as
JSONL (``REDDIT_SOURCE=export:<file or directory>``). The mapping is in
``mapping.py``. Only GET listing reads and the OAuth token endpoint (or, with
the session transport, a signed-in browser sign-in) are used: nothing is
posted, voted, saved or changed on Reddit.
"""

import copy
import gzip
import json
import os
from datetime import datetime, timezone
from pathlib import Path

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

from . import fetch, mapping
from .mapping import MAPPING_REVISION, field_declarations

CHANNEL_DESCRIPTIONS = {
    "reddit.saved": "Posts and comments the owner saved on Reddit (time first seen)",
    "reddit.votes": "Posts and comments the owner upvoted or downvoted (time first seen)",
    "reddit.posts": "Posts the owner submitted to Reddit",
    "reddit.comments": "Comments the owner wrote on Reddit",
}


def declaration(name, description, required=False):
    env, default = fetch.SETTINGS[name]
    return Declaration(
        env=env, description=description, required=required, default=default
    )


class Reddit(Plugin):
    manifest = Manifest(
        name="reddit",
        version="0.1.0",
        default_interval_s=3600,
        channels=[
            Channel(
                name=name,
                description=description,
                fields=field_declarations(),
                conversation_gap_s=1800,
            )
            for name, description in CHANNEL_DESCRIPTIONS.items()
        ],
        settings={
            "source": Declaration(
                env="REDDIT_SOURCE",
                default="live",
                description="live (run fetch.py here) or export:<JSONL file or directory>",
            ),
            "batch_size": Declaration(
                env="REDDIT_BATCH_SIZE",
                default="500",
                description="Messages per checkpoint, 1..5000",
            ),
            "archive_dir": Declaration(
                env="REDDIT_ARCHIVE_DIR",
                required=False,
                description="Live mode: also keep each run's raw JSONL in this directory",
            ),
            "user_agent": declaration(
                "user_agent",
                "Required for live mode: '<platform>:<app id>:<version> (by /u/<name>)'",
            ),
            "username": declaration(
                "username",
                "Reddit account name (password grant or session sign-in; else read from the account)",
            ),
            "listings": declaration(
                "listings",
                "Account listings: saved, upvoted, downvoted, submitted, comments or none",
            ),
            "transport": declaration(
                "transport", "Account listing transport: oauth or session (signed-in browser)"
            ),
            "session_profile": declaration(
                "session_profile",
                "Session transport: private persistent browser profile directory",
            ),
            "since": declaration("since", "Lower bound on item creation time (ISO)"),
            "until": declaration("until", "Upper bound on item creation time (ISO)"),
            "max_pages": declaration("max_pages", "Pages per account listing per run"),
            "page_size": declaration("page_size", "Items per listing page, 1..100"),
            "max_requests": declaration("max_requests", "Reddit HTTP requests per run"),
            "max_requests_per_day": declaration(
                "max_requests_per_day",
                "Reddit HTTP requests per UTC day (kept in the cursor)",
            ),
            "min_interval": declaration(
                "min_interval", "Seconds between Reddit requests"
            ),
            "retries": declaration(
                "retries", "Retries after 429, 5xx and transport errors"
            ),
            "max_wait": declaration(
                "max_wait", "Longest single wait in seconds before stopping"
            ),
            "timeout": declaration("timeout", "Seconds per request"),
            "api_base": declaration("api_base", "OAuth API base URL"),
            "auth_base": declaration("auth_base", "Token endpoint base URL"),
        },
        secrets={
            name: Declaration(env=env, required=False, description=description)
            for name, env, description in (
                ("client_id", "REDDIT_CLIENT_ID", "Reddit OAuth app client id"),
                (
                    "client_secret",
                    "REDDIT_CLIENT_SECRET",
                    "Reddit OAuth app secret (empty for installed apps)",
                ),
                (
                    "refresh_token",
                    "REDDIT_REFRESH_TOKEN",
                    "Permanent user refresh token",
                ),
                ("password", "REDDIT_PASSWORD", "Account password (script-app grant or session sign-in)"),
                (
                    "access_token",
                    "REDDIT_ACCESS_TOKEN",
                    "Ready bearer token (no refresh)",
                ),
            )
        },
        link_types=["saved", "voted", "represents", "posted_in", "reply_to"],
    )

    # Test hook: keyword options passed to fetch.Client (sleep, clock, opener).
    client_options = {}

    def bounded(self, name, low, high):
        try:
            value = int(self.settings.get(name) or 0)
        except ValueError:
            value = 0
        if not low <= value <= high:
            env = self.manifest.settings[name].env
            raise ValueError(f"{env} must be between {low} and {high}")
        return value

    def export_files(self, location):
        path = Path(location).expanduser()
        if path.is_file():
            return [path]
        if path.is_dir():
            return sorted(
                (
                    p
                    for p in path.rglob("*")
                    if p.is_file()
                    and (p.name.endswith(".jsonl") or p.name.endswith(".jsonl.gz"))
                ),
                key=lambda p: p.relative_to(path).as_posix(),
            )
        raise ValueError("REDDIT_SOURCE export path does not exist")

    def read_export(self, location):
        records = []
        for path in self.export_files(location):
            opener = gzip.open if path.name.endswith(".gz") else open
            with opener(path, "rt", encoding="utf-8") as handle:
                for number, line in enumerate(handle, 1):
                    if not line.strip():
                        continue
                    try:
                        records.append(json.loads(line))
                    except ValueError:
                        raise ValueError(
                            f"invalid JSON in export file {path.name} line {number}"
                        ) from None
        return records

    def fetch_live(self, fetch_state, mode):
        values = {name: self.settings.get(name) for name in fetch.SETTINGS}
        values["full"] = mode == "backfill"
        try:
            config = fetch.Config(values)
            if not config.user_agent and config.listings:
                raise fetch.FetchError("live mode needs REDDIT_USER_AGENT")
            exporter = fetch.Exporter(
                config,
                fetch.Credentials(**self.secrets),
                fetch_state,
                **self.client_options,
            )
        except fetch.FetchError as error:
            raise RuntimeError(f"Reddit fetch failed: {error}") from None
        try:
            records = list(exporter.records())
        except fetch.FetchError as error:
            # Requests were spent: hand back the state so the per-day count
            # is kept even though nothing else advances.
            return None, exporter.state, f"Reddit fetch failed: {error}"
        if self.settings.get("archive_dir"):
            folder = Path(self.settings["archive_dir"]).expanduser()
            folder.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            target = folder / f"reddit-{stamp}.jsonl"
            temporary = target.with_suffix(".part")
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                for record in records:
                    handle.write(
                        json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
                    )
            os.replace(temporary, target)
        return records, exporter.state, None

    @staticmethod
    def plan(records, items):
        """Collapse occurrences per item and keep only what changed.

        Each item becomes one message: the earliest occurrence carrying the
        item's latest significant content (so a rerun over more exports never
        reverts it), dated by the earliest time it was ever observed.
        """
        occurrences = {}
        for order, record in enumerate(records):
            item = mapping.item_of(record)
            if item is not None:
                occurrences.setdefault(item.key, []).append(
                    (item.observed_at, order, item)
                )
        plans = []
        for key, found in occurrences.items():
            found.sort(key=lambda x: (x[0], x[1]))
            final = found[-1][2].digest
            chosen = next(item for _, _, item in found if item.digest == final)
            first_seen = found[0][0]
            previous = items.get(key)
            if previous:
                remembered = mapping.parse_iso(previous[1])
                if remembered and remembered < first_seen:
                    first_seen = remembered
            first = mapping.utc(first_seen)
            if previous == [final, first]:
                continue
            message, links = chosen.build(first_seen)
            plans.append((key, [final, first], message, links))
        plans.sort(
            key=lambda p: (
                datetime.fromisoformat(p[2]["happened_at"]),
                p[2]["channel"],
                p[2]["external_id"],
            )
        )
        return plans

    def records(self, cursor, mode):
        batch = self.bounded("batch_size", 1, 5000)
        state = copy.deepcopy(cursor)
        items = state.setdefault("items", {})
        source = (self.settings.get("source") or "live").strip()
        fetch_state = None
        if source == "live":
            previous = state.get("fetch") or {}
            raw, fetch_state, failure = self.fetch_live(previous, mode)
            # Until the final checkpoint only the request usage moves on, so a
            # crash refetches (never skips) yet still counts toward the
            # per-day request limit.
            usage = copy.deepcopy(previous)
            if "requests" in fetch_state:
                usage["requests"] = copy.deepcopy(fetch_state["requests"])
            state["fetch"] = usage
            if failure:
                yield Checkpoint(cursor=copy.deepcopy(state))
                raise RuntimeError(failure)
        elif source.startswith("export:") and source[len("export:") :].strip():
            raw = self.read_export(source[len("export:") :].strip())
        else:
            raise ValueError("REDDIT_SOURCE must be live or export:<path>")
        emitted = 0
        for key, entry, message, links in self.plan(raw, items):
            yield message
            for link in links:
                yield Record(kind="link", data=link)
            items[key] = entry
            emitted += 1
            if emitted % batch == 0:
                # The fetch cursor stays at its old position until every
                # fetched item is acknowledged: a crash refetches, never skips.
                yield Checkpoint(cursor=copy.deepcopy(state))
        if fetch_state is not None:
            state["fetch"] = fetch_state
        summary = next(
            (r for r in raw if r.get("kind") == "reddit.export_summary"), None
        )
        # Export-mode fetches run beside the credentials and ship only raw
        # data here. Their summary still carries the capped-window coverage.
        if summary is not None:
            yield from self.coverage_reports(summary)
        state["mapping_revision"] = MAPPING_REVISION
        yield Checkpoint(cursor=state)

    @staticmethod
    def coverage_reports(summary):
        """Coverage for account listings a live run could not read in full."""
        reports = []
        for listing, counts in sorted((summary.get("listings") or {}).items()):
            if counts.get("gap"):
                reports.append(
                    coverage(
                        channel=mapping.CHANNELS[listing],
                        scope_key=listing,
                        gaps=[dict(counts["gap"], reason="source_cap")],
                        detail={"end": "max_pages"},
                        status="partial",
                        reasons=["source_cap"],
                    )
                )
        return reports


def main():
    run(Reddit)
