#!/usr/bin/env python3
"""Fetch a lossless Spotify polling snapshot beside an authenticated Composio MCP.

This file deliberately uses only the Python standard library so it can be copied
alone to the host that holds the Composio credential and run there. The credential
is read from the environment (COMPOSIO_MCP_TOKEN) or from the output of a
--token-command, and is never included in the emitted recording JSON.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shlex
import subprocess
import time
import urllib.error
import urllib.request
import uuid
import zlib
from pathlib import Path


DEFAULT_MCP_URL = "https://connect.composio.dev/mcp"
PAYLOAD_PREFIX = "THROUGHLINE_SPOTIFY_SNAPSHOT_B64="
ALLOWED_TOOLS = frozenset(
    {
        "SPOTIFY_GET_CURRENT_USER_S_PROFILE",
        "SPOTIFY_GET_PLAYBACK_STATE",
        "SPOTIFY_GET_RECENTLY_PLAYED_TRACKS",
        "SPOTIFY_GET_PLAYLIST",
    }
)


def unwrap(value):
    """Remove Composio's success/data envelopes without accepting failures."""
    for _ in range(7):
        if not isinstance(value, dict) or value.get("successful") is False:
            raise ValueError("Invalid Spotify response")
        if isinstance(value.get("data"), dict):
            value = value["data"]
        else:
            return value
    raise ValueError("Excessive Spotify response nesting")


def playlist_id(context):
    uri = (context or {}).get("uri")
    if (context or {}).get("type") != "playlist" or not isinstance(uri, str):
        return None
    marker = ":playlist:"
    candidate = uri.rsplit(marker, 1)[-1] if marker in uri else ""
    return candidate if candidate.isalnum() else None


class ComposioSnapshotClient:
    """Minimal read-only Composio MCP client with redacted failures."""

    def __init__(self, url, token, account, opener=None, sleep=time.sleep):
        self.url = url
        self.token = token
        self.account = account
        self.opener = opener or urllib.request.urlopen
        self.sleep = sleep
        self.session = "throughline-spotify-" + uuid.uuid4().hex

    def request(self, body):
        encoded = json.dumps(body).encode()
        for attempt in range(5):
            request = urllib.request.Request(
                self.url,
                data=encoded,
                headers={
                    "Authorization": "Bearer " + self.token,
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
            )
            try:
                with self.opener(request, timeout=180) as response:
                    return response.read().decode()
            except urllib.error.HTTPError as exc:
                if exc.code not in {429, 500, 502, 503, 504} or attempt == 4:
                    raise RuntimeError(f"Spotify Composio HTTP {exc.code}") from None
                delay = exc.headers.get("Retry-After", str(2**attempt))
                try:
                    delay_s = max(1.0, float(delay))
                except ValueError:
                    delay_s = float(2**attempt)
                self.sleep(min(120.0, delay_s))
            except (urllib.error.URLError, TimeoutError):
                if attempt == 4:
                    raise RuntimeError(
                        "Spotify Composio transport unavailable after retries"
                    ) from None
                self.sleep(float(2**attempt))
        raise RuntimeError("Spotify Composio retry budget exhausted")

    def call_many(self, calls):
        specs = []
        for label, tool, arguments in calls:
            if tool not in ALLOWED_TOOLS:
                raise ValueError("This Spotify snapshot client is read-only")
            specs.append((label, tool, arguments))
        code = (
            "import base64, json, zlib\n"
            f"_specs = json.loads({json.dumps(specs)!r})\n"
            "_out = {}\n"
            "for _label, _tool, _arguments in _specs:\n"
            + (
                f"    _result, _error = run_composio_tool(_tool, _arguments, account={self.account!r})\n"
                if self.account
                else "    _result, _error = run_composio_tool(_tool, _arguments)\n"
            )
            + 
            "    _out[_label] = {'result': _result, 'error': _error}\n"
            "_blob = zlib.compress(json.dumps(_out, ensure_ascii=False).encode())\n"
            f"print({PAYLOAD_PREFIX!r} + base64.b64encode(_blob).decode())\n"
        )
        body = {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex,
            "method": "tools/call",
            "params": {
                "name": "COMPOSIO_REMOTE_WORKBENCH",
                "arguments": {
                    "code_to_execute": code,
                    "session_id": self.session,
                    "current_step": "SYNC_SPOTIFY",
                    "thought": "Read Spotify playback and recently played data without truncation.",
                },
            },
        }
        text = self.request(body)
        try:
            if text.lstrip().startswith("{"):
                envelope = json.loads(text)
            else:
                candidates = [
                    json.loads(line[5:].strip())
                    for line in text.splitlines()
                    if line.startswith("data:")
                    and line[5:].strip() not in ("", "[DONE]")
                ]
                envelope = next(
                    item
                    for item in candidates
                    if isinstance(item, dict) and ("result" in item or "error" in item)
                )
            if envelope.get("error"):
                raise ValueError
            content = (envelope.get("result") or {}).get("content") or []
            workbench = json.loads(
                next(item["text"] for item in content if item.get("type") == "text")
            )
            data = workbench.get("data") or {}
            if (
                workbench.get("successful") is not True
                or data.get("error")
                or data.get("stderr")
            ):
                raise ValueError
            stdout = data.get("stdout") or ""
            payloads = [
                line[len(PAYLOAD_PREFIX) :]
                for line in stdout.splitlines()
                if line.startswith(PAYLOAD_PREFIX)
            ]
            if len(payloads) != 1:
                raise ValueError
            decoded = json.loads(
                zlib.decompress(base64.b64decode(payloads[0], validate=True))
            )
            if not isinstance(decoded, dict):
                raise ValueError
            return decoded
        except Exception:
            raise RuntimeError("Spotify Composio response invalid") from None


def build_snapshot(client, *, expected_user, now_ms=None):
    calls = [
        ("profile", "SPOTIFY_GET_CURRENT_USER_S_PROFILE", {}),
        ("recent", "SPOTIFY_GET_RECENTLY_PLAYED_TRACKS", {"limit": 50}),
        (
            "playback",
            "SPOTIFY_GET_PLAYBACK_STATE",
            {"additional_types": "track,episode"},
        ),
    ]
    first = client.call_many(calls)
    for required in ("profile", "recent", "playback"):
        if required not in first or first[required].get("error"):
            raise RuntimeError(f"Spotify {required} read failed")
    profile = unwrap(first["profile"]["result"])
    if not expected_user or profile.get("id") != expected_user:
        raise RuntimeError("Spotify identity does not match expected user")
    recent = unwrap(first["recent"]["result"])
    items = recent.get("items")
    if not isinstance(items, list) or len(items) > 50:
        raise RuntimeError("Spotify recently-played response invalid")
    playback = unwrap(first["playback"]["result"])
    if not isinstance(playback, dict) or not playback.get("item"):
        playback = None

    contexts = [item.get("context") for item in items if isinstance(item, dict)]
    if playback:
        contexts.append(playback.get("context"))
    playlist_ids = sorted(filter(None, {playlist_id(c) for c in contexts}))
    playlist_calls = [
        (
            "playlist:" + candidate,
            "SPOTIFY_GET_PLAYLIST",
            {"playlist_id": candidate, "fields": "name,owner(id,display_name)"},
        )
        for candidate in playlist_ids
    ]
    playlists = {}
    if playlist_calls:
        metadata = client.call_many(playlist_calls)
        for candidate in playlist_ids:
            entry = metadata.get("playlist:" + candidate) or {}
            if entry.get("error") or not entry.get("result"):
                continue
            try:
                value = unwrap(entry["result"])
            except ValueError:
                continue
            if isinstance(value, dict) and value.get("name"):
                playlists[candidate] = value
    return {
        "now": int(now_ms if now_ms is not None else time.time() * 1000),
        "playback": playback,
        "recent": items,
        "playlists": playlists,
    }


def read_token(command=None):
    """COMPOSIO_MCP_TOKEN, or the first line printed by ``command`` (for a
    secret manager on the host, e.g. ``pass show composio/mcp``)."""
    if not command:
        return os.environ.get("COMPOSIO_MCP_TOKEN", "").strip()
    try:
        return subprocess.check_output(
            shlex.split(command),
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=60,
        ).strip()
    except Exception:
        raise RuntimeError("Required Composio credential unavailable") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--mcp-url", default=DEFAULT_MCP_URL)
    parser.add_argument(
        "--account",
        default=os.environ.get("COMPOSIO_SPOTIFY_ACCOUNT"),
        help="Composio connected account id (env COMPOSIO_SPOTIFY_ACCOUNT); "
        "omit to use the default Spotify connection",
    )
    parser.add_argument(
        "--expected-user",
        default=os.environ.get("SPOTIFY_EXPECTED_USER"),
        help="Spotify user id the connection must belong to (env SPOTIFY_EXPECTED_USER); required",
    )
    parser.add_argument(
        "--token-command",
        help="command that prints the Composio MCP token; default: env COMPOSIO_MCP_TOKEN",
    )
    args = parser.parse_args()
    if not args.expected_user:
        parser.error("--expected-user (or SPOTIFY_EXPECTED_USER) is required")
    token = read_token(args.token_command)
    if not token:
        raise RuntimeError("Required Composio credential unavailable")
    snapshot = build_snapshot(
        ComposioSnapshotClient(args.mcp_url, token, args.account),
        expected_user=args.expected_user,
    )
    path = Path(args.out)
    temporary = path.with_name("." + path.name + "-" + uuid.uuid4().hex)
    temporary.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


if __name__ == "__main__":
    main()
