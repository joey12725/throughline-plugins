"""GitHub REST client with pagination and rate-limit handling.

- Primary limits: every response carries X-RateLimit-Remaining/Reset. When
  the remaining count reaches zero the next request waits for the reset.
- 403/429 answers that are rate limits (Retry-After, X-RateLimit-Remaining: 0
  or a "secondary rate limit" message) are retried after the advertised wait,
  else with exponential backoff (60 s doubling); 5xx and transport errors are
  retried with a short backoff.
- A wait longer than ``max_wait_s`` raises RateLimited, so a run stops
  cleanly at its last checkpoint instead of sleeping for an hour.
"""

import logging
import time
import httpx

log = logging.getLogger(__name__)

SEARCH_CAP = 1000


class RateLimited(Exception):
    def __init__(self, wait_s):
        super().__init__(f"GitHub rate limit: next request allowed in {int(wait_s)} s")
        self.wait_s = wait_s


def _next_link(header):
    for part in (header or "").split(","):
        section = part.split(";")
        if len(section) >= 2 and section[1].strip() == 'rel="next"':
            return section[0].strip().strip("<>")
    return None


class GitHub:
    def __init__(self, token, base_url="https://api.github.com", *, transport=None, sleep=time.sleep,
                 clock=time.time, max_wait_s=900, retries=5, per_page=100):
        self.http = httpx.Client(
            base_url=base_url.rstrip("/"),
            transport=transport,
            timeout=60,
            follow_redirects=False,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "throughline-github",
            },
        )
        self.sleep = sleep
        self.clock = clock
        self.max_wait_s = max_wait_s
        self.retries = retries
        self.per_page = per_page
        self.blocked_until = 0.0
        self.requests = 0

    def close(self):
        self.http.close()

    def _wait(self, seconds, why):
        seconds = max(0.0, seconds)
        if seconds > self.max_wait_s:
            raise RateLimited(seconds)
        if seconds:
            log.info("waiting %.0f s (%s)", seconds, why)
            self.sleep(seconds)

    def _limit_wait(self, response, attempt):
        """Seconds to wait for a rate-limited answer, or None if it is not one."""
        if response.status_code not in (403, 429):
            return None
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass
        if response.headers.get("x-ratelimit-remaining") == "0":
            reset = float(response.headers.get("x-ratelimit-reset") or 0)
            return reset - self.clock() + 1
        text = response.text.lower()
        if response.status_code == 429 or "rate limit" in text:
            return 60 * 2**attempt
        return None

    def request(self, path, params=None, headers=None):
        if self.blocked_until > self.clock():
            self._wait(self.blocked_until - self.clock(), "primary rate limit")
        for attempt in range(self.retries + 1):
            try:
                self.requests += 1
                response = self.http.get(path, params=params, headers=headers)
            except httpx.TransportError:
                if attempt == self.retries:
                    raise
                self._wait(min(2**attempt, 30), "transport error")
                continue
            if response.headers.get("x-ratelimit-remaining") == "0":
                self.blocked_until = float(response.headers.get("x-ratelimit-reset") or 0) + 1
            wait = self._limit_wait(response, attempt)
            if wait is not None and attempt < self.retries:
                self._wait(wait, f"rate limited ({response.status_code})")
                continue
            if response.status_code >= 500 and attempt < self.retries:
                self._wait(min(2**attempt, 30), f"server error {response.status_code}")
                continue
            response.raise_for_status()
            return response
        raise RuntimeError("unreachable")

    def get(self, path, params=None, headers=None):
        return self.request(path, params, headers).json()

    def paginate(self, path, params=None, headers=None):
        """Yield (item, page_done) pairs; page_done is True for a page's last item."""
        params = dict(params or {}, per_page=self.per_page)
        url = path
        while url:
            response = self.request(url, params, headers)
            body = response.json()
            items = body.get("items", []) if isinstance(body, dict) else body
            for index, item in enumerate(items):
                yield item, index == len(items) - 1
            # The next link is absolute and carries every query parameter.
            url = _next_link(response.headers.get("link"))
            if url and httpx.URL(url).host != self.http.base_url.host:
                raise ValueError("refusing a pagination link to another host")
            params = None

    def search_total(self, kind, q):
        body = self.get(f"/search/{kind}", {"q": q, "per_page": 1})
        return int(body.get("total_count") or 0)
