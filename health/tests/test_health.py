"""The health plugin: mapping, export and live modes, limits and the SDK contract."""

import copy
import json
from datetime import date, datetime
from pathlib import Path

import pytest
from throughline_sdk import Checkpoint
from throughline_sdk.contract import check_plugin

from throughline_health import Health, emit_buckets, fetch, mapping, sources


def messages(items):
    return [i for i in items if isinstance(i, dict)]


def checkpoints(items):
    return [i for i in items if isinstance(i, Checkpoint)]


def by_id(rows):
    return {m["external_id"]: m for m in rows}


def configure(monkeypatch, env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Health()


@pytest.fixture
def export_file(tmp_path, fake_env):
    """A synthetic export made by fetch.py itself from the fake API."""
    out = tmp_path / "exports" / "health-1.jsonl"
    out.parent.mkdir()
    code = fetch.main(
        [
            "export",
            "--out",
            str(out),
            "--start",
            "2024-03-25",
            "--end",
            "2024-04-05",
            "--window-days",
            "3",
            "--min-interval",
            "0",
            "--backoff",
            "0",
        ],
        fake_env,
    )
    assert code == 0
    return out


@pytest.fixture
def exported(export_file):
    return mapping.map_records(sources.read_export(export_file))


def test_channels_and_declared_fields_cover_every_message(exported):
    declared = {
        c.name: {f["name"]: f["type"] for f in c.fields}
        for c in Health.manifest.channels
    }
    assert set(declared) == set(mapping.CHANNELS)
    assert {m["channel"] for m in exported} == set(mapping.CHANNELS)
    python = {
        "text": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "timestamp": str,
        "json": (list, dict),
    }
    for message in exported:
        for name, value in message["fields"].items():
            assert name in declared[message["channel"]], (message["channel"], name)
            assert isinstance(value, python[declared[message["channel"]][name]]), (
                name,
                value,
            )
            if declared[message["channel"]][name] == "integer":
                assert not isinstance(value, bool)
        assert message["fields"]["mapping_revision"] == mapping.MAPPING_REVISION
        assert message["content"].strip()
        datetime.fromisoformat(message["happened_at"])


def test_daily_summary_line_fields_and_subject_day(exported):
    rows = by_id(exported)
    day = rows["oura:daily:2024-03-30"]
    assert day["content"] == (
        "Oura 2024-03-30: sleep 71, readiness 66, activity 81, slept 7h 01m, 7,000 steps, "
        "high stress 15m, stress normal, SpO2 96.5%, temperature -0.20 C, resilience solid"
    )
    assert day["happened_at"] == "2024-03-30T00:00:00-05:00"
    # A day bound is 00:00 UTC of the day with day precision and no end, so the
    # core places the record on exactly that day whatever the local offset.
    assert day["subject"] == {
        "start": "2024-03-30T00:00:00+00:00",
        "start_precision": "day",
    }
    f = day["fields"]
    assert f["sleep_score"] == 71 and f["sleep_contrib_deep_sleep"] == 61
    assert (
        "readiness_contrib_sleep_regularity" not in f
    )  # absent in the source stays absent
    assert f["steps"] == 7000 and f["average_met_minutes"] == 1.4375
    assert (
        f["spo2_average"] == 96.5 and f["vascular_age"] == 41 and f["vo2_max"] == 42.5
    )
    assert f["main_sleep_total_s"] == 25260 and f["sleep_periods"] == 2
    assert f["main_sleep_start"] == "2024-03-29T23:10:00-05:00"
    assert f["daily_sources"] == sorted(day["payload"])
    assert "class_5_min" in day["payload"]["daily_activity"]
    assert day["thread"] == {
        "external_id": "health.oura.daily:2024-03",
        "title": "Oura daily 2024-03",
    }
    last = rows["oura:daily:2024-04-02"]
    assert "spo2_average" not in last["fields"] and "vo2_max" not in last["fields"]


def test_sleep_session_workout_heartrate_messages(exported):
    rows = by_id(exported)
    night = rows["oura:sleep:fake-sleep-0"]
    assert night["happened_at"] == "2024-03-28T23:10:00-05:00"
    assert night["ended_at"] == "2024-03-29T06:40:00-05:00"
    assert night["content"] == (
        "Slept 7h 00m (long sleep, 23:10-06:40): deep 1h 20m, REM 1h 45m, light 3h 55m, "
        "efficiency 88%, lowest HR 48, HRV 45 ms"
    )
    assert (
        night["fields"]["main_sleep"] is True
        and night["fields"]["readiness_score"] == 65
    )
    assert night["payload"]["movement_30_sec"] == "112233"
    nap = rows["oura:sleep:fake-nap-1"]
    assert nap["fields"]["main_sleep"] is False and nap["content"].startswith(
        "Slept 20m (late nap"
    )
    assert rows["oura:sleep:fake-deleted-sleep"]["fields"]["sleep_type"] == "deleted"
    session = rows["oura:session:fake-session-1"]
    assert session["content"] == "Oura session: breathing, 10m, mean HR 62, mood good"
    assert (
        session["fields"]["hrv_mean"] == 41.0 and session["fields"]["duration_s"] == 600
    )
    ride = rows["oura:workout:fake-workout-2"]
    assert (
        ride["content"]
        == "Workout: Invented ride, 1h 00m, 480 kcal, 15 km, hard intensity"
    )
    assert (
        rows["oura:workout:fake-workout-1"]["content"]
        == "Workout: walking, 45m, 210 kcal, 3.2 km, moderate intensity"
    )
    heart = rows["oura:heartrate:2024-03-30"]
    assert heart["fields"]["sample_count"] == 12
    assert heart["fields"]["bpm_min"] == 50 and heart["fields"]["bpm_max"] == 72
    assert heart["happened_at"] == "2024-03-30T00:00:00+00:00"
    assert heart["ended_at"] == "2024-03-30T22:00:00+00:00"
    assert heart["subject"] == {
        "start": "2024-03-30T00:00:00+00:00",
        "start_precision": "day",
    }
    assert heart["content"].startswith(
        "Heart rate 2024-03-30: 12 samples, 50-72 bpm, mean 61 (sleep"
    )
    assert len(heart["payload"]["items"]) == 12


def test_weighins_scaling_units_and_deletion(exported):
    rows = by_id(exported)
    first = rows["weightgurus:2024-03-29T13:00:00.000Z"]
    assert (
        first["content"]
        == "Weigh-in: 180.4 lb, body fat 24.3%, muscle 38.9%, water 55.4%, BMI 28.1"
    )
    assert first["fields"]["weight_kg"] == 81.83 and first["fields"]["deleted"] is False
    assert first["happened_at"] == "2024-03-29T13:00:00+00:00"
    deleted = rows["weightgurus:2024-03-30T13:00:00.000Z"]
    assert deleted["fields"]["deleted"] is True and deleted["fields"]["weight"] == 180.1
    assert deleted["fields"]["operation_count"] == 2
    assert deleted["content"].endswith("(deleted in Weight Gurus)")
    assert [o["operationType"] for o in deleted["payload"]["operations"]] == [
        "create",
        "delete",
    ]
    bare = rows["weightgurus:2024-04-01T13:00:00.000Z"]
    assert (
        "body_fat_pct" not in bare["fields"]
        and bare["content"] == "Weigh-in: 179.5 lb, BMI 28.1"
    )
    kg = mapping.map_records(
        [
            {
                "kind": "weightgurus.operation",
                "id": "x",
                "payload": {
                    "entryTimestamp": "2024-01-01T00:00:00Z",
                    "weight": 800,
                    "operationType": "create",
                },
            }
        ],
        unit="kg",
    )
    assert (
        kg[0]["fields"]["weight_kg"] == 80.0 and kg[0]["content"] == "Weigh-in: 80 kg"
    )


def test_mapping_is_deterministic_and_order_independent(export_file):
    raws = sources.read_export(export_file)
    once = mapping.map_records(raws)
    assert mapping.map_records(list(reversed(raws))) == once
    assert mapping.map_records(raws + copy.deepcopy(raws)) == once


def test_newest_fetch_of_a_day_wins_across_overlapping_exports(export_file, tmp_path):
    raws = sources.read_export(export_file)
    later = [
        {
            "kind": "oura.daily_sleep",
            "id": "fake-regenerated",
            "provider": "oura",
            "fetched_at": "2999-01-01T00:00:00Z",
            "payload": {
                "id": "fake-regenerated",
                "day": "2024-03-30",
                "score": 99,
                "timestamp": "2024-03-30T00:00:00-05:00",
            },
        },
    ]
    rows = by_id(mapping.map_records(raws + later))
    assert rows["oura:daily:2024-03-30"]["fields"]["sleep_score"] == 99
    assert rows["oura:daily:2024-03-31"]["fields"]["sleep_score"] == 72


def test_export_mode_reruns_send_nothing_and_changes_send_one_bucket(
    monkeypatch, export_file
):
    plugin = configure(monkeypatch, {"HEALTH_SOURCE": f"export:{export_file.parent}"})
    first = list(plugin.records({}, "backfill"))
    assert len(messages(first)) == len(
        mapping.map_records(sources.read_export(export_file))
    )
    cursor = checkpoints(first)[-1].cursor
    assert cursor["export"]["revision"] == mapping.MAPPING_REVISION
    again = list(plugin.records(cursor, "sync"))
    assert messages(again) == [] and checkpoints(again)[-1].cursor == cursor
    extra = export_file.parent / "health-2.jsonl"
    extra.write_text(
        json.dumps(
            {
                "kind": "oura.workout",
                "id": "fake-workout-3",
                "provider": "oura",
                "fetched_at": "2999-01-01T00:00:00Z",
                "payload": {
                    "id": "fake-workout-3",
                    "day": "2024-04-02",
                    "activity": "running",
                    "start_datetime": "2024-04-02T07:00:00-05:00",
                    "end_datetime": "2024-04-02T07:30:00-05:00",
                },
            }
        )
        + "\n"
    )
    changed = messages(plugin.records(cursor, "sync"))
    assert {m["external_id"] for m in changed} == {
        "oura:workout:fake-workout-2",
        "oura:workout:fake-workout-3",
    }
    # A crash after the first bucket resumes with the remaining ones only.
    items = list(plugin.records({}, "backfill"))
    first_checkpoint = next(
        i for i, item in enumerate(items) if isinstance(item, Checkpoint)
    )
    resumed = messages(plugin.records(items[first_checkpoint].cursor, "backfill"))
    assert len(resumed) == len(messages(items)) - len(
        messages(items[:first_checkpoint])
    )


def test_export_reader_errors_name_the_line_not_its_content(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text(
        '{"kind": "oura.sleep", "id": "a", "payload": {}}\nnot-json fake-secret-line\n'
    )
    with pytest.raises(ValueError) as error:
        sources.read_export(bad)
    assert str(error.value) == "bad.jsonl:2 is not JSON"
    newer = tmp_path / "newer.jsonl"
    newer.write_text(
        json.dumps({"kind": "export.end", "id": "x", "payload": {"format": 99}}) + "\n"
    )
    with pytest.raises(ValueError, match="format 99"):
        sources.read_export(newer)
    with pytest.raises(ValueError, match="does not exist"):
        sources.read_export(tmp_path / "missing")


def test_live_equals_export(monkeypatch, live_env, exported):
    plugin = configure(monkeypatch, live_env)
    live = messages(plugin.records({}, "backfill"))
    assert sorted(live, key=lambda m: m["external_id"]) == sorted(
        exported, key=lambda m: m["external_id"]
    )


def test_live_backfill_bounds_cursor_and_sync_overlap(monkeypatch, live_env, server):
    plugin = configure(monkeypatch, dict(live_env, HEALTH_SYNC_OVERLAP_DAYS="2"))
    items = list(plugin.live({}, "backfill", today=date(2024, 4, 10)))
    cursor = checkpoints(items)[-1].cursor
    assert cursor["backfill"] == {
        "bounds": ["2024-03-25", "2024-04-05"],
        "next": "2024-04-06",
    }
    # A bounded backfill that ends before today does not move the sync cursor.
    assert "oura_next" not in cursor
    assert cursor["requests"]["day"] == "2024-04-10" and cursor["requests"][
        "used"
    ] == len(server.requests)
    assert len(checkpoints(items)) >= 4
    server.requests.clear()
    synced = list(
        plugin.live(
            dict(cursor, oura_next="2024-04-02"), "sync", today=date(2024, 4, 3)
        )
    )
    starts = [
        r["query"]["start_date"]
        for r in server.requests
        if r["path"].endswith("daily_sleep") and "next_token" not in r["query"]
    ]
    assert starts == ["2024-03-31"]  # oura_next minus two overlap days
    assert not server.paths("/v3")  # Weight Gurus is not due yet
    assert checkpoints(synced)[-1].cursor["oura_next"] == "2024-04-04"
    assert {m["external_id"] for m in messages(synced)} >= {
        "oura:daily:2024-04-02",
        "oura:sleep:fake-sleep-4",
    }


def test_live_sync_on_empty_cursor_starts_at_backfill_start(
    monkeypatch, live_env, server
):
    plugin = configure(monkeypatch, live_env)
    items = list(plugin.live({}, "sync", today=date(2024, 4, 3)))
    starts = [
        r["query"]["start_date"]
        for r in server.requests
        if r["path"].endswith("daily_sleep")
    ]
    assert starts[0] == "2024-03-25"
    assert checkpoints(items)[-1].cursor["oura_next"] == "2024-04-04"
    assert checkpoints(items)[-1].cursor["weightgurus_at"] > 0


def test_live_request_limits_stop_at_a_complete_window(
    monkeypatch, live_env, server, caplog
):
    plugin = configure(
        monkeypatch,
        dict(live_env, HEALTH_MAX_REQUESTS_PER_RUN="20", HEALTH_PROVIDERS="oura"),
    )
    items = list(plugin.live({}, "backfill", today=date(2024, 4, 10)))
    cursor = checkpoints(items)[-1].cursor
    assert len(server.requests) == 20
    assert "request limit reached" in caplog.text
    done = cursor["backfill"]["next"]
    assert done < "2024-04-06"
    days = {
        m["fields"]["day"] for m in messages(items) if m["channel"] == mapping.DAILY
    }
    assert all(d < done for d in days)
    # The per-day allowance is spent: the next run today sends nothing.
    server.requests.clear()
    plugin = configure(
        monkeypatch,
        dict(live_env, HEALTH_MAX_REQUESTS_PER_DAY="20", HEALTH_PROVIDERS="oura"),
    )
    again = list(plugin.live(cursor, "backfill", today=date(2024, 4, 10)))
    assert not server.requests and not messages(again)
    tomorrow = list(plugin.live(cursor, "backfill", today=date(2024, 4, 11)))
    assert server.requests and messages(tomorrow)


def test_live_401_without_opt_in_fails_without_refreshing(
    monkeypatch, live_env, server
):
    env = dict(
        live_env,
        OURA_ACCESS_TOKEN="fake-stale-canary",
        OURA_REFRESH_TOKEN="fake-refresh-canary",
        OURA_CLIENT_ID="fake-client",
        OURA_CLIENT_SECRET="fake-client-secret",
        HEALTH_PROVIDERS="oura",
    )
    plugin = configure(monkeypatch, env)
    with pytest.raises(fetch.AuthError) as error:
        list(plugin.records({}, "backfill"))
    assert "fake-stale-canary" not in str(error.value)
    assert not server.paths("/oauth")


def test_settings_are_validated(monkeypatch):
    for env, message in (
        ({"HEALTH_SOURCE": "elsewhere"}, "HEALTH_SOURCE"),
        ({"HEALTH_OURA_COLLECTIONS": "daily_sleep,bogus"}, "bogus"),
        ({"WEIGHTGURUS_UNIT": "stone"}, "WEIGHTGURUS_UNIT"),
        ({"HEALTH_WINDOW_DAYS": "0"}, "HEALTH_WINDOW_DAYS"),
    ):
        with monkeypatch.context() as m:
            for key, value in env.items():
                m.setenv(key, value)
            with pytest.raises((ValueError, fetch.FetchError), match=message):
                Health()
    plugin = Health()
    with pytest.raises(ValueError, match="no health source configured"):
        list(plugin.records({}, "sync"))


def test_emit_buckets_resets_on_mapping_revision_change():
    rows = [
        {
            "channel": "health.weight",
            "happened_at": "2024-01-02T00:00:00+00:00",
            "external_id": "a",
        }
    ]
    state = {
        "export": {
            "revision": mapping.MAPPING_REVISION - 1,
            "buckets": {"health.weight|2024-01": mapping.digest(rows)},
        }
    }
    assert messages(emit_buckets(rows, state, "export")) == rows
    assert messages(emit_buckets(rows, state, "export")) == []


def test_contract_export_mode(export_file):
    result = check_plugin(
        "throughline_health:Health",
        {
            "HEALTH_SOURCE": f"export:{export_file.parent}",
            "OURA_ACCESS_TOKEN": "fake-contract-canary-oura",
            "WEIGHTGURUS_PASSWORD": "fake-contract-canary-weightgurus",
        },
        secrets=["fake-contract-canary-oura", "fake-contract-canary-weightgurus"],
    )
    assert result["messages"] == len(
        mapping.map_records(sources.read_export(export_file))
    )


def test_contract_live_mode(live_env):
    env = dict(live_env, HEALTH_WINDOW_DAYS="400")
    env.pop("HEALTH_BACKFILL_END")
    result = check_plugin(
        "throughline_health:Health",
        env,
        secrets=[env["OURA_ACCESS_TOKEN"], env["WEIGHTGURUS_PASSWORD"]],
    )
    assert result["messages"] > 20


def test_readme_documents_every_environment_variable():
    readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
    declarations = {**Health.manifest.settings, **Health.manifest.secrets}
    for declaration in declarations.values():
        assert f"`{declaration.env}`" in readme, declaration.env


def test_daily_subject_is_the_day_even_east_of_utc():
    raws = [
        {
            "kind": "oura.daily_readiness",
            "id": "fake-east",
            "fetched_at": "2024-01-01T00:00:00Z",
            "payload": {
                "id": "fake-east",
                "day": "2024-01-01",
                "score": 80,
                "timestamp": "2024-01-01T00:00:00+09:00",
            },
        }
    ]
    (day,) = mapping.map_records(raws)
    assert day["happened_at"] == "2024-01-01T00:00:00+09:00"
    assert day["subject"] == {
        "start": "2024-01-01T00:00:00+00:00",
        "start_precision": "day",
    }


def test_live_weightgurus_failure_does_not_block_oura(monkeypatch, live_env, server):
    plugin = configure(monkeypatch, dict(live_env, WEIGHTGURUS_PASSWORD="fake-wrong"))
    items = []
    with pytest.raises(fetch.AuthError, match="Weight Gurus login failed") as error:
        for item in plugin.live({}, "sync", today=date(2024, 4, 3)):
            items.append(item)
    assert "fake-wrong" not in str(error.value)
    sent = messages(items)
    assert sent and {m["channel"] for m in sent} <= set(mapping.CHANNELS) - {
        mapping.WEIGHT
    }
    cursor = checkpoints(items)[-1].cursor
    assert cursor["oura_next"] == "2024-04-04" and "weightgurus_at" not in cursor


def test_live_refused_collection_reports_its_missed_window(
    monkeypatch, live_env, server
):
    from throughline_sdk import Record

    server.failures["/v2/usercollection/daily_spo2"] = [403]
    plugin = configure(monkeypatch, dict(live_env, HEALTH_PROVIDERS="oura"))
    items = list(plugin.live({}, "backfill", today=date(2024, 4, 10)))
    reports = {
        r.data["scope_key"]: r.data
        for r in items
        if isinstance(r, Record) and r.kind == "coverage"
    }
    # The shared window moved on without daily_spo2: its days are a gap.
    assert checkpoints(items)[-1].cursor["backfill"]["next"] == "2024-04-06"
    assert reports["daily_spo2"] == {
        "channel": mapping.DAILY,
        "scope_key": "daily_spo2",
        "status": "partial",
        "reasons": ["source_error"],
        "gaps": [
            {
                "from": "2024-03-25T00:00:00+00:00",
                "to": "2024-04-06T00:00:00+00:00",
                "reason": "source_error",
            }
        ],
        "detail": {"skipped": "Oura refused the collection (HTTP 403/404)"},
    }
    assert "daily_sleep" not in reports
