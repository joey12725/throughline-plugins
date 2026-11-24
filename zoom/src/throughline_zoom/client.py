"""Zoom REST client: Server-to-Server OAuth, paging, transcript download.

The access token lives in memory only (one hour, refreshed early or on a
401) and is never logged or returned. Tests inject an httpx transport, or
point ZOOM_FIXTURE at a recorded-response file (see fixture_transport).
"""

import json
import logging
import time
from pathlib import Path
from urllib.parse import parse_qsl, quote

import httpx

log = logging.getLogger("throughline_zoom")

API = "https://api.zoom.us/v2"
OAUTH = "https://zoom.us/oauth/token"


class ZoomError(RuntimeError):
    def __init__(self, status, code=None, message=""):
        super().__init__(f"Zoom API HTTP {status}" + (f" code {code}" if code else ""))
        self.status = status
        self.code = code
        self.message = message


def meeting_path_id(uuid):
    """Zoom: a meeting UUID that begins with / or contains // must be
    double URL-encoded; any other UUID is encoded once."""
    once = quote(str(uuid), safe="")
    if str(uuid).startswith("/") or "//" in str(uuid):
        return quote(once, safe="")
    return once


def fixture_transport(path):
    """A MockTransport answering from a recorded JSON list of
    {method, path, params?, status?, json | text, headers?}. `path` is the
    raw (still percent-encoded) URL path; `params` is a subset that must
    match the query. First match wins; the host is ignored; no match is a
    404 so a missing recording fails loudly."""
    rules = json.loads(Path(path).read_text())

    def handler(request):
        raw = request.url.raw_path.decode()
        route = raw.split("?", 1)[0]
        query = dict(parse_qsl(raw.split("?", 1)[1])) if "?" in raw else {}
        for rule in rules:
            if rule.get("method", "GET") != request.method or rule["path"] != route:
                continue
            if any(query.get(k) != str(v) for k, v in (rule.get("params") or {}).items()):
                continue
            status = rule.get("status", 200)
            headers = rule.get("headers") or {}
            if "json" in rule:
                return httpx.Response(status, json=rule["json"], headers=headers)
            return httpx.Response(status, text=rule.get("text", ""), headers=headers)
        return httpx.Response(404, json={"code": 404, "message": "no recording for " + route})

    return httpx.MockTransport(handler)


class Zoom:
    def __init__(
        self,
        account_id,
        client_id,
        client_secret,
        transport=None,
        sleep=time.sleep,
        clock=time.monotonic,
    ):
        self._account = account_id
        self._basic = (client_id, client_secret)
        self._token = None
        self._expires = 0.0
        self._sleep = sleep
        self._clock = clock
        self.api = API
        self.http = httpx.Client(
            transport=transport, timeout=60, follow_redirects=True
        )

    def close(self):
        self.http.close()

    def _access_token(self, force=False):
        if not force and self._token and self._clock() < self._expires:
            return self._token
        response = self.http.post(
            OAUTH,
            params={"grant_type": "account_credentials", "account_id": self._account},
            auth=self._basic,
        )
        if response.status_code != 200:
            # Never include the response body: it could echo credentials.
            raise ZoomError(response.status_code, message="token request refused")
        body = response.json()
        self._token = body["access_token"]
        # Refresh a minute early; Zoom issues one-hour tokens.
        self._expires = self._clock() + max(60, int(body.get("expires_in", 3600)) - 60)
        return self._token

    def request(self, url, params=None, *, raw=False):
        """GET with bearer auth; refreshes the token once on 401 and waits
        out 429s (Retry-After) a few times. Returns JSON, or the response
        when raw (transcript downloads)."""
        if not url.startswith("http"):
            url = self.api + url
        refreshed = False
        for attempt in range(5):
            token = self._access_token()
            response = self.http.get(
                url, params=params, headers={"Authorization": "Bearer " + token}
            )
            if response.status_code == 401 and not refreshed:
                refreshed = True
                self._access_token(force=True)
                continue
            if response.status_code == 429 and attempt < 4:
                try:
                    wait = float(response.headers.get("Retry-After") or 0)
                except ValueError:
                    wait = 0
                self._sleep(min(max(wait, 2 ** attempt), 60))
                continue
            if response.status_code >= 400:
                code = message = None
                try:
                    body = response.json()
                    code, message = body.get("code"), body.get("message")
                except ValueError:
                    pass
                raise ZoomError(response.status_code, code, message or "")
            return response if raw else response.json()
        raise ZoomError(response.status_code, message="retries exhausted")

    def me(self):
        return self.request("/users/me")

    def recordings(self, start, end, page_size, token=None):
        params = {"from": start, "to": end, "page_size": page_size}
        if token:
            params["next_page_token"] = token
        return self.request("/users/me/recordings", params)

    def participants(self, uuid):
        out, token = [], None
        while True:
            params = {"page_size": 300}
            if token:
                params["next_page_token"] = token
            page = self.request(
                f"/report/meetings/{meeting_path_id(uuid)}/participants", params
            )
            out.extend(page.get("participants") or [])
            token = page.get("next_page_token")
            if not token:
                return out

    def download_text(self, url):
        response = self.request(url, raw=True)
        response.encoding = response.encoding or "utf-8"
        return response.text
