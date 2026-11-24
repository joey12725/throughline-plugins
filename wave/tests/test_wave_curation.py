"""Coverage, source curation and late enrolment against a core.

The archive is the invented one of conftest.py: three recordings of four
turns each; turn 4 of every recording carries ignore_reason, recording 2 is
ignored as a whole, "Voice A" is an enrolled voice_profile and "Voice B" is a
bare speaker label.
"""

import contextlib
import json
import subprocess
import sys
from pathlib import Path

import psycopg
from throughline_sdk import Client, Record
from throughline_sdk._server import core_server

from throughline_wave import RECORDINGS, TURNS, Wave

RECORDED = Path(__file__).parent / "recorded"


def wave(monkeypatch, source, **env):
    monkeypatch.setenv("WAVE_SOURCE", source)
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    return Wave()


def reports(records):
    return {
        (r.data["channel"], r.data["scope_key"]): r.data
        for r in records
        if isinstance(r, Record) and r.kind == "coverage"
    }


def plugin_token(env):
    output = subprocess.run(
        [
            sys.executable,
            "-m",
            "throughline",
            "token",
            "create",
            "invented-wave",
            "--kind",
            "plugin",
            "--plugin",
            "wave",
            "--json",
        ],
        env=env,
        check=True,
        capture_output=True,
    ).stdout
    return json.loads(output)["token"]


@contextlib.contextmanager
def wave_core():
    """(owner client, plugin client) against a disposable core."""
    with core_server() as (_http, env):
        owner = Client(env["THROUGHLINE_URL"], env["THROUGHLINE_TOKEN"], "wave")
        plugin = Client(env["THROUGHLINE_URL"], plugin_token(env), "wave")
        try:
            yield owner, plugin
        finally:
            owner.close()
            plugin.close()


def sql_rows(client, statement):
    return client.request("POST", "sql", {"statement": statement})["rows"]


def recalled(client, channel=TURNS, **scope):
    """Every record recall may return in the channel (enumerated, no top-k)."""
    answer = client.request(
        "POST",
        "recall",
        {"query": "", "exhaustive": True, "scope": {"channels": [channel], **scope}},
    )
    return {r["external_id"] for r in answer["records"]}


def annotate(owner, external_id, value, reason):
    return owner.request(
        "POST",
        "annotations",
        {
            "target": {"channel": TURNS, "external_id": external_id},
            "attribute": "ignore",
            "value": value,
            "reason": reason,
        },
    )


def edit_turn(dsn, recording, turn, text):
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "UPDATE transcripts.turn SET text=%s WHERE recording_id=%s AND turn_no=%s",
            (text, recording, turn),
        )


def versions(client, external_id):
    return sql_rows(
        client,
        f"SELECT count(*) AS n FROM api.message_versions WHERE channel='{TURNS}' AND external_id='{external_id}'",
    )[0]["n"]


# Current turns with the kind of their author identifier.
AUTHORED = "api.messages m LEFT JOIN api.identifiers i ON i.id=m.author_identifier_id"
AUTHOR_KIND = "i.kind AS author_kind"
ALL_TURNS = {f"fake-recording-{r}:{t}" for r in range(3) for t in range(1, 5)}
SOURCE_IGNORED = {f"fake-recording-{r}:4" for r in range(3)}


def test_wave_coverage_archive_complete_api_best_effort_listing_lag(
    monkeypatch, archive_dsn
):
    # Archive: a full reconciliation of the recording set is complete.
    archive = wave(monkeypatch, "archive", WAVE_ARCHIVE_DSN=archive_dsn)
    found = reports(archive.records({}, "backfill"))
    assert set(found) == {(RECORDINGS, "archive"), (TURNS, "archive")}
    for report in found.values():
        assert report["status"] == "complete" and report["verified"] is True
        assert "window" not in report and "gaps" not in report
    assert found[(TURNS, "archive")]["rows_delivered"] == 12
    assert found[(RECORDINGS, "archive")]["rows_delivered"] == 3
    # API mode: best effort with listing_lag during the overlap window.
    api = wave(monkeypatch, "api", WAVE_API_FIXTURE=RECORDED / "api.json")
    records = list(api.records({}, "backfill"))
    found = reports(records)
    for channel in (RECORDINGS, TURNS):
        report = found[(channel, "api")]
        assert report["status"] == "best_effort"
        assert report["reasons"] == ["listing_lag"]
        assert report["detail"] == {"overlap_hours": 168.0}
        assert "verified" not in report
    # The daily budget stops a sync: partial with rate_limited, no window.
    cursor = dict(records[-1].cursor)
    cursor["budget"] = dict(cursor["budget"], requests=9000)
    stopped = reports(api.records(cursor, "sync"))
    for channel in (RECORDINGS, TURNS):
        report = stopped[(channel, "api")]
        assert report["status"] == "partial"
        assert report["reasons"] == ["rate_limited"]
        assert report["detail"]["limit"] == 8900
        assert "window" not in report
    # Against a core: the reports are stored per channel and scope key, and a
    # best-effort report opens no gap.
    with wave_core() as (owner, plugin):
        archive.execute(plugin, "backfill")
        api.execute(plugin, "backfill")
        rows = {
            (r["channel"], r["scope_key"]): r
            for r in sql_rows(
                owner,
                "SELECT channel,scope_key,status,reasons FROM api.coverage WHERE plugin='wave' AND reported",
            )
        }
        assert rows[(TURNS, "archive")]["status"] == "complete"
        assert rows[(RECORDINGS, "archive")]["status"] == "complete"
        assert rows[(TURNS, "api")]["status"] == "best_effort"
        assert rows[(TURNS, "api")]["reasons"] == ["listing_lag"]
        assert not plugin.coverage_gaps()


def test_ignore_reason_excluded_by_default_and_curation_survives_reimport(
    monkeypatch, archive_dsn
):
    archive = wave(monkeypatch, "archive", WAVE_ARCHIVE_DSN=archive_dsn)
    with wave_core() as (owner, plugin):
        archive.execute(plugin, "backfill")
        # Source ignore_reason turns (and the ignored recording) are out of
        # default recall and back with include_ignored.
        assert recalled(owner) == ALL_TURNS - SOURCE_IGNORED
        assert recalled(owner, include_ignored=True) == ALL_TURNS
        assert recalled(owner, RECORDINGS) == {"fake-recording-0", "fake-recording-1"}
        # Owner curation: an overlay annotation keyed on the external id.
        annotate(owner, "fake-recording-0:1", {"ignored": True}, "Invented chatter")
        curated = ALL_TURNS - SOURCE_IGNORED - {"fake-recording-0:1"}
        assert recalled(owner) == curated
        # A re-import that changes the turn writes a new version; the
        # annotation follows the record.
        edit_turn(archive_dsn, "fake-recording-0", 1, "Invented corrected chatter")
        archive.execute(plugin, "sync")
        assert versions(owner, "fake-recording-0:1") == 2
        assert recalled(owner) == curated
        # An unchanged re-import writes nothing and keeps the curation too.
        before = sql_rows(owner, "SELECT count(*) AS n FROM api.message_versions")
        archive.execute(plugin, "sync")
        assert (
            sql_rows(owner, "SELECT count(*) AS n FROM api.message_versions") == before
        )
        assert recalled(owner) == curated


def test_owner_unignore_survives_reimport(monkeypatch, archive_dsn):
    archive = wave(monkeypatch, "archive", WAVE_ARCHIVE_DSN=archive_dsn)
    with wave_core() as (owner, plugin):
        archive.execute(plugin, "backfill")
        assert "fake-recording-1:4" not in recalled(owner)
        annotate(owner, "fake-recording-1:4", {"ignored": False}, "Invented: we spoke")
        assert "fake-recording-1:4" in recalled(owner)
        # The source still flags the turn; a changed re-import is a new
        # version that keeps its ignore_reason, and the owner still decides.
        edit_turn(archive_dsn, "fake-recording-1", 4, "Invented words, corrected")
        archive.execute(plugin, "sync")
        assert versions(owner, "fake-recording-1:4") == 2
        (row,) = sql_rows(
            owner,
            f"SELECT id,fields FROM api.messages WHERE channel='{TURNS}' AND external_id='fake-recording-1:4'",
        )
        assert row["fields"]["ignore_reason"] == "synthetic ignored turn"
        assert "fake-recording-1:4" in recalled(owner)
        (effective,) = owner.request("GET", "effective", params={"ids": row["id"]})[
            "rows"
        ]
        assert effective["ignored"] is False
        # The other source-ignored turns stay out.
        assert not {"fake-recording-0:4", "fake-recording-2:4"} & recalled(owner)


def test_late_enrolment_resolves_earlier_turns(monkeypatch, archive_dsn):
    archive = wave(monkeypatch, "archive", WAVE_ARCHIVE_DSN=archive_dsn)
    voice_b = {"fake-recording-0:2", "fake-recording-0:4"}
    statement = (
        f"SELECT m.external_id,m.person_id,{AUTHOR_KIND} FROM {AUTHORED} WHERE m.channel='{TURNS}'"
        " AND m.thread_external_id='fake-recording-0'"
    )
    with wave_core() as (owner, plugin):
        archive.execute(plugin, "backfill")
        person = owner.request(
            "POST",
            "identity/people",
            {"handle": "invented-voice-b", "display_name": "Invented Voice B"},
        )
        before = {r["external_id"]: r for r in sql_rows(owner, statement)}
        assert {before[k]["author_kind"] for k in voice_b} == {"speaker_label"}
        assert {before[k]["person_id"] for k in voice_b} == {None}
        # The voice is enrolled in the source after the first import; the
        # next reconciliation re-sends the recording's turns with the
        # voice_profile author.
        with psycopg.connect(archive_dsn) as conn:
            conn.execute(
                "INSERT INTO transcripts.speaker VALUES ('fake-recording-0','Voice B','Voice B','wave','voice_profile','fake-profile-b','voice_match')"
            )
        archive.execute(plugin, "sync")
        (voice,) = sql_rows(
            owner,
            "SELECT id FROM api.identifiers WHERE kind='voice_profile' AND value='fake-profile-b'",
        ) or [None]
        if voice is None:
            voice = next(
                i
                for i in owner.registry("identifiers", method="GET")
                if i["kind"] == "voice_profile" and i["value"] == "fake-profile-b"
            )
        owner.request(
            "POST",
            "identity/attach",
            {"identifier_id": voice["id"], "person_id": person["id"]},
        )
        after = {r["external_id"]: r for r in sql_rows(owner, statement)}
        # The same records (new versions, no new turns) resolve to the person.
        assert set(after) == set(before)
        assert {after[k]["author_kind"] for k in voice_b} == {"voice_profile"}
        assert {after[k]["person_id"] for k in voice_b} == {person["id"]}
        assert all(versions(owner, k) == 2 for k in voice_b)
        # Voice B of another recording is still only a label.
        (other,) = sql_rows(
            owner,
            f"SELECT m.person_id,{AUTHOR_KIND} FROM {AUTHORED} WHERE m.channel='{TURNS}' AND m.external_id='fake-recording-1:2'",
        )
        assert other == {"person_id": None, "author_kind": "speaker_label"}


def test_speaker_labels_stay_unclaimed(monkeypatch, archive_dsn):
    archive = wave(monkeypatch, "archive", WAVE_ARCHIVE_DSN=archive_dsn)
    with wave_core() as (owner, plugin):
        archive.execute(plugin, "backfill")
        labels = [
            i
            for i in owner.registry("identifiers", method="GET")
            if i["kind"] == "speaker_label"
        ]
        assert sorted(i["value"] for i in labels) == [
            f"fake-recording-{r}:Voice B" for r in range(3)
        ]
        assert {i["person_id"] for i in labels} == {None}
        assert {
            r["person_id"]
            for r in sql_rows(
                owner,
                f"SELECT m.person_id FROM {AUTHORED} WHERE m.channel='{TURNS}' AND i.kind='speaker_label'",
            )
        } == {None}
        inbox = owner.request("GET", "transcripts/speakers")["speakers"]
        titles = {r["recording_title"] for r in inbox}
        assert titles == {f"Invented meeting {r}" for r in range(3)}
        assert all("Voice B" in r["label"] for r in inbox)
        assert len(inbox) == 3
        # The enrolled voice never appears in the inbox.
        assert not any("Voice A" in r["label"] for r in inbox)
