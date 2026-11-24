"""Trakt: the owner's watch history, ratings and watchlist.

History comes from /sync/history with a fixed start_at/end_at window per run,
read oldest page first, so the cursor (the newest watched_at delivered) only
moves forward and a crash resumes where it stopped. Ratings and the watchlist
are small reference lists read in full, skipped on sync runs when
/sync/last_activities says they have not changed.

Authentication is Trakt OAuth: `throughline-trakt-login` (device code flow)
writes a token file that this plugin reads and refreshes in place.
"""

import logging
from datetime import datetime, timezone

from throughline_sdk import Channel, Checkpoint, Declaration, Manifest, Plugin, Record, run

from .client import DEFAULT_TOKEN_FILE, TokenStore, TraktApi
from .common import (
    compact,
    field_list,
    fields_for,
    integer,
    iso,
    link,
    load_transport,
    parse_time,
)

log = logging.getLogger(__name__)

HISTORY = "trakt.history"
RATINGS = "trakt.ratings"
WATCHLIST = "trakt.watchlist"
SHARED = [
    "service", "source_kind", "event_type", "media_type", "title", "show", "season",
    "episode", "year", "duration_s", "account", "native_id", "native_ids", "url", "detail",
    "timing_basis", "time_precision", "source", "author_kind", "mapping_revision",
]
HISTORY_FIELDS = field_list(
    SHARED, {"action": ("text", "How Trakt recorded the play: scrobble, checkin or watch")}
)
RATING_FIELDS = field_list(SHARED + ["rating", "rating_raw"])
WATCHLIST_FIELDS = field_list(
    SHARED,
    {
        "rank": ("integer", "Position in the owner's watchlist"),
        "notes": ("text", "The owner's note on the watchlist entry"),
    },
)
LINK_TYPES = ["consumed", "rated", "saved", "part_of"]
KINDS = ("movie", "show", "season", "episode")


def work_ref(kind, ids, fallback=None):
    """Cross-source reference: IMDb, else TMDB, else TVDB, else Trakt."""
    ids = ids or {}
    if ids.get("imdb"):
        return f"imdb:{ids['imdb']}"
    for scheme in ("tmdb", "tvdb", "trakt"):
        if ids.get(scheme) not in (None, ""):
            return f"{scheme}:{kind}:{ids[scheme]}"
    if ids.get("slug"):
        return f"trakt:{kind}:{ids['slug']}"
    return fallback


def entity(ref, name):
    return {"kind": "work", "external_ref": ref, "name": name}


def episode_code(season, number):
    if season is None or number is None:
        return ""
    return f" S{season:02}E{number:02}"


def describe(item):
    """The work an item is about: fields, a label for content, refs and links.

    Returns None for item types this plugin does not understand."""
    kind = item.get("type")
    if kind not in KINDS or not isinstance(item.get(kind), dict):
        return None
    show = item.get("show") or {}
    show_ids = show.get("ids") or {}
    show_title = show.get("title") or ("Unknown show" if kind != "movie" else None)
    show_ref = work_ref("show", show_ids) if show else None
    show_slug = show_ids.get("slug")
    work = item[kind]
    ids = work.get("ids") or {}
    links = []
    out = {"media_type": kind, "year": None, "show": None, "season": None, "episode": None}
    if kind == "movie":
        title = work.get("title") or "Unknown title"
        out.update(title=title, year=integer(work.get("year")))
        label = title + (f" ({out['year']})" if out["year"] is not None else "")
        ref = work_ref("movie", ids)
        url = f"https://trakt.tv/movies/{ids['slug']}" if ids.get("slug") else None
    elif kind == "show":
        title = work.get("title") or "Unknown show"
        out.update(title=title, year=integer(work.get("year")))
        label = title + (f" ({out['year']})" if out["year"] is not None else "")
        ref = work_ref("show", ids)
        url = f"https://trakt.tv/shows/{ids['slug']}" if ids.get("slug") else None
        show_ids = {}
    elif kind == "season":
        number = integer(work.get("number"))
        title = f"Season {number}" if number is not None else "Unknown season"
        out.update(title=title, show=show_title, season=number)
        label = f"{show_title} {title}"
        ref = f"{show_ref}:season:{number}" if show_ref and number is not None else work_ref(
            "season", ids
        )
        url = (
            f"https://trakt.tv/shows/{show_slug}/seasons/{number}"
            if show_slug and number is not None
            else None
        )
        if show_ref:
            links.append(link("part_of", entity(ref, f"{show_title} {title}"), entity(show_ref, show_title)))
    else:
        season, number = integer(work.get("season")), integer(work.get("number"))
        title = work.get("title") or (f"Episode {number}" if number is not None else "Unknown title")
        out.update(title=title, show=show_title, season=season, episode=number)
        label = f"{show_title}{episode_code(season, number)} - {title}"
        ref = work_ref("episode", ids)
        url = (
            f"https://trakt.tv/shows/{show_slug}/seasons/{season}/episodes/{number}"
            if show_slug and season is not None and number is not None
            else None
        )
        if show_ref:
            name = f"{show_title}{episode_code(season, number)} - {title}"
            if season is not None:
                season_ref = f"{show_ref}:season:{season}"
                season_entity = entity(season_ref, f"{show_title} Season {season}")
                links.append(link("part_of", entity(ref, name), season_entity))
                links.append(link("part_of", season_entity, entity(show_ref, show_title)))
            else:
                links.append(link("part_of", entity(ref, name), entity(show_ref, show_title)))
    runtime = integer(work.get("runtime"))
    out.update(
        label=label,
        ref=ref or f"trakt:{kind}:unknown",
        url=url,
        duration_s=runtime * 60 if runtime else None,
        native_id=f"trakt:{kind}:{ids['trakt']}" if ids.get("trakt") is not None else None,
        native_ids=compact(
            {
                "trakt": ids.get("trakt"),
                "slug": ids.get("slug"),
                "imdb": ids.get("imdb"),
                "tmdb": ids.get("tmdb"),
                "tvdb": ids.get("tvdb"),
                "show_trakt": show_ids.get("trakt"),
                "show_imdb": show_ids.get("imdb"),
                "show_tmdb": show_ids.get("tmdb"),
                "show_tvdb": show_ids.get("tvdb"),
            }
        )
        or None,
        links=links,
    )
    return out


def owner(user, name=None):
    return {"system": "trakt", "kind": "account", "value": user, "display_name": name or user}


def work_fields(declared, work, user, **values):
    return fields_for(
        declared,
        service="trakt",
        media_type=work["media_type"],
        title=work["title"],
        show=work["show"],
        season=work["season"],
        episode=work["episode"],
        year=work["year"],
        duration_s=work["duration_s"],
        account=user,
        native_id=work["native_id"],
        native_ids=work["native_ids"],
        url=work["url"],
        time_precision="source_mark",
        source="api",
        author_kind="owner",
        mapping_revision=1,
        **values,
    )


def endpoint(message):
    return {"channel": message["channel"], "external_id": message["external_id"]}


def with_links(message, work, relation):
    out = [link(relation, endpoint(message), entity(work["ref"], work["label"]))]
    out.extend(work["links"])
    return message, out


def map_history(item, user, name=None):
    """One /sync/history item to (message, links), or None."""
    when = parse_time(item.get("watched_at"))
    work = describe(item)
    if when is None or work is None or item.get("id") is None:
        return None
    fields = work_fields(
        HISTORY_FIELDS,
        work,
        user,
        source_kind="history",
        event_type="play",
        action=item.get("action"),
        detail=compact({"history_id": item.get("id"), "action": item.get("action")}) or None,
        timing_basis="Trakt watched_at: when the play was scrobbled, checked in or marked watched",
    )
    at = iso(when)
    day = when.date().isoformat()
    message = {
        "channel": HISTORY,
        "external_id": f"trakt:history:{item['id']}",
        "thread": {"external_id": f"trakt:{user.casefold()}:day:{day}", "title": f"Trakt {day}"},
        "happened_at": at,
        "author": owner(user, name),
        "content": f"Watched {work['label']}",
        "fields": fields,
        "payload": item,
    }
    return with_links(message, work, "consumed")


def item_key(item):
    kind = item.get("type")
    ids = (item.get(kind) or {}).get("ids") or {}
    key = ids.get("trakt") if ids.get("trakt") is not None else ids.get("slug")
    return f"{kind}:{key}" if key is not None else None


def map_rating(item, user, name=None):
    when = parse_time(item.get("rated_at"))
    work = describe(item)
    key = item_key(item)
    rating = integer(item.get("rating"))
    if when is None or work is None or key is None or rating is None:
        return None
    fields = work_fields(
        RATING_FIELDS,
        work,
        user,
        source_kind="rating",
        event_type="rated",
        rating=float(rating),
        rating_raw=f"{rating}/10",
        timing_basis="When the rating was last set on Trakt",
    )
    message = {
        "channel": RATINGS,
        "external_id": f"trakt:{user.casefold()}:rating:{key}",
        "thread": {"external_id": f"trakt:{user.casefold()}:ratings", "title": "Trakt ratings"},
        "happened_at": iso(when),
        "author": owner(user, name),
        "content": f"Rated {work['label']} {rating}/10 on Trakt",
        "fields": fields,
        "payload": item,
    }
    return with_links(message, work, "rated")


def map_watchlist(item, user, name=None):
    when = parse_time(item.get("listed_at"))
    work = describe(item)
    key = item_key(item)
    if when is None or work is None or key is None:
        return None
    notes = (item.get("notes") or "").strip() or None
    fields = work_fields(
        WATCHLIST_FIELDS,
        work,
        user,
        source_kind="watchlist",
        event_type="watchlist_add",
        rank=integer(item.get("rank")),
        notes=notes,
        detail=compact({"list_item_id": item.get("id")}) or None,
        timing_basis="When the item was added to the Trakt watchlist",
    )
    message = {
        "channel": WATCHLIST,
        "external_id": f"trakt:{user.casefold()}:watchlist:{key}",
        "thread": {"external_id": f"trakt:{user.casefold()}:watchlist", "title": "Trakt watchlist"},
        "happened_at": iso(when),
        "author": owner(user, name),
        "content": f"Added {work['label']} to the Trakt watchlist"
        + (f": {notes}" if notes else ""),
        "fields": fields,
        "payload": item,
    }
    return with_links(message, work, "saved")


def emit(mapped):
    message, links = mapped
    yield message
    for data in links:
        yield Record(kind="link", data=data)


def latest(activities, key):
    """Newest of one activity timestamp across movies/shows/seasons/episodes."""
    stamps = [
        parse_time((activities.get(group) or {}).get(key))
        for group in ("movies", "shows", "seasons", "episodes")
    ]
    stamps = [s for s in stamps if s]
    return iso(max(stamps)) if stamps else None


def trakt_time(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class Trakt(Plugin):
    manifest = Manifest(
        name="trakt",
        version="0.1.0",
        channels=[
            Channel(
                name=HISTORY,
                description="Every movie and episode play in the owner's Trakt history",
                fields=HISTORY_FIELDS,
                conversation_gap_s=4 * 3600,
                record_kind="life_event",
                embed=False,
            ),
            Channel(
                name=RATINGS,
                description="Movies, shows, seasons and episodes the owner rated on Trakt (0-10)",
                fields=RATING_FIELDS,
                is_reference=True,
                record_kind="reference",
            ),
            Channel(
                name=WATCHLIST,
                description="Movies, shows, seasons and episodes on the owner's Trakt watchlist",
                fields=WATCHLIST_FIELDS,
                is_reference=True,
                record_kind="reference",
            ),
        ],
        settings={
            "token_file": Declaration(
                env="TRAKT_TOKEN_FILE", required=False, default=DEFAULT_TOKEN_FILE,
                description="OAuth token file written by throughline-trakt-login (refreshed in place)",
            ),
            "user": Declaration(
                env="TRAKT_USER", required=False,
                description="Trakt user name (default: looked up from /users/settings)",
            ),
            "page_size": Declaration(
                env="TRAKT_PAGE_SIZE", required=False, default="100",
                description="Items per API page; one checkpoint per page",
            ),
            "delay_s": Declaration(
                env="TRAKT_REQUEST_DELAY_S", required=False, default="0.3",
                description="Pause between API requests (Trakt allows 1000 GETs per 5 minutes)",
            ),
            "ratings": Declaration(
                env="TRAKT_RATINGS", required=False, default="true",
                description="Also import ratings (true/false)",
            ),
            "watchlist": Declaration(
                env="TRAKT_WATCHLIST", required=False, default="true",
                description="Also import the watchlist (true/false)",
            ),
            "test_transport": Declaration(
                env="TRAKT_TEST_TRANSPORT", required=False,
                description="Testing only: module:factory returning an httpx transport",
            ),
        },
        secrets={
            "client_id": Declaration(env="TRAKT_CLIENT_ID", description="Trakt API app client ID"),
            "client_secret": Declaration(
                env="TRAKT_CLIENT_SECRET", description="Trakt API app client secret"
            ),
        },
        run_mode="poll",
        default_interval_s=900,
        link_types=LINK_TYPES,
    )
    transport = None
    clock = None

    def api(self):
        extra = {"clock": self.clock} if self.clock else {}
        return TraktApi(
            self.secrets["client_id"],
            self.secrets["client_secret"],
            TokenStore(self.settings.get("token_file")),
            transport=self.transport or load_transport(self.settings.get("test_transport")),
            delay_s=float(self.settings.get("delay_s") or 0),
            **extra,
        )

    def enabled(self, name):
        return (self.settings.get(name) or "true").strip().lower() not in ("false", "0", "no")

    def page_size(self):
        return max(1, min(1000, int(self.settings.get("page_size") or 100)))

    def records(self, cursor, mode):
        api = self.api()
        try:
            user = (self.settings.get("user") or "").strip()
            name = None
            if not user:
                profile = (api.get("/users/settings")[0] or {}).get("user") or {}
                user = profile.get("username") or (profile.get("ids") or {}).get("slug")
                name = profile.get("name") or None
                if not user:
                    raise RuntimeError("Trakt /users/settings returned no user name; set TRAKT_USER")
            cursor = dict(cursor or {})
            cursor = yield from self.history(api, cursor, user, name)
            activities = {}
            if self.enabled("ratings") or self.enabled("watchlist"):
                activities = api.get("/sync/last_activities")[0] or {}
            if self.enabled("ratings"):
                cursor = yield from self.reference(
                    api, cursor, mode, "/sync/ratings", "ratings_activity",
                    latest(activities, "rated_at"), map_rating, user, name,
                )
            if self.enabled("watchlist"):
                cursor = yield from self.reference(
                    api, cursor, mode, "/sync/watchlist", "watchlist_activity",
                    latest(activities, "watchlisted_at"), map_watchlist, user, name,
                )
        finally:
            api.close()

    def history(self, api, cursor, user, name):
        limit = self.page_size()
        # Inclusive lower bound: plays sharing the boundary instant are
        # re-read and deduplicated by their history IDs.
        since = cursor.get("history_at")
        params = {"end_at": trakt_time(datetime.now(timezone.utc)), "extended": "full"}
        if since:
            params["start_at"] = since
        first, pages = api.pages("/sync/history", params, 1, limit)
        newest = parse_time(since)
        # Pages are newest first: walk from the last (oldest) page to page 1
        # inside the fixed window so the high-water mark only moves forward.
        for page in range(pages, 0, -1):
            items = first if page == 1 else api.pages("/sync/history", params, page, limit)[0]
            for item in reversed(items):
                mapped = map_history(item, user, name)
                if mapped is None:
                    continue
                yield from emit(mapped)
                when = parse_time(mapped[0]["happened_at"])
                newest = max(newest, when) if newest else when
            if newest:
                cursor = dict(cursor, history_at=trakt_time(newest))
                yield Checkpoint(cursor=cursor)
        return cursor

    def reference(self, api, cursor, mode, path, stamp_key, stamp, mapper, user, name):
        """A whole small list, oldest first, one checkpoint per page_size.

        The activity stamp is stored only with the final checkpoint, so a
        crash part way through re-reads the list on the next run."""
        if mode == "sync" and stamp and cursor.get(stamp_key) == stamp:
            return cursor
        limit = self.page_size()
        items = api.all_pages(path, {"extended": "full"}, limit)
        mapped = [m for m in (mapper(item, user, name) for item in items) if m]
        mapped.sort(key=lambda m: (m[0]["happened_at"], m[0]["external_id"]))
        for index, entry in enumerate(mapped, 1):
            yield from emit(entry)
            if index % limit == 0 and index < len(mapped):
                yield Checkpoint(cursor=cursor)
        if stamp:
            cursor = dict(cursor, **{stamp_key: stamp})
        yield Checkpoint(cursor=cursor)
        return cursor


def main():
    run(Trakt)
