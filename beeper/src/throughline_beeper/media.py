"""Instagram media download (yt-dlp), frames and audio (ffmpeg), transcription.

Every external tool sits behind a small interface so tests use fakes:
``InstagramFetcher.fetch`` and ``Transcriber.transcribe``. Transcription,
ffmpeg, the yt-dlp runner and its error classes live in the shared
``throughline_media`` package; this module keeps Instagram specifics and the
Beeper setting names.
"""

import io
import json
import logging
import subprocess  # noqa: F401  (tests patch media.subprocess.run)
from pathlib import Path

import httpx
from throughline_media import ffmpeg as av
from throughline_media import transcribe as stt
from throughline_media import ytdlp
from throughline_media.errors import MediaTransient, MediaUnavailable
from throughline_media.transcribe import (
    FasterWhisper,
    NullTranscriber,
    OpenAICompatible,
    Transcriber,
)
from throughline_media.ytdlp import classify

FRAME_EVERY_S = 3

__all__ = [
    "FRAME_EVERY_S",
    "FasterWhisper",
    "InstagramFetcher",
    "MediaTransient",
    "MediaUnavailable",
    "NullTranscriber",
    "OpenAICompatible",
    "Transcriber",
    "YtDlp",
    "audio",
    "classify",
    "ffmpeg",
    "frames",
    "transcriber_from",
]


def transcriber_from(settings, secrets):
    """BEEPER_TRANSCRIBER choice; the endpoint uses THROUGHLINE_TRANSCRIBE_*."""
    choice = (settings.get("transcriber") or "auto").lower()
    url = settings.get("transcribe_url")
    if choice in ("none", "null", "off"):
        return NullTranscriber()
    whisper = FasterWhisper.available()
    if choice == "faster-whisper" or (choice == "auto" and whisper):
        if not whisper:
            raise ValueError(
                "BEEPER_TRANSCRIBER=faster-whisper needs the 'whisper' extra"
            )
        return FasterWhisper(settings.get("whisper_model"))
    if choice in ("auto", "openai", "openai-compatible"):
        if url:
            return stt.endpoint_from(settings, secrets)
        if choice != "auto":
            raise ValueError(
                "BEEPER_TRANSCRIBER=openai needs THROUGHLINE_TRANSCRIBE_URL"
                " (or its alias BEEPER_TRANSCRIBE_URL)"
            )
        return NullTranscriber()
    raise ValueError("unknown BEEPER_TRANSCRIBER")


# ffmpeg ------------------------------------------------------------------------

ffmpeg = av.ffmpeg
audio = av.audio


def frames(video, outdir, every=FRAME_EVERY_S, limit=200):
    """[(at_seconds, path)]: one JPEG every `every` seconds from t=0."""
    return av.frames(video, outdir, every, limit)


# Instagram ---------------------------------------------------------------------


class InstagramFetcher:
    def fetch(self, url, workdir):
        """[{type: video, path, duration?} | {type: image, path}] in post order.

        Raises MediaUnavailable for private/removed posts and MediaTransient
        for anything a later run might get past.
        """
        raise NotImplementedError


def has_video(entry):
    formats = entry.get("formats") or []
    return (
        bool(entry.get("duration"))
        or any((f.get("vcodec") or "none") != "none" for f in formats)
        or (entry.get("ext") in ("mp4", "webm", "mov") and entry.get("url"))
    )


def image_url(entry):
    if entry.get("url") and entry.get("ext") in ("jpg", "jpeg", "png", "webp", "heic"):
        return entry["url"]
    thumbnails = [t for t in entry.get("thumbnails") or [] if t.get("url")]
    if not thumbnails:
        return entry.get("thumbnail")
    return max(
        enumerate(thumbnails),
        key=lambda pair: (
            (pair[1].get("width") or 0) * (pair[1].get("height") or 0),
            pair[0],
        ),
    )[1]["url"]


IMAGE_FORMATS = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "GIF": "gif"}


def image_extension(blob):
    """File extension of a real raster image, or None for anything else."""
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(blob)) as image:
            image.verify()
            return IMAGE_FORMATS.get(image.format or "")
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        return None
    except Image.DecompressionBombError:
        return None


class YtDlp(ytdlp.YtDlp, InstagramFetcher):
    def __init__(
        self,
        cookies=None,
        cookies_from_browser=None,
        max_bytes=200 * 1024 * 1024,
        max_seconds=1800,
        binary=None,
        transport=None,
    ):
        super().__init__(
            cookies, cookies_from_browser, binary=binary, classify=classify
        )
        self.max_bytes = max_bytes
        self.max_seconds = max_seconds
        self.transport = transport

    def download_image(self, source):
        """Image bytes, streamed under the size cap; None when too large."""
        try:
            with httpx.Client(
                timeout=60, follow_redirects=True, transport=self.transport
            ) as http:
                with http.stream("GET", source) as response:
                    if response.status_code in (403, 404, 410):
                        raise MediaUnavailable("dead", "image no longer available")
                    if response.is_error:
                        raise MediaTransient(f"image HTTP {response.status_code}")
                    chunks, total = [], 0
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > self.max_bytes:
                            return None
                        chunks.append(chunk)
                    return b"".join(chunks)
        except httpx.HTTPError:
            raise MediaTransient("image download failed") from None

    def fetch(self, url, workdir):
        workdir = Path(workdir)
        try:
            info = json.loads(
                self.run(["--dump-single-json", "--ignore-no-formats-error", url], 180)
            )
        except ValueError:
            raise MediaTransient("yt-dlp returned no metadata") from None
        playlist = isinstance(info.get("entries"), list)
        entries = info["entries"] if playlist else [info]
        items = []
        for index, entry in enumerate(entries, 1):
            if not isinstance(entry, dict):
                continue
            if has_video(entry):
                duration = entry.get("duration")
                if duration and duration > self.max_seconds:
                    items.append(
                        {"type": "video", "skipped": "too_long", "duration": duration}
                    )
                    continue
                args = [
                    "-f",
                    "b[height<=720]/bv*[height<=720]+ba/b",
                    "--merge-output-format",
                    "mp4",
                    "--max-filesize",
                    str(self.max_bytes),
                    "-o",
                    str(workdir / f"item{index}.%(ext)s"),
                ]
                if playlist:
                    args += ["--playlist-items", str(index)]
                self.run(args + [url], 900)
                found = [
                    p
                    for p in workdir.glob(f"item{index}.*")
                    if p.suffix not in (".part", ".ytdl", ".json")
                ]
                if not found:
                    items.append({"type": "video", "skipped": "too_large"})
                    continue
                items.append({"type": "video", "path": found[0], "duration": duration})
            else:
                source = image_url(entry)
                if not source:
                    continue
                blob = self.download_image(source)
                if blob is None:
                    items.append({"type": "image", "skipped": "too_large"})
                    continue
                ext = image_extension(blob)
                if ext is None:
                    # A login wall or error page instead of the picture.
                    raise MediaTransient("image URL did not return an image")
                path = workdir / f"item{index}.{ext}"
                path.write_bytes(blob)
                items.append({"type": "image", "path": path})
        if not items:
            raise MediaUnavailable("dead", "no media in post")
        logging.debug("Instagram fetched %d items", len(items))
        return items
