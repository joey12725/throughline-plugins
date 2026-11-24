"""Export files written by fetch.py: discovery, digests and line reading."""

import gzip
import hashlib
import json
from pathlib import Path

SUFFIXES = (".jsonl", ".jsonl.gz")


def parse_source(value):
    """("live", None) or ("export", Path) from EMAIL_SOURCE."""
    value = (value or "live").strip()
    if value == "live":
        return "live", None
    kind, sep, path = value.partition(":")
    if kind != "export" or not sep or not path.strip():
        raise ValueError("EMAIL_SOURCE must be live or export:<file or directory>")
    return "export", Path(path.strip())


def export_files(root):
    """[(stable name, path)] in name order: one file, or a directory tree."""
    root = Path(root)
    if root.is_file():
        return [(root.name, root)]
    if not root.is_dir():
        raise ValueError("EMAIL_SOURCE export path does not exist")
    files = [p for p in root.rglob("*") if p.is_file() and p.name.endswith(SUFFIXES)]
    return sorted(((p.relative_to(root).as_posix(), p) for p in files))


def digest(path):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(block)
    return hasher.hexdigest()


def read(path, name):
    """(line number, record) for every non-blank line of an export file."""
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                raise ValueError(f"export {name} line {number} is not JSON") from None
            if not isinstance(record, dict):
                raise ValueError(f"export {name} line {number} is not an object")
            yield number, record
