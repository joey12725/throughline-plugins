"""Jellyfin: what the owner played on a Jellyfin server, and what was added
to its libraries.

Play history has two sources, chosen per run (JELLYFIN_HISTORY=auto):

* The Playback Reporting plugin, when installed, records every playback
  (time, item, client, device, method, seconds played). It is read through
  its custom-query endpoint, oldest first, with the newest row time as the
  cursor: every play is delivered.
* Without it, core Jellyfin keeps only per-user item state: LastPlayedDate,
  PlayCount, Played and the resume position. Each run delivers the items
  whose last-played time moved since the previous run, one message per
  (item, last-played time). Plays of the same item between two runs, and
  all plays before the first run except the latest, cannot be recovered;
  every such run reports best_effort coverage with reason source_cap.

Library additions come from the items' DateCreated, newest first down to
the previous high-water mark, like media.consumption's library_added.
"""

import logging
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

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

PLAYS = "jellyfin.plays"
LIBRARY = "jellyfin.library"
SHARED = [
    "service", "source_kind", "event_type", "media_type", "title", "creator",
    "creators", "album", "show", "season", "episode", "year", "duration_s",
    "position_s", "progress", "is_finished", "device", "client", "account",
    "library", "added_at", "native_id", "native_ids", "url", "detail",
    "timing_basis", "time_precision", "source", "author_kind", "mapping_revision",
]
EXTRA_PLAYS = {
    "listened_s": ("number", "Time actually spent playing in seconds (Playback Reporting)"),
    "play_method": ("text", "DirectPlay, DirectStream or Transcode (Playback Reporting)"),
    "play_count": ("integer", "Jellyfin's total play count for the item at the time"),
}
PLAY_FIELDS = field_list(SHARED, EXTRA_PLAYS)
LIBRARY_FIELDS = field_list(SHARED)
LINK_TYPES = ["consumed", "saved", "part_of", "created_by"]
MEDIA_TYPES = {
    "Movie": "movie",
    "Episode": "episode",
    "Series": "show",
    "Season": "season",
    "Audio": "track",
    "MusicAlbum": "album",
    "AudioBook": "audiobook",
    "Book": "book",
    "MusicVideo": "video",
    "Video": "video",
    "TvChannel": "tv",
}
ITEM_FIELDS = "DateCreated,ProviderIds,Genres,ProductionYear,Path,ParentId"
TICKS = 10_000_000
ROW_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d{1,7})?$")
REPORTING_COLUMNS = [
    "DateCreated", "UserId", "ItemId", "ItemType", "ItemName",
    "PlaybackMethod", "ClientName", "DeviceName", "PlayDuration",
]


def compact_id(value):
    return (value or "").replace("-", "").lower()


def creators_of(item):
    names = [a for a in item.get("Artists") or [] if a]
    if not names and item.get("AlbumArtist"):
        names = [item["AlbumArtist"]]
    return names


def item_fields(declared, item, server, user_name, **values):
    fallback_type = values.pop("item_type", None)
    kind = item.get("Type") or fallback_type
    media_type = MEDIA_TYPES.get(kind, (kind or "item").lower())
    names = creators_of(item)
    providers = {k.lower(): v for k, v in (item.get("ProviderIds") or {}).items() if v}
    ticks = integer(item.get("RunTimeTicks"))
    return fields_for(
        declared,
        service="jellyfin",
        media_type=media_type,
        title=item.get("Name") or "Unknown item",
        creator=", ".join(names) or None,
        creators=names or None,
        album=item.get("Album") if media_type == "track" else None,
        show=item.get("SeriesName") if media_type in ("episode", "season") else None,
        season=integer(item.get("ParentIndexNumber")) if media_type == "episode" else None,
        episode=integer(item.get("IndexNumber")) if media_type == "episode" else None,
        year=integer(item.get("ProductionYear")),
        duration_s=ticks / TICKS if ticks else None,
        account=user_name,
        added_at=iso(parse_time(item.get("DateCreated"))),
        native_id=f"jellyfin:{server}:item:{compact_id(item.get('Id'))}" if item.get("Id") else None,
        native_ids=compact(dict(providers, jellyfin_item=compact_id(item.get("Id")) or None)) or None,
        source="api",
        author_kind="owner",
        mapping_revision=1,
        **values,
    )


def describe(fields):
    title = fields["title"]
    if fields["media_type"] == "episode":
        numbers = (
            f" S{fields['season']:02}E{fields['episode']:02}"
            if fields["season"] is not None and fields["episode"] is not None
            else ""
        )
        return f"{fields['show'] or 'an unknown show'}{numbers} - {title}"
    if fields["media_type"] in ("track", "album"):
        return f"{title} by {fields['creator'] or 'an unknown artist'}"
    return title + (f" ({fields['year']})" if fields["year"] else "")


def verb(media_type):
    if media_type in ("track", "album", "audiobook"):
        return "Listened to"
    if media_type == "book":
        return "Read"
    return "Watched"


def work_ref(item, server, fields):
    providers = {k.lower(): v for k, v in (item.get("ProviderIds") or {}).items() if v}
    if providers.get("imdb") and fields["media_type"] in ("movie", "episode", "show"):
        ref = "imdb:" + providers["imdb"]
    elif providers.get("musicbrainztrack"):
        ref = "musicbrainz:recording:" + providers["musicbrainztrack"]
    elif providers.get("musicbrainzalbum") and fields["media_type"] == "album":
        ref = "musicbrainz:release:" + providers["musicbrainzalbum"]
    else:
        ref = f"jellyfin:{server}:item:{compact_id(item.get('Id'))}"
    return {"kind": "work", "external_ref": ref, "name": fields["title"]}


def hierarchy(work, item, server, fields):
    out = []
    if fields["media_type"] == "episode" and item.get("SeriesId"):
        out.append(
            link(
                "part_of",
                work,
                {
                    "kind": "show",
                    "external_ref": f"jellyfin:{server}:item:{compact_id(item['SeriesId'])}",
                    "name": fields["show"] or "Unknown show",
                },
            )
        )
    if fields["media_type"] == "track" and item.get("AlbumId"):
        out.append(
            link(
                "part_of",
                work,
                {
                    "kind": "album",
                    "external_ref": f"jellyfin:{server}:item:{compact_id(item['AlbumId'])}",
                    "name": fields["album"] or "Unknown album",
                },
            )
        )
    for artist in item.get("ArtistItems") or []:
        if artist.get("Id") and artist.get("Name"):
            out.append(
                link(
                    "created_by",
                    work,
                    {
                        "kind": "artist",
                        "external_ref": f"jellyfin:{server}:item:{compact_id(artist['Id'])}",
                        "name": artist["Name"],
                    },
                )
            )
    return out


def day_thread(server, user, at):
    date = datetime.fromisoformat(at).astimezone(timezone.utc).date().isoformat()
    return {"external_id": f"jellyfin:{server}:{user}:day:{date}", "title": f"Jellyfin {date}"}


def owner(user):
    return {
        "system": "jellyfin",
        "kind": "user_id",
        "value": compact_id(user["Id"]),
        "display_name": user.get("Name"),
    }


def map_report_row(row, item, server, user, zone):
    """A Playback Reporting row (dict by column) and the item, if it still
    exists, to (message, links)."""
    local = datetime.fromisoformat(row["DateCreated"].replace(" ", "T")[:26])
    at = local.replace(tzinfo=zone).astimezone(timezone.utc).isoformat()
    listened = number(row.get("PlayDuration"))
    known = item or {"Name": row.get("ItemName"), "Id": row.get("ItemId")}
    fields = item_fields(
        PLAY_FIELDS, known, server, user.get("Name"),
        item_type=row.get("ItemType"),
        source_kind="playback_reporting",
        event_type="play",
        device=row.get("DeviceName") or None,
        client=row.get("ClientName") or None,
        listened_s=listened,
        play_method=row.get("PlaybackMethod") or None,
        play_count=None,
        timing_basis="Playback Reporting row time (when playback started)",
        time_precision="source_mark",
        detail=compact({"reported_name": row.get("ItemName"), "item_missing": item is None or None}) or None,
    )
    if fields["media_type"] == "item":
        fields["media_type"] = MEDIA_TYPES.get(row.get("ItemType"), "item")
    content = f"{verb(fields['media_type'])} {describe(fields) if item else row.get('ItemName') or 'an unknown item'}"
    if listened:
        content += f" ({max(1, round(listened / 60))} min)"
    key = digest(row["DateCreated"], compact_id(row.get("UserId")), compact_id(row.get("ItemId")))
    message = {
        "channel": PLAYS,
        "external_id": f"jellyfin:{server}:play:{key}",
        "thread": day_thread(server, compact_id(user["Id"]), at),
        "happened_at": at,
        "author": owner(user),
        "content": content,
        "fields": fields,
        "payload": {"row": row, "item": item_payload(item)},
    }
    work = work_ref(known, server, fields)
    source = {"channel": PLAYS, "external_id": message["external_id"]}
    return message, [link("consumed", source, work)] + (hierarchy(work, item, server, fields) if item else [])


def item_payload(item):
    if not item:
        return None
    return {k: v for k, v in item.items() if k not in ("ImageTags", "BackdropImageTags", "ImageBlurHashes")}


def map_last_played(item, server, user):
    data = item.get("UserData") or {}
    when = parse_time(data.get("LastPlayedDate"))
    if when is None:
        return None
    at = iso(when)
    position = integer(data.get("PlaybackPositionTicks"))
    percent = number(data.get("PlayedPercentage"))
    played = bool(data.get("Played"))
    fields = item_fields(
        PLAY_FIELDS, item, server, user.get("Name"),
        source_kind="last_played",
        event_type="play" if played or not position else "progress",
        position_s=position / TICKS if position else None,
        progress=1.0 if played and not position else (percent / 100 if percent is not None else None),
        is_finished=played,
        play_count=integer(data.get("PlayCount")),
        timing_basis="Jellyfin LastPlayedDate: the latest play of the item only",
        time_precision="source_mark",
    )
    content = f"{verb(fields['media_type'])} {describe(fields)}"
    if fields["event_type"] == "progress" and fields["progress"] is not None:
        content += f" ({round(fields['progress'] * 100)}% so far)"
    message = {
        "channel": PLAYS,
        "external_id": f"jellyfin:{server}:{compact_id(user['Id'])}:last_played:"
        f"{compact_id(item['Id'])}:{int(when.timestamp())}",
        "thread": day_thread(server, compact_id(user["Id"]), at),
        "happened_at": at,
        "author": owner(user),
        "content": content,
        "fields": fields,
        "payload": item_payload(item),
    }
    work = work_ref(item, server, fields)
    source = {"channel": PLAYS, "external_id": message["external_id"]}
    return message, [link("consumed", source, work)] + hierarchy(work, item, server, fields)


def map_added(item, server, user, library, server_name):
    when = parse_time(item.get("DateCreated"))
    if when is None or not item.get("Id"):
        return None
    fields = item_fields(
        LIBRARY_FIELDS, item, server, user.get("Name"),
        source_kind="library_added",
        event_type="library_added",
        library=library,
        timing_basis="Jellyfin DateCreated: when the item was added to the library",
        time_precision="source_mark",
    )
    server_name = server_name or "Jellyfin"
    content = f"Added {describe(fields)} to {server_name}" + (f" ({library})" if library else "")
    message = {
        "channel": LIBRARY,
        "external_id": f"jellyfin:{server}:added:{compact_id(item['Id'])}",
        "thread": {
            "external_id": f"jellyfin:{server}:additions:{when.date().isoformat()[:7]}",
            "title": f"{server_name} additions {when.date().isoformat()[:7]}",
        },
        "happened_at": iso(when),
        "content": content,
        "fields": fields,
        "payload": item_payload(item),
    }
    work = work_ref(item, server, fields)
    source = {"channel": LIBRARY, "external_id": message["external_id"]}
    return message, [link("saved", source, work)] + hierarchy(work, item, server, fields)


class JellyfinApi:
    def __init__(self, url, key, transport=None):
        auth = (
            'MediaBrowser Client="Throughline", Device="throughline", '
            f'DeviceId="throughline-connector", Version="0.1.0", Token="{key}"'
        )
        self.http = Http(url.rstrip("/") + "/", headers={"Authorization": auth}, transport=transport)

    def get(self, path, **params):
        response = self.http.request("GET", path, params=params or None)
        if response.status_code in (401, 403):
            raise ApiError(f"Jellyfin refused the API key (HTTP {response.status_code}) for {path}")
        if response.status_code >= 400:
            raise ApiError(f"HTTP {response.status_code} for {path}")
        return response.json()

    def has_reporting(self):
        response = self.http.request("GET", "Plugins")
        if response.status_code >= 400:
            return False
        return any("playback reporting" in (p.get("Name") or "").lower() for p in response.json())

    def query(self, sql):
        response = self.http.request(
            "POST",
            "user_usage_stats/submit_custom_query",
            json={"CustomQueryString": sql, "ReplaceUserId": False},
        )
        if response.status_code >= 400:
            raise ApiError(f"Playback Reporting query failed (HTTP {response.status_code})")
        data = response.json()
        columns = data.get("colums") or data.get("columns") or []
        return [dict(zip(columns, row)) for row in data.get("results") or []]

    def items(self, user_id, **params):
        return self.get(f"Users/{user_id}/Items", **params)


class Jellyfin(Plugin):
    manifest = Manifest(
        name="jellyfin",
        version="0.1.0",
        channels=[
            Channel(
                name=PLAYS,
                description=(
                    "Movies, episodes, music and audiobooks the owner played on Jellyfin "
                    "(every play with the Playback Reporting plugin, else the latest play per item)"
                ),
                fields=PLAY_FIELDS,
                record_kind="life_event",
                conversation_gap_s=3600,
                embed=False,
            ),
            Channel(
                name=LIBRARY,
                description="Movies, shows, episodes, albums and books added to the Jellyfin libraries",
                fields=LIBRARY_FIELDS,
                record_kind="life_event",
                embed=False,
            ),
        ],
        settings={
            "url": Declaration(env="JELLYFIN_URL", description="Server URL, e.g. http://jellyfin.local:8096"),
            "user": Declaration(env="JELLYFIN_USER", description="Jellyfin user name whose plays are imported"),
            "history": Declaration(
                env="JELLYFIN_HISTORY", required=False, default="auto",
                description="auto, playback_reporting or last_played",
            ),
            "timezone": Declaration(
                env="JELLYFIN_TIMEZONE", required=False, default="UTC",
                description="IANA zone of the server clock (Playback Reporting stores local times)",
            ),
            "added_types": Declaration(
                env="JELLYFIN_ADDED_TYPES", required=False,
                default="Movie,Series,Episode,MusicAlbum,AudioBook,Book",
                description="Item types reported as library additions ('' to disable)",
            ),
            "played_types": Declaration(
                env="JELLYFIN_PLAYED_TYPES", required=False,
                default="Movie,Episode,Audio,AudioBook,MusicVideo,Video",
                description="Item types read for last-played history",
            ),
            "page_size": Declaration(
                env="JELLYFIN_PAGE_SIZE", required=False, default="500",
                description="Rows or items per request; one checkpoint per page",
            ),
            "test_transport": Declaration(
                env="JELLYFIN_TEST_TRANSPORT", required=False,
                description="Testing only: module:factory returning an httpx transport",
            ),
        },
        secrets={"api_key": Declaration(env="JELLYFIN_API_KEY", description="API key (Dashboard > API Keys)")},
        run_mode="poll",
        default_interval_s=900,
        link_types=LINK_TYPES,
    )
    transport = None

    def records(self, cursor, mode):
        api = JellyfinApi(
            self.settings["url"],
            self.secrets["api_key"],
            transport=self.transport or load_transport(self.settings.get("test_transport")),
        )
        try:
            info = api.get("System/Info")
            server = compact_id(info.get("Id")) or "server"
            wanted = self.settings["user"].strip().casefold()
            user = next(
                (u for u in api.get("Users") if (u.get("Name") or "").casefold() == wanted), None
            )
            if user is None:
                raise ApiError("JELLYFIN_USER not found on the server")
            state = dict(cursor)
            history = (self.settings.get("history") or "auto").strip()
            if history == "auto":
                history = "playback_reporting" if api.has_reporting() else "last_played"
            if history == "playback_reporting":
                yield from self.reporting(api, server, user, state)
            elif history == "last_played":
                yield from self.last_played(api, server, user, state)
            else:
                raise ValueError("JELLYFIN_HISTORY must be auto, playback_reporting or last_played")
            yield from self.added(api, server, user, state, info.get("ServerName"))
        finally:
            api.http.close()

    @property
    def page_size(self):
        return max(1, int(self.settings.get("page_size") or 500))

    def lookup(self, api, user, ids):
        if not ids:
            return {}
        data = api.items(
            user["Id"], Ids=",".join(sorted(ids)), Fields=ITEM_FIELDS, EnableUserData="false"
        )
        return {compact_id(i["Id"]): i for i in data.get("Items") or []}

    def reporting(self, api, server, user, state):
        zone = ZoneInfo(self.settings.get("timezone") or "UTC")
        after = state.get("reporting_after") or "0001-01-01 00:00:00"
        uid = compact_id(user["Id"])
        while True:
            if not ROW_TIME.match(after):
                raise ValueError("invalid Playback Reporting cursor")
            rows = api.query(
                f"SELECT {', '.join(REPORTING_COLUMNS)} FROM PlaybackActivity "
                f"WHERE DateCreated > '{after}' ORDER BY DateCreated LIMIT {self.page_size}"
            )
            fresh = [r for r in rows if str(r.get("DateCreated")) > after]
            mine = [r for r in fresh if compact_id(r.get("UserId")) == uid]
            items = self.lookup(api, user, {compact_id(r.get("ItemId")) for r in mine if r.get("ItemId")})
            for row in mine:
                message, links = map_report_row(row, items.get(compact_id(row.get("ItemId"))), server, user, zone)
                yield message
                for data in links:
                    yield Record(kind="link", data=data)
            if fresh:
                after = max(str(r["DateCreated"]) for r in fresh)
                state["reporting_after"] = after
                yield Checkpoint(cursor=dict(state))
            if len(rows) < self.page_size or not fresh:
                break

    def last_played(self, api, server, user, state):
        mark = state.get("last_played_at")
        collected, start = [], 0
        while True:
            data = api.items(
                user["Id"],
                Recursive="true",
                IncludeItemTypes=self.settings.get("played_types") or "",
                SortBy="DatePlayed",
                SortOrder="Descending",
                Fields=ITEM_FIELDS,
                EnableUserData="true",
                StartIndex=start,
                Limit=self.page_size,
            )
            page = data.get("Items") or []
            stop = False
            for item in page:
                when = parse_time((item.get("UserData") or {}).get("LastPlayedDate"))
                if when is None or (mark and iso(when) < mark):
                    stop = True
                    break
                collected.append(item)
            start += len(page)
            if stop or not page or start >= int(data.get("TotalRecordCount") or 0):
                break
        mapped = [m for m in (map_last_played(i, server, user) for i in collected) if m]
        mapped.sort(key=lambda m: (m[0]["happened_at"], m[0]["external_id"]))
        newest = mark
        for index, (message, links) in enumerate(mapped, 1):
            yield message
            for data in links:
                yield Record(kind="link", data=data)
            newest = max(newest or "", message["happened_at"])
            if index % self.page_size == 0 or index == len(mapped):
                state["last_played_at"] = newest
                yield Checkpoint(cursor=dict(state))
        yield coverage(
            channel=PLAYS,
            scope_key=compact_id(user["Id"]),
            status="best_effort",
            reasons=["source_cap"],
            detail={
                "source": "last_played",
                "note": "core Jellyfin keeps only the latest play per item; repeat plays between "
                "runs and plays before the first run are not recoverable (install the "
                "Playback Reporting plugin for full history)",
                "items": len(mapped),
            },
        )

    def added(self, api, server, user, state, server_name):
        types = (self.settings.get("added_types") or "").strip()
        if not types:
            return
        views = [
            v
            for v in api.get(f"Users/{user['Id']}/Views").get("Items") or []
            if v.get("CollectionType") not in ("playlists", "boxsets", "livetv", "folders")
        ]
        marks = dict(state.get("added_at") or {})
        for view in sorted(views, key=lambda v: compact_id(v.get("Id"))):
            library_id = compact_id(view.get("Id"))
            mark = marks.get(library_id)
            collected, start = [], 0
            while True:
                data = api.items(
                    user["Id"],
                    ParentId=view["Id"],
                    Recursive="true",
                    IncludeItemTypes=types,
                    SortBy="DateCreated",
                    SortOrder="Descending",
                    Fields=ITEM_FIELDS,
                    EnableUserData="false",
                    StartIndex=start,
                    Limit=self.page_size,
                )
                page = data.get("Items") or []
                stop = False
                for item in page:
                    when = parse_time(item.get("DateCreated"))
                    if when is not None and mark and iso(when) < mark:
                        stop = True
                        break
                    collected.append(item)
                start += len(page)
                if stop or not page or start >= int(data.get("TotalRecordCount") or 0):
                    break
            mapped = [
                m
                for m in (map_added(i, server, user, view.get("Name"), server_name) for i in collected)
                if m
            ]
            mapped.sort(key=lambda m: (m[0]["happened_at"], m[0]["external_id"]))
            newest = mark
            for index, (message, links) in enumerate(mapped, 1):
                yield message
                for data in links:
                    yield Record(kind="link", data=data)
                newest = max(newest or "", message["happened_at"])
                if index % self.page_size == 0 or index == len(mapped):
                    marks[library_id] = newest
                    state["added_at"] = dict(marks)
                    yield Checkpoint(cursor=dict(state))


def main():
    run(Jellyfin)
