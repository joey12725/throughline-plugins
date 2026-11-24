#!/usr/bin/env python3
"""Standard-library, read-only qBittorrent WebUI API exporter.

The fetcher is intentionally standalone so it can run inside the host that owns
the qBittorrent credentials. It writes only sanitized torrent lifecycle events:
tracker URLs, magnet URIs, cookies, usernames and passwords never reach output.
"""

import argparse
import copy
import gzip
import http.cookiejar
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

FORMAT = "throughline-qbittorrent-export"
FORMAT_VERSION = 1
FETCHER_VERSION = "1.0"
USER_AGENT = "throughline-qbittorrent-fetch/" + FETCHER_VERSION

ENV = {
    "source": (
        "QBITTORRENT_SOURCE",
        "live",
        False,
        "live, or export:<file or directory of export JSONL files>",
    ),
    "url": (
        "QBITTORRENT_URL",
        None,
        False,
        "qBittorrent WebUI base URL",
    ),
    "timeout_s": (
        "QBITTORRENT_TIMEOUT_S",
        "30",
        False,
        "HTTP request timeout in seconds",
    ),
    "checkpoint_every": (
        "QBITTORRENT_CHECKPOINT_EVERY",
        "100",
        False,
        "Plugin checkpoint interval in records",
    ),
    "username": (
        "QBITTORRENT_USERNAME",
        None,
        True,
        "qBittorrent WebUI username",
    ),
    "password": (
        "QBITTORRENT_PASSWORD",
        None,
        True,
        "qBittorrent WebUI password",
    ),
}
SECRET_KEYS = tuple(key for key, value in ENV.items() if value[2])


class FetchError(RuntimeError):
    """A sanitized source error safe to include in local logs."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def utc_now():
    return datetime.now(timezone.utc)


def iso_from_epoch(value, fallback):
    try:
        seconds = int(value or 0)
    except (TypeError, ValueError):
        seconds = 0
    if seconds > 0:
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat(), "exact"
    return fallback, "observed"


def normalize_hash(value):
    text = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", text):
        raise FetchError("qBittorrent returned an invalid torrent hash")
    return text


def tracker_host(value):
    """Return only the hostname, never the announce URL or its passkey."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = urllib.parse.urlsplit(text if "://" in text else "//" + text)
        return parsed.hostname.lower() if parsed.hostname else None
    except ValueError:
        return None


def normalized_tags(value):
    if isinstance(value, list):
        parts = value
    else:
        parts = str(value or "").split(",")
    return sorted({str(item).strip() for item in parts if str(item).strip()})


def media_kind(category, save_path):
    """Deterministic, path/category-only media classification; no fuzzy match."""
    category = str(category or "").lower()
    path = str(save_path or "").lower().rstrip("/")
    checks = (
        ("tv", ("sonarr", "tv"), ("/tv-direct", "/tv")),
        ("movie", ("radarr", "movie", "movies"), ("/movie-direct", "/movies")),
        ("music", ("lidarr", "music"), ("/music",)),
        ("audiobook", ("audiobook",), ("/audiobooks", "-audiobooks")),
        ("book", ("book", "manga"), ("/books",)),
        ("game", ("arcade", "rom", "game"), ("/arcade-roms",)),
    )
    for kind, category_terms, path_terms in checks:
        if any(term in category for term in category_terms) or any(
            path == term or path.startswith(term + "/") or term in path
            for term in path_terms
        ):
            return kind
    return None


def snapshot(raw):
    """Whitelist source fields and sanitize the tracker before persistence."""
    item = {
        "name": str(raw.get("name") or "Unknown torrent"),
        "hash": normalize_hash(raw.get("hash")),
        "size": int(raw.get("total_size") or raw.get("size") or 0),
        "category": str(raw.get("category") or ""),
        "tags": normalized_tags(raw.get("tags")),
        "save_path": str(raw.get("save_path") or ""),
        "tracker_host": tracker_host(raw.get("tracker")),
        "added_on": int(raw.get("added_on") or 0),
        "completion_on": int(raw.get("completion_on") or 0),
        "ratio": float(raw.get("ratio") or 0),
        "state": str(raw.get("state") or "unknown"),
    }
    item["media_kind"] = media_kind(item["category"], item["save_path"])
    return item


def merge_snapshot(previous, patch):
    merged = dict(previous or {})
    merged.update(patch)
    merged.setdefault("hash", patch.get("hash") or previous.get("hash"))
    return snapshot(merged)


def event(event_type, item, generation, fetched_at, sequence=None, previous=None):
    if event_type == "added":
        at, precision = iso_from_epoch(item.get("added_on"), fetched_at)
    elif event_type == "completed":
        at, precision = iso_from_epoch(item.get("completion_on"), fetched_at)
    else:
        at, precision = fetched_at, "observed"
    suffix = event_type if sequence is None else f"{event_type}:{sequence}"
    event_id = f"{item['hash']}:g{generation}:{suffix}"
    raw = dict(item)
    raw.update(
        {
            "event_id": event_id,
            "event_type": event_type,
            "event_at": at,
            "time_precision": precision,
            "generation": generation,
        }
    )
    if previous is not None:
        raw["previous"] = previous
    return {
        "kind": "qbittorrent.event",
        "id": event_id,
        "fetched_at": fetched_at,
        "raw": raw,
    }


class Config:
    def __init__(self, env=None):
        values = os.environ if env is None else env
        self.url = str(values.get("QBITTORRENT_URL") or "").rstrip("/")
        self.username = values.get("QBITTORRENT_USERNAME")
        self.password = values.get("QBITTORRENT_PASSWORD")
        try:
            self.timeout_s = float(values.get("QBITTORRENT_TIMEOUT_S") or 30)
        except ValueError as error:
            raise FetchError("QBITTORRENT_TIMEOUT_S must be a number") from error
        if not self.url:
            raise FetchError("QBITTORRENT_URL is required")
        if not self.username or not self.password:
            raise FetchError("QBITTORRENT_USERNAME and QBITTORRENT_PASSWORD are required")
        parsed = urllib.parse.urlsplit(self.url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise FetchError("QBITTORRENT_URL must be an HTTP(S) URL")
        if parsed.username or parsed.password:
            raise FetchError("QBITTORRENT_URL must not contain credentials")


class Api:
    """Read-only allowlisted WebUI client."""

    GETS = {"app/version", "torrents/info", "sync/maindata"}

    def __init__(self, config, opener=None):
        self.config = config
        self.base = config.url + "/api/v2/"
        self.opener = opener or urllib.request.build_opener(
            NoRedirect(),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
        )
        self.logged_in = False

    def _open(self, request, label):
        try:
            return self.opener.open(request, timeout=self.config.timeout_s)
        except urllib.error.HTTPError as error:
            raise FetchError(f"{label} failed (HTTP {error.code})") from None
        except urllib.error.URLError as error:
            reason = type(error.reason).__name__
            raise FetchError(f"{label} failed ({reason})") from None
        except TimeoutError:
            raise FetchError(f"{label} failed (timeout)") from None

    def login(self):
        body = urllib.parse.urlencode(
            {"username": self.config.username, "password": self.config.password}
        ).encode()
        request = urllib.request.Request(
            self.base + "auth/login",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": self.config.url + "/",
                "User-Agent": USER_AGENT,
            },
        )
        with self._open(request, "qBittorrent login") as response:
            if response.read(32).strip() != b"Ok.":
                raise FetchError("qBittorrent login failed")
        self.logged_in = True

    def get(self, path, query=None):
        if path not in self.GETS:
            raise FetchError("qBittorrent endpoint is not read-only allowlisted")
        if not self.logged_in:
            self.login()
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(
            url,
            method="GET",
            headers={"Referer": self.config.url + "/", "User-Agent": USER_AGENT},
        )
        with self._open(request, "qBittorrent " + path) as response:
            data = response.read()
        if path == "app/version":
            return data.decode("utf-8", "replace").strip()
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            raise FetchError(f"qBittorrent {path} returned invalid JSON") from None


class Fetcher:
    def __init__(self, config, state=None, api=None, now=None):
        self.config = config
        self.state = copy.deepcopy(state or {})
        self.api = api or Api(config)
        self.now = now or utc_now()
        self.counts = {}
        self.version = None

    def _emit(self, output, value):
        output.append(value)
        kind = value["raw"]["event_type"]
        self.counts[kind] = self.counts.get(kind, 0) + 1

    def _new_entry(self, item, fetched_at):
        tombstones = self.state.setdefault("tombstones", {})
        generation = int(tombstones.get(item["hash"], 0)) + 1
        entry = {
            "generation": generation,
            "category_seq": 0,
            "tags_seq": 0,
            "completed": bool(item.get("completion_on")),
            "snapshot": item,
        }
        return entry, [
            event("added", item, generation, fetched_at),
            *(
                [event("completed", item, generation, fetched_at)]
                if item.get("completion_on")
                else []
            ),
        ]

    def _changed_entry(self, entry, item, fetched_at):
        old = entry["snapshot"]
        output = []
        generation = entry["generation"]
        if old.get("category") != item.get("category"):
            entry["category_seq"] = int(entry.get("category_seq", 0)) + 1
            output.append(
                event(
                    "category_changed",
                    item,
                    generation,
                    fetched_at,
                    entry["category_seq"],
                    {"category": old.get("category")},
                )
            )
        if old.get("tags") != item.get("tags"):
            entry["tags_seq"] = int(entry.get("tags_seq", 0)) + 1
            output.append(
                event(
                    "tags_changed",
                    item,
                    generation,
                    fetched_at,
                    entry["tags_seq"],
                    {"tags": old.get("tags")},
                )
            )
        if not entry.get("completed") and item.get("completion_on"):
            entry["completed"] = True
            output.append(event("completed", item, generation, fetched_at))
        entry["snapshot"] = item
        return output

    def run(self):
        fetched_at = self.now.astimezone(timezone.utc).isoformat()
        output = []
        current = self.state.setdefault("torrents", {})
        self.version = self.api.get("app/version")
        rid = self.state.get("rid")
        if rid is None:
            # Establish the incremental watermark first, then take the full
            # snapshot. A change racing the snapshot remains newer than this
            # rid and is therefore returned on the next incremental request.
            sync = self.api.get("sync/maindata", {"rid": 0})
            if not isinstance(sync, dict) or "rid" not in sync:
                raise FetchError("qBittorrent sync/maindata returned an invalid payload")
            rows = self.api.get("torrents/info")
            if not isinstance(rows, list):
                raise FetchError("qBittorrent torrents/info returned an invalid payload")
            for raw in rows:
                item = snapshot(raw)
                entry, events = self._new_entry(item, fetched_at)
                current[item["hash"]] = entry
                for value in events:
                    self._emit(output, value)
            self.state["rid"] = int(sync.get("rid") or 0)
        else:
            sync = self.api.get("sync/maindata", {"rid": int(rid)})
            if not isinstance(sync, dict) or "rid" not in sync:
                raise FetchError("qBittorrent sync/maindata returned an invalid payload")
            patches = sync.get("torrents") or {}
            if not isinstance(patches, dict):
                raise FetchError("qBittorrent sync torrents payload is invalid")
            seen = set()
            for hash_value, patch in patches.items():
                hash_value = normalize_hash(hash_value)
                patch = dict(patch or {}, hash=hash_value)
                seen.add(hash_value)
                if hash_value in current:
                    item = merge_snapshot(current[hash_value]["snapshot"], patch)
                    for value in self._changed_entry(
                        current[hash_value], item, fetched_at
                    ):
                        self._emit(output, value)
                else:
                    item = snapshot(patch)
                    entry, events = self._new_entry(item, fetched_at)
                    current[hash_value] = entry
                    for value in events:
                        self._emit(output, value)
            removed = {normalize_hash(value) for value in sync.get("torrents_removed", [])}
            if sync.get("full_update"):
                removed |= set(current) - seen
            for hash_value in sorted(removed):
                entry = current.pop(hash_value, None)
                if not entry:
                    continue
                item = dict(entry["snapshot"], state="removed")
                self._emit(
                    output,
                    event("removed", item, entry["generation"], fetched_at),
                )
                self.state.setdefault("tombstones", {})[hash_value] = entry[
                    "generation"
                ]
            self.state["rid"] = int(sync["rid"])
        self.state["schema_version"] = 1
        return output

    def footer(self):
        return {
            "kind": "export.footer",
            "complete": True,
            "counts": self.counts,
            "torrent_count": len(self.state.get("torrents", {})),
            "rid": self.state.get("rid"),
            "app_version": self.version,
        }


def read_state(path):
    if not path or not Path(path).exists():
        return {}
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FetchError("qBittorrent state file is unreadable") from error


def write_bytes(path, data):
    if path == "-":
        sys.stdout.buffer.write(data)
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".partial")
    partial.write_bytes(data)
    os.chmod(partial, 0o600)
    partial.replace(target)


def export(config, out, state_path=None, now=None):
    fetcher = Fetcher(config, read_state(state_path), now=now)
    records = fetcher.run()
    header = {
        "kind": "export.header",
        "format": FORMAT,
        "version": FORMAT_VERSION,
        "fetched_at": (now or fetcher.now).astimezone(timezone.utc).isoformat(),
        "source": "qbittorrent-webui-api-v2",
    }
    lines = [header, *records, fetcher.footer()]
    data = ("\n".join(json.dumps(row, sort_keys=True) for row in lines) + "\n").encode()
    if out.endswith(".gz"):
        data = gzip.compress(data, mtime=0)
    write_bytes(out, data)
    if state_path:
        state_data = (json.dumps(fetcher.state, sort_keys=True) + "\n").encode()
        write_bytes(state_path, state_data)
    return fetcher.footer()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("export", help="write a sanitized JSONL export")
    command.add_argument("--out", required=True)
    command.add_argument("--state")
    args = parser.parse_args(argv)
    try:
        footer = export(Config(), args.out, args.state)
    except FetchError as error:
        print(f"throughline-qbittorrent: {error}", file=sys.stderr)
        return 1
    print(
        "throughline-qbittorrent: exported "
        + str(sum(footer["counts"].values()))
        + " events",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
