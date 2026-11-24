"""ListenBrainz: the owner's full listen history and recording feedback.

Listens come from GET /1/user/{user}/listens paged forward with min_ts.
With only min_ts set the server returns the `count` listens immediately
after min_ts (exclusive), newest first, so each page is reversed and
yielded oldest first and the cursor (every listen at or before it has been
delivered) only moves forward. A crash resumes from the last checkpoint;
re-read listens deduplicate on their stable external IDs. Only one page is
held in memory at a time.

Feedback (loved/hated recordings) comes from
GET /1/feedback/user/{user}/get-feedback into a reference channel.
"""

import logging

from throughline_sdk import Channel, Checkpoint, Declaration, Manifest, Plugin, Record, run

from .common import (
    ApiError,
    Http,
    compact,
    digest,
    field_list,
    fields_for,
    integer,
    iso,
    link,
    load_transport,
    number,
    parse_time,
)

log = logging.getLogger(__name__)

API = "https://api.listenbrainz.org"
LISTENS = "listenbrainz.listens"
FEEDBACK = "listenbrainz.feedback"
MAX_COUNT = 1000
SHARED = [
    "service", "source_kind", "event_type", "media_type", "title", "creator",
    "creators", "album", "duration_s", "client", "account", "native_id",
    "native_ids", "url", "detail", "timing_basis", "time_precision", "source",
    "author_kind", "mapping_revision",
]
EXTRA = {
    "music_service": ("text", "Streaming service the listen was played on, when submitted"),
    "submission_client": ("text", "Program that submitted the listen to ListenBrainz"),
}
FEEDBACK_EXTRA = {"feedback_score": ("integer", "ListenBrainz feedback score: 1 love, -1 hate")}
LISTEN_FIELDS = field_list(SHARED, EXTRA)
FEEDBACK_FIELDS = field_list(SHARED, FEEDBACK_EXTRA)
LINK_TYPES = ["consumed", "liked", "hated", "created_by", "part_of"]


def clean(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def first(*values):
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def metadata_ids(meta):
    """Recording/release/artist MBIDs: submitted values first, then the
    server's MusicBrainz mapping."""
    info = meta.get("additional_info") or {}
    mapping = meta.get("mbid_mapping") or {}
    artist_mbids = first(info.get("artist_mbids"), mapping.get("artist_mbids")) or []
    return {
        "recording_mbid": first(info.get("recording_mbid"), mapping.get("recording_mbid")),
        "release_mbid": first(info.get("release_mbid"), mapping.get("release_mbid")),
        "artist_mbids": [m for m in artist_mbids if isinstance(m, str) and m] or None,
    }


def credited_artists(meta, ids):
    """[(name, mbid or None)] for created_by links and the creators field."""
    mapping = meta.get("mbid_mapping") or {}
    artists = [
        (clean(a.get("artist_credit_name")), a.get("artist_mbid") or None)
        for a in mapping.get("artists") or []
        if isinstance(a, dict) and clean(a.get("artist_credit_name"))
    ]
    if artists:
        return artists
    name = clean(meta.get("artist_name"))
    if not name:
        return []
    mbids = ids["artist_mbids"] or []
    return [(name, mbids[0] if len(mbids) == 1 else None)]


def work_refs(meta):
    ids = metadata_ids(meta)
    artist = clean(meta.get("artist_name")) or "Unknown artist"
    title = clean(meta.get("track_name")) or "Unknown track"
    mbid = ids["recording_mbid"]
    work = {
        "kind": "work",
        "external_ref": f"musicbrainz:recording:{mbid}"
        if mbid
        else f"listenbrainz:track:{artist.casefold()}|{title.casefold()}",
        "name": title,
    }
    people = [
        {
            "kind": "artist",
            "external_ref": f"musicbrainz:artist:{artist_mbid}"
            if artist_mbid
            else f"listenbrainz:artist:{name.casefold()}",
            "name": name,
        }
        for name, artist_mbid in credited_artists(meta, ids)
    ] or [
        {"kind": "artist", "external_ref": "listenbrainz:artist:unknown artist", "name": artist}
    ]
    album = None
    album_name = clean(meta.get("release_name"))
    if album_name:
        release = ids["release_mbid"]
        album = {
            "kind": "album",
            "external_ref": f"musicbrainz:release:{release}"
            if release
            else f"listenbrainz:album:{artist.casefold()}|{album_name.casefold()}",
            "name": album_name,
        }
    return work, people, album


def track_fields(declared, meta, user, msid=None, **values):
    info = meta.get("additional_info") or {}
    ids = metadata_ids(meta)
    artist = clean(meta.get("artist_name"))
    creators = [name for name, _ in credited_artists(meta, ids)]
    duration = number(info.get("duration_ms"), 1000)
    if duration is None:
        duration = number(info.get("duration"))
    origin = clean(info.get("origin_url"))
    mbid = ids["recording_mbid"]
    url = origin or (f"https://musicbrainz.org/recording/{mbid}" if mbid else None)
    detail = compact(
        {
            k: info.get(k)
            for k in (
                "media_player_version", "submission_client_version", "spotify_id",
                "isrc", "tracknumber", "discnumber", "work_mbids", "release_group_mbid",
                "tags",
            )
        }
    )
    return fields_for(
        declared,
        service="listenbrainz",
        media_type="track",
        title=clean(meta.get("track_name")) or "Unknown track",
        creator=artist,
        creators=creators or None,
        album=clean(meta.get("release_name")),
        duration_s=duration,
        client=clean(info.get("media_player")),
        account=user,
        native_id=msid or mbid,
        native_ids=compact(dict(ids, recording_msid=msid, origin_url=origin)) or None,
        url=url,
        detail=detail or None,
        source="api",
        author_kind="owner",
        mapping_revision=1,
        **values,
    )


def owner(user):
    return {"system": "listenbrainz", "kind": "account", "value": user, "display_name": user}


def by(fields):
    return fields["creator"] or "an unknown artist"


def map_listen(listen, user):
    """One listens item to (message, links); None without a timestamp."""
    when = parse_time(listen.get("listened_at"))
    if when is None:
        return None
    meta = listen.get("track_metadata") or {}
    info = meta.get("additional_info") or {}
    msid = listen.get("recording_msid") or None
    fields = track_fields(
        LISTEN_FIELDS,
        meta,
        user,
        msid=msid,
        source_kind="listen",
        event_type="play",
        music_service=clean(info.get("music_service_name")) or clean(info.get("music_service")),
        submission_client=clean(info.get("submission_client")),
        timing_basis="ListenBrainz listened_at (when playback started, as submitted)",
        time_precision="source_mark",
    )
    ts = int(when.timestamp())
    key = msid or digest(fields["creator"], fields["title"], fields["album"])
    message = {
        "channel": LISTENS,
        "external_id": f"listenbrainz:{user.casefold()}:listen:{ts}:{key}",
        "thread": {
            "external_id": f"listenbrainz:{user.casefold()}:day:{when.date().isoformat()}",
            "title": f"ListenBrainz {when.date().isoformat()}",
        },
        "happened_at": iso(when),
        "author": owner(user),
        "content": f"Listened to {fields['title']} by {by(fields)}"
        + (f" ({fields['album']})" if fields["album"] else ""),
        "fields": fields,
        "payload": listen,
    }
    return message, links_for(message, meta, "consumed")


def map_feedback(item, user):
    when = parse_time(item.get("created"))
    score = integer(item.get("score"))
    if when is None or score not in (1, -1):
        return None
    meta = item.get("track_metadata") or {}
    msid = item.get("recording_msid") or None
    mbid = item.get("recording_mbid") or None
    if mbid and not metadata_ids(meta)["recording_mbid"]:
        meta = dict(meta, additional_info=dict(meta.get("additional_info") or {}, recording_mbid=mbid))
    loved = score == 1
    fields = track_fields(
        FEEDBACK_FIELDS,
        meta,
        user,
        msid=msid,
        source_kind="recording_feedback",
        event_type="liked" if loved else "hated",
        feedback_score=score,
        timing_basis="When the feedback was given",
        time_precision="source_mark",
    )
    key = f"msid:{msid}" if msid else f"mbid:{mbid}" if mbid else digest(fields["creator"], fields["title"])
    message = {
        "channel": FEEDBACK,
        "external_id": f"listenbrainz:{user.casefold()}:feedback:{key}",
        "thread": {
            "external_id": f"listenbrainz:{user.casefold()}:feedback",
            "title": "ListenBrainz feedback",
        },
        "happened_at": iso(when),
        "author": owner(user),
        "content": f"{'Loved' if loved else 'Hated'} {fields['title']} by {by(fields)} on ListenBrainz",
        "fields": fields,
        "payload": item,
    }
    return message, links_for(message, meta, "liked" if loved else "hated")


def links_for(message, meta, relation):
    work, artists, album = work_refs(meta)
    out = [link(relation, {"channel": message["channel"], "external_id": message["external_id"]}, work)]
    out.extend(link("created_by", work, artist) for artist in artists)
    if album:
        out.append(link("part_of", work, album))
    return out


class ListenBrainzApi:
    def __init__(self, token, user, base_url=API, transport=None, delay_s=0.0):
        self.user = user
        self.http = Http(
            base_url.rstrip("/"),
            headers={"Authorization": f"Token {token}"},
            transport=transport,
            delay_s=delay_s,
        )

    def validate(self):
        data = self.http.get_json("/1/validate-token")
        if not data.get("valid"):
            raise ApiError("ListenBrainz rejected the user token (LISTENBRAINZ_TOKEN)")
        return data.get("user_name")

    def listens_after(self, min_ts, count):
        data = self.http.get_json(
            f"/1/user/{self.user}/listens", params={"min_ts": min_ts, "count": count}
        )
        return (data.get("payload") or {}).get("listens") or []

    def feedback(self, offset, count):
        data = self.http.get_json(
            f"/1/feedback/user/{self.user}/get-feedback",
            params={"offset": offset, "count": count, "metadata": "true"},
        )
        return data.get("feedback") or [], int(data.get("total_count") or 0)


class ListenBrainz(Plugin):
    manifest = Manifest(
        name="listenbrainz",
        version="0.1.0",
        channels=[
            Channel(
                name=LISTENS,
                description="Every track the owner submitted as a listen to ListenBrainz (full history)",
                fields=LISTEN_FIELDS,
                conversation_gap_s=3600,
                record_kind="life_event",
                embed=False,
            ),
            Channel(
                name=FEEDBACK,
                description="Recordings the owner loved or hated on ListenBrainz",
                fields=FEEDBACK_FIELDS,
                is_reference=True,
                record_kind="reference",
            ),
        ],
        settings={
            "user": Declaration(env="LISTENBRAINZ_USER", description="ListenBrainz user name"),
            "api_url": Declaration(
                env="LISTENBRAINZ_API_URL", required=False, default=API,
                description="API root (change for a self-hosted ListenBrainz)",
            ),
            "page_size": Declaration(
                env="LISTENBRAINZ_PAGE_SIZE", required=False, default="1000",
                description="Items per API page (max 1000); one checkpoint per page",
            ),
            "lookback_s": Declaration(
                env="LISTENBRAINZ_LOOKBACK_S", required=False, default="86400",
                description="Re-read this many seconds before the cursor on each run to "
                "catch listens submitted late with older timestamps",
            ),
            "delay_s": Declaration(
                env="LISTENBRAINZ_REQUEST_DELAY_S", required=False, default="0.2",
                description="Pause between API requests",
            ),
            "feedback": Declaration(
                env="LISTENBRAINZ_FEEDBACK", required=False, default="true",
                description="Also import loved/hated feedback (true/false)",
            ),
            "test_transport": Declaration(
                env="LISTENBRAINZ_TEST_TRANSPORT", required=False,
                description="Testing only: module:factory returning an httpx transport",
            ),
        },
        secrets={
            "token": Declaration(
                env="LISTENBRAINZ_TOKEN",
                description="ListenBrainz user token (listenbrainz.org/settings)",
            ),
        },
        run_mode="poll",
        default_interval_s=900,
        link_types=LINK_TYPES,
    )
    transport = None

    def api(self):
        return ListenBrainzApi(
            self.secrets["token"].strip(),
            self.settings["user"].strip(),
            base_url=self.settings.get("api_url") or API,
            transport=self.transport or load_transport(self.settings.get("test_transport")),
            delay_s=float(self.settings.get("delay_s") or 0),
        )

    def page_size(self):
        return max(1, min(MAX_COUNT, int(self.settings.get("page_size") or MAX_COUNT)))

    def records(self, cursor, mode):
        api = self.api()
        try:
            token_user = api.validate()
            if token_user and token_user.casefold() != api.user.casefold():
                log.warning("the token belongs to a different ListenBrainz user than LISTENBRAINZ_USER")
            cursor = yield from self.listens(api, dict(cursor or {}))
            if (self.settings.get("feedback") or "true").lower() != "false":
                yield from self.feedback(api, cursor, mode)
        finally:
            api.http.close()

    def listens(self, api, cursor):
        """Page forward from the cursor; returns the final cursor.

        Cursor invariant: every listen with listened_at <= listens_ts has
        been yielded. min_ts is exclusive, and a full page may cut through
        listens sharing its newest second, so that second is held back and
        re-read by the next page (min_ts = newest - 1)."""
        count = self.page_size()
        high = int(cursor.get("listens_ts") or 0)
        lookback = max(0, int(self.settings.get("lookback_s") or 0))
        # min_ts=0 is treated as "unset" by the server, so start at 1.
        after = max(1, high - lookback) if high else 1
        while True:
            page = api.listens_after(after, count)
            if not page:
                break
            stamps = [integer(item.get("listened_at")) or 0 for item in page]
            newest = max(stamps)
            full = len(page) >= count
            if full and min(stamps) == newest:
                log.warning(
                    "more than %d listens share one second; some may be skipped", count
                )
                full = False
            ordered = sorted(zip(stamps, range(len(page))), key=lambda p: (p[0], -p[1]))
            for ts, index in ordered:
                if full and ts == newest:
                    continue
                mapped = map_listen(page[index], api.user)
                if mapped is None:
                    continue
                message, links = mapped
                yield message
                for data in links:
                    yield Record(kind="link", data=data)
            reached = newest - 1 if full else newest
            high = max(high, reached)
            cursor = dict(cursor, listens_ts=high)
            yield Checkpoint(cursor=cursor)
            if not full:
                break
            after = reached
        return cursor

    def feedback(self, api, cursor, mode):
        count = self.page_size()
        known = None if mode == "backfill" else cursor.get("feedback_created")
        collected = []
        offset = 0
        while True:
            items, total = api.feedback(offset, count)
            collected.extend(items)
            offset += len(items)
            oldest = min((integer(i.get("created")) or 0 for i in items), default=0)
            if not items or offset >= total or (known and oldest and oldest < int(known)):
                break
        newest = int(known or 0)
        mapped = [m for m in (map_feedback(i, api.user) for i in collected) if m]
        mapped.sort(key=lambda m: (m[0]["happened_at"], m[0]["external_id"]))
        for index, (message, links) in enumerate(mapped, 1):
            yield message
            for data in links:
                yield Record(kind="link", data=data)
            created = int(parse_time(message["happened_at"]).timestamp())
            newest = max(newest, created)
            if index % count == 0 or index == len(mapped):
                yield Checkpoint(cursor=dict(cursor, feedback_created=newest))


def main():
    run(ListenBrainz)
