"""OAuth 2.0 device-code and refresh-token flows for Microsoft identity (v2.0
endpoints), as a public client: your own Entra app registration's client id,
no client secret, no MSAL.

The token cache is a JSON file written with 0600 permissions (atomic
replace). It holds a refresh token: treat it like a password.
"""

import json
import logging
import os
import stat
import time
from pathlib import Path

import httpx

LOG = logging.getLogger("throughline_msgraph")
LOGIN = "https://login.microsoftonline.com"
DEFAULT_SCOPES = "offline_access User.Read Mail.Read Calendars.Read"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
EARLY_S = 120  # Refresh this long before the access token expires.


class AuthError(RuntimeError):
    """Sign-in is missing, expired or was refused."""


def load(path):
    path = Path(path).expanduser()
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        raise AuthError(f"no token cache at {path}; run throughline-msgraph-login first") from None
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        LOG.warning("token cache %s is readable by others; setting it to 0600", path)
        os.chmod(path, 0o600)
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def save(path, data):
    """Write the cache atomically, never readable by group or others."""
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def _error(response):
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    # error_description never contains the tokens we sent.
    text = body.get("error_description") or ""
    return f"{body.get('error') or response.status_code}: {text.splitlines()[0] if text else ''}".strip()


def _requested(scope):
    """The scopes to ask for: as first requested, always with offline_access
    (the token answer's `scope` omits it, and a refresh without it can come
    back without a new refresh token)."""
    words = (scope or DEFAULT_SCOPES).split()
    return " ".join(words if "offline_access" in words else ["offline_access", *words])


def _tokens(body, client_id, tenant, scope, now):
    return {
        "client_id": client_id,
        "tenant": tenant,
        "scope": _requested(scope),
        "granted_scope": body.get("scope"),
        "token_type": body.get("token_type", "Bearer"),
        "access_token": body["access_token"],
        "refresh_token": body.get("refresh_token"),
        "expires_at": int(now + int(body.get("expires_in", 3600))),
    }


def device_login(http, client_id, tenant="common", scope=DEFAULT_SCOPES, notify=print, sleep=time.sleep, clock=time.time):
    """Run the device-code flow; returns the cache dict to save."""
    response = http.post(f"{LOGIN}/{tenant}/oauth2/v2.0/devicecode", data={"client_id": client_id, "scope": scope})
    if response.status_code != 200:
        raise AuthError("device code request refused: " + _error(response))
    start = response.json()
    notify(start.get("message") or f"Open {start['verification_uri']} and enter the code {start['user_code']}")
    interval = int(start.get("interval", 5))
    deadline = clock() + int(start.get("expires_in", 900))
    while clock() < deadline:
        sleep(interval)
        answer = http.post(
            f"{LOGIN}/{tenant}/oauth2/v2.0/token",
            data={"grant_type": DEVICE_GRANT, "client_id": client_id, "device_code": start["device_code"]},
        )
        if answer.status_code == 200:
            return _tokens(answer.json(), client_id, tenant, scope, clock())
        try:
            code = answer.json().get("error")
        except ValueError:
            code = None
        if code == "authorization_pending":
            continue
        if code == "slow_down":
            interval += 5
            continue
        raise AuthError("sign-in failed: " + _error(answer))
    raise AuthError("the device code expired before sign-in completed")


class Tokens:
    """Access tokens for Graph: a static token (MSGRAPH_ACCESS_TOKEN), or the
    cache file refreshed with its refresh token when it is about to expire."""

    def __init__(self, cache_path=None, *, static=None, http=None, clock=time.time, on_change=None):
        self.on_change = on_change  # Called with the token strings after each load/refresh.
        self.cache_path = cache_path
        self.static = static
        self.http = http
        self.clock = clock
        self.cache = None

    def access(self, force=False):
        if self.static:
            if force:
                raise AuthError("MSGRAPH_ACCESS_TOKEN was refused; it may have expired")
            return self.static
        if self.cache is None:
            self.cache = load(self.cache_path)
            self._changed()
        if force or not self.cache.get("access_token") or self.cache.get("expires_at", 0) - EARLY_S <= self.clock():
            self.refresh()
        return self.cache["access_token"]

    def refresh(self):
        cache = self.cache
        if not cache.get("refresh_token"):
            raise AuthError("token cache has no refresh token; run throughline-msgraph-login again")
        tenant = cache.get("tenant") or "common"
        http = self.http or httpx.Client(timeout=60)
        try:
            response = http.post(
                f"{LOGIN}/{tenant}/oauth2/v2.0/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": cache["client_id"],
                    "refresh_token": cache["refresh_token"],
                    "scope": _requested(cache.get("scope")),
                },
            )
        except httpx.HTTPError as error:
            raise AuthError(f"token refresh failed: {type(error).__name__}") from None
        finally:
            if self.http is None:
                http.close()
        if response.status_code != 200:
            raise AuthError("token refresh refused (" + _error(response) + "); run throughline-msgraph-login again")
        fresh = _tokens(response.json(), cache["client_id"], tenant, cache.get("scope"), self.clock())
        # Microsoft may rotate the refresh token; keep the old one otherwise.
        fresh["refresh_token"] = fresh["refresh_token"] or cache["refresh_token"]
        self.cache = fresh
        self._changed()
        save(self.cache_path, fresh)

    def _changed(self):
        if self.on_change:
            self.on_change(self.values())

    def values(self):
        """Token strings currently held (for log redaction)."""
        if self.static:
            return [self.static]
        return [v for v in ((self.cache or {}).get("access_token"), (self.cache or {}).get("refresh_token")) if v]
