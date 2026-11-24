"""Read fetch.py export files: one .jsonl(.gz) file or a directory of them."""

import gzip
import json
from pathlib import Path

from .fetch import FORMAT_VERSION


def export_files(path):
    root = Path(path).expanduser()
    if root.is_dir():
        files = sorted(
            p
            for p in root.rglob("*")
            if p.is_file()
            and (p.name.endswith(".jsonl") or p.name.endswith(".jsonl.gz"))
        )
        if not files:
            raise ValueError("HEALTH_SOURCE export directory holds no .jsonl files")
        return files
    if root.is_file():
        return [root]
    raise ValueError("HEALTH_SOURCE export path does not exist")


def read_export(path):
    """All records in file order (files sorted by path); export markers kept.

    A malformed line fails the run with its file name and line number only,
    never its content.
    """
    records = []
    for file in export_files(path):
        opener = gzip.open if file.name.endswith(".gz") else open
        with opener(file, "rt", encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    raise ValueError(f"{file.name}:{number} is not JSON") from None
                if not isinstance(item, dict) or not isinstance(item.get("kind"), str):
                    raise ValueError(
                        f"{file.name}:{number} is not a health export record"
                    )
                if item["kind"] == "export.end":
                    version = (item.get("payload") or {}).get("format", FORMAT_VERSION)
                    if not isinstance(version, int) or version > FORMAT_VERSION:
                        raise ValueError(
                            f"{file.name} uses export format {version}; update the plugin"
                        )
                records.append(item)
    return records
