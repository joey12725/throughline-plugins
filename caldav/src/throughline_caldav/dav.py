"""Minimal read-only CalDAV (RFC 4791) and ICS feed client over httpx.

PROPFIND for discovery (current-user-principal -> calendar-home-set ->
calendars, with displayname, getctag and sync-token), REPORT calendar-query
(time-range, with or without calendar-data) and calendar-multiget. Plain ICS
feeds are fetched with GET and conditional headers.
"""

from datetime import UTC
from datetime import timedelta
from urllib.parse import quote, unquote, urljoin, urlsplit
from xml.etree import ElementTree

import httpx

DAV, CAL, CS, ICAL = "DAV:", "urn:ietf:params:xml:ns:caldav", "http://calendarserver.org/ns/", "http://apple.com/ns/ical/"
HEAD = '<?xml version="1.0" encoding="utf-8"?>'
DISCOVER = (
    HEAD + f'<d:propfind xmlns:d="{DAV}" xmlns:c="{CAL}" xmlns:cs="{CS}"><d:prop>'
    "<d:resourcetype/><d:displayname/><d:current-user-principal/><c:calendar-home-set/>"
    "<cs:getctag/><d:sync-token/><c:supported-calendar-component-set/></d:prop></d:propfind>"
)
LIST = (
    HEAD + f'<d:propfind xmlns:d="{DAV}" xmlns:c="{CAL}" xmlns:cs="{CS}"><d:prop>'
    "<d:resourcetype/><d:displayname/><cs:getctag/><d:sync-token/>"
    "<c:supported-calendar-component-set/><c:calendar-timezone/></d:prop></d:propfind>"
)


PAD = timedelta(days=1)
SAFE = "/:@!$&'()*+,;=~"


def _path(base, href):
    """The unquoted path of an href (servers differ in percent-encoding)."""
    return unquote(urlsplit(urljoin(base, href)).path)


def _complete(calendar_url, found):
    """False when an answer was truncated (507) or carried any non-200 entry,
    the collection's own included."""
    return all(code == 200 for _, code, _ in found)


class DavError(RuntimeError):
    """The server could not be read (HTTP error, auth failure, bad XML)."""


def stamp(value):
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _query(low, high, data):
    prop = "<d:getetag/>" + ("<c:calendar-data/>" if data else "")
    return (
        HEAD + f'<c:calendar-query xmlns:d="{DAV}" xmlns:c="{CAL}"><d:prop>{prop}</d:prop>'
        '<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">'
        # Padded by a day: servers judge floating and all-day events in UTC.
        f'<c:time-range start="{stamp(low - PAD)}" end="{stamp(high + PAD)}"/>'
        "</c:comp-filter></c:comp-filter></c:filter></c:calendar-query>"
    )


def _multiget(hrefs):
    refs = "".join(f"<d:href>{_escape(quote(h, safe=SAFE))}</d:href>" for h in hrefs)
    return (
        HEAD + f'<c:calendar-multiget xmlns:d="{DAV}" xmlns:c="{CAL}">'
        f"<d:prop><d:getetag/><c:calendar-data/></d:prop>{refs}</c:calendar-multiget>"
    )


def _escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _tag(element):
    return element.tag.split("}", 1)[-1]


def parse_multistatus(text):
    """[(href, status code, {prop name: element})] of a 207 answer; only
    properties in a 200 propstat are returned."""
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as error:
        raise DavError(f"unreadable WebDAV answer: {error}") from None
    result = []
    for response in root.iter(f"{{{DAV}}}response"):
        href = (response.findtext(f"{{{DAV}}}href") or "").strip()
        props, code = {}, None
        status = response.findtext(f"{{{DAV}}}status")
        if status:
            code = _code(status)
        for propstat in response.findall(f"{{{DAV}}}propstat"):
            if _code(propstat.findtext(f"{{{DAV}}}status") or "") != 200:
                continue
            code = code or 200
            for prop in propstat.findall(f"{{{DAV}}}prop"):
                for child in prop:
                    props[_tag(child)] = child
        result.append((href, code, props))
    return result


def _code(status):
    parts = status.split()
    return int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None


def _href(props, name):
    element = props.get(name)
    if element is None:
        return None
    found = element.find(f"{{{DAV}}}href")
    return found.text.strip() if found is not None and found.text else None


def _text(props, name):
    element = props.get(name)
    return element.text.strip() if element is not None and element.text else None


def _is_calendar(props):
    kinds = props.get("resourcetype")
    return kinds is not None and kinds.find(f"{{{CAL}}}calendar") is not None


def _components(props):
    element = props.get("supported-calendar-component-set")
    if element is None:
        return None
    return [c.get("name") for c in element if c.get("name")]


class DavClient:
    def __init__(self, username=None, password=None, transport=None, timeout=60.0):
        self.http = httpx.Client(
            auth=(username, password or "") if username else None,
            transport=transport,
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "throughline-caldav/0.1"},
        )

    def close(self):
        self.http.close()

    def _send(self, method, url, body=None, depth=None, headers=None, label=None):
        label = label or _safe(url)
        extra = dict(headers or {})
        if body is not None:
            extra["Content-Type"] = "application/xml; charset=utf-8"
        if depth is not None:
            extra["Depth"] = str(depth)
        try:
            response = self.http.request(method, url, content=body, headers=extra)
        except httpx.HTTPError as error:
            raise DavError(f"{method} {label} failed: {type(error).__name__}") from None
        if response.status_code in (401, 403):
            raise DavError(f"{method} {label}: authentication failed ({response.status_code})")
        return response

    def _multistatus(self, method, url, body, depth):
        response = self._send(method, url, body, depth)
        if response.status_code != 207:
            raise DavError(f"{method} {_safe(url)}: HTTP {response.status_code}")
        return parse_multistatus(response.text)

    def discover(self, url):
        """Calendars ({url, href, name, ctag, sync_token}) reachable from `url`
        (a calendar, a calendar home, a principal or the server root)."""
        found = self._multistatus("PROPFIND", url, DISCOVER, 0)
        props = found[0][2] if found else {}
        if _is_calendar(props):
            return [self._calendar(url, url, props)]
        home = _href(props, "calendar-home-set")
        if not home:
            principal = _href(props, "current-user-principal")
            if not principal and urlsplit(url).path in ("", "/"):
                found = self._multistatus("PROPFIND", urljoin(url, "/.well-known/caldav"), DISCOVER, 0)
                props = found[0][2] if found else {}
                principal = _href(props, "current-user-principal")
                home = _href(props, "calendar-home-set")
            if principal and not home:
                found = self._multistatus("PROPFIND", urljoin(url, principal), DISCOVER, 0)
                home = _href(found[0][2], "calendar-home-set") if found else None
        if not home:
            raise DavError(f"no CalDAV calendar home found at {_safe(url)}")
        home_url = urljoin(url, home)
        calendars = []
        for href, _, props in self._multistatus("PROPFIND", home_url, LIST, 1):
            if not _is_calendar(props):
                continue
            components = _components(props)
            if components is not None and "VEVENT" not in components:
                continue  # A task list or journal.
            calendars.append(self._calendar(home_url, href, props))
        return sorted(calendars, key=lambda c: c["href"])

    @staticmethod
    def _calendar(base, href, props):
        path = _path(base, href)
        return {
            "url": urljoin(base, href),
            "href": path,
            "name": _text(props, "displayname") or path.rstrip("/").rsplit("/", 1)[-1],
            "ctag": _text(props, "getctag"),
            "sync_token": _text(props, "sync-token"),
        }

    def etags(self, calendar_url, low, high):
        """({href: etag} of the events overlapping [low, high), complete).
        complete is False when the server truncated the answer (507) or
        reported a non-200 status for any entry."""
        found = self._multistatus("REPORT", calendar_url, _query(low, high, False), 1)
        listing = {
            _path(calendar_url, href): _text(props, "getetag")
            for href, code, props in found
            if code == 200 and href and _path(calendar_url, href) != _path(calendar_url, calendar_url)
        }
        return listing, _complete(calendar_url, found)

    def objects(self, calendar_url, low, high):
        """({href: (etag, ics text)} of the events overlapping [low, high), complete)."""
        found = self._multistatus("REPORT", calendar_url, _query(low, high, True), 1)
        return self._objects(calendar_url, found), _complete(calendar_url, found)

    def multiget(self, calendar_url, hrefs, chunk=50):
        """({href: (etag, ics text)}, {hrefs the server answered 404 for}).
        An href missing from the answer is neither: its state is unknown."""
        result, gone = {}, set()
        hrefs = sorted(hrefs)
        for start in range(0, len(hrefs), chunk):
            part = hrefs[start : start + chunk]
            found = self._multistatus("REPORT", calendar_url, _multiget(part), 1)
            wanted = set(part)
            result.update({h: v for h, v in self._objects(calendar_url, found).items() if h in wanted})
            gone.update(
                _path(calendar_url, href) for href, code, _ in found if code == 404 and _path(calendar_url, href) in wanted
            )
        return result, gone - set(result)

    @staticmethod
    def _objects(calendar_url, found):
        result = {}
        for href, code, props in found:
            data = props.get("calendar-data")
            if code != 200 or data is None or not data.text:
                continue
            result[_path(calendar_url, href)] = (_text(props, "getetag"), data.text)
        return result

    def feed(self, url, etag=None, modified=None, label="ICS feed"):
        """(status, text, etag, last-modified) of an ICS subscription URL."""
        headers = {"Accept": "text/calendar, */*;q=0.5"}
        if etag:
            headers["If-None-Match"] = etag
        if modified:
            headers["If-Modified-Since"] = modified
        response = self._send("GET", url.replace("webcal://", "https://", 1), headers=headers, label=label)
        if response.status_code == 304:
            return 304, None, etag, modified
        if response.status_code != 200:
            raise DavError(f"{label}: HTTP {response.status_code}")
        return 200, response.text, response.headers.get("ETag"), response.headers.get("Last-Modified")


def _safe(url):
    """A URL fit for logs: scheme, host and path only (ICS feed URLs often
    carry a private token in the path or query, so the path is shortened)."""
    parts = urlsplit(url)
    path = parts.path if len(parts.path) <= 40 else parts.path[:20] + "..."
    return f"{parts.scheme}://{parts.hostname or ''}{path}"
