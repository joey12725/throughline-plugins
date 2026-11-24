"""Where raw records come from: export files or an in-process live fetch."""

import gzip
import json
import os
from pathlib import Path

from . import fetch

SUFFIXES = (".jsonl", ".jsonl.gz", ".ndjson")


def source_spec(value):
    """('live', None) or ('export', Path) from HERMES_SOURCE."""
    value = (value or "live").strip()
    if value == "live":
        return "live", None
    if value.startswith("export:") and value[7:].strip():
        return "export", Path(value[7:].strip()).expanduser()
    raise ValueError("HERMES_SOURCE must be 'live' or 'export:<file or directory>'")


def export_files(path):
    if path.is_dir():
        files = sorted(
            p for p in path.rglob("*") if p.is_file() and p.name.endswith(SUFFIXES)
        )
        if not files:
            raise ValueError("the export directory holds no .jsonl files")
        return files
    if path.is_file():
        return [path]
    raise ValueError("the HERMES_SOURCE export path does not exist")


def read_lines(path):
    opener = gzip.open if path.name.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                raise ValueError(f"{path.name} line {number} is not JSON") from None
            if (
                not isinstance(record, dict)
                or not record.get("kind")
                or not record.get("run")
            ):
                raise ValueError(
                    f"{path.name} line {number} is not a Hermes export record"
                )
            yield record


def run_key(records):
    """Sort key of a run: (fetched_at in UTC, run id); None without a header."""
    header = next((r for r in records if r.get("kind") == "run"), None)
    if header is None:
        return None
    try:
        at = fetch.parse_moment(header["fetched_at"])
    except (KeyError, TypeError, ValueError):
        return None
    return [at.isoformat(), header["run"]]


def export_runs(path):
    """Every run in the export files as (key, records), oldest first.

    A run split over several files (or appended twice) is merged by run id.
    Runs without a header are reported and skipped.
    """
    runs = {}
    for file in export_files(path):
        for record in read_lines(file):
            runs.setdefault(record["run"], []).append(record)
    keyed, skipped = [], 0
    for records in runs.values():
        key = run_key(records)
        if key is None:
            skipped += 1
            continue
        keyed.append((key, records))
    keyed.sort(key=lambda pair: pair[0])
    return keyed, skipped


def live_run(settings, mode, fetch_state, *, now=None, log=None, popen=None):
    """Fetch one run in-process; returns (key, records, report)."""
    cfg = fetch.config({k: v for k, v in settings.items() if v is not None})
    fetcher = fetch.Fetcher(cfg, mode=mode, now=now, log=log, popen=popen)
    records = list(fetcher.run(fetch_state))
    return run_key(records), records, fetcher.report


def archive(directory, records):
    """Keep a live run's raw records as an export file (replayable later)."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"hermes-{records[0]['run']}.jsonl"
    partial = target.with_name(target.name + ".part")
    with open(partial, "w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(partial, target)
    return target
