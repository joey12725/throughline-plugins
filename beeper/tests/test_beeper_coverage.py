"""Coverage reports, message aliases and the single identity system.

Every source row is invented; core-backed tests run a disposable core.
"""

import contextlib
import copy
import json
from pathlib import Path

import pytest
from throughline_sdk import Client, Record
from throughline_sdk._server import core_server

from throughline_beeper import Beeper
from throughline_beeper.mapping import live_id
from throughline_beeper.sources import Archive

RECORDINGS = Path(__file__).parent / "recorded"
LIVE_INSTAGRAM = live_id(
    "fake-account-instagram", "fake-chat-instagram", "fake-message-0"
)


def recording(tmp_path, source, change=None, name=None):
    data = json.loads((RECORDINGS / (source + ".json")).read_text())
    if change:
        change(data)
    path = tmp_path / (name or f"invented-{source}.json")
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def beeper(monkeypatch, source, path, **env):
    monkeypatch.setenv("BEEPER_SOURCE", source)
    monkeypatch.setenv("BEEPER_BATCH_SIZE", "4")
    monkeypatch.setenv("BEEPER_RECORDING", str(path))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Beeper()


def reports(records):
    return {
        (r.data["channel"], r.data["scope_key"]): r.data
        for r in records
        if isinstance(r, Record) and r.kind == "coverage"
    }


class Recorder:
    """A stand-in core that records every call (no network)."""

    def __init__(self, features=("coverage",)):
        self.features = list(features)
        self.calls, self.messages, self.reports = [], [], []
        self.saved = {}

    def request(self, method, path, data=None, **kwargs):
        self.calls.append((method, path))
        if (method, path) == ("GET", "describe"):
            return {"subjects": [{"subject": f} for f in self.features]}
        raise AssertionError("unexpected request " + path)

    def cursor(self, key="default"):
        self.calls.append(("GET", "cursor"))
        return copy.deepcopy(self.saved)

    def register(self, manifest):
        self.calls.append(("POST", "manifest"))

    def ingest(self, messages, *, links=None, dry_run=False):
        self.calls.append(("POST", "ingest/messages"))
        self.messages.extend(messages)
        return [{"rejected": []}]

    def checkpoint(self, cursor, key="default"):
        self.saved = copy.deepcopy(cursor)

    def heartbeat(self, **data):
        self.calls.append(("POST", "heartbeat"))

    def coverage(self, items):
        self.calls.append(("POST", "coverage"))
        self.reports.extend(items)
        return {"stored": len(items), "gaps_opened": [], "gaps_closed": []}

    def coverage_attempt(self, channels=None):
        self.calls.append(("POST", "coverage/attempt"))

    def coverage_gaps(self, channel=None, scope_key=None):
        self.calls.append(("GET", "coverage/gaps"))
        return []


@contextlib.contextmanager
def beeper_core():
    with core_server() as (_http, env):
        client = Client(env["THROUGHLINE_URL"], env["THROUGHLINE_TOKEN"], "beeper")
        try:
            yield client
        finally:
            client.close()


def sql_rows(client, statement):
    return client.request("POST", "sql", {"statement": statement})["rows"]


def coverage_rows(client, channel):
    return {
        r["scope_key"]: r
        for r in sql_rows(
            client,
            "SELECT scope_key,status,reasons,raw_status,raw_detail,pending_retries,rows_delivered"
            f" FROM api.coverage WHERE channel='{channel}' AND reported",
        )
    }


def aliases_supported(client):
    describe = client.request("GET", "describe")
    names = {s.get("subject") for s in describe.get("subjects", [])}
    return "message_aliases" in names | set(describe.get("functions", []))


# --- coverage -------------------------------------------------------------------


def test_coverage_instagram_cap_partial_source_cap(monkeypatch, tmp_path):
    path = recording(tmp_path, "live")
    plugin = beeper(monkeypatch, "live", path, BEEPER_LIVE_HISTORY_CAPS="instagram=6")
    found = reports(plugin.records({}, "backfill"))
    capped = found[("chat.instagram", "beeper:fake-chat-instagram")]
    assert capped["status"] == "partial"
    assert capped["reasons"] == ["source_cap"]
    assert capped["detail"]["cap"] == 6 and capped["detail"]["history_rows"] == 6
    assert "gaps" not in capped and "window" not in capped
    assert found[("chat.discord", "beeper:fake-chat-discord")]["status"] == "complete"
    assert found[("chat.sms", "beeper:fake-chat-sms")]["rows_delivered"] == 6
    # Below the cap (the default 1,000) the same chat is complete.
    plugin = beeper(
        monkeypatch, "live", path, BEEPER_LIVE_HISTORY_CAPS="instagram=1000"
    )
    found = reports(plugin.records({}, "backfill"))
    assert (
        found[("chat.instagram", "beeper:fake-chat-instagram")]["status"] == "complete"
    )
    # The cap persists: later incremental runs keep reporting it.
    plugin = beeper(monkeypatch, "live", path, BEEPER_LIVE_HISTORY_CAPS="instagram=6")
    last = [
        r for r in plugin.records({}, "backfill") if not isinstance(r, (dict, Record))
    ][-1]
    again = reports(plugin.records(last.cursor, "sync"))
    capped = again[("chat.instagram", "beeper:fake-chat-instagram")]
    assert capped["status"] == "partial" and capped["rows_delivered"] == 0


def failing_sms(data):
    for response in data["responses"]:
        if response["path"] == "v1/chats/fake-chat-sms/messages" and response.get(
            "params"
        ) == {"cursor": "fake-page-1", "direction": "before"}:
            response["error"] = 503


def test_coverage_sms_failure_gap_and_retry_closes(monkeypatch, tmp_path):
    broken = recording(tmp_path, "live", failing_sms, "broken.json")
    fixed = recording(tmp_path, "live", None, "fixed.json")
    with beeper_core() as client:
        first = beeper(monkeypatch, "live", broken).execute(client, "backfill")
        # Other chats were not held back by the failing one.
        assert first["counts"]["chat.discord"] == 6
        assert first["counts"]["chat.sms"] == 2
        (gap_id,) = first["coverage"]["gaps_opened"]
        (gap,) = client.coverage_gaps("chat.sms")
        assert gap["id"] == gap_id
        assert gap["scope_key"] == "beeper:fake-chat-sms"
        assert gap["reason"] == "source_error"
        sms = coverage_rows(client, "chat.sms")["beeper:fake-chat-sms"]
        assert sms["status"] == "partial"
        assert set(sms["reasons"]) >= {"source_error", "retry_pending"}
        assert sms["pending_retries"] == 1
        # Next run: the plugin reads the open gap back and closes it by id
        # once the window loads.
        second = beeper(monkeypatch, "live", fixed).execute(client, "sync")
        assert second["counts"]["chat.sms"] == 4
        assert second["coverage"]["gaps_closed"] == 1
        assert client.coverage_gaps("chat.sms") == []
        sms = coverage_rows(client, "chat.sms")["beeper:fake-chat-sms"]
        assert sms["status"] == "complete"
        assert sql_rows(
            client, "SELECT count(*) AS n FROM api.messages WHERE channel='chat.sms'"
        ) == [{"n": 6}]


def test_coverage_best_effort_without_listing_opens_no_gap(monkeypatch, tmp_path):
    path = recording(tmp_path, "live")
    with beeper_core() as client:
        result = beeper(monkeypatch, "live", path).execute(client, "backfill")
        assert result["coverage"]["gaps_opened"] == []
        snapchat = coverage_rows(client, "chat.snapchat")["beeper:fake-chat-snapchat"]
        assert snapchat["status"] == "best_effort"
        assert snapchat["raw_detail"]["history_listing"] is False
        assert client.coverage_gaps("chat.snapchat") == []
        # Each run is reported, also when nothing changed.
        beeper(monkeypatch, "live", path).execute(client, "sync")
        discord = coverage_rows(client, "chat.discord")["beeper:fake-chat-discord"]
        assert discord["status"] == "complete" and discord["rows_delivered"] == 0
        assert client.coverage_gaps() == []


def test_unknown_status_sent_and_degraded_not_fatal(monkeypatch, tmp_path):
    def odd(data):
        conversation = data["conversations"][0]
        conversation["metadata_json"] = json.dumps(
            {"history_status": "invented_status", "history_detail": [1, 2]}
        )
        for conversation in data["conversations"][1:]:
            conversation["kind"] = "invented-kind"

    path = recording(tmp_path, "archive", odd)
    with beeper_core() as client:
        result = beeper(monkeypatch, "archive", path).execute(client, "backfill")
        assert sum(result["counts"].values()) == 30
        assert result["coverage"]["refused"] == 0
        discord = coverage_rows(client, "chat.discord")["fake-chat-discord"]
        assert discord["raw_status"] == "invented_status"
        assert discord["status"] == "partial"
        assert "unrecognised_status" in discord["reasons"]
        assert discord["raw_detail"]["source_detail"] == [1, 2]
        # An unknown conversation kind is detail, not a failure.
        instagram = coverage_rows(client, "chat.instagram")["fake-chat-instagram"]
        assert instagram["status"] == "complete"
        assert instagram["raw_detail"]["conversation_kind"] == "unknown"
        assert instagram["raw_detail"]["unknown_kind_rows"] == 6


def test_archive_quarantined_rows_partial(monkeypatch, tmp_path):
    def broken(data):
        # An unsafe row (a NUL in the body) is quarantined; undated rows are
        # not, they take their time from a conversation neighbour.
        row = next(
            m for m in data["messages"] if m["conversation_id"] == "fake-chat-discord"
        )
        row["body"] = "Invented\u0000broken"

    plugin = beeper(monkeypatch, "archive", recording(tmp_path, "archive", broken))
    found = reports(plugin.records({}, "backfill"))
    chat = found[("chat.discord", "fake-chat-discord")]
    assert chat["status"] == "partial"
    assert chat["reasons"] == ["quarantined_rows"]
    assert chat["detail"]["quarantined"] == 1
    assert "window" not in chat
    assert found[("chat.sms", "fake-chat-sms")]["status"] == "complete"


# --- aliases --------------------------------------------------------------------


def with_aliases(data):
    """One invented archive message identified by fingerprint, whose live id
    the archive learned later, plus a native alias and an ambiguous one."""
    row = copy.deepcopy(
        next(
            m for m in data["messages"] if m["conversation_id"] == "fake-chat-instagram"
        )
    )
    row.update(
        id="instagram:fake-fp-0",
        identity_quality="fingerprint",
        body="Invented fingerprint orchard",
        sent_at="2020-01-06T12:00:00+02:00",
    )
    data["messages"].append(row)
    data["message_aliases"] = [
        {"platform": "instagram", "native_id": LIVE_INSTAGRAM, "message_id": row["id"]},
        {
            "platform": "instagram",
            "native_id": "fake-native-0",
            "message_id": row["id"],
        },
        # Ambiguous and cross-network aliases are never sent.
        {
            "platform": "discord",
            "native_id": "fake-twice",
            "message_id": "discord:fake-1",
        },
        {
            "platform": "discord",
            "native_id": "fake-twice",
            "message_id": "discord:fake-2",
        },
        {"platform": "sms", "native_id": "fake-cross", "message_id": "discord:fake-3"},
    ]


def test_archive_alias_map_unambiguous_same_network(tmp_path):
    source = Archive(recording=recording(tmp_path, "archive", with_aliases))
    assert source.message_aliases() == {
        "instagram:fake-fp-0": [LIVE_INSTAGRAM, "instagram:fake-native-0"]
    }


def test_aliases_skipped_when_core_lacks_feature(monkeypatch, tmp_path):
    path = recording(tmp_path, "archive", with_aliases)
    older = Recorder(features=("coverage",))
    beeper(monkeypatch, "archive", path).execute(older, "backfill")
    assert len(older.messages) == 31
    assert not any("aliases" in m for m in older.messages)
    newer = Recorder(features=("coverage", "message_aliases"))
    result = beeper(monkeypatch, "archive", path).execute(newer, "backfill")
    aliased = [m for m in newer.messages if "aliases" in m]
    assert [(m["external_id"], m["aliases"]) for m in aliased] == [
        ("instagram:fake-fp-0", [LIVE_INSTAGRAM, "instagram:fake-native-0"])
    ]
    assert result["aliased"] == 1


def test_aliases_outside_payload_hash_msg_2(monkeypatch, tmp_path):
    path = recording(tmp_path, "archive", with_aliases)
    plain = beeper(monkeypatch, "archive", path)
    with_feature = beeper(monkeypatch, "archive", path)
    with_feature.aliases_on = True
    a = [r for r in plain.records({}, "backfill") if isinstance(r, dict)]
    b = [r for r in with_feature.records({}, "backfill") if isinstance(r, dict)]
    assert len(a) == len(b) == 31
    for left, right in zip(a, b):
        right = dict(right)
        right.pop("aliases", None)
        # Payload, fields and content (everything the core hashes) are equal.
        assert left == right
    fp = next(r for r in b if r["external_id"] == "instagram:fake-fp-0")
    assert fp["fields"]["identity_quality"] == "fingerprint"
    assert "aliases" not in fp["payload"] and "aliases" not in fp["fields"]
    # Live messages carry the identity quality the core ranks.
    live = beeper(monkeypatch, "live", RECORDINGS / "live.json")
    assert {
        r["fields"]["identity_quality"]
        for r in live.records({}, "backfill")
        if isinstance(r, dict)
    } == {"beeper_id"}


def test_no_second_identity_system(monkeypatch, tmp_path):
    def crosswalk(data):
        data["people"] = [{"id": "fake-person", "display_name": "Invented Person"}]
        data["person_identity_links"] = [
            {"person_id": "fake-person", "identity_id": "fake-identity-discord"}
        ]

    path = recording(tmp_path, "archive", crosswalk)
    read = []
    original = Archive.table

    def table(self, name):
        read.append(name)
        return original(self, name)

    monkeypatch.setattr(Archive, "table", table)
    core = Recorder(features=("coverage", "message_aliases"))
    beeper(monkeypatch, "archive", path).execute(core, "backfill")
    # Identity reaches the core only as message authors (ingest identifiers):
    # no identity or registry route, no people crosswalk read.
    assert not any(
        p.startswith(("identity", "registry", "people")) for _, p in core.calls
    )
    assert not {"people", "person_identity_links"} & set(read)
    authors = {
        (m["author"]["system"], m["author"]["kind"])
        for m in core.messages
        if m["author"]
    }
    assert authors == {(n, "user_id") for n in ("discord", "instagram", "reddit")} | {
        ("sms", "phone"),
        ("snapchat", "user_id"),
    }


# --- aliases against the core (wave-integration with the write contract) --------


def alias_world(monkeypatch, tmp_path):
    archive = recording(tmp_path, "archive", with_aliases)
    return (
        lambda: beeper(monkeypatch, "archive", archive),
        lambda: beeper(monkeypatch, "live", RECORDINGS / "live.json"),
    )


def versions(client, external_id):
    return sql_rows(
        client,
        "SELECT count(*) AS n FROM api.message_versions WHERE channel='chat.instagram'"
        f" AND external_id='{external_id}'",
    )[0]["n"]


def current_count(client):
    return sql_rows(
        client,
        "SELECT count(*) AS n FROM api.messages WHERE channel='chat.instagram'",
    )[0]["n"]


@pytest.fixture
def aliasing_core():
    with beeper_core() as client:
        if not aliases_supported(client):
            pytest.skip("core does not advertise message_aliases (w5-write-contract)")
        yield client


def test_p8_aliases_upgrade_fingerprint_no_duplicate(
    monkeypatch, tmp_path, aliasing_core
):
    client = aliasing_core
    archive, live = alias_world(monkeypatch, tmp_path)
    archive().execute(client, "backfill")
    before = current_count(client)
    assert versions(client, "instagram:fake-fp-0") == 1
    live().execute(client, "backfill")
    # The live delivery of the fingerprinted message resolved to its record:
    # one record for 6 live messages plus 7 archive ones, not two.
    assert current_count(client) == before + 5
    assert versions(client, LIVE_INSTAGRAM) == 0
    # The live id ranks above a fingerprint: at most one upgrade version.
    assert versions(client, "instagram:fake-fp-0") <= 2


def test_live_delivery_of_archived_message_resolves_to_archive_record(
    monkeypatch, tmp_path, aliasing_core
):
    client = aliasing_core
    archive, live = alias_world(monkeypatch, tmp_path)
    archive().execute(client, "backfill")
    (record,) = sql_rows(
        client,
        "SELECT id,external_id FROM api.messages WHERE external_id='instagram:fake-fp-0'",
    )
    live().execute(client, "backfill")
    (after,) = sql_rows(
        client,
        "SELECT external_id FROM api.messages WHERE channel='chat.instagram' AND thread_external_id='fake-chat-instagram' AND content LIKE 'Invented%orchard 0%'",
    ) or [None]
    assert after is None or after["external_id"] == "instagram:fake-fp-0"
    assert not sql_rows(
        client, f"SELECT id FROM api.messages WHERE external_id='{LIVE_INSTAGRAM}'"
    )
    assert sql_rows(
        client,
        "SELECT external_id FROM api.message_versions WHERE external_id='instagram:fake-fp-0' ORDER BY version DESC LIMIT 1",
    ) == [{"external_id": record["external_id"]}]


def test_msg_2_alternating_archive_and_live_runs_zero_new_versions(
    monkeypatch, tmp_path, aliasing_core
):
    client = aliasing_core
    archive, live = alias_world(monkeypatch, tmp_path)
    archive().execute(client, "backfill")
    live().execute(client, "backfill")

    def total():
        return sql_rows(
            client,
            "SELECT count(*) AS n FROM api.message_versions WHERE channel LIKE 'chat.%'",
        )[0]["n"]

    settled = total()
    for _ in range(2):
        archive().execute(client, "backfill")
        client.checkpoint({})  # the live history is read again in full
        live().execute(client, "backfill")
    assert total() == settled


def test_unfinished_history_keeps_open_gaps(monkeypatch, tmp_path):
    plugin = beeper(monkeypatch, "live", recording(tmp_path, "live"))
    chat = "beeper:fake-chat-sms"
    plugin.open_gaps = {("chat.sms", chat): [{"id": 7}]}
    # The source stopped paging without an error: the older window has not
    # loaded, so the gap stays open.
    report = plugin.live_coverage("sms", chat, {"complete": False}, False, 2, None).data
    assert report["status"] == "partial"
    assert report["reasons"] == ["partial_history"]
    assert "closes" not in report
    # A finished history closes it by id.
    report = plugin.live_coverage("sms", chat, {"complete": True}, False, 2, None).data
    assert report["status"] == "complete" and report["closes"] == [7]
