import copy
import json
import sys
from pathlib import Path

import httpx
import pytest
from throughline_sdk import Checkpoint, Record
from throughline_sdk.contract import check_plugin

sys.path.insert(0, str(Path(__file__).parent))

import granola_fixtures as fx  # noqa: E402

from throughline_granola import Granola, cache, text  # noqa: E402
from throughline_granola import api as api_mod  # noqa: E402
from throughline_granola.mapping import Mapper  # noqa: E402

RECORDED = Path(__file__).parent / "recorded"
CANARY = "grn_InventedCanaryKey0000"
ENV_KEYS = [d.env for d in Granola.manifest.settings.values()] + ["GRANOLA_API_KEY"]


@pytest.fixture
def env(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def write_cache(tmp_path, payload, name="cache-v3.json"):
    path = tmp_path / name
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return path


def cache_plugin(env, path, **settings):
    env.setenv("GRANOLA_SOURCE", "cache")
    env.setenv("GRANOLA_CACHE_PATH", str(path))
    for key, value in settings.items():
        env.setenv(key, value)
    return Granola()


def messages(items):
    return [i for i in items if isinstance(i, dict)]


def by_id(items):
    return {m["external_id"]: m for m in messages(items)}


# ------------------------------------------------------------ contract ----

def test_granola_contract_kit_cache(env):
    result = check_plugin(
        "throughline_granola:Granola",
        {
            "GRANOLA_SOURCE": "cache",
            "GRANOLA_CACHE_PATH": str(RECORDED / "cache-v3.json"),
            "GRANOLA_BATCH_SIZE": "1",
        },
    )
    assert result["messages"] >= 20


def test_granola_contract_kit_api(env):
    result = check_plugin(
        "throughline_granola:Granola",
        {
            "GRANOLA_SOURCE": "api",
            "GRANOLA_FIXTURE": str(RECORDED / "api.json"),
            "GRANOLA_API_KEY": CANARY,
            "GRANOLA_BATCH_SIZE": "1",
        },
        secrets=[CANARY],
    )
    assert result["messages"] >= 12


def test_recorded_files_match_builders():
    assert json.loads((RECORDED / "cache-v3.json").read_text()) == fx.cache_file()
    assert json.loads((RECORDED / "api.json").read_text()) == fx.api_recording()


# --------------------------------------------------------- cache shape ----

def test_cache_double_encoded_and_object_forms(tmp_path):
    state = fx.cache_state()
    double = cache.load_state(write_cache(tmp_path, fx.cache_file(state)))
    plain = cache.load_state(write_cache(tmp_path, {"cache": {"state": state}}, "b.json"))
    bare = cache.load_state(write_cache(tmp_path, state, "c.json"))
    assert double == plain == bare == state


@pytest.mark.parametrize("payload, message", [
    ("{not json", "not valid JSON"),
    ("\x00\x01binary", "not JSON"),
    ({"cache": "{\"state\": {\"documentz\": {}}, \"version\": 9}"}, "no state.documents"),
    ({"cache": "[bad"}, "inner JSON"),
    ({"cache": 5}, "no cache object"),
])
def test_cache_unknown_shapes_raise_clear_errors(tmp_path, payload, message):
    with pytest.raises(cache.CacheError, match=message):
        cache.load_state(write_cache(tmp_path, payload))


def test_find_cache_picks_newest_plaintext_and_reports_encryption(tmp_path):
    write_cache(tmp_path, {}, "cache-v3.json")
    write_cache(tmp_path, {}, "cache-v4.json")
    assert cache.find_cache(str(tmp_path)).name == "cache-v4.json"
    enc = tmp_path / "enc"
    enc.mkdir()
    (enc / "cache-v6.json.enc").write_bytes(b"\x93\x00ciphertext")
    with pytest.raises(cache.CacheError, match="encrypts"):
        cache.find_cache(str(enc))
    with pytest.raises(cache.CacheError, match="not found"):
        cache.find_cache(str(tmp_path / "missing"))


def test_cache_never_writes_the_file(env, tmp_path):
    path = write_cache(tmp_path, fx.cache_file())
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    list(cache_plugin(env, path).records({}, "backfill"))
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_cache_meetings_tolerate_missing_keys():
    state = {"documents": {"x": {"id": "x", "created_at": "2025-01-02T03:04:05Z"},
                           "y": "not a dict", "z": {"title": "No times"}},
             "transcripts": {"x": [None, {"text": "no time"}, {"start_timestamp": "2025-01-02T03:04:06Z"}]},
             "documentPanels": {"x": "garbage"}, "documentLists": {"l": {"x": True}}}
    found = {m["id"]: m for m in cache.meetings(state)}
    assert set(found) == {"x", "z"}
    assert found["x"]["utterances"] == [] and found["x"]["summaries"] == []
    assert found["x"]["folders"] == ["l"]
    plugin_messages = [m for meeting in found.values() for m in Mapper().messages(meeting)]
    # z has no usable time at all and is skipped rather than given a fake one.
    assert [m["external_id"] for m in plugin_messages] == ["x"]


def test_cache_segments_skip_interim_and_empty_and_sort():
    segs = [
        {"id": "b", "text": "second", "start_timestamp": "2025-01-01T00:00:05Z", "source": "system"},
        {"id": "a", "text": "first", "start_timestamp": "2025-01-01T00:00:01Z", "source": "microphone"},
        {"id": "c", "text": "interim", "start_timestamp": "2025-01-01T00:00:06Z", "is_final": False},
        {"id": "d", "text": "  ", "start_timestamp": "2025-01-01T00:00:07Z"},
        {"id": "e", "text": "odd source", "start_timestamp": "2025-01-01T00:00:08Z", "source": "radio"},
    ]
    out = cache.segments_of("doc", segs)
    assert [(u["id"], u["attribution"]) for u in out] == [("a", "me"), ("b", "them"), ("e", None)]


def test_prosemirror_and_html_text():
    doc = {"type": "doc", "content": [
        {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "Title"}]},
        {"type": "bulletList", "content": [
            {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "one"}]}]},
            {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "two"}]}]},
        ]},
        {"type": "paragraph", "content": [{"type": "text", "text": "a"}, {"type": "hardBreak"},
                                          {"type": "text", "text": "b"}]},
    ]}
    assert text.prosemirror_text(doc) == "## Title\n- one\n- two\na\nb"
    assert text.html_text("<h3>Head</h3><ul><li>x &amp; y</li></ul><p>end</p>") == "### Head\n- x & y\nend"
    assert text.html_text(None) == ""


def test_moments_are_offset_aware():
    assert text.iso("2025-01-02T03:04:05.123Z") == "2025-01-02T03:04:05.123000+00:00"
    assert text.iso("2025-01-02T03:04:05") == "2025-01-02T03:04:05+00:00"
    assert text.iso("2025-01-02") == "2025-01-02T00:00:00+00:00"
    assert text.iso(1735787045000) == "2025-01-02T03:04:05+00:00"
    assert text.iso("garbage") is None and text.iso(True) is None


# -------------------------------------------------------------- mapping ----

def test_cache_mapping_channels_speakers_and_provenance(env, tmp_path):
    items = list(cache_plugin(env, write_cache(tmp_path, fx.cache_file())).records({}, "backfill"))
    rows = by_id(items)
    assert not any(k.startswith(fx.DOC_C) for k in rows)  # deleted document
    meeting = rows[fx.DOC_A]
    assert meeting["channel"] == "granola.meetings"
    assert meeting["happened_at"] == "2025-03-04T09:00:00-06:00"
    assert meeting["ended_at"] == "2025-03-04T09:30:00-06:00"
    assert meeting["fields"]["attendee_emails"] == ["avery@example.com", "robin@example.com"]
    assert meeting["fields"]["calendar_event_id"] == "evt-invented-a"
    mine = rows[f"{fx.DOC_A}:u:seg-aaaa-000"]
    theirs = rows[f"{fx.DOC_A}:u:seg-aaaa-001"]
    assert mine["author"]["value"] == "robin@example.com" and mine["fields"]["author_kind"] == "owner"
    assert theirs["author"] == {"system": "email", "kind": "email", "value": "avery@example.com",
                                "display_name": "Invented Avery Example"}
    assert theirs["fields"]["speaker_inferred"] is True
    assert theirs["fields"]["start_s"] == 7.0 and theirs["fields"]["end_s"] == 13.0
    group = rows[f"{fx.DOC_B}:u:seg-bbbb-000"]
    assert group["author"]["kind"] == "speaker_label"
    assert group["author"]["value"] == f"{fx.DOC_B}:Others"
    assert rows[fx.DOC_B]["fields"]["folders"] == ["Invented Team"]
    assert rows[f"{fx.DOC_B}:notes"]["content"] == "Typed in the editor only"
    assert f"{fx.DOC_E}:u:seg-eeee-002" not in rows  # interim segment
    assert f"{fx.DOC_E}:panel:panel-e-1" not in rows  # link-only panel
    assert f"{fx.DOC_B}:panel:panel-b-2" not in rows  # deleted panel
    summary = rows[f"{fx.DOC_A}:panel:panel-a-1"]
    assert summary["fields"]["author_kind"] == "agent" and summary["author"]["kind"] == "agent"
    assert summary["content"].startswith("### Invented budget")
    prov = [i.data for i in items if isinstance(i, Record) and i.kind == "provenance"
            and i.data["derived"]["external_id"] == summary["external_id"]]
    assert len(prov) == 1
    assert {s["external_id"] for s in prov[0]["sources"]} == {f"{fx.DOC_A}:notes"} | {
        f"{fx.DOC_A}:u:seg-aaaa-00{i}" for i in range(5)}
    untitled = rows[fx.DOC_D]
    assert untitled["thread"]["title"] == "Untitled meeting"
    assert untitled["happened_at"] == "2025-03-07T12:00:00+00:00"
    for message in messages(items):
        assert message["content"].strip()
        assert "+" in message["happened_at"] or "-" in message["happened_at"][19:]


def test_shared_note_is_not_owner_and_access_label(env, tmp_path):
    state = fx.cache_state()
    state["documents"][fx.DOC_A]["people"]["creator"] = fx.BLAKE
    rows = by_id(cache_plugin(env, write_cache(tmp_path, fx.cache_file(state)),
                              GRANOLA_OWNER_EMAIL="robin@example.com",
                              GRANOLA_ACCESS_LABEL="work").records({}, "backfill"))
    mic = rows[f"{fx.DOC_A}:u:seg-aaaa-000"]
    assert mic["author"]["value"] == "blake@example.com"
    assert mic["fields"]["author_kind"] == "other"
    assert rows[fx.DOC_A]["fields"]["is_own_note"] is False
    assert rows[f"{fx.DOC_A}:notes"]["fields"]["author_kind"] == "other"
    assert rows[f"{fx.DOC_B}:u:seg-bbbb-001"]["fields"]["author_kind"] == "owner"
    assert {m["access_label"] for m in rows.values()} == {"work"}


def test_cache_sync_skips_unchanged_and_reemits_changed_meeting(env, tmp_path):
    path = write_cache(tmp_path, fx.cache_file())
    plugin = cache_plugin(env, path, GRANOLA_BATCH_SIZE="2")
    first = list(plugin.records({}, "backfill"))
    checkpoints = [i for i in first if isinstance(i, Checkpoint)]
    assert len(checkpoints) == 3  # 5 meetings in batches of 2, plus the final one
    cursor = checkpoints[-1].cursor
    assert messages(plugin.records(cursor, "sync")) == []
    state = fx.cache_state()
    state["transcripts"][fx.DOC_B].append({
        "id": "seg-bbbb-new", "text": "One more invented line.", "source": "microphone",
        "start_timestamp": "2025-03-05T16:05:00.000Z", "end_timestamp": "2025-03-05T16:05:03.000Z"})
    write_cache(tmp_path, fx.cache_file(state))
    again = messages(plugin.records(cursor, "sync"))
    assert {m["thread"]["external_id"] for m in again} == {fx.DOC_B}
    assert f"{fx.DOC_B}:u:seg-bbbb-new" in by_id(again)
    # Resume from a middle checkpoint delivers exactly the remainder.
    rest = messages(plugin.records(checkpoints[0].cursor, "backfill"))
    assert {m["thread"]["external_id"] for m in rest} == {fx.DOC_B, fx.DOC_D, fx.DOC_E, fx.DOC_F}


def test_mapping_config_change_resets_cursor(env, tmp_path):
    path = write_cache(tmp_path, fx.cache_file())
    items = list(cache_plugin(env, path).records({}, "backfill"))
    cursor = items[-1].cursor
    changed = cache_plugin(env, path, GRANOLA_ACCESS_LABEL="work")
    assert len(messages(changed.records(cursor, "sync"))) == len(messages(items))


# ------------------------------------------------------------------ API ----

def api_plugin(env, transport=None, **settings):
    env.setenv("GRANOLA_SOURCE", "api")
    env.setenv("GRANOLA_API_KEY", CANARY)
    for key, value in settings.items():
        env.setenv(key, value)
    return Granola(transport=transport or api_mod.fixture_transport(RECORDED / "api.json"))


def test_api_mapping_pagination_413_and_disabled_transcript(env):
    items = list(api_plugin(env).records({}, "backfill"))
    rows = by_id(items)
    assert fx.NOTE_4 not in rows  # deleted
    assert [m["external_id"] for m in messages(items) if m["channel"] == "granola.meetings"] == [
        fx.NOTE_1, fx.NOTE_2, fx.NOTE_3]
    named = rows[f"{fx.NOTE_1}:u:00001"]
    assert named["author"]["value"] == "avery@example.com" and named["fields"]["speaker_inferred"] is False
    assert rows[f"{fx.NOTE_1}:u:00002"]["author"]["value"] == f"{fx.NOTE_1}:Others"
    assert rows[fx.NOTE_1]["author"]["value"] == "avery@example.com"  # organiser
    assert rows[fx.NOTE_1]["fields"]["folders"] == ["Invented Design"]
    assert rows[f"{fx.NOTE_1}:notes"]["content"] == "- invented mockups look good"
    # 413: transcript fetched in two pages; phone lines keep diarization labels.
    phone = [rows[f"{fx.NOTE_2}:u:0000{i}"] for i in range(3)]
    assert [p["fields"]["speaker_label"] for p in phone] == ["Speaker A", "Speaker B", "Speaker A"]
    assert {p["fields"]["speaker_attribution"] for p in phone} == {"unknown"}
    assert rows[f"{fx.NOTE_2}:panel:summary"]["content"] == "Invented memo summary"
    assert rows[fx.NOTE_3]["fields"]["transcript_status"] == "disabled"
    cov = [i.data for i in items if isinstance(i, Record) and i.kind == "coverage"]
    assert cov and cov[0]["reasons"] == ["transcript_access_disabled"]
    # Attachment download URLs are bearer links and never stored.
    assert "files.example.com" not in json.dumps(messages(items))
    assert CANARY not in json.dumps([getattr(i, "data", i) for i in items], default=str)


def test_api_incremental_high_water(env):
    seen = []
    recorded = api_mod.fixture_transport(RECORDED / "api.json")

    def handler(request):
        seen.append((request.url.path, dict(request.url.params)))
        assert request.headers["authorization"] == "Bearer " + CANARY
        return recorded.handle_request(request)

    plugin = api_plugin(env, httpx.MockTransport(handler), GRANOLA_BATCH_SIZE="1")
    items = list(plugin.records({}, "backfill"))
    cursor = items[-1].cursor
    assert cursor["high_water"] == ["2025-04-03T12:00:00Z", fx.NOTE_3]
    assert len([i for i in items if isinstance(i, Checkpoint)]) == 4
    seen.clear()
    assert messages(plugin.records(cursor, "sync")) == []
    assert seen[0][1]["updated_after"] == "2025-04-03T11:59:59Z"
    assert not any(p.startswith(f"/v1/notes/{fx.NOTE_1}") for p, _ in seen)
    # Resuming after the first note re-fetches only the later ones.
    first = [i for i in items if isinstance(i, Checkpoint)][0].cursor
    rest = messages(plugin.records(first, "backfill"))
    assert {m["thread"]["external_id"] for m in rest} == {fx.NOTE_2, fx.NOTE_3}


def test_api_retries_rate_limit_then_raises_when_unreachable(env):
    calls = []

    def flaky(request):
        calls.append(request.url.path)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"notes": [], "hasMore": False, "cursor": None})

    assert messages(api_plugin(env, httpx.MockTransport(flaky)).records({}, "sync")) == []
    assert len(calls) == 2

    def down(request):
        raise httpx.ConnectError("unreachable")

    plugin = api_plugin(env, httpx.MockTransport(down))
    sleeps = []
    original = api_mod.Api.__init__

    def fast(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.sleep = sleeps.append

    env.setattr(api_mod.Api, "__init__", fast)
    with pytest.raises(api_mod.ApiError, match="ConnectError"):
        list(plugin.records({}, "sync"))
    assert len(sleeps) == 4


def test_api_bad_key_raises(env):
    plugin = api_plugin(env, httpx.MockTransport(lambda r: httpx.Response(401)))
    with pytest.raises(api_mod.ApiError, match="401"):
        list(plugin.records({}, "sync"))


def test_api_requires_key(env):
    env.setenv("GRANOLA_SOURCE", "api")
    with pytest.raises(ValueError, match="GRANOLA_API_KEY"):
        list(Granola().records({}, "sync"))


def test_auto_mode_selection(env, tmp_path):
    assert Granola().mode == "cache"
    env.setenv("GRANOLA_API_KEY", CANARY)
    assert Granola().mode == "api"
    env.setenv("GRANOLA_SOURCE", "bogus")
    with pytest.raises(ValueError):
        Granola()


def test_api_unattributed_desktop_speaker_is_them():
    out = api_mod.utterances_of([
        {"speaker": {"source": "speaker"}, "text": "x", "start_time": "2025-01-01T00:00:00Z",
         "end_time": "2025-01-01T00:00:01Z"},
        {"speaker": {"source": "microphone"}, "text": "y", "start_time": "2025-01-01T00:00:00Z",
         "end_time": "2025-01-01T00:00:01Z"},
        {"speaker": {}, "text": "", "start_time": "2025-01-01T00:00:02Z"},
    ])
    assert [(u["id"], u["attribution"]) for u in out] == [("00000", "them"), ("00001", None)]


def test_meeting_without_attendees_or_calendar_uses_recording_span():
    one, *_ = fx.api_notes()
    bare = copy.deepcopy(one)
    bare.update(calendar_event=None, attendees=[], owner={"name": None, "email": "robin@example.com"})
    meeting = api_mod.meeting_of(bare)
    rows = Mapper().messages(meeting)
    assert rows[0]["happened_at"] == "2025-04-01T15:01:00+00:00"
    assert rows[0]["ended_at"] == "2025-04-01T15:01:14+00:00"
