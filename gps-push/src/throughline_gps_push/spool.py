"""Append-only JSONL spool shared by the receiver (writer) and the plugin (reader).

The receiver appends one JSON object per accepted request to a file named
``<prefix>-YYYY-MM-DD.jsonl`` (UTC day of receipt). Each line is written with
a single ``write`` on an ``O_APPEND`` descriptor under a process lock, so a
reader never sees half a line except at the very end of a file that is being
written; the reader only consumes lines that end in a newline.

The plugin keeps a byte offset per file name in its cursor and resumes there.
"""

import json
import logging
import os
import threading
from pathlib import Path

log = logging.getLogger(__name__)


class SpoolWriter:
    def __init__(self, directory, prefix):
        self.directory = Path(directory)
        self.prefix = prefix
        self.lock = threading.Lock()
        self.directory.mkdir(parents=True, exist_ok=True)

    def path_for(self, day):
        return self.directory / f"{self.prefix}-{day}.jsonl"

    def append(self, record, day):
        line = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        with self.lock:
            fd = os.open(self.path_for(day), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, line)
                os.fsync(fd)
            finally:
                os.close(fd)


def spool_files(directory):
    directory = Path(directory)
    if not directory.is_dir():
        raise FileNotFoundError(f"spool directory does not exist: {directory}")
    return sorted(p for p in directory.iterdir() if p.is_file() and p.suffix == ".jsonl")


def read_spool(directory, offsets):
    """Yield (file name, offset after the line, parsed record or None) for
    every complete line after the stored offsets, files in name order.

    A line that is not valid JSON yields None for the record (the caller
    skips it but still advances past it). A file shorter than its stored
    offset was replaced: it is read again from the start.
    """
    for path in spool_files(directory):
        start = int(offsets.get(path.name, 0))
        size = path.stat().st_size
        if start > size:
            log.warning("spool file %s shrank; reading it again from the start", path.name)
            start = 0
        if start == size:
            continue
        with open(path, "rb") as f:
            f.seek(start)
            position = start
            while True:
                line = f.readline()
                if not line or not line.endswith(b"\n"):
                    break
                position += len(line)
                text = line.strip()
                if not text:
                    continue
                try:
                    record = json.loads(text)
                except ValueError:
                    log.warning("skipping a malformed spool line in %s", path.name)
                    record = None
                yield path.name, position, record
