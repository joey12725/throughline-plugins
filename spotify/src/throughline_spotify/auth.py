"""Interactive Authorization Code flow, bound to the local loopback interface."""

import argparse
import os
import secrets
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

from .api import SpotifyAPI, save_token

SCOPES = "user-read-recently-played user-read-playback-state playlist-read-private playlist-read-collaborative"
REDIRECT = "http://127.0.0.1:8765/callback"


def callback_values(path, state):
    parsed = urlparse(path)
    query = parse_qs(parsed.query)
    if parsed.path != "/callback" or not secrets.compare_digest(
        query.get("state", [""])[0], state
    ):
        raise ValueError("Invalid OAuth callback state or path")
    if "error" in query or not query.get("code"):
        raise ValueError("Spotify authorization denied")
    return query["code"][0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--token-file", default=os.getenv("SPOTIFY_TOKEN_FILE"), required=False
    )
    args = parser.parse_args()
    if not args.token_file:
        parser.error(
            "set SPOTIFY_TOKEN_FILE or --token-file to a private path outside the repository"
        )
    client_id = os.environ["SPOTIFY_CLIENT_ID"]
    client_secret = os.environ["SPOTIFY_CLIENT_SECRET"]
    state = secrets.token_urlsafe(32)
    result = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            try:
                result["code"] = callback_values(self.path, state)
                self.send_response(200)
                message = b"Authorization received. You may close this tab."
            except ValueError:
                self.send_response(400)
                message = b"Invalid or denied authorization."
            self.end_headers()
            self.wfile.write(message)

    url = "https://accounts.spotify.com/authorize?" + urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": REDIRECT,
            "scope": SCOPES,
            "state": state,
        }
    )
    with HTTPServer(("127.0.0.1", 8765), Handler) as server:
        server.timeout = 1
        print(
            "Opening Spotify authorization in the local browser (five minute timeout)."
        )
        webbrowser.open(url)
        deadline = time.monotonic() + 300
        while "code" not in result and time.monotonic() < deadline:
            server.handle_request()
    if "code" not in result:
        raise SystemExit("Authorization timed out; run the helper again")
    api = SpotifyAPI(client_id, client_secret, None)
    try:
        response = api.request(
            "POST",
            "https://accounts.spotify.com/api/token",
            auth=(client_id, client_secret),
            data={
                "grant_type": "authorization_code",
                "code": result["code"],
                "redirect_uri": REDIRECT,
            },
        )
        if response.status_code != 200:
            raise SystemExit("Authorization exchange failed; run the helper again")
        save_token(args.token_file, {"refresh_token": response.json()["refresh_token"]})
        print("Refresh token saved to the selected private file.")
    finally:
        api.close()
