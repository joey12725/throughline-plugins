import copy
import json
import shutil

import pytest
from throughline_sdk import Checkpoint, Record
from throughline_sdk.contract import check_plugin

from throughline_calendar import MAPPING_REVISION, Calendar


def plugin(env, source, **settings):
    env.setenv("CALENDAR_SOURCE", "export:" + str(source))
    for key, value in settings.items():
        env.setenv(key, value)
    return Calendar()


def messages(items):
    return [i for i in items if isinstance(i, dict)]


def final_cursor(items):
    return [i for i in items if isinstance(i, Checkpoint)][-1].cursor


def by_id(items):
    return {m["external_id"].split(":", 1)[-1]: m for m in messages(items)}


def test_cal_contract_kit(calendar_env, export_dir, data):
    calendar_env.setenv("COMPOSIO_API_KEY", data.CANARY_KEY)
    result = check_plugin(
        "throughline_calendar:Calendar",
        {
            "CALENDAR_SOURCE": "export:" + str(export_dir),
            "CALENDAR_BATCH_SIZE": "4",
            "COMPOSIO_API_KEY": data.CANARY_KEY,
        },
        secrets=[data.CANARY_KEY],
    )
    assert result["messages"] >= 20


def test_cal_occurrences_subject_time_and_fields(calendar_env, export_dir, data):
    rows = by_id(plugin(calendar_env, export_dir).records({}, "backfill"))
    dentist = rows[data.CAL_A + ":inventeddentist"]
    assert dentist["channel"] == "calendar.events"
    assert dentist["happened_at"] == "2024-02-10T15:00:00-06:00"
    assert dentist["ended_at"] == "2024-02-10T16:00:00-06:00"
    assert dentist["subject"] == {
        "start": "2024-02-10T15:00:00-06:00",
        "start_precision": "exact",
        "end": "2024-02-10T16:00:00-06:00",
        "end_precision": "exact",
    }
    assert dentist["thread"]["external_id"] == f"{data.ACCOUNT}:{data.CAL_A}"
    assert dentist["thread"]["title"] == "Invented Personal"
    assert dentist["author"] == {
        "system": "email",
        "kind": "email",
        "value": data.CAL_A,
        "display_name": None,
    }
    assert dentist["content"].splitlines()[:4] == [
        "Invented dentist",
        "When: 2024-02-10 15:00 to 16:00 (UTC-06:00)",
        "Where: Invented Clinic, 1 Example Street",
        "Calendar: Invented Personal",
    ]
    fields = dentist["fields"]
    assert fields["place"] == "Invented Clinic, 1 Example Street"
    assert fields["mapping_revision"] == MAPPING_REVISION
    assert fields["event_created"] == "2024-01-15T10:00:00+00:00"
    assert dentist["payload"]["source"] == data.DENTIST

    trip = rows[data.CAL_A + ":inventedtrip"]
    assert trip["happened_at"] == "2024-03-20T00:00:00+00:00"
    assert trip["ended_at"] == "2024-03-23T00:00:00+00:00"
    assert trip["subject"]["end"] == "2024-03-22T00:00:00+00:00"
    assert (
        trip["subject"]["start_precision"] == trip["subject"]["end_precision"] == "day"
    )
    assert trip["fields"]["all_day"] is True
    assert "When: 2024-03-20 to 2024-03-22 (all day)" in trip["content"]

    review = rows[data.CAL_B + ":inventedreview"]
    assert review["author"] is None  # A calendar address is not a person.
    assert (
        review["fields"]["description"]
        == "Agenda:\n1. Invented item\n\nSee the doc (https://docs.invalid/x)"
    )
    assert review["fields"]["conference_url"] == "https://meet.invalid/abc-defg-hij"

    dinner = rows[data.CAL_A + ":inventeddinner"]
    assert dinner["fields"]["status"] == "cancelled"
    assert dinner["content"].startswith("Cancelled: Invented dinner")
    assert dinner["author"]["value"] == "invented.friend@example.invalid"
    assert dinner["fields"]["my_response"] == "tentative"
    assert dinner["fields"]["attendee_count"] == 2


def test_cal_recurring_instances_and_removals(calendar_env, export_dir, data):
    rows = by_id(plugin(calendar_env, export_dir).records({}, "backfill"))
    cancelled = rows[f"{data.CAL_A}:{data.SERIES}_20240311T150000Z"]
    # The tombstone has no title: the earlier snapshot supplies it.
    assert cancelled["fields"]["title"] == "Invented standup"
    assert cancelled["fields"]["status"] == "cancelled"
    assert cancelled["happened_at"] == "2024-03-11T15:00:00+00:00"
    for day in ("20240325", "20240401"):
        gone = rows[f"{data.CAL_A}:{data.SERIES}_{day}T150000Z"]
        assert gone["fields"]["status"] == "removed"
        assert gone["fields"]["observed_at"] == "2024-03-15T12:00:00+00:00"
        assert gone["content"].startswith("Removed from calendar: Invented standup")
    kept = rows[f"{data.CAL_A}:{data.SERIES}_20240318T150000Z"]
    assert kept["fields"]["status"] == "confirmed"
    assert kept["fields"]["event_updated"] == "2024-03-14T09:00:00+00:00"
    assert rows[data.CAL_A + ":inventedcall"]["fields"]["status"] == "removed"


def test_cal_lifecycle_record_times(calendar_env, export_dir, data):
    rows = by_id(plugin(calendar_env, export_dir).records({}, "backfill"))
    a = data.CAL_A
    created = rows[a + ":inventedlunch:created"]
    assert created["channel"] == "calendar.changes"
    assert created["happened_at"] == "2024-02-20T10:00:00+00:00"
    assert created["subject"]["start"] == "2024-03-05T12:00:00-06:00"
    assert created["author"]["value"] == a
    assert created["fields"]["time_precision"] == "source"
    assert created["thread"]["external_id"] == f"{data.ACCOUNT}:{a}:inventedlunch"
    # Changed before it was first seen: the API time, fields unknown.
    first = rows[a + ":inventedlunch:changed:2024-02-25T10:00:00+00:00"]
    assert first["fields"]["changed_fields"] is None
    moved = rows[a + ":inventedlunch:changed:2024-03-07T08:00:00+00:00"]
    assert moved["happened_at"] == "2024-03-07T08:00:00+00:00"
    assert moved["fields"]["changed_fields"] == ["event_start", "event_end"]
    assert moved["content"].startswith(
        "Changed event: Invented lunch (event_start, event_end)"
    )
    # A new event created within the grace period has no change record.
    assert not [k for k in rows if k.startswith(a + ":inventeddentist:changed")]
    series = rows[f"{a}:{data.SERIES}:created"]
    assert series["happened_at"] == "2024-01-01T09:00:00+00:00"
    assert "subject" not in series  # The series start is unknown from an instance.
    rule = rows[f"{a}:{data.SERIES}:changed:2024-03-14T09:00:00+00:00"]
    assert rule["fields"]["changed_fields"] == ["recurrence"]
    exception = rows[
        f"{a}:{data.SERIES}_20240311T150000Z:cancelled:2024-03-07T09:00:00+00:00"
    ]
    assert exception["content"].startswith("Cancelled occurrence of Invented standup")
    assert exception["thread"]["external_id"] == f"{data.ACCOUNT}:{a}:{data.SERIES}"
    dinner = rows[a + ":inventeddinner:cancelled:2024-03-12T07:00:00+00:00"]
    assert dinner["fields"]["change"] == "cancelled"
    # Snapshot diffs carry the fetch time and observed precision.
    call = rows[a + ":inventedcall:removed:observed:2024-03-15T12:00:00+00:00"]
    assert call["fields"]["time_precision"] == "observed"
    assert call["happened_at"] == "2024-03-15T12:00:00+00:00"
    gone = rows[f"{a}:{data.SERIES}:removed:observed:2024-03-15T12:00:00+00:00"]
    assert gone["fields"]["occurrence_count"] == 2
    assert "2024-03-25, 2024-04-01" in gone["content"]
    retitled = rows[
        data.CAL_B + ":inventedreview:changed:observed:2024-03-15T12:00:00+00:00"
    ]
    assert retitled["fields"]["changed_fields"] == ["title"]


def test_cal_links_to_event_entities(calendar_env, export_dir, data):
    items = list(plugin(calendar_env, export_dir).records({}, "backfill"))
    links = [i.data for i in items if isinstance(i, Record) and i.kind == "link"]
    assert len(links) == len(messages(items))
    assert {link["method"] for link in links} == {"source"}
    assert {link["type"] for link in links} == {"part_of", "about"}
    series = {
        link["to"]["external_ref"]
        for link in links
        if data.SERIES in link["from"]["external_id"]
    }
    assert series == {"ical:" + data.SERIES + "@invented.invalid"}
    assert {link["to"]["kind"] for link in links} == {"calendar_event"}
    calendar_env.setenv("CALENDAR_LINKS", "false")
    assert not [i for i in Calendar().records({}, "backfill") if isinstance(i, Record)]


def test_cal_rerun_and_replay_are_idempotent(calendar_env, export_dir):
    first = list(plugin(calendar_env, export_dir).records({}, "backfill"))
    cursor = final_cursor(first)
    again = list(Calendar().records(copy.deepcopy(cursor), "sync"))
    assert messages(again) == []
    assert final_cursor(again) == cursor
    replay = list(Calendar().records({}, "backfill"))
    assert messages(replay) == messages(first)
    # Every record appears once per invocation (reversed delivery is safe).
    keys = [(m["channel"], m["external_id"]) for m in messages(first)]
    assert len(keys) == len(set(keys))


def test_cal_incremental_runs_end_in_the_full_state(calendar_env, export_dir, tmp_path):
    full = {
        (m["channel"], m["external_id"]): m
        for m in messages(plugin(calendar_env, export_dir).records({}, "backfill"))
    }
    growing = tmp_path / "growing"
    growing.mkdir()
    calendar_env.setenv("CALENDAR_SOURCE", "export:" + str(growing))
    cursor, latest = {}, {}
    for path in sorted(export_dir.iterdir()):
        shutil.copy(path, growing / path.name)
        items = list(Calendar().records(cursor, "sync"))
        for message in messages(items):
            latest[(message["channel"], message["external_id"])] = message
        cursor = final_cursor(items)
    assert latest == full


def test_cal_resume_from_interim_checkpoint(calendar_env, export_dir):
    items = list(
        plugin(calendar_env, export_dir, CALENDAR_BATCH_SIZE="3").records(
            {}, "backfill"
        )
    )
    checkpoints = [i for i, item in enumerate(items) if isinstance(item, Checkpoint)]
    assert len(checkpoints) > 3
    cut = checkpoints[2]
    interim = items[cut].cursor
    assert interim["resume"]["offset"] == 9
    assert "last_run" not in interim
    rest = list(Calendar().records(copy.deepcopy(interim), "backfill"))
    assert messages(items[: cut + 1]) + messages(rest) == messages(items)
    assert final_cursor(rest) == final_cursor(items)


def test_cal_mapping_revision_change_replays(calendar_env, export_dir):
    first = list(plugin(calendar_env, export_dir).records({}, "backfill"))
    old = dict(final_cursor(first), mapping_revision=MAPPING_REVISION - 1)
    assert messages(Calendar().records(old, "sync")) == messages(first)


def test_cal_calendar_filter(calendar_env, export_dir, data):
    rows = messages(
        plugin(calendar_env, export_dir, CALENDAR_EXCLUDE_CALENDARS=data.CAL_B).records(
            {}, "backfill"
        )
    )
    assert rows and all(m["fields"]["calendar_id"] == data.CAL_A for m in rows)
    calendar_env.delenv("CALENDAR_EXCLUDE_CALENDARS")
    calendar_env.setenv("CALENDAR_CALENDARS", data.CAL_B)
    rows = messages(Calendar().records({}, "backfill"))
    assert rows and all(m["fields"]["calendar_id"] == data.CAL_B for m in rows)


def test_cal_late_older_run_is_skipped(calendar_env, export_dir, data, tmp_path):
    p = plugin(calendar_env, export_dir)
    cursor = final_cursor(list(p.records({}, "backfill")))
    old = data.run_records(
        "20240201T000000Z-late0001",
        "2024-02-01T00:00:00+00:00",
        ["2024-01-01T00:00:00+00:00", "2024-03-01T00:00:00+00:00"],
        [],
        [],
        [],
        [],
    )
    data.write_jsonl(export_dir / "late.jsonl", old)
    assert messages(Calendar().records(cursor, "sync")) == []


def test_cal_rejects_bad_inputs(calendar_env, tmp_path):
    calendar_env.setenv("CALENDAR_SOURCE", "somewhere")
    with pytest.raises(ValueError, match="CALENDAR_SOURCE"):
        Calendar()
    broken = tmp_path / "broken.jsonl"
    broken.write_text(
        '{"kind": "run", "run": "x", "fetched_at": "2024-01-01T00:00:00Z"}\nnot json\n',
        encoding="utf-8",
    )
    calendar_env.setenv("CALENDAR_SOURCE", "export:" + str(broken))
    with pytest.raises(ValueError, match="line 2"):
        list(Calendar().records({}, "backfill"))
    calendar_env.setenv("CALENDAR_SOURCE", "export:" + str(tmp_path / "missing"))
    with pytest.raises(ValueError, match="does not exist"):
        list(Calendar().records({}, "backfill"))


def test_cal_manifest_declares_every_field(calendar_env, export_dir):
    manifest = json.loads(Calendar.manifest.model_dump_json())
    declared = {
        c["name"]: {f["name"] for f in c["fields"]} for c in manifest["channels"]
    }
    for message in messages(plugin(calendar_env, export_dir).records({}, "backfill")):
        assert set(message["fields"]) <= declared[message["channel"]]
    for declaration in list(manifest["settings"].values()) + list(
        manifest["secrets"].values()
    ):
        assert declaration["required"] is False
    assert all(d["default"] is None for d in manifest["secrets"].values())


def test_cal_event_moved_outside_the_window_is_not_removed(
    calendar_env, data, tmp_path
):
    window = ["2024-03-01T00:00:00+00:00", "2024-04-01T00:00:00+00:00"]
    later = ["2024-03-02T00:00:00+00:00", "2024-04-02T00:00:00+00:00"]
    moved = dict(
        data.CALL,
        start=data.when("2024-09-25T09:00:00-05:00"),
        end=data.when("2024-09-25T09:30:00-05:00"),
        updated="2024-03-01T20:00:00Z",
    )
    folder = tmp_path / "moved"
    folder.mkdir()
    data.write_jsonl(
        folder / "1.jsonl",
        data.run_records(
            "r1", "2024-03-01T12:00:00+00:00", window, [data.CALL], [], [], []
        ),
    )
    data.write_jsonl(
        folder / "2.jsonl",
        data.run_records("r2", "2024-03-02T12:00:00+00:00", later, [], [moved], [], []),
    )
    rows = by_id(plugin(calendar_env, folder).records({}, "backfill"))
    call = rows[data.CAL_A + ":inventedcall"]
    assert call["fields"]["status"] == "confirmed"
    assert call["happened_at"] == "2024-09-25T09:00:00-05:00"
    assert not [k for k in rows if ":removed" in k]
    change = rows[data.CAL_A + ":inventedcall:changed:2024-03-01T20:00:00+00:00"]
    assert change["fields"]["changed_fields"] == ["event_start", "event_end"]


def test_cal_exception_gets_no_second_created_record(calendar_env, tmp_path, data):
    moved = data.instance(
        "20240304",
        updated="2024-02-20T08:00:00Z",
        start={"dateTime": "2024-03-04T16:00:00Z"},
        end={"dateTime": "2024-03-04T16:15:00Z"},
    )
    window = ["2024-02-01T00:00:00+00:00", "2024-04-01T00:00:00+00:00"]
    rows = data.run_records(
        "20240301T120000Z-bbbb0001",
        "2024-03-01T12:00:00+00:00",
        window,
        [moved, data.instance("20240311")],
        [data.master(), moved],
        [],
        [],
    )
    data.write_jsonl(tmp_path / "one.jsonl", rows)
    out = by_id(plugin(calendar_env, tmp_path / "one.jsonl").records({}, "backfill"))
    a = data.CAL_A
    created = [k for k in out if k.endswith(":created")]
    assert created == [f"{a}:{data.SERIES}:created"]
    # The modified instance is still recorded as changed at its API time.
    assert f"{a}:{moved['id']}:changed:2024-02-20T08:00:00+00:00" in out


def test_cal_empty_window_removes_nothing(calendar_env, tmp_path, data):
    window = ["2024-02-01T00:00:00+00:00", "2024-04-01T00:00:00+00:00"]
    first = data.run_records(
        "20240301T120000Z-cccc0001",
        "2024-03-01T12:00:00+00:00",
        window,
        [data.DENTIST, data.LUNCH, data.CALL],
        [],
        [data.REVIEW],
        [],
    )
    # A complete pass that came back with nothing at all: likely a misread.
    empty = data.run_records(
        "20240302T120000Z-cccc0002",
        "2024-03-02T12:00:00+00:00",
        window,
        [],
        [],
        [data.REVIEW],
        [],
    )
    data.write_jsonl(tmp_path / "1.jsonl", first)
    data.write_jsonl(tmp_path / "2.jsonl", empty)
    live = plugin(calendar_env, tmp_path)
    out = by_id(live.records({}, "backfill"))
    assert not [k for k in out if ":removed" in k]
    assert {
        m["fields"]["status"] for m in out.values() if m["channel"] == "calendar.events"
    } == {"confirmed"}
    assert live.stats["skipped_empty_window"] == 1
    # A later pass that returns part of the calendar still judges removals.
    partial = data.run_records(
        "20240303T120000Z-cccc0003",
        "2024-03-03T12:00:00+00:00",
        window,
        [data.DENTIST, data.LUNCH],
        [],
        [data.REVIEW],
        [],
    )
    data.write_jsonl(tmp_path / "3.jsonl", partial)
    out = by_id(plugin(calendar_env, tmp_path).records({}, "backfill"))
    assert out[data.CAL_A + ":inventedcall"]["fields"]["status"] == "removed"
