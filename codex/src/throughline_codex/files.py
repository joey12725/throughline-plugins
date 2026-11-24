"""Finding Codex rollout files and reading them incrementally.

Standard library only. Files are opened read-only and never written.

Layout of one Codex home (default ~/.codex, or $CODEX_HOME):

    sessions/YYYY/MM/DD/rollout-<time>-<session id>.jsonl   live sessions
    archived_sessions/rollout-<time>-<session id>.jsonl     archived sessions
    session_index.jsonl                                     thread names

Rollouts are append-only JSONL; Codex later compresses idle ones to
`.jsonl.zst`. The cursor keeps, per file name (without `.zst`), the byte
offset after the last complete line read and a fingerprint of the first
line. Keying by name (not path or compression) means a session Codex moves
into archived_sessions or compresses keeps its place: the decompressed
stream has the same offsets. A file that was truncated or rewritten is read
again from the start.

Decompression uses the standard library's compression.zstd (Python 3.14+)
or else the zstandard package.
"""

import hashlib
import io
import json
import os
import re
from pathlib import Path

try:  # Python 3.14+
    from compression import zstd as _zstd
except ImportError:  # pragma: no cover - depends on the interpreter
    _zstd = None
    import zstandard

FINGERPRINT_BYTES = 65536
# Lines above this size are only parsed when they could be mapped: UI events
# and tool outputs (which can be tens of megabytes) are skipped unparsed.
LARGE_LINE = 1 << 20
UNMAPPED = re.compile(rb'"type":\s*"(?:event_msg|token_usage_record|world_state)"')
OUTPUT_ITEM = re.compile(rb'"type":\s*"[a-z_]*_output"')
SESSION_ID = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$")


class HomeError(RuntimeError):
    """A configured home could not be read."""


class Truncated(RuntimeError):
    """A compressed rollout is shorter than the stored offset."""


def is_compressed(path):
    return path.name.endswith(".jsonl.zst")


def open_rollout(path):
    """Binary reader of a rollout's JSONL bytes, decompressing .zst."""
    handle = open(path, "rb")
    if not is_compressed(path):
        return handle
    if _zstd is not None:
        return _zstd.ZstdFile(handle)
    return io.BufferedReader(zstandard.ZstdDecompressor().stream_reader(handle, closefd=True))


def split_list(value):
    return [p.strip() for p in str(value or "").split(",") if p.strip()]


def default_home():
    return os.environ.get("CODEX_HOME") or "~/.codex"


def parse_homes(value):
    """LABEL=PATH,LABEL=PATH -> [(label, Path)]."""
    homes = []
    for item in split_list(value or f"local={default_home()}"):
        label, sep, path = item.partition("=")
        if not sep:
            path = item
            label = "local" if not homes else (Path(item).expanduser().parent.name or "home")
        label, path = label.strip(), path.strip()
        if not label or not path:
            raise ValueError(f"bad CODEX_HOMES entry {item!r}: use LABEL=PATH")
        homes.append((label, Path(path).expanduser()))
    if len({label for label, _ in homes}) != len(homes):
        raise ValueError("CODEX_HOMES labels must be unique")
    return homes


def rollouts(home, archived=True):
    """[(file name, Path)] of a home's rollouts, sorted; the name of a
    compressed rollout drops `.zst`.

    When one name is found twice (mid-move or mid-compression), the
    uncompressed copy, else the larger one, is read. Raises HomeError when
    the home has no sessions folder."""
    sessions = home / "sessions"
    if not sessions.is_dir():
        raise HomeError(f"{sessions} is not a folder")
    folders = [sessions]
    if archived and (home / "archived_sessions").is_dir():
        folders.append(home / "archived_sessions")
    found = {}
    try:
        for folder in folders:
            for path in folder.rglob("rollout-*"):
                if not path.name.endswith((".jsonl", ".jsonl.zst")) or not path.is_file():
                    continue
                name = path.name.removesuffix(".zst")
                other = found.get(name)
                if other is None or (is_compressed(other), -other.stat().st_size) > (
                    is_compressed(path),
                    -path.stat().st_size,
                ):
                    found[name] = path
    except OSError as error:
        raise HomeError(f"{home}: {error.strerror or error}") from None
    return sorted(found.items())


def session_names(home):
    """{session id: thread name} from session_index.jsonl (latest wins)."""
    names = {}
    path = home / "session_index.jsonl"
    try:
        handle = open(path, encoding="utf-8")
    except OSError:
        return names
    with handle:
        rows = []
        for line in handle:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("id") and row.get("thread_name"):
                rows.append((str(row.get("updated_at") or ""), row["id"], row["thread_name"]))
    for _, ident, name in sorted(rows):
        names[ident] = name
    return names


def fingerprint(path):
    """Short hash of the first complete line, or None when there is none yet."""
    with open_rollout(path) as handle:
        head = handle.read(FINGERPRINT_BYTES)
    end = head.find(b"\n")
    if end < 0:
        return None if len(head) < FINGERPRINT_BYTES else hashlib.sha256(head).hexdigest()[:16]
    return hashlib.sha256(head[:end]).hexdigest()[:16]


def lines(path, start):
    """Yield (start offset, end offset, parsed JSON or None) for every
    complete line from byte `start`. A last line without a newline is still
    being written and is left for the next run. Raises Truncated when a
    compressed rollout ends before `start`."""
    with open_rollout(path) as handle:
        if is_compressed(path):
            left = start
            while left:
                chunk = handle.read(min(left, 1 << 20))
                if not chunk:
                    raise Truncated(str(path))
                left -= len(chunk)
        else:
            handle.seek(start)
        position = start
        for raw in handle:
            if not raw.endswith(b"\n"):
                return
            begin, position = position, position + len(raw)
            if len(raw) > LARGE_LINE and unmapped(raw):
                yield begin, position, None
                continue
            text = raw.strip()
            if not text:
                yield begin, position, None
                continue
            try:
                value = json.loads(text.decode("utf-8", "replace"))
            except ValueError:
                value = None
            yield begin, position, value if isinstance(value, dict) else None


def unmapped(raw):
    """A large line that holds an event or a tool output, judged without
    parsing it (the item type comes first in the payload)."""
    return bool(OUTPUT_ITEM.search(raw, 0, 4096) or UNMAPPED.search(raw))


def session_id_of(name):
    match = SESSION_ID.search(name.removesuffix(".zst"))
    return match.group(1) if match else name.removesuffix(".jsonl")
