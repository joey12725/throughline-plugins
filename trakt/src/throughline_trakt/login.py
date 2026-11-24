"""throughline-trakt-login: Trakt device-code OAuth.

Reads TRAKT_CLIENT_ID and TRAKT_CLIENT_SECRET, prints a code to enter at
trakt.tv/activate, polls until the owner approves, then writes the token file
(TRAKT_TOKEN_FILE, default ~/.config/throughline/trakt-token.json) with mode
0600. Tokens are never printed.
"""

import argparse
import os
import sys
import time

import httpx

from .client import API, TokenStore, base_headers, protect, token_from_response
from .common import ApiError, load_transport

FAILURES = {
    404: "Invalid device code",
    409: "This device code was already used",
    410: "The device code expired before it was approved; run the login again",
    418: "Access was denied on Trakt",
}


def device_login(
    client_id,
    client_secret,
    store,
    *,
    transport=None,
    out=print,
    sleep=time.sleep,
    clock=time.monotonic,
):
    """Run the device flow and save the token. Returns the user settings (or None)."""
    protect(client_id, client_secret)
    client = httpx.Client(
        base_url=API,
        headers=dict(base_headers(client_id), **{"User-Agent": "throughline-connector/0.1"}),
        transport=transport,
        timeout=30.0,
    )
    try:
        response = client.post("/oauth/device/code", json={"client_id": client_id})
        if response.status_code >= 400:
            raise ApiError(
                f"HTTP {response.status_code} requesting a device code (check TRAKT_CLIENT_ID)"
            )
        device = response.json()
        interval = max(1, int(device.get("interval") or 5))
        deadline = clock() + int(device.get("expires_in") or 600)
        out(
            f"Open {device.get('verification_url') or 'https://trakt.tv/activate'} "
            f"and enter the code {device['user_code']}"
        )
        while True:
            if clock() > deadline:
                raise ApiError(FAILURES[410])
            sleep(interval)
            response = client.post(
                "/oauth/device/token",
                json={
                    "code": device["device_code"],
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
            )
            status = response.status_code
            if status == 200:
                break
            if status == 400:  # pending: the owner has not approved yet
                continue
            if status == 429:  # slow down
                interval += 1
                continue
            if status in FAILURES:
                raise ApiError(FAILURES[status])
            if status >= 500:
                continue
            raise ApiError(f"HTTP {status} polling for the device token")
        token = token_from_response(response.json())
        store.save(token)
        settings = None
        me = client.get(
            "/users/settings", headers={"Authorization": "Bearer " + token["access_token"]}
        )
        if me.status_code == 200:
            settings = me.json()
        user = ((settings or {}).get("user") or {}).get("username")
        out(f"Signed in{' as ' + user if user else ''}; token saved to {store.path}")
        return settings
    finally:
        client.close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="throughline-trakt-login",
        description="Authorize Throughline to read your Trakt history (device code flow).",
    )
    parser.add_argument(
        "--token-file",
        default=os.getenv("TRAKT_TOKEN_FILE"),
        help="Where to save the token (default: TRAKT_TOKEN_FILE or "
        "~/.config/throughline/trakt-token.json)",
    )
    args = parser.parse_args(argv)
    client_id = os.getenv("TRAKT_CLIENT_ID")
    client_secret = os.getenv("TRAKT_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("Set TRAKT_CLIENT_ID and TRAKT_CLIENT_SECRET first.", file=sys.stderr)
        raise SystemExit(2)
    try:
        device_login(
            client_id,
            client_secret,
            TokenStore(args.token_file),
            transport=load_transport(os.getenv("TRAKT_TEST_TRANSPORT")),
        )
    except ApiError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
