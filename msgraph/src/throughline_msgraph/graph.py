"""A small Microsoft Graph v1.0 GET client: bearer tokens, paging, retries."""

import time
from urllib.parse import urlsplit

import httpx

BASE = "https://graph.microsoft.com/v1.0"
HOST = "graph.microsoft.com"


class GraphError(RuntimeError):
    """Graph answered with an error (or could not be reached)."""


class DeltaExpired(GraphError):
    """A stored delta link is no longer valid: start a fresh delta round."""


class GraphClient:
    def __init__(self, tokens, transport=None, timeout=60.0, retries=4, sleep=time.sleep):
        self.tokens = tokens
        self.http = httpx.Client(transport=transport, timeout=timeout, follow_redirects=False)
        self.retries = retries
        self.sleep = sleep
        self.calls = 0

    def close(self):
        self.http.close()

    def get(self, url, params=None, headers=None):
        full = url if url.startswith("https://") else BASE + url
        if urlsplit(full).hostname != HOST:
            # Paging links are followed only on Graph itself, so the token is
            # never sent anywhere else.
            raise GraphError("refusing a link outside graph.microsoft.com")
        label = urlsplit(full).path
        refreshed = False
        for attempt in range(self.retries + 1):
            token = self.tokens.access()
            self.calls += 1
            try:
                response = self.http.get(
                    full, params=params, headers={"Authorization": f"Bearer {token}", **(headers or {})}
                )
            except httpx.HTTPError as error:
                if attempt < self.retries:
                    self.sleep(min(60, 2**attempt))
                    continue
                raise GraphError(f"GET {label} failed: {type(error).__name__}") from None
            status = response.status_code
            if status == 401 and not refreshed:
                refreshed = True
                self.tokens.access(force=True)
                continue
            if status in (429, 500, 502, 503, 504) and attempt < self.retries:
                self.sleep(_retry_after(response, attempt))
                continue
            if status == 410 or (status == 400 and _code(response) in ("syncStateNotFound", "resyncRequired", "SyncStateNotFound")):
                raise DeltaExpired(f"GET {label}: delta state expired ({_code(response) or status})")
            if status >= 400:
                raise GraphError(f"GET {label}: HTTP {status} {_code(response) or ''}".strip())
            return response.json()
        raise GraphError(f"GET {label}: gave up after retries")  # pragma: no cover

    def delta(self, url, params=None, headers=None, max_pages=None):
        """(items, next link, complete) following @odata.nextLink pages until
        an @odata.deltaLink (complete) or max_pages (next link to resume)."""
        items, pages, link = [], 0, url
        while True:
            page = self.get(link, params if link is url else None, headers)
            pages += 1
            items.extend(page.get("value") or [])
            if page.get("@odata.deltaLink"):
                return items, page["@odata.deltaLink"], True
            link = page.get("@odata.nextLink")
            if not link:
                raise GraphError("delta page has neither nextLink nor deltaLink")
            if max_pages and pages >= max_pages:
                return items, link, False


def _code(response):
    try:
        return (response.json().get("error") or {}).get("code")
    except (ValueError, AttributeError):
        return None


def _retry_after(response, attempt):
    value = response.headers.get("Retry-After")
    if value and value.isdigit():
        return min(120, int(value))
    return min(60, 2 ** (attempt + 1))
