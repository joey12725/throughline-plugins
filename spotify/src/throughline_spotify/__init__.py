"""Throughline Spotify connector: one shared generator for export and polling."""

import copy
import hashlib
import json
import logging
import re
import time
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

from .api import ComposioSpotifyAPI, SpotifyAPI
from .normalize import (
    CHANNEL,
    DESCRIPTIONS,
    FIELD_TYPES,
    accept,
    api_play,
    dt,
    export_play,
    iso_ms,
    milliseconds,
    relationships,
)

log = logging.getLogger(__name__)


def history(api, after=None):
    """Read newest window then page backwards, never combine before and after."""
    params = {"limit": 50}
    if after is not None:
        params["after"] = after
    rows, seen = {}, set()
    for _ in range(200):
        page = api.get("me/player/recently-played", params=params) or {}
        items = page.get("items", [])
        if not items:
            break
        for item in items:
            at = milliseconds(dt(item["played_at"]))
            if after is None or at > after:
                rows[(at, (item.get("track") or {}).get("uri"))] = item
        oldest = min(milliseconds(dt(i["played_at"])) for i in items)
        if len(items) < 50 or (after is not None and oldest <= after):
            break
        before = int((page.get("cursors") or {}).get("before") or oldest)
        # Only walk backward. Some servers expose no older data outside last 50.
        before = min(before, oldest)
        if before in seen:
            log.warning(
                "Spotify history cursor did not advance; history may have a gap"
            )
            break
        seen.add(before)
        params = {"limit": 50, "before": before}
    else:
        raise RuntimeError(
            "Spotify history pagination exceeded safety limit; cursor not advanced"
        )
    return [rows[k] for k in sorted(rows, key=lambda k: (k[0], k[1] or ""))]


def playlist(api, context, cache, now):
    uri = context.get("uri")
    if context.get("type") != "playlist" or not uri:
        return None, None
    old = cache.get(uri)
    if old and now - old["at"] < 86400000:
        return old["name"], old["owner"]
    # URI forms include legacy spotify:user:<user>:playlist:<id>.
    match = re.search(r":playlist:([A-Za-z0-9]+)$", uri)
    result = (
        api.get(
            "playlists/" + match[1],
            params={"fields": "name,owner(id,display_name)"},
            missing_ok=True,
        )
        if match
        else None
    )
    result = result or {}
    owner = result.get("owner") or {}
    entry = {
        "at": now,
        "name": result.get("name"),
        "owner": owner.get("display_name") or owner.get("id"),
    }
    cache[uri] = entry
    return entry["name"], entry["owner"]


def observe(raw, pending, now):
    """Return completed prior observation and durable current observation.

    timestamp is state-change time, NOT response time. Use local receive time
    minus progress, with polling uncertainty, and reject pauses/seeks as exact.
    """
    item = (raw or {}).get("item") or {}
    uri = item.get("uri")
    progress = (raw or {}).get("progress_ms")
    valid = (
        uri
        and progress is not None
        and item.get("type", "track") in ("track", "episode")
    )
    same = bool(valid and pending and pending["uri"] == uri)
    if same and progress + 2000 < pending["progress"] and progress < 2000:
        same = False  # repeat-one / same-track restart
    completed = None
    if pending and not same:
        gap = now - pending["last"]
        end = None
        uncertainty = None
        if gap <= 45000:
            expected = pending["start"] + (
                pending["raw"]["item"].get("duration_ms") or 0
            )
            new_start = now - progress if valid else None
            if (
                pending["continuous"]
                and new_start is not None
                and abs(expected - new_start) <= 2000
            ):
                end, uncertainty = iso_ms(expected), 2000
            else:
                end, uncertainty = iso_ms(now), gap
        completed = api_play(
            pending.get("history") or pending["raw"],
            iso_ms(pending["start"]),
            end,
            uncertainty,
        )
        if pending.get("history"):
            completed[0]["payload"] = {
                "history": pending["history"],
                "playback": pending["raw"],
            }
            completed[0]["fields"].update(
                device=pending["raw"].get("device"),
                shuffle=pending["raw"].get("shuffle_state"),
            )
            if not pending["history"].get("context"):
                context = pending["raw"].get("context") or {}
                completed[0]["fields"].update(
                    context_type=context.get("type"), context_uri=context.get("uri")
                )
    if not valid:
        return completed, None
    playing = bool(raw.get("is_playing"))
    if same:
        elapsed = now - pending["last"]
        continuous = (
            pending["continuous"]
            and playing
            and pending["playing"]
            and elapsed <= 45000
            and abs(progress - pending["progress"] - elapsed) <= 2000
        )
        pending = dict(
            pending, last=now, progress=progress, playing=playing, continuous=continuous
        )
    else:
        pending = {
            "uri": uri,
            "start": now - progress,
            "last": now,
            "progress": progress,
            "playing": playing,
            "continuous": playing,
            "raw": raw,
        }
    return completed, pending


class Spotify(Plugin):
    manifest = Manifest(
        name="spotify",
        version="0.1.0",
        default_interval_s=15,
        channels=[
            Channel(
                name=CHANNEL,
                description="The owner's Spotify track and podcast listening plays",
                fields=[
                    {"name": k, "type": v, "description": DESCRIPTIONS[k]}
                    for k, v in FIELD_TYPES.items()
                ],
            )
        ],
        settings={
            "export_path": Declaration(
                env="SPOTIFY_EXPORT_PATH",
                required=False,
                description="Extracted export directory; optional for API-only sync",
            ),
            "recording": Declaration(
                env="SPOTIFY_RECORDING",
                required=False,
                description="Invented recorded API session JSON; disables network",
            ),
            "token_file": Declaration(
                env="SPOTIFY_TOKEN_FILE",
                required=False,
                description="Private refresh-token JSON file outside the repository",
            ),
            "composio_account": Declaration(
                env="SPOTIFY_COMPOSIO_ACCOUNT",
                required=False,
                description="Optional Composio Spotify connected account ID",
            ),
        },
        secrets={
            "client_id": Declaration(
                env="SPOTIFY_CLIENT_ID",
                required=False,
                description="Spotify application client ID",
            ),
            "client_secret": Declaration(
                env="SPOTIFY_CLIENT_SECRET",
                required=False,
                description="Spotify application client secret",
            ),
            "refresh_token": Declaration(
                env="SPOTIFY_REFRESH_TOKEN",
                required=False,
                description="Initial refresh token; token file preferred for rotation",
            ),
            "composio_mcp_url": Declaration(
                env="SPOTIFY_COMPOSIO_MCP_URL",
                required=False,
                description="Composio MCP endpoint URL for Spotify access",
            ),
            "composio_mcp_token": Declaration(
                env="SPOTIFY_COMPOSIO_MCP_TOKEN",
                required=False,
                description="Composio MCP bearer token for Spotify access",
            ),
        },
        link_types=["consumed", "played_from", "part_of", "created_by"],
    )

    def source(self):
        if self.settings["recording"]:
            return Recording(self.settings["recording"])
        token = self.secrets["refresh_token"]
        if self.settings["token_file"]:
            token = json.loads(
                Path(self.settings["token_file"]).read_text(encoding="utf-8")
            ).get("refresh_token")
        if all((self.secrets["client_id"], self.secrets["client_secret"], token)):
            return SpotifyAPI(
                self.secrets["client_id"],
                self.secrets["client_secret"],
                token,
                self.settings["token_file"],
            )
        if self.secrets["composio_mcp_url"] and self.secrets["composio_mcp_token"]:
            return ComposioSpotifyAPI(
                self.secrets["composio_mcp_url"],
                self.secrets["composio_mcp_token"],
                account=self.settings["composio_account"],
            )
        if self.secrets["composio_mcp_url"] or self.secrets["composio_mcp_token"]:
            raise ValueError(
                "Composio Spotify sync requires both SPOTIFY_COMPOSIO_MCP_URL and SPOTIFY_COMPOSIO_MCP_TOKEN"
            )
        raise ValueError(
            "API sync requires SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET and a refresh token (run throughline-spotify-auth), or SPOTIFY_COMPOSIO_MCP_URL and SPOTIFY_COMPOSIO_MCP_TOKEN"
        )

    def records(self, cursor, mode):
        state = copy.deepcopy(cursor)
        ledger = state.setdefault("plays", {})
        cache = state.setdefault("playlists", {})
        index = {}
        for key, entry in ledger.items():
            if not entry.get("alias"):
                index.setdefault(entry["uri"], []).append(key)

        def emit(play):
            if play and accept(play, ledger, index):
                yield play[0]
                yield from relationships(play)

        folder = self.settings["export_path"]
        if folder:
            root = Path(folder)
            if not root.is_dir():
                raise ValueError(
                    "SPOTIFY_EXPORT_PATH must be an extracted export directory"
                )
            paths = sorted(
                {
                    *root.rglob("Streaming_History_Audio_*.json"),
                    *root.rglob("StreamingHistory*.json"),
                }
            )
            if not paths:
                raise ValueError(
                    "No supported Spotify streaming-history JSON files found"
                )
            # Content-derived IDs and the durable ledger tolerate renamed or added files.
            for path in paths:
                data = path.read_bytes()
                fingerprint = hashlib.sha256(data).hexdigest()
                offsets = state.setdefault("exports", {})
                offset = offsets.get(fingerprint, 0)
                records = json.loads(data.decode("utf-8-sig"))
                if not isinstance(records, list):
                    raise ValueError("Spotify export must contain an array")
                for position, raw in enumerate(records):
                    if position < offset:
                        continue
                    yield from emit(export_play(raw))
                    offsets[fingerprint] = position + 1
                    if (position + 1) % 500 == 0:
                        yield Checkpoint(cursor=copy.deepcopy(state))
                yield Checkpoint(cursor=copy.deepcopy(state))
        # Export-only operation intentionally needs no Spotify account access.
        has_api = bool(
            self.settings["recording"]
            or self.secrets["client_id"]
            or self.secrets["composio_mcp_url"]
            or self.secrets["composio_mcp_token"]
        )
        if not has_api:
            if not folder:
                raise ValueError("Set SPOTIFY_EXPORT_PATH or Spotify API credentials")
            return
        api = self.source()
        try:
            raw = api.get("me/player", params={"additional_types": "track,episode"})
            now = getattr(api, "now", None) or int(time.time() * 1000)
            completed, pending = observe(raw, state.get("pending"), now)
            state["pending"] = pending
            plays = []
            observations = state.setdefault("recent_observations", {})
            observations = {
                key: value
                for key, value in observations.items()
                if now - value["seen"] <= 600000
            }
            state["recent_observations"] = observations
            if completed:
                plays.append(completed)
                message = completed[0]
                observations[message["external_id"]] = dict(
                    uri=message["fields"]["track_uri"],
                    start=milliseconds(dt(message["happened_at"])),
                    seen=now,
                    end=milliseconds(dt(message["ended_at"]))
                    if message["ended_at"]
                    else now,
                    history=(message["payload"].get("history") or {}).get("played_at"),
                )
            # Replay overlap tolerates delayed publication. Matching uses the
            # observed interval: Spotify does not specify start versus end semantics.
            after = state.get("after")
            recent = history(api, max(0, after - 300000) if after is not None else None)
            for row in recent:
                item = row.get("track") or {}
                start = milliseconds(dt(row["played_at"]))
                candidates = [
                    (key, value)
                    for key, value in observations.items()
                    if value["uri"] == item.get("uri")
                    and value["start"] - 5000 <= start <= value["end"] + 5000
                    and value.get("history") in (None, row["played_at"])
                ]
                pending_match = (
                    pending
                    and pending["uri"] == item.get("uri")
                    and pending["start"] - 5000 <= start <= pending["last"] + 5000
                    and (
                        not pending.get("history")
                        or pending["history"]["played_at"] == row["played_at"]
                    )
                )
                # A row can fit two back-to-back plays of one track (the end of
                # one is the start of the next). It is still one of them, never a
                # third play: keep a claim it already holds, otherwise give it to
                # the oldest unclaimed play, since rows are published in order.
                matches = [(value["start"], key) for key, value in candidates]
                if pending_match:
                    matches.append((pending["start"], None))
                if matches:
                    claimed = [
                        match
                        for match in matches
                        if (
                            pending.get("history")
                            if match[1] is None
                            else observations[match[1]].get("history")
                        )
                    ]
                    key = min(claimed or matches, key=lambda match: match[0])[1]
                    if key is None:
                        if abs(start - pending["start"]) <= 5000:
                            pending["start"] = start
                        pending["history"] = row
                    else:
                        observation = observations[key]
                        observation["history"] = row["played_at"]
                        history_key = api_play(row)[0]["external_id"]
                        if history_key != key:
                            ledger[history_key] = {"alias": key}
                    continue
                play = api_play(row)
                if play:
                    plays.append(play)
            # Bound only the short-lived reconciliation cache; the existing
            # cross-export ledger is a separate, documented lifetime index.
            while len(observations) > 128:
                del observations[
                    min(observations, key=lambda k: observations[k]["seen"])
                ]
            for play in plays:
                fields = play[0]["fields"]
                fields["playlist_name"], fields["playlist_owner"] = playlist(
                    api,
                    {"type": fields["context_type"], "uri": fields["context_uri"]},
                    cache,
                    now,
                )
                yield from emit(play)
            if recent:
                oldest = min(milliseconds(dt(r["played_at"])) for r in recent)
                if after is not None and len(recent) >= 50 and oldest > after:
                    # Spotify keeps only the newest 50 plays: a full window
                    # that starts after the last one seen lost the plays
                    # between them for good.
                    log.warning(
                        "Spotify returned a full history window; plays during downtime may be missing"
                    )
                    yield coverage(
                        channel=CHANNEL,
                        status="partial",
                        reasons=["source_cap"],
                        gaps=[
                            {
                                "from": iso_ms(after),
                                "to": iso_ms(oldest),
                                "reason": "source_cap",
                            }
                        ],
                        detail={"history_rows": len(recent)},
                    )
                state["after"] = max(
                    state.get("after", 0),
                    max(milliseconds(dt(r["played_at"])) for r in recent),
                )
            yield Checkpoint(cursor=copy.deepcopy(state))
        finally:
            api.close()


class Recording:
    """Obviously invented endpoint responses; no network fallback permitted."""

    def __init__(self, path):
        self.data = json.loads(Path(path).read_text(encoding="utf-8"))
        self.now = self.data["now"]

    def get(self, path, params=None, missing_ok=False):
        if path == "me/player":
            return self.data.get("playback")
        if path == "me/player/recently-played":
            rows = self.data.get("recent", [])
            params = params or {}
            if "after" in params:
                rows = [
                    r
                    for r in rows
                    if milliseconds(dt(r["played_at"])) > int(params["after"])
                ]
            if "before" in params:
                rows = [
                    r
                    for r in rows
                    if milliseconds(dt(r["played_at"])) < int(params["before"])
                ]
            rows = sorted(rows, key=lambda r: r["played_at"], reverse=True)[:50]
            return {
                "items": rows,
                "cursors": {
                    "before": str(milliseconds(dt(rows[-1]["played_at"])))
                    if rows
                    else None
                },
            }
        if path.startswith("playlists/"):
            return self.data.get("playlists", {}).get(path.split("/")[-1])
        raise AssertionError("Unrecorded source endpoint")

    def close(self):
        pass


def main():
    run(Spotify)
