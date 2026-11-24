"""Microsoft 365 / Outlook.com mail and calendar through Microsoft Graph.

Three channels with the same shapes as the IMAP / CalDAV connectors (and the
Gmail / Google Calendar ones): `msgraph.message` (one record per email,
threaded by conversationId), `msgraph.events` (occurrences, recurring
series already expanded by Graph's calendarView) and `msgraph.changes`
(event lifecycle). Change tracking uses Graph delta queries: messages delta
per mail folder, calendarView delta per calendar, each delta link stored in
the plugin cursor.
"""

import copy
import logging
import re
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from throughline_sdk import Channel, Checkpoint, Declaration, Manifest, Plugin, Record, coverage, run

from . import calmap, mailmap
from .auth import Tokens
from .graph import DeltaExpired, GraphClient, GraphError

LOG = logging.getLogger("throughline_msgraph")
MAIL = "msgraph.message"
EVENTS = "msgraph.events"
CHANGES = "msgraph.changes"
# 1: first mapping.
MAPPING_REVISION = 1
DEFAULT_CACHE = "~/.config/throughline/msgraph-token.json"
MESSAGE_SELECT = (
    "id,conversationId,internetMessageId,subject,from,sender,toRecipients,ccRecipients,"
    "bccRecipients,replyTo,receivedDateTime,sentDateTime,isRead,isDraft,flag,importance,"
    "hasAttachments,body,bodyPreview,parentFolderId,categories"
)
MAIL_EXTRA = [
    ("graph_message_id", "text", "Graph message id of the primary copy (changes when the message moves)"),
    ("graph_conversation_id", "text", "Graph conversationId (the thread)"),
    ("importance", "text", "low, normal or high"),
]

SETTINGS = [
    ("client_id", "MSGRAPH_CLIENT_ID", None, "Application (client) id of your own Entra app registration (public client)"),
    ("tenant", "MSGRAPH_TENANT", "common", "common, consumers (Outlook.com), organizations, or a tenant id"),
    ("token_cache", "MSGRAPH_TOKEN_CACHE", DEFAULT_CACHE, "Token cache written by throughline-msgraph-login (0600)"),
    ("account", "MSGRAPH_ACCOUNT", None, "Account label used in ids (default: the mailbox address from /me)"),
    ("owner", "MSGRAPH_OWNER_ADDRESSES", None, "Extra comma-separated addresses that are you (aliases)"),
    ("mail", "MSGRAPH_MAIL", "true", "Read mail"),
    ("folders", "MSGRAPH_MAIL_FOLDERS", "inbox,sentitems", "Comma-separated well-known folder names or folder ids"),
    ("since", "MSGRAPH_SINCE", None, "First mail round only: messages received on or after YYYY-MM-DD"),
    ("calendar", "MSGRAPH_CALENDAR", "true", "Read calendars"),
    ("calendars", "MSGRAPH_CALENDARS", "primary", "Comma-separated calendar ids; primary = the default calendar"),
    ("past_days", "MSGRAPH_PAST_DAYS", "30", "Rolling calendar window: days before today (UTC)"),
    ("future_days", "MSGRAPH_FUTURE_DAYS", "90", "Rolling calendar window: days after today"),
    ("window_start", "MSGRAPH_WINDOW_START", None, "Fixed calendar window start YYYY-MM-DD"),
    ("window_end", "MSGRAPH_WINDOW_END", None, "Fixed calendar window end YYYY-MM-DD, exclusive"),
    ("page_size", "MSGRAPH_PAGE_SIZE", "50", "Items per Graph page (Prefer: odata.maxpagesize)"),
    ("max_pages", "MSGRAPH_MAX_PAGES", "200", "Graph pages per folder or calendar per run; the rest continues next run"),
    ("batch_size", "MSGRAPH_BATCH_SIZE", "200", "Messages per acknowledged batch and checkpoint"),
    ("body_limit", "MSGRAPH_BODY_LIMIT", str(mailmap.DEFAULT_BODY_LIMIT), "Body characters kept in content (0 = all)"),
    ("links", "MSGRAPH_LINKS", "true", "Link calendar records to a calendar_event entity"),
    ("timeout", "MSGRAPH_TIMEOUT_S", "60", "HTTP timeout in seconds"),
    ("fixture", "MSGRAPH_FIXTURE", None, "Tests only: recorded HTTP answers (JSON) instead of the network"),
]


def _split(value):
    return [v.strip() for v in str(value or "").split(",") if v.strip()]


def _truthy(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _day(value):
    return datetime.combine(date.fromisoformat(value), time(), UTC)


def graph_time(value, zone=None):
    """An aware datetime from Graph's dateTimeTimeZone or an ISO string."""
    if isinstance(value, dict):
        text, zone = value.get("dateTime"), value.get("timeZone") or zone
    else:
        text = value
    if not text:
        return None
    text = re.sub(r"(\.\d{6})\d+", r"\1", str(text).replace("Z", "+00:00"))
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.utcoffset() is None:
        try:
            parsed = parsed.replace(tzinfo=UTC if not zone or zone.upper() == "UTC" else ZoneInfo(zone))
        except (ZoneInfoNotFoundError, ValueError):
            parsed = parsed.replace(tzinfo=UTC)  # A Windows zone name: Prefer UTC avoids this.
    return parsed


def _midnight(value):
    if value is None:
        return None
    return datetime.combine((value.astimezone(UTC) + timedelta(hours=12)).date(), time(), UTC)


def _address(entry):
    email = (entry or {}).get("emailAddress") or {}
    address = (email.get("address") or "").strip().lower()
    return {"address": address, "name": email.get("name") or None} if "@" in address else None


def _addresses(values):
    return [a for a in (_address(v) for v in values or []) if a]


def redact(values):
    """Add values to the runner's log redaction filter."""
    values = {v for v in values if v}
    for handler in logging.getLogger().handlers:
        for item in handler.filters:
            if hasattr(item, "clean") and isinstance(getattr(item, "values", None), list):
                item.values = sorted(set(item.values) | values, key=len, reverse=True)


class MsGraph(Plugin):
    manifest = Manifest(
        name="msgraph",
        version="0.1.0",
        channels=[
            Channel(
                name=MAIL,
                description=(
                    "Email from Microsoft 365 / Outlook.com through Microsoft Graph, one record per message "
                    "(deduplicated across folders by Message-ID); threads are Graph conversations; "
                    "happened_at is the received time"
                ),
                fields=[dict(name=n, type=t, description=d) for n, t, d in mailmap.COMMON_FIELDS + MAIL_EXTRA],
                conversation_whole_thread=True,
            ),
            Channel(
                name=EVENTS,
                description=(
                    "Event occurrences from Outlook calendars (recurring events expanded within the window); "
                    "the record time and subject are when the event happens"
                ),
                fields=calmap.EVENT_FIELDS,
            ),
            Channel(
                name=CHANGES,
                description=(
                    "Outlook event lifecycle: created, changed, cancelled, removed, restored. The record time "
                    "is the event's created/lastModified time, or the fetch time for changes found by snapshot diff"
                ),
                fields=calmap.CHANGE_FIELDS,
            ),
        ],
        settings={
            key: Declaration(env=env, required=False, default=default, description=text)
            for key, env, default, text in SETTINGS
        },
        secrets={
            "access_token": Declaration(
                env="MSGRAPH_ACCESS_TOKEN",
                required=False,
                description="Optional static bearer token instead of the token cache (expires within about an hour)",
            )
        },
        run_mode="poll",
        default_interval_s=900,
        link_types=["part_of", "about"],
    )

    # Test seams.
    transport = None
    auth_http = None
    clock = None

    def __init__(self):
        super().__init__()
        s = self.settings
        self.batch_size = int(s["batch_size"])
        if self.batch_size < 1:
            raise ValueError("MSGRAPH_BATCH_SIZE must be positive")
        if bool(s.get("window_start")) != bool(s.get("window_end")):
            raise ValueError("set both MSGRAPH_WINDOW_START and MSGRAPH_WINDOW_END, or neither")
        self.page_headers = {"Prefer": f'odata.maxpagesize={int(s["page_size"])}'}
        self.max_pages = int(s["max_pages"])

    def guard_tokens(self, values):
        """Tokens from the cache file are secrets too: redact them from logs
        and refuse records that contain them, like declared secrets."""
        for index, value in enumerate(v for v in values if v):
            self.secrets[f"cached_token_{index}"] = value
        redact(values)

    def now(self):
        return (self.clock or (lambda: datetime.now(UTC)))().astimezone(UTC).replace(microsecond=0)

    def window(self, now):
        s = self.settings
        if s.get("window_start"):
            return _day(s["window_start"]), _day(s["window_end"])
        today = datetime.combine(now.date(), time(), UTC)
        return today - timedelta(days=int(s["past_days"])), today + timedelta(days=int(s["future_days"]))

    def client(self):
        transport = self.transport
        if transport is None and self.settings.get("fixture"):
            from .fixture import transport_from_file

            transport = transport_from_file(self.settings["fixture"])
        tokens = Tokens(
            self.settings["token_cache"],
            static=self.secrets.get("access_token"),
            http=self.auth_http,
            on_change=self.guard_tokens,
        )
        return GraphClient(tokens, transport=transport, timeout=float(self.settings["timeout"]))

    # --- fetch ---------------------------------------------------------------------

    def attachments(self, client, item):
        if not item.get("hasAttachments"):
            return item
        page = client.get(f"/me/messages/{item['id']}/attachments", {"$select": "id,name,contentType,size,isInline"})
        return dict(item, attachments=page.get("value") or [])

    def fetch_mail(self, client, state, fetched, failed):
        known = state.setdefault("mail", {})
        folders, incremental = {}, []
        for name in _split(self.settings["folders"]):
            try:
                folder = client.get(f"/me/mailFolders/{name}", {"$select": "id,displayName"})
                folders[folder["id"]] = (name, folder.get("displayName") or name)
                entry = known.get(name) or {}
                link = entry.get("link")
                if link:
                    incremental.append(name)
                if link:
                    try:
                        items, link, complete = client.delta(link, headers=self.page_headers, max_pages=self.max_pages)
                    except DeltaExpired:
                        LOG.warning("mail delta for %s expired; reading the folder again", name)
                        link = None
                if not link:
                    if name in incremental:
                        incremental.remove(name)
                    params = {"$select": MESSAGE_SELECT}
                    if self.settings.get("since"):
                        params["$filter"] = f"receivedDateTime ge {self.settings['since']}T00:00:00Z"
                        params["$orderby"] = "receivedDateTime desc"
                    items, link, complete = client.delta(
                        f"/me/mailFolders/{folder['id']}/messages/delta", params, self.page_headers, self.max_pages
                    )
                for item in items:
                    if "@removed" in item or not item.get("id"):
                        continue  # Deletions and moves out are not mapped.
                    item = self.attachments(client, item)
                    fetched["mail"].append([name, folder.get("displayName") or name, item])
                known[name] = {"link": link, "complete": complete}
            except GraphError as error:
                LOG.warning("mail folder %s skipped: %s", name, error)
                failed.append((MAIL, name))
                incremental = [n for n in incremental if n != name]
        if len(folders) > 1 and incremental:
            self.other_copies(client, fetched, folders, incremental, failed)

    def other_copies(self, client, fetched, folders, incremental, failed):
        """Copies of this run's messages in folders read only incrementally
        (a folder read from scratch already delivered its copies), so a
        version lists every folder the message is in."""
        seen = {}
        for name, _, item in fetched["mail"]:
            if item.get("internetMessageId"):
                seen.setdefault(item["internetMessageId"], set()).add(name)
        for mid in sorted(seen):
            if all(name in seen[mid] for name in incremental):
                continue
            quoted = mid.replace("'", "''")
            try:
                page = client.get("/me/messages", {"$filter": f"internetMessageId eq '{quoted}'", "$select": MESSAGE_SELECT})
                for item in page.get("value") or []:
                    name, display = folders.get(item.get("parentFolderId"), (None, None))
                    if name in incremental and name not in seen[mid]:
                        fetched["mail"].append([name, display, self.attachments(client, item)])
                        seen[mid].add(name)
            except GraphError as error:
                LOG.warning("looking up other copies of a message failed: %s", error)
                if (MAIL, "copies") not in failed:
                    failed.append((MAIL, "copies"))

    def fetch_calendars(self, client, state, fetched, failed, low, high):
        known = state.setdefault("calendar", {})
        window = [calmap.iso(low), calmap.iso(high)]
        headers = dict(self.page_headers, Prefer=self.page_headers["Prefer"] + ', outlook.timezone="UTC"')
        for cid in _split(self.settings["calendars"]):
            base = "/me/calendar" if cid == "primary" else f"/me/calendars/{cid}"
            try:
                info = client.get(base, {"$select": "id,name"})
                entry = known.get(cid) or {}
                link = entry.get("link") if entry.get("window") == window else None
                complete = False
                if link:
                    try:
                        items, link, done = client.delta(link, headers=headers, max_pages=self.max_pages)
                    except DeltaExpired:
                        LOG.warning("calendar delta for %s expired; reading the window again", cid)
                        link = None
                if not link:
                    params = {"startDateTime": window[0], "endDateTime": window[1]}
                    view = "/me/calendarView/delta" if cid == "primary" else f"{base}/calendarView/delta"
                    items, link, done = client.delta(view, params, headers, self.max_pages)
                    complete = done
                fetched["calendar"].append(
                    {"calendar_id": cid, "name": info.get("name"), "items": items, "complete": complete}
                )
                known[cid] = {"link": link, "window": window}
            except GraphError as error:
                LOG.warning("calendar %s skipped: %s", cid, error)
                failed.append((EVENTS, cid))

    # --- mapping -----------------------------------------------------------------------

    def mail_messages(self, fetched, account, owner):
        """One message per Message-ID across the folders read in this run."""
        order = {name: index for index, name in enumerate(_split(self.settings["folders"]))}
        groups, latest = {}, {}
        for name, display, item in fetched:
            latest.pop((name, item.get("id")), None)  # The last state in the round wins.
            latest[(name, item.get("id"))] = (name, display, item)
        for name, display, item in latest.values():
            key = mailmap.message_id(item.get("internetMessageId"))
            if not key:
                sender = _address(item.get("from")) or {}
                key = mailmap.fallback_key(
                    item.get("sentDateTime"),
                    sender.get("address"),
                    ",".join(a["address"] for a in _addresses(item.get("toRecipients"))),
                    item.get("subject"),
                )
            groups.setdefault(key, []).append((order.get(name, 99), name, display, item))
        result = []
        for key in sorted(groups):
            copies = sorted(groups[key], key=lambda c: (c[0], c[3].get("id")))
            _, name, _, item = copies[0]
            happened = graph_time(item.get("receivedDateTime")) or graph_time(item.get("sentDateTime"))
            if happened is None:
                LOG.warning("message without a time skipped")
                continue
            body = item.get("body") or {}
            is_html = str(body.get("contentType") or "").lower() == "html"
            attachments = [
                {
                    "filename": a.get("name"),
                    "mime_type": a.get("contentType"),
                    "size": a.get("size"),
                    "content_id": None,
                    "inline": bool(a.get("isInline")),
                }
                for a in item.get("attachments") or []
            ]
            sender = _addresses([item.get("from") or item.get("sender")])
            headers = {
                k: v
                for k, v in (
                    ("message-id", item.get("internetMessageId")),
                    ("subject", item.get("subject")),
                    ("from", sender[0]["address"] if sender else None),
                    ("sent", item.get("sentDateTime")),
                )
                if v
            }
            mail = {
                "account": account,
                "provider": "msgraph",
                "key": key,
                "message_id": mailmap.message_id(item.get("internetMessageId")),
                "subject": item.get("subject"),
                "from": sender,
                "to": _addresses(item.get("toRecipients")),
                "cc": _addresses(item.get("ccRecipients")),
                "bcc": _addresses(item.get("bccRecipients")),
                "reply_to": _addresses(item.get("replyTo")),
                "date_header": item.get("sentDateTime"),
                "happened_at": happened,
                "folders": list(dict.fromkeys(c[2] for c in copies)),
                "labels": list(item.get("categories") or []),
                "is_unread": not item.get("isRead", True),
                "is_starred": ((item.get("flag") or {}).get("flagStatus") == "flagged"),
                "is_important": item.get("importance") == "high",
                "is_draft": bool(item.get("isDraft")),
                "in_sent": any(c[1].lower() == "sentitems" for c in copies),
                "plain": None if is_html else body.get("content"),
                "html": body.get("content") if is_html else None,
                "snippet": (item.get("bodyPreview") or "").strip() or None,
                "attachments": attachments,
                "size": None,
                "thread_id": ("conv:" + item["conversationId"]) if item.get("conversationId") else None,
                "headers": headers,
            }
            result.append(
                mailmap.map_mail(
                    mail,
                    channel=MAIL,
                    revision=MAPPING_REVISION,
                    owner=owner,
                    body_limit=int(self.settings["body_limit"]),
                    extra_fields={
                        "graph_message_id": item.get("id"),
                        "graph_conversation_id": item.get("conversationId"),
                        "importance": item.get("importance"),
                    },
                    extra_payload={
                        "graph_ids": {c[2]: c[3].get("id") for c in copies},
                        "conversation_id": item.get("conversationId"),
                        "is_read": item.get("isRead"),
                        "flag": (item.get("flag") or {}).get("flagStatus"),
                        "importance": item.get("importance"),
                        "categories": item.get("categories") or [],
                    },
                )
            )
        return result

    @staticmethod
    def occurrence(event, owner):
        all_day = bool(event.get("isAllDay"))
        start, end = graph_time(event.get("start")), graph_time(event.get("end"))
        if all_day:
            # Days, not instants: round to the nearest midnight, because
            # `Prefer: outlook.timezone="UTC"` may shift the day boundary.
            start = _midnight(start)
            end = _midnight(end)
        kind = event.get("type")
        series = event.get("seriesMasterId") if kind in ("occurrence", "exception") else None
        organizer = _address(event.get("organizer"))
        attendees = []
        for a in event.get("attendees") or []:
            who = _address(a) or {}
            attendees.append(
                {
                    "email": who.get("address"),
                    "name": who.get("name"),
                    "response": calmap.response((a.get("status") or {}).get("response")),
                    "optional": a.get("type") == "optional" or None,
                    "resource": a.get("type") == "resource" or None,
                    "self": bool(who.get("address") and who["address"] in owner) or None,
                }
            )
        body = event.get("body") or {}
        meeting = event.get("onlineMeeting") or {}
        show = str(event.get("showAs") or "").lower()
        return {
            "occurrence_id": event["id"],
            "unit_id": series or event["id"],
            "series_id": series,
            # Graph's iCalUId differs per occurrence of a series: only a
            # single event's matches the iCalendar UID other providers use.
            "ical_uid": event.get("iCalUId") if not series else None,
            "title": event.get("subject"),
            "status": "cancelled" if event.get("isCancelled") else "confirmed",
            "start": start,
            "end": end,
            "all_day": all_day,
            "time_zone": event.get("originalStartTimeZone"),
            "original_start": graph_time(event.get("originalStart")) if kind == "exception" else None,
            "place": ((event.get("location") or {}).get("displayName") or "").strip() or None,
            "description": body.get("content") or event.get("bodyPreview"),
            "organizer": {"email": organizer["address"], "name": organizer["name"]} if organizer else None,
            "creator": None,
            "attendees": attendees,
            "my_response": calmap.response((event.get("responseStatus") or {}).get("response")),
            "conference_url": meeting.get("joinUrl") or event.get("onlineMeetingUrl"),
            "html_link": event.get("webLink"),
            "event_type": kind,
            "visibility": event.get("sensitivity"),
            "transparency": ("transparent" if show == "free" else "opaque") if show else None,
            "created": graph_time(event.get("createdDateTime")),
            "updated": graph_time(event.get("lastModifiedDateTime")),
            "recurrence": [str(event["recurrence"])] if event.get("recurrence") else None,
            "source": {k: v for k, v in event.items() if not k.startswith("@odata.")},
        }

    def apply_calendar(self, mapper, account, item, low, high, observed, owner):
        cid = item["calendar_id"]
        mapper.set_name(account, cid, item.get("name"))
        mapper.prune(account, cid, low)
        removed, produced = [], set()
        last = {}
        for event in item["items"]:
            if event.get("id"):
                last.pop(event["id"], None)  # The last state in the round wins.
                last[event["id"]] = event
        for event in last.values():
            if "@removed" in event:
                removed.append(event["id"])
                continue
            if event.get("type") == "seriesMaster" or not event.get("id"):
                continue
            occ = self.occurrence(event, owner)
            if occ["start"] is None:
                continue
            if occ["series_id"]:
                mapper.series_seen(account, cid, occ["series_id"], occ)
            if event.get("type") != "occurrence":
                # A single event or an exception: its own lifecycle unit.
                mapper.unit(account, cid, dict(occ, unit_id=occ["occurrence_id"]), observed)
            mapper.occurrence(account, cid, occ)
            produced.add(occ["occurrence_id"])
        if item["complete"]:
            live = mapper.live_ids(account, cid, low=low, high=high)
            if not item["items"] and live:
                mapper.count("skipped_empty_window")
                return
            removed += [i for i in live if i not in produced]
        mapper.remove(account, cid, removed, observed, "deleted")

    # --- run ---------------------------------------------------------------------------

    def records(self, cursor, mode):
        state = copy.deepcopy(cursor or {})
        if state.get("mapping_revision") != MAPPING_REVISION:
            state = {"mapping_revision": MAPPING_REVISION}
        resume = state.pop("resume", None)
        base = copy.deepcopy(state)
        now = self.now()
        low, high = self.window(now)
        fetched, failed = {"mail": [], "calendar": []}, []
        client = self.client()
        try:
            try:
                me = client.get("/me", {"$select": "mail,userPrincipalName,displayName"})
            except GraphError as error:
                raise GraphError(f"Microsoft Graph is unreachable: {error}") from None
            account = (self.settings.get("account") or me.get("mail") or me.get("userPrincipalName") or "msgraph").lower()
            owner = {a.lower() for a in _split(self.settings.get("owner")) + [me.get("mail"), me.get("userPrincipalName")] if a}
            scopes = 0
            if _truthy(self.settings["mail"]):
                scopes += len(_split(self.settings["folders"]))
                self.fetch_mail(client, state, fetched, failed)
            if _truthy(self.settings["calendar"]):
                scopes += len(_split(self.settings["calendars"]))
                self.fetch_calendars(client, state, fetched, failed, low, high)
            if scopes and len([f for f in failed if f != (MAIL, "copies")]) >= scopes:
                raise GraphError("every mail folder and calendar failed; see the warnings above")
        finally:
            client.close()
        signature = calmap.digest([base, account, calmap.iso(low), calmap.iso(high), fetched], 64)
        offset, observed = 0, now
        if resume and resume.get("signature") == signature:
            offset = resume.get("offset", 0)
            observed = datetime.fromisoformat(resume["observed"])
        plan, links = {}, {}
        for message in self.mail_messages(fetched["mail"], account, owner):
            plan[(message["channel"], message["external_id"])] = message

        def emit(message, link):
            key = (message["channel"], message["external_id"])
            plan[key] = message
            if link:
                links[key] = link

        mapper = calmap.Mapper(state, events=EVENTS, changes=CHANGES, revision=MAPPING_REVISION, emit=emit)
        for item in fetched["calendar"]:
            self.apply_calendar(mapper, account, item, low, high, observed, owner)
        items = list(plan.items())
        with_links = _truthy(self.settings["links"])
        for start in range(offset, len(items), self.batch_size):
            chunk = items[start : start + self.batch_size]
            for _, message in chunk:
                yield message
            if with_links:
                for key, _ in chunk:
                    if key in links:
                        yield Record(kind="link", data=links[key])
            if start + self.batch_size < len(items):
                interim = copy.deepcopy(base)
                interim["resume"] = {"signature": signature, "offset": start + self.batch_size, "observed": observed.isoformat()}
                yield Checkpoint(cursor=interim)
        for channel, scope in failed:
            channels = (EVENTS, CHANGES) if channel == EVENTS else (MAIL,)
            for name in channels:
                yield coverage(name, scope_key=f"{account}:{scope}", status="partial", reasons=["source_error"])
        yield Checkpoint(cursor=state)


def main():
    run(MsGraph)
