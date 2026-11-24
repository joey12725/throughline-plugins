import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from throughline_sdk import Checkpoint
from throughline_sdk.contract import check_plugin

from throughline_qbittorrent import QBittorrent, map_record
from throughline_qbittorrent import fetch

RECORDED = Path(__file__).parent / "recorded" / "events.jsonl"
NOW = datetime(2020, 1, 5, tzinfo=timezone.utc)
HASH_A = "a" * 40
HASH_B = "b" * 40


class FakeApi:
    def __init__(self, info=None, sync=None):
        self.info = info or []
        self.sync = list(sync or [])
        self.calls = []

    def get(self, path, query=None):
        self.calls.append((path, query))
        if path == "app/version":
            return "v5.1.2"
        if path == "torrents/info":
            return self.info
        if path == "sync/maindata":
            return self.sync.pop(0)
        raise AssertionError(path)


def torrent(hash_value=HASH_A, **overrides):
    row = {
        "hash": hash_value,
        "name": "Invented Torrent",
        "total_size": 123,
        "category": "tv-sonarr",
        "tags": "one, two",
        "save_path": "/tv-direct/Invented",
        "tracker": "https://user:pass@tracker.invalid/announce/private-passkey",
        "added_on": 1577836800,
        "completion_on": 0,
        "ratio": 0.5,
        "state": "downloading",
    }
    row.update(overrides)
    return row


def config():
    return fetch.Config(
        {
            "QBITTORRENT_URL": "https://invented.invalid:8080",
            "QBITTORRENT_USERNAME": "fake-user-canary",
            "QBITTORRENT_PASSWORD": "fake-password-canary",
        }
    )


def messages(records):
    return [record for record in records if isinstance(record, dict)]


def test_first_snapshot_emits_full_available_history_and_sanitizes():
    api = FakeApi(
        info=[torrent(completion_on=1577923200, state="uploading", ratio=2.0)],
        sync=[{"rid": 3, "full_update": True, "torrents": {}}],
    )
    worker = fetch.Fetcher(config(), api=api, now=NOW)
    records = worker.run()
    assert [row["raw"]["event_type"] for row in records] == ["added", "completed"]
    assert api.calls == [
        ("app/version", None),
        ("sync/maindata", {"rid": 0}),
        ("torrents/info", None),
    ]
    serialized = json.dumps([records, worker.state])
    assert "private-passkey" not in serialized
    assert "fake-password-canary" not in serialized
    assert records[0]["raw"]["tracker_host"] == "tracker.invalid"
    assert records[0]["raw"]["media_kind"] == "tv"


def test_incremental_changes_completion_removal_and_readd_generation():
    first = fetch.Fetcher(
        config(),
        api=FakeApi(info=[torrent()], sync=[{"rid": 1}]),
        now=NOW,
    )
    first.run()
    changed = fetch.Fetcher(
        config(),
        state=first.state,
        api=FakeApi(
            sync=[
                {
                    "rid": 2,
                    "torrents": {
                        HASH_A: {
                            "category": "movies",
                            "tags": "two, three",
                            "completion_on": 1578096000,
                            "state": "uploading",
                        }
                    },
                }
            ]
        ),
        now=NOW,
    )
    rows = changed.run()
    assert [x["raw"]["event_type"] for x in rows] == [
        "category_changed",
        "tags_changed",
        "completed",
    ]
    assert rows[0]["id"].endswith("category_changed:1")
    removed = fetch.Fetcher(
        config(),
        state=changed.state,
        api=FakeApi(sync=[{"rid": 3, "torrents_removed": [HASH_A]}]),
        now=NOW,
    )
    assert removed.run()[0]["raw"]["event_type"] == "removed"
    readded = fetch.Fetcher(
        config(),
        state=removed.state,
        api=FakeApi(sync=[{"rid": 4, "torrents": {HASH_A: torrent()}}]),
        now=NOW,
    )
    assert ":g2:added" in readded.run()[0]["id"]


def test_full_update_detects_missing_torrents():
    first = fetch.Fetcher(
        config(),
        api=FakeApi(info=[torrent(), torrent(HASH_B)], sync=[{"rid": 1}]),
        now=NOW,
    )
    first.run()
    worker = fetch.Fetcher(
        config(),
        state=first.state,
        api=FakeApi(sync=[{"rid": 2, "full_update": True, "torrents": {HASH_A: {}}}]),
        now=NOW,
    )
    rows = worker.run()
    assert len(rows) == 1 and rows[0]["raw"]["hash"] == HASH_B
    assert rows[0]["raw"]["event_type"] == "removed"


def test_mapping_fields_and_times():
    raw = json.loads(RECORDED.read_text().splitlines()[1])
    message = map_record(raw)
    assert message["channel"] == "downloads.qbittorrent"
    assert message["fields"]["added_on"] == "2020-01-01T00:00:00+00:00"
    assert message["fields"]["completion_on"] is None
    assert message["fields"]["tags"] == ["invented"]
    assert message["payload"]["tracker_host"] == "tracker.invalid"


def test_export_round_trip_and_incremental_state(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    out = tmp_path / "events.jsonl.gz"
    api = FakeApi(info=[torrent()], sync=[{"rid": 1}])
    monkeypatch.setattr(fetch, "Api", lambda _config: api)
    footer = fetch.export(config(), str(out), str(state), now=NOW)
    assert footer["counts"] == {"added": 1}
    rows = [json.loads(line) for line in gzip.decompress(out.read_bytes()).splitlines()]
    assert rows[0]["format"] == fetch.FORMAT and rows[-1]["complete"]
    second_api = FakeApi(sync=[{"rid": 1}])
    monkeypatch.setattr(fetch, "Api", lambda _config: second_api)
    footer = fetch.export(config(), str(out), str(state), now=NOW)
    assert footer["counts"] == {}
    assert not out.with_name(out.name + ".partial").exists()


def test_export_plugin_resume(monkeypatch):
    monkeypatch.setenv("QBITTORRENT_SOURCE", f"export:{RECORDED}")
    monkeypatch.setenv("QBITTORRENT_CHECKPOINT_EVERY", "1")
    plugin = QBittorrent()
    records = list(plugin.records({}, "backfill"))
    assert len(messages(records)) == 4
    checkpoints = [record for record in records if isinstance(record, Checkpoint)]
    assert checkpoints
    resumed = messages(plugin.records(checkpoints[1].cursor, "sync"))
    assert len(resumed) == 2
    assert messages(plugin.records(checkpoints[-1].cursor, "sync")) == []


def test_contract():
    assert check_plugin(
        "throughline_qbittorrent:QBittorrent",
        {
            "QBITTORRENT_SOURCE": f"export:{RECORDED}",
            "QBITTORRENT_CHECKPOINT_EVERY": "1",
        },
    )


def test_bad_hash_and_url_credentials_are_rejected():
    with pytest.raises(fetch.FetchError, match="invalid torrent hash"):
        fetch.snapshot(torrent(hash="not-a-hash"))
    with pytest.raises(fetch.FetchError, match="must not contain credentials"):
        fetch.Config(
            {
                "QBITTORRENT_URL": "http://user:pass@invented.invalid",
                "QBITTORRENT_USERNAME": "u",
                "QBITTORRENT_PASSWORD": "p",
            }
        )
