"""Source boundaries: injectable HTTP, recorded pages, or user-installed command."""

import json
import subprocess
import time

import httpx


def request(client, method, url, **kwargs):
    for attempt in range(5):
        try:
            response = client.request(method, url, **kwargs)
            response.raise_for_status()
            return response.json()
        except (httpx.TransportError, httpx.HTTPStatusError) as error:
            retryable = (
                not isinstance(error, httpx.HTTPStatusError)
                or error.response.status_code == 429
                or error.response.status_code >= 500
            )
            if not retryable or attempt == 4:
                # Response bodies and request arguments can contain credentials.
                raise RuntimeError(
                    "YouTube OAuth/API request failed; check credentials and quota"
                ) from None
            time.sleep(min(0.1 * 2**attempt, 2))


def subscriptions(secrets, transport=None):
    with httpx.Client(timeout=60, transport=transport) as client:
        auth = request(
            client,
            "POST",
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": secrets["client_id"],
                "client_secret": secrets["client_secret"],
                "refresh_token": secrets["refresh_token"],
                "grant_type": "refresh_token",
            },
        )
        token = auth.get("access_token")
        if not token:
            raise RuntimeError("OAuth response has no access token")
        page = None
        seen = set()
        rows = []
        while True:
            params = {"part": "snippet", "mine": "true", "maxResults": 50}
            if page:
                params["pageToken"] = page
            result = request(
                client,
                "GET",
                "https://www.googleapis.com/youtube/v3/subscriptions",
                params=params,
                headers={"Authorization": "Bearer " + token},
            )
            if not isinstance(result.get("items"), list):
                raise ValueError("Incomplete subscriptions response")
            rows.extend(result["items"])
            page = result.get("nextPageToken")
            if not page:
                return rows
            if page in seen:
                raise ValueError("Repeated subscriptions page token")
            seen.add(page)


def ytdlp(command, browser, feed):
    if not browser:
        raise ValueError("YOUTUBE_BROWSER is required when yt-dlp is enabled")
    try:
        result = subprocess.run(
            [
                *command,
                "--ignore-config",
                "--no-cache-dir",
                "--flat-playlist",
                "--skip-download",
                "--dump-single-json",
                "--cookies-from-browser",
                browser,
                feed,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=600,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("yt-dlp failed to start or timed out") from None
    if result.returncode:
        raise RuntimeError("yt-dlp failed; no snapshot removals applied")
    try:
        data = json.loads(result.stdout)
        entries = data["entries"]
        if not isinstance(entries, list) or any(
            not isinstance(e, dict) or not e.get("id") for e in entries
        ):
            raise ValueError()
        return entries
    except (ValueError, KeyError, TypeError):
        raise ValueError(
            "Incomplete yt-dlp JSON; no snapshot removals applied"
        ) from None
