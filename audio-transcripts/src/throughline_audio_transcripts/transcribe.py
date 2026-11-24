"""OpenAI-compatible speech to text: POST {base}/audio/transcriptions.

Works with OpenAI and with self-hosted servers that implement the same route
(faster-whisper-server/speaches, whisper.cpp server, LocalAI, Groq, ...).
Each request asks for `response_format=verbose_json` to get segments with
timestamps and falls back to `json` (one segment spanning the audio) when a
server refuses it. The result has the shape the other Throughline media
plugins use: {text, segments: [{start, end, text, speaker?}], language?,
duration?, timestamps}.
"""

import mimetypes
import tempfile
import time
from pathlib import Path

import httpx

# Formats the OpenAI endpoint accepts as uploads.
DIRECT = {".flac", ".m4a", ".mp3", ".mp4", ".mpeg", ".mpga", ".oga", ".ogg", ".wav", ".webm"}


class Transient(RuntimeError):
    """The endpoint is unreachable or overloaded: try the file again later."""


class Permanent(RuntimeError):
    """The endpoint refused this file: do not send it again."""


class NeedsFFmpeg(RuntimeError):
    """The file must be converted or split, and ffmpeg is not available."""


def parse(body, offset=0.0):
    if not isinstance(body, dict):
        body = {"text": str(body)}
    segments = []
    for segment in body.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        try:
            start, end = float(segment["start"]), float(segment["end"])
        except (KeyError, TypeError, ValueError):
            continue
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        item = {
            "start": round(offset + start, 3),
            "end": round(offset + max(end, start), 3),
            "text": text,
        }
        if segment.get("speaker") not in (None, ""):
            item["speaker"] = str(segment["speaker"])
        segments.append(item)
    result = {"text": str(body.get("text") or "").strip(), "segments": segments}
    if isinstance(body.get("language"), str) and body["language"]:
        result["language"] = body["language"]
    try:
        result["duration"] = float(body["duration"])
    except (KeyError, TypeError, ValueError):
        pass
    return result


class Endpoint:
    def __init__(
        self,
        base_url,
        api_key=None,
        model="whisper-1",
        *,
        language=None,
        transport=None,
        timeout=600,
        retries=3,
        max_bytes=24 * 1024 * 1024,
        chunk_seconds=1200,
        tools=None,
        sleep=time.sleep,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model or "whisper-1"
        self.language = language or None
        headers = {"Authorization": "Bearer " + api_key} if api_key else {}
        self.http = httpx.Client(timeout=timeout, headers=headers, transport=transport)
        self.retries = int(retries)
        self.max_bytes = int(max_bytes)
        self.chunk_seconds = max(1, int(chunk_seconds))
        self.tools = tools
        self.sleep = sleep
        self.verbose = None  # Unknown until the server accepts or refuses it.
        self.requests = 0

    def close(self):
        self.http.close()

    def transcript(self, path, duration=None):
        """Transcribe one file; long or unsupported files go through ffmpeg."""
        path = Path(path)
        size = path.stat().st_size
        direct = path.suffix.lower() in DIRECT
        fits = size <= self.max_bytes and (duration is None or duration <= self.chunk_seconds)
        if direct and fits:
            return self.finish(self.request(path), duration, 1)
        if not (self.tools and self.tools.ffmpeg):
            raise NeedsFFmpeg(
                "needs ffmpeg to "
                + ("convert" if not direct else "split")
                + " this file before upload"
            )
        with tempfile.TemporaryDirectory(prefix="throughline-audio-") as tmp:
            whole = self.tools.extract(path, Path(tmp) / "audio.ogg")
            if duration is None:
                duration, _ = self.tools.probe(whole)
            if whole.stat().st_size <= self.max_bytes and (
                duration is None or duration <= self.chunk_seconds
            ):
                return self.finish(self.request(whole), duration, 1)
            if not duration:
                raise Permanent("cannot measure the audio to split it")
            pieces, start, index = [], 0.0, 0
            while start < duration - 0.05:
                length = min(self.chunk_seconds, duration - start)
                chunk = self.tools.extract(
                    path, Path(tmp) / f"chunk{index:04d}.ogg", start=start, length=length
                )
                if chunk.stat().st_size > self.max_bytes:
                    raise Permanent("an audio chunk is larger than the upload limit")
                pieces.append(self.request(chunk, offset=start, span=length))
                chunk.unlink(missing_ok=True)
                start += length
                index += 1
        merged = {
            "text": " ".join(p["text"] for p in pieces if p["text"]).strip(),
            "segments": [s for p in pieces for s in p["segments"]],
        }
        language = next((p["language"] for p in pieces if p.get("language")), None)
        if language:
            merged["language"] = language
        return self.finish(merged, duration, len(pieces))

    @staticmethod
    def finish(result, duration, chunks):
        duration = duration or result.get("duration")
        if duration:
            result["duration"] = round(float(duration), 3)
        result["timestamps"] = bool(result["segments"])
        if not result["segments"] and result["text"]:
            # A plain json answer: one segment for the whole span.
            result["segments"] = [
                {"start": 0.0, "end": round(float(duration or 0.0), 3), "text": result["text"]}
            ]
        result["chunks"] = chunks
        return result

    def request(self, path, offset=0.0, span=None):
        formats = ["json"] if self.verbose is False else ["verbose_json", "json"]
        for fmt in formats:
            response = self.post(path, fmt)
            if fmt == "verbose_json" and response.status_code in (400, 415, 422):
                self.verbose = False  # This server only answers json.
                continue
            if response.status_code in (401, 403):
                raise Transient(f"transcription endpoint refused the key (HTTP {response.status_code})")
            if response.is_error:
                raise Permanent(f"transcription endpoint refused the file (HTTP {response.status_code})")
            if fmt == "verbose_json":
                self.verbose = True
            try:
                body = response.json()
            except ValueError:
                body = {"text": response.text}
            result = parse(body, offset)
            if not result["segments"] and result["text"] and span:
                result["segments"] = [
                    {"start": round(offset, 3), "end": round(offset + span, 3), "text": result["text"]}
                ]
            return result
        raise Permanent("transcription endpoint refused every response format")

    def post(self, path, fmt):
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        data = {"model": self.model, "response_format": fmt}
        if self.language:
            data["language"] = self.language
        for attempt in range(self.retries + 1):
            self.requests += 1
            try:
                with path.open("rb") as handle:
                    response = self.http.post(
                        self.base_url + "/audio/transcriptions",
                        data=data,
                        files={"file": (path.name, handle, mime)},
                    )
            except httpx.TransportError as error:
                if attempt == self.retries:
                    raise Transient(f"transcription endpoint unreachable ({type(error).__name__})") from None
                self.sleep(min(30.0, 2.0**attempt))
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == self.retries:
                    raise Transient(f"transcription endpoint HTTP {response.status_code}")
                after = response.headers.get("retry-after", "").strip()
                self.sleep(min(60.0, float(after)) if after.isdigit() else min(30.0, 2.0**attempt))
                continue
            return response
        raise Transient("transcription endpoint retry limit reached")
