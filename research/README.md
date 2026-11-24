# Throughline research plugin

Research streams: named lists of searches that run on a cadence. Each result is
recorded with its findings and cited URLs. Structured items found inside a result,
such as events with a date, venue and URL, are recorded as separate messages.

| Channel | One message per | Record time (`happened_at`) | Subject time |
|---|---|---|---|
| `research.results` | stream, search and run date | when the result was produced | the search window (day precision) |
| `research.items` | item inside a result | the result's time | when the item happens (exact or day precision) |

Both channels have `record_kind` `derived` and `is_outside` set. A result is model
output, so on its own it never counts as evidence. Each item carries provenance
pointing to the result it came from.

Links (all `method: source`):

- result `cites` a `web_page` entity for each cited URL
- item `found_in` its result
- item `about` an `event` (or other `item_type`) entity keyed by the item

The item key is the canonical URL plus the start time, or the title, start time and
venue when there is no URL. The same occurrence found by several searches or on
several days therefore links to one entity.

## Two layers

- `src/throughline_research/fetch.py` uses only the standard library (Python
  3.12 to 3.14, with no SDK, httpx or pydantic). It loads stream configs, decides
  which searches are due, calls the provider or imports finished results, and
  writes raw JSON Lines. It never maps records to messages.
- The plugin maps raw records to messages. It either calls fetch in-process
  (`RESEARCH_SOURCE=live`) or reads files written by fetch
  (`RESEARCH_SOURCE=export:<file or directory of .jsonl>`). Both paths share the
  same mapping, so either route produces identical messages for the same raw record.

Raw record (one JSON object per line):

```json
{"kind": "research.result", "schema": 1, "id": "<stream>/<search>/<run date>",
 "stream": {"id", "title", "description", "timezone", "revision"},
 "search": {"id", "label", "cluster", "variant", "kind", "output", "item_type", "recency"},
 "run_date": "YYYY-MM-DD", "window": {"start": "...", "end": "..."},
 "completed_at": "<ISO with offset>", "provider": {"type": "openai|import", ...},
 "prompt": "...", "prompt_hash": "...", "response": {...} | null,
 "imported": {"json": {...}, "text": "..."} | null}
```

The plugin ignores lines of any other `kind`.

## A stream is one file

Put JSON files in a directory, one stream per file (a file may also hold
`{"streams": [...]}`). YAML works only when PyYAML is installed. Example:

```json
{
  "id": "weekend-ideas",
  "title": "Weekend ideas",
  "timezone": "UTC",
  "cadence": "weekly",
  "window_days": 7,
  "output": "labeled_markdown",
  "item_type": "event",
  "variables": {"city": null},
  "providers": [{"type": "openai", "web_search": "plugin"}],
  "limits": {"max_calls_per_run": 5, "max_calls_per_day": 5},
  "searches": [
    {"id": "outdoors", "prompt": "Outdoor events in {city} from {window_start} to {window_end}."}
  ]
}
```

- `cadence`: `daily`, `weekly`, `monthly`, `manual` or a number of days. A
  search may set its own value. A search runs at most once per run date.
- Window: `window_days` ahead of and `lookback_days` before the run date, on
  the stream or on a search. Use lookback for checks such as "the last 7 days".
- `output`: `markdown` (text only), `labeled_markdown` (one `###` heading per
  item, with `Label: value` lines such as `Event`, `Occurrence start`, `Venue`,
  `City`, `Canonical URL`, `Price`, `Primary confirmed`), or `json` (an object with
  `items`/`events`/`results`, an optional `summary`, or a fenced JSON block).
  Set `"json_mode": true` on a search to request `response_format`
  `json_object`. Unknown labels are kept in the item's `details` field.
- `matrix`: generates one search per group and variant with the id
  `<group>-<variant>`. Group keys become variables (`{group_label}` and any
  others). Add explicit `searches` alongside the matrix.
- Variables: `{today}`, `{run_date}`, `{weekday}`, `{timezone}`,
  `{window_start}`, `{window_end}`, `{stream}`, `{search_id}`, `{cluster}`,
  `{variant}`, plus stream and search `variables`. Use `{{` and `}}` for literal
  braces. A variable set to `null` is required. Set it with
  `RESEARCH_VAR_<NAME>` (for example `RESEARCH_VAR_CITY`) so personal values stay
  out of config files. Precedence: built-ins, then stream values, then
  `RESEARCH_VAR_*`, then search values. A prompt with an unset variable fails
  before any call of that stream is made.
- `system`: an optional system prompt (grounding and output rules).

### Providers

List them in order under `providers`. Import providers run first in the order
listed. A model provider then runs only the searches the imports did not answer for
today's run date, so no spend is repeated.

1. `{"type": "openai", ...}`: an OpenAI-compatible endpoint. It is off unless
   `RESEARCH_ALLOW_MODEL_CALLS=1` and `RESEARCH_OPENAI_BASE_URL`,
   `RESEARCH_OPENAI_API_KEY` and a model are set. Options:
   - `endpoint`: `chat` (`/chat/completions`) or `responses` (`/responses`).
   - `web_search`: `none` (use a search-capable model), `options`
     (`web_search_options`, as OpenAI search models expect), `plugin`
     (`plugins: [{"id": "web"}]`, as OpenRouter expects) or `tool`
     (`tools: [{"type": "web_search"}]`).
   - `model`, `system`, and `request` (extra body fields merged as given).
   Citations come from `message.annotations` (`url_citation`), responses-API
   annotations, top-level `citations` or `search_results`, and then from URLs
   in the text.
2. `{"type": "import", "path": "${SOME_DIR}", ...}`: results produced elsewhere,
   such as a directory of cached per-search JSON or daily run folders. The path
   may use environment variables. Options:
   - `glob` (default `**/*.json` and `**/*.md`).
   - `records` (a JSON pointer to a list inside each file).
   - `id_pattern` (a regex with an `id` group applied to file stems).
   - `fields` (overrides for the keys to read: `search_id`, `run_date`,
     `completed_at`, `prompt`, `model`, `text`, `citations`).
   A `.json` file with a `.md` sibling of the same stem takes its text from the
   Markdown file. The run date comes from a key, then a timestamp key, then a
   `YYYY-MM-DD` in the path, then the file time. Sync reads files changed since
   the last pass (by modification time). Backfill rereads every file within
   `RESEARCH_SINCE` and `RESEARCH_UNTIL`. Files copied with their original
   modification times preserved need a backfill.

### Example: `events`

`src/throughline_research/examples/events.json` follows the shape of a daily
local-event search pack. It has 35 interest groups, each with an anchored (A) and
an open (B) variant, plus 3 current checks, for 73 searches. It also carries the
grounding rules and the labeled output format. Every personal value is a
placeholder:

- the `<interest NN>` labels and anchors, and the `<news topic NN>` topics: edit
  these in a copy of the file
- `city`, `secondary_city` and `region`: set these with `RESEARCH_VAR_*`

To convert a real pack, run `python fetch.py convert-pack --pack PACK.json --out
events.json`. The pack's items have the shape `{id, cluster, kind, variant,
variant_label, query}`. Template variables not built in become required stream
variables. Do this on the host where the pack lives, and do not commit the result.

## Settings

| Env | Default | Meaning |
|---|---|---|
| `RESEARCH_SOURCE` | `live` | `live` or `export:<file or directory>` |
| `RESEARCH_STREAMS` | none | Stream file or directory (live mode) |
| `RESEARCH_SINCE` / `RESEARCH_UNTIL` | none | Run-date bounds (inclusive) for mapping, imports and calls |
| `RESEARCH_DATE` | today in the stream's timezone | Run-date override |
| `RESEARCH_BATCH` | 25 | Raw records per checkpoint |
| `RESEARCH_CACHE_DIR` | none | Keep each call's raw record at `<dir>/<stream>/<date>/<search>.json` and reuse it |
| `RESEARCH_ALLOW_MODEL_CALLS` | 0 | Set to `1` to allow paid model calls |
| `RESEARCH_MAX_CALLS_PER_RUN` | 10 | Model calls allowed in one run (all streams) |
| `RESEARCH_MAX_CALLS_PER_DAY` | 80 | Model calls allowed per UTC day (ledger in the cursor or `--state`) |
| `RESEARCH_MIN_INTERVAL_S` | 1 | Pause between model calls |
| `RESEARCH_TIMEOUT_S` | 150 | Model call timeout |
| `RESEARCH_OPENAI_BASE_URL` | none | For example `https://<provider host>/v1` |
| `RESEARCH_OPENAI_MODEL` | none | Model name (a provider's `model` overrides it) |
| `RESEARCH_OPENAI_WEB_SEARCH` | none | Overrides every provider's `web_search` mode |
| `RESEARCH_OPENAI_API_KEY` | none | Secret. Sent only in the `Authorization` header and never logged |
| `RESEARCH_VAR_<NAME>` | none | Prompt variable `{name}` |

A stream's `limits` (`max_calls_per_run`, `max_calls_per_day`,
`max_output_tokens`, `timeout_s`, `min_interval_s`) tighten the global limits for
that stream. Searches over a limit wait for the next run. Requests retry with
exponential backoff on 429, 5xx and transport errors, honoring `Retry-After`
(4 retries). On 401 or 403 the run stops. Other client errors skip the search,
which stays due. Error messages carry only the HTTP status.

## Commands

```powershell
uv run --package throughline-research throughline-research manifest
$env:THROUGHLINE_URL = 'http://localhost:8000'
$env:THROUGHLINE_TOKEN = '<plugin token for research>'
$env:RESEARCH_STREAMS = '<path to streams directory>'
uv run --package throughline-research throughline-research sync --once
```

Live runs keep their cadence, the call ledger and the import positions in the
plugin cursor. A `--dry-run` starts from an empty cursor, so it never makes
model calls (imports still run). A crash between a paid call and the next checkpoint repeats that
call unless `RESEARCH_CACHE_DIR` is set, so set it for model streams.

## Export workflow (credentials stay on the remote host)

On the host that holds the credentials and caches (Python 3.12 or newer, no
packages needed), copy `fetch.py` and the stream files, then run:

```sh
export RESEARCH_OPENAI_BASE_URL='https://<provider host>/v1'
export RESEARCH_OPENAI_MODEL='<model>'
export RESEARCH_OPENAI_API_KEY="$(<your secret reader> RESEARCH_OPENAI_API_KEY)"
export RESEARCH_VAR_CITY='<city>'
export RESEARCH_IMPORT_DIR='<directory of results produced elsewhere>'
python3 fetch.py validate --streams streams/
python3 fetch.py plan --streams streams/ --state research-state.json --prompts
python3 fetch.py export --streams streams/ --state research-state.json \
  --cache-dir research-cache --out exports/research-$(date +%F).jsonl
# with --allow-model-calls (and optionally --max-calls N) to permit paid calls;
# --mode backfill --since 2026-01-01 re-imports every cached result in range.
```

`export` writes to `<out>.partial` and renames it when done (`--append` appends
instead). If the run stops early (for example on HTTP 401), the records already
written, which the state file already marks done, are still moved to `<out>`;
the summary then says `"complete": false`. It prints only a JSON summary to
stderr. The state file holds cadence
and the per-day ledger. It never holds credentials. Ship the export files back,
then map them locally:

```powershell
$env:RESEARCH_SOURCE = 'export:<directory with the .jsonl files>'
uv run --package throughline-research throughline-research backfill --dry-run
uv run --package throughline-research throughline-research backfill
```

Export mode keeps, per file name, the number of lines mapped and a hash of
those lines in the cursor. A file rewritten under the same name (a rerun
export) no longer matches the hash and is mapped again from the start. It skips
a final line that has no newline yet, because that file is still being written. When
`MAPPING_REVISION` changes, every line is mapped again once. Unchanged output
does nothing at ingest. Changed output becomes one new version.

## Determinism and limits

- External ids: `<stream>/<search>/<run date>` for model results,
  `<stream>/<search>/<run date>/import-<hash of file path and entry>` for
  imported results (so two files answering the same search and day, such as
  two models or a cache and a bundle, never overwrite each other), and
  `<result id>#<item key>` for items. A repeat of the same search on the same
  date updates that one message as a new version.
- The mapping depends only on the raw record: the same record read live or
  from an export gives identical messages. An imported entry without a
  timestamp takes its record time from the run date (midnight in the stream
  timezone), never from the file time, unless neither the entry nor its path
  holds a date.
- Every message carries `mapping_revision`. The full raw record is the result's
  payload.
- Content is capped at 200,000 characters. The payload always keeps the full text.
- Imported files over 20 MB are skipped.
- No model call is made unless it is explicitly enabled. There is no
  per-token cost accounting; use the per-run and per-day call limits.

## Tests

`uv run --no-sync pytest -q plugins/research` covers:

- the contract kit on a synthetic export
- mapping of labeled Markdown, JSON and responses-API results
- a fake local OpenAI-compatible server: request shape, limits, cadence,
  retries, auth failure and the cache
- import directories and watermarks
- the stdlib-only `fetch.py` CLI end to end
- pack conversion and the example stream

All fixtures are invented.
