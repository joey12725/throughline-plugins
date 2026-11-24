"""Run explicitly: python -m throughline_youtube.auth --output private-token.json.

The SDK runner has no extra-command extension. Tokens never go to stdout.
"""

import argparse
import base64
import hashlib
import json
import os
import secrets
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

SCOPE = "https://www.googleapis.com/auth/youtube.readonly"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    client_id = os.environ["YOUTUBE_CLIENT_ID"]
    client_secret = os.environ["YOUTUBE_CLIENT_SECRET"]
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    result = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            query = parse_qs(urlparse(self.path).query)
            valid = query.get("state") == [state] and bool(query.get("code"))
            if valid:
                result["code"] = query["code"][0]
            self.send_response(200 if valid else 400)
            self.end_headers()
            self.wfile.write(
                b"Authorization received. Return to the terminal."
                if valid
                else b"Authorization rejected."
            )

    # Reserve output before requesting consent; never overwrite existing secrets.
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with HTTPServer(("127.0.0.1", 0), Handler) as server:
            server.timeout = 1
            redirect = f"http://127.0.0.1:{server.server_port}/"
            url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(
                dict(
                    client_id=client_id,
                    redirect_uri=redirect,
                    response_type="code",
                    scope=SCOPE,
                    access_type="offline",
                    prompt="consent",
                    state=state,
                    code_challenge=challenge,
                    code_challenge_method="S256",
                )
            )
            if not webbrowser.open(url):
                raise RuntimeError(
                    "Could not open a browser; run the helper on a desktop with a browser"
                )
            print("Waiting for browser consent (five-minute timeout).")
            deadline = time.monotonic() + 300
            while not result and time.monotonic() < deadline:
                server.handle_request()
            if not result:
                raise RuntimeError("Authorization timed out")
            with httpx.Client(timeout=60) as client:
                response = client.post(
                    "https://oauth2.googleapis.com/token",
                    data=dict(
                        client_id=client_id,
                        client_secret=client_secret,
                        code=result["code"],
                        code_verifier=verifier,
                        redirect_uri=redirect,
                        grant_type="authorization_code",
                    ),
                )
                if response.status_code != 200:
                    raise RuntimeError("OAuth token exchange failed")
                token = response.json().get("refresh_token")
                if not token:
                    raise RuntimeError(
                        "No refresh token returned; revoke the prior grant and retry consent"
                    )
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            fd = None
            json.dump({"refresh_token": token}, out)
        print(
            "Refresh token saved to the requested private file. Restrict its filesystem permissions."
        )
    except Exception:
        if fd is not None:
            os.close(fd)
        os.unlink(args.output)
        raise SystemExit("Authorization failed; no token was saved") from None


if __name__ == "__main__":
    main()
