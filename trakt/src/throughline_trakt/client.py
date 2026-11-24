"""Trakt API access: the OAuth token file, refresh and an authenticated client.

Access and refresh tokens come from a file, not the environment, so the SDK
does not know to redact them. Every token value this module touches is added
to a log Redactor attached to the root handlers, and no error message ever
includes one.
"""

import json
import logging
import os
import time
from pathlib import Path

from throughline_sdk.plugin import Redactor

from .common import ApiError, Http

log = logging.getLogger(__name__)

API = "https://api.trakt.tv"
REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"
DEFAULT_TOKEN_FILE = "~/.config/throughline/trakt-token.json"
LOGIN_HINT = "run throughline-trakt-login"
# Refresh this many seconds before the recorded expiry.
EXPIRY_MARGIN_S = 300

_REDACTOR = Redactor([])


def protect(*values):
    """Redact these values from every log line from now on."""
    fresh = [v for v in values if isinstance(v, str) and v and v not in _REDACTOR.values]
    if fresh:
        _REDACTOR.values = sorted(_REDACTOR.values + fresh, key=len, reverse=True)
    for handler in logging.getLogger().handlers:
        if _REDACTOR not in handler.filters:
            handler.addFilter(_REDACTOR)


def token_path(value=None):
    return Path(os.path.expanduser(value or DEFAULT_TOKEN_FILE))


def token_from_response(body, now=None):
    """The stored token shape from an /oauth/token or /oauth/device/token reply."""
    if not isinstance(body, dict) or not body.get("access_token") or not body.get("refresh_token"):
        raise ApiError("Trakt returned no token")
    created = int(body.get("created_at") or now or time.time())
    expires_in = int(body.get("expires_in") or 0)
    return {
        "access_token": body["access_token"],
        "refresh_token": body["refresh_token"],
        "expires_at": created + expires_in if expires_in else None,
        "created_at": created,
        "scope": body.get("scope"),
    }


class TokenStore:
    def __init__(self, path):
        self.path = token_path(path)

    def load(self):
        try:
            token = json.loads(self.path.read_text())
        except FileNotFoundError:
            raise ApiError(f"No Trakt token file at {self.path}; {LOGIN_HINT}") from None
        except (OSError, ValueError):
            raise ApiError(f"Unreadable Trakt token file at {self.path}; {LOGIN_HINT}") from None
        if not isinstance(token, dict) or not token.get("access_token"):
            raise ApiError(f"Trakt token file at {self.path} has no access token; {LOGIN_HINT}")
        protect(token.get("access_token"), token.get("refresh_token"))
        return token

    def save(self, token):
        """Atomically replace the token file, readable by the owner only."""
        protect(token.get("access_token"), token.get("refresh_token"))
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(token, handle, indent=1)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp, 0o600)
            os.replace(temp, self.path)
        except BaseException:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise


def base_headers(client_id):
    return {
        "Content-Type": "application/json",
        "trakt-api-version": "2",
        "trakt-api-key": client_id,
    }


class TraktApi:
    """Authenticated GETs with refresh on expiry or a 401."""

    def __init__(
        self,
        client_id,
        client_secret,
        store,
        transport=None,
        delay_s=0.0,
        clock=time.time,
        sleep=time.sleep,
    ):
        protect(client_id, client_secret)
        self.client_id = client_id
        self.client_secret = client_secret
        self.store = store
        self.clock = clock
        self.http = Http(
            API, headers=base_headers(client_id), transport=transport, delay_s=delay_s, sleep=sleep
        )
        self.token = store.load()
        self.refreshes = 0

    def close(self):
        self.http.close()

    def expired(self):
        expires = self.token.get("expires_at")
        return bool(expires) and float(expires) - EXPIRY_MARGIN_S <= self.clock()

    def refresh(self):
        refresh_token = self.token.get("refresh_token")
        if not refresh_token:
            raise ApiError(f"Trakt token expired and no refresh token is stored; {LOGIN_HINT}")
        response = self.http.request(
            "POST",
            "/oauth/token",
            json={
                "refresh_token": refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "redirect_uri": REDIRECT_URI,
                "grant_type": "refresh_token",
            },
        )
        if response.status_code in (400, 401, 403):
            raise ApiError(
                f"Trakt rejected the refresh token (HTTP {response.status_code}); {LOGIN_HINT}"
            )
        if response.status_code >= 400:
            raise ApiError(f"HTTP {response.status_code} refreshing the Trakt token")
        token = token_from_response(response.json(), now=self.clock())
        self.store.save(token)
        self.token = token
        self.refreshes += 1
        log.info("refreshed the Trakt access token")

    def get(self, path, params=None):
        """(json, headers) for an authenticated GET."""
        if self.expired():
            self.refresh()
        for attempt in (0, 1):
            response = self.http.request(
                "GET",
                path,
                params=params,
                headers={"Authorization": "Bearer " + self.token["access_token"]},
            )
            if response.status_code == 401 and attempt == 0:
                self.refresh()
                continue
            if response.status_code == 401:
                raise ApiError(f"Trakt rejected the access token; {LOGIN_HINT}")
            if response.status_code >= 400:
                raise ApiError(f"HTTP {response.status_code} for {path}")
            return response.json(), response.headers
        raise AssertionError("unreachable")

    def pages(self, path, params, page, limit):
        """One page: (items, page_count)."""
        body, headers = self.get(path, dict(params or {}, page=page, limit=limit))
        count = headers.get("X-Pagination-Page-Count")
        return body or [], int(count) if (count or "").isdigit() else (1 if body else 0)

    def all_pages(self, path, params, limit):
        out, page, count = [], 1, 1
        while page <= count:
            items, count = self.pages(path, params, page, limit)
            out.extend(items)
            page += 1
        return out
