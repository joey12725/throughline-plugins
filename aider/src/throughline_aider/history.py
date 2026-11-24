"""Parse aider's chat history markdown (standard library only).

aider appends to `.aider.chat.history.md` in the repository it runs in:

    # aider chat started at 2024-05-01 10:00:00     <- one per session

    > Aider v0.50.0                                 <- tool output: "> "
    > Main model: gpt-4o with diff edit format

    #### the user's prompt                          <- user input: "#### "
    #### a second line of it

    The assistant's reply, plain markdown, until the next user or tool line.

    > Applied edit to app.py

The file has no message ids or per-message times. Times come from the
session header and, when present, from `.aider.input.history` (prompt_toolkit
history: "# <timestamp>" followed by "+"-prefixed lines per input), matched
to user messages by text; anything else is placed one second after the
previous message and flagged as estimated.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

HEADER = re.compile(rb"^# aider chat started at (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*$")
MODEL = re.compile(r"^(?:Main )?[Mm]odel: (\S+)")
VERSION = re.compile(r"^Aider v(\S+)")
INPUT_STAMP = re.compile(r"^# (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)\s*$")


@dataclass
class Block:
    role: str  # user, assistant or tool
    lines: list = field(default_factory=list)
    closed: bool = False  # A blank line followed it

    @property
    def text(self):
        return "\n".join(self.lines).strip("\n").strip()


@dataclass
class Session:
    offset: int  # Byte offset of the header line in the file
    end: int  # Byte offset just past the session
    stamp: str  # Header time as written (local, naive)
    header: str
    blocks: list = field(default_factory=list)


def classify(line):
    if line.startswith("#### ") or line == "####":
        return "user", line[5:]
    if line.startswith("> ") or line == ">":
        return "tool", line[2:]
    return "assistant", line


def parse(data, base=0):
    """Sessions in `data` (bytes read from byte offset `base`). Lines before
    the first session header are returned as a count of skipped lines."""
    sessions, skipped = [], 0
    offset = base
    current = None
    for raw in data.splitlines(keepends=True):
        start = offset
        offset += len(raw)
        bare = raw.rstrip(b"\r\n")
        match = HEADER.match(bare)
        if match:
            if current:
                current.end = start
            current = Session(start, offset, match.group(1).decode(), bare.decode("utf-8", "replace"))
            sessions.append(current)
            continue
        if current is None:
            skipped += bool(bare.strip())
            continue
        current.end = offset
        line = bare.decode("utf-8", "replace").rstrip()
        blocks = current.blocks
        if not line:
            if blocks and blocks[-1].role == "assistant":
                blocks[-1].lines.append("")
            elif blocks:
                blocks[-1].closed = True
            continue
        role, text = classify(line)
        last = blocks[-1] if blocks else None
        if (
            last
            and last.role == role
            and (role != "user" or not last.closed)
        ):
            # Tool lines separated by blank lines stay one block; a blank line
            # always separates two user inputs.
            last.lines.append(text)
            last.closed = False
        else:
            blocks.append(Block(role, [text]))
    return sessions, skipped


def local_time(stamp, tz):
    """An aware datetime from a naive local stamp, in tz (a tzinfo) or the
    system's local time zone when tz is None."""
    naive = datetime.fromisoformat(stamp)
    return naive.replace(tzinfo=tz) if tz else naive.astimezone()


def normalize(text):
    return "\n".join(line.strip() for line in text.strip().splitlines())


def parse_inputs(text, tz):
    """[(aware time, normalized text)] from an .aider.input.history file."""
    entries = []
    when, lines = None, []

    def close():
        if when is not None and lines:
            entries.append((when, normalize("\n".join(lines))))

    for line in text.splitlines():
        match = INPUT_STAMP.match(line)
        if match:
            close()
            try:
                when = local_time(match.group(1), tz)
            except ValueError:
                when = None
            lines = []
        elif line.startswith("+") and when is not None:
            lines.append(line[1:])
    close()
    entries.sort(key=lambda e: e[0])
    return entries


class InputTimes:
    """Matches user messages to input history entries, in order."""

    def __init__(self, entries):
        self.entries = entries
        self.position = 0

    def seek(self, start):
        # Allow a little clock skew between the two files.
        floor = start - timedelta(seconds=2)
        self.position = 0
        while self.position < len(self.entries) and self.entries[self.position][0] < floor:
            self.position += 1

    def match(self, text, before=None):
        target = normalize(text)
        for index in range(self.position, len(self.entries)):
            when, value = self.entries[index]
            if before is not None and when >= before:
                return None
            if value == target:
                self.position = index + 1
                return when
        return None
