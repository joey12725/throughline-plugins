"""Local media helpers: ffmpeg/ffprobe detection, duration, tags, audio cuts.

ffmpeg is optional. Without it the plugin can still upload files the
endpoint accepts directly (wav, mp3, m4a, ...) when they fit one request;
longer or larger files and video containers need ffmpeg to be split or
converted, and are left for a later run (reported as partial coverage).
"""

import json
import shutil
import subprocess
import wave
from pathlib import Path


class Tools:
    """Located binaries. `ffmpeg` and `ffprobe` are paths or None."""

    def __init__(self, ffmpeg="auto"):
        ffmpeg = (ffmpeg or "auto").strip()
        if ffmpeg.lower() in ("off", "none", "0", "false"):
            self.ffmpeg = self.ffprobe = None
            return
        if ffmpeg.lower() == "auto":
            self.ffmpeg = shutil.which("ffmpeg")
            self.ffprobe = shutil.which("ffprobe")
        else:
            self.ffmpeg = ffmpeg if Path(ffmpeg).exists() else shutil.which(ffmpeg)
            sibling = Path(ffmpeg).with_name("ffprobe")
            self.ffprobe = str(sibling) if sibling.exists() else shutil.which("ffprobe")

    def probe(self, path):
        """(duration seconds or None, format tags dict)."""
        duration, tags = None, {}
        if Path(path).suffix.lower() == ".wav":
            try:
                with wave.open(str(path), "rb") as handle:
                    duration = handle.getnframes() / float(handle.getframerate())
            except (wave.Error, EOFError, OSError, ZeroDivisionError):
                duration = None
        if self.ffprobe:
            try:
                result = subprocess.run(
                    [
                        self.ffprobe,
                        "-v",
                        "error",
                        "-show_entries",
                        "format=duration:format_tags:stream=codec_type",
                        "-of",
                        "json",
                        str(path),
                    ],
                    capture_output=True,
                    timeout=120,
                )
                info = json.loads(result.stdout or b"{}") if result.returncode == 0 else {}
            except (OSError, ValueError, subprocess.TimeoutExpired):
                info = {}
            fmt = info.get("format") or {}
            tags = {str(k).lower(): str(v) for k, v in (fmt.get("tags") or {}).items()}
            if duration is None:
                try:
                    duration = float(fmt.get("duration"))
                except (TypeError, ValueError):
                    duration = None
        return duration, tags

    def extract(self, path, target, start=None, length=None):
        """Mono 16 kHz Opus speech audio of `path` (or a span of it)."""
        if not self.ffmpeg:
            raise RuntimeError("ffmpeg is not available")
        command = [self.ffmpeg, "-v", "error", "-nostdin", "-y"]
        if start is not None:
            command += ["-ss", f"{start:.3f}"]
        if length is not None:
            command += ["-t", f"{length:.3f}"]
        command += [
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "libopus",
            "-b:a",
            "32k",
            str(target),
        ]
        result = subprocess.run(command, capture_output=True, timeout=3600)
        if result.returncode != 0 or not Path(target).exists():
            raise ValueError("ffmpeg could not read an audio stream from the file")
        return Path(target)
