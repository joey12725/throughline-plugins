"""Research stream fetcher. Standard library only; runs on Python 3.12 to 3.14.

A research stream is one config file (JSON, or YAML when PyYAML happens to be
installed) naming a list of searches. This module runs the searches that are
due and writes one raw record per result as JSON Lines. It never maps records
to Throughline messages; the plugin does that, live (in-process) or from the
exported file.

    python fetch.py export --streams STREAMS --out FILE [options]
    python fetch.py plan --streams STREAMS [--date YYYY-MM-DD]
    python fetch.py validate --streams STREAMS
    python fetch.py convert-pack --pack PACK.json --out STREAM.json

Credentials come only from the environment and are never printed or written:
RESEARCH_OPENAI_API_KEY (with RESEARCH_OPENAI_BASE_URL and RESEARCH_OPENAI_MODEL).
Model calls are off unless RESEARCH_ALLOW_MODEL_CALLS=1 (or --allow-model-calls).
Prompt variables can be set with RESEARCH_VAR_<NAME> (for example
RESEARCH_VAR_CITY), so personal values stay out of config files.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

SCHEMA = 1
RECORD_KIND = "research.result"
USER_AGENT = "throughline-research/0.1"
SLUG = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
VARIABLE = re.compile(r"\{\{|\}\}|\{([A-Za-z_][A-Za-z0-9_]*)\}")
DAY = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)")
CADENCES = {"daily": 1, "weekly": 7, "monthly": 30, "manual": 0}
WEB_SEARCH_MODES = ("none", "options", "plugin", "tool")
OUTPUTS = ("markdown", "labeled_markdown", "json")
TEXT_KEYS = (
    "text",
    "markdown",
    "content",
    "answer",
    "output_text",
    "findings",
    "result",
    "response",
)
TIME_KEYS = ("completed_at", "created_at", "generated_at", "fetched_at", "timestamp")
DATE_KEYS = ("run_date", "date", "day")
ID_KEYS = ("search_id", "id", "search", "query_id")
PROMPT_KEYS = ("prompt", "query")
MAX_IMPORT_BYTES = 20 * 1024 * 1024
# Sleep is a module attribute so tests can make backoff instantaneous.
SLEEP = time.sleep


class ConfigError(ValueError):
    """A stream file or setting is invalid."""


class FetchError(RuntimeError):
    """A provider request failed. Messages never include credentials or bodies."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def truthy(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def digest(value, length=16):
    if not isinstance(value, (str, bytes)):
        value = json.dumps(value, sort_keys=True, ensure_ascii=False)
    if isinstance(value, str):
        value = value.encode()
    return hashlib.sha256(value).hexdigest()[:length]


def parse_day(value, name="date"):
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise ConfigError(f"{name} must be YYYY-MM-DD") from None


def zone(name):
    """A tzinfo for an IANA name; UTC when the name is UTC or empty."""
    if not name or name.upper() == "UTC":
        return UTC
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        raise ConfigError(
            f"unknown timezone {name!r} (install tzdata on this host)"
        ) from None


# ---------------------------------------------------------------- configs


def read_config(path):
    text = Path(path).read_text(encoding="utf-8")
    if Path(path).suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml  # optional; never a hard dependency
        except ImportError:
            raise ConfigError(
                f"{Path(path).name}: YAML needs PyYAML; use JSON instead"
            ) from None
        return yaml.safe_load(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise ConfigError(f"{Path(path).name}: invalid JSON ({error})") from None


def config_files(path):
    path = Path(path)
    if path.is_dir():
        return sorted(
            p
            for p in path.rglob("*")
            if p.is_file()
            and p.suffix.lower() in (".json", ".yaml", ".yml")
            and not p.name.startswith((".", "_"))
        )
    if not path.exists():
        raise ConfigError(f"stream config not found: {path}")
    return [path]


def load_streams(path):
    """Every stream in a file or a directory of files, validated and expanded."""
    streams = []
    for file in config_files(path):
        data = read_config(file)
        if isinstance(data, dict) and isinstance(data.get("streams"), list):
            data = data["streams"]
        for raw in data if isinstance(data, list) else [data]:
            streams.append(normalize_stream(raw, origin=file.name))
    seen = set()
    for stream in streams:
        if stream["id"] in seen:
            raise ConfigError(f"duplicate stream id {stream['id']!r}")
        seen.add(stream["id"])
    return streams


def cadence_days(value, where):
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("days")
    if isinstance(value, str) and value.lower() in CADENCES:
        return CADENCES[value.lower()]
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    raise ConfigError(
        f"{where}: cadence must be daily, weekly, monthly, manual or a number of days"
    )


def non_negative(value, where, default):
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(f"{where} must be a non-negative integer")
    return value


def normalize_provider(raw, where):
    if isinstance(raw, str):
        raw = {"type": raw}
    if not isinstance(raw, dict) or raw.get("type") not in ("openai", "import"):
        raise ConfigError(f"{where}: provider type must be openai or import")
    provider = dict(raw)
    if provider["type"] == "import":
        if not provider.get("path"):
            raise ConfigError(f"{where}: import provider needs a path")
        provider.setdefault("glob", ["**/*.json", "**/*.md"])
        if isinstance(provider["glob"], str):
            provider["glob"] = [provider["glob"]]
        provider.setdefault("fields", {})
        if not isinstance(provider["fields"], dict):
            raise ConfigError(f"{where}: import fields must be an object")
    else:
        provider.setdefault("endpoint", "chat")
        provider.setdefault("web_search", "none")
        if provider["endpoint"] not in ("chat", "responses"):
            raise ConfigError(f"{where}: endpoint must be chat or responses")
        if provider["web_search"] not in WEB_SEARCH_MODES:
            raise ConfigError(
                f"{where}: web_search must be one of {', '.join(WEB_SEARCH_MODES)}"
            )
        if not isinstance(provider.get("request", {}), dict):
            raise ConfigError(f"{where}: request must be an object of body fields")
    return provider


def normalize_search(raw, stream, where):
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: a search must be an object")
    search_id = str(raw.get("id") or "")
    if not SLUG.match(search_id):
        raise ConfigError(f"{where}: search id {search_id!r} must be a lowercase slug")
    prompt = raw.get("prompt", raw.get("query"))
    if not isinstance(prompt, str) or not prompt.strip():
        raise ConfigError(f"{where}: search {search_id} needs a prompt")
    output = raw.get("output", stream["output"])
    if output not in OUTPUTS:
        raise ConfigError(f"{where}: output must be one of {', '.join(OUTPUTS)}")
    variables = raw.get("variables") or {}
    if not isinstance(variables, dict):
        raise ConfigError(f"{where}: variables must be an object")
    return {
        "id": search_id,
        "label": str(raw.get("label") or search_id),
        "cluster": raw.get("cluster"),
        "variant": raw.get("variant"),
        "kind": raw.get("kind") or "search",
        "prompt": prompt,
        "output": output,
        "json_mode": bool(raw.get("json_mode", False)),
        "item_type": raw.get("item_type", stream["item_type"]),
        "recency": raw.get("recency"),
        "cadence": cadence_days(raw.get("cadence"), where),
        "window_days": non_negative(
            raw.get("window_days"), f"{where}: window_days", None
        ),
        "lookback_days": non_negative(
            raw.get("lookback_days"), f"{where}: lookback_days", None
        ),
        "variables": variables,
    }


def expand_matrix(matrix, stream, where):
    """groups x variants: one search per pair, id '<group>-<variant>'."""
    if not isinstance(matrix, dict):
        raise ConfigError(f"{where}: matrix must be an object")
    groups, variants = matrix.get("groups") or [], matrix.get("variants") or []
    if not isinstance(groups, list) or not isinstance(variants, list):
        raise ConfigError(f"{where}: matrix groups and variants must be lists")
    searches = []
    for group in groups:
        if not isinstance(group, dict) or not group.get("id"):
            raise ConfigError(f"{where}: every matrix group needs an id")
        for variant in variants:
            if not isinstance(variant, dict) or not variant.get("id"):
                raise ConfigError(f"{where}: every matrix variant needs an id")
            group_vars = {
                k: v for k, v in group.items() if k not in ("id", "variables")
            }
            group_vars.update(group.get("variables") or {})
            label = group.get("label") or group["id"]
            searches.append(
                {
                    **{k: v for k, v in variant.items() if k not in ("id", "label")},
                    "id": f"{group['id']}-{variant['id']}",
                    "label": f"{label} ({variant.get('label') or variant['id']})",
                    "cluster": group["id"],
                    "variant": variant.get("name", variant["id"]),
                    "variables": {
                        **group_vars,
                        "group_id": group["id"],
                        "group_label": label,
                        **(variant.get("variables") or {}),
                    },
                }
            )
    return searches


def normalize_stream(raw, origin="stream"):
    if not isinstance(raw, dict):
        raise ConfigError(f"{origin}: a stream must be an object")
    stream_id = str(raw.get("id") or "")
    if not SLUG.match(stream_id):
        raise ConfigError(f"{origin}: stream id {stream_id!r} must be a lowercase slug")
    where = f"{origin}:{stream_id}"
    zone(raw.get("timezone") or "UTC")
    output = raw.get("output", "markdown")
    if output not in OUTPUTS:
        raise ConfigError(f"{where}: output must be one of {', '.join(OUTPUTS)}")
    providers = raw.get("providers", raw.get("provider", []))
    if not isinstance(providers, list):
        providers = [providers]
    variables = raw.get("variables") or {}
    if not isinstance(variables, dict):
        raise ConfigError(f"{where}: variables must be an object")
    limits = raw.get("limits") or {}
    if not isinstance(limits, dict):
        raise ConfigError(f"{where}: limits must be an object")
    stream = {
        "id": stream_id,
        "title": str(raw.get("title") or stream_id),
        "description": raw.get("description"),
        "timezone": raw.get("timezone") or "UTC",
        "cadence": cadence_days(raw.get("cadence", "daily"), where),
        "window_days": non_negative(raw.get("window_days"), f"{where}: window_days", 7),
        "lookback_days": non_negative(
            raw.get("lookback_days"), f"{where}: lookback_days", 0
        ),
        "output": output,
        "item_type": raw.get("item_type"),
        "system": raw.get("system"),
        "variables": variables,
        "providers": [
            normalize_provider(p, f"{where}: provider {i}")
            for i, p in enumerate(providers)
        ],
        "limits": {
            key: non_negative(limits.get(key), f"{where}: limits.{key}", None)
            for key in (
                "max_calls_per_run",
                "max_calls_per_day",
                "max_output_tokens",
                "timeout_s",
            )
        },
        "revision": digest(raw, 12),
    }
    if limits.get("min_interval_s") is not None:
        stream["limits"]["min_interval_s"] = float(limits["min_interval_s"])
    searches = expand_matrix(raw["matrix"], stream, where) if raw.get("matrix") else []
    searches += list(raw.get("searches") or [])
    stream["searches"] = [normalize_search(s, stream, where) for s in searches]
    ids = [s["id"] for s in stream["searches"]]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise ConfigError(f"{where}: duplicate search ids {duplicates}")
    return stream


# ------------------------------------------------------------- variables


def env_variables(env):
    return {
        key[len("RESEARCH_VAR_") :].lower(): value
        for key, value in env.items()
        if key.startswith("RESEARCH_VAR_") and value != ""
    }


def window(stream, search, run_date):
    ahead = search.get("window_days")
    ahead = stream["window_days"] if ahead is None else ahead
    back = search.get("lookback_days")
    back = stream["lookback_days"] if back is None else back
    return {
        "start": (run_date - timedelta(days=back)).isoformat(),
        "end": (run_date + timedelta(days=ahead)).isoformat(),
    }


def variables_for(stream, search, run_date, env):
    """Built-ins < stream variables < RESEARCH_VAR_* < search variables."""
    span = window(stream, search, run_date)
    values = {
        "today": run_date.isoformat(),
        "run_date": run_date.isoformat(),
        "weekday": run_date.strftime("%A"),
        "timezone": stream["timezone"],
        "window_start": span["start"],
        "window_end": span["end"],
        "stream": stream["id"],
        "search_id": search["id"],
        "cluster": search.get("cluster") or "",
        "variant": search.get("variant") or "",
    }
    values.update({k: v for k, v in stream["variables"].items() if v is not None})
    values.update(env_variables(env))
    values.update({k: v for k, v in search["variables"].items() if v is not None})
    return values


def render(template, values):
    """Fill {name} placeholders; {{ and }} are literal braces.

    Raises ConfigError naming every unset variable, so a stream whose personal
    values were left as placeholders never reaches a paid call.
    """
    missing = set()

    def substitute(match):
        token = match.group(0)
        if token in ("{{", "}}"):
            return token[0]
        name = match.group(1)
        if name not in values:
            missing.add(name)
            return token
        value = values[name]
        return value if isinstance(value, str) else json.dumps(value)

    text = VARIABLE.sub(substitute, template)
    if missing:
        names = ", ".join(sorted(missing))
        raise ConfigError(
            f"unset prompt variables: {names} (set them in the stream or as RESEARCH_VAR_<NAME>)"
        )
    return text


# ---------------------------------------------------------------- options


@dataclass
class Options:
    """Run settings. The API key is held here only for the call itself."""

    allow_model_calls: bool = False
    base_url: str | None = None
    api_key: str | None = field(default=None, repr=False)
    model: str | None = None
    web_search: str | None = None
    max_calls_per_run: int = 10
    max_calls_per_day: int = 80
    timeout_s: float = 150.0
    min_interval_s: float = 1.0
    retries: int = 4
    cache_dir: str | None = None
    run_date: date | None = None
    since: date | None = None
    until: date | None = None
    only_streams: tuple = ()
    only_searches: tuple = ()
    force: bool = False
    log: object = None
    # Filled in by a run: provider calls made and the ones that failed
    # ("stream/search"), so the caller can tell an outage from a quiet run.
    calls_attempted: int = 0
    call_failures: list = field(default_factory=list)

    @classmethod
    def from_env(cls, env=None, **overrides):
        env = os.environ if env is None else env

        def number(name, default, kind=int):
            value = env.get(name)
            if value in (None, ""):
                return default
            try:
                return kind(value)
            except ValueError:
                raise ConfigError(f"{name} must be a number") from None

        options = cls(
            allow_model_calls=truthy(env.get("RESEARCH_ALLOW_MODEL_CALLS")),
            base_url=env.get("RESEARCH_OPENAI_BASE_URL") or None,
            api_key=env.get("RESEARCH_OPENAI_API_KEY") or None,
            model=env.get("RESEARCH_OPENAI_MODEL") or None,
            web_search=env.get("RESEARCH_OPENAI_WEB_SEARCH") or None,
            max_calls_per_run=number("RESEARCH_MAX_CALLS_PER_RUN", 10),
            max_calls_per_day=number("RESEARCH_MAX_CALLS_PER_DAY", 80),
            timeout_s=number("RESEARCH_TIMEOUT_S", 150.0, float),
            min_interval_s=number("RESEARCH_MIN_INTERVAL_S", 1.0, float),
            cache_dir=env.get("RESEARCH_CACHE_DIR") or None,
            run_date=parse_day(env.get("RESEARCH_DATE"), "RESEARCH_DATE"),
            since=parse_day(env.get("RESEARCH_SINCE"), "RESEARCH_SINCE"),
            until=parse_day(env.get("RESEARCH_UNTIL"), "RESEARCH_UNTIL"),
        )
        for key, value in overrides.items():
            if value is not None:
                setattr(options, key, value)
        if options.web_search and options.web_search not in WEB_SEARCH_MODES:
            raise ConfigError("RESEARCH_OPENAI_WEB_SEARCH is not a known mode")
        return options

    def note(self, text):
        if self.log:
            self.log(text)

    def model_ready(self):
        return bool(self.allow_model_calls and self.base_url and self.api_key)


def in_bounds(day, options):
    return not (
        (options.since and day < options.since)
        or (options.until and day > options.until)
    )


# ------------------------------------------------------------------- HTTP


def post_json(url, body, headers, *, timeout, retries, note=None):
    """POST JSON with bounded exponential backoff on 429, 5xx and transport errors.

    Errors carry only the HTTP status, never the URL, headers or response body.
    """
    data = json.dumps(body).encode()
    for attempt in range(retries + 1):
        request = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
                **headers,
            },
        )
        wait = min(60.0, 2.0 * 2**attempt)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read()
            try:
                return json.loads(payload)
            except ValueError:
                raise FetchError("provider returned invalid JSON") from None
        except urllib.error.HTTPError as error:
            status = error.code
            retry_after = error.headers.get("Retry-After") if error.headers else None
            error.close()
            if status != 429 and status < 500:
                raise FetchError(
                    f"provider request failed (HTTP {status})", status
                ) from None
            if retry_after and retry_after.strip().isdigit():
                wait = min(120.0, float(retry_after.strip()))
            if attempt == retries:
                raise FetchError(
                    f"provider request failed after retries (HTTP {status})", status
                ) from None
            if note:
                note(f"provider HTTP {status}; retrying in {wait:.0f}s")
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
            if attempt == retries:
                raise FetchError(
                    f"provider transport failed ({type(error).__name__})"
                ) from None
            if note:
                note(f"provider transport error; retrying in {wait:.0f}s")
        SLEEP(wait)
    raise FetchError("provider request failed")


def build_request(stream, search, prompt, provider, options):
    endpoint = provider.get("endpoint", "chat")
    model = provider.get("model") or options.model
    if not model:
        raise ConfigError("no model: set RESEARCH_OPENAI_MODEL or provider.model")
    mode = options.web_search or provider.get("web_search", "none")
    system = provider.get("system", stream.get("system"))
    max_tokens = stream["limits"].get("max_output_tokens")
    if endpoint == "responses":
        body = {"model": model, "input": prompt}
        if system:
            body["instructions"] = system
        if max_tokens:
            body["max_output_tokens"] = max_tokens
        if mode == "tool":
            body["tools"] = [{"type": provider.get("tool_type", "web_search")}]
        if search.get("json_mode"):
            body["text"] = {"format": {"type": "json_object"}}
    else:
        messages = [{"role": "system", "content": system}] if system else []
        messages.append({"role": "user", "content": prompt})
        body = {"model": model, "messages": messages}
        if max_tokens:
            body["max_tokens"] = max_tokens
        if mode == "options":
            body["web_search_options"] = provider.get("web_search_options", {})
        elif mode == "plugin":
            body["plugins"] = [{"id": "web", **provider.get("plugin_options", {})}]
        elif mode == "tool":
            body["tools"] = [{"type": provider.get("tool_type", "web_search")}]
        if search.get("json_mode"):
            body["response_format"] = {"type": "json_object"}
    body.update(provider.get("request") or {})
    path = "/responses" if endpoint == "responses" else "/chat/completions"
    return path, body, {"model": model, "endpoint": endpoint, "web_search": mode}


# ------------------------------------------------------------------ state


def fresh_state(state):
    state.setdefault("last_run", {})
    state.setdefault("calls", {})
    state.setdefault("import_mtime", {})
    state.setdefault("import_pos", {})
    state.setdefault("import_edge", {})
    return state


def prune(state, today):
    keep = {(today - timedelta(days=n)).isoformat() for n in range(8)}
    state["calls"] = {
        k: v for k, v in state["calls"].items() if k.split("/")[0] in keep
    }


def is_due(stream, search, run_date, state, force=False):
    last = state["last_run"].get(f"{stream['id']}/{search['id']}")
    if last == run_date.isoformat():
        return False
    if force:
        return True
    cadence = search["cadence"] if search["cadence"] is not None else stream["cadence"]
    if cadence == 0:
        return False
    return last is None or (run_date - date.fromisoformat(last)).days >= cadence


def make_record(stream, search, run_date, completed_at, provider, prompt, **extra):
    record = {
        "kind": RECORD_KIND,
        "schema": SCHEMA,
        "id": f"{stream['id']}/{search['id']}/{run_date.isoformat()}",
        "stream": {
            key: stream.get(key)
            for key in ("id", "title", "description", "timezone", "revision")
        },
        "search": {
            key: search.get(key)
            for key in (
                "id",
                "label",
                "cluster",
                "variant",
                "kind",
                "output",
                "item_type",
                "recency",
            )
        },
        "run_date": run_date.isoformat(),
        "window": window(stream, search, run_date),
        "completed_at": completed_at,
        "provider": provider,
        "prompt": prompt,
        "prompt_hash": digest(prompt) if prompt else None,
        "response": None,
        "imported": None,
    }
    record.update(extra)
    return record


# ------------------------------------------------------------- providers


def lookup(entry, names):
    for name in names:
        if name.startswith("/"):
            value = entry
            for part in name.strip("/").split("/"):
                if isinstance(value, dict):
                    value = value.get(part.replace("~1", "/").replace("~0", "~"))
                elif (
                    isinstance(value, list)
                    and part.isdigit()
                    and int(part) < len(value)
                ):
                    value = value[int(part)]
                else:
                    value = None
                    break
        else:
            value = entry.get(name) if isinstance(entry, dict) else None
        if value not in (None, ""):
            return value
    return None


def field_names(provider, key, default):
    configured = provider["fields"].get(key)
    if configured is None:
        return default
    return [configured] if isinstance(configured, str) else list(configured)


def instant(value, tz):
    """ISO timestamp with offset from an ISO string or epoch seconds/ms."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = value / 1000 if value > 10**11 else value
        return datetime.fromtimestamp(seconds, UTC).isoformat()
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.isoformat()


def import_files(root, patterns):
    seen = {}
    for pattern in patterns:
        for path in root.glob(pattern):
            if path.is_file():
                seen[path.relative_to(root).as_posix()] = path
    return [seen[k] for k in sorted(seen)]


def run_import(stream, provider, state, options, mode):
    """Results produced elsewhere: JSON objects, JSON lists, or Markdown files.

    A .json file with a .md sibling of the same stem takes its text from the
    Markdown. Sync reads files changed since the last pass; backfill reads all.
    Records are bounded by run date with since/until.
    """
    root = Path(os.path.expandvars(os.path.expanduser(str(provider["path"]))))
    if not root.is_dir():
        options.note(f"{stream['id']}: import directory is missing; skipped")
        return
    tz = zone(stream["timezone"])
    key = f"{stream['id']}/" + digest(
        [str(provider["path"]), provider["glob"], provider.get("records")], 8
    )
    watermark, edge = 0, set()
    if mode == "sync":
        watermark = state["import_mtime"].get(key, 0)
        edge = set(state["import_edge"].get(key, []))
    position = state["import_pos"].get(key)
    newest, newest_files = watermark, set(edge)
    searches = {s["id"]: s for s in stream["searches"]}
    id_pattern = (
        re.compile(provider["id_pattern"]) if provider.get("id_pattern") else None
    )
    for path in import_files(root, provider["glob"]):
        rel = path.relative_to(root).as_posix()
        if position is not None and rel <= position:
            continue
        info = path.stat()
        if info.st_mtime_ns > newest:
            newest, newest_files = info.st_mtime_ns, {rel}
        elif info.st_mtime_ns == newest:
            newest_files.add(rel)
        if info.st_mtime_ns < watermark or (
            info.st_mtime_ns == watermark and rel in edge
        ):
            continue
        if path.suffix.lower() == ".md" and path.with_suffix(".json").exists():
            continue
        if info.st_size > MAX_IMPORT_BYTES:
            options.note(f"{stream['id']}: {rel} is too large to import; skipped")
            continue
        content = path.read_bytes()
        text = None
        if path.suffix.lower() == ".json":
            try:
                document = json.loads(content)
            except ValueError:
                options.note(f"{stream['id']}: {rel} is not valid JSON; skipped")
                continue
            sibling = path.with_suffix(".md")
            if sibling.exists():
                text = sibling.read_text(encoding="utf-8", errors="replace")
            if provider.get("records"):
                document = lookup(document, [provider["records"]]) or []
            entries = document if isinstance(document, list) else [document]
        else:
            text = content.decode("utf-8", errors="replace")
            entries = [None]
        mtime = datetime.fromtimestamp(info.st_mtime, UTC)
        for index, entry in enumerate(entries):
            if entry is not None and not isinstance(entry, dict):
                continue
            entry = entry or {}
            stem = path.stem
            if id_pattern and id_pattern.search(stem):
                stem = id_pattern.search(stem).group("id")
            search_id = lookup(entry, field_names(provider, "search_id", ID_KEYS))
            search_id = str(search_id or stem)
            search = searches.get(search_id) or {
                "id": search_id,
                "label": search_id,
                "kind": "imported",
                "output": stream["output"],
                "item_type": stream["item_type"],
                "window_days": None,
                "lookback_days": None,
            }
            stamp = lookup(entry, field_names(provider, "completed_at", TIME_KEYS))
            completed = instant(stamp, tz) if stamp is not None else None
            day = lookup(entry, field_names(provider, "run_date", DATE_KEYS))
            run_date, from_mtime = None, False
            if day:
                try:
                    run_date = parse_day(day)
                except ConfigError:
                    run_date = None
            if run_date is None and completed:
                run_date = datetime.fromisoformat(completed).astimezone(tz).date()
            if run_date is None:
                found = DAY.findall(rel)
                if found:
                    run_date = date.fromisoformat(found[-1])
                else:
                    run_date, from_mtime = mtime.astimezone(tz).date(), True
            if not in_bounds(run_date, options):
                continue
            if completed is None:
                # File times change when files are copied or touched, which
                # would make a new version on every re-import; use them only
                # when neither the entry nor its path carries a date.
                completed = (
                    mtime.isoformat()
                    if from_mtime
                    else datetime.combine(run_date, datetime.min.time(), tz).isoformat()
                )
            prompt = lookup(entry, field_names(provider, "prompt", PROMPT_KEYS))
            record = make_record(
                stream,
                search,
                run_date,
                completed,
                {
                    "type": "import",
                    "path": rel,
                    "entry": index if len(entries) > 1 else None,
                    "sha256": digest(content, 64),
                    "model": lookup(entry, field_names(provider, "model", ["model"])),
                    "fields": provider["fields"],
                },
                prompt if isinstance(prompt, str) else None,
                imported={"json": entry or None, "text": text},
            )
            # One message per imported file entry: two files answering the same
            # search and day (another model, a cache and a bundle) must not
            # overwrite each other, which would add versions on every pass.
            record["id"] += "/import-" + digest(f"{rel}#{index}", 10)
            yield record
        state["import_pos"][key] = rel
    state["import_mtime"][key] = newest
    state["import_edge"][key] = sorted(newest_files)
    state["import_pos"].pop(key, None)


def cache_path(options, stream, search, run_date):
    return (
        Path(options.cache_dir)
        / stream["id"]
        / run_date.isoformat()
        / f"{search['id']}.json"
    )


def write_atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


class Budget:
    """Per-run and per-day call limits, global and per stream."""

    def __init__(self, options, state, today):
        self.options, self.state, self.today = options, state, today.isoformat()
        self.run_total = 0
        self.run_stream = {}

    def allows(self, stream):
        limits = stream["limits"]
        day_total = self.state["calls"].get(self.today, 0)
        day_stream = self.state["calls"].get(f"{self.today}/{stream['id']}", 0)
        checks = [
            (self.run_total, self.options.max_calls_per_run),
            (day_total, self.options.max_calls_per_day),
            (self.run_stream.get(stream["id"], 0), limits.get("max_calls_per_run")),
            (day_stream, limits.get("max_calls_per_day")),
        ]
        return all(limit is None or used < limit for used, limit in checks)

    def spend(self, stream):
        self.run_total += 1
        self.run_stream[stream["id"]] = self.run_stream.get(stream["id"], 0) + 1
        for key in (self.today, f"{self.today}/{stream['id']}"):
            self.state["calls"][key] = self.state["calls"].get(key, 0) + 1


def run_model(stream, provider, searches, run_date, state, options, budget, clock):
    tz = zone(stream["timezone"])
    last_call = [0.0]
    # Render every prompt and build every request first, so an unset variable
    # or a missing model fails before any call of this stream is made.
    planned = []
    for search in searches:
        prompt = render(
            search["prompt"], variables_for(stream, search, run_date, os.environ)
        )
        planned.append(
            (search, prompt, *build_request(stream, search, prompt, provider, options))
        )
    for search, prompt, path, body, meta in planned:
        cached = None
        if options.cache_dir:
            location = cache_path(options, stream, search, run_date)
            if location.exists():
                try:
                    cached = json.loads(location.read_text(encoding="utf-8"))
                except ValueError:
                    cached = None
                if cached and (
                    cached.get("prompt_hash") != digest(prompt)
                    or (cached.get("provider") or {}).get("model") != meta["model"]
                ):
                    cached = None
        if cached:
            state["last_run"][f"{stream['id']}/{search['id']}"] = run_date.isoformat()
            yield cached
            continue
        if not budget.allows(stream):
            options.note(f"{stream['id']}: call limit reached; remaining searches wait")
            return
        interval = stream["limits"].get("min_interval_s", options.min_interval_s)
        pause = last_call[0] + interval - clock()
        if last_call[0] and pause > 0:
            SLEEP(pause)
        budget.spend(stream)
        last_call[0] = clock()
        options.calls_attempted += 1
        try:
            response = post_json(
                options.base_url.rstrip("/") + path,
                body,
                {"Authorization": "Bearer " + options.api_key},
                timeout=stream["limits"].get("timeout_s") or options.timeout_s,
                retries=options.retries,
                note=options.note,
            )
        except FetchError as error:
            if error.status in (401, 403):
                raise
            options.note(f"{stream['id']}/{search['id']}: {error}; will retry next run")
            options.call_failures.append(f"{stream['id']}/{search['id']}")
            continue
        record = make_record(
            stream,
            search,
            run_date,
            datetime.now(tz).isoformat(timespec="seconds"),
            {"type": "openai", **meta},
            prompt,
            response=response,
        )
        if options.cache_dir:
            write_atomic(
                cache_path(options, stream, search, run_date),
                json.dumps(record, ensure_ascii=False, sort_keys=True),
            )
        state["last_run"][f"{stream['id']}/{search['id']}"] = run_date.isoformat()
        yield record


def collect(streams, options, state, mode="sync", clock=time.monotonic):
    """Yield raw result records for every due search, updating state in place.

    State is changed before each record is yielded, so a snapshot taken after
    the caller has handled a record covers it.
    """
    fresh_state(state)
    today_utc = datetime.now(UTC).date()
    prune(state, today_utc)
    budget = Budget(options, state, today_utc)
    warned = False
    for stream in streams:
        if options.only_streams and stream["id"] not in options.only_streams:
            continue
        run_date = options.run_date or datetime.now(zone(stream["timezone"])).date()
        for provider in stream["providers"]:
            if provider["type"] == "import":
                for record in run_import(stream, provider, state, options, mode):
                    if (
                        not options.only_searches
                        or record["search"]["id"] in options.only_searches
                    ):
                        if record["run_date"] == run_date.isoformat():
                            state["last_run"][
                                f"{stream['id']}/{record['search']['id']}"
                            ] = record["run_date"]
                        yield record
                continue
            if not options.model_ready():
                if not warned:
                    options.note(
                        "model calls are off (set RESEARCH_ALLOW_MODEL_CALLS=1, "
                        "RESEARCH_OPENAI_BASE_URL and RESEARCH_OPENAI_API_KEY)"
                    )
                    warned = True
                continue
            if not in_bounds(run_date, options):
                continue
            due = [
                s
                for s in stream["searches"]
                if (not options.only_searches or s["id"] in options.only_searches)
                and is_due(stream, s, run_date, state, options.force)
            ]
            yield from run_model(
                stream, provider, due, run_date, state, options, budget, clock
            )


# ------------------------------------------------------------ pack import


BUILTINS = {
    "today",
    "run_date",
    "weekday",
    "timezone",
    "window_start",
    "window_end",
    "stream",
    "search_id",
    "cluster",
    "variant",
}


def find_searches(pack):
    if isinstance(pack, list):
        return pack
    for key in ("searches", "calls", "queries", "items"):
        if isinstance(pack.get(key), list):
            return pack[key]
    for value in pack.values():
        if isinstance(value, dict):
            found = find_searches(value)
            if found:
                return found
    return []


def convert_pack(pack, stream_id="events", title=None):
    """A search pack ({id, cluster, kind, variant, variant_label, query} items)
    as one stream config. Unknown template variables become required stream
    variables (null), to be set by RESEARCH_VAR_<NAME> or by editing."""
    searches, variables, seen = [], {}, set()
    items = list(find_searches(pack))
    # A pack may keep one more search outside its list, such as a contextual
    # search fed with known plans through {planning_context}.
    extra = pack.get("contextual_search") if isinstance(pack, dict) else None
    if isinstance(extra, dict):
        items.append(extra)
    for item in items:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        prompt = item.get("query") or item.get("prompt")
        search_id = re.sub(r"[^a-z0-9_.-]+", "-", str(item["id"]).lower()).strip("-")
        if not isinstance(prompt, str) or search_id in seen:
            continue
        seen.add(search_id)
        for match in VARIABLE.finditer(prompt):
            name = match.group(1)
            if name and name not in BUILTINS:
                variables.setdefault(name, "" if name == "planning_context" else None)
        local = item.get("kind") in (None, "local-event", "event")
        cluster = item.get("cluster")
        label = item.get("variant_label") or item.get("variant")
        search = {
            "id": search_id,
            "label": f"{cluster} ({label})" if cluster and label else str(item["id"]),
            "cluster": cluster,
            "variant": item.get("variant"),
            "kind": item.get("kind") or "search",
            "prompt": prompt,
            "output": "labeled_markdown" if local else "markdown",
        }
        if local:
            search["item_type"] = "event"
        if item.get("recency"):
            search["recency"] = item["recency"]
        searches.append(search)
    meta = pack if isinstance(pack, dict) else {}
    system = meta.get("system") or meta.get("grounding_rules") or meta.get("rules")
    if isinstance(system, list):
        system = "\n".join(f"- {rule}" for rule in system if isinstance(rule, str))
    contract = meta.get("output_contract")
    if isinstance(contract, str) and contract.strip():
        parts = [
            p.strip() for p in (system, contract) if isinstance(p, str) and p.strip()
        ]
        system = "\n\n".join(parts)
    window_meta = meta.get("window") if isinstance(meta.get("window"), dict) else {}
    stream = {
        "id": stream_id,
        "title": title or meta.get("title") or stream_id,
        "timezone": meta.get("timezone") or window_meta.get("timezone") or "UTC",
        "cadence": "daily",
        "window_days": int(
            meta.get("window_days")
            or window_meta.get("days")
            or window_meta.get("default_days")
            or 10
        ),
        "output": "labeled_markdown",
        "item_type": "event",
        "variables": variables,
        "providers": [
            {"type": "import", "path": "${RESEARCH_IMPORT_DIR}"},
            {"type": "openai", "web_search": "plugin"},
        ],
        "limits": {"max_calls_per_run": 80, "max_calls_per_day": 80},
        "searches": searches,
    }
    if isinstance(system, str) and system.strip():
        stream["system"] = system
    normalize_stream(stream, "converted pack")
    return stream


# -------------------------------------------------------------------- CLI


def load_state(path):
    if not path or not Path(path).exists():
        return {}
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_state(path, state):
    if path:
        write_atomic(Path(path), json.dumps(state, indent=1, sort_keys=True))


def stderr(text):
    print(text, file=sys.stderr, flush=True)


def options_from_args(args):
    return Options.from_env(
        allow_model_calls=True if getattr(args, "allow_model_calls", False) else None,
        max_calls_per_run=getattr(args, "max_calls", None),
        cache_dir=getattr(args, "cache_dir", None),
        run_date=parse_day(getattr(args, "date", None)),
        since=parse_day(getattr(args, "since", None), "--since"),
        until=parse_day(getattr(args, "until", None), "--until"),
        only_streams=tuple(getattr(args, "stream", None) or ()) or None,
        only_searches=tuple(getattr(args, "search", None) or ()) or None,
        force=True if getattr(args, "force", False) else None,
        log=stderr,
    )


def command_export(args):
    streams = load_streams(args.streams)
    options = options_from_args(args)
    state = load_state(args.state)
    count = 0
    if args.out == "-":
        out, temporary = sys.stdout, None
    else:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = (
            target if args.append else target.with_name(target.name + ".partial")
        )
        out = temporary.open(
            "a" if args.append else "w", encoding="utf-8", newline="\n"
        )
    finished = False
    try:
        for record in collect(streams, options, state, mode=args.mode):
            out.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            out.flush()
            count += 1
            save_state(args.state, state)
        save_state(args.state, state)
        finished = True
    finally:
        if out is not sys.stdout:
            out.close()
        # The state file already marks every written record as done, so the
        # file is moved into place even when the run stops early (auth failure,
        # config error, interrupt); otherwise those results would be lost.
        if temporary is not None and not args.append and (finished or count):
            os.replace(temporary, args.out)
        stderr(json.dumps({"records": count, "out": args.out, "complete": finished}))


def command_plan(args):
    streams = load_streams(args.streams)
    options = options_from_args(args)
    state = fresh_state(load_state(args.state))
    for stream in streams:
        run_date = options.run_date or datetime.now(zone(stream["timezone"])).date()
        for search in stream["searches"]:
            values = variables_for(stream, search, run_date, os.environ)
            try:
                prompt = render(search["prompt"], values)
            except ConfigError as error:
                prompt = f"[not renderable: {error}]"
            print(
                json.dumps(
                    {
                        "stream": stream["id"],
                        "search": search["id"],
                        "due": is_due(stream, search, run_date, state, options.force),
                        "prompt": prompt if args.prompts else None,
                    },
                    ensure_ascii=False,
                )
            )


def command_validate(args):
    streams = load_streams(args.streams)
    for stream in streams:
        print(
            json.dumps(
                {
                    "stream": stream["id"],
                    "searches": len(stream["searches"]),
                    "providers": [p["type"] for p in stream["providers"]],
                }
            )
        )


def command_convert(args):
    pack = json.loads(Path(args.pack).read_text(encoding="utf-8"))
    stream = convert_pack(pack, args.stream_id, args.title)
    write_atomic(
        Path(args.out), json.dumps(stream, indent=2, ensure_ascii=False) + "\n"
    )
    stderr(json.dumps({"stream": stream["id"], "searches": len(stream["searches"])}))


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="fetch.py", description=__doc__.split("\n")[0]
    )
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="run due searches and write raw JSONL")
    plan = commands.add_parser(
        "plan", help="list searches, due flags and prompts; no calls"
    )
    for sub in (export, plan):
        sub.add_argument("--streams", required=True, help="stream file or directory")
        sub.add_argument("--state", help="JSON state file (cadence, call ledger)")
        sub.add_argument(
            "--date", help="run date YYYY-MM-DD (default: today in stream tz)"
        )
        sub.add_argument("--stream", action="append", help="only this stream id")
        sub.add_argument("--search", action="append", help="only this search id")
        sub.add_argument("--force", action="store_true", help="ignore cadence")
    export.add_argument(
        "--out", required=True, help="output JSONL file, or - for stdout"
    )
    export.add_argument("--append", action="store_true", help="append to --out")
    export.add_argument("--cache-dir", help="reuse and keep per-call results here")
    export.add_argument("--since", help="earliest run date to import")
    export.add_argument("--until", help="latest run date to import")
    export.add_argument("--max-calls", type=int, help="model calls allowed this run")
    export.add_argument(
        "--allow-model-calls", action="store_true", help="permit paid model calls"
    )
    export.add_argument(
        "--mode",
        choices=["sync", "backfill"],
        default="sync",
        help="backfill re-reads every import file within --since/--until",
    )
    plan.add_argument("--prompts", action="store_true", help="print rendered prompts")
    validate = commands.add_parser("validate", help="check stream configs")
    validate.add_argument("--streams", required=True)
    convert = commands.add_parser("convert-pack", help="search pack JSON to a stream")
    convert.add_argument("--pack", required=True)
    convert.add_argument("--out", required=True)
    convert.add_argument("--stream-id", default="events")
    convert.add_argument("--title")
    args = parser.parse_args(argv)
    try:
        {
            "export": command_export,
            "plan": command_plan,
            "validate": command_validate,
            "convert-pack": command_convert,
        }[args.command](args)
    except (ConfigError, FetchError) as error:
        stderr(f"error: {error}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
