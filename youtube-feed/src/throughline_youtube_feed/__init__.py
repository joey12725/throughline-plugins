"""YouTube subscription feed and router decisions for Throughline.

Reads raw records live (fetch.py in-process) or from export files written by
`python fetch.py export` on another host, and maps them to youtube.uploads and
youtube.picks. See README.md.
"""

import copy
import logging
import os
from datetime import UTC, datetime

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

DESCRIPTIONS = {
    "channels": "Channel list files (OPML, JSON, CSV or text), separated by the OS path separator",
    "channel_ids": "Comma-separated channel IDs (UC...) in addition to the files",
    "transport": "rss (public channel feeds), page (public channel videos page) or auto (rss, then page)",
    "rss_url": "Feed URL template with {channel_id}",
    "web_url": "YouTube web origin for pages",
    "api_url": "YouTube Data API base URL (playlist writes only)",
    "token_url": "Google OAuth token endpoint (playlist writes only)",
    "user_agent": "User-Agent for YouTube requests",
    "delay_s": "Minimum seconds between YouTube requests",
    "timeout_s": "Per-request timeout in seconds",
    "retries": "Retries on 429, 5xx and transport errors (exponential backoff)",
    "backoff_s": "First backoff in seconds (doubles per retry; Retry-After honoured)",
    "channels_per_run": "Channels read per run, rotating through the list (0 = all)",
    "requests_per_day": "Daily cap on YouTube requests including retries (0 = none)",
    "details": "1 to fetch each new video's watch page once (duration, description, exact date)",
    "details_per_run": "Watch pages fetched per run",
    "check_shorts": "1 to check unknown Shorts status with one HEAD request per new video",
    "seen_days": "Days a seen video is remembered for incremental fetches",
    "router": "1 to score new uploads with an OpenAI-compatible model (paid; off by default)",
    "router_url": "OpenAI-compatible base URL (ending in /v1) for the router",
    "router_model": "Router model name",
    "router_prompt": "Path to a system prompt file (default: built-in prompt)",
    "router_json_mode": "1 to send response_format json_object",
    "router_per_run": "Videos scored per run",
    "router_per_day": "Videos scored per UTC day",
    "router_max_age_h": "Only score (and add) videos published or picked within this many hours",
    "router_skip_shorts": "1 to never score Shorts",
    "router_max_tokens": "max_tokens for router answers",
    "thresholds": "Verdict thresholds: JSON object or file (min_watch, min_progress, max_junk)",
    "cache_dir": "Directory caching router answers so a retried run does not pay twice",
    "playlist": "Playlist ID, WL or your playlist title for picked videos (unset: no action)",
    "playlist_apply": "1 to really add to the playlist; otherwise a dry run that only reports",
    "playlist_per_day": "Playlist inserts per UTC day (each costs 50 API quota units)",
    "client_id": "Google OAuth client ID (with client secret and refresh token)",
    "import_scorer": "Scorer name given to imported youtube-notify predictions",
}
SECRET_DESCRIPTIONS = {
    "router_api_key": "Router API key (Bearer)",
    "oauth_token": "YouTube OAuth access token for playlist writes",
    "client_secret": "Google OAuth client secret",
    "refresh_token": "Google OAuth refresh token (youtube scope)",
}
PLUGIN_SETTINGS = {
    "source": (
        "live",
        "live, or export:<file or directory>[<pathsep>more] of fetch.py exports",
    ),
    "since": (None, "Only uploads published (picks scored) at or after this ISO time"),
    "until": (None, "Only uploads published (picks scored) before this ISO time"),
    "batch": ("500", "Messages per checkpointed batch"),
    "archive": (None, "Live mode: append fetched raw records to this JSONL file"),
}


def env_name(key):
    return fetch.PREFIX + key.upper()


class YouTubeFeed(Plugin):
    manifest = Manifest(
        name="youtube_feed",
        version="0.1.0",
        channels=[
            Channel(
                name=mapping.UPLOADS,
                description="New uploads of followed YouTube channels from their public feeds, at the published time; outside-world events, threaded per channel",
                is_outside=True,
                location_tolerance_s=0,
                fields=mapping.UPLOAD_FIELDS,
            ),
            Channel(
                name=mapping.PICKS,
                description="Router decisions on feed uploads (scores, verdict, reason, model), at the scoring time with the video's published time as subject",
                record_kind="derived",
                provenance_link_types=["pick_of"],
                fields=mapping.PICK_FIELDS,
            ),
        ],
        settings={
            **{
                k: Declaration(
                    env=env_name(k),
                    required=False,
                    default=d,
                    description=DESCRIPTIONS[k],
                )
                for k, d in fetch.DEFAULTS.items()
            },
            **{
                k: Declaration(
                    env=env_name(k), required=False, default=d, description=desc
                )
                for k, (d, desc) in PLUGIN_SETTINGS.items()
            },
        },
        secrets={
            k: Declaration(
                env=env_name(k), required=False, description=SECRET_DESCRIPTIONS[k]
            )
            for k in fetch.SECRETS
        },
        default_interval_s=1800,
        link_types=["upload_of", "published_by", "pick_of"],
    )

    def __init__(self):
        super().__init__()
        self.dry_run = False
        self.stats = {}
        source = (self.settings["source"] or "").strip()
        if source != "live" and not source.startswith("export:"):
            raise ValueError("YOUTUBE_FEED_SOURCE must be live or export:<path>")
        self.bounds = [self._bound("since"), self._bound("until")]

    def _bound(self, name):
        value = self.settings[name]
        if not value:
            return None
        moment = fetch.parse_time(value)
        if moment is None:
            raise ValueError(f"{env_name(name)} must be an ISO date or time")
        return moment

    def execute(self, client, mode, dry_run=False):
        # A dry run never calls the paid router or writes to a playlist.
        self.dry_run = dry_run
        report = super().execute(client, mode, dry_run)
        if self.stats:
            report["feed"] = dict(sorted(self.stats.items()))
        return report

    def config(self):
        env = {env_name(k): v for k, v in self.settings.items() if v is not None}
        env.update({env_name(k): v for k, v in self.secrets.items() if v})
        if self.dry_run:
            env[env_name("router")] = "0"
            env[env_name("playlist_apply")] = "0"
        return fetch.config(env)

    def raw_records(self, state):
        """Raw records and, in live mode, the new fetch state."""
        source = self.settings["source"].strip()
        cfg = self.config()
        if source.startswith("export:"):
            paths = [p for p in source[len("export:") :].split(os.pathsep) if p.strip()]
            for path in paths:
                if not os.path.exists(path):
                    raise ValueError("export path does not exist")
            return fetch.read_records(paths, cfg["import_scorer"]), None
        if (
            not (cfg["channels"] or "").strip()
            and not (cfg["channel_ids"] or "").strip()
        ):
            raise ValueError(
                "live mode needs YOUTUBE_FEED_CHANNELS or YOUTUBE_FEED_CHANNEL_IDS"
            )
        fetch.check_playlist(cfg)  # before any paid router call
        fetch_state = copy.deepcopy(state.get("fetch", {}))
        http = fetch.Http(cfg, fetch_state)
        records, stats = fetch.collect(cfg, fetch_state, http)
        picks, route_stats = fetch.route(cfg, fetch_state, records, http)
        adds, add_stats = fetch.playlist(cfg, fetch_state, picks, http)
        records = records + picks + adds
        stats.update(route_stats)
        stats.update(add_stats)
        stats.update(http.stats)
        self.stats.update(stats)
        if self.settings["archive"] and not self.dry_run and records:
            fetch.write_records(self.settings["archive"], records, append=True)
        return records, fetch_state

    def in_bounds(self, at):
        moment = fetch.parse_time(at)
        since, until = self.bounds
        return (
            moment is not None
            and (not since or moment >= since)
            and (not until or moment < until)
        )

    def records(self, cursor, mode):
        self.stats = {}
        state = copy.deepcopy(cursor)
        state.pop("observation_at", None)
        sent_uploads = state.setdefault("uploads", {})
        sent_picks = state.setdefault("picks", {})
        raw, fetch_state = self.raw_records(state)
        limits = fetch.thresholds(self.config())
        uploads = mapping.best_uploads(raw)
        groups, adds = {}, {}
        for record in raw:
            if record.get("kind") == "pick" and record.get("id"):
                groups.setdefault(record["id"], []).append(record)
            elif record.get("kind") == "playlist_add" and record.get("pick_id"):
                adds.setdefault(record["pick_id"], []).append(record)
        decisions = {}
        for pick_id in sorted(groups):
            decision = fetch.pick_decision(groups[pick_id], limits)
            if not decision.get("scored_at") and not (decision.get("video") or {}).get(
                "published_at"
            ):
                self.stats["skipped_picks"] = self.stats.get("skipped_picks", 0) + 1
                continue
            if self.in_bounds(
                decision.get("scored_at") or decision["video"]["published_at"]
            ):
                decisions[pick_id] = decision
        needed = set()
        for pick_id, decision in decisions.items():
            vid = decision["video"]["video_id"]
            needed.add(vid)
            if vid not in uploads:
                uploads[vid] = mapping.synthesized_upload(decision)

        planned = []
        for vid in sorted(uploads):
            record = uploads[vid]
            built = mapping.upload_message(record)
            if built is None:
                self.stats["skipped_uploads"] = self.stats.get("skipped_uploads", 0) + 1
                continue
            message, links = built
            if vid not in needed and not self.in_bounds(message["happened_at"]):
                continue
            rank = fetch.upload_rank(record)
            fingerprint = fetch.digest(message)
            previous = sent_uploads.get(vid)
            if previous and (
                rank < previous[0]
                or (rank == previous[0] and fingerprint == previous[1])
            ):
                continue
            planned.append((message, links, ("uploads", vid, [rank, fingerprint])))
        pending_uploads = {p[2][1] for p in planned}
        for pick_id, decision in sorted(decisions.items()):
            vid = decision["video"]["video_id"]
            linked = vid in sent_uploads or vid in pending_uploads
            built = mapping.pick_message(
                pick_id, decision, groups[pick_id], adds.get(pick_id, []), linked
            )
            if built is None:
                continue
            message, links = built
            fingerprint = fetch.digest([message, links])
            if sent_picks.get(pick_id) == fingerprint:
                continue
            planned.append((message, links, ("picks", pick_id, fingerprint)))

        # Uploads before picks (links need their target), each in time order.
        planned.sort(
            key=lambda p: (
                p[0]["channel"] != mapping.UPLOADS,
                p[0]["happened_at"],
                p[0]["external_id"],
            )
        )
        batch = max(1, int(self.settings["batch"] or 500))
        pending = 0
        for message, links, (table, key, value) in planned:
            yield message
            for link in links:
                yield Record(kind="link", data=link)
            state[table][key] = value
            pending += 1
            if pending >= batch:
                yield Checkpoint(cursor=copy.deepcopy(state))
                pending = 0
        failed = []
        if fetch_state is not None:
            # Live: report channels this run could not read or fully cover.
            failed = [r for r in raw if r.get("kind") == "feed_error"]
            for record in raw:
                if record.get("kind") == "feed_gap":
                    yield coverage(
                        channel=mapping.UPLOADS,
                        scope_key=record["channel_id"],
                        status="partial",
                        reasons=["source_cap"],
                        gaps=[
                            {
                                "from": record["from"],
                                "to": record["to"],
                                "reason": "source_cap",
                            }
                        ],
                        detail={"feed_entries": record.get("entries")},
                    )
            if failed and len(failed) < self.stats.get("channels_read", 0):
                for record in failed:
                    yield coverage(
                        channel=mapping.UPLOADS,
                        scope_key=record["channel_id"],
                        status="partial",
                        reasons=["source_error"],
                        detail={"status": record.get("status")},
                    )
            # Only now: a crash before this point fetches (and routes) again.
            state["fetch"] = fetch_state
            state["last_fetch_at"] = datetime.now(UTC).isoformat()
        self.stats["emitted"] = len(planned)
        if self.stats:
            logging.info("youtube_feed %s", dict(sorted(self.stats.items())))
        yield Checkpoint(cursor=state)
        if failed and len(failed) == self.stats.get("channels_read", 0):
            # Every channel read this run failed: an outage, not a quiet run.
            raise fetch.FetchError(f"every channel feed failed this run ({len(failed)})")


def main(argv=None):
    run(YouTubeFeed, argv)
