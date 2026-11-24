"""HTTP access to one Mastodon instance's client API.

The client only ever talks to the configured base URL: pagination follows
the max_id/min_id values in a response's Link header, never the URL itself.
A recorded-response file (MASTODON_FIXTURE) or an injected httpx transport
replaces the network for tests.
"""

import json
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

LINK = re.compile(r'<([^>]+)>\s*;\s*rel="?([a-z]+)"?')


class SourceError(RuntimeError):
    """The instance answered with an error (status code only, never the token)."""


def page_params(link_header):
    """{rel: {param: value}} for the pagination params of a Link header."""
    out = {}
    for url, rel in LINK.findall(link_header or ""):
        query = parse_qs(urlsplit(url).query)
        out[rel] = {
            key: values[0]
            for key, values in query.items()
            if key in ("max_id", "min_id", "since_id") and values
        }
    return out


def fixture_transport(path):
    """An httpx.MockTransport answering from a recorded-response JSON file:
    a list of {method?, path, params?, status?, json|text, headers?}. The
    first entry whose method, path and params (a subset of the request's
    query; list values match repeated keys) all match answers; the host is
    ignored. An unmatched request is a 404 so a missing recording is loud."""
    entries = json.loads(Path(path).read_text(encoding="utf-8"))

    def matches(entry, request):
        if (entry.get("method") or "GET").upper() != request.method:
            return False
        if entry["path"] != request.url.path:
            return False
        for key, want in (entry.get("params") or {}).items():
            have = request.url.params.get_list(key)
            if want is None:  # the key must be absent
                if have:
                    return False
                continue
            want = want if isinstance(want, list) else [want]
            if sorted(map(str, want)) != sorted(have):
                return False
        return True

    def handler(request):
        for entry in entries:
            if matches(entry, request):
                body = (
                    json.dumps(entry["json"]).encode()
                    if "json" in entry
                    else (entry.get("text") or "").encode()
                )
                headers = dict(entry.get("headers") or {})
                if "json" in entry:
                    headers.setdefault("content-type", "application/json")
                return httpx.Response(
                    entry.get("status", 200), content=body, headers=headers
                )
        return httpx.Response(404, json={"error": "no recorded response"})

    return httpx.MockTransport(handler)


class MastodonClient:
    def __init__(
        self,
        base_url,
        token,
        *,
        transport=None,
        timeout=30.0,
        sleep=time.sleep,
        max_retries=3,
    ):
        base = (base_url or "").strip().rstrip("/")
        if not base:
            raise ValueError("MASTODON_BASE_URL is required")
        if "://" not in base:
            base = "https://" + base
        self.base_url = base
        self.host = (urlsplit(base).hostname or "").lower()
        if not self.host:
            raise ValueError("MASTODON_BASE_URL has no host")
        self.sleep = sleep
        self.max_retries = max_retries
        self.http = httpx.Client(
            base_url=base,
            transport=transport,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": "throughline-mastodon/0.1",
            },
        )

    def close(self):
        self.http.close()

    def get(self, path, params=None):
        """(json body, Link-header pagination params) for one GET."""
        attempt = 0
        while True:
            try:
                response = self.http.get(path, params=params)
            except httpx.TransportError as error:
                if attempt >= self.max_retries:
                    raise SourceError(
                        f"{path}: instance unreachable ({type(error).__name__})"
                    ) from None
                attempt += 1
                self.sleep(min(2**attempt, 30))
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt >= self.max_retries:
                    raise SourceError(f"{path}: HTTP {response.status_code}")
                attempt += 1
                self.sleep(self._retry_after(response, attempt))
                continue
            if response.status_code >= 400:
                raise SourceError(f"{path}: HTTP {response.status_code}")
            return response.json(), page_params(response.headers.get("link"))

    @staticmethod
    def _retry_after(response, attempt):
        value = response.headers.get("retry-after")
        if value and value.isdigit():
            return min(int(value), 300)
        reset = response.headers.get("x-ratelimit-reset")
        if reset:
            from datetime import datetime, timezone

            try:
                when = datetime.fromisoformat(reset.replace("Z", "+00:00"))
                wait = (when - datetime.now(timezone.utc)).total_seconds()
                return max(1, min(int(wait) + 1, 300))
            except ValueError:
                pass
        return min(2**attempt, 60)
