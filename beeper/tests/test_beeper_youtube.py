"""YouTube links in Beeper messages: transcript plus one frame per minute.

yt-dlp is replaced by an in-process fake that makes synthetic test-pattern
video and tone audio with ffmpeg; no real YouTube content is fetched.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from throughline_media.errors import MediaTransient, MediaUnavailable

from throughline_beeper import Beeper, enrichment_flags
from throughline_beeper import media
from throughline_beeper.mapping import CONTENT_LIMIT

RECORDINGS = Path(__file__).parent / "recorded"
needs_ffmpeg = pytest.mark.skipif(
    not shutil.which("ffmpeg"), reason="ffmpeg not on PATH"
)


def synthetic(path, seconds, video):
    source = (
        f"testsrc=size=320x240:rate=2:duration={seconds}"
        if video
        else f"sine=frequency=440:duration={seconds}"
    )
    codec = ["-pix_fmt", "yuv420p", "-c:v", "mpeg4"] if video else ["-c:a", "libopus"]
    subprocess.run(
        [shutil.which("ffmpeg"), "-v", "error", "-y", "-f", "lavfi", "-i", source]
        + codec
        + [str(path)],
        check=True,
        capture_output=True,
    )
    return path


def vtt(cues):
    def stamp(s):
        return f"{s // 3600:02}:{s % 3600 // 60:02}:{s % 60:02}.000"

    blocks = ["WEBVTT"]
    for start, end, text in cues:
        blocks.append(f"{stamp(start)} --> {stamp(end)}\n{text}")
    return "\n\n".join(blocks) + "\n"


class FakeYouTube:
    """Stands in for the shared YouTubeFetcher."""

    authenticated = False

    def __init__(self, videos):
        self.videos, self.calls = videos, []

    def outcome(self, video):
        value = self.videos[video]
        if isinstance(value, Exception):
            raise value
        return value

    def info(self, video):
        self.calls.append(("info", video))
        return dict(self.outcome(video)["meta"])

    def captions(self, video, kind, lang, workdir):
        self.calls.append(("captions", video))
        return self.outcome(video)["vtt"]

    def audio(self, video, workdir, max_bytes):
        self.calls.append(("audio", video))
        (Path(workdir) / "dl").mkdir()
        seconds = self.outcome(video)["meta"]["duration"]
        return synthetic(Path(workdir) / "dl" / "audio.ogg", seconds, False)

    def video(self, video, workdir, max_bytes):
        self.calls.append(("video", video))
        (Path(workdir) / "dl").mkdir()
        seconds = self.outcome(video)["meta"]["duration"]
        return synthetic(Path(workdir) / "dl" / "video.mp4", seconds, True)


def meta(video, duration=130, **extra):
    return dict(
        id=video,
        title="Invented clip " + video,
        duration=duration,
        subtitles=["en"],
        timestamp=1577880000,
        **extra,
    )


def fixture(tmp_path, bodies):
    data = json.loads((RECORDINGS / "archive.json").read_text())
    base = data["messages"][1]
    data["messages"] = [
        dict(
            base,
            id=f"discord:fake-yt-{i}",
            body=body,
            sent_at=f"2020-01-0{i + 1}T00:00:00Z",
            reply_to="",
            attachments_json="[]",
        )
        for i, body in enumerate(bodies)
    ]
    path = tmp_path / "youtube.json"
    path.write_text(json.dumps(data))
    return path


def plugin_for(monkeypatch, recording, options=None, **env):
    monkeypatch.setenv("BEEPER_SOURCE", "archive")
    monkeypatch.setenv("BEEPER_RECORDING", str(recording))
    for key in ("BEEPER_YOUTUBE_LINKS", "BEEPER_YOUTUBE_LIMIT"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    plugin = Beeper()
    plugin.enrich_options = dict(options or {})
    return plugin


def messages(plugin):
    return {
        r["external_id"]: r
        for r in plugin.records({}, "backfill")
        if isinstance(r, dict)
    }


CAPTIONS = vtt([(1, 4, "invented caption"), (65, 67, "later words")])


@needs_ffmpeg
def test_youtube_links_transcript_frames_unavailable_and_retries(monkeypatch, tmp_path):
    fetcher = FakeYouTube(
        {
            "FAKEVIDEO01": {"meta": meta("FAKEVIDEO01"), "vtt": CAPTIONS},
            "FAKEPRIVAT1": MediaUnavailable("private", "private video"),
            "FAKEFLAKY01": MediaTransient("bot check"),
        }
    )
    recording = fixture(
        tmp_path,
        [
            "watch https://youtu.be/FAKEVIDEO01 now",
            "short youtube.com/shorts/FAKEPRIVAT1",
            "https://www.youtube.com/watch?v=FAKEFLAKY01",
        ],
    )

    def run(**env):
        plugin = plugin_for(monkeypatch, recording, {"youtube_fetcher": fetcher}, **env)
        return messages(plugin)

    off = run()
    assert not fetcher.calls
    assert all("youtube_links" not in m["fields"] for m in off.values())

    first = run(BEEPER_YOUTUBE_LINKS="1")
    content = first["discord:fake-yt-0"]["content"]
    field = first["discord:fake-yt-0"]["fields"]["youtube_links"][0]
    assert (
        "youtube FAKEVIDEO01: transcript\n[00:00] invented caption later words"
        in content
    )
    assert [f["at_s"] for f in field["frames"]] == [0, 60, 120]
    assert f"youtube FAKEVIDEO01 frame 01:00: {field['frames'][1]['link']}" in content
    assert field["frames"][0]["link"].startswith("throughline-asset://beeper/")
    assert field["transcript"] == "[00:00] invented caption later words"
    assert field["transcript_source"] == "captions"
    assert field["caption_kind"] == "manual" and field["kind"] == "watch"
    assert field["url"] == "https://www.youtube.com/watch?v=FAKEVIDEO01"
    private = first["discord:fake-yt-1"]
    assert private["content"].endswith("youtube FAKEPRIVAT1: unavailable (private)")
    assert private["fields"]["youtube_links"][0]["kind"] == "short"
    # Retryable: not shown, so retries add no versions.
    assert first["discord:fake-yt-2"]["content"] == (
        "https://www.youtube.com/watch?v=FAKEFLAKY01"
    )
    assert "youtube_links" not in first["discord:fake-yt-2"]["fields"]

    fetcher.calls.clear()
    again = run(BEEPER_YOUTUBE_LINKS="1")
    assert fetcher.calls == [("info", "FAKEFLAKY01")]
    assert again["discord:fake-yt-0"] == first["discord:fake-yt-0"]
    final = run(BEEPER_YOUTUBE_LINKS="1")
    assert final["discord:fake-yt-2"]["content"].endswith(
        "youtube FAKEFLAKY01: unavailable (failed repeatedly)"
    )
    # Switched off: the cache still renders, so nothing reverts.
    fetcher.calls.clear()
    assert run() == final and not fetcher.calls


@needs_ffmpeg
def test_long_transcript_stays_in_budget_with_full_text_in_field(monkeypatch, tmp_path):
    words = "invented syllable " * 40
    cues = [(s, s + 1, f"{words}{s}") for s in range(0, 7000, 20)]
    fetcher = FakeYouTube(
        {"FAKELONGTX1": {"meta": meta("FAKELONGTX1", 130), "vtt": vtt(cues)}}
    )
    recording = fixture(tmp_path, ["https://www.youtube.com/watch?v=FAKELONGTX1"])
    plugin = plugin_for(
        monkeypatch,
        recording,
        {"youtube_fetcher": fetcher},
        BEEPER_YOUTUBE_LINKS="1",
    )
    message = messages(plugin)["discord:fake-yt-0"]
    field = message["fields"]["youtube_links"][0]
    assert len(message["content"].encode()) <= CONTENT_LIMIT
    assert "[truncated:" in message["content"]
    assert "full text in field youtube_links" in message["content"]
    assert "youtube FAKELONGTX1 frame 02:00: " in message["content"]
    assert field["truncated"] and field["transcript"].endswith(f"{words}6980")
    assert "[115:00] " in field["transcript"]  # Five-minute markers.


@needs_ffmpeg
def test_configured_transcriber_is_used_for_youtube(monkeypatch, tmp_path):
    class Fake(media.Transcriber):
        name = "fake-endpoint"

        def __init__(self):
            self.calls = 0

        def transcript(self, path):
            self.calls += 1
            return {
                "text": "spoken",
                "segments": [{"start": 301.0, "end": 303.0, "text": "spoken words"}],
                "timestamps": True,
            }

    fetcher = FakeYouTube({"FAKEVIDEO02": {"meta": meta("FAKEVIDEO02", 400)}})
    transcriber = Fake()
    recording = fixture(tmp_path, ["https://www.youtube.com/live/FAKEVIDEO02"])
    plugin = plugin_for(
        monkeypatch,
        recording,
        {"youtube_fetcher": fetcher, "transcriber": transcriber},
        BEEPER_YOUTUBE_LINKS="1",
    )
    message = messages(plugin)["discord:fake-yt-0"]
    field = message["fields"]["youtube_links"][0]
    assert "youtube FAKEVIDEO02: transcript\n[05:00] spoken words" in message["content"]
    assert field["transcript_source"] == "endpoint" and field["kind"] == "live"
    assert transcriber.calls == 1
    assert ("captions", "FAKEVIDEO02") not in fetcher.calls


def test_transcription_settings_and_beeper_aliases(monkeypatch):
    monkeypatch.setenv("BEEPER_SOURCE", "archive")
    monkeypatch.setenv("BEEPER_RECORDING", str(RECORDINGS / "archive.json"))
    monkeypatch.setenv("BEEPER_TRANSCRIBER", "openai")
    monkeypatch.setenv("BEEPER_TRANSCRIBE_URL", "https://alias.example.invalid/v1")
    monkeypatch.setenv("BEEPER_TRANSCRIBE_MODEL", "fake-alias-model")
    monkeypatch.setenv("BEEPER_TRANSCRIBE_API_KEY", "fake-alias-key")
    plugin = Beeper()
    chosen = media.transcriber_from(plugin.settings, plugin.secrets)
    assert isinstance(chosen, media.OpenAICompatible)
    assert chosen.base_url == "https://alias.example.invalid/v1"
    assert chosen.model == "fake-alias-model"
    assert chosen.http.headers["Authorization"] == "Bearer fake-alias-key"
    monkeypatch.setenv("THROUGHLINE_TRANSCRIBE_URL", "https://main.example.invalid/v1")
    monkeypatch.setenv("THROUGHLINE_TRANSCRIBE_API_KEY", "fake-main-key")
    monkeypatch.setenv("THROUGHLINE_TRANSCRIBE_MAX_BYTES", "1000")
    plugin = Beeper()
    chosen = media.transcriber_from(plugin.settings, plugin.secrets)
    assert chosen.base_url == "https://main.example.invalid/v1"
    assert chosen.http.headers["Authorization"] == "Bearer fake-main-key"
    assert chosen.max_bytes == 1000
    declared = {d.env for d in Beeper.manifest.secrets.values()}
    assert {"THROUGHLINE_TRANSCRIBE_API_KEY", "BEEPER_TRANSCRIBE_API_KEY"} <= declared


def test_youtube_settings_validated(monkeypatch):
    monkeypatch.setenv("BEEPER_SOURCE", "archive")
    monkeypatch.setenv("BEEPER_RECORDING", str(RECORDINGS / "archive.json"))
    monkeypatch.setenv("BEEPER_YOUTUBE_LINKS", "1")
    Beeper()  # Captions need no transcriber.
    monkeypatch.setenv("BEEPER_YOUTUBE_TRANSCRIPT_SOURCE", "endpoint")
    with pytest.raises(ValueError, match="THROUGHLINE_TRANSCRIBE_URL"):
        Beeper()
    monkeypatch.setenv("BEEPER_YOUTUBE_TRANSCRIPT_SOURCE", "subtitles")
    with pytest.raises(ValueError, match="BEEPER_YOUTUBE_TRANSCRIPT_SOURCE"):
        Beeper()
    monkeypatch.setenv("BEEPER_YOUTUBE_TRANSCRIPT_SOURCE", "auto")
    monkeypatch.setenv("BEEPER_YOUTUBE_FRAME_EVERY", "0")
    with pytest.raises(ValueError, match="BEEPER_YOUTUBE_FRAME_EVERY"):
        Beeper()


def test_youtube_flags():
    env, rest = enrichment_flags(
        [
            "sync",
            "--youtube-links",
            "--youtube-limit",
            "3",
            "--transcript-source",
            "captions",
        ]
    )
    assert rest == ["sync"]
    assert env == {
        "BEEPER_YOUTUBE_LINKS": "1",
        "BEEPER_YOUTUBE_LIMIT": "3",
        "BEEPER_YOUTUBE_TRANSCRIPT_SOURCE": "captions",
    }
