"""Vault discovery: roots, ignore rules, daily-note rules and link resolution."""

from __future__ import annotations

import base64
import datetime as dt
import fnmatch
import gzip
import json
import os
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

DEFAULT_IGNORE = (
    ".obsidian/**",
    ".trash/**",
    ".git/**",
    "logseq/bak/**",
    "logseq/.recycle/**",
    "logseq/version-files/**",
    "node_modules/**",
)
GENERIC_DAILY = re.compile(r"(\d{4})[-_.](\d{2})[-_.](\d{2})")
MONTHS = {
    name.lower(): number
    for number, names in enumerate(
        [
            ("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"),
            ("may", "may"), ("june", "jun"), ("july", "jul"), ("august", "aug"),
            ("september", "sep"), ("october", "oct"), ("november", "nov"), ("december", "dec"),
        ],
        1,
    )
    for name in names
}


def pack(value):
    """Compact cursor encoding: gzip + base64 of canonical JSON."""
    raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    return base64.b64encode(gzip.compress(raw, mtime=0)).decode("ascii")


def unpack(text):
    if not text:
        return {}
    return json.loads(gzip.decompress(base64.b64decode(text)))


@dataclass
class Root:
    label: str
    path: Path
    format: str = "markdown"
    daily: list = field(default_factory=list)  # [(folder, compiled regex)]

    def detect(self):
        if (self.path / ".obsidian").is_dir():
            self.format = "obsidian"
            self.daily = obsidian_daily(self.path)
        elif (self.path / "logseq" / "config.edn").is_file() or (
            (self.path / "journals").is_dir() and (self.path / "pages").is_dir()
        ):
            self.format = "logseq"
            self.daily = [("journals", moment_regex(logseq_format(self.path)))]
        return self


def parse_roots(value):
    """`label=path` or bare paths, separated by commas or os.pathsep."""
    roots = []
    seen = set()
    for item in re.split(r"[,\n]" if "," in (value or "") else re.escape(os.pathsep), value or ""):
        item = item.strip()
        if not item:
            continue
        label, sep, path = item.partition("=")
        if not sep:
            label, path = "", item
        path = Path(path.strip()).expanduser()
        label = label.strip() or path.name or "notes"
        label = re.sub(r"[^A-Za-z0-9_.-]+", "-", label).strip("-") or "notes"
        if label in seen:
            raise ValueError(f"duplicate markdown notes root label {label!r}; use label=path")
        seen.add(label)
        roots.append(Root(label, path))
    if not roots:
        raise ValueError("MARKDOWN_NOTES_ROOTS names no folder")
    return roots


def ignored(rel, patterns):
    for pattern in patterns:
        if pattern.endswith("/**"):
            base = pattern[:-3]
            if rel == base or rel.startswith(base + "/") or fnmatch.fnmatchcase(rel, pattern):
                return True
        elif fnmatch.fnmatchcase(rel, pattern) or fnmatch.fnmatchcase(posixpath.basename(rel), pattern):
            return True
    return False


def scan(root, *, patterns, extensions, include_hidden=False):
    """{relpath: (mtime_ns, size)} of every note under the root, never
    following directory symlinks."""
    found = {}
    base = str(root.path)
    for directory, dirs, files in os.walk(base, followlinks=False):
        rel_dir = os.path.relpath(directory, base).replace(os.sep, "/")
        rel_dir = "" if rel_dir == "." else rel_dir + "/"
        keep = []
        for name in sorted(dirs):
            rel = rel_dir + name
            if (not include_hidden and name.startswith(".")) or ignored(rel, patterns):
                continue
            keep.append(name)
        dirs[:] = keep
        for name in files:
            if not name.lower().endswith(extensions):
                continue
            rel = rel_dir + name
            if (not include_hidden and name.startswith(".")) or ignored(rel, patterns):
                continue
            try:
                info = os.stat(os.path.join(directory, name))
            except OSError:
                continue
            found[rel] = (info.st_mtime_ns, info.st_size)
    return found


# --- daily notes ------------------------------------------------------------

TOKENS = [
    ("YYYY", r"\d{4}", "Y"), ("YY", r"\d{2}", "y"), ("MMMM", r"[A-Za-z]+", "B"),
    ("MMM", r"[A-Za-z]{3}", "b"), ("MM", r"\d{2}", "m"), ("M", r"\d{1,2}", "m"),
    ("Do", r"\d{1,2}(?:st|nd|rd|th)", "o"), ("DD", r"\d{2}", "d"), ("D", r"\d{1,2}", "d"),
    ("dddd", r"[A-Za-z]+", None), ("ddd", r"[A-Za-z]{3}", None),
    # Logseq/Java style tokens.
    ("yyyy", r"\d{4}", "Y"), ("dd", r"\d{2}", "d"), ("EEEE", r"[A-Za-z]+", None), ("EEE", r"[A-Za-z]{3}", None),
]


def moment_regex(fmt):
    """Compile a moment.js (Obsidian) or Java (Logseq) date format to a
    regex with named Y/y/m/B/b/d/o groups."""
    out = []
    used = set()
    index = 0
    while index < len(fmt):
        if fmt[index] == "[":
            end = fmt.find("]", index)
            end = len(fmt) if end < 0 else end
            out.append(re.escape(fmt[index + 1 : end]))
            index = end + 1
            continue
        for token, pattern, group in TOKENS:
            if fmt.startswith(token, index):
                if group is None:
                    out.append(f"(?:{pattern})")
                elif group in used:
                    out.append(f"(?P={group})")
                else:
                    used.add(group)
                    out.append(f"(?P<{group}>{pattern})")
                index += len(token)
                break
        else:
            out.append(re.escape(fmt[index]))
            index += 1
    return re.compile("".join(out), re.I)


def obsidian_daily(path):
    config = path / ".obsidian" / "daily-notes.json"
    folder, fmt = "", "YYYY-MM-DD"
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
        folder = str(data.get("folder") or "").strip("/")
        fmt = str(data.get("format") or fmt)
    except (OSError, ValueError, AttributeError):
        pass
    return [(folder, moment_regex(fmt))]


def logseq_format(path):
    try:
        text = (path / "logseq" / "config.edn").read_text(encoding="utf-8")
    except OSError:
        return "yyyy_MM_dd"
    match = re.search(r':journal/file-name-format\s+"([^"]+)"', text)
    return match.group(1) if match else "yyyy_MM_dd"


def date_of(match):
    groups = match.groupdict()
    year = int(groups["Y"]) if groups.get("Y") else (2000 + int(groups["y"]) if groups.get("y") else None)
    month = groups.get("m")
    month = int(month) if month else MONTHS.get((groups.get("B") or groups.get("b") or "").lower())
    day = groups.get("d") or (groups.get("o") or "")[:-2]
    if not (year and month and day):
        return None
    try:
        return dt.date(year, int(month), int(day))
    except ValueError:
        return None


def daily_date(root, rel):
    """The day a daily/journal note is about, or None."""
    stem_path = rel.rsplit(".", 1)[0]
    for folder, pattern in root.daily:
        inner = stem_path
        if folder:
            if not stem_path.startswith(folder + "/"):
                continue
            inner = stem_path[len(folder) + 1 :]
        match = pattern.fullmatch(inner) or pattern.fullmatch(posixpath.basename(inner))
        if match:
            day = date_of(match)
            if day:
                return day
    match = GENERIC_DAILY.fullmatch(posixpath.basename(stem_path))
    if match:
        try:
            return dt.date(*(int(g) for g in match.groups()))
        except ValueError:
            return None
    return None


# --- titles and wikilink resolution ------------------------------------------


def page_name(root, rel):
    """The name a note is linked by: its file stem (Logseq namespaces
    decoded: `a___b.md` and `a%2Fb.md` are page `a/b`)."""
    stem = posixpath.basename(rel).rsplit(".", 1)[0]
    if root.format == "logseq":
        stem = unquote(stem.replace("___", "/"))
    return stem


class Index:
    """Resolve link targets the way Obsidian and Logseq do: exact path,
    path suffix, then unique-ish file name (shortest path wins)."""

    def __init__(self, root, rels):
        self.root = root
        self.paths = {}
        self.names = {}
        for rel in sorted(rels, key=lambda r: (r.count("/"), len(r), r)):
            self.paths.setdefault(rel.casefold(), rel)
            self.paths.setdefault(rel.rsplit(".", 1)[0].casefold(), rel)
            self.names.setdefault(page_name(root, rel).casefold(), rel)

    def resolve(self, source_rel, target, kind):
        target = target.strip().replace("\\", "/")
        if not target:
            return None
        if kind.startswith("markdown"):
            joined = posixpath.normpath(posixpath.join(posixpath.dirname(source_rel), target))
            hit = self.paths.get(joined.casefold())
            if hit:
                return hit
        key = target.lstrip("/").casefold()
        if key in self.paths:
            return self.paths[key]
        if "/" in key:
            for path_key, rel in self.paths.items():
                if path_key.endswith("/" + key):
                    return rel
        if key in self.names:
            return self.names[key]
        if key.endswith(".md") and key[:-3] in self.names:
            return self.names[key[:-3]]
        return None
