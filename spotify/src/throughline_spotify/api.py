"""Spotify HTTP transport. Errors intentionally exclude bodies and credentials."""

import base64
import json
import os
import re
import time
import uuid
import zlib
from pathlib import Path

import httpx

ALLOWED_COMPOSIO_TOOLS = frozenset(
    {
        "SPOTIFY_GET_PLAYBACK_STATE",
        "SPOTIFY_GET_RECENTLY_PLAYED_TRACKS",
        "SPOTIFY_GET_PLAYLIST",
    }
)
_B64_PREFIX = "THROUGHLINE_SPOTIFY_B64="


def _coerce_status(value):
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _extract_composio_status(res, err):
    candidates = []
    if isinstance(res, dict):
        candidates.append(res)
        data = res.get("data")
        if isinstance(data, dict):
            candidates.append(data)
            if isinstance(data.get("data"), dict):
                candidates.append(data["data"])
    for obj in candidates:
        for key in ("status_code", "status"):
            code = _coerce_status(obj.get(key))
            if code is not None:
                return code
    texts = []
    for obj in candidates:
        for key in ("http_error", "error", "message"):
            val = obj.get(key)
            if isinstance(val, str) and val:
                texts.append(val)
    if isinstance(err, str) and err:
        texts.append(err)
    for text in texts:
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                nested = parsed.get("error")
                if isinstance(nested, dict):
                    code = _coerce_status(nested.get("status"))
                    if code is not None:
                        return code
        except ValueError:
            pass
        match = re.search(r"\b(204|401|403|404|429|5\d\d)\b", text)
        if match:
            return int(match.group(1))
    return None


class ComposioSpotifyAPI:
    """Read-only Spotify API transport backed by an authenticated Composio MCP endpoint."""

    def __init__(
        self,
        mcp_url,
        mcp_token,
        account=None,
        transport=None,
        sleep=time.sleep,
    ):
        self.http = httpx.Client(transport=transport, timeout=120)
        self.mcp_url = mcp_url
        self.mcp_token = mcp_token
        self.account = account or None
        self.session = "throughline-spotify-" + uuid.uuid4().hex
        self.sleep = sleep

    def close(self):
        self.http.close()

    def request(self, body):
        headers = {
            "Authorization": "Bearer " + self.mcp_token,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        for attempt in range(5):
            try:
                response = self.http.post(self.mcp_url, json=body, headers=headers)
            except httpx.TransportError:
                if attempt == 4:
                    raise RuntimeError("Spotify Composio transport failed") from None
                self.sleep(2**attempt)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == 4:
                    raise RuntimeError("Spotify Composio retry budget exhausted")
                delay = 2**attempt
                if response.status_code == 429:
                    try:
                        delay = max(
                            delay, float(response.headers.get("Retry-After", delay))
                        )
                    except ValueError:
                        pass
                self.sleep(delay)
                continue
            return response
        raise RuntimeError("Spotify Composio request failed")

    def _call_tool(self, tool, arguments):
        if tool not in ALLOWED_COMPOSIO_TOOLS:
            raise ValueError("Unsupported Spotify Composio tool")
        spec = {"tool": tool, "arguments": arguments}
        if self.account:
            spec["account"] = self.account
        code = (
            "import base64, json, zlib\n"
            f"_spec = json.loads({json.dumps(spec)!r})\n"
            "_kwargs = {'account': _spec['account']} if 'account' in _spec else {}\n"
            "_res, _err = run_composio_tool(_spec['tool'], _spec['arguments'], **_kwargs)\n"
            "_blob = json.dumps({'result': _res, 'error': _err}, ensure_ascii=False).encode()\n"
            f"print({_B64_PREFIX!r} + base64.b64encode(zlib.compress(_blob)).decode())\n"
        )
        rpc_body = {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex,
            "method": "tools/call",
            "params": {
                "name": "COMPOSIO_REMOTE_WORKBENCH",
                "arguments": {
                    "code_to_execute": code,
                    "session_id": self.session,
                    "current_step": "SYNC_SPOTIFY",
                    "thought": "Read full Spotify listening history and metadata without truncation.",
                },
            },
        }
        response = self.request(rpc_body)
        if response.status_code != 200:
            raise RuntimeError(f"Spotify Composio MCP failed ({response.status_code})")
        text = response.text
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
                    x
                    for x in candidates
                    if isinstance(x, dict) and ("result" in x or "error" in x)
                )
        except Exception:
            raise RuntimeError("Spotify Composio MCP response invalid") from None
        if not isinstance(envelope, dict) or envelope.get("error"):
            raise RuntimeError("Spotify Composio MCP rejected the request")
        try:
            contents = (envelope.get("result") or {}).get("content") or []
            wb_result = json.loads(
                next(x["text"] for x in contents if x.get("type") == "text")
            )
        except Exception:
            raise RuntimeError("Spotify Composio workbench payload invalid") from None
        wb_data = wb_result.get("data") if isinstance(wb_result, dict) else None
        if (
            not isinstance(wb_result, dict)
            or wb_result.get("successful") is not True
            or not isinstance(wb_data, dict)
            or wb_data.get("error")
            or wb_data.get("stderr")
        ):
            raise RuntimeError("Spotify Composio workbench execution failed")
        stdout = wb_data.get("stdout") or ""
        lines = [
            line[len(_B64_PREFIX) :]
            for line in stdout.splitlines()
            if line.startswith(_B64_PREFIX)
        ]
        if len(lines) != 1:
            raise RuntimeError("Spotify Composio response missing payload")
        try:
            decoded = json.loads(
                zlib.decompress(base64.b64decode(lines[0], validate=True))
            )
        except Exception:
            raise RuntimeError("Spotify Composio response payload corrupt") from None
        if not isinstance(decoded, dict):
            raise RuntimeError("Spotify Composio response payload invalid")
        return decoded.get("result"), decoded.get("error")

    def get(self, path, params=None, missing_ok=False):
        params = params or {}
        if path == "me/player":
            tool = "SPOTIFY_GET_PLAYBACK_STATE"
            arguments = {
                k: v
                for k, v in params.items()
                if k in ("additional_types", "market") and v is not None
            }
        elif path == "me/player/recently-played":
            tool = "SPOTIFY_GET_RECENTLY_PLAYED_TRACKS"
            arguments = {
                k: int(params[k])
                for k in ("limit", "after", "before")
                if params.get(k) is not None
            }
        elif path.startswith("playlists/"):
            playlist_id = path.split("/", 1)[1]
            tool = "SPOTIFY_GET_PLAYLIST"
            arguments = {"playlist_id": playlist_id}
            for k in ("fields", "market", "additional_types"):
                if params.get(k) is not None:
                    arguments[k] = params[k]
        else:
            raise ValueError(f"Unsupported Spotify path: {path}")

        res, err = self._call_tool(tool, arguments)
        status = _extract_composio_status(res, err)
        failed = bool(err) or (
            isinstance(res, dict)
            and (bool(res.get("error")) or res.get("successful") is False)
        )
        if failed:
            if missing_ok and status in (403, 404):
                return None
            if status == 204:
                return None
            raise RuntimeError(
                f"Spotify Composio API failed ({status})"
                if status
                else "Spotify Composio API failed"
            )
        data = res
        for _ in range(6):
            if isinstance(data, dict) and isinstance(data.get("data"), dict):
                data = data["data"]
            else:
                break
        if data is None:
            return None
        if isinstance(data, dict):
            if status == 204 and "item" not in data and "items" not in data:
                return None
            if missing_ok and status in (403, 404) and "name" not in data:
                return None
        return data


class SpotifyAPI:
    def __init__(
        self,
        client_id,
        client_secret,
        refresh_token,
        token_file=None,
        transport=None,
        sleep=time.sleep,
    ):
        self.http = httpx.Client(transport=transport, timeout=30)
        self.credentials = (client_id, client_secret)
        self.refresh_token = refresh_token
        self.token_file = token_file
        self.access_token = None
        self.expires = 0
        self.sleep = sleep

    def close(self):
        self.http.close()

    def request(self, method, url, **kwargs):
        for attempt in range(5):
            try:
                response = self.http.request(method, url, **kwargs)
            except httpx.TransportError:
                if attempt == 4:
                    raise RuntimeError("Spotify transport failed") from None
                self.sleep(2**attempt)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == 4:
                    raise RuntimeError("Spotify retry budget exhausted")
                delay = 2**attempt
                if response.status_code == 429:
                    try:
                        delay = max(
                            delay, float(response.headers.get("Retry-After", delay))
                        )
                    except ValueError:
                        pass
                self.sleep(delay)
                continue
            return response
        raise RuntimeError("Spotify request failed")

    def refresh(self):
        response = self.request(
            "POST",
            "https://accounts.spotify.com/api/token",
            auth=self.credentials,
            data={"grant_type": "refresh_token", "refresh_token": self.refresh_token},
        )
        if (
            response.status_code == 400
            and response.json().get("error") == "invalid_grant"
        ):
            self.refresh_token = None
            if self.token_file:
                save_token(self.token_file, {})
            raise RuntimeError(
                "Spotify authorization expired or revoked; run throughline-spotify-auth"
            )
        if response.status_code != 200:
            raise RuntimeError(f"Spotify token refresh failed ({response.status_code})")
        body = response.json()
        self.access_token = body["access_token"]
        self.expires = time.monotonic() + body.get("expires_in", 3600) - 60
        self.refresh_token = body.get("refresh_token", self.refresh_token)
        if self.token_file:
            save_token(self.token_file, {"refresh_token": self.refresh_token})

    def get(self, path, params=None, missing_ok=False):
        for retry in range(2):
            if not self.access_token or time.monotonic() >= self.expires:
                self.refresh()
            response = self.request(
                "GET",
                "https://api.spotify.com/v1/" + path,
                params=params,
                headers={"Authorization": "Bearer " + self.access_token},
            )
            if response.status_code == 401 and retry == 0:
                self.access_token = None
                continue
            if response.status_code == 204:
                return None
            if missing_ok and response.status_code in (403, 404):
                return None
            if response.status_code != 200:
                raise RuntimeError(
                    f"Spotify API failed ({response.status_code}); check app access and scopes"
                )
            return response.json()
        raise RuntimeError("Spotify authorization failed")


def save_token(path, body):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    # No tokens ever enter a Throughline cursor, payload, or log.
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(body, stream)
    os.replace(temporary, path)
