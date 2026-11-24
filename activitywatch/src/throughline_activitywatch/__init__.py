"""ActivityWatch desktop activity as Throughline sessions.

Reads the local ActivityWatch REST API (default http://localhost:5600/api/0)
and stores coalesced sessions rather than raw watcher events:

- activitywatch.windows: app/window-title sessions (aw-watcher-window),
  limited to not-AFK time;
- activitywatch.web: browser tab sessions (aw-watcher-web-*), url, title,
  domain, limited to not-AFK time;
- activitywatch.editor: editor sessions (aw-watcher-vscode and other
  app.editor.activity watchers): project, file, language;
- activitywatch.afk: active / away periods (aw-watcher-afk).

Each bucket keeps its own cursor (the start of its first session not yet
emitted). Sessions are emitted only once they can no longer grow, so the
in-progress event at the end of a bucket is held back until it is stable.
"""

import copy
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import httpx
from throughline_sdk import Channel, Checkpoint, Declaration, Manifest, Plugin, coverage, run

from . import sessions as S
from .client import ActivityWatch, fixture_transport

log = logging.getLogger(__name__)

MAPPING_REVISION = 1
WINDOWS = "activitywatch.windows"
WEB = "activitywatch.web"
EDITOR = "activitywatch.editor"
AFK = "activitywatch.afk"

KINDS = {
    "currentwindow": WINDOWS,
    "web.tab.current": WEB,
    "app.editor.activity": EDITOR,
    "afkstatus": AFK,
}


def fields(**values):
    return [dict(name=k, type=v[0], description=v[1]) for k, v in values.items()]


COMMON = dict(
    host=("text", "Computer (bucket hostname) the session was recorded on"),
    bucket=("text", "ActivityWatch bucket id"),
    duration_s=("number", "Session length from first to last event, seconds"),
    active_s=("number", "Seconds actually covered by events (gaps under the merge gap excluded)"),
    event_count=("integer", "Raw ActivityWatch events merged into this session"),
    mapping_revision=("integer", "Mapping revision that produced this record"),
)


def setting(env, default=None, description="", required=False):
    return Declaration(env=env, default=default, description=description, required=required)


def duration(seconds):
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m" if not secs else f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def host_of(bucket):
    host = bucket.get("hostname")
    if not host or host == "unknown":
        bucket_id = bucket.get("id") or ""
        host = bucket_id.rsplit("_", 1)[1] if "_" in bucket_id else "unknown"
    return host


def domain_of(url):
    try:
        host = urlsplit(url or "").hostname or ""
    except ValueError:
        return None
    return host[4:] if host.startswith("www.") else (host or None)


class ActivityWatchPlugin(Plugin):
    manifest = Manifest(
        name="activitywatch",
        version="0.1.0",
        channels=[
            Channel(
                name=WINDOWS,
                description="Desktop app and window-title sessions from ActivityWatch (blips dropped, consecutive events merged, AFK time removed)",
                fields=fields(
                    app=("text", "Application name"),
                    title=("text", "Window title"),
                    afk_filtered=("boolean", "Limited to not-AFK time (false when the host has no AFK watcher)"),
                    **COMMON,
                ),
                embed=False,
            ),
            Channel(
                name=WEB,
                description="Browser tab sessions from ActivityWatch web watchers (AFK time removed)",
                fields=fields(
                    url=("text", "Page URL"),
                    title=("text", "Page title"),
                    domain=("text", "Host name of the URL without www."),
                    browser=("text", "Browser, from the watcher bucket (chrome, firefox, ...)"),
                    audible=("boolean", "The tab played sound during the session"),
                    afk_filtered=("boolean", "Limited to not-AFK time"),
                    **COMMON,
                ),
            ),
            Channel(
                name=EDITOR,
                description="Code editor sessions from ActivityWatch editor watchers (project, file, language)",
                fields=fields(
                    project=("text", "Project or workspace folder"),
                    file=("text", "File being edited"),
                    language=("text", "Language of the file"),
                    editor=("text", "Editor watcher client (aw-watcher-vscode, ...)"),
                    afk_filtered=("boolean", "Limited to not-AFK time"),
                    **COMMON,
                ),
                embed=False,
            ),
            Channel(
                name=AFK,
                description="Periods at the computer (active) and away from it (afk) from aw-watcher-afk",
                fields=fields(status=("text", "active or afk"), **COMMON),
                embed=False,
            ),
        ],
        settings={
            "url": setting("ACTIVITYWATCH_URL", "http://localhost:5600/api/0", "ActivityWatch REST API base URL"),
            "fixture": setting("ACTIVITYWATCH_FIXTURE", None, "Recorded JSON fixture served instead of the API (tests)"),
            "since": setting("ACTIVITYWATCH_SINCE", None, "Earliest time to import on an empty cursor (ISO); default each bucket's creation"),
            "window_h": setting("ACTIVITYWATCH_WINDOW_H", "24", "Hours of events per request; one checkpoint per window"),
            "min_event_s": setting("ACTIVITYWATCH_MIN_EVENT_S", "5", "Drop event pieces shorter than this (alt-tab blips)"),
            "merge_gap_s": setting("ACTIVITYWATCH_MERGE_GAP_S", "60", "Merge same app+title (or url, file) events separated by at most this"),
            "min_session_s": setting("ACTIVITYWATCH_MIN_SESSION_S", "30", "Drop app/web/editor sessions with less active time than this"),
            "settle_s": setting("ACTIVITYWATCH_SETTLE_S", "600", "Events newer than now minus this are not final yet (AFK detection is retroactive)"),
            "hosts": setting("ACTIVITYWATCH_HOSTS", "", "Comma list of hostnames to import; empty imports all"),
            "exclude_apps": setting("ACTIVITYWATCH_EXCLUDE_APPS", "", "Comma list of app names never stored (e.g. a password manager)"),
            "include_incognito": setting("ACTIVITYWATCH_INCLUDE_INCOGNITO", "0", "1 also stores private/incognito tabs"),
        },
        run_mode="poll",
        default_interval_s=900,
        link_types=[],
    )

    def __init__(self, transport=None, now=None):
        super().__init__()
        if transport is None and self.settings["fixture"]:
            transport = fixture_transport(self.settings["fixture"])
        self.transport = transport
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.window = timedelta(hours=float(self.settings["window_h"]))
        self.min_event_s = float(self.settings["min_event_s"])
        self.merge_gap_s = float(self.settings["merge_gap_s"])
        self.min_session_s = float(self.settings["min_session_s"])
        self.hosts = {h.strip() for h in (self.settings["hosts"] or "").split(",") if h.strip()}
        self.exclude_apps = {a.strip().lower() for a in (self.settings["exclude_apps"] or "").split(",") if a.strip()}
        self.incognito = self.settings["include_incognito"] == "1"

    def records(self, cursor, mode):
        state = copy.deepcopy(cursor) if cursor else {}
        positions = state.setdefault("buckets", {})
        horizon = self.now() - timedelta(seconds=float(self.settings["settle_s"]))
        aw = ActivityWatch(self.settings["url"], transport=self.transport)
        try:
            buckets = aw.buckets()  # unreachable server: raise, the run fails
            afk_of = {}
            for bucket_id, bucket in sorted(buckets.items()):
                bucket = dict(bucket, id=bucket.get("id") or bucket_id)
                if bucket.get("type") == "afkstatus":
                    afk_of.setdefault(host_of(bucket), bucket)
            streams = []
            for bucket_id, bucket in sorted(buckets.items()):
                bucket = dict(bucket, id=bucket.get("id") or bucket_id)
                channel = KINDS.get(bucket.get("type"))
                host = host_of(bucket)
                if channel is None:
                    log.info("skipping bucket %s of type %s", bucket["id"], bucket.get("type"))
                    continue
                if self.hosts and host not in self.hosts:
                    continue
                streams.append((bucket, channel, host))
            failed = 0
            for bucket, channel, host in streams:
                try:
                    yield from self.stream(aw, bucket, channel, host, afk_of.get(host), positions, state, horizon)
                except (httpx.HTTPError, ValueError, KeyError) as error:
                    # One bucket failing must not stop the others.
                    failed += 1
                    log.warning("bucket %s failed: %s", bucket["id"], type(error).__name__)
                    yield coverage(channel, scope_key=bucket["id"], status="partial", reasons=["source_error"])
            if streams and failed == len(streams):
                raise RuntimeError("every ActivityWatch bucket failed")
        finally:
            aw.close()

    def start_of(self, bucket):
        if self.settings["since"]:
            return S.parse(self.settings["since"])
        created = bucket.get("created")
        return S.parse(created) if created else datetime(2016, 1, 1, tzinfo=timezone.utc)

    def stream(self, aw, bucket, channel, host, afk_bucket, positions, state, horizon):
        bucket_id = bucket["id"]
        # Nothing newer than the bucket's last update exists yet; its last
        # event may still grow, so it stays unfinished until newer data comes.
        # Window sessions also wait for the AFK data covering them.
        for source in (bucket, afk_bucket or {}):
            last = source.get("last_updated") or (source.get("metadata") or {}).get("end")
            if last:
                horizon = min(horizon, S.parse(last))
        afk_bucket = afk_bucket["id"] if afk_bucket else None
        lo = S.parse(positions[bucket_id]) if bucket_id in positions else self.start_of(bucket)
        span = self.window
        while lo < horizon:
            hi = min(lo + span, horizon)
            events = aw.events(bucket_id, lo, hi)
            pieces = S.clip(events, lo, hi)
            filtered = False
            if channel != AFK and afk_bucket:
                pieces = S.intersect(pieces, S.active_intervals(aw.events(afk_bucket, lo, hi), lo, hi))
                filtered = True
            if channel == AFK:
                done, hold = S.window(pieces, lambda d: (d.get("status"),), hi, min_event_s=0, merge_gap_s=self.merge_gap_s, min_session_s=0)
            else:
                done, hold = S.window(
                    pieces, KEYS[channel], hi,
                    min_event_s=self.min_event_s, merge_gap_s=self.merge_gap_s, min_session_s=self.min_session_s,
                )
            next_lo = hold or hi
            if next_lo <= lo and hi < horizon:
                span *= 2  # one session longer than the window: widen and retry
                continue
            for session in done:
                message = self.message(channel, bucket, host, session, filtered)
                if message:
                    yield message
            if next_lo <= lo:
                break  # only an unfinished session is left: wait for more data
            positions[bucket_id] = next_lo.isoformat()
            yield Checkpoint(cursor=copy.deepcopy(state))
            lo, span = next_lo, self.window

    def message(self, channel, bucket, host, session, filtered):
        data = session.data
        common = {
            "host": host,
            "bucket": bucket["id"],
            "duration_s": round(session.duration_s, 3),
            "active_s": round(session.active_s, 3),
            "event_count": session.events,
            "mapping_revision": MAPPING_REVISION,
        }
        if channel == WINDOWS:
            app = str(data.get("app") or "unknown app")
            if app.lower() in self.exclude_apps:
                return None
            title = str(data.get("title") or "")
            fields_ = dict(app=app, title=title, afk_filtered=filtered, **common)
            content = f"{app}: {title}" if title else app
        elif channel == WEB:
            if data.get("incognito") and not self.incognito:
                return None
            url = str(data.get("url") or "")
            title = str(data.get("title") or "")
            browser = bucket["id"].split("_", 1)[0].removeprefix("aw-watcher-web-") or None
            fields_ = dict(
                url=url, title=title, domain=domain_of(url), browser=browser,
                audible=any(bool(p.data.get("audible")) for p in session.pieces),
                afk_filtered=filtered, **common,
            )
            content = f"{title} ({domain_of(url) or url})" if title else url or "browser tab"
        elif channel == EDITOR:
            project, file = data.get("project"), data.get("file")
            fields_ = dict(
                project=project, file=file, language=data.get("language"),
                editor=bucket.get("client"), afk_filtered=filtered, **common,
            )
            content = "Editing " + (str(file) if file else "a file") + (f" in {project}" if project else "")
        else:
            status = "active" if data.get("status") == "not-afk" else "afk"
            fields_ = dict(status=status, **common)
            content = ("At the computer" if status == "active" else "Away from the computer") + f" for {duration(session.duration_s)}"
        if channel != AFK:
            content += f" ({duration(session.active_s)})"
        start = session.start.astimezone(timezone.utc)
        day = start.date().isoformat()
        return {
            "channel": channel,
            "external_id": f"{host}/{bucket['id']}/{start.isoformat()}",
            "thread": {"external_id": f"{host}:{day}", "title": f"{host} {day}"},
            "happened_at": start.isoformat(),
            "ended_at": session.end.astimezone(timezone.utc).isoformat(),
            "content": content,
            "fields": {k: v for k, v in fields_.items() if v is not None},
            "payload": {
                "bucket": bucket["id"],
                "data": data,
                "pieces": [[p.start.isoformat(), p.end.isoformat()] for p in session.pieces][:500],
            },
        }


KEYS = {
    WINDOWS: lambda d: (d.get("app"), d.get("title")),
    WEB: lambda d: (d.get("url"), d.get("title")),
    EDITOR: lambda d: (d.get("project"), d.get("file"), d.get("language")),
}

__all__ = ["ActivityWatchPlugin", "MAPPING_REVISION", "main"]


def main(argv=None):
    run(ActivityWatchPlugin, argv)
