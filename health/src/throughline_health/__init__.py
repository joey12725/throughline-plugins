"""Throughline health: Oura v2 daily summaries, sleep, sessions, workouts and
heart rate, and Weight Gurus scale weigh-ins.

Two layers: fetch.py (standard library only) reads the services and writes raw
JSONL; this plugin maps raw records to messages, either fetching live in
process or reading export files (HEALTH_SOURCE=export:<file or directory>).
"""

import copy
import logging
import time
from datetime import date, timedelta

from throughline_sdk import (
    Channel,
    Checkpoint,
    Declaration,
    Manifest,
    Plugin,
    coverage,
    run,
)

from . import fetch, mapping, sources
from .mapping import MAPPING_REVISION

log = logging.getLogger(__name__)

DESCRIPTIONS = {
    mapping.DAILY: "One Oura summary per day: sleep, readiness, activity, stress, SpO2, resilience and fitness scores and metrics; subject time is the day",
    mapping.SLEEP: "Oura sleep periods (nights and naps) from bedtime start to end, with stages, heart rate and HRV",
    mapping.SESSION: "Oura sessions (breathing, meditation, rest, naps started in the app)",
    mapping.WORKOUT: "Oura workouts, detected or logged",
    mapping.HEARTRATE: "Oura heart rate samples summarised per UTC day; the samples are kept in the payload",
    mapping.WEIGHT: "Weight Gurus smart scale weigh-ins with body composition; deletions are new versions",
}


def setting(env, default=None, description="", required=False):
    return Declaration(
        env=env, default=default, description=description, required=required
    )


class Health(Plugin):
    manifest = Manifest(
        name="health",
        version="0.1.0",
        default_interval_s=3600,
        channels=[
            Channel(
                name=name, description=DESCRIPTIONS[name], fields=mapping.FIELDS[name]
            )
            for name in mapping.CHANNELS
        ],
        settings={
            "source": setting(
                "HEALTH_SOURCE",
                "live",
                "live, or export:<file or directory of fetch.py JSONL>",
            ),
            "providers": setting(
                "HEALTH_PROVIDERS",
                "auto",
                "auto (whatever has credentials) or a comma list of oura,weightgurus",
            ),
            "collections": setting(
                "HEALTH_OURA_COLLECTIONS",
                ",".join(fetch.OURA_COLLECTIONS),
                "Oura collections to read",
            ),
            "backfill_start": setting(
                "HEALTH_BACKFILL_START",
                fetch.DEFAULT_START,
                "First day for backfill and for sync on an empty cursor (YYYY-MM-DD)",
            ),
            "backfill_end": setting(
                "HEALTH_BACKFILL_END",
                None,
                "Last day for backfill (YYYY-MM-DD); empty means today",
            ),
            "overlap_days": setting(
                "HEALTH_SYNC_OVERLAP_DAYS",
                "3",
                "Days re-read before the last synced day, for late Oura updates",
            ),
            "window_days": setting(
                "HEALTH_WINDOW_DAYS",
                "60",
                "Days per Oura fetch window (one checkpoint per window)",
            ),
            "max_requests_per_run": setting(
                "HEALTH_MAX_REQUESTS_PER_RUN",
                "1500",
                "Source HTTP requests allowed per run",
            ),
            "max_requests_per_day": setting(
                "HEALTH_MAX_REQUESTS_PER_DAY",
                "6000",
                "Source HTTP requests allowed per local day (kept in the cursor)",
            ),
            "min_interval_s": setting(
                "HEALTH_MIN_INTERVAL_S",
                "0.2",
                "Minimum seconds between source requests",
            ),
            "retries": setting(
                "HEALTH_RETRIES", "5", "Retries for 429, 5xx and transport failures"
            ),
            "backoff_s": setting(
                "HEALTH_BACKOFF_S",
                "1",
                "First retry delay in seconds, doubled per retry (max 60)",
            ),
            "weightgurus_every_h": setting(
                "HEALTH_WEIGHTGURUS_EVERY_H",
                "6",
                "Hours between Weight Gurus logins during sync",
            ),
            "weightgurus_unit": setting(
                "WEIGHTGURUS_UNIT",
                "lb",
                "Unit of Weight Gurus weight values (lb or kg) when an operation has none",
            ),
            "weightgurus_base": setting(
                "WEIGHTGURUS_API_BASE",
                fetch.WEIGHTGURUS_BASE,
                "Weight Gurus API base URL",
            ),
            "oura_base": setting("OURA_API_BASE", fetch.OURA_BASE, "Oura API base URL"),
            "oura_token_file": setting(
                "OURA_TOKEN_FILE",
                None,
                "File holding the Oura access token (plain text or JSON with access_token); re-read every run",
            ),
            "oura_allow_refresh": setting(
                "OURA_ALLOW_REFRESH",
                "0",
                "1 lets this plugin refresh Oura tokens with its own client; never set it when another app owns the refresh token",
            ),
            "oura_token_url": setting(
                "OURA_TOKEN_URL",
                fetch.OURA_TOKEN_URL,
                "Oura OAuth token endpoint for refresh",
            ),
        },
        secrets={
            "oura_access_token": setting(
                "OURA_ACCESS_TOKEN", None, "Oura access token or personal access token"
            ),
            "oura_refresh_token": setting(
                "OURA_REFRESH_TOKEN",
                None,
                "Oura refresh token; used only with OURA_ALLOW_REFRESH=1",
            ),
            "oura_client_id": setting(
                "OURA_CLIENT_ID",
                None,
                "This installation's own Oura OAuth client ID (refresh only)",
            ),
            "oura_client_secret": setting(
                "OURA_CLIENT_SECRET",
                None,
                "This installation's own Oura OAuth client secret (refresh only)",
            ),
            "weightgurus_email": setting(
                "WEIGHTGURUS_EMAIL", None, "Weight Gurus account email"
            ),
            "weightgurus_password": setting(
                "WEIGHTGURUS_PASSWORD", None, "Weight Gurus account password"
            ),
        },
        link_types=[],
    )

    def __init__(self):
        super().__init__()
        s = self.settings
        source = (s["source"] or "live").strip()
        if source != "live" and not source.startswith("export:"):
            raise ValueError("HEALTH_SOURCE must be live or export:<path>")
        self.source = source
        self.collections = fetch.parse_list(
            s["collections"], fetch.OURA_COLLECTIONS, "HEALTH_OURA_COLLECTIONS"
        )
        self.backfill_start = date.fromisoformat(s["backfill_start"])
        self.backfill_end = (
            date.fromisoformat(s["backfill_end"]) if s.get("backfill_end") else None
        )
        self.unit = (s["weightgurus_unit"] or "lb").strip().lower()
        if self.unit not in ("lb", "kg"):
            raise ValueError("WEIGHTGURUS_UNIT must be lb or kg")
        self.numbers = {}
        for key in (
            "overlap_days",
            "window_days",
            "max_requests_per_run",
            "max_requests_per_day",
            "retries",
            "weightgurus_every_h",
        ):
            value = int(s[key])
            if value < 0 or (key == "window_days" and value < 1):
                raise ValueError(
                    f"{self.manifest.settings[key].env} must be a positive integer"
                )
            self.numbers[key] = value
        self.min_interval = float(s["min_interval_s"])
        self.backoff = float(s["backoff_s"])

    def env(self):
        """Settings and secrets under their environment names, for fetch.py."""
        out = {}
        for values, declarations in (
            (self.settings, self.manifest.settings),
            (self.secrets, self.manifest.secrets),
        ):
            for name, declaration in declarations.items():
                if values.get(name) is not None:
                    out[declaration.env] = values[name]
        return out

    def records(self, cursor, mode):
        state = copy.deepcopy(cursor or {})
        if self.source.startswith("export:"):
            raws = sources.read_export(self.source[len("export:") :])
            yield from emit_buckets(
                mapping.map_records(raws, unit=self.unit), state, "export"
            )
            return
        yield from self.live(state, mode)

    def live(self, state, mode, today=None):
        n = self.numbers
        today = today or date.today()
        day = today.isoformat()
        spent = state.get("requests") or {}
        used = spent.get("used", 0) if spent.get("day") == day else 0
        budget = max(
            0, min(n["max_requests_per_run"], n["max_requests_per_day"] - used)
        )
        http = fetch.Http(
            max_requests=budget,
            min_interval=self.min_interval,
            retries=n["retries"],
            backoff=self.backoff,
        )
        env = self.env()
        auth = fetch.OuraAuth(env, http)
        providers = fetch.providers_from(self.settings["providers"], env, auth)
        if not providers:
            raise ValueError(
                "no health source configured: set OURA_ACCESS_TOKEN or OURA_TOKEN_FILE, "
                "and/or WEIGHTGURUS_EMAIL and WEIGHTGURUS_PASSWORD"
            )

        def spend():
            state["requests"] = {"day": day, "used": used + http.count}

        deferred = None
        # Oura collections refused (403/404) this run: {collection: first day
        # of the first complete window it was missing from}, and the last day
        # of the last complete window. The shared window still advances, so
        # the days in between are reported as not delivered.
        skipped_from, covered_to = {}, None
        try:
            last = state.get("weightgurus_at") or 0
            due = (
                mode == "backfill"
                or time.time() - last >= n["weightgurus_every_h"] * 3600
            )
            if "weightgurus" in providers and due:
                try:
                    raws = fetch.weightgurus_records(env, http)
                except fetch.BudgetExhausted:
                    raise
                except fetch.FetchError as error:
                    # Do not let the unofficial scale API block Oura; the run
                    # still fails at the end so the error is reported.
                    log.warning("health: Weight Gurus failed: %s", error)
                    deferred = error
                    raws = None
                spend()
                if raws is not None:
                    yield from emit_buckets(
                        mapping.map_records(raws, unit=self.unit),
                        state,
                        "weightgurus",
                    )
                    state["weightgurus_at"] = int(time.time())
            if "oura" in providers:
                oura = fetch.Oura(
                    http, auth, env.get("OURA_API_BASE") or fetch.OURA_BASE
                )
                overlap = timedelta(days=n["overlap_days"])
                if mode == "backfill":
                    bounds = [
                        self.backfill_start.isoformat(),
                        self.backfill_end.isoformat() if self.backfill_end else None,
                    ]
                    progress = state.get("backfill")
                    if not progress or progress.get("bounds") != bounds:
                        progress = {"bounds": bounds, "next": bounds[0]}
                        start = self.backfill_start
                    else:
                        start = max(
                            self.backfill_start,
                            date.fromisoformat(progress["next"]) - overlap,
                        )
                    end = self.backfill_end or today
                else:
                    progress = None
                    following = state.get("oura_next")
                    start = (
                        date.fromisoformat(following) - overlap
                        if following
                        else self.backfill_start
                    )
                    end = today
                skipped = set()
                for low, high in fetch.windows(start, end, n["window_days"]):
                    raws = fetch.oura_window(oura, self.collections, low, high, skipped)
                    yield from mapping.map_records(raws, unit=self.unit)
                    for collection in skipped:
                        skipped_from.setdefault(collection, low)
                    covered_to = high
                    following = (high + timedelta(days=1)).isoformat()
                    if progress is not None:
                        progress["next"] = following
                        state["backfill"] = progress
                    if (
                        progress is None
                        or self.backfill_end is None
                        or self.backfill_end >= today
                    ):
                        state["oura_next"] = max(
                            state.get("oura_next") or "", following
                        )
                    spend()
                    yield Checkpoint(cursor=copy.deepcopy(state))
        except fetch.BudgetExhausted:
            log.warning(
                "health: request limit reached; the next run continues after the last complete window"
            )
        for collection, low in sorted(skipped_from.items()):
            yield coverage(
                channel=collection_channel(collection),
                scope_key=collection,
                status="partial",
                reasons=["source_error"],
                gaps=[
                    {
                        "from": f"{low.isoformat()}T00:00:00+00:00",
                        "to": f"{(covered_to + timedelta(days=1)).isoformat()}T00:00:00+00:00",
                        "reason": "source_error",
                    }
                ],
                detail={"skipped": "Oura refused the collection (HTTP 403/404)"},
            )
        spend()
        yield Checkpoint(cursor=state)
        if deferred is not None:
            raise deferred


def collection_channel(collection):
    if collection in fetch.OURA_DAILY:
        return mapping.DAILY
    return {
        "sleep": mapping.SLEEP,
        "session": mapping.SESSION,
        "workout": mapping.WORKOUT,
        "heartrate": mapping.HEARTRATE,
    }[collection]


def emit_buckets(messages, state, key):
    """Send only (channel, month) groups whose content changed since last run.

    For complete data sets (export files, the full Weight Gurus history) this
    keeps reruns cheap; a checkpoint follows every sent group, so a crash
    resumes at the first group not yet acknowledged.
    """
    stored = state.get(key) or {}
    if stored.get("revision") != MAPPING_REVISION:
        stored = {"revision": MAPPING_REVISION, "buckets": {}}
    groups = {}
    for item in messages:
        groups.setdefault(f"{item['channel']}|{item['happened_at'][:7]}", []).append(
            item
        )
    for bucket in sorted(groups):
        signature = mapping.digest(groups[bucket])
        if stored["buckets"].get(bucket) == signature:
            continue
        yield from groups[bucket]
        stored["buckets"][bucket] = signature
        state[key] = stored
        yield Checkpoint(cursor=copy.deepcopy(state))
    state[key] = stored
    yield Checkpoint(cursor=copy.deepcopy(state))


def main():
    run(Health)
