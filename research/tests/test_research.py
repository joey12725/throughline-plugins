"""Offline tests: synthetic fixtures, a local fake HTTP server, no real network."""

import ast
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from throughline_sdk import Checkpoint, Record
from throughline_sdk.contract import check_plugin

from throughline_research import Research, fetch, mapping
from throughline_research.mapping import ITEMS, RESULTS

HERE = Path(__file__).parent
EXPORT = HERE / "recorded" / "export"
PACKAGE = Path(fetch.__file__).parent
EXAMPLE = PACKAGE / "examples" / "events.json"
CANARY = "invented-secret-canary-key"


def messages(records):
    return [r for r in records if isinstance(r, dict)]


def of(records, channel):
    return [r for r in messages(records) if r["channel"] == channel]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("RESEARCH_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    waits = []
    monkeypatch.setattr(fetch, "SLEEP", waits.append)
    return waits


def plugin(monkeypatch, **env):
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    return Research()


# ------------------------------------------------------------ fake server


class FakeProvider:
    """OpenAI-compatible fake on 127.0.0.1 with scripted responses."""

    def __init__(self):
        self.requests = []
        self.script = []
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                provider.requests.append(
                    {"path": self.path, "headers": dict(self.headers), "body": body}
                )
                status, headers, payload = (
                    provider.script.pop(0)
                    if provider.script
                    else provider.default(body)
                )
                data = json.dumps(payload).encode()
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def default(self, body):
        prompt = body["messages"][-1]["content"]
        text = (
            f"### Invented result for: {prompt[:40]}\n"
            "Event: Invented Picnic\n"
            "Occurrence start: 2020-03-04T12:00:00+01:00\n"
            "Venue: Invented Park\n"
            "Canonical URL: https://picnic.example.com/2020\n"
        )
        return (
            200,
            {},
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": text,
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url_citation": {
                                        "url": "https://picnic.example.com/2020",
                                        "title": "Invented picnic",
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20},
            },
        )

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def provider():
    fake = FakeProvider()
    yield fake
    fake.close()


def write_stream(tmp_path, **overrides):
    stream = {
        "id": "invented",
        "title": "Invented stream",
        "timezone": "Europe/Berlin",
        "cadence": "daily",
        "window_days": 5,
        "output": "labeled_markdown",
        "item_type": "event",
        "variables": {"city": None},
        "providers": [{"type": "openai", "web_search": "plugin"}],
        "searches": [
            {
                "id": f"search-{n}",
                "prompt": f"Invented search {n} in {{city}} from {{window_start}} to {{window_end}}.",
            }
            for n in range(1, 5)
        ],
    }
    stream.update(overrides)
    path = tmp_path / "streams" / "invented.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stream))
    return path.parent


def live_env(provider, streams, **extra):
    env = {
        "RESEARCH_SOURCE": "live",
        "RESEARCH_STREAMS": str(streams),
        "RESEARCH_DATE": "2020-03-01",
        "RESEARCH_ALLOW_MODEL_CALLS": "1",
        "RESEARCH_OPENAI_BASE_URL": provider.url,
        "RESEARCH_OPENAI_MODEL": "invented-model",
        "RESEARCH_OPENAI_API_KEY": CANARY,
        "RESEARCH_MIN_INTERVAL_S": "0",
        "RESEARCH_VAR_CITY": "Example City",
    }
    env.update(extra)
    return env


def final_cursor(records):
    return [r for r in records if isinstance(r, Checkpoint)][-1].cursor


# ---------------------------------------------------------------- contract


def test_res_1_contract_export(monkeypatch):
    env = {
        "RESEARCH_SOURCE": f"export:{EXPORT}",
        "RESEARCH_BATCH": "2",
        "RESEARCH_OPENAI_API_KEY": CANARY,
    }
    result = check_plugin("throughline_research:Research", env, secrets=[CANARY])
    assert result["messages"] == 11


def test_res_2_manifest_channels_and_fields():
    manifest = json.loads(Research.manifest.model_dump_json())
    channels = {c["name"]: c for c in manifest["channels"]}
    assert set(channels) == {RESULTS, ITEMS}
    assert all(c["record_kind"] == "derived" for c in channels.values())
    reserved = {
        "kind",
        "id",
        "content",
        "fields",
        "channel",
        "external_id",
        "happened_at",
        "score",
    }
    for channel in channels.values():
        names = [f["name"] for f in channel["fields"]]
        assert len(names) == len(set(names)) and not reserved & set(names)
        assert "mapping_revision" in names
    assert manifest["secrets"]["api_key"]["env"] == "RESEARCH_OPENAI_API_KEY"
    assert manifest["settings"]["allow_model_calls"]["default"] == "0"


# ----------------------------------------------------------------- mapping


def test_res_3_export_mapping(monkeypatch):
    records = list(
        plugin(monkeypatch, RESEARCH_SOURCE=f"export:{EXPORT}").records({}, "backfill")
    )
    results, items = of(records, RESULTS), of(records, ITEMS)
    assert [r["external_id"] for r in results] == [
        "events/interest-01-a/2020-03-01",
        "events/interest-01-b/2020-03-01",
        "events/news-check-01/2020-03-01",
        "events/interest-01-a/2020-03-02",
        "events/interest-02-a/2020-03-02",
        "events/interest-01-b/2020-03-02/import-14b1f5d8bc",
    ]
    first = results[0]
    assert first["happened_at"] == "2020-03-01T08:00:00+01:00"
    assert first["subject"] == {
        "start": "2020-03-01T00:00:00+01:00",
        "end": "2020-03-11T00:00:00+01:00",
        "start_precision": "day",
        "end_precision": "day",
    }
    assert first["fields"]["cited_urls"] == [
        "https://events.example.com/orchard-night",
        "https://calendar.example.org/orchard",
        "https://library.example.net/synth",
    ]
    assert first["fields"]["item_count"] == 2 and first["fields"]["status"] == "ok"
    assert first["fields"]["prompt_tokens"] == 120
    assert first["fields"]["mapping_revision"] == mapping.MAPPING_REVISION
    assert first["content"].startswith(
        "Invented events / Invented interest one (anchored) (2020-03-01)"
    )
    assert (
        results[1]["fields"]["status"] == "empty"
        and results[1]["fields"]["item_count"] == 0
    )
    news = results[2]
    assert news["fields"]["cited_urls"] == ["https://sport.example.com/final"]
    assert news["subject"]["start"] == "2020-02-23T00:00:00+01:00"
    assert news["fields"]["completion_tokens"] == 20
    imported = results[5]
    assert imported["fields"]["provider"] == "import"
    assert imported["fields"]["import_path"] == "2020-03-02/interest-01-b.json"
    assert "https://zine.example.org/issue-4" in imported["fields"]["cited_urls"]
    json_result = results[4]
    assert json_result["content"].endswith(
        "One invented event found.\n- Invented Board Game Evening | Sun 2020-03-08 19:30 | Invented Cafe | https://games.example.com/evening"
    )

    market, workshop = items[0], items[1]
    assert market["happened_at"] == first["happened_at"]
    assert market["subject"] == {
        "start": "2020-03-05T18:00:00+01:00",
        "start_precision": "exact",
        "end": "2020-03-05T22:00:00+01:00",
        "end_precision": "exact",
    }
    assert market["fields"]["ticket_url"] is None  # "N/A"
    assert market["fields"]["primary_confirmed"] is True
    assert market["fields"]["details"] == {"public_listing": "yes"}
    assert workshop["subject"] == {
        "start": "2020-03-07T00:00:00+01:00",
        "start_precision": "day",
    }
    assert workshop["fields"]["date_precision"] == "day"
    assert workshop["fields"]["canonical_url"] == "https://library.example.net/synth"
    assert workshop["fields"]["primary_confirmed"] is False
    # The same occurrence found on another day shares the event entity.
    again = items[2]
    assert again["fields"]["item_key"] == market["fields"]["item_key"]
    assert again["external_id"] != market["external_id"]
    game = items[3]
    assert game["fields"]["occurrence_start"] == "2020-03-08T19:30:00+01:00"
    assert game["fields"]["details"] == {"accessibility": "step-free"}

    links = [r.data for r in records if isinstance(r, Record) and r.kind == "link"]
    about = [x for x in links if x["type"] == "about"]
    assert about[0]["to"] == about[2]["to"]
    assert about[0]["to"]["kind"] == "event"
    assert all(x["method"] == "source" for x in links)
    cites = [x for x in links if x["type"] == "cites"]
    assert len(cites) == sum(r["fields"]["citation_count"] for r in results)
    found = [x for x in links if x["type"] == "found_in"]
    assert [x["from"]["external_id"] for x in found] == [
        i["external_id"] for i in items
    ]
    sources = [
        r.data for r in records if isinstance(r, Record) and r.kind == "provenance"
    ]
    assert [s["derived"]["external_id"] for s in sources] == [
        i["external_id"] for i in items
    ]
    assert sources[0]["sources"] == [
        {"channel": RESULTS, "external_id": first["external_id"]}
    ]


def test_res_4_mapping_is_deterministic(monkeypatch):
    research = plugin(monkeypatch, RESEARCH_SOURCE=f"export:{EXPORT}")
    once = [
        r if isinstance(r, dict) else r.model_dump()
        for r in research.records({}, "backfill")
    ]
    twice = [
        r if isinstance(r, dict) else r.model_dump()
        for r in research.records({}, "sync")
    ]
    assert once == twice
    ids = [
        (m["channel"], m["external_id"]) for m in messages(research.records({}, "sync"))
    ]
    assert len(ids) == len(set(ids))


def test_res_5_export_cursor_partial_line_and_bounds(monkeypatch, tmp_path):
    lines = (EXPORT / "2020-03-01.jsonl").read_text().splitlines()
    target = tmp_path / "exports"
    target.mkdir()
    file = target / "run.jsonl"
    file.write_text(lines[0] + "\n" + lines[1][:50], newline="\n")
    research = plugin(monkeypatch, RESEARCH_SOURCE=f"export:{target}")
    records = list(research.records({}, "sync"))
    assert [m["external_id"] for m in of(records, RESULTS)] == [
        "events/interest-01-a/2020-03-01"
    ]
    cursor = final_cursor(records)
    assert cursor["files"]["run.jsonl"]["lines"] == 1
    file.write_text("\n".join(lines) + "\n", newline="\n")
    resumed = list(research.records(cursor, "sync"))
    assert [m["external_id"] for m in of(resumed, RESULTS)] == [
        "events/interest-01-b/2020-03-01",
        "events/news-check-01/2020-03-01",
    ]
    assert messages(research.records(final_cursor(resumed), "sync")) == []
    # A newer mapping revision re-maps every line once.
    stale = dict(final_cursor(resumed), mapping_revision=0)
    assert len(of(research.records(stale, "sync"), RESULTS)) == 3
    bounded = plugin(
        monkeypatch, RESEARCH_SOURCE=f"export:{EXPORT}", RESEARCH_SINCE="2020-03-02"
    )
    assert {
        m["fields"]["run_date"] for m in messages(bounded.records({}, "backfill"))
    } == {"2020-03-02"}


def test_res_6_labeled_parser_variants():
    text = (
        "Intro line\n\n"
        "## **Invented Fair**\n"
        "* Occurrence start: 2020-05-01 10:00\n"
        "* Venue: Invented Square\n"
        "* Price: TBA\n"
        "* Odd label: kept\n"
        "### Lonely heading\n"
        "just prose\n"
    )
    raw = {
        "kind": "research.result",
        "id": "s/q/2020-04-30",
        "run_date": "2020-04-30",
        "completed_at": "2020-04-30T06:00:00Z",
        "stream": {"id": "s", "timezone": "Europe/Berlin"},
        "search": {"id": "q", "output": "labeled_markdown", "item_type": "event"},
        "window": {"start": "2020-04-30", "end": "2020-05-07"},
        "provider": {"type": "openai"},
        "response": {"choices": [{"message": {"content": text}}]},
    }
    found = mapping.extract(raw)
    assert len(found["items"]) == 1
    item = found["items"][0]
    assert item["title"] == "Invented Fair"
    assert item["occurrence_start"] == "2020-05-01T10:00:00+02:00"
    assert item["price"] is None
    assert item["details"] == {"odd_label": "kept"}
    out = mapping.result_records(raw)
    assert out[0][1]["happened_at"] == "2020-04-30T06:00:00+00:00"
    # Markdown output never yields items, even with headings.
    raw["search"]["output"] = "markdown"
    assert mapping.extract(raw)["items"] == []


# ------------------------------------------------------------------ config


def test_res_7_example_stream_is_placeholder_pack(monkeypatch):
    streams = fetch.load_streams(EXAMPLE)
    assert len(streams) == 1
    events = streams[0]
    searches = events["searches"]
    assert len(searches) == 73
    groups = {s["cluster"] for s in searches if s["kind"] != "current-check"}
    assert len(groups) == 35
    assert sum(s["kind"] == "current-check" for s in searches) == 3
    assert {s["variant"] for s in searches if s["cluster"]} == {"A", "B"}
    for search in searches:
        values = search["variables"]
        for key in ("group_label", "anchors", "topic"):
            if key in values:
                assert values[key].startswith("<") and values[key].endswith(">")
    assert events["variables"]["city"] is None
    day = fetch.parse_day("2020-03-01")
    with pytest.raises(fetch.ConfigError, match="city"):
        fetch.render(
            searches[0]["prompt"], fetch.variables_for(events, searches[0], day, {})
        )
    env = {
        "RESEARCH_VAR_CITY": "Example City",
        "RESEARCH_VAR_SECONDARY_CITY": "Other City",
        "RESEARCH_VAR_REGION": "Example Region",
    }
    for search in searches:
        prompt = fetch.render(
            search["prompt"], fetch.variables_for(events, search, day, env)
        )
        assert "{" not in prompt
    assert "2020-03-11" in fetch.render(
        searches[0]["prompt"], fetch.variables_for(events, searches[0], day, env)
    )


def test_res_8_render_and_validation():
    assert (
        fetch.render('{{literal}} {a} {"json": 1}', {"a": "x"})
        == '{literal} x {"json": 1}'
    )
    with pytest.raises(fetch.ConfigError, match="b, c"):
        fetch.render("{b} {c}", {})
    with pytest.raises(fetch.ConfigError, match="duplicate search"):
        fetch.normalize_stream(
            {
                "id": "x",
                "searches": [{"id": "a", "prompt": "p"}, {"id": "a", "prompt": "q"}],
            }
        )
    with pytest.raises(fetch.ConfigError, match="cadence"):
        fetch.normalize_stream({"id": "x", "cadence": "fortnightly"})
    with pytest.raises(fetch.ConfigError, match="provider"):
        fetch.normalize_stream({"id": "x", "provider": "gemini"})
    stream = fetch.normalize_stream(
        {
            "id": "x",
            "matrix": {
                "groups": [{"id": "g1", "label": "G one", "anchors": "a1"}],
                "variants": [
                    {"id": "a", "prompt": "{group_label} at {anchors}"},
                    {"id": "b", "prompt": "{group_id}"},
                ],
            },
            "searches": [{"id": "extra", "prompt": "p", "cadence": "weekly"}],
        }
    )
    assert [s["id"] for s in stream["searches"]] == ["g1-a", "g1-b", "extra"]
    assert stream["searches"][2]["cadence"] == 7
    day = fetch.parse_day("2020-01-01")
    assert (
        fetch.render(
            stream["searches"][0]["prompt"],
            fetch.variables_for(stream, stream["searches"][0], day, {}),
        )
        == "G one at a1"
    )


def test_res_9_convert_pack(tmp_path):
    pack = {
        "schema": "2.0",
        "timezone": "Europe/Berlin",
        "window_days": 10,
        "grounding_rules": ["Invented rule one", "Invented rule two"],
        "searches": [
            {
                "id": "cluster-one-A",
                "cluster": "cluster-one",
                "cluster_number": 1,
                "kind": "local-event",
                "variant": "A",
                "variant_label": "anchored",
                "query": "Events {window_start} to {window_end} near {home_area}. {planning_context}",
            },
            {
                "id": "check-one",
                "kind": "current-check",
                "variant": "C",
                "query": "Latest invented score as of {today}",
            },
        ],
    }
    source = tmp_path / "pack.json"
    source.write_text(json.dumps(pack))
    out = tmp_path / "converted.json"
    assert fetch.main(["convert-pack", "--pack", str(source), "--out", str(out)]) == 0
    stream = json.loads(out.read_text())
    assert stream["variables"] == {"home_area": None, "planning_context": ""}
    assert stream["system"] == "- Invented rule one\n- Invented rule two"
    first, check = stream["searches"]
    assert first["id"] == "cluster-one-a" and first["output"] == "labeled_markdown"
    assert first["item_type"] == "event" and first["label"] == "cluster-one (anchored)"
    assert check["output"] == "markdown" and "item_type" not in check
    assert len(fetch.load_streams(out)[0]["searches"]) == 2


def test_res_9b_convert_pack_contract_window_and_contextual_search():
    pack = {
        "window": {"timezone": "Europe/Berlin", "default_days": 6},
        "grounding_rules": ["Invented rule"],
        "output_contract": "Invented output contract.",
        "contextual_search": {
            "id": "near-plan",
            "cluster": "near-plan",
            "kind": "local-event",
            "variant": "contextual",
            "query": "Things near {planning_context} from {window_start}",
        },
        "searches": [
            {"id": "one-a", "kind": "local-event", "query": "Invented {window_end}"},
            {"id": "near-plan", "kind": "local-event", "query": "duplicate id"},
        ],
    }
    stream = fetch.convert_pack(pack)
    assert stream["system"] == "- Invented rule\n\nInvented output contract."
    assert stream["timezone"] == "Europe/Berlin" and stream["window_days"] == 6
    ids = [s["id"] for s in stream["searches"]]
    assert ids == ["one-a", "near-plan"]
    assert stream["searches"][1]["prompt"] == "duplicate id"
    assert stream["variables"] == {}
    pack["searches"].pop()
    stream = fetch.convert_pack(pack)
    assert stream["searches"][1]["variant"] == "contextual"
    assert stream["variables"] == {"planning_context": ""}


def test_res_10_fetch_is_standard_library_only():
    tree = ast.parse((PACKAGE / "fetch.py").read_text())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "fetch.py must not use relative imports"
            modules.add(node.module.split(".")[0])
    assert modules - set(sys.stdlib_module_names) == {"yaml"}  # optional, guarded


# -------------------------------------------------------------------- live


def test_res_11_model_calls_off_by_default(monkeypatch, tmp_path, provider):
    streams = write_stream(tmp_path)
    env = live_env(provider, streams)
    del env["RESEARCH_ALLOW_MODEL_CALLS"]
    records = list(plugin(monkeypatch, **env).records({}, "sync"))
    assert messages(records) == [] and provider.requests == []


def test_res_12_live_calls_limits_and_cadence(monkeypatch, tmp_path, provider):
    streams = write_stream(tmp_path)
    research = plugin(
        monkeypatch,
        **live_env(
            provider,
            streams,
            RESEARCH_MAX_CALLS_PER_RUN=2,
            RESEARCH_MAX_CALLS_PER_DAY=3,
        ),
    )
    first = list(research.records({}, "sync"))
    assert [m["external_id"] for m in of(first, RESULTS)] == [
        "invented/search-1/2020-03-01",
        "invented/search-2/2020-03-01",
    ]
    request = provider.requests[0]
    assert request["path"] == "/v1/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer " + CANARY
    assert request["body"]["model"] == "invented-model"
    assert request["body"]["plugins"] == [{"id": "web"}]
    assert request["body"]["messages"][-1]["content"] == (
        "Invented search 1 in Example City from 2020-03-01 to 2020-03-06."
    )
    result = of(first, RESULTS)[0]
    assert (
        "source" not in result["fields"]
        and result["fields"]["model"] == "invented-model"
    )
    assert len(of(first, ITEMS)) == 2
    assert CANARY not in json.dumps(
        [r if isinstance(r, dict) else r.model_dump() for r in first]
    )
    cursor = final_cursor(first)
    assert CANARY not in json.dumps(cursor)
    second = list(research.records(cursor, "sync"))
    # Per-day limit 3: only one more call today.
    assert [m["fields"]["search_id"] for m in of(second, RESULTS)] == ["search-3"]
    assert len(provider.requests) == 3
    # Done searches are not repeated; the next day the cadence allows them again.
    research.settings["max_calls_per_day"] = "100"
    third = list(research.records(final_cursor(second), "sync"))
    assert [m["fields"]["search_id"] for m in of(third, RESULTS)] == ["search-4"]
    assert messages(research.records(final_cursor(third), "sync")) == []
    research.settings["date"] = "2020-03-02"
    research.settings["max_calls_per_run"] = "10"
    fourth = list(research.records(final_cursor(third), "sync"))
    assert len(of(fourth, RESULTS)) == 4


def test_res_13_retry_backoff_and_auth_failure(
    monkeypatch, tmp_path, provider, clean_env
):
    streams = write_stream(
        tmp_path, searches=[{"id": "only", "prompt": "Invented {city}"}]
    )
    research = plugin(monkeypatch, **live_env(provider, streams))
    provider.script = [
        (429, {"Retry-After": "3"}, {"error": "slow down"}),
        (503, {}, {"error": "unavailable"}),
    ]
    records = list(research.records({}, "sync"))
    assert len(of(records, RESULTS)) == 1 and len(provider.requests) == 3
    assert clean_env[:2] == [3.0, 4.0]
    provider.script = [(401, {}, {"error": "invented bad key " + CANARY})]
    with pytest.raises(fetch.FetchError) as error:
        list(research.records({}, "sync"))
    assert "401" in str(error.value) and CANARY not in str(error.value)
    # Other client errors skip the search for this run and leave it due; when
    # every call of the run failed, the run fails after its checkpoint.
    provider.script = [(400, {}, {"error": "bad request"})]
    skipped = []
    with pytest.raises(fetch.FetchError, match="every provider call failed"):
        for item in research.records({}, "sync"):
            skipped.append(item)
    assert messages(skipped) == [] and final_cursor(skipped)["live"]["last_run"] == {}


def test_res_24_some_failed_calls_report_partial_coverage(
    monkeypatch, tmp_path, provider
):
    streams = write_stream(tmp_path)
    research = plugin(monkeypatch, **live_env(provider, streams))
    ok = provider.default({"messages": [{"content": "Invented"}]})
    provider.script = [ok, (400, {}, {"error": "invented"})]
    records = list(research.records({}, "sync"))
    reports = [r.data for r in records if isinstance(r, Record) and r.kind == "coverage"]
    assert reports == [
        {
            "channel": RESULTS,
            "scope_key": "invented",
            "status": "partial",
            "reasons": ["source_error"],
            "detail": {"failed_searches": ["invented/search-2"]},
        }
    ]
    assert len(of(records, RESULTS)) == 3


def test_res_14_cache_prevents_repeat_spend(monkeypatch, tmp_path, provider):
    streams = write_stream(tmp_path)
    env = live_env(provider, streams, RESEARCH_CACHE_DIR=tmp_path / "cache")
    research = plugin(monkeypatch, **env)
    first = messages(research.records({}, "sync"))
    assert len(provider.requests) == 4
    assert (tmp_path / "cache" / "invented" / "2020-03-01" / "search-1.json").exists()
    again = messages(research.records({}, "sync"))  # cursor lost: cache answers
    assert len(provider.requests) == 4 and again == first


def test_res_15_responses_endpoint(monkeypatch, tmp_path, provider):
    streams = write_stream(
        tmp_path,
        output="markdown",
        providers=[
            {
                "type": "openai",
                "endpoint": "responses",
                "web_search": "tool",
                "model": "invented-search-model",
            }
        ],
        searches=[{"id": "only", "prompt": "Invented {city}"}],
        system="Invented rules",
    )
    provider.script = [
        (
            200,
            {},
            {
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Invented answer.",
                                "annotations": [
                                    {
                                        "type": "url_citation",
                                        "url": "https://answer.example.com/a",
                                        "title": "A",
                                    }
                                ],
                            }
                        ],
                    }
                ],
                "usage": {"input_tokens": 3, "output_tokens": 4},
            },
        )
    ]
    records = messages(
        plugin(monkeypatch, **live_env(provider, streams)).records({}, "sync")
    )
    body = provider.requests[0]["body"]
    assert provider.requests[0]["path"] == "/v1/responses"
    assert body == {
        "model": "invented-search-model",
        "input": "Invented Example City",
        "instructions": "Invented rules",
        "tools": [{"type": "web_search"}],
    }
    assert records[0]["fields"]["cited_urls"] == ["https://answer.example.com/a"]
    assert records[0]["fields"]["model"] == "invented-search-model"


# ------------------------------------------------------------------ import


def base(external_id):
    return external_id.split("/import-")[0]


def make_imports(root):
    day1 = root / "2020-03-01"
    day1.mkdir(parents=True)
    (day1 / "search-1.json").write_text(
        json.dumps(
            {
                "id": "search-1",
                "model": "invented-model",
                "query": "Invented query",
                "citations": [
                    {"url": "https://cached.example.com/1", "title": "Cached"}
                ],
            }
        )
    )
    (day1 / "search-1.md").write_text(
        "### Invented Cached Gig\nEvent: Invented Cached Gig\nOccurrence start: 2020-03-03T20:00:00+01:00\nVenue: Invented Basement\n"
    )
    day2 = root / "2020-03-02"
    day2.mkdir()
    (day2 / "search-2.md").write_text("No verifiable events found for this window.")
    (root / "bundle.json").write_text(
        json.dumps(
            {
                "results": [
                    {
                        "search_id": "search-3",
                        "completed_at": "2020-03-02T05:00:00Z",
                        "answer": "Invented bundled finding https://bundle.example.com/x",
                    }
                ]
            }
        )
    )


def test_res_16_import_provider(monkeypatch, tmp_path, provider):
    root = tmp_path / "cache-from-elsewhere"
    make_imports(root)
    streams = write_stream(
        tmp_path,
        providers=[
            {
                "type": "import",
                "path": str(root),
                "glob": ["2020-*/*.json", "2020-*/*.md"],
            },
            {
                "type": "import",
                "path": str(root),
                "glob": "bundle.json",
                "records": "/results",
            },
            {"type": "openai", "web_search": "plugin"},
        ],
    )
    research = plugin(
        monkeypatch, **live_env(provider, streams, RESEARCH_MAX_CALLS_PER_RUN=10)
    )
    records = list(research.records({}, "sync"))
    results = of(records, RESULTS)
    imported = [r for r in results if r["fields"]["provider"] == "import"]
    assert all("/import-" in r["external_id"] for r in imported)
    assert [(base(r["external_id"]), r["fields"]["status"]) for r in imported] == [
        ("invented/search-1/2020-03-01", "ok"),
        ("invented/search-2/2020-03-02", "empty"),
        ("invented/search-3/2020-03-02", "ok"),
    ]
    first = imported[0]
    assert first["fields"]["import_path"] == "2020-03-01/search-1.json"
    assert first["fields"]["prompt"] == "Invented query"
    assert first["fields"]["cited_urls"] == ["https://cached.example.com/1"]
    assert of(records, ITEMS)[0]["fields"]["title"] == "Invented Cached Gig"
    assert imported[2]["happened_at"] == "2020-03-02T05:00:00+00:00"
    # search-1 was answered for the run date by the import: no model call for it.
    called = [
        r["fields"]["search_id"] for r in results if r["fields"]["provider"] == "openai"
    ]
    assert called == ["search-2", "search-3", "search-4"]
    cursor = final_cursor(records)
    assert messages(research.records(cursor, "sync")) == []
    os.utime(root / "2020-03-02" / "search-2.md", (2_000_000_000, 2_000_000_000))
    changed = of(research.records(cursor, "sync"), RESULTS)
    assert [base(r["external_id"]) for r in changed] == ["invented/search-2/2020-03-02"]
    research.settings["since"] = "2020-03-02"
    backfill = of(research.records(cursor, "backfill"), RESULTS)
    assert {base(r["external_id"]) for r in backfill} == {
        "invented/search-2/2020-03-02",
        "invented/search-3/2020-03-02",
    }


# --------------------------------------------------------------------- CLI


def test_res_17_cli_export_then_plugin_maps_it(monkeypatch, tmp_path, provider):
    streams = write_stream(tmp_path)
    out = tmp_path / "exports" / "2020-03-01.jsonl"
    state = tmp_path / "state.json"
    env = {k: v for k, v in os.environ.items() if not k.startswith("RESEARCH_")}
    env.update(live_env(provider, streams))
    del env["RESEARCH_ALLOW_MODEL_CALLS"]
    command = [
        sys.executable,
        str(PACKAGE / "fetch.py"),
        "export",
        "--streams",
        str(streams),
        "--out",
        str(out),
        "--state",
        str(state),
        "--date",
        "2020-03-01",
    ]
    off = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60)
    assert off.returncode == 0 and provider.requests == []
    assert "model calls are off" in off.stderr
    done = subprocess.run(
        command + ["--allow-model-calls", "--max-calls", "3"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    assert CANARY not in done.stdout + done.stderr
    lines = out.read_text().splitlines()
    assert len(lines) == 3 and len(provider.requests) == 3
    assert CANARY not in out.read_text() and CANARY not in state.read_text()
    assert (
        json.loads(state.read_text())["last_run"]["invented/search-3"] == "2020-03-01"
    )
    raw = json.loads(lines[0])
    assert (
        raw["kind"] == "research.result" and raw["id"] == "invented/search-1/2020-03-01"
    )
    plan = subprocess.run(
        [
            sys.executable,
            str(PACKAGE / "fetch.py"),
            "plan",
            "--streams",
            str(streams),
            "--state",
            str(state),
            "--date",
            "2020-03-01",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    due = [json.loads(line)["due"] for line in plan.stdout.splitlines()]
    assert due == [False, False, False, True]
    mapped = messages(
        plugin(monkeypatch, RESEARCH_SOURCE=f"export:{out.parent}").records({}, "sync")
    )
    assert [m["external_id"] for m in mapped if m["channel"] == RESULTS] == [
        f"invented/search-{n}/2020-03-01" for n in (1, 2, 3)
    ]
    assert all("source" not in m["fields"] for m in mapped)


# ------------------------------------------------------ checker regressions


def test_res_18_export_keeps_written_records_when_run_stops(
    monkeypatch, tmp_path, provider
):
    """A 401 after two calls must not lose the two results the state marks done."""
    streams = write_stream(tmp_path)
    for key, value in live_env(provider, streams).items():
        monkeypatch.setenv(key, str(value))
    ok = provider.default({"messages": [{"content": "Invented"}]})
    provider.script = [ok, ok, (401, {}, {"error": "invented"})]
    out, state = tmp_path / "out" / "run.jsonl", tmp_path / "state.json"
    code = fetch.main(
        [
            "export",
            "--streams",
            str(streams),
            "--out",
            str(out),
            "--state",
            str(state),
            "--date",
            "2020-03-01",
        ]
    )
    assert code == 2 and len(provider.requests) == 3
    lines = out.read_text().splitlines()
    assert [json.loads(line)["search"]["id"] for line in lines] == [
        "search-1",
        "search-2",
    ]
    assert sorted(json.loads(state.read_text())["last_run"]) == [
        "invented/search-1",
        "invented/search-2",
    ]
    assert not (out.parent / "run.jsonl.partial").exists()
    # A run that fails before writing anything leaves an earlier file alone.
    provider.script = [(401, {}, {"error": "invented"})]
    assert (
        fetch.main(
            [
                "export",
                "--streams",
                str(streams),
                "--out",
                str(out),
                "--state",
                str(state),
                "--date",
                "2020-03-01",
            ]
        )
        == 2
    )
    assert out.read_text().splitlines() == lines


def test_res_19_unset_search_variable_fails_before_any_call(
    monkeypatch, tmp_path, provider
):
    streams = write_stream(
        tmp_path,
        searches=[
            {"id": "fine", "prompt": "Invented {city}"},
            {"id": "broken", "prompt": "Invented {not_set_anywhere}"},
        ],
    )
    research = plugin(monkeypatch, **live_env(provider, streams))
    with pytest.raises(fetch.ConfigError, match="not_set_anywhere"):
        list(research.records({}, "sync"))
    assert provider.requests == []


def test_res_20_imports_are_idempotent_and_never_collide(monkeypatch, tmp_path):
    root = tmp_path / "imports"
    day = root / "2020-03-01"
    day.mkdir(parents=True)
    # Two results for one search and day (for example two models), no timestamps.
    (day / "search-1.json").write_text(
        json.dumps({"search_id": "search-1", "text": "Invented answer one"})
    )
    (day / "search-1-other.json").write_text(
        json.dumps({"search_id": "search-1", "text": "Invented answer two"})
    )
    streams = write_stream(tmp_path, providers=[{"type": "import", "path": str(root)}])
    research = plugin(
        monkeypatch, RESEARCH_SOURCE="live", RESEARCH_STREAMS=str(streams)
    )
    first = of(research.records({}, "backfill"), RESULTS)
    ids = [r["external_id"] for r in first]
    assert len(ids) == 2 and len(set(ids)) == 2
    assert {base(i) for i in ids} == {"invented/search-1/2020-03-01"}
    # The record time comes from the dated path, not the file time.
    assert {r["happened_at"] for r in first} == {"2020-03-01T00:00:00+01:00"}
    for file in day.iterdir():
        os.utime(file, (2_000_000_000, 2_000_000_000))  # copied or touched
    again = of(research.records({}, "backfill"), RESULTS)
    assert again == first


def test_res_21_rewritten_export_file_is_mapped_again(monkeypatch, tmp_path):
    lines = (EXPORT / "2020-03-01.jsonl").read_text().splitlines()
    target = tmp_path / "exports"
    target.mkdir()
    file = target / "backfill.jsonl"
    file.write_text("\n".join(lines[:2]) + "\n", newline="\n")
    research = plugin(monkeypatch, RESEARCH_SOURCE=f"export:{target}")
    cursor = final_cursor(list(research.records({}, "sync")))
    # A later export rewrites the same file name with different leading lines.
    file.write_text("\n".join([lines[2], lines[0], lines[1]]) + "\n", newline="\n")
    remapped = of(research.records(cursor, "sync"), RESULTS)
    assert [m["external_id"] for m in remapped] == [
        "events/news-check-01/2020-03-01",
        "events/interest-01-a/2020-03-01",
        "events/interest-01-b/2020-03-01",
    ]
    # An old integer cursor still resumes by line count.
    legacy = {
        "mapping_revision": mapping.MAPPING_REVISION,
        "files": {"backfill.jsonl": 2},
    }
    assert len(of(research.records(legacy, "sync"), RESULTS)) == 1


class DryClient:
    def __init__(self):
        self.ingested = []

    def register(self, manifest):
        pass

    def cursor(self):
        raise AssertionError("a dry run must not read the cursor")

    def ingest(self, items, dry_run=False):
        assert dry_run
        self.ingested += items

    def heartbeat(self, **kwargs):
        raise AssertionError("a dry run must not heartbeat")


def test_res_22_dry_run_never_spends(monkeypatch, tmp_path, provider):
    root = tmp_path / "imports" / "2020-03-01"
    root.mkdir(parents=True)
    (root / "search-1.md").write_text("Invented imported finding")
    streams = write_stream(
        tmp_path,
        providers=[
            {"type": "import", "path": str(root.parent)},
            {"type": "openai", "web_search": "plugin"},
        ],
    )
    research = plugin(monkeypatch, **live_env(provider, streams))
    client = DryClient()
    report = research.execute(client, "backfill", dry_run=True)
    assert provider.requests == [] and report["counts"] == {RESULTS: 1}
    assert research.dry_run is False
    # A real run is still allowed to call.
    list(research.records({}, "sync"))
    assert len(provider.requests) == 3


def test_res_23_live_auth_failure_keeps_finished_results(
    monkeypatch, tmp_path, provider
):
    streams = write_stream(tmp_path)
    research = plugin(monkeypatch, **live_env(provider, streams))
    ok = provider.default({"messages": [{"content": "Invented"}]})
    provider.script = [ok, (403, {}, {"error": "invented"})]
    seen = []
    with pytest.raises(fetch.FetchError):
        for item in research.records({}, "sync"):
            seen.append(item)
    cursor = final_cursor(seen)
    assert [m["fields"]["search_id"] for m in of(seen, RESULTS)] == ["search-1"]
    assert cursor["live"]["last_run"] == {"invented/search-1": "2020-03-01"}
    assert sum(v for k, v in cursor["live"]["calls"].items() if "/" not in k) == 2
