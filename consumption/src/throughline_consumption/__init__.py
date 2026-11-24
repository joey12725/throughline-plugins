"""Unified media consumption: Spotify, Plex, Audiobookshelf and YouTube.

Two layers. `fetch.py` (standard library only) reads the services and writes
raw JSONL records; it runs on whatever host holds the credentials. This
plugin maps those records to `media.consumption` messages, either live
(running the fetcher in process) or from export files shipped back:
CONSUMPTION_SOURCE=live | export:<file or directory>.
"""

import copy
import gzip
import hashlib
import json
import logging
from datetime import datetime
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

from . import fetch
from .mapping import (
    BOOK_TEXT_CHANNEL,
    BOOK_TEXT_FIELDS,
    CHANNEL,
    FIELDS,
    LINK_TYPES,
    MUTABLE,
    day_thread,
    map_record,
)

log = logging.getLogger(__name__)


def declarations(secret):
    return {
        key: Declaration(
            env=name, description=description, required=False, default=default
        )
        for key, (name, default, is_secret, description) in fetch.ENV.items()
        if is_secret == secret
    }


def export_files(path):
    root = Path(path)
    if root.is_file():
        return [root]
    if root.is_dir():
        files = [
            p
            for p in root.rglob("*")
            if p.is_file() and p.name.endswith((".jsonl", ".jsonl.gz"))
        ]
        # Export names start with their fetch time, so name order is time order.
        return sorted(files, key=lambda p: (p.name, str(p)))
    raise ValueError("CONSUMPTION_SOURCE export path does not exist")


def read_export(path):
    """Header, records and footer of one export file; incomplete files are refused."""
    data = path.read_bytes()
    if path.name.endswith(".gz"):
        data = gzip.decompress(data)
    lines = [
        json.loads(line)
        for line in data.decode("utf-8-sig").splitlines()
        if line.strip()
    ]
    if (
        not lines
        or lines[0].get("kind") != "export.header"
        or lines[0].get("format") != fetch.FORMAT
    ):
        raise ValueError(f"{path.name} is not a consumption export")
    if int(lines[0].get("version") or 0) != fetch.FORMAT_VERSION:
        raise ValueError(
            f"{path.name} has unsupported export version {lines[0].get('version')}"
        )
    if lines[-1].get("kind") != "export.footer" or not lines[-1].get("complete"):
        raise ValueError(
            f"{path.name} is incomplete (no footer); re-run fetch.py export"
        )
    fingerprint = hashlib.sha256(data).hexdigest()
    return fingerprint, lines[0], lines[1:-1], lines[-1]


class Consumption(Plugin):
    manifest = Manifest(
        name="consumption",
        version="0.1.0",
        default_interval_s=3600,
        channels=[
            Channel(
                name=CHANNEL,
                description=(
                    "Media the owner played, watched, listened to, liked or saved: Spotify, "
                    "Plex, Audiobookshelf and YouTube in one channel, with a service field"
                ),
                fields=[
                    {"name": k, "type": t, "description": d}
                    for k, (t, d) in FIELDS.items()
                ],
            ),
            Channel(
                name=BOOK_TEXT_CHANNEL,
                description=(
                    "Raw extracted text of ebooks matching audiobooks the owner has "
                    "listened past 25% of (or finished), split into ordered chapter chunks "
                    "with one thread per book"
                ),
                is_reference=True,
                fields=[
                    {"name": k, "type": t, "description": d}
                    for k, (t, d) in BOOK_TEXT_FIELDS.items()
                ],
            ),
        ],
        settings=declarations(False),
        secrets=declarations(True),
        link_types=LINK_TYPES,
    )

    def __init__(self):
        super().__init__()
        self.checkpoint_every = max(
            1, int(self.settings.get("checkpoint_every") or 500)
        )
        self.report = {}

    def execute(self, client, mode, dry_run=False):
        self.report = {}
        report = super().execute(client, mode, dry_run)
        report.update(self.report)
        return report

    def config(self):
        env = {}
        for key, (name, _default, _secret, _description) in fetch.ENV.items():
            value = (
                self.secrets.get(key)
                if key in fetch.SECRET_KEYS
                else self.settings.get(key)
            )
            if value is not None:
                env[name] = value
        return fetch.Config(env)

    def emit(self, state, record, link_buffer=None):
        """Map one record and apply the two small ledgers kept in the cursor."""
        mapped = map_record(record)
        if mapped is None:
            self.report["skipped_records"] = self.report.get("skipped_records", 0) + 1
            return
        message = mapped.message
        key = message["external_id"]
        clock = MUTABLE.get(record["kind"])
        if clock:
            version = int(record["raw"].get(clock) or 0)
            latest = state.setdefault("latest", {})
            if version < latest.get(key, -1):
                # An older export of a record the source has since changed.
                return
            latest[key] = version
        if mapped.pinned:
            seen = state.setdefault("first_seen", {})
            first = seen.get(key)
            if first and datetime.fromisoformat(first) <= datetime.fromisoformat(
                message["happened_at"]
            ):
                message["happened_at"] = first
                service = message["fields"]["service"]
                if message["thread"]["external_id"].startswith(f"{service}:day:"):
                    # A day thread follows the pinned time, or a later export
                    # would move the message to another day's thread.
                    message["thread"] = day_thread(service, first)
            else:
                seen[key] = message["happened_at"]
        yield message
        if link_buffer is not None:
            link_buffer.extend(mapped.links)
        else:
            for data in mapped.links:
                yield Record(kind="link", data=data)

    @staticmethod
    def flush_links(link_buffer):
        for data in link_buffer:
            yield Record(kind="link", data=data)
        link_buffer.clear()

    def records(self, cursor, mode):
        state = copy.deepcopy(cursor)
        source = (self.settings.get("source") or "live").strip()
        if source.startswith("export:"):
            yield from self.from_exports(state, source[len("export:") :])
        elif source == "live":
            yield from self.live(state)
        else:
            raise ValueError("CONSUMPTION_SOURCE must be live or export:<path>")

    def from_exports(self, state, path):
        done = state.setdefault("exports", {})
        files = export_files(path)
        if not files:
            raise ValueError("No .jsonl export files found at CONSUMPTION_SOURCE")
        imported = 0
        link_buffer = []
        for file in files:
            fingerprint, _header, records, footer = read_export(file)
            position = done.get(fingerprint, 0)
            if position == "done":
                continue
            for index, record in enumerate(records):
                if index < position:
                    continue
                yield from self.emit(state, record, link_buffer)
                done[fingerprint] = index + 1
                imported += 1
                if (index + 1) % self.checkpoint_every == 0:
                    yield from self.flush_links(link_buffer)
                    yield Checkpoint(cursor=copy.deepcopy(state))
            done[fingerprint] = "done"
            yield from self.flush_links(link_buffer)
            yield from capped(footer.get("notes") or {})
            if footer.get("errors") or footer.get("stopped"):
                log.warning(
                    "%s: fetch stopped or failed for %s",
                    file.name,
                    ", ".join(
                        sorted({*footer.get("errors", {}), *footer.get("stopped", {})})
                    ),
                )
            yield Checkpoint(cursor=copy.deepcopy(state))
        self.report["export_records"] = imported

    def live(self, state):
        config = self.config()
        fetcher = fetch.Fetcher(config, state.get("fetch_state") or {})
        count = 0
        link_buffer = []
        for record in fetcher.run():
            yield from self.emit(state, record, link_buffer)
            count += 1
            if count % self.checkpoint_every == 0:
                # The fetch state moves only at the end: a crash refetches,
                # and stable IDs make the replay a no-op.
                yield from self.flush_links(link_buffer)
                yield Checkpoint(cursor=copy.deepcopy(state))
        state["fetch_state"] = fetcher.state
        yield from self.flush_links(link_buffer)
        yield from capped(fetcher.notes)
        yield Checkpoint(cursor=copy.deepcopy(state))
        self.report["fetch"] = {
            k: v
            for k, v in fetcher.footer().items()
            if k in ("counts", "requests", "stopped", "notes")
        }
        if fetcher.errors:
            raise RuntimeError(
                "fetch failed for "
                + "; ".join(
                    f"{service}: {error}"
                    for service, error in sorted(fetcher.errors.items())
                )
            )


def capped(notes):
    """Coverage for a Spotify recently-played window that no longer reached
    the previous run's newest play: the plays in between cannot be fetched."""
    gap = notes.get("spotify_recent_gap")
    if gap:
        yield coverage(
            channel=CHANNEL,
            scope_key="spotify",
            status="partial",
            reasons=["source_cap"],
            gaps=[dict(gap, reason="source_cap")],
            detail={"source": "recently-played"},
        )


def main():
    run(Consumption)
