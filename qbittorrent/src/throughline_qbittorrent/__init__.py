"""qBittorrent lifecycle connector for Throughline."""

import copy
import gzip
import hashlib
import json
from pathlib import Path

from throughline_sdk import Channel, Checkpoint, Declaration, Manifest, Plugin, run

from . import fetch

CHANNEL = "downloads.qbittorrent"

FIELDS = {
    "event_type": ("text", "added, completed, removed, category_changed or tags_changed"),
    "name": ("text", "Torrent display name"),
    "hash": ("text", "Lower-case BitTorrent info hash"),
    "size": ("integer", "Torrent total size in bytes"),
    "category": ("text", "qBittorrent category at this event"),
    "tags": ("json", "Sorted qBittorrent tag names at this event"),
    "save_path": ("text", "Container-visible save path at this event"),
    "tracker_host": ("text", "Tracker hostname only; announce credentials removed"),
    "added_on": ("timestamp", "qBittorrent added timestamp when available"),
    "completion_on": ("timestamp", "qBittorrent completion timestamp when available"),
    "ratio": ("number", "Share ratio at this event"),
    "state": ("text", "qBittorrent state at this event"),
    "media_kind": ("text", "Exact category/save-path classification such as tv or movie"),
    "time_precision": ("text", "exact source timestamp or observed poll time"),
    "previous": ("json", "Previous category or tags for a detected change"),
}


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
        return sorted(
            (
                item
                for item in root.iterdir()
                if item.is_file() and item.name.endswith((".jsonl", ".jsonl.gz"))
            ),
            key=lambda item: item.name,
        )
    raise ValueError("QBITTORRENT_SOURCE export path does not exist")


def read_export(path):
    data = path.read_bytes()
    if path.name.endswith(".gz"):
        data = gzip.decompress(data)
    try:
        rows = [json.loads(line) for line in data.decode("utf-8").splitlines() if line]
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{path.name} is not valid qBittorrent JSONL") from error
    if (
        not rows
        or rows[0].get("kind") != "export.header"
        or rows[0].get("format") != fetch.FORMAT
        or int(rows[0].get("version") or 0) != fetch.FORMAT_VERSION
    ):
        raise ValueError(f"{path.name} is not a qBittorrent export")
    if rows[-1].get("kind") != "export.footer" or not rows[-1].get("complete"):
        raise ValueError(f"{path.name} is incomplete")
    return hashlib.sha256(data).hexdigest(), rows[1:-1], rows[-1]


def as_timestamp(value):
    if not value:
        return None
    return fetch.iso_from_epoch(value, "1970-01-01T00:00:00+00:00")[0]


def map_record(record):
    raw = record["raw"]
    event_type = raw["event_type"]
    verbs = {
        "added": "Added torrent",
        "completed": "Completed torrent",
        "removed": "Removed torrent",
        "category_changed": "Changed torrent category",
        "tags_changed": "Changed torrent tags",
    }
    if event_type not in verbs:
        raise ValueError(f"unsupported qBittorrent event type {event_type}")
    return {
        "channel": CHANNEL,
        "external_id": raw["event_id"],
        "thread": {
            "external_id": f"{raw['hash']}:g{raw['generation']}",
            "title": raw["name"],
        },
        "happened_at": raw["event_at"],
        "content": f"{verbs[event_type]}: {raw['name']}",
        "fields": {
            "event_type": event_type,
            "name": raw["name"],
            "hash": raw["hash"],
            "size": int(raw.get("size") or 0),
            "category": raw.get("category") or "",
            "tags": raw.get("tags") or [],
            "save_path": raw.get("save_path") or "",
            "tracker_host": raw.get("tracker_host"),
            "added_on": as_timestamp(raw.get("added_on")),
            "completion_on": as_timestamp(raw.get("completion_on")),
            "ratio": float(raw.get("ratio") or 0),
            "state": raw.get("state") or "unknown",
            "media_kind": raw.get("media_kind"),
            "time_precision": raw.get("time_precision") or "observed",
            "previous": raw.get("previous"),
        },
        "payload": raw,
    }


class QBittorrent(Plugin):
    manifest = Manifest(
        name="qbittorrent",
        version="0.1.0",
        default_interval_s=900,
        channels=[
            Channel(
                name=CHANNEL,
                description=(
                    "Read-only qBittorrent torrent lifecycle events: additions, "
                    "completions, removals, category changes and tag changes"
                ),
                fields=[
                    {"name": name, "type": kind, "description": description}
                    for name, (kind, description) in FIELDS.items()
                ],
            )
        ],
        settings=declarations(False),
        secrets=declarations(True),
    )

    def __init__(self):
        super().__init__()
        self.checkpoint_every = max(
            1, int(self.settings.get("checkpoint_every") or 100)
        )

    def config(self):
        env = {}
        for key, (name, _default, is_secret, _description) in fetch.ENV.items():
            value = self.secrets.get(key) if is_secret else self.settings.get(key)
            if value is not None:
                env[name] = value
        return fetch.Config(env)

    def records(self, cursor, mode):
        state = copy.deepcopy(cursor)
        source = (self.settings.get("source") or "live").strip()
        if source == "live":
            fetcher = fetch.Fetcher(self.config(), state.get("fetch_state") or {})
            records = fetcher.run()
            for index, record in enumerate(records, 1):
                yield map_record(record)
                if index % self.checkpoint_every == 0:
                    yield Checkpoint(cursor=copy.deepcopy(state))
            state["fetch_state"] = fetcher.state
            yield Checkpoint(cursor=state)
            return
        if not source.startswith("export:"):
            raise ValueError("QBITTORRENT_SOURCE must be live or export:<path>")
        files = export_files(source[len("export:") :])
        if not files:
            raise ValueError("No qBittorrent export files found")
        mark = state.get("export") or {}
        for path in files:
            fingerprint, records, _footer = read_export(path)
            position = int(mark.get("position") or 0) if mark.get("hash") == fingerprint else 0
            if mark.get("hash") == fingerprint and mark.get("done"):
                continue
            for index, record in enumerate(records):
                if index < position:
                    continue
                yield map_record(record)
                state["export"] = {"hash": fingerprint, "position": index + 1}
                if (index + 1) % self.checkpoint_every == 0:
                    yield Checkpoint(cursor=copy.deepcopy(state))
            state["export"] = {
                "hash": fingerprint,
                "position": len(records),
                "done": True,
            }
            yield Checkpoint(cursor=copy.deepcopy(state))


def main():
    run(QBittorrent)
