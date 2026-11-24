"""Last.fm: the owner's full scrobble history and loved tracks.

Scrobbles come from user.getRecentTracks with a fixed from/to window per
run, read oldest page first, so the cursor (the newest scrobble time
delivered) only moves forward and a crash resumes where it stopped. The
currently playing track is ignored until it is scrobbled. Loved tracks come
from user.getLovedTracks into a reference channel.
"""

import logging
from datetime import datetime, timezone

from throughline_sdk import Channel, Checkpoint, Declaration, Manifest, Plugin, Record, run

from .common import (
    Http,
    ApiError,
    compact,
    digest,
    field_list,
    fields_for,
    iso,
    link,
    load_transport,
    parse_time,
)

log = logging.getLogger(__name__)

API = "https://ws.audioscrobbler.com/2.0/"
SCROBBLES = "lastfm.scrobbles"
LOVED = "lastfm.loved"
SHARED = [
    "service", "source_kind", "event_type", "media_type", "title", "creator",
    "creators", "album", "account", "native_id", "native_ids", "url", "detail",
    "timing_basis", "time_precision", "source", "author_kind", "mapping_revision",
]
EXTRA = {"loved": ("boolean", "The track was loved when it was scrobbled (extended=1)")}
SCROBBLE_FIELDS = field_list(SHARED, EXTRA)
LOVED_FIELDS = field_list(SHARED)
LINK_TYPES = ["consumed", "liked", "created_by", "part_of"]


def as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def text_of(value):
    """Last.fm nests names as {"#text": ...} or {"name": ...} or plain strings."""
    if isinstance(value, dict):
        return (value.get("name") or value.get("#text") or "").strip() or None
    return (value or "").strip() or None


def mbid_of(value):
    return (value.get("mbid") or None) if isinstance(value, dict) else None


def work_refs(track):
    artist = text_of(track.get("artist")) or "Unknown artist"
    title = (track.get("name") or "").strip() or "Unknown track"
    mbid = track.get("mbid") or None
    work = {
        "kind": "work",
        "external_ref": f"musicbrainz:recording:{mbid}"
        if mbid
        else f"lastfm:track:{artist.casefold()}|{title.casefold()}",
        "name": title,
    }
    artist_mbid = mbid_of(track.get("artist"))
    person = {
        "kind": "artist",
        "external_ref": f"musicbrainz:artist:{artist_mbid}"
        if artist_mbid
        else f"lastfm:artist:{artist.casefold()}",
        "name": artist,
    }
    album_name = text_of(track.get("album"))
    album = None
    if album_name:
        album_mbid = mbid_of(track.get("album"))
        album = {
            "kind": "album",
            "external_ref": f"musicbrainz:release:{album_mbid}"
            if album_mbid
            else f"lastfm:album:{artist.casefold()}|{album_name.casefold()}",
            "name": album_name,
        }
    return work, person, album


def track_fields(declared, track, user, **values):
    artist = text_of(track.get("artist"))
    album = text_of(track.get("album"))
    return fields_for(
        declared,
        service="lastfm",
        media_type="track",
        title=(track.get("name") or "").strip() or "Unknown track",
        creator=artist,
        creators=[artist] if artist else None,
        album=album,
        account=user,
        native_id=track.get("url") or None,
        native_ids=compact(
            {
                "recording_mbid": track.get("mbid") or None,
                "artist_mbid": mbid_of(track.get("artist")),
                "release_mbid": mbid_of(track.get("album")),
                "url": track.get("url") or None,
            }
        )
        or None,
        url=track.get("url") or None,
        source="api",
        author_kind="owner",
        mapping_revision=1,
        **values,
    )


def owner(user):
    return {"system": "lastfm", "kind": "account", "value": user, "display_name": user}


def map_scrobble(track, user):
    """One getRecentTracks item to (message, links); None for now-playing."""
    if (track.get("@attr") or {}).get("nowplaying") in ("true", True):
        return None
    when = parse_time((track.get("date") or {}).get("uts"))
    if when is None:
        return None
    fields = track_fields(
        SCROBBLE_FIELDS,
        track,
        user,
        source_kind="scrobble",
        event_type="play",
        loved={"1": True, "0": False}.get(str(track.get("loved"))) if "loved" in track else None,
        timing_basis="Last.fm scrobble timestamp (when playback started)",
        time_precision="source_mark",
    )
    at = iso(when)
    key = digest(fields["creator"], fields["title"], fields["album"])
    message = {
        "channel": SCROBBLES,
        "external_id": f"lastfm:{user.casefold()}:scrobble:{int(when.timestamp())}:{key}",
        "thread": {
            "external_id": f"lastfm:{user.casefold()}:day:{when.date().isoformat()}",
            "title": f"Last.fm {when.date().isoformat()}",
        },
        "happened_at": at,
        "author": owner(user),
        "content": f"Listened to {fields['title']} by {fields['creator'] or 'an unknown artist'}"
        + (f" ({fields['album']})" if fields["album"] else ""),
        "fields": fields,
        "payload": {k: v for k, v in track.items() if k not in ("image", "streamable")},
    }
    return message, links_for(message, track, "consumed")


def map_loved(track, user):
    when = parse_time((track.get("date") or {}).get("uts"))
    if when is None:
        return None
    fields = track_fields(
        LOVED_FIELDS,
        track,
        user,
        source_kind="loved_track",
        event_type="liked",
        timing_basis="When the track was loved",
        time_precision="source_mark",
    )
    message = {
        "channel": LOVED,
        "external_id": f"lastfm:{user.casefold()}:loved:"
        + digest(fields["creator"], fields["title"]),
        "thread": {
            "external_id": f"lastfm:{user.casefold()}:loved",
            "title": "Last.fm loved tracks",
        },
        "happened_at": iso(when),
        "author": owner(user),
        "content": f"Loved {fields['title']} by {fields['creator'] or 'an unknown artist'} on Last.fm",
        "fields": fields,
        "payload": {k: v for k, v in track.items() if k not in ("image", "streamable")},
    }
    return message, links_for(message, track, "liked")


def links_for(message, track, relation):
    work, artist, album = work_refs(track)
    out = [
        link(relation, {"channel": message["channel"], "external_id": message["external_id"]}, work),
        link("created_by", work, artist),
    ]
    if album:
        out.append(link("part_of", work, album))
    return out


class LastFmApi:
    def __init__(self, api_key, user, transport=None, delay_s=0.25):
        self.key = api_key
        self.user = user
        self.http = Http(API, transport=transport, delay_s=delay_s)

    def call(self, method, **params):
        data = self.http.get_json(
            "",
            params=dict(params, method=method, user=self.user, api_key=self.key, format="json"),
        )
        if isinstance(data, dict) and data.get("error"):
            # Last.fm error messages never echo the key.
            raise ApiError(f"Last.fm error {data['error']}: {data.get('message')}")
        return data

    def recent(self, start, end, page, limit):
        data = self.call(
            "user.getrecenttracks",
            limit=limit,
            page=page,
            extended=1,
            **{"from": start, "to": end},
        )["recenttracks"]
        attr = data.get("@attr") or {}
        return as_list(data.get("track")), int(attr.get("totalPages") or 0)

    def loved(self, page, limit):
        data = self.call("user.getlovedtracks", limit=limit, page=page)["lovedtracks"]
        attr = data.get("@attr") or {}
        return as_list(data.get("track")), int(attr.get("totalPages") or 0)


class LastFm(Plugin):
    manifest = Manifest(
        name="lastfm",
        version="0.1.0",
        channels=[
            Channel(
                name=SCROBBLES,
                description="Every track the owner scrobbled to Last.fm (full history)",
                fields=SCROBBLE_FIELDS,
                conversation_gap_s=3600,
                record_kind="life_event",
                embed=False,
            ),
            Channel(
                name=LOVED,
                description="Tracks the owner loved on Last.fm",
                fields=LOVED_FIELDS,
                is_reference=True,
                record_kind="reference",
            ),
        ],
        settings={
            "user": Declaration(env="LASTFM_USER", description="Last.fm user name"),
            "page_size": Declaration(
                env="LASTFM_PAGE_SIZE", required=False, default="200",
                description="Items per API page (max 200); one checkpoint per page",
            ),
            "delay_s": Declaration(
                env="LASTFM_REQUEST_DELAY_S", required=False, default="0.25",
                description="Pause between API requests (Last.fm allows about 5/s)",
            ),
            "loved": Declaration(
                env="LASTFM_LOVED", required=False, default="true",
                description="Also import loved tracks (true/false)",
            ),
            "test_transport": Declaration(
                env="LASTFM_TEST_TRANSPORT", required=False,
                description="Testing only: module:factory returning an httpx transport",
            ),
        },
        secrets={
            "api_key": Declaration(env="LASTFM_API_KEY", description="Last.fm API key"),
        },
        run_mode="poll",
        default_interval_s=900,
        link_types=LINK_TYPES,
    )
    transport = None

    def api(self):
        return LastFmApi(
            self.secrets["api_key"],
            self.settings["user"].strip(),
            transport=self.transport or load_transport(self.settings.get("test_transport")),
            delay_s=float(self.settings.get("delay_s") or 0),
        )

    def records(self, cursor, mode):
        api = self.api()
        try:
            yield from self.scrobbles(api, cursor)
            if (self.settings.get("loved") or "true").lower() != "false":
                yield from self.loved(api, cursor, mode)
        finally:
            api.http.close()

    def scrobbles(self, api, cursor):
        user = api.user
        limit = max(1, min(200, int(self.settings.get("page_size") or 200)))
        # Inclusive lower bound: scrobbles sharing the last second are
        # re-read and deduplicated by their stable external IDs.
        start = int(cursor.get("scrobbles_uts") or 0)
        end = int(datetime.now(timezone.utc).timestamp())
        _, pages = api.recent(start, end, 1, limit)
        newest = start or None
        # Pages are newest first: walk from the last (oldest) page to page 1
        # inside the fixed window so the high-water mark only moves forward.
        for page in range(pages, 0, -1):
            tracks, _ = api.recent(start, end, page, limit)
            for track in reversed(tracks):
                mapped = map_scrobble(track, user)
                if mapped is None:
                    continue
                message, links = mapped
                yield message
                for data in links:
                    yield Record(kind="link", data=data)
                uts = int(datetime.fromisoformat(message["happened_at"]).timestamp())
                newest = max(newest or 0, uts)
            if newest:
                yield Checkpoint(cursor=dict(cursor, scrobbles_uts=newest))
                cursor = dict(cursor, scrobbles_uts=newest)
        self._cursor = cursor

    def loved(self, api, cursor, mode):
        cursor = getattr(self, "_cursor", cursor)
        limit = max(1, min(1000, int(self.settings.get("page_size") or 200)))
        known = None if mode == "backfill" else cursor.get("loved_uts")
        collected = []
        page, pages = 1, 1
        while page <= pages:
            tracks, pages = api.loved(page, limit)
            collected.extend(tracks)
            oldest = min(
                (int((t.get("date") or {}).get("uts") or 0) for t in tracks), default=0
            )
            if known and oldest and oldest < int(known):
                break
            page += 1
        newest = int(known or 0)
        # Oldest first, one checkpoint per page-sized batch.
        mapped = [m for m in (map_loved(t, api.user) for t in reversed(collected)) if m]
        mapped.sort(key=lambda m: m[0]["happened_at"])
        for index, (message, links) in enumerate(mapped, 1):
            yield message
            for data in links:
                yield Record(kind="link", data=data)
            newest = max(newest, int(datetime.fromisoformat(message["happened_at"]).timestamp()))
            if index % limit == 0 or index == len(mapped):
                yield Checkpoint(cursor=dict(cursor, loved_uts=newest))


def main():
    run(LastFm)
