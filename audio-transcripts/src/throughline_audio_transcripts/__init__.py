"""Audio transcripts: watched folders of recordings, transcribed and searchable.

Point it at folders that something else keeps filling — phone voice memos
synced to a computer, a meeting recorder's export folder, a Plaud or
Limitless-style device's sync folder, OBS or Zoom local recordings — and every
new audio or video file is transcribed through an OpenAI-compatible
`/v1/audio/transcriptions` endpoint (OpenAI, or a self-hosted Whisper server).

Records follow the transcript shape of the Wave plugin: one recording record
per file and one record per transcript segment, with a `part_of` link from
each segment to its recording. Ids are content hashes, so renaming or moving
a file never duplicates it. Transcription is capped per run (files and audio
minutes) and every transcript is cached on disk, so re-running, resetting the
cursor or rebuilding the store never pays for the same audio twice.
"""

import hashlib
import json
import logging
import math
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from throughline_sdk import (
    Channel,
    Checkpoint,
    Declaration,
    Manifest,
    Plugin,
    Record,
    coverage,
    run,
)

from . import fake
from .media import Tools
from .timing import recording_start
from .transcribe import Endpoint, NeedsFFmpeg, Permanent, Transient

__all__ = ["AudioTranscripts", "main"]

RECORDINGS = "audio_transcripts.recordings"
SEGMENTS = "audio_transcripts.segments"
DEFAULT_EXTENSIONS = (
    "m4a,mp3,wav,ogg,oga,opus,flac,webm,mp4,mpeg,mpga,aac,amr,3gp,caf,wma,mov,mkv,m4v"
)
SKIP_SUFFIXES = (".part", ".partial", ".tmp", ".crdownload", ".download")
log = logging.getLogger("throughline_audio_transcripts")


def fields(**values):
    return [dict(name=k, type=v[0], description=v[1]) for k, v in values.items()]


SHARED = dict(
    folder=("text", "Label of the watched folder the file is in"),
    file_name=("text", "File name when transcribed"),
    content_hash=("text", "SHA-256 of the file's bytes (the record id is built from it)"),
    author_kind=(
        "text",
        "owner when the folder is listed in AUDIO_TRANSCRIPTS_OWNER_FOLDERS (your own "
        "voice memos), else other (speakers unknown)",
    ),
    time_source=(
        "text",
        "Where the recording time came from: quicktime_creationdate, file_name, "
        "creation_time, mtime_minus_duration or mtime",
    ),
    time_precision=("text", "exact, day or approximate"),
)

RECORDING_FIELDS = fields(
    **SHARED,
    relative_path=("text", "Path inside the watched folder"),
    size_bytes=("integer", "File size"),
    duration_s=("number", "Audio duration in seconds"),
    language=("text", "Language the endpoint detected"),
    model=("text", "Transcription model"),
    segment_count=("integer", "Transcript segments"),
    chunks=("integer", "Requests the file was split into"),
    timestamps=("boolean", "The endpoint returned timed segments (false: one untimed block)"),
)

SEGMENT_FIELDS = fields(
    **SHARED,
    segment_no=("integer", "Position in the transcript, from 0"),
    start_s=("number", "Seconds from the start of the recording"),
    end_s=("number", "Seconds from the start of the recording"),
    speaker_label=("text", "Speaker label when the endpoint diarizes (most do not)"),
    model=("text", "Transcription model"),
)


def setting(env, default=None, description="", required=False):
    return Declaration(env=env, default=default, required=required, description=description)


SETTINGS = {
    "dirs": setting(
        "AUDIO_TRANSCRIPTS_DIRS",
        required=True,
        description="Folders to watch, separated by the OS path separator (: or ;) or newlines; "
        "each `label=path` or a bare path (the label is the folder name)",
    ),
    "url": setting(
        "AUDIO_TRANSCRIPTS_URL",
        required=True,
        description="OpenAI-compatible API base ending in /v1, e.g. https://api.openai.com/v1",
    ),
    "model": setting("AUDIO_TRANSCRIPTS_MODEL", "whisper-1", "Model name sent to the endpoint"),
    "language": setting(
        "AUDIO_TRANSCRIPTS_LANGUAGE", None, "ISO-639-1 language hint (default: detect)"
    ),
    "max_files": setting(
        "AUDIO_TRANSCRIPTS_MAX_FILES", "20", "Files sent to the endpoint per run; 0 = no cap"
    ),
    "max_minutes": setting(
        "AUDIO_TRANSCRIPTS_MAX_MINUTES", "120", "Audio minutes sent to the endpoint per run; 0 = no cap"
    ),
    "min_age_s": setting(
        "AUDIO_TRANSCRIPTS_MIN_AGE_S",
        "300",
        "Leave files modified more recently than this for a later run (still syncing)",
    ),
    "extensions": setting(
        "AUDIO_TRANSCRIPTS_EXTENSIONS", DEFAULT_EXTENSIONS, "Comma-separated file extensions"
    ),
    "recursive": setting("AUDIO_TRANSCRIPTS_RECURSIVE", "true", "Also watch subfolders"),
    "owner_folders": setting(
        "AUDIO_TRANSCRIPTS_OWNER_FOLDERS",
        None,
        "Comma-separated folder labels whose recordings are you speaking (voice memos)",
    ),
    "owner_identifier": setting(
        "AUDIO_TRANSCRIPTS_OWNER_IDENTIFIER",
        "audio_transcripts:owner:owner",
        "Author identifier of your own recordings, system:kind:value",
    ),
    "owner_name": setting("AUDIO_TRANSCRIPTS_OWNER_NAME", None, "Display name on your recordings"),
    "timezone": setting(
        "AUDIO_TRANSCRIPTS_TIMEZONE",
        None,
        "IANA zone of times in file names (default: this machine's zone)",
    ),
    "cache_dir": setting(
        "AUDIO_TRANSCRIPTS_CACHE_DIR",
        None,
        "Transcript cache (default $XDG_CACHE_HOME/throughline/audio_transcripts)",
    ),
    "ffmpeg": setting(
        "AUDIO_TRANSCRIPTS_FFMPEG", "auto", "ffmpeg binary: auto (find on PATH), a path, or off"
    ),
    "chunk_seconds": setting(
        "AUDIO_TRANSCRIPTS_CHUNK_SECONDS", "1200", "Longest audio per request; longer files are split"
    ),
    "max_bytes": setting(
        "AUDIO_TRANSCRIPTS_MAX_BYTES", str(24 * 1024 * 1024), "Upload limit per request"
    ),
    "timeout": setting("AUDIO_TRANSCRIPTS_TIMEOUT", "600", "Seconds to wait for one request"),
    "retries": setting(
        "AUDIO_TRANSCRIPTS_RETRIES", "3", "Retries after HTTP 429, 5xx or connection errors"
    ),
    "fixture": setting(
        "AUDIO_TRANSCRIPTS_FIXTURE",
        None,
        "Tests only: JSON file of recorded endpoint answers instead of the network",
    ),
}


def truthy(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def integer(value, default):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def parse_dirs(value):
    entries = []
    for chunk in str(value or "").replace("\n", os.pathsep).split(os.pathsep):
        chunk = chunk.strip()
        if not chunk:
            continue
        label, _, path = chunk.partition("=") if "=" in chunk else ("", "", chunk)
        path = Path(path.strip()).expanduser()
        entries.append(((label.strip() or path.name or "audio"), path))
    if not entries:
        raise ValueError("AUDIO_TRANSCRIPTS_DIRS names no folder")
    return entries


def default_cache():
    base = os.getenv("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "throughline" / "audio_transcripts"


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def clock(seconds):
    seconds = int(round(seconds or 0))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


class AudioTranscripts(Plugin):
    manifest = Manifest(
        name="audio_transcripts",
        version="0.1.0",
        channels=[
            Channel(
                name=RECORDINGS,
                description=(
                    "One record per audio or video file found in the watched folders "
                    "(voice memos, meeting recorders, wearable recorders): when it was "
                    "recorded, how long, which folder, and the start of its transcript"
                ),
                fields=RECORDING_FIELDS,
            ),
            Channel(
                name=SEGMENTS,
                description=(
                    "Transcript of each recording, one record per timed segment, threaded "
                    "by recording and linked to it (part_of); machine transcription of "
                    "what was said, speakers unknown unless the folder is your own memos"
                ),
                fields=SEGMENT_FIELDS,
                conversation_whole_thread=True,
            ),
        ],
        settings=SETTINGS,
        secrets={
            "api_key": Declaration(
                env="AUDIO_TRANSCRIPTS_API_KEY",
                required=False,
                description="Bearer key for the endpoint (omit for a local server without auth)",
            )
        },
        run_mode="poll",
        default_interval_s=900,
        link_types=["part_of"],
    )

    def __init__(self, transport=None):
        super().__init__()
        s = self.settings
        self.folders = parse_dirs(s["dirs"])
        self.extensions = {
            "." + e.strip().lower().lstrip(".") for e in (s["extensions"] or "").split(",") if e.strip()
        }
        self.owner_folders = {
            f.strip() for f in (s["owner_folders"] or "").split(",") if f.strip()
        }
        identifier = s["owner_identifier"] or "audio_transcripts:owner:owner"
        parts = identifier.split(":", 2)
        if len(parts) != 3:
            raise ValueError("AUDIO_TRANSCRIPTS_OWNER_IDENTIFIER must be system:kind:value")
        self.owner = dict(zip(("system", "kind", "value"), parts), display_name=s["owner_name"])
        self.zone = ZoneInfo(s["timezone"]) if s["timezone"] else datetime.now().astimezone().tzinfo
        self.cache = Path(s["cache_dir"]).expanduser() if s["cache_dir"] else default_cache()
        self.tools = Tools(s["ffmpeg"])
        if transport is None and s["fixture"]:
            transport = fake.from_file(s["fixture"])
        self.transport = transport
        self.max_files = max(0, integer(s["max_files"], 20))
        self.max_seconds = max(0, integer(s["max_minutes"], 120)) * 60
        self.min_age = max(0, integer(s["min_age_s"], 300))
        self.recursive = truthy(s["recursive"])
        self.sleep = time.sleep
        self.now = None  # Test seam for the minimum age.
        self.stats = {}

    def execute(self, client, mode, dry_run=False):
        report = super().execute(client, mode, dry_run)
        report["audio_transcripts"] = self.stats
        return report

    # -- discovery --------------------------------------------------------------

    def scan(self):
        """[(label, root, path, stat)] of candidate files, oldest first; missing folders."""
        found, missing = [], []
        now = self.now or time.time()
        for label, root in self.folders:
            if not root.is_dir():
                missing.append(label)
                continue
            walker = root.rglob("*") if self.recursive else root.glob("*")
            for path in walker:
                name = path.name
                if name.startswith((".", "~$")) or name.lower().endswith(SKIP_SUFFIXES):
                    continue
                if path.suffix.lower() not in self.extensions:
                    continue
                if any(part.startswith(".") for part in path.relative_to(root).parts[:-1]):
                    continue
                try:
                    st = path.stat()
                except OSError:
                    continue
                if not path.is_file() or st.st_size == 0:
                    continue
                if now - st.st_mtime < self.min_age:
                    self.stats["too_new"] = self.stats.get("too_new", 0) + 1
                    continue
                found.append((label, root, path, st))
        found.sort(key=lambda item: (item[3].st_mtime, str(item[2])))
        return found, missing

    def identify(self, index, path, st):
        key = str(path.resolve())
        known = index.get(key)
        if known and known[0] == st.st_size and known[1] == st.st_mtime_ns:
            return known[2]
        digest = sha256(path)
        index[key] = [st.st_size, st.st_mtime_ns, digest]
        return digest

    # -- mapping ----------------------------------------------------------------

    def map(self, label, root, path, st, digest, tags, result):
        duration = result.get("duration")
        start, source, precision = recording_start(path.stem, tags, st.st_mtime, duration, self.zone)
        rec_id = "sha256-" + digest[:32]
        owner = label in self.owner_folders
        author = self.owner if owner else None
        shared = dict(
            folder=label,
            file_name=path.name,
            content_hash=digest,
            author_kind="owner" if owner else "other",
            time_source=source,
            time_precision=precision,
        )
        thread = {
            "external_id": rec_id,
            "title": f"{label} · {path.stem}"[:300],
            "metadata": {"kind": "audio_recording", "folder": label},
        }
        text = result.get("text") or ""
        segments = result.get("segments") or []
        summary = f"Recording {path.name} ({clock(duration)}, {label})"
        summary += ": " + (text[:500] + ("…" if len(text) > 500 else "")) if text else ": no speech detected"
        relative = str(path.relative_to(root))
        recording = dict(
            channel=RECORDINGS,
            external_id=rec_id,
            thread=thread,
            happened_at=start.isoformat(),
            content=summary,
            fields=dict(
                shared,
                relative_path=relative,
                size_bytes=st.st_size,
                duration_s=duration,
                language=result.get("language"),
                model=result.get("model"),
                segment_count=len(segments),
                chunks=result.get("chunks"),
                timestamps=result.get("timestamps"),
            ),
            payload={
                "file": {"name": path.name, "relative_path": relative, "folder": label, "size": st.st_size},
                "tags": {k: v for k, v in tags.items() if "date" in k or "time" in k},
                "transcript": result,
            },
        )
        if duration:
            recording["ended_at"] = (start + timedelta(seconds=duration)).isoformat()
        if precision != "exact":
            recording["subject"] = {"start": start.isoformat(), "start_precision": precision}
        if author:
            recording["author"] = author
        out = [recording]
        for number, segment in enumerate(segments):
            # Whole seconds on the timeline (start_s/end_s keep the exact
            # offsets): one thread then never mixes fractional and whole
            # timestamps, which text-sorting readers order wrongly.
            begin = start + timedelta(seconds=math.floor(segment["start"]))
            finish = start + timedelta(seconds=math.floor(max(segment["end"], segment["start"])))
            message = dict(
                channel=SEGMENTS,
                external_id=f"{rec_id}:{number:05d}",
                thread=thread,
                happened_at=begin.isoformat(),
                ended_at=finish.isoformat(),
                content=segment["text"],
                fields=dict(
                    shared,
                    segment_no=number,
                    start_s=segment["start"],
                    end_s=segment["end"],
                    speaker_label=segment.get("speaker"),
                    model=result.get("model"),
                ),
                payload={"recording": rec_id, "file_name": path.name, "segment": segment},
            )
            if author:
                message["author"] = author
            out.append(message)
        return out

    # -- run --------------------------------------------------------------------

    def records(self, cursor, mode):
        self.stats = {"transcribed": 0, "cached": 0, "seconds_sent": 0.0}
        done = set(cursor.get("done") or [])
        files, missing = self.scan()
        if missing and len(missing) == len(self.folders):
            raise RuntimeError("no watched folder exists: " + ", ".join(sorted(missing)))
        index_path = self.cache / "index.json"
        index = read_json(index_path, {}) or {}
        endpoint = Endpoint(
            self.settings["url"],
            self.secrets.get("api_key"),
            self.settings["model"],
            language=self.settings["language"],
            transport=self.transport,
            timeout=integer(self.settings["timeout"], 600),
            retries=integer(self.settings["retries"], 3),
            max_bytes=integer(self.settings["max_bytes"], 24 * 1024 * 1024),
            chunk_seconds=integer(self.settings["chunk_seconds"], 1200),
            tools=self.tools,
            sleep=self.sleep,
        )
        seen = set()
        problems = {"source_error": [], "needs_ffmpeg": [], "run_cap": []}
        transient = None
        try:
            for label, root, path, st in files:
                try:
                    digest = self.identify(index, path, st)
                except OSError:
                    continue
                if digest in done or digest in seen:
                    seen.add(digest)
                    continue
                seen.add(digest)
                cached_path = self.cache / "transcripts" / (digest + ".json")
                cached = read_json(cached_path)
                duration, tags = self.tools.probe(path)
                if cached and cached.get("error"):
                    self.stats["failed_before"] = self.stats.get("failed_before", 0) + 1
                    continue
                if cached is None:
                    if transient:
                        problems["source_error"].append(path.name)
                        continue
                    over_files = self.max_files and self.stats["transcribed"] >= self.max_files
                    over_time = self.max_seconds and (
                        self.stats["seconds_sent"] + (duration or 0) > self.max_seconds
                    )
                    if over_files or over_time:
                        problems["run_cap"].append(path.name)
                        continue
                    try:
                        result = endpoint.transcript(path, duration)
                    except NeedsFFmpeg as error:
                        problems["needs_ffmpeg"].append(f"{path.name}: {error}")
                        continue
                    except (Permanent, ValueError) as error:
                        write_json(cached_path, {"error": str(error), "model": endpoint.model})
                        problems["source_error"].append(f"{path.name}: {error}")
                        continue
                    except Transient as error:
                        transient = str(error)
                        problems["source_error"].append(path.name)
                        continue
                    result["model"] = endpoint.model
                    write_json(cached_path, result)
                    self.stats["transcribed"] += 1
                    self.stats["seconds_sent"] += result.get("duration") or 0
                    cached = result
                else:
                    self.stats["cached"] += 1
                if duration is None:
                    duration = cached.get("duration")
                result = dict(cached, duration=duration if duration is not None else cached.get("duration"))
                messages = self.map(label, root, path, st, digest, tags, result)
                for message in messages:
                    yield message
                recording = messages[0]["external_id"]
                for message in messages[1:]:
                    yield Record(
                        kind="link",
                        data={
                            "type": "part_of",
                            "from": {"channel": SEGMENTS, "external_id": message["external_id"]},
                            "to": {"channel": RECORDINGS, "external_id": recording},
                            "method": "source",
                        },
                    )
                done.add(digest)
                yield Checkpoint(cursor={"done": sorted(done)})
        finally:
            endpoint.close()
            try:
                write_json(index_path, index)
            except OSError:
                log.warning("could not write the file index cache")
        self.stats["requests"] = endpoint.requests
        if transient and not self.stats["transcribed"]:
            raise Transient(transient)
        for reason, names in problems.items():
            if names:
                for channel in (RECORDINGS, SEGMENTS):
                    yield coverage(
                        channel=channel,
                        status="partial",
                        reasons=[reason],
                        detail={"files": len(names), "examples": names[:5]},
                    )
        if missing:
            for channel in (RECORDINGS, SEGMENTS):
                yield coverage(
                    channel=channel,
                    status="partial",
                    reasons=["source_error"],
                    detail={"missing_folders": sorted(missing)},
                )
        yield Checkpoint(cursor={"done": sorted(done)})


def main(argv=None):
    run(AudioTranscripts, argv)
