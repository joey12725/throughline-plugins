"""Public Instagram account feeds: posts and 24h stories of followed accounts.

Two layers. ``fetch.py`` (standard library only) reads the public web profile
API or public web profile/post HTML (and active 24h stories via
``/api/v1/feed/reels_media/`` when authenticated via
``INSTAGRAM_FEED_COOKIES_FILE``) and writes raw JSONL records; this plugin maps
them to ``instagram.feed`` messages, either live (calling the fetcher
in-process) or from export files
(``INSTAGRAM_FEED_SOURCE=export:<file or directory>``).
No media is downloaded; posts and stories are recorded as links and text.
"""

import copy
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from throughline_sdk import (
    Channel,
    Checkpoint,
    Declaration,
    Manifest,
    Plugin,
    coverage,
    run,
)

from . import fetch
from .mapping import CHANNEL, FIELDS, MAPPING_REVISION, map_post, map_record, map_story

__all__ = ["InstagramFeed", "MAPPING_REVISION", "main", "map_post", "map_record", "map_story"]

PREFIX = fetch.ENV_PREFIX
# Checkpoint an export at least this often even inside one account's lines.
EXPORT_CHECKPOINT_LINES = 500
LIVE_SETTINGS = [
    (name, default, description)
    for name, (default, description) in fetch.OPTIONS.items()
]


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _moment(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _bound(value, name):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(
            f"{PREFIX}{name.upper()} must be an ISO date or timestamp"
        ) from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class InstagramFeed(Plugin):
    manifest = Manifest(
        name="instagram_feed",
        version="0.2.0",
        default_interval_s=7200,
        channels=[
            Channel(
                name=CHANNEL,
                description=(
                    "Posts and 24h stories of followed public Instagram accounts "
                    "(image, video, carousel, reel, story): caption, stickers/links, "
                    "canonical URL and posted time; one thread per account"
                ),
                # Other people's posts/stories: outside material, excluded from
                # recall unless asked for. The owner's track position at posting
                # time says nothing about a post, so attach only on an exact
                # second (practically never).
                is_outside=True,
                location_tolerance_s=0,
                fields=FIELDS,
            )
        ],
        settings={
            "source": Declaration(
                env=PREFIX + "SOURCE",
                default="live",
                description="live, or export:<JSONL file or directory of *.jsonl files from fetch.py>",
            ),
            "since": Declaration(
                env=PREFIX + "SINCE",
                required=False,
                description="Skip posts and stories before this ISO date/time (backfill bound)",
            ),
            "until": Declaration(
                env=PREFIX + "UNTIL",
                required=False,
                description="Skip posts and stories at or after this ISO date/time (backfill bound)",
            ),
            "all": Declaration(
                env=PREFIX + "ALL",
                default="0",
                description="Live: 1 fetches every account each run instead of one rotating batch",
            ),
            **{
                name: Declaration(
                    env=PREFIX + name.upper(),
                    required=False,
                    default=default,
                    description="Live: " + description,
                )
                for name, default, description in LIVE_SETTINGS
            },
        },
        secrets={
            "cookies_file": Declaration(
                env=fetch.COOKIES_ENV,
                required=False,
                description="Optional cookies.txt or JSON cookie list path (required for 24h stories); values are never logged",
            )
        },
        link_types=[],
    )

    # Test seams: a fetch transport callable and a sleep function.
    transport = None
    sleep = None
    clock = None

    def records(self, cursor, mode):
        self.since = _bound(self.settings.get("since"), "since")
        self.until = _bound(self.settings.get("until"), "until")
        source = (self.settings.get("source") or "live").strip()
        self.emitted = {}
        if source == "live":
            yield from self.live(cursor)
        elif source.startswith("export:") and source[7:].strip():
            yield from self.export(cursor, Path(source[7:].strip()))
        else:
            raise ValueError(f"{PREFIX}SOURCE must be live or export:<path>")

    # ------------------------------------------------------------ mapping

    def map(self, record):
        """(message or None, conflict) for one raw record.

        ``conflict`` is True when this run already emitted a different
        version of the same post or story; the caller checkpoints first so two
        versions never share one ingest batch.
        """
        try:
            message = map_record(record)
        except ValueError as error:
            logging.warning(
                "instagram-feed: skipped a malformed %s record (%s)",
                record.get("kind") or "unknown",
                error,
            )
            return None, False
        if message is None:
            return None, False
        at = datetime.fromisoformat(message["happened_at"])
        if (self.since and at < self.since) or (self.until and at >= self.until):
            return None, False
        key = message["external_id"]
        digest = _digest(message)
        previous = self.emitted.get(key)
        if previous == digest:
            return None, False
        self.emitted[key] = digest
        return message, previous is not None

    # ------------------------------------------------------------ export

    @staticmethod
    def files(path):
        if path.is_dir():
            return sorted(
                (
                    p
                    for p in path.iterdir()
                    if p.is_file() and p.name.endswith(".jsonl")
                ),
                key=lambda p: p.name,
            )
        if path.is_file():
            return [path]
        raise ValueError("export source path does not exist")

    def export(self, cursor, path):
        state = copy.deepcopy(cursor.get("export") or {})
        for file in self.files(path):
            data = file.read_bytes()
            first = data.split(b"\n", 1)[0]
            head = hashlib.sha256(first).hexdigest()[:16]
            prior = state.get(file.name) or {}
            offset = prior.get("offset", 0) if prior.get("head") == head else 0
            if offset > len(data):
                offset = 0  # Replaced or truncated: replay (idempotent).
            pending = 0
            last_account = None
            lines = 0
            while True:
                end = data.find(b"\n", offset)
                if end < 0:
                    break  # Partial last line: a writer may still be appending.
                line = data[offset:end].strip()
                record = None
                if line:
                    try:
                        record = json.loads(line)
                    except ValueError:
                        logging.warning(
                            "instagram-feed: skipped an unreadable export line"
                        )
                account = record.get("account") if isinstance(record, dict) else None
                message, conflict = (
                    self.map(record) if isinstance(record, dict) else (None, False)
                )
                if pending and (
                    conflict
                    or account != last_account
                    or lines >= EXPORT_CHECKPOINT_LINES
                ):
                    state[file.name] = {"offset": offset, "head": head}
                    yield Checkpoint(cursor={"export": copy.deepcopy(state)})
                    pending = lines = 0
                last_account = account
                lines += 1
                if message is not None:
                    yield message
                    pending += 1
                offset = end + 1
            state[file.name] = {"offset": offset, "head": head}
            yield Checkpoint(cursor={"export": copy.deepcopy(state)})

    # ------------------------------------------------------------ live

    def live(self, cursor):
        state = copy.deepcopy(cursor.get("live") or {})
        accounts = fetch.parse_accounts(
            self.settings.get("accounts"), self.settings.get("accounts_file")
        )
        if not accounts:
            raise ValueError(
                f"live mode needs {PREFIX}ACCOUNTS or {PREFIX}ACCOUNTS_FILE"
            )
        options = {name: self.settings.get(name) for name, _, _ in LIVE_SETTINGS}
        seams = {
            k: v
            for k, v in (
                ("transport", self.transport),
                ("sleep", self.sleep),
                ("clock", self.clock),
            )
            if v is not None
        }
        fetcher = fetch.Fetcher(
            accounts,
            state,
            options,
            cookies_file=self.secrets.get("cookies_file") or None,
            **seams,
        )
        committed = copy.deepcopy(state)
        everyone = str(self.settings.get("all") or "0").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        for handle, records in fetcher.run(select_all=everyone):
            for record in records:
                message, conflict = self.map(record)
                if conflict:
                    yield Checkpoint(cursor={"live": copy.deepcopy(committed)})
                if message is not None:
                    yield message
            if handle in fetcher.capped:
                # A full page newer than the stored newest post: what lies in
                # between is beyond the endpoint's newest-12 window.
                start, end = (_moment(t) for t in fetcher.capped[handle])
                yield coverage(
                    channel=CHANNEL,
                    scope_key=handle,
                    status="partial",
                    reasons=["source_cap"],
                    gaps=[{"from": start, "to": end, "reason": "source_cap"}],
                    detail={"posts_per_fetch": fetcher.posts},
                )
            committed = copy.deepcopy(state)
            yield Checkpoint(cursor={"live": committed})
        attempted = fetcher.summary["accounts"]
        failed = fetcher.failed
        if failed and len(failed) < attempted:
            for handle in failed:
                yield coverage(
                    channel=CHANNEL,
                    scope_key=handle,
                    status="partial",
                    reasons=["source_error"],
                    detail={
                        "status": (state["accounts"].get(handle) or {}).get("status")
                    },
                )
        # Persist the cool-down, strikes and request count even when nothing
        # was fetched (for example after a 429 on the first account).
        yield Checkpoint(cursor={"live": copy.deepcopy(state)})
        summary = dict(fetcher.summary)
        if summary.get("stories_auth_required"):
            logging.info(
                "instagram-feed: 24h stories require authenticated Instagram cookies "
                "(set %s); posts fetched anonymously",
                fetch.COOKIES_ENV,
            )
        logging.info("instagram-feed live run: %s", json.dumps(summary, sort_keys=True))
        if failed and len(failed) == attempted:
            # Every account fetched this run failed (a cool-down or the daily
            # budget is not a failure): an outage, not a quiet run.
            raise fetch.FetchError(
                f"every account fetch failed this run ({len(failed)})"
            )


def main():
    run(InstagramFeed)
