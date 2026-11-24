import json
import math
import os
import shutil
import struct
import wave
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from throughline_sdk import Checkpoint, Record
from throughline_sdk.contract import check_plugin

from throughline_audio_transcripts import AudioTranscripts
from throughline_audio_transcripts.fake import endpoint
from throughline_audio_transcripts.timing import from_name, parse_tag, recording_start
from throughline_audio_transcripts.transcribe import parse

RECORDED = Path(__file__).parent / "recorded" / "endpoint.json"
KEY = "invented-transcribe-key-canary"
NY = ZoneInfo("America/New_York")
OLD = datetime(2025, 3, 8, 12, 0, tzinfo=timezone.utc).timestamp()
FILES = {
    "2025-03-04 10-15-00 Invented memo.wav": (2.0, 440),
    "REC_20250305_093000.wav": (3.0, 550),
    "Invented meeting 2025-03-06.wav": (1.5, 660),
    "untitled.wav": (1.0, 770),
    "Invented lecture 20250307T1400.wav": (2.0, 880),
}


def tone(path, seconds, freq, rate=8000):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        frames = b"".join(
            struct.pack("<h", int(8000 * math.sin(2 * math.pi * freq * i / rate)))
            for i in range(int(seconds * rate))
        )
        out.writeframes(frames)
    os.utime(path, (OLD, OLD))
    return path


def library(root):
    memos, meetings = root / "memos", root / "meetings"
    tone(memos / "2025-03-04 10-15-00 Invented memo.wav", *FILES["2025-03-04 10-15-00 Invented memo.wav"])
    tone(memos / "untitled.wav", *FILES["untitled.wav"])
    for name in ("REC_20250305_093000.wav", "Invented meeting 2025-03-06.wav"):
        tone(meetings / name, *FILES[name])
    tone(meetings / "sub" / "Invented lecture 20250307T1400.wav", *FILES["Invented lecture 20250307T1400.wav"])
    (meetings / "notes.txt").write_text("not audio")
    (meetings / ".hidden.wav").write_bytes(b"RIFF")
    return memos, meetings


@pytest.fixture
def env(monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.startswith("AUDIO_TRANSCRIPTS_"):
            monkeypatch.delenv(key)
    memos, meetings = library(tmp_path / "audio")
    monkeypatch.setenv("AUDIO_TRANSCRIPTS_DIRS", f"memos={memos}{os.pathsep}{meetings}")
    monkeypatch.setenv("AUDIO_TRANSCRIPTS_URL", "https://stt.example.com/v1")
    monkeypatch.setenv("AUDIO_TRANSCRIPTS_API_KEY", KEY)
    monkeypatch.setenv("AUDIO_TRANSCRIPTS_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("AUDIO_TRANSCRIPTS_TIMEZONE", "America/New_York")
    monkeypatch.setenv("AUDIO_TRANSCRIPTS_MIN_AGE_S", "0")
    monkeypatch.setenv("AUDIO_TRANSCRIPTS_OWNER_FOLDERS", "memos")
    monkeypatch.setenv("AUDIO_TRANSCRIPTS_OWNER_NAME", "Invented Owner")
    monkeypatch.setenv("AUDIO_TRANSCRIPTS_FFMPEG", "off")
    return monkeypatch


def plugin(calls=None, transport=None):
    data = json.loads(RECORDED.read_text())
    p = AudioTranscripts(transport=transport or endpoint(data, calls))
    p.sleep = lambda s: None
    return p


def messages(items):
    return [i for i in items if isinstance(i, dict)]


def test_time_evidence_order():
    assert from_name("REC_20250305_093000", NY)[0] == datetime(2025, 3, 5, 9, 30, tzinfo=NY)
    assert from_name("2025-03-04 10-15-00 memo", NY)[1] == "exact"
    assert from_name("meeting 2025-03-06", NY) == (datetime(2025, 3, 6, tzinfo=NY), "day")
    assert from_name("track 12", NY) == (None, None)
    assert parse_tag("2025-03-04T10:15:00-0800").utcoffset().total_seconds() == -8 * 3600
    assert parse_tag("1904-01-01T00:00:00Z") is None
    tags = {"com.apple.quicktime.creationdate": "2025-01-02T03:04:05+0100", "creation_time": "2025-05-05T00:00:00Z"}
    assert recording_start("REC_20250305_093000", tags, OLD, 2, NY)[1] == "quicktime_creationdate"
    tags.pop("com.apple.quicktime.creationdate")
    assert recording_start("REC_20250305_093000", tags, OLD, 2, NY)[1] == "file_name"
    assert recording_start("memo", tags, OLD, 2, NY)[1] == "creation_time"
    when, source, precision = recording_start("memo", {}, OLD, 60, NY)
    assert (source, precision) == ("mtime_minus_duration", "approximate")
    assert when == datetime(2025, 3, 8, 11, 59, tzinfo=timezone.utc)


def test_parse_verbose_and_plain_json():
    out = parse({"text": "x", "language": "en", "duration": 4, "segments": [
        {"start": 1, "end": 2, "text": " hi ", "speaker": "A"}, {"start": 2, "end": 3, "text": " "}]}, offset=10)
    assert out == {"text": "x", "language": "en", "duration": 4.0,
                   "segments": [{"start": 11.0, "end": 12.0, "text": "hi", "speaker": "A"}]}
    assert parse("plain")["text"] == "plain"


def test_records_segments_links_and_owner(env):
    calls = []
    items = list(plugin(calls).records({}, "backfill"))
    out = messages(items)
    assert sorted(calls) == sorted(FILES)
    recordings = {m["fields"]["file_name"]: m for m in out if m["channel"] == "audio_transcripts.recordings"}
    assert set(recordings) == set(FILES)
    memo = recordings["2025-03-04 10-15-00 Invented memo.wav"]
    assert memo["happened_at"] == "2025-03-04T10:15:00-05:00"
    assert memo["ended_at"] == "2025-03-04T10:15:02-05:00"
    assert memo["fields"]["author_kind"] == "owner"
    assert memo["author"]["display_name"] == "Invented Owner"
    assert memo["fields"]["duration_s"] == 2.0 and memo["fields"]["segment_count"] == 2
    assert memo["external_id"].startswith("sha256-") and len(memo["external_id"]) == 39
    segs = [m for m in out if m["channel"] == "audio_transcripts.segments" and m["thread"]["external_id"] == memo["external_id"]]
    assert [s["content"] for s in segs] == ["Remember to water the invented ferns.", "Then call the pretend plumber."]
    assert segs[1]["happened_at"] == "2025-03-04T10:15:01-05:00"
    assert segs[1]["fields"]["start_s"] == 1.1
    assert segs[1]["external_id"] == memo["external_id"] + ":00001"
    standup = recordings["REC_20250305_093000.wav"]
    assert standup["fields"]["author_kind"] == "other" and "author" not in standup
    assert standup["fields"]["folder"] == "meetings"
    day = recordings["Invented meeting 2025-03-06.wav"]
    assert day["subject"] == {"start": "2025-03-06T00:00:00-05:00", "start_precision": "day"}
    silent = recordings["untitled.wav"]
    assert silent["content"].endswith("no speech detected")
    assert silent["fields"]["time_source"] == "mtime_minus_duration"
    lecture = recordings["Invented lecture 20250307T1400.wav"]
    assert lecture["fields"]["relative_path"] == "sub/Invented lecture 20250307T1400.wav"
    links = [i.data for i in items if isinstance(i, Record) and i.kind == "link"]
    assert len(links) == 8
    assert {
        "type": "part_of",
        "from": {"channel": "audio_transcripts.segments", "external_id": segs[0]["external_id"]},
        "to": {"channel": "audio_transcripts.recordings", "external_id": memo["external_id"]},
        "method": "source",
    } in links
    assert len([i for i in items if isinstance(i, Checkpoint)]) == 6
    assert KEY not in json.dumps(out)


def test_cache_and_cursor_prevent_paying_twice(env, tmp_path):
    calls = []
    first = list(plugin(calls).records({}, "backfill"))
    assert len(calls) == 5
    cursor = [i for i in first if isinstance(i, Checkpoint)][-1].cursor
    assert messages(plugin(calls).records(cursor, "sync")) == []
    # A reset cursor (or a rebuilt store) reads every transcript from the cache.
    again = messages(plugin(calls).records({}, "backfill"))
    assert len(calls) == 5
    assert again == messages(first)
    # A renamed copy of a known file is the same recording.
    memos = Path(os.environ["AUDIO_TRANSCRIPTS_DIRS"].split(os.pathsep)[0].split("=", 1)[1])
    shutil.copy(memos / "untitled.wav", memos / "copy of untitled.wav")
    assert messages(plugin(calls).records(cursor, "sync")) == []


def test_per_run_caps_leave_the_rest_for_later(env):
    env.setenv("AUDIO_TRANSCRIPTS_MAX_FILES", "2")
    calls = []
    items = list(plugin(calls).records({}, "backfill"))
    assert len(calls) == 2
    reports = [i.data for i in items if isinstance(i, Record) and i.kind == "coverage"]
    assert reports and all(r["reasons"] == ["run_cap"] for r in reports)
    assert reports[0]["detail"]["files"] == 3
    cursor = [i for i in items if isinstance(i, Checkpoint)][-1].cursor
    later = messages(plugin(calls).records(cursor, "sync"))
    assert len(calls) == 4 and len({m["thread"]["external_id"] for m in later}) == 2


def test_minute_cap(env):
    env.setenv("AUDIO_TRANSCRIPTS_MAX_MINUTES", "0")
    env.setenv("AUDIO_TRANSCRIPTS_MAX_FILES", "0")
    calls = []
    list(plugin(calls).records({}, "backfill"))
    assert len(calls) == 5  # 0 means no cap.


def test_too_new_files_wait(env):
    env.setenv("AUDIO_TRANSCRIPTS_MIN_AGE_S", "600")
    p = plugin([])
    p.now = OLD + 60
    assert messages(p.records({}, "backfill")) == []
    assert p.stats["too_new"] == 5


def test_endpoint_down_raises_and_refusal_is_remembered(env):
    def down(request):
        raise httpx.ConnectError("down")

    with pytest.raises(RuntimeError, match="unreachable"):
        list(plugin(transport=httpx.MockTransport(down)).records({}, "backfill"))
    refused = httpx.MockTransport(lambda r: httpx.Response(413, json={"error": "too big"}))
    items = list(plugin(transport=refused).records({}, "backfill"))
    assert messages(items) == []
    reports = [i.data for i in items if isinstance(i, Record) and i.kind == "coverage"]
    assert reports[0]["reasons"] == ["source_error"]
    calls = []
    assert messages(plugin(calls).records({}, "backfill")) == []
    assert calls == []  # Refusals are cached; delete the cache entry to retry.


def test_json_fallback_when_verbose_is_refused(env):
    seen = []

    def handler(request):
        body = request.read()
        seen.append(b'name="response_format"\r\n\r\nverbose_json' in body)
        if seen[-1]:
            return httpx.Response(400, json={"error": "unsupported"})
        return httpx.Response(200, json={"text": "Untimed invented words."})

    env.setenv("AUDIO_TRANSCRIPTS_MAX_FILES", "1")
    out = messages(plugin(transport=httpx.MockTransport(handler)).records({}, "backfill"))
    assert seen == [True, False]
    recording, segment = out
    assert recording["fields"]["timestamps"] is False
    assert segment["fields"]["start_s"] == 0.0 and segment["fields"]["end_s"] == 1.5


def test_long_files_need_ffmpeg(env):
    env.setenv("AUDIO_TRANSCRIPTS_CHUNK_SECONDS", "1")
    items = list(plugin([]).records({}, "backfill"))
    reports = [i.data for i in items if isinstance(i, Record) and i.kind == "coverage"]
    assert reports[0]["reasons"] == ["needs_ffmpeg"]
    assert [m["fields"]["file_name"] for m in messages(items)] == ["untitled.wav"]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_long_files_are_split_with_ffmpeg(env):
    env.setenv("AUDIO_TRANSCRIPTS_FFMPEG", "auto")
    env.setenv("AUDIO_TRANSCRIPTS_CHUNK_SECONDS", "1")
    env.setenv("AUDIO_TRANSCRIPTS_MAX_FILES", "1")
    env.setenv("AUDIO_TRANSCRIPTS_OWNER_FOLDERS", "")
    names = []

    def handler(request):
        body = request.read()
        import re

        names.append(re.search(rb'filename="([^"]*)"', body).group(1).decode())
        return httpx.Response(200, json={"text": "chunk", "segments": [{"start": 0.1, "end": 0.9, "text": "chunk words"}]})

    out = messages(plugin(transport=httpx.MockTransport(handler)).records({}, "backfill"))
    assert names == ["chunk0000.ogg", "chunk0001.ogg"]
    segments = [m for m in out if m["channel"] == "audio_transcripts.segments"]
    assert [s["fields"]["start_s"] for s in segments] == [0.1, 1.1]
    assert out[0]["fields"]["chunks"] == 2


def test_contract_kit(tmp_path):
    memos, meetings = library(tmp_path / "audio")
    result = check_plugin(
        "throughline_audio_transcripts:AudioTranscripts",
        {
            "AUDIO_TRANSCRIPTS_DIRS": f"memos={memos}{os.pathsep}{meetings}",
            "AUDIO_TRANSCRIPTS_URL": "https://stt.example.com/v1",
            "AUDIO_TRANSCRIPTS_API_KEY": KEY,
            "AUDIO_TRANSCRIPTS_FIXTURE": str(RECORDED),
            "AUDIO_TRANSCRIPTS_CACHE_DIR": str(tmp_path / "cache"),
            "AUDIO_TRANSCRIPTS_TIMEZONE": "America/New_York",
            "AUDIO_TRANSCRIPTS_MIN_AGE_S": "0",
            "AUDIO_TRANSCRIPTS_OWNER_FOLDERS": "memos",
            "AUDIO_TRANSCRIPTS_FFMPEG": "off",
        },
        secrets=[KEY],
    )
    assert result["messages"] == 13
