"""Enrichment pacing in Beeper: daily limits and new-inbound-only.

Generated fixtures only: downloads go to a fake transport, Instagram and
YouTube to in-process fakes, local files to a temp folder. The rolling
24-hour window is advanced by ageing the usage rows in the state directory.
"""

import io
import json
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from throughline_media.errors import BudgetExhausted, MediaTransient
from throughline_sdk import Checkpoint

from throughline_beeper import Beeper, enrichment_flags
from throughline_beeper import media

RECORDINGS = Path(__file__).parent / "recorded"
needs_ffmpeg = pytest.mark.skipif(
    not shutil.which("ffmpeg"), reason="ffmpeg not on PATH"
)
ENV = (
    "BEEPER_ATTACHMENTS",
    "BEEPER_INSTAGRAM_LINKS",
    "BEEPER_YOUTUBE_LINKS",
    "BEEPER_ATTACHMENTS_PER_DAY",
    "BEEPER_INSTAGRAM_PER_DAY",
    "BEEPER_YOUTUBE_PER_DAY",
    "BEEPER_BACKLOG",
    "BEEPER_ENRICH",
)


@pytest.fixture(autouse=True)
def new_inbound_default(monkeypatch):
    """Undo conftest's early enable time: these tests check the default."""
    monkeypatch.delenv("BEEPER_ENRICH_SINCE", raising=False)


class Server:
    def __init__(self, files):
        self.files, self.requests = files, []

    def __call__(self, request):
        self.requests.append(request.url.path)
        status, body = self.files.get(request.url.path, (404, b"gone"))
        return httpx.Response(status, content=body)


def png():
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (40, 30), (30, 120, 200)).save(out, "PNG")
    return out.getvalue()


def cdn(att_id):
    return {
        "id": att_id,
        "fileName": f"n{att_id}.txt",
        "url": f"https://cdn.fake.invalid/attachments/1/{att_id}/n{att_id}.txt?ex=s",
    }


def archive_fixture(tmp_path, rows):
    """rows: [(body, attachments)], sent on 2020-01-01, -02, ... in order."""
    data = json.loads((RECORDINGS / "archive.json").read_text())
    base = data["messages"][1]
    data["messages"] = [
        dict(
            base,
            id=f"discord:fake-pace-{i}",
            body=body,
            sent_at=f"2020-01-0{i + 1}T00:00:00Z",
            reply_to="",
            attachments_json=json.dumps(attachments),
        )
        for i, (body, attachments) in enumerate(rows)
    ]
    path = tmp_path / "pacing.json"
    path.write_text(json.dumps(data))
    return path


def plugin(monkeypatch, recording, options=None, **env):
    monkeypatch.setenv("BEEPER_SOURCE", "archive")
    monkeypatch.setenv("BEEPER_RECORDING", str(recording))
    for key in ENV:
        monkeypatch.delenv(key, raising=False)  # Each run states its own flags.
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    made = Beeper()
    made.enrich_options = dict(options or {})
    return made


def run(made, cursor=None, mode="backfill"):
    items = list(made.records(cursor or {}, mode))
    found = {r["external_id"]: r for r in items if isinstance(r, dict)}
    checkpoints = [i.cursor for i in items if isinstance(i, Checkpoint)]
    return found, (checkpoints[-1] if checkpoints else cursor)


def messages(made):
    return run(made)[0]


def state_db(state):
    return sqlite3.connect(state / "enrichment.sqlite3")


def age(state, seconds=24 * 60 * 60 + 1):
    """Move every recorded fetch back in time: the rolling window frees up."""
    with state_db(state) as conn:
        conn.execute("UPDATE usage SET at = at - ?", (seconds,))


def converted(found):
    return sorted(
        k
        for k, m in found.items()
        if m["fields"].get("attachment_files", [{}])[0].get("status") == "converted"
    )


def test_new_messages_only_by_default_backlog_on_request(
    monkeypatch, tmp_path, isolated_enrichment
):
    server = Server(
        {f"/attachments/1/{i}/n{i}.txt": (200, b"x%d" % i) for i in range(3)}
    )
    recording = archive_fixture(
        tmp_path, [("Invented", [cdn(str(i))]) for i in range(3)]
    )
    transport = {"transport": httpx.MockTransport(server)}
    off = messages(plugin(monkeypatch, recording, transport))

    # First use stores the enable time; every fixture message is older.
    first = plugin(monkeypatch, recording, transport, BEEPER_ATTACHMENTS="1")
    found = messages(first)
    assert not server.requests and found == off  # Skipped by policy: unchanged.
    assert first.pacing == {"backlog_skipped": {"attachments": 3}}
    with state_db(isolated_enrichment) as conn:
        (since,) = conn.execute(
            "SELECT since FROM enabled WHERE kind='attachments'"
        ).fetchone()
    assert datetime.now(UTC) - datetime.fromisoformat(since) < timedelta(minutes=5)

    # An explicit enable time replaces the stored one and is kept.
    dated = plugin(
        monkeypatch,
        recording,
        transport,
        BEEPER_ATTACHMENTS="1",
        BEEPER_ENRICH_SINCE="2020-01-02",
    )
    found = messages(dated)
    assert converted(found) == ["discord:fake-pace-1", "discord:fake-pace-2"]
    assert found["discord:fake-pace-0"] == off["discord:fake-pace-0"]
    assert dated.pacing == {"backlog_skipped": {"attachments": 1}}
    monkeypatch.delenv("BEEPER_ENRICH_SINCE")
    again = messages(plugin(monkeypatch, recording, transport, BEEPER_ATTACHMENTS="1"))
    assert again == found and len(server.requests) == 2

    # The backlog only on request; it still respects the limits.
    backlog = plugin(
        monkeypatch, recording, transport, BEEPER_ATTACHMENTS="1", BEEPER_BACKLOG="1"
    )
    found = messages(backlog)
    assert converted(found) == [f"discord:fake-pace-{i}" for i in range(3)]
    assert backlog.pacing == {}


def test_daily_limit_defers_newest_first_and_frees_after_24_hours(
    monkeypatch, tmp_path, isolated_enrichment
):
    monkeypatch.setenv("BEEPER_ENRICH_SINCE", "2000-01-01")
    server = Server(
        {f"/attachments/1/{i}/n{i}.txt": (200, b"x%d" % i) for i in range(3)}
    )
    recording = archive_fixture(
        tmp_path, [("Invented", [cdn(str(i))]) for i in range(3)]
    )
    transport = {"transport": httpx.MockTransport(server)}
    env = dict(BEEPER_ATTACHMENTS="1", BEEPER_ATTACHMENTS_PER_DAY="2")
    off = messages(plugin(monkeypatch, recording, transport))

    first = plugin(monkeypatch, recording, transport, **env)
    found = messages(first)
    assert converted(found) == ["discord:fake-pace-1", "discord:fake-pace-2"]
    # Deferred, not failed: the message is exactly as without enrichment.
    assert found["discord:fake-pace-0"] == off["discord:fake-pace-0"]
    assert first.pacing["deferred"] == {"attachments": 1}
    frees = datetime.fromisoformat(first.pacing["deferred_until"]["attachments"])
    assert timedelta(hours=23) < frees - datetime.now(UTC) <= timedelta(hours=24)

    # A separate run in the same 24 hours: no fetch, no change.
    second = plugin(monkeypatch, recording, transport, **env)
    assert messages(second) == found and len(server.requests) == 2
    assert second.pacing["deferred"] == {"attachments": 1}

    age(isolated_enrichment)
    third = plugin(monkeypatch, recording, transport, **env)
    later = messages(third)
    assert converted(later) == [f"discord:fake-pace-{i}" for i in range(3)]
    assert {k: later[k] for k in found if k != "discord:fake-pace-0"} == {
        k: v for k, v in found.items() if k != "discord:fake-pace-0"
    }
    assert third.pacing == {}


def test_zero_disables_and_unlimited_lifts(monkeypatch, tmp_path, isolated_enrichment):
    monkeypatch.setenv("BEEPER_ENRICH_SINCE", "2000-01-01")
    server = Server(
        {f"/attachments/1/{i}/n{i}.txt": (200, b"x%d" % i) for i in range(3)}
    )
    recording = archive_fixture(
        tmp_path, [("Invented", [cdn(str(i))]) for i in range(3)]
    )
    transport = {"transport": httpx.MockTransport(server)}
    disabled = plugin(
        monkeypatch,
        recording,
        transport,
        BEEPER_ATTACHMENTS="1",
        BEEPER_ATTACHMENTS_PER_DAY="0",
    )
    assert converted(messages(disabled)) == [] and not server.requests
    assert not (isolated_enrichment / "enrichment.sqlite3").exists()
    unlimited = plugin(
        monkeypatch,
        recording,
        transport,
        BEEPER_ATTACHMENTS="1",
        BEEPER_ATTACHMENTS_PER_DAY="unlimited",
        BEEPER_ATTACHMENT_LIMIT="1",
    )
    # Per-run limits stay: one per run, however many the day allows.
    assert len(converted(messages(unlimited))) == 1
    assert unlimited.pacing == {"deferred": {"attachments": 2}}


class FakeInstagram(media.InstagramFetcher):
    def __init__(self, outcomes):
        self.outcomes, self.calls = outcomes, []

    def fetch(self, url, workdir):
        self.calls.append(url)
        outcome = self.outcomes[url]
        if isinstance(outcome, Exception):
            raise outcome
        if outcome == "video":
            return [
                {
                    "type": "video",
                    "duration": 7.0,
                    "path": generated_video(Path(workdir) / "v.mp4"),
                }
            ]
        path = Path(workdir) / "i.png"
        path.write_bytes(png())
        return [{"type": "image", "path": path}]


def test_instagram_daily_limit_counts_failed_attempts(monkeypatch, tmp_path):
    monkeypatch.setenv("BEEPER_ENRICH_SINCE", "2000-01-01")
    urls = [f"https://www.instagram.com/p/FakePace{i}/" for i in range(3)]
    fetcher = FakeInstagram(
        {urls[0]: "image", urls[1]: "image", urls[2]: MediaTransient("rate limited")}
    )
    recording = archive_fixture(tmp_path, [(u, []) for u in urls])
    env = dict(BEEPER_INSTAGRAM_LINKS="1", BEEPER_INSTAGRAM_PER_DAY="2")
    first = plugin(monkeypatch, recording, {"instagram": fetcher}, **env)
    found = messages(first)
    # Newest first: the failing link and the middle one used the day's two.
    assert sorted(fetcher.calls) == [urls[1], urls[2]]
    assert "instagram_links" in found["discord:fake-pace-1"]["fields"]
    assert "instagram_links" not in found["discord:fake-pace-0"]["fields"]
    assert "instagram_links" not in found["discord:fake-pace-2"]["fields"]
    assert first.pacing["deferred"] == {"instagram": 1}
    again = plugin(monkeypatch, recording, {"instagram": fetcher}, **env)
    assert messages(again) == found and len(fetcher.calls) == 2


def test_enrich_env_switches_flags_and_validation(monkeypatch, tmp_path):
    env, rest = enrichment_flags(
        [
            "sync",
            "--instagram-per-day",
            "unlimited",
            "--attachments-per-day",
            "0",
            "--youtube-per-day",
            "5",
            "--transcribe-minutes-per-day",
            "30",
            "--backlog",
            "--enrich-since",
            "2026-01-31",
        ]
    )
    assert rest == ["sync"]
    assert env == {
        "BEEPER_INSTAGRAM_PER_DAY": "unlimited",
        "BEEPER_ATTACHMENTS_PER_DAY": "0",
        "BEEPER_YOUTUBE_PER_DAY": "5",
        "THROUGHLINE_TRANSCRIBE_MINUTES_PER_DAY": "30",
        "BEEPER_BACKLOG": "1",
        "BEEPER_ENRICH_SINCE": "2026-01-31",
    }
    recording = archive_fixture(tmp_path, [("Invented", [])])
    made = plugin(monkeypatch, recording, BEEPER_ENRICH="attachments, instagram")
    assert made.settings["attachments"] == "1"
    assert made.settings["instagram_links"] == "1"
    assert made.settings.get("youtube_links") in (None, "", "0")
    for key, value, message in (
        ("BEEPER_ENRICH", "attachments,podcasts", "BEEPER_ENRICH"),
        ("BEEPER_INSTAGRAM_PER_DAY", "-2", "BEEPER_INSTAGRAM_PER_DAY"),
        ("BEEPER_ATTACHMENTS_PER_DAY", "lots", "BEEPER_ATTACHMENTS_PER_DAY"),
        ("BEEPER_ENRICH_SINCE", "yesterday", "BEEPER_ENRICH_SINCE"),
        ("THROUGHLINE_TRANSCRIBE_MINUTES_PER_DAY", "x", "MINUTES_PER_DAY"),
    ):
        with monkeypatch.context() as patch:
            patch.setenv(key, value)
            with pytest.raises(ValueError, match=message):
                Beeper()


def live_fixture(tmp_path, attached):
    """The live recording with a local text file on each `attached` message."""
    folder = tmp_path / "desktop-media"
    folder.mkdir()
    live = json.loads((RECORDINGS / "live.json").read_text())
    pages = [
        r["body"]
        for r in live["responses"]
        if r["path"] == "v1/chats/fake-chat-discord/messages"
    ]
    for page in pages:
        for item in page["items"]:
            index = int(item["id"].rsplit("-", 1)[1])
            if index in attached:
                (folder / f"n{index}.txt").write_bytes(b"live %d" % index)
                item["attachments"] = [
                    {
                        "id": f"fake-live-{index}",
                        "fileName": f"n{index}.txt",
                        "srcURL": (folder / f"n{index}.txt").as_uri(),
                    }
                ]
    path = tmp_path / "live.json"
    path.write_text(json.dumps(live))
    return path


def attached(found):
    return sorted(
        m["fields"]["attachment_files"][0]["name"]
        for m in found.values()
        if m["fields"].get("attachment_files")
    )


def test_live_sync_enriches_new_inbound_and_revisits_deferred(
    monkeypatch, tmp_path, isolated_enrichment
):
    path = live_fixture(tmp_path, {0, 1, 2, 3})
    monkeypatch.setenv("BEEPER_SOURCE", "live")
    monkeypatch.setenv("BEEPER_RECORDING", str(path))
    # Scheduled syncs switch enrichment on by environment, no flags.
    monkeypatch.setenv("BEEPER_ENRICH", "attachments")
    monkeypatch.setenv("BEEPER_ATTACHMENTS_PER_DAY", "1")
    # Messages 0-2 arrived after enabling (6, 5 and 4 January); 3 before.
    monkeypatch.setenv("BEEPER_ENRICH_SINCE", "2020-01-03T12:00:00Z")
    first = Beeper()
    found, cursor = run(first)
    assert attached(found) == ["n0.txt"]
    assert first.pacing["deferred"] == {"attachments": 2}
    assert first.pacing["backlog_skipped"] == {"attachments": 1}
    with state_db(isolated_enrichment) as conn:
        assert conn.execute("SELECT COUNT(*) FROM revisit").fetchone() == (2,)

    # Same day: the queue waits; nothing is sent again.
    second = Beeper()
    found, cursor = run(second, cursor, "sync")
    assert found == {}

    # Window freed: the newest queued message is finished and sent again.
    age(isolated_enrichment)
    found, cursor = run(Beeper(), cursor, "sync")
    assert attached(found) == ["n1.txt"] and len(found) == 1
    age(isolated_enrichment)
    found, cursor = run(Beeper(), cursor, "sync")
    assert attached(found) == ["n2.txt"]
    with state_db(isolated_enrichment) as conn:
        assert conn.execute("SELECT COUNT(*) FROM revisit").fetchone() == (0,)
    age(isolated_enrichment)
    assert run(Beeper(), cursor, "sync")[0] == {}


def generated_video(path, seconds=7):
    subprocess.run(
        [
            shutil.which("ffmpeg"),
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size=160x120:rate=10:duration={seconds}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "mpeg4",
            "-shortest",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


class MinutesTranscriber(media.Transcriber):
    """Fake endpoint whose allowance can run out or be too small."""

    name = "fake-endpoint"

    def __init__(self, exhausted=False, capacity=None):
        self.exhausted, self.capacity, self.calls = exhausted, capacity, 0

    def capacity_s(self):
        return self.capacity

    def remaining_s(self):
        return 0.0 if self.exhausted else None

    def transcribe(self, audio_path):
        if self.exhausted:
            raise BudgetExhausted("daily transcription minutes used up")
        self.calls += 1
        return "invented spoken words"


@needs_ffmpeg
def test_reel_waits_for_transcription_minutes_without_version_churn(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("BEEPER_ENRICH_SINCE", "2000-01-01")
    reel = "https://www.instagram.com/reel/FakePaceReel/"
    fetcher = FakeInstagram({reel: "video"})
    recording = archive_fixture(tmp_path, [(reel, [])])

    def go(transcriber):
        made = plugin(
            monkeypatch,
            recording,
            {"instagram": fetcher, "transcriber": transcriber},
            BEEPER_INSTAGRAM_LINKS="1",
        )
        return made, messages(made)["discord:fake-pace-0"]

    # Minutes already used up today: the reel is not even fetched.
    made, waiting = go(MinutesTranscriber(exhausted=True))
    assert not fetcher.calls and "instagram_links" not in waiting["fields"]
    assert made.pacing["deferred"] == {"transcribe_minutes": 1}

    # Minutes run out mid-way: fetched, but not shown until transcribed.
    out_of_minutes = MinutesTranscriber()
    out_of_minutes.remaining_s = lambda: None
    out_of_minutes.transcribe = lambda path: (_ for _ in ()).throw(
        BudgetExhausted("daily transcription minutes used up")
    )
    made, fetched = go(out_of_minutes)
    assert fetcher.calls == [reel] and fetched == waiting
    assert made.pacing["deferred"] == {"transcribe_minutes": 1}

    # Minutes free again: transcribed from the stored audio, no new download.
    later = MinutesTranscriber()
    made, done = go(later)
    assert later.calls == 1 and fetcher.calls == [reel]
    assert "invented spoken words" in done["content"]
    assert made.pacing == {}


@needs_ffmpeg
def test_shown_reel_keeps_its_content_while_retranscription_waits(
    monkeypatch, tmp_path
):
    """A reel already shown (no transcriber then) must not vanish from the
    message while a newly configured endpoint has no minutes left."""
    monkeypatch.setenv("BEEPER_ENRICH_SINCE", "2000-01-01")
    reel = "https://www.instagram.com/reel/FakePaceShown/"
    fetcher = FakeInstagram({reel: "video"})
    recording = archive_fixture(tmp_path, [(reel, [])])

    def go(transcriber=None):
        options = {"instagram": fetcher}
        if transcriber is not None:
            options["transcriber"] = transcriber
        made = plugin(monkeypatch, recording, options, BEEPER_INSTAGRAM_LINKS="1")
        return made, messages(made)["discord:fake-pace-0"]

    made, shown = go()  # BEEPER_TRANSCRIBER=none: final "transcript unavailable".
    item = shown["fields"]["instagram_links"][0]["items"][0]
    assert item["transcript_status"] == "unavailable" and fetcher.calls == [reel]

    out_of_minutes = MinutesTranscriber()
    out_of_minutes.remaining_s = lambda: None
    out_of_minutes.transcribe = lambda path: (_ for _ in ()).throw(
        BudgetExhausted("daily transcription minutes used up")
    )
    made, waiting = go(out_of_minutes)
    assert waiting == shown  # Same content and fields: no new version.
    assert made.pacing["deferred"] == {"transcribe_minutes": 1}
    made, again = go(out_of_minutes)
    assert again == shown

    later = MinutesTranscriber()
    made, done = go(later)
    assert later.calls == 1 and fetcher.calls == [reel]
    assert "invented spoken words" in done["content"] and made.pacing == {}


@needs_ffmpeg
def test_reel_longer_than_the_allowance_is_final_until_raised(monkeypatch, tmp_path):
    monkeypatch.setenv("BEEPER_ENRICH_SINCE", "2000-01-01")
    reel = "https://www.instagram.com/reel/FakePaceLong/"
    fetcher = FakeInstagram({reel: "video"})
    recording = archive_fixture(tmp_path, [(reel, [])])

    def go(transcriber):
        made = plugin(
            monkeypatch,
            recording,
            {"instagram": fetcher, "transcriber": transcriber},
            BEEPER_INSTAGRAM_LINKS="1",
        )
        return messages(made)["discord:fake-pace-0"]

    small = MinutesTranscriber(capacity=3)
    first = go(small)
    item = first["fields"]["instagram_links"][0]["items"][0]
    assert item["transcript_status"] == "over_budget" and small.calls == 0
    assert (
        "transcript unavailable (longer than the transcription minutes allowed"
        in first["content"]
    )
    assert go(MinutesTranscriber(capacity=3)) == first  # Final: no churn.
    raised = MinutesTranscriber(capacity=600)
    assert "invented spoken words" in go(raised)["content"]
    assert raised.calls == 1 and len(fetcher.calls) == 1


class FakeYouTube:
    authenticated = False

    def __init__(self, ids):
        self.ids, self.calls = ids, []

    def info(self, video):
        self.calls.append(video)
        return {
            "id": video,
            "title": "Invented " + video,
            "duration": 5,
            "subtitles": ["en"],
            "timestamp": 1577880000,
        }

    def captions(self, video, kind, lang, workdir):
        return "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\ninvented words\n"

    def video(self, video, workdir, max_bytes):
        (Path(workdir) / "dl").mkdir()
        return generated_video(Path(workdir) / "dl" / "video.mp4", 5)


@needs_ffmpeg
def test_youtube_links_daily_limit(monkeypatch, tmp_path):
    monkeypatch.setenv("BEEPER_ENRICH_SINCE", "2000-01-01")
    ids = [f"FAKEPACE0{i}A" for i in range(3)]
    fetcher = FakeYouTube(ids)
    recording = archive_fixture(tmp_path, [(f"https://youtu.be/{v}", []) for v in ids])
    env = dict(
        BEEPER_YOUTUBE_LINKS="1",
        BEEPER_YOUTUBE_PER_DAY="1",
        BEEPER_YOUTUBE_TRANSCRIPT_SOURCE="captions",
    )
    first = plugin(monkeypatch, recording, {"youtube_fetcher": fetcher}, **env)
    found = messages(first)
    assert fetcher.calls == [ids[2]]  # The newest message's video.
    assert [k for k, m in found.items() if "youtube_links" in m["fields"]] == [
        "discord:fake-pace-2"
    ]
    assert first.pacing["deferred"] == {"youtube": 2}
    assert "youtube" in first.pacing["deferred_until"]
    assert (
        messages(plugin(monkeypatch, recording, {"youtube_fetcher": fetcher}, **env))
        == found
    )
    assert fetcher.calls == [ids[2]]


def test_archive_candidates_query_matches_fixture_filter(synthetic_archive):
    """The planning pre-filter on a real (throwaway) Postgres archive."""
    import psycopg

    from throughline_beeper.sources import Archive

    with psycopg.connect(synthetic_archive) as conn:
        conn.execute(
            "INSERT INTO archive.messages (id, platform, body, attachments_json,"
            " sent_at) VALUES ('discord:fake-link', 'discord',"
            " 'see https://WWW.Instagram.com/reel/FakeQuery/', '[]',"
            " '2020-02-01T00:00:00Z'), ('discord:fake-null', 'discord', NULL,"
            " NULL, '2020-02-02T00:00:00Z')"
        )
    fixture = Archive(None, RECORDINGS / "archive.json")
    live = Archive(synthetic_archive)
    try:

        def ids(source, **kinds):
            return sorted(r["id"] for r in source.candidates(**kinds))

        attachments = ids(live, links=False, attachments=True)
        assert attachments == ids(fixture, links=False, attachments=True)
        assert len(attachments) == 15
        assert ids(live, links=True, attachments=False) == ["discord:fake-link"]
        assert ids(live) == sorted([*attachments, "discord:fake-link"])
        assert ids(live, links=False, attachments=False) == []
        row = next(iter(live.candidates(links=True, attachments=False)))
        assert set(row) == set(Archive.CANDIDATE_COLUMNS)
    finally:
        live.close()
