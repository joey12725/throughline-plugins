"""Finding Claude Code transcripts and reading them incrementally.

Standard library only. Files are opened read-only and never written.

Layout of one Claude Code home (default ~/.claude, or $CLAUDE_CONFIG_DIR):

    projects/<encoded cwd>/<session id>.jsonl                      a session
    projects/<encoded cwd>/<session id>/subagents/agent-<id>.jsonl a subagent
    projects/<encoded cwd>/<session id>/subagents/agent-<id>.meta.json
    projects/<encoded cwd>/<session id>/subagents/workflows/<run>/agent-<id>.jsonl

Transcripts are append-only JSONL. The cursor keeps, per file, the byte
offset after the last complete line read and a fingerprint of the first
line, so a file that was truncated or rewritten (a different first line, or
shorter than the offset) is read again from the start.
"""

import hashlib
import json
import os
from pathlib import Path

FINGERPRINT_BYTES = 65536


class HomeError(RuntimeError):
    """A configured home could not be read."""


def split_list(value):
    return [p.strip() for p in str(value or "").split(",") if p.strip()]


def default_home():
    return os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"


def parse_homes(value):
    """LABEL=PATH,LABEL=PATH -> [(label, Path)]; a bare PATH is labelled
    'local' (first) or by its parent folder name."""
    homes = []
    for item in split_list(value or f"local={default_home()}"):
        label, sep, path = item.partition("=")
        if not sep:
            path = item
            label = "local" if not homes else (Path(item).expanduser().parent.name or "home")
        label, path = label.strip(), path.strip()
        if not label or not path:
            raise ValueError(f"bad CLAUDE_CODE_HOMES entry {item!r}: use LABEL=PATH")
        homes.append((label, Path(path).expanduser()))
    if len({label for label, _ in homes}) != len(homes):
        raise ValueError("CLAUDE_CODE_HOMES labels must be unique")
    return homes


def transcripts(home, subagents=True):
    """[(relative key, Path, kind)] of every transcript in a home, sorted.

    kind is 'session' or 'subagent'. Raises HomeError when the home has no
    readable projects folder."""
    projects = home / "projects"
    if not projects.is_dir():
        raise HomeError(f"{projects} is not a folder")
    found = []
    try:
        for project in sorted(projects.iterdir()):
            if not project.is_dir():
                continue
            for path in sorted(project.glob("*.jsonl")):
                found.append((path.relative_to(projects).as_posix(), path, "session"))
            if subagents:
                for path in sorted(project.glob("*/subagents/**/agent-*.jsonl")):
                    found.append((path.relative_to(projects).as_posix(), path, "subagent"))
    except OSError as error:
        raise HomeError(f"{projects}: {error.strerror or error}") from None
    return found


def fingerprint(path):
    """Short hash of the first complete line, or None when there is none yet."""
    with open(path, "rb") as handle:
        head = handle.read(FINGERPRINT_BYTES)
    end = head.find(b"\n")
    if end < 0:
        return None if len(head) < FINGERPRINT_BYTES else hashlib.sha256(head).hexdigest()[:16]
    return hashlib.sha256(head[:end]).hexdigest()[:16]


def lines(path, start):
    """Yield (start offset, end offset, parsed JSON or None) for every
    complete line from byte `start`. A last line without a newline is still
    being written and is left for the next run."""
    with open(path, "rb") as handle:
        handle.seek(start)
        position = start
        for raw in handle:
            if not raw.endswith(b"\n"):
                return
            begin, position = position, position + len(raw)
            text = raw.strip()
            if not text:
                yield begin, position, None
                continue
            try:
                value = json.loads(text.decode("utf-8", "replace"))
            except ValueError:
                value = None
            yield begin, position, value if isinstance(value, dict) else None


def read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}
