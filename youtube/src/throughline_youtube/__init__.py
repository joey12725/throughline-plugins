"""YouTube observations from Takeout, watcher exports, OAuth subscriptions and optional yt-dlp."""

import copy
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from throughline_sdk import (
    Channel,
    Checkpoint,
    Declaration,
    Manifest,
    Plugin,
    Record,
    run,
)

from . import fetch as watcher_fetch
from . import sources
from .parsers import csv_rows, timestamp, watch_html, watch_json


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:24]


def fields(**values):
    return [dict(name=k, type=v[0], description=v[1]) for k, v in values.items()]


COMMON = dict(
    time_precision=(
        "text",
        "source timestamp, day (history day heading), estimated (watcher session) or first_seen; first_seen is not the action time",
    ),
    source=("text", "takeout, ytdlp, watcher, or api"),
)
# Watcher (fetch.py) fields of youtube.history; null on other sources.
SESSION = dict(
    record_type=(
        "text",
        "day (watcher: watched on this history day) or session (watcher: one viewing session); null for Takeout and yt-dlp",
    ),
    day=("text", "History day heading as a date (watcher)"),
    length_s=("integer", "Video length in seconds when the feed shows it"),
    progress=(
        "number",
        "Resume bar percent when the history day item was first seen",
    ),
    session_kind=("text", "new, resume, progress or rewatch"),
    progress_before=("number", "Resume bar percent at the previous poll"),
    progress_after=("number", "Resume bar percent at this poll"),
    watched_fraction=(
        "number",
        "Fraction of the video watched in this session, estimated",
    ),
    watched_s=(
        "integer",
        "Seconds watched in this session, estimated from fraction and length",
    ),
    window_start=("text", "Previous poll: the session happened after this"),
    window_end=("text", "This poll: the session happened before this"),
    session_number=(
        "integer",
        "Ordinal of this session of the video seen by the watcher",
    ),
)
WATCHER_FIELDS = tuple(SESSION)


def watcher_row(raw, identity, at, precision, **extra):
    """A watcher record in the row shape the snapshot machinery uses."""
    vid = raw.get("video_id")
    row = dict(
        video_id=vid,
        title=raw.get("title"),
        channel=raw.get("channel"),
        channel_id=raw.get("channel_id"),
        url=raw.get("url")
        or (f"https://www.youtube.com/watch?v={vid}" if vid else None),
        at=at,
        time_precision=precision,
        payload=raw,
    )
    if raw.get("length_s") is not None:
        row["length_s"] = raw["length_s"]
    if raw.get("is_short") is not None:
        row["is_short"] = raw["is_short"]
    if identity:
        row["identity"] = identity
    row.update({k: v for k, v in extra.items() if v is not None})
    return row


VIDEO = dict(
    video_id=("text", "YouTube video ID, absent for unavailable views"),
    url=("text", "Source video URL"),
)


class YouTube(Plugin):
    manifest = Manifest(
        name="youtube",
        version="0.1.0",
        channels=[
            Channel(
                name="youtube.history",
                description="Individual exported views, watcher viewing sessions/days, or approximate first-seen history observations",
                fields=fields(
                    **COMMON,
                    **VIDEO,
                    video_title=("text", "Source video title"),
                    youtube_channel_id=("text", "Publisher channel ID when supplied"),
                    channel_title=("text", "Publisher title when supplied"),
                    is_short=("boolean", "Source explicitly identifies a Short"),
                    is_music=("boolean", "Source explicitly identifies YouTube Music"),
                    is_ad=(
                        "boolean",
                        "Source identifies an ad view; these are flagged",
                    ),
                    **SESSION,
                ),
            ),
            Channel(
                name="youtube.watch_later",
                description="Saved playlist items; removed=true is a new version, never deletion",
                fields=fields(
                    **COMMON,
                    **VIDEO,
                    title=("text", "Source video title"),
                    channel_title=("text", "Source publisher title"),
                    position=("integer", "Null: feed rank is not a change to a save"),
                    playlist=("text", "Playlist identity; WL for Watch Later"),
                    removed=("boolean", "Absent from a later complete snapshot"),
                ),
            ),
            Channel(
                name="youtube.subscriptions",
                description="Channel subscriptions and observed unsubscriptions",
                fields=fields(
                    **COMMON,
                    youtube_channel_id=("text", "Subscribed channel ID"),
                    channel_title=("text", "Subscribed channel title"),
                    url=("text", "Channel URL"),
                    removed=("boolean", "Absent from a later complete snapshot"),
                ),
            ),
        ],
        settings={
            k: Declaration(
                env="YOUTUBE_" + k.upper(),
                required=False,
                default=d,
                description=desc,
            )
            for k, d, desc in [
                ("takeout", None, "Extracted Takeout directory; read recursively"),
                (
                    "watcher",
                    None,
                    "Watcher export (fetch.py poll) or a directory of them, read in name order",
                ),
                ("api", "false", "Enable OAuth subscription snapshots"),
                (
                    "ytdlp",
                    "false",
                    "Enable unofficial cookie-based watch/history snapshots",
                ),
                (
                    "command",
                    '["yt-dlp"]',
                    "JSON argv prefix of a user-installed executable; no shell",
                ),
                (
                    "history_names",
                    '["watch-history.json", "watch-history.html"]',
                    "History filenames including localized names",
                ),
                (
                    "watch_later_names",
                    '["Watch later", "Watch Later", "WL"]',
                    "Playlist filename stems identifying Watch Later",
                ),
                (
                    "playlist_dirs",
                    '["playlists"]',
                    "Playlist directory names, including translated names",
                ),
                (
                    "subscription_dirs",
                    '["subscriptions"]',
                    "Subscription directory names, including translated names",
                ),
                (
                    "csv_headers",
                    "{}",
                    "JSON translated CSV header to canonical column mapping",
                ),
                (
                    "date_format",
                    None,
                    "Optional strptime format with %z for localized HTML/CSV dates",
                ),
                (
                    "recording",
                    None,
                    "Invented offline API/yt-dlp recording JSON; disables live source calls",
                ),
            ]
        },
        secrets={
            k: Declaration(env="YOUTUBE_" + k.upper(), required=False, description=desc)
            for k, desc in [
                ("client_id", "Google OAuth client ID"),
                ("client_secret", "Google OAuth client secret"),
                ("refresh_token", "Google OAuth refresh token"),
                (
                    "browser",
                    "yt-dlp browser/profile selector; may contain a private path",
                ),
            ]
        },
        link_types=[
            "consumed",
            "published_by",
            "saved",
            "subscribed_to",
        ],
    )

    def takeout(self):
        root = Path(self.settings["takeout"])
        if not root.is_dir():
            raise ValueError("YOUTUBE_TAKEOUT must be an extracted directory")
        names = json.loads(self.settings["history_names"])
        aliases = json.loads(self.settings["csv_headers"])
        wl = {s.casefold() for s in json.loads(self.settings["watch_later_names"])}
        paths = sorted(p for p in root.rglob("*") if p.is_file())
        # Prefer JSON when both export variants exist: don't double the views.
        history = [p for p in paths if p.name in names]
        if any(p.suffix == ".json" for p in history):
            history = [p for p in history if p.suffix == ".json"]
        watches = []
        for path in history:
            parser = watch_json if path.suffix == ".json" else watch_html
            watches.extend(
                parser(
                    path.read_text(encoding="utf-8-sig"), self.settings["date_format"]
                )
            )
        yield "takeout:history", "history", watches, False, "takeout"
        for path in paths:
            if path.suffix.lower() != ".csv":
                continue
            # Only Takeout's subscription and playlist folders, not unrelated
            # exports elsewhere under the supplied root.
            parent = path.parent.name.casefold()
            subscription_dirs = {
                x.casefold() for x in json.loads(self.settings["subscription_dirs"])
            }
            playlist_dirs = {
                x.casefold() for x in json.loads(self.settings["playlist_dirs"])
            }
            if parent not in subscription_dirs | playlist_dirs:
                continue
            rows = list(
                csv_rows(
                    path.read_text(encoding="utf-8-sig"),
                    aliases,
                    self.settings["date_format"],
                )
            )
            if parent in subscription_dirs:
                yield "takeout:subscriptions", "subscriptions", rows, True, "takeout"
            else:
                stem = path.stem.removesuffix("-videos")
                playlist = "WL" if stem.casefold() in wl else stem
                for pos, row in enumerate(rows, 1):
                    row["playlist"] = playlist
                    row["position"] = int(row.get("position") or pos)
                yield (
                    "takeout:playlist:" + playlist,
                    "watch_later",
                    rows,
                    True,
                    "takeout",
                )

    def watcher(self):
        """History days, sessions, Watch Later and subscriptions from watcher exports."""
        root = Path(self.settings["watcher"])
        if root.is_dir():
            paths = sorted(
                p
                for p in root.iterdir()
                if p.is_file() and p.name.endswith((".jsonl", ".jsonl.gz"))
            )
        elif root.is_file():
            paths = [root]
        else:
            raise ValueError("YOUTUBE_WATCHER must be an export file or directory")
        # Later exports win per item; the watcher tracks removals itself.
        watches, saved, subs = {}, {}, {}
        for path in paths:
            header, records = watcher_fetch.read_export(path)
            tz = watcher_fetch.zone(header.get("tz"))
            for r in records:
                raw = r["raw"]
                if r["kind"] == "history_item":
                    key = "day:" + r["id"]
                    if raw.get("day"):
                        moment = datetime.fromisoformat(raw["day"]).replace(tzinfo=tz)
                        at, precision = moment.isoformat(), "day"
                    else:
                        at, precision = raw["first_seen"], "first_seen"
                    watches[key] = watcher_row(
                        raw,
                        key,
                        at,
                        precision,
                        record_type="day",
                        day=raw.get("day"),
                        progress=raw.get("progress"),
                    )
                elif r["kind"] == "session":
                    key = "session:" + r["id"]
                    watches[key] = watcher_row(
                        raw,
                        key,
                        raw["start_est"],
                        "estimated",
                        ended_at=raw["end_est"],
                        record_type="session",
                        **{k: raw.get(k) for k in WATCHER_FIELDS if k in raw},
                    )
                elif r["kind"] == "watch_later":
                    row = watcher_row(
                        raw, "watcher:" + r["id"], raw["first_seen"], "first_seen"
                    )
                    row.update(playlist="WL", removed=bool(raw.get("removed")))
                    saved[r["id"]] = row
                elif r["kind"] == "subscription":
                    row = watcher_row(raw, None, raw["first_seen"], "first_seen")
                    row.update(removed=bool(raw.get("removed")))
                    subs[r["id"]] = row
        yield "watcher:history", "history", list(watches.values()), False, "watcher"
        if saved:
            yield (
                "watcher:watch_later",
                "watch_later",
                list(saved.values()),
                False,
                "watcher",
            )
        if subs:
            yield (
                "watcher:subscriptions",
                "subscriptions",
                list(subs.values()),
                False,
                "watcher",
            )

    def snapshots(self):
        if self.settings["takeout"]:
            yield from self.takeout()
        if self.settings["watcher"]:
            yield from self.watcher()
        if self.settings["recording"]:
            recording = json.loads(
                Path(self.settings["recording"]).read_text(encoding="utf-8")
            )
        else:
            recording = None
        if self.settings["api"].lower() == "true":
            if recording is not None:
                pages = recording.get("api_pages", [])
                if (
                    not pages
                    or any(not isinstance(p.get("items"), list) for p in pages)
                    or pages[-1].get("nextPageToken")
                ):
                    raise ValueError("Incomplete recorded API snapshot")
                rows = [r for p in pages for r in p["items"]]
            else:
                if not all(
                    self.secrets.get(k)
                    for k in ("client_id", "client_secret", "refresh_token")
                ):
                    raise ValueError(
                        "Enable API with YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET and YOUTUBE_REFRESH_TOKEN"
                    )
                rows = sources.subscriptions(self.secrets)
            normalized = []
            for row in rows:
                s = row["snippet"]
                normalized.append(
                    dict(
                        channel_id=s["resourceId"]["channelId"],
                        channel=s.get("title"),
                        at=timestamp(s.get("publishedAt")),
                        payload=row,
                    )
                )
            yield "api:subscriptions", "subscriptions", normalized, True, "api"
        if self.settings["ytdlp"].lower() == "true":
            command = json.loads(self.settings["command"])
            if (
                not isinstance(command, list)
                or not command
                or not all(isinstance(s, str) for s in command)
            ):
                raise ValueError("YOUTUBE_COMMAND must be a nonempty JSON argv array")
            for feed, kind in [
                (":ythistory", "history"),
                (":ytwatchlater", "watch_later"),
            ]:
                entries = (
                    recording[feed]
                    if recording is not None and not recording.get("use_command")
                    else sources.ytdlp(command, self.secrets["browser"], feed)
                )
                rows = [
                    dict(
                        video_id=e["id"],
                        title=e.get("title"),
                        channel=e.get("channel") or e.get("uploader"),
                        channel_id=e.get("channel_id"),
                        url=e.get("webpage_url")
                        or f"https://www.youtube.com/watch?v={e['id']}",
                        playlist="WL",
                        position=i,
                        at=None,
                        payload=e,
                    )
                    for i, e in enumerate(entries, 1)
                ]
                yield "ytdlp:" + kind, kind, rows, kind != "history", "ytdlp"

    def normalize(self, row, kind, source, external_id, at):
        title = row.get("title") or "Unavailable video"
        ch = row.get("channel") or "Unknown channel"
        if kind == "history":
            f = dict(
                video_id=row.get("video_id"),
                video_title=title,
                youtube_channel_id=row.get("channel_id"),
                channel_title=row.get("channel"),
                url=row.get("url"),
            )
            f.update({k: row[k] for k in ("is_short", "is_music", "is_ad") if k in row})
            f.update({k: row[k] for k in WATCHER_FIELDS if k in row})
            content = f"Watched {title} by {ch}"
            if row.get("record_type") == "session":
                part = row.get("watched_fraction")
                if part is not None:
                    content = f"Watched {round(part * 100)}% of {title} by {ch}"
                content += f" ({row.get('session_kind')} session)"
            elif row.get("record_type") == "day":
                content += f" on {row.get('day')}"
        elif kind == "watch_later":
            f = dict(
                video_id=row.get("video_id"),
                title=title,
                channel_title=row.get("channel"),
                url=row.get("url")
                or (
                    f"https://www.youtube.com/watch?v={row['video_id']}"
                    if row.get("video_id")
                    else None
                ),
                position=None,  # Feed rank changes do not change a save event.
                playlist=row.get("playlist", "WL"),
                # Only watcher rows carry their own removal state.
                removed=bool(row.get("removed")),
            )
            content = f"Saved {title} in {f['playlist']}"
            if f["removed"]:
                content = "Removed: " + content
        else:
            f = dict(
                youtube_channel_id=row["channel_id"],
                channel_title=row.get("channel"),
                url=row.get("url")
                or f"https://www.youtube.com/channel/{row['channel_id']}",
                removed=bool(row.get("removed")),
            )
            content = f"Subscribed to {ch}"
            if f["removed"]:
                content = "Removed: " + content
        f.update(
            time_precision=row.get("time_precision")
            or ("source" if row.get("at") else "first_seen"),
            source=source,
        )
        extra = {"ended_at": row["ended_at"]} if row.get("ended_at") else {}
        return extra | dict(
            channel="youtube." + kind,
            external_id=external_id,
            thread={"external_id": "owner"},
            happened_at=row.get("at") or at,
            content=content,
            fields=f,
            payload=row["payload"],
        )

    def links(self, message, row, kind):
        if kind == "subscriptions":
            yield Record(
                kind="link",
                data={
                    "type": "subscribed_to",
                    "from": {
                        "channel": message["channel"],
                        "external_id": message["external_id"],
                    },
                    "to": {
                        "kind": "organization",
                        "external_ref": "youtube:" + row["channel_id"],
                        "name": row.get("channel"),
                    },
                    "method": "source",
                },
            )
            return
        vid = row.get("video_id")
        if not vid:
            return
        work = dict(
            kind="work",
            external_ref="youtube:" + vid,
            name=row.get("title") or "Unavailable video",
        )
        yield Record(
            kind="link",
            data={
                "type": "consumed" if kind == "history" else "saved",
                "from": {
                    "channel": message["channel"],
                    "external_id": message["external_id"],
                },
                "to": work,
                "method": "source",
            },
        )
        if row.get("channel_id"):
            yield Record(
                kind="link",
                data={
                    "type": "published_by",
                    "from": work,
                    "to": dict(
                        kind="organization",
                        external_ref="youtube:" + row["channel_id"],
                        name=row.get("channel"),
                    ),
                    "method": "source",
                },
            )

    def records(self, cursor, mode):
        state = copy.deepcopy(cursor)
        state.setdefault("snapshots", {})
        # Reserve the observation clock before emitting any timestamp-less record.
        # This is not a progress checkpoint: a retry still emits the pending data.
        if not state.get("observation_at"):
            if self.settings["recording"]:
                data = json.loads(
                    Path(self.settings["recording"]).read_text(encoding="utf-8")
                )
                now = timestamp(data["observed_at"])
            else:
                now = datetime.now(UTC).isoformat()
            state["observation_at"] = now
            yield Checkpoint(cursor=copy.deepcopy(state))
        now = state["observation_at"]

        for key, kind, rows, complete, source in self.snapshots():
            # Archive snapshots are processed once per content digest. Leaving
            # the same export configured must not overwrite a newer live state.
            fingerprint = digest(rows)
            if (
                source == "takeout"
                and state.get("archives", {}).get(key) == fingerprint
            ):
                continue
            storage_key = key
            if kind == "subscriptions":
                storage_key = "subscriptions"
            elif kind == "watch_later":
                storage_key = "playlist:" + (
                    rows[0].get("playlist", "WL")
                    if rows
                    else (key.split("playlist:", 1)[-1] if "playlist:" in key else "WL")
                )
            old = state["snapshots"].setdefault(storage_key, {})
            # The first yt-dlp history snapshot is a silent baseline: its videos
            # were watched at unknown times, possibly already imported by Takeout.
            # A cursor that already holds history observations (written before
            # this marker existed) has been emitting; it is not baselined again.
            # history_baselining keeps a baseline resumed from a mid-pass
            # checkpoint silent although its map is no longer empty.
            baseline = (
                kind == "history"
                and source == "ytdlp"
                and not state.get("history_baselined")
                and (not old or bool(state.get("history_baselining")))
            )
            if baseline:
                state["history_baselining"] = True
            current = set()
            occurrences = {}
            pending = 0
            for row in rows:
                if kind == "history":
                    if source == "takeout":
                        base = digest(
                            [
                                row.get("video_id"),
                                row.get("at"),
                                row["payload"].get("title")
                                if not row.get("video_id")
                                else None,
                            ]
                        )
                        occurrences[base] = occurrences.get(base, 0) + 1
                        identity = f"{base}:{occurrences[base]}"
                    elif row.get("identity"):
                        identity = row["identity"]  # Watcher day or session.
                    else:
                        # The history feed is not a per-view log. One first-seen
                        # observation per video; never invent repeat view times.
                        identity = row["video_id"]
                elif row.get("identity"):
                    identity = row["identity"]  # Watcher Watch Later item.
                else:
                    identity = (
                        row.get("video_id")
                        if kind == "watch_later"
                        else row.get("channel_id")
                    )
                    if not identity:
                        if kind == "subscriptions":
                            raise ValueError("Snapshot item has no stable source ID")
                        identity = "unavailable:" + digest(row["payload"])
                    if kind == "watch_later":
                        occurrences[identity] = occurrences.get(identity, 0) + 1
                        identity = f"{identity}:{occurrences[identity]}"
                eid = storage_key + ":" + identity
                current.add(eid)
                previous = old.get(eid)
                if previous and previous.get("baseline"):
                    # Never emitted, so there is nothing to version: an edited
                    # title or thumbnail must not surface it as a new view.
                    continue
                at = previous["happened_at"] if previous else now
                message = self.normalize(row, kind, source, eid, at)
                # Preserve a missing historical action time across newer snapshots.
                if previous and not row.get("at"):
                    message["fields"]["time_precision"] = previous["fields"][
                        "time_precision"
                    ]
                previous_hash = (
                    previous.get("hash") if previous and kind == "history" else None
                )
                changed = (
                    digest(message) != previous_hash
                    if kind == "history"
                    else message != previous
                )
                if changed:
                    if not baseline:
                        yield message
                        yield from self.links(message, row, kind)
                    old[eid] = (
                        dict(
                            hash=digest(message),
                            happened_at=message["happened_at"],
                            fields={
                                "time_precision": message["fields"]["time_precision"]
                            },
                        )
                        if kind == "history"
                        else message
                    )
                    if baseline:
                        old[eid]["baseline"] = True
                    pending += 1
                    if pending == 100:
                        yield Checkpoint(cursor=copy.deepcopy(state))
                        pending = 0
            if kind == "history" and source == "ytdlp":
                state["history_baselined"] = True
                state.pop("history_baselining", None)
            if complete:
                for eid in sorted(set(old) - current):
                    if old[eid]["fields"]["removed"]:
                        continue
                    message = copy.deepcopy(old[eid])
                    message["fields"]["removed"] = True
                    message["content"] = "Removed: " + message["content"]
                    # Raw payload remains the last actual source item, untouched.
                    yield message
                    old[eid] = message
                    yield Checkpoint(cursor=copy.deepcopy(state))
            if source == "takeout":
                state.setdefault("archives", {})[key] = fingerprint
            yield Checkpoint(cursor=copy.deepcopy(state))
        state.pop("observation_at", None)
        yield Checkpoint(cursor=state)


def main(argv=None):
    run(YouTube, argv)
