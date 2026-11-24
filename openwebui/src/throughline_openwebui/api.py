"""A small Open WebUI REST client (httpx).

Endpoints used (Open WebUI 0.3 - 0.6; all JSON, Bearer API key):

* GET /api/v1/chats/?page=N        chat list {id, title, updated_at, created_at},
                                   newest updated first, a page at a time
* GET /api/v1/chats/pinned         pinned chats (newer releases keep them out of
                                   the list above); 404/405 on older releases
* GET /api/v1/chats/archived?page=N archived chats; 404/405 when unsupported
* GET /api/v1/chats/{id}           one chat with its message tree

Pass `transport=httpx.MockTransport(...)` to run without a server; the
plugin's OPENWEBUI_FIXTURE setting builds one from recorded responses.
"""

import json
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

RETRY_STATUS = {429, 500, 502, 503, 504}


class NotFound(Exception):
    pass


class OpenWebUI:
    def __init__(self, url, api_key, *, transport=None, timeout=30.0, attempts=3, sleep=time.sleep):
        self.http = httpx.Client(
            base_url=url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            transport=transport,
            timeout=timeout,
            follow_redirects=True,
        )
        self.attempts = max(1, attempts)
        self.sleep = sleep
        self.requests = 0

    def close(self):
        self.http.close()

    def get(self, path, params=None):
        for attempt in range(self.attempts):
            self.requests += 1
            try:
                response = self.http.get(path, params=params)
            except httpx.TransportError:
                if attempt + 1 == self.attempts:
                    raise
                self.sleep(2**attempt)
                continue
            if response.status_code in RETRY_STATUS and attempt + 1 < self.attempts:
                self.sleep(2**attempt)
                continue
            if response.status_code in (404, 405):
                raise NotFound(path)
            if response.status_code in (401, 403):
                raise PermissionError(
                    f"Open WebUI refused the API key ({response.status_code}); create one under "
                    "Settings > Account > API keys and make sure API keys are enabled"
                )
            response.raise_for_status()
            return response.json()
        raise RuntimeError("unreachable")

    def chat_pages(self, path):
        """Pages of a paginated chat list, until an empty page (or a page with
        nothing new: a release that ignores `page` returns everything once)."""
        page, seen = 1, set()
        while True:
            items = self.get(path, params={"page": page})
            if not isinstance(items, list) or not items:
                return
            ids = {i.get("id") for i in items if isinstance(i, dict)}
            if ids <= seen:
                return
            seen |= ids
            yield items
            page += 1

    def pinned(self):
        try:
            items = self.get("/api/v1/chats/pinned")
        except NotFound:
            return []
        return items if isinstance(items, list) else []

    def chat(self, chat_id):
        return self.get(f"/api/v1/chats/{chat_id}")


def fixture_transport(path):
    """A MockTransport answering from a recorded file:
    {"routes": {"/api/v1/chats/?page=1": <json body>, ...}}; anything else is 404."""
    routes = json.loads(Path(path).read_text(encoding="utf-8"))["routes"]

    def handle(request):
        key = request.url.path
        if request.url.query:
            query = dict(httpx.QueryParams(request.url.query))
            key += "?" + urlencode(sorted(query.items()))
        if key in routes:
            return httpx.Response(200, json=routes[key])
        return httpx.Response(404, json={"detail": "Not Found"})

    return httpx.MockTransport(handle)
