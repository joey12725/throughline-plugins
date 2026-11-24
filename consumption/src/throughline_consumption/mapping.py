"""Deterministic mapping from raw export records to media.consumption messages.

The same record always maps to the same message and links. Nothing here reads
the clock, the network or plugin state; the plugin applies its small ledgers
(mutable-record ordering, first-seen times) around this module.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .fetch import known_time, ms_iso, parse_time

CHANNEL = "media.consumption"
BOOK_TEXT_CHANNEL = "audiobooks.text"
# Bump when the mapping of an unchanged record changes. Every message carries
# it, so one deliberate re-version of the whole channel follows a bump.
MAPPING_REVISION = 2

FIELDS = {
    "service": ("text", "spotify, plex, audiobookshelf or youtube"),
    "source_kind": ("text", "Service-specific event kind, including history, active_session and library_added"),
    "event_type": (
        "text",
        "play (a play or view), session (a timed listening session), progress, finished, liked or playlist_add",
    ),
    "media_type": ("text", "track, episode, movie, video, audiobook or podcast"),
    "title": ("text", "Track, episode, movie, video or book title"),
    "creator": ("text", "Artists, author, channel or podcast author, comma separated"),
    "creators": ("json", "Ordered creator names when the source lists them"),
    "album": ("text", "Album of a track"),
    "show": ("text", "TV show or podcast of an episode"),
    "series": ("text", "Book series, with sequence when known"),
    "season": ("integer", "Season number of a TV episode"),
    "episode": ("integer", "Episode number of a TV episode"),
    "year": ("integer", "Release year when known"),
    "duration_s": ("number", "Runtime of the work in seconds, not time spent"),
    "position_s": ("number", "Last known playback position in seconds"),
    "start_position_s": ("number", "Playback position at session start in seconds"),
    "progress": ("number", "Fraction of the work completed, 0 to 1"),
    "listened_s": ("number", "Time actually spent listening in seconds"),
    "is_finished": ("boolean", "Source reports the work as finished"),
    "device": ("text", "Device name or model"),
    "client": ("text", "Player application or platform"),
    "account": ("text", "Source account or user name"),
    "library": ("text", "Server library name or ID"),
    "added_at": ("timestamp", "Source timestamp when a work was added to a media library"),
    "plex_rating_key": ("text", "Plex ratingKey for joining additions, sessions and history"),
    "guids": ("json", "Plex primary and provider GUIDs for the work"),
    "playlist_name": ("text", "Playlist name for playlist entries"),
    "playlist_id": ("text", "Source playlist ID"),
    "context_type": (
        "text",
        "Spotify playback context type (playlist, album, artist, show)",
    ),
    "context_uri": ("text", "Spotify playback context URI"),
    "play_method": (
        "text",
        "Audiobookshelf play method (direct play, direct stream, transcode, local)",
    ),
    "is_short": ("boolean", "YouTube Short"),
    "is_music": ("boolean", "YouTube Music view"),
    "is_ad": ("boolean", "YouTube view marked as an ad"),
    "native_id": (
        "text",
        "Primary source ID of the work (URI, GUID, video ID or item ID)",
    ),
    "native_ids": ("json", "All source IDs of the record and work"),
    "url": ("text", "Link to the work when the service has a public URL"),
    "detail": ("json", "Further service-specific values"),
    "timing_basis": ("text", "What happened_at and ended_at mean for this record"),
    "time_precision": (
        "text",
        "source_mark, session, progress, day, estimated (YouTube watcher session) or first_seen",
    ),
    "source": ("text", "api, backup, takeout, youtube_plugin or watcher"),
    "mapping_revision": ("integer", "Mapping revision that produced this message"),
}
BOOK_TEXT_FIELDS = {
    "service": ("text", "audiobookshelf"),
    "ebook_source": ("text", "kavita"),
    "title": ("text", "Book title"),
    "creator": ("text", "Author(s), comma separated"),
    "creators": ("json", "Ordered author names when known"),
    "series": ("text", "Book series, with sequence when known"),
    "chapter_index": ("integer", "1-based chapter or section index within the book"),
    "chapter_title": ("text", "Chapter or section title when known"),
    "part_index": ("integer", "1-based chunk index within the chapter"),
    "part_count": ("integer", "Total chunks in this chapter"),
    "chunk_index": ("integer", "1-based global chunk index within the book"),
    "chunk_count": ("integer", "Total chunks in the book"),
    "char_count": ("integer", "Character length of this chunk"),
    "word_count": ("integer", "Approximate word count of this chunk"),
    "book_format": ("text", "Source ebook format (epub, pdf, txt)"),
    "abs_item_id": ("text", "Audiobookshelf library item ID"),
    "abs_progress": ("number", "Audiobookshelf progress fraction when matched (0 to 1)"),
    "abs_is_finished": ("boolean", "Whether Audiobookshelf marked the audiobook finished"),
    "kavita_series_id": ("integer", "Matched Kavita series ID when known"),
    "kavita_chapter_id": ("integer", "Matched Kavita chapter ID"),
    "kavita_file_path": ("text", "Matched Kavita file path"),
    "kavita_file_hash": ("text", "Stable fingerprint of the matched Kavita file"),
    "match_score": ("number", "Title/author match confidence score (0 to 1)"),
    "match_method": ("text", "How the Kavita ebook was matched"),
    "native_id": ("text", "Primary Audiobookshelf work reference (audiobookshelf:item:<id>)"),
    "mapping_revision": ("integer", "Mapping revision that produced this message"),
}
LINK_TYPES = [
    "consumed",
    "liked",
    "saved",
    "added_to",
    "part_of",
    "created_by",
    "published_by",
]
SERVICE_NAMES = {
    "spotify": "Spotify",
    "plex": "Plex",
    "audiobookshelf": "Audiobookshelf",
    "youtube": "YouTube",
}
PLAY_METHODS = {0: "direct play", 1: "direct stream", 2: "transcode", 3: "local"}


class Mapped:
    """A message, its links, and whether happened_at is a first-seen fallback."""

    def __init__(self, message, links, pinned=False):
        self.message = message
        self.links = links
        self.pinned = pinned


def number(value, scale=1):
    try:
        return float(value) / scale if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def integer(value):
    try:
        return int(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def clock(seconds):
    seconds = int(seconds or 0)
    hours, rest = divmod(seconds, 3600)
    return f"{hours} h {rest // 60} min" if hours else f"{max(1, rest // 60)} min"


def day(moment_iso):
    return parse_time(moment_iso).astimezone(timezone.utc).date().isoformat()


def day_thread(service, at):
    date = day(at)
    return {
        "external_id": f"{service}:day:{date}",
        "title": f"{SERVICE_NAMES[service]} {date}",
    }


def fallback_time(record):
    """First-seen time for a record without a source time; the plugin pins it."""
    moment = known_time(record.get("observed_at")) or known_time(
        record.get("fetched_at")
    )
    if moment is None:
        raise ValueError("record has no source time and no fetched_at")
    return moment.astimezone(timezone.utc).isoformat()


def base_fields(service, event_type, media_type, **values):
    fields = dict.fromkeys(FIELDS)
    fields.update(service=service, event_type=event_type, media_type=media_type)
    fields.update(values)
    fields["mapping_revision"] = MAPPING_REVISION
    return fields


def message(record, external_id, thread, at, content, fields, ended=None, author=None):
    result = {
        "channel": CHANNEL,
        "external_id": external_id,
        "thread": thread,
        "happened_at": at,
        "ended_at": ended,
        "content": content,
        "fields": fields,
        # fetched_at and observed_at describe the export run, not the source,
        # so they stay out of the payload: re-exports hash the same.
        "payload": {
            k: record[k]
            for k in ("kind", "id", "service", "raw", "context")
            if k in record
        },
    }
    if author:
        result["author"] = author
    return result


def link(kind, source, target):
    return {"type": kind, "from": source, "to": target, "method": "source"}


def endpoint(msg):
    return {"channel": CHANNEL, "external_id": msg["external_id"]}


def compact(value):
    return {k: v for k, v in value.items() if v not in (None, "", [], {})}


# ---------------------------------------------------------------- Spotify


def spotify_work(item):
    episode = item.get("type") == "episode"
    artists = [
        a for a in item.get("artists") or [] if isinstance(a, dict) and a.get("name")
    ]
    names = [a["name"] for a in artists]
    album = item.get("show" if episode else "album") or {}
    kind = "episode" if episode else "track"
    url = (item.get("external_urls") or {}).get("spotify") or (
        f"https://open.spotify.com/{kind}/{item['id']}"
        if item.get("id") and not item.get("is_local")
        else None
    )
    creator = ", ".join(names) or (album.get("publisher") if episode else None)
    fields = dict(
        title=item.get("name") or "Unknown item",
        creator=creator,
        creators=names or None,
        album=None if episode else album.get("name"),
        show=album.get("name") if episode else None,
        duration_s=number(item.get("duration_ms"), 1000),
        year=integer(str(album.get("release_date") or "")[:4]) if not episode else None,
        native_id=item.get("uri") or item.get("id"),
        native_ids=compact(
            {
                "uri": item.get("uri"),
                "id": item.get("id"),
                "album_uri": album.get("uri"),
            }
        )
        or None,
        url=url,
        detail=compact(
            {"explicit": item.get("explicit"), "is_local": item.get("is_local")}
        )
        or None,
    )
    return fields, "podcast" if episode else "track", artists, album


def spotify_links(msg, item, artists, album, relation):
    uri = item.get("uri")
    if not uri:
        return []
    work = {
        "kind": "work",
        "external_ref": uri,
        "name": item.get("name") or "Unknown item",
    }
    links = [link(relation, endpoint(msg), work)]
    if album.get("uri"):
        links.append(
            link(
                "part_of",
                work,
                {
                    "kind": "podcast" if item.get("type") == "episode" else "album",
                    "external_ref": album["uri"],
                    "name": album.get("name"),
                },
            )
        )
    for artist in artists:
        if artist.get("uri"):
            links.append(
                link(
                    "created_by",
                    work,
                    {
                        "kind": "artist",
                        "external_ref": artist["uri"],
                        "name": artist["name"],
                    },
                )
            )
    return links


def listen_phrase(fields, media_type):
    if media_type == "podcast":
        return f"{fields['title']} from {fields['show'] or 'an unknown show'}"
    return f"{fields['title']} by {fields['creator'] or 'an unknown artist'}"


def map_spotify_play(record):
    raw = record["raw"]
    item = raw.get("item") or raw.get("track") or {}
    played = known_time(raw.get("played_at"))
    if not item or played is None:
        return None
    work, media_type, artists, album = spotify_work(item)
    context = raw.get("context") or {}
    at = played.isoformat()
    fields = base_fields(
        "spotify",
        "play",
        media_type,
        **work,
        context_type=context.get("type"),
        context_uri=context.get("uri"),
        timing_basis="Spotify played_at mark; listening start and stop are not reported",
        time_precision="source_mark",
        source="api",
    )
    msg = message(
        record,
        "spotify:play:" + record["id"],
        day_thread("spotify", at),
        at,
        "Listened to " + listen_phrase(fields, media_type),
        fields,
    )
    return Mapped(msg, spotify_links(msg, item, artists, album, "consumed"))


def map_spotify_saved(record):
    raw = record["raw"]
    item = raw.get("item") or raw.get("track") or {}
    if not item:
        return None
    work, media_type, artists, album = spotify_work(item)
    added = known_time(raw.get("added_at"))
    at = added.isoformat() if added else fallback_time(record)
    fields = base_fields(
        "spotify",
        "liked",
        media_type,
        **work,
        timing_basis="added_at of the liked song"
        if added
        else "first export that contained the like",
        time_precision="source_mark" if added else "first_seen",
        source="backup" if "observed_at" in record else "api",
    )
    msg = message(
        record,
        "spotify:liked:" + record["id"],
        {"external_id": "spotify:liked", "title": "Spotify liked songs"},
        at,
        "Liked " + listen_phrase(fields, media_type) + " on Spotify",
        fields,
    )
    return Mapped(
        msg, spotify_links(msg, item, artists, album, "liked"), pinned=added is None
    )


def map_spotify_playlist_item(record):
    raw = record["raw"]
    context = record.get("context") or {}
    item = raw.get("item") or raw.get("track") or {}
    if not item or not context.get("playlist_id"):
        return None
    work, media_type, artists, album = spotify_work(item)
    added = known_time(raw.get("added_at"))
    at = added.isoformat() if added else fallback_time(record)
    name = context.get("playlist_name") or "an unnamed playlist"
    fields = base_fields(
        "spotify",
        "playlist_add",
        media_type,
        **work,
        playlist_name=context.get("playlist_name"),
        playlist_id=context["playlist_id"],
        account=context.get("playlist_owner_name") or context.get("playlist_owner_id"),
        timing_basis="added_at of the playlist entry"
        if added
        else "first export that contained the entry",
        time_precision="source_mark" if added else "first_seen",
        source="backup" if "observed_at" in record else "api",
    )
    added_by = (raw.get("added_by") or {}).get("id")
    author = (
        {"system": "spotify", "kind": "user_id", "value": added_by}
        if added_by
        else None
    )
    msg = message(
        record,
        "spotify:playlist_item:" + record["id"],
        {
            "external_id": f"spotify:playlist:{context['playlist_id']}",
            "title": f"Spotify playlist {name}",
        },
        at,
        f"Added {listen_phrase(fields, media_type)} to playlist {name}",
        fields,
        author=author,
    )
    links = spotify_links(msg, item, artists, album, "saved")
    links.append(
        link(
            "added_to",
            endpoint(msg),
            {
                "kind": "playlist",
                "external_ref": f"spotify:playlist:{context['playlist_id']}",
                "name": context.get("playlist_name"),
            },
        )
    )
    return Mapped(msg, links, pinned=added is None)


# ---------------------------------------------------------------- Plex


def plex_guids(item):
    values = [item.get("guid")]
    values.extend(
        guid.get("id")
        for guid in item.get("Guid") or []
        if isinstance(guid, dict)
    )
    return list(dict.fromkeys(str(value) for value in values if value))


def plex_work(item, server, fallback):
    return {
        "kind": "work",
        "external_ref": item.get("guid")
        or f"plex:{server}:{item.get('ratingKey') or fallback}",
        "name": item.get("title") or "Unknown title",
    }


def plex_hierarchy_links(work, item, server):
    if item.get("type") != "episode":
        return []
    links = []
    show = None
    if item.get("grandparentGuid") or item.get("grandparentRatingKey"):
        show = {
            "kind": "work",
            "external_ref": item.get("grandparentGuid")
            or f"plex:{server}:{item['grandparentRatingKey']}",
            "name": item.get("grandparentTitle") or "Unknown show",
        }
        links.append(link("part_of", work, show))
    if item.get("parentGuid") or item.get("parentRatingKey"):
        season = {
            "kind": "work",
            "external_ref": item.get("parentGuid")
            or f"plex:{server}:{item['parentRatingKey']}",
            "name": item.get("parentTitle")
            or (
                f"Season {item['parentIndex']}"
                if item.get("parentIndex") is not None
                else "Unknown season"
            ),
        }
        links.append(link("part_of", work, season))
        if show:
            links.append(link("part_of", season, show))
    return links


def map_plex(record):
    row = record["raw"]
    context = record.get("context") or {}
    meta = context.get("metadata") or {}
    item = {**meta, **{k: v for k, v in row.items() if v is not None}}
    viewed = integer(row.get("viewedAt"))
    if viewed is None:
        return None
    server = context.get("server") or "unknown"
    kind = item.get("type") or "unknown"
    media_type = {
        "movie": "movie",
        "episode": "episode",
        "track": "track",
        "clip": "video",
    }.get(kind, kind)
    title = item.get("title") or "Unknown title"
    account = context.get("account") or {}
    device = context.get("device") or {}
    episode = kind == "episode"
    track = kind == "track"
    creator = (
        (item.get("originalTitle") or item.get("grandparentTitle")) if track else None
    )
    at = datetime.fromtimestamp(viewed, timezone.utc).isoformat()
    fields = base_fields(
        "plex",
        "play",
        media_type,
        source_kind="history",
        title=title,
        creator=creator,
        album=item.get("parentTitle") if track else None,
        show=item.get("grandparentTitle") if episode else None,
        season=integer(item.get("parentIndex")) if episode else None,
        episode=integer(item.get("index")) if episode else None,
        year=integer(item.get("year")),
        duration_s=number(item.get("duration"), 1000),
        device=device.get("name"),
        client=device.get("platform"),
        account=account.get("name") or account.get("id"),
        library=item.get("librarySectionTitle")
        or (
            str(item["librarySectionID"])
            if item.get("librarySectionID") is not None
            else None
        ),
        native_id=item.get("guid")
        or (str(item["ratingKey"]) if item.get("ratingKey") is not None else None),
        native_ids=compact(
            {
                "server": server,
                "history_key": row.get("historyKey"),
                "rating_key": str(item["ratingKey"])
                if item.get("ratingKey") is not None
                else None,
                "guid": item.get("guid"),
                "show_guid": item.get("grandparentGuid"),
            }
        )
        or None,
        plex_rating_key=str(item["ratingKey"])
        if item.get("ratingKey") is not None
        else None,
        guids=plex_guids(item) or None,
        timing_basis="Plex viewedAt played mark; start and stop are not reported",
        time_precision="source_mark",
        source="api",
    )
    if episode:
        numbers = (
            f" S{fields['season']:02}E{fields['episode']:02}"
            if fields["season"] is not None and fields["episode"] is not None
            else ""
        )
        content = f"Watched {fields['show'] or 'an unknown show'}{numbers} - {title}"
    elif track:
        content = f"Listened to {title} by {creator or 'an unknown artist'}"
    else:
        content = f"Watched {title}" + (
            f" ({fields['year']})" if fields["year"] is not None else ""
        )
    author = None
    if account.get("id"):
        author = {"system": "plex", "kind": "account_id", "value": str(account["id"])}
        if account.get("name"):
            author["display_name"] = account["name"]
    msg = message(
        record,
        "plex:" + record["id"],
        day_thread("plex", at),
        at,
        content,
        fields,
        author=author,
    )
    work = plex_work(item, server, record["id"])
    links = [link("consumed", endpoint(msg), work)]
    links.extend(plex_hierarchy_links(work, item, server))
    return Mapped(msg, links)


def map_plex_session(record):
    row = record["raw"]
    context = record.get("context") or {}
    item = {**(context.get("metadata") or {}), **row}
    started = integer(row.get("_sessionStart"))
    seen = integer(row.get("_sessionSeen"))
    ended = integer(row.get("_sessionEnd"))
    if started is None or seen is None:
        return None
    server = context.get("server") or "unknown"
    account = context.get("account") or {}
    player = row.get("Player") or {}
    if isinstance(player, list):
        player = player[0] if player else {}
    kind = item.get("type") or "unknown"
    episode = kind == "episode"
    track = kind == "track"
    title = item.get("title") or "Unknown title"
    creator = (
        item.get("originalTitle") or item.get("grandparentTitle") if track else None
    )
    guids = plex_guids(item)
    fields = base_fields(
        "plex",
        "session",
        {"clip": "video"}.get(kind, kind),
        source_kind="active_session",
        title=title,
        creator=creator,
        album=item.get("parentTitle") if track else None,
        show=item.get("grandparentTitle") if episode else None,
        season=integer(item.get("parentIndex")) if episode else None,
        episode=integer(item.get("index")) if episode else None,
        year=integer(item.get("year")),
        duration_s=number(item.get("duration"), 1000),
        position_s=number(row.get("viewOffset"), 1000),
        start_position_s=number(row.get("_initialOffset"), 1000),
        device=player.get("title"),
        client=player.get("product"),
        account=account.get("name") or account.get("id"),
        library=item.get("librarySectionTitle")
        or (
            str(item["librarySectionID"])
            if item.get("librarySectionID") is not None
            else None
        ),
        plex_rating_key=str(item["ratingKey"])
        if item.get("ratingKey") is not None
        else None,
        guids=guids or None,
        native_id=item.get("guid")
        or (str(item["ratingKey"]) if item.get("ratingKey") is not None else None),
        native_ids=compact(
            {
                "server": server,
                "session_id": (row.get("Session") or {}).get("id")
                if isinstance(row.get("Session"), dict)
                else None,
                "rating_key": str(item["ratingKey"])
                if item.get("ratingKey") is not None
                else None,
                "guid": item.get("guid"),
                "guids": guids or None,
            }
        )
        or None,
        detail=compact(
            {
                "playback_state": player.get("state"),
                "position_observed_at": datetime.fromtimestamp(
                    seen, timezone.utc
                ).isoformat(),
            }
        )
        or None,
        timing_basis="first observed poll to last observed playing poll",
        time_precision="first_seen",
        source="api",
    )
    if episode:
        numbers = (
            f" S{fields['season']:02}E{fields['episode']:02}"
            if fields["season"] is not None and fields["episode"] is not None
            else ""
        )
        content = f"Watching {fields['show'] or 'an unknown show'}{numbers} - {title}"
    elif track:
        content = f"Listening to {title} by {creator or 'an unknown artist'}"
    else:
        content = f"Watching {title}"
    author = None
    if account.get("id"):
        author = {"system": "plex", "kind": "account_id", "value": str(account["id"])}
        if account.get("name"):
            author["display_name"] = account["name"]
    at = datetime.fromtimestamp(started, timezone.utc).isoformat()
    msg = message(
        record,
        "plex:" + record["id"],
        day_thread("plex", at),
        at,
        content,
        fields,
        ended=datetime.fromtimestamp(ended, timezone.utc).isoformat()
        if ended is not None
        else None,
        author=author,
    )
    work = plex_work(item, server, record["id"])
    links = [link("consumed", endpoint(msg), work)]
    links.extend(plex_hierarchy_links(work, item, server))
    return Mapped(msg, links)


def map_plex_library_added(record):
    item = record["raw"]
    context = record.get("context") or {}
    section = context.get("section") or {}
    added = integer(item.get("addedAt"))
    if added is None:
        return None
    server = context.get("server") or "unknown"
    kind = item.get("type") or "unknown"
    episode = kind == "episode"
    title = item.get("title") or "Unknown title"
    guids = plex_guids(item)
    at = datetime.fromtimestamp(added, timezone.utc).isoformat()
    fields = base_fields(
        "plex",
        "library_added",
        kind,
        source_kind="library_added",
        title=title,
        creator=item.get("parentTitle") if kind == "album" else None,
        show=item.get("grandparentTitle") if episode else None,
        season=integer(item.get("parentIndex")) if episode else None,
        episode=integer(item.get("index")) if episode else None,
        year=integer(item.get("year")),
        duration_s=number(item.get("duration"), 1000),
        library=section.get("title") or item.get("librarySectionTitle") or section.get("id"),
        added_at=at,
        plex_rating_key=str(item["ratingKey"])
        if item.get("ratingKey") is not None
        else None,
        guids=guids or None,
        native_id=item.get("guid")
        or (str(item["ratingKey"]) if item.get("ratingKey") is not None else None),
        native_ids=compact(
            {
                "server": server,
                "rating_key": str(item["ratingKey"])
                if item.get("ratingKey") is not None
                else None,
                "guid": item.get("guid"),
                "guids": guids or None,
                "library_section_id": section.get("id"),
            }
        )
        or None,
        timing_basis="Plex addedAt library timestamp",
        time_precision="source_mark",
        source="api",
    )
    if episode:
        numbers = (
            f" S{fields['season']:02}E{fields['episode']:02}"
            if fields["season"] is not None and fields["episode"] is not None
            else ""
        )
        content = f"Added {fields['show'] or 'an unknown show'}{numbers} - {title} to Plex"
    elif kind == "album":
        content = f"Added album {title} by {fields['creator'] or 'an unknown artist'} to Plex"
    else:
        content = f"Added {title} to Plex"
    msg = message(
        record,
        "plex:library_added:" + record["id"],
        {
            "external_id": f"plex:library:{section.get('id') or 'unknown'}:additions",
            "title": f"Plex additions / {section.get('title') or 'Unknown library'}",
        },
        at,
        content,
        fields,
    )
    work = plex_work(item, server, record["id"])
    links = [link("saved", endpoint(msg), work)]
    links.extend(plex_hierarchy_links(work, item, server))
    return Mapped(msg, links)


# ---------------------------------------------------------------- Audiobookshelf


def abs_work_ref(item_id, episode_id=None):
    return f"audiobookshelf:item:{item_id}" + (
        f":episode:{episode_id}" if episode_id else ""
    )


def abs_thread(item_id, episode_id, title):
    return {"external_id": abs_work_ref(item_id, episode_id), "title": title}


def abs_author(context, user_id):
    user = (context or {}).get("user") or {}
    value = user_id or user.get("id")
    if not value:
        return None
    author = {"system": "audiobookshelf", "kind": "user_id", "value": str(value)}
    if user.get("username"):
        author["display_name"] = user["username"]
    return author


def abs_links(msg, item_id, episode_id, title, show):
    work = {
        "kind": "work",
        "external_ref": abs_work_ref(item_id, episode_id),
        "name": title,
    }
    links = [link("consumed", endpoint(msg), work)]
    if episode_id:
        links.append(
            link(
                "part_of",
                work,
                {
                    "kind": "podcast",
                    "external_ref": abs_work_ref(item_id),
                    "name": show,
                },
            )
        )
    return links


def map_abs_session(record):
    s = record["raw"]
    started = integer(s.get("startedAt"))
    updated = integer(s.get("updatedAt"))
    if started is None and updated is None:
        return None
    started = started if started is not None else updated
    meta = s.get("mediaMetadata") or {}
    podcast = s.get("mediaType") == "podcast" or bool(s.get("episodeId"))
    authors = [
        a.get("name")
        for a in meta.get("authors") or []
        if isinstance(a, dict) and a.get("name")
    ]
    series = next(
        (x for x in meta.get("series") or [] if isinstance(x, dict) and x.get("name")),
        None,
    )
    if podcast:
        title = s.get("displayTitle") or meta.get("title") or "Unknown episode"
        show = meta.get("title") or s.get("displayTitle")
        creator = meta.get("author") or s.get("displayAuthor")
    else:
        title = meta.get("title") or s.get("displayTitle") or "Unknown book"
        show = None
        creator = s.get("displayAuthor") or ", ".join(authors) or meta.get("authorName")
    device = s.get("deviceInfo") or {}
    duration = number(s.get("duration"))
    position = number(s.get("currentTime"))
    listened = number(s.get("timeListening"))
    at = ms_iso(started)
    ended = ms_iso(updated) if updated is not None and updated >= started else None
    fields = base_fields(
        "audiobookshelf",
        "session",
        "podcast" if podcast else "audiobook",
        title=title,
        creator=creator,
        creators=authors or None,
        show=show if podcast else None,
        series=(
            series["name"]
            + (f" #{series['sequence']}" if series.get("sequence") else "")
        )
        if series and not podcast
        else None,
        duration_s=duration,
        position_s=position,
        start_position_s=number(s.get("startTime")),
        progress=round(min(1.0, position / duration), 4)
        if duration and position is not None
        else None,
        listened_s=listened,
        device=device.get("deviceName")
        or " ".join(x for x in (device.get("manufacturer"), device.get("model")) if x)
        or None,
        client=" ".join(
            x for x in (device.get("clientName"), device.get("clientVersion")) if x
        )
        or s.get("mediaPlayer"),
        account=((record.get("context") or {}).get("user") or {}).get("username"),
        library=s.get("libraryId"),
        play_method=PLAY_METHODS.get(s.get("playMethod")),
        native_id=abs_work_ref(s.get("libraryItemId"), s.get("episodeId"))
        if s.get("libraryItemId")
        else None,
        native_ids=compact(
            {
                "session_id": s.get("id"),
                "library_item_id": s.get("libraryItemId"),
                "episode_id": s.get("episodeId"),
                "library_id": s.get("libraryId"),
            }
        )
        or None,
        detail=compact(
            {
                "narrators": meta.get("narrators")
                if isinstance(meta.get("narrators"), list)
                else None,
                "media_player": s.get("mediaPlayer"),
                "date": s.get("date"),
            }
        )
        or None,
        timing_basis="Session startedAt to its last updatedAt; listened_s is actual listening time",
        time_precision="session",
        source="api",
    )
    phrase = (
        f"{title} from {show}"
        if podcast and show and show != title
        else f"{title} by {creator or 'an unknown author'}"
    )
    content = f"Listened to {phrase}" + (f" for {clock(listened)}" if listened else "")
    item_id = s.get("libraryItemId") or "unknown"
    msg = message(
        record,
        "audiobookshelf:session:" + record["id"],
        abs_thread(item_id, s.get("episodeId"), show if podcast and show else title),
        at,
        content,
        fields,
        ended=ended,
        author=abs_author(record.get("context"), s.get("userId")),
    )
    links = (
        abs_links(msg, item_id, s.get("episodeId"), title, show)
        if s.get("libraryItemId")
        else []
    )
    return Mapped(msg, links)


def map_abs_progress(record):
    p = record["raw"]
    context = record.get("context") or {}
    item = context.get("item") or {}
    updated = integer(p.get("lastUpdate"))
    started = integer(p.get("startedAt")) or updated
    if started is None:
        return None
    episode = item.get("episode") or {}
    podcast = item.get("mediaType") == "podcast" or bool(p.get("episodeId"))
    title = (
        (episode.get("title") if podcast else item.get("title"))
        or item.get("title")
        or "Unknown item"
    )
    show = item.get("title") if podcast else None
    finished = bool(p.get("isFinished"))
    finished_at = integer(p.get("finishedAt"))
    duration = number(p.get("duration")) or number(
        episode.get("duration") if podcast else item.get("duration")
    )
    progress = number(p.get("progress"))
    at = ms_iso(started)
    ended = (
        ms_iso(finished_at)
        if finished and finished_at and finished_at >= started
        else None
    )
    fields = base_fields(
        "audiobookshelf",
        "finished" if finished else "progress",
        "podcast" if podcast else "audiobook",
        title=title,
        creator=item.get("author"),
        show=show,
        series=item.get("series") if not podcast else None,
        duration_s=duration,
        position_s=number(p.get("currentTime")),
        progress=progress,
        is_finished=finished,
        account=(context.get("user") or {}).get("username"),
        library=item.get("libraryId"),
        native_id=abs_work_ref(p.get("libraryItemId"), p.get("episodeId"))
        if p.get("libraryItemId")
        else None,
        native_ids=compact(
            {
                "progress_id": p.get("id"),
                "library_item_id": p.get("libraryItemId"),
                "episode_id": p.get("episodeId"),
            }
        )
        or None,
        detail=compact(
            {
                "narrator": item.get("narrator"),
                "last_update": ms_iso(updated) if updated else None,
                "hide_from_continue_listening": p.get("hideFromContinueListening"),
            }
        )
        or None,
        timing_basis="Progress startedAt; ended_at is finishedAt when finished",
        time_precision="progress",
        source="api",
    )
    name = f"{title} from {show}" if show and show != title else title
    if finished:
        content = f"Finished {name}"
    elif progress is not None:
        content = f"{round(progress * 100)}% through {name}"
    else:
        content = f"In progress: {name}"
    item_id = p.get("libraryItemId") or "unknown"
    msg = message(
        record,
        "audiobookshelf:progress:" + record["id"],
        abs_thread(item_id, p.get("episodeId"), show or title),
        at,
        content,
        fields,
        ended=ended,
        author=abs_author(context, p.get("userId")),
    )
    links = (
        abs_links(msg, item_id, p.get("episodeId"), title, show)
        if p.get("libraryItemId")
        else []
    )
    return Mapped(msg, links)


# ---------------------------------------------------------------- YouTube


def map_youtube(record):
    parsed = (record.get("context") or {}).get("parsed") or {}
    known = known_time(parsed.get("at"))
    at = known.isoformat() if known else fallback_time(record)
    title = parsed.get("title") or "Unavailable video"
    channel = parsed.get("channel")
    video = parsed.get("video_id")
    precision = parsed.get("time_precision") or (
        "source_mark" if known else "first_seen"
    )
    fields = base_fields(
        "youtube",
        "play",
        "video",
        title=title,
        creator=channel,
        native_id=video,
        native_ids=compact({"video_id": video, "channel_id": parsed.get("channel_id")})
        or None,
        url=parsed.get("url")
        or (f"https://www.youtube.com/watch?v={video}" if video else None),
        is_short=parsed.get("is_short"),
        is_music=parsed.get("is_music"),
        is_ad=parsed.get("is_ad"),
        timing_basis=(
            "first time the history feed showed the video, not a watch time"
            if precision == "first_seen"
            else "Takeout watch time"
        ),
        time_precision=precision,
        source=parsed.get("source") or "takeout",
    )
    msg = message(
        record,
        "youtube:" + record["id"],
        day_thread("youtube", at),
        at,
        f"Watched {title}" + (f" by {channel}" if channel else ""),
        fields,
    )
    links = []
    if video:
        work = {"kind": "work", "external_ref": "youtube:" + video, "name": title}
        links.append(link("consumed", endpoint(msg), work))
        if parsed.get("channel_id"):
            links.append(
                link(
                    "published_by",
                    work,
                    {
                        "kind": "organization",
                        "external_ref": "youtube:" + parsed["channel_id"],
                        "name": channel,
                    },
                )
            )
    return Mapped(msg, links, pinned=known is None)


def _youtube_work_links(msg, video, title, channel, channel_id):
    links = []
    if video:
        work = {"kind": "work", "external_ref": "youtube:" + video, "name": title}
        links.append(link("consumed", endpoint(msg), work))
        if channel_id:
            links.append(
                link(
                    "published_by",
                    work,
                    {
                        "kind": "organization",
                        "external_ref": "youtube:" + channel_id,
                        "name": channel,
                    },
                )
            )
    return links


def _percent(value):
    return round(value / 100, 4) if isinstance(value, (int, float)) else None


def _at_percent(value, length):
    return round(value / 100 * length) if value is not None and length else None


def map_youtube_session(record):
    """One viewing session found by the youtube plugin's history watcher."""
    raw = record["raw"]
    video, title = raw.get("video_id"), raw.get("title") or "Unavailable video"
    channel, length = raw.get("channel"), raw.get("length_s")
    before, after = raw.get("progress_before"), raw.get("progress_after")
    fields = base_fields(
        "youtube",
        "session",
        "video",
        title=title,
        creator=channel,
        duration_s=length,
        position_s=_at_percent(after, length),
        start_position_s=_at_percent(before, length),
        progress=_percent(after),
        listened_s=raw.get("watched_s"),
        is_finished=True if isinstance(after, (int, float)) and after >= 99 else None,
        is_short=raw.get("is_short"),
        native_id=video,
        native_ids=compact({"video_id": video, "channel_id": raw.get("channel_id")})
        or None,
        url=f"https://www.youtube.com/watch?v={video}" if video else None,
        detail=compact(
            {
                k: raw.get(k)
                for k in (
                    "session_kind",
                    "progress_before",
                    "progress_after",
                    "watched_fraction",
                    "window_start",
                    "window_end",
                    "session_number",
                    "day",
                )
            }
        )
        or None,
        timing_basis=(
            "estimated session between two history polls (window_start to "
            "window_end); start and end assume back-to-back viewing"
        ),
        time_precision="estimated",
        source="watcher",
    )
    part = raw.get("watched_fraction")
    content = (
        f"Watched {round(part * 100)}% of {title}"
        if part is not None
        else f"Watched {title}"
    ) + (f" by {channel}" if channel else "")
    msg = message(
        record,
        "youtube:session:" + record["id"],
        day_thread("youtube", raw["start_est"]),
        raw["start_est"],
        content,
        fields,
        ended=raw.get("end_est"),
    )
    return Mapped(
        msg, _youtube_work_links(msg, video, title, channel, raw.get("channel_id"))
    )


def map_youtube_history_day(record):
    """A video under a YouTube history day heading: watched that day."""
    raw = record["raw"]
    video, title = raw.get("video_id"), raw.get("title") or "Unavailable video"
    channel = raw.get("channel")
    if raw.get("day"):
        tz_name = (record.get("context") or {}).get("tz") or "UTC"
        try:
            tz = timezone.utc if tz_name.upper() == "UTC" else ZoneInfo(tz_name)
        except (ValueError, KeyError, OSError):
            tz = timezone.utc
        at = datetime.fromisoformat(raw["day"]).replace(tzinfo=tz).isoformat()
        precision = "day"
    else:
        at, precision = raw.get("first_seen") or fallback_time(record), "first_seen"
    fields = base_fields(
        "youtube",
        "play",
        "video",
        title=title,
        creator=channel,
        duration_s=raw.get("length_s"),
        progress=_percent(raw.get("progress")),
        is_short=raw.get("is_short"),
        native_id=video,
        native_ids=compact({"video_id": video, "channel_id": raw.get("channel_id")})
        or None,
        url=f"https://www.youtube.com/watch?v={video}" if video else None,
        timing_basis=(
            "the YouTube history day the video was listed under (local day "
            "start); the time of day is unknown"
            if precision == "day"
            else "first time the watcher saw the video, not a watch time"
        ),
        time_precision=precision,
        source="watcher",
    )
    msg = message(
        record,
        "youtube:history_day:" + record["id"],
        day_thread("youtube", at),
        at,
        f"Watched {title}" + (f" by {channel}" if channel else ""),
        fields,
    )
    return Mapped(
        msg,
        _youtube_work_links(msg, video, title, channel, raw.get("channel_id")),
        pinned=precision == "first_seen",
    )


# ---------------------------------------------------------------- Audiobook text


def map_audiobook_text_chunk(record):
    raw = record["raw"]
    text = (raw.get("text") or "").strip()
    item_id = raw.get("abs_item_id")
    chunk_index = integer(raw.get("chunk_index"))
    if not text or not item_id or chunk_index is None or chunk_index < 1:
        return None
    title = raw.get("title") or "Unknown book"
    authors = [
        str(a)
        for a in (raw.get("authors") or [])
        if isinstance(a, str) and a.strip()
    ]
    anchor_ms = integer(raw.get("anchor_ms"))
    pinned = anchor_ms is None or anchor_ms <= 0
    if pinned:
        base_dt = parse_time(fallback_time(record))
    else:
        base_dt = datetime.fromtimestamp(anchor_ms / 1000, timezone.utc)
    at = (base_dt + timedelta(seconds=chunk_index - 1)).astimezone(timezone.utc).isoformat()
    fields = dict.fromkeys(BOOK_TEXT_FIELDS)
    fields.update(
        service="audiobookshelf",
        ebook_source="kavita",
        title=title,
        creator=", ".join(authors) or None,
        creators=authors or None,
        series=raw.get("series") or None,
        chapter_index=integer(raw.get("chapter_index")),
        chapter_title=raw.get("chapter_title") or None,
        part_index=integer(raw.get("part_index")),
        part_count=integer(raw.get("part_count")),
        chunk_index=chunk_index,
        chunk_count=integer(raw.get("chunk_count")),
        char_count=integer(raw.get("char_count")) or len(text),
        word_count=integer(raw.get("word_count")) or len(text.split()),
        book_format=raw.get("book_format") or None,
        abs_item_id=str(item_id),
        abs_progress=number(raw.get("progress")),
        abs_is_finished=bool(raw["is_finished"])
        if raw.get("is_finished") is not None
        else None,
        kavita_series_id=integer(raw.get("kavita_series_id")),
        kavita_chapter_id=integer(raw.get("kavita_chapter_id")),
        kavita_file_path=raw.get("kavita_file_path") or None,
        kavita_file_hash=raw.get("kavita_file_hash") or None,
        match_score=number(raw.get("match_score")),
        match_method=raw.get("match_method") or None,
        native_id=abs_work_ref(item_id),
        mapping_revision=MAPPING_REVISION,
    )
    external_id = f"audiobookshelf:book:{item_id}:chunk:{chunk_index:04d}"
    msg = {
        "channel": BOOK_TEXT_CHANNEL,
        "external_id": external_id,
        "thread": {
            "external_id": f"audiobookshelf:book:{item_id}",
            "title": title,
        },
        "happened_at": at,
        "ended_at": None,
        "content": text,
        "fields": fields,
        "payload": {
            k: record[k]
            for k in ("kind", "id", "service", "raw", "context")
            if k in record
        },
    }
    author = abs_author(record.get("context"), None)
    if author:
        msg["author"] = author
    links = []
    if chunk_index == 1:
        links.append(
            link(
                "part_of",
                {"channel": BOOK_TEXT_CHANNEL, "external_id": external_id},
                {
                    "kind": "work",
                    "external_ref": abs_work_ref(item_id),
                    "name": title,
                },
            )
        )
    return Mapped(msg, links, pinned=pinned)


MAPPERS = {
    "youtube.session": map_youtube_session,
    "youtube.history_day": map_youtube_history_day,
    "spotify.play": map_spotify_play,
    "spotify.saved_track": map_spotify_saved,
    "spotify.playlist_item": map_spotify_playlist_item,
    "plex.history": map_plex,
    "plex.session": map_plex_session,
    "plex.library_added": map_plex_library_added,
    "audiobookshelf.session": map_abs_session,
    "audiobookshelf.progress": map_abs_progress,
    "audiobook.text_chunk": map_audiobook_text_chunk,
    "youtube.watch": map_youtube,
}
# Records whose source keeps changing them; the value is their version clock.
MUTABLE = {
    "plex.session": "_sessionSeen",
    "plex.library_added": "addedAt",
    "audiobookshelf.session": "updatedAt",
    "audiobookshelf.progress": "lastUpdate",
}


def map_record(record):
    """Map one raw record; None for unknown kinds and unusable records."""
    mapper = MAPPERS.get(record.get("kind"))
    if (
        mapper is None
        or not isinstance(record.get("raw"), dict)
        or not record.get("id")
    ):
        return None
    return mapper(record)
