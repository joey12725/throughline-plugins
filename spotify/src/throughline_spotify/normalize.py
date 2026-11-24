"""Spotify play normalization, source relationships, and conservative reconciliation."""

import hashlib
from datetime import UTC, datetime, timedelta

from throughline_sdk import Record

CHANNEL = "music.spotify"
FIELD_TYPES = {
    "track_name": "text",
    "artist_names": "json",
    "album_name": "text",
    "track_uri": "text",
    "album_uri": "text",
    "ms_played": "integer",
    "duration_ms": "integer",
    "context_type": "text",
    "context_uri": "text",
    "playlist_name": "text",
    "playlist_owner": "text",
    "skipped": "boolean",
    "shuffle": "boolean",
    "offline": "boolean",
    "reason_start": "text",
    "reason_end": "text",
    "device": "json",
    "platform": "text",
    "source": "text",
    "timing_basis": "text",
    "end_uncertainty_ms": "integer",
}
DESCRIPTIONS = {
    "track_name": "Track title, or episode title for podcasts",
    "artist_names": "Ordered artist names; empty for unknown artists or podcasts",
    "album_name": "Album title, or podcast show title",
    "track_uri": "Spotify track or episode URI; null for short exports without IDs",
    "album_uri": "Spotify album URI when supplied",
    "ms_played": "Actual listening milliseconds from export; null when not known",
    "duration_ms": "Full item duration; distinct from time listened",
    "context_type": "Playback context: playlist, album, artist, show, or null",
    "context_uri": "Original playback context URI, even if inaccessible",
    "playlist_name": "Resolved playlist name; null for inaccessible/deleted playlists",
    "playlist_owner": "Playlist owner display name or account ID when supplied",
    "skipped": "Source skip flag; null when unavailable",
    "shuffle": "Source shuffle flag; null when unavailable",
    "offline": "Source offline flag; null when unavailable",
    "reason_start": "Export reason_start; null for API plays",
    "reason_end": "Export reason_end; null for API plays",
    "device": "Playback device object when supplied; null for exports",
    "platform": "Export client platform string when supplied",
    "source": "api or export (first accepted source wins reconciliation)",
    "timing_basis": "export_elapsed, export_minute_precision, api_played_at, or observed_progress",
    "end_uncertainty_ms": "Polling interval bounding an observed end; null if unknown",
}


def dt(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def milliseconds(value):
    return int(value.timestamp() * 1000)


def iso_ms(value):
    return datetime.fromtimestamp(value / 1000, UTC).isoformat()


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()[:32]


def api_play(raw, start=None, end=None, uncertainty=None):
    item = raw.get("track") or raw.get("item") or {}
    if item.get("type", "track") not in ("track", "episode"):
        return None
    if not item:
        return None
    episode = item.get("type") == "episode"
    album = item.get("show" if episode else "album") or {}
    context = raw.get("context") or {}
    artists = item.get("artists") or []
    start = start or raw["played_at"]
    fields = dict.fromkeys(FIELD_TYPES)
    fields.update(
        track_name=item.get("name") or "Unknown item",
        artist_names=[a["name"] for a in artists if a.get("name")],
        album_name=album.get("name"),
        track_uri=item.get("uri"),
        album_uri=None if episode else album.get("uri"),
        duration_ms=item.get("duration_ms"),
        context_type=context.get("type"),
        context_uri=context.get("uri"),
        device=raw.get("device"),
        shuffle=raw.get("shuffle_state"),
        source="api",
        timing_basis="observed_progress" if "item" in raw else "api_played_at",
        end_uncertainty_ms=uncertainty,
    )
    return build(fields, start, end, raw, episode, artists, album)


def export_play(raw):
    short = "endTime" in raw
    end = dt(raw["endTime"] + "+00:00") if short else dt(raw["ts"])
    played = max(0, int(raw.get("msPlayed" if short else "ms_played", 0)))
    start = end - timedelta(milliseconds=played)
    episode = bool(raw.get("episode_name") or raw.get("spotify_episode_uri"))
    name = (
        raw.get("trackName")
        if short
        else raw.get("episode_name" if episode else "master_metadata_track_name")
    )
    artist = (
        raw.get("artistName") if short else raw.get("master_metadata_album_artist_name")
    )
    fields = dict.fromkeys(FIELD_TYPES)
    fields.update(
        track_name=name or "Unknown item",
        artist_names=[artist] if artist else [],
        album_name=raw.get(
            "episode_show_name" if episode else "master_metadata_album_album_name"
        ),
        track_uri=raw.get("spotify_episode_uri" if episode else "spotify_track_uri"),
        ms_played=played,
        skipped=raw.get("skipped"),
        shuffle=raw.get("shuffle"),
        offline=raw.get("offline"),
        reason_start=raw.get("reason_start"),
        reason_end=raw.get("reason_end"),
        platform=raw.get("platform"),
        source="export",
        timing_basis="export_minute_precision" if short else "export_elapsed",
    )
    # An elapsed-listening-derived start is not a wall-clock session start across pauses.
    return build(fields, start.isoformat(), end.isoformat(), raw, episode, [], {})


def build(fields, start, end, raw, episode, artists, album):
    uri = fields["track_uri"]
    # Preserve source precision; don't round independent plays into one event.
    identity = uri or digest(str(fields["track_name"]) + str(fields["artist_names"]))
    anchor = end if fields["source"] == "export" else start
    external_id = (
        fields["source"] + ":" + digest(identity + ":" + str(milliseconds(dt(anchor))))
    )
    title = fields["track_name"]
    content = (
        f"Listened to {title} from {fields['album_name'] or 'Unknown show'}"
        if episode
        else f"Listened to {title} by {', '.join(fields['artist_names']) or 'Unknown artist'}"
    )
    message = dict(
        channel=CHANNEL,
        external_id=external_id,
        thread={"external_id": "day:" + dt(start).astimezone(UTC).date().isoformat()},
        happened_at=start,
        ended_at=end,
        content=content,
        fields=fields,
        payload=raw,
    )
    return message, artists, album


def relationships(play):
    message, artists, album = play
    fields = message["fields"]
    work = {
        "kind": "work",
        "external_ref": fields["track_uri"],
        "name": fields["track_name"],
    }
    endpoint = {"channel": CHANNEL, "external_id": message["external_id"]}

    def link(kind, source, target):
        return Record(
            kind="link",
            data={"type": kind, "from": source, "to": target, "method": "source"},
        )

    if fields["track_uri"]:
        yield link("consumed", endpoint, work)
        if fields["album_uri"]:
            yield link(
                "part_of",
                work,
                {
                    "kind": "album",
                    "external_ref": fields["album_uri"],
                    "name": fields["album_name"],
                },
            )
        for artist in artists:
            if artist.get("uri"):
                yield link(
                    "created_by",
                    work,
                    {
                        "kind": "artist",
                        "external_ref": artist["uri"],
                        "name": artist.get("name"),
                    },
                )
    if fields["context_type"] == "playlist" and fields["context_uri"]:
        yield link(
            "played_from",
            endpoint,
            {
                "kind": "playlist",
                "external_ref": fields["context_uri"],
                "name": fields["playlist_name"],
            },
        )


def accept(play, ledger, index=None):
    """Persistent aliases keep cross-source matches stable without overwriting raw data."""
    message = play[0]
    key = message["external_id"]
    if key in ledger:
        return False
    fields = message["fields"]
    start = milliseconds(dt(message["happened_at"]))
    end = milliseconds(dt(message["ended_at"])) if message["ended_at"] else None
    candidates = []
    if index is None:
        index = {}
        for old_key, old in ledger.items():
            if not old.get("alias"):
                index.setdefault(old["uri"], []).append(old_key)
    if fields["track_uri"] and fields["timing_basis"] != "export_minute_precision":
        for old_key in index.get(fields["track_uri"], []):
            old = ledger[old_key]
            if (
                old.get("alias")
                or old["source"] == fields["source"]
                or old["uri"] != fields["track_uri"]
            ):
                continue
            # Match only end times known on both sides, within two seconds.
            if (
                end is not None
                and old["end"] is not None
                and abs(end - old["end"]) <= 2000
            ):
                if (
                    abs(start - old["start"]) <= 2000
                    and (old.get("uncertainty") or 0) <= 2000
                    and (fields["end_uncertainty_ms"] or 0) <= 2000
                ):
                    candidates.append(old_key)
    if len(candidates) == 1:
        ledger[key] = {"alias": candidates[0]}
        return False
    ledger[key] = {
        "source": fields["source"],
        "uri": fields["track_uri"],
        "start": start,
        "end": end,
        "uncertainty": fields["end_uncertainty_ms"],
    }
    index.setdefault(fields["track_uri"], []).append(key)
    return True
