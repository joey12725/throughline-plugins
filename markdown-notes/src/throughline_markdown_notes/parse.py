"""Parse one Markdown note: YAML frontmatter, Logseq page properties,
[[wikilinks]], Markdown links to other notes and #tags."""

from __future__ import annotations

import datetime as dt
import re
from urllib.parse import unquote

import yaml

FENCE = re.compile(r"^(```|~~~).*?^\1[^\n]*$", re.M | re.S)
INLINE_CODE = re.compile(r"`[^`\n]*`")
WIKILINK = re.compile(r"(!?)\[\[([^\[\]\n]+?)\]\]")
MD_LINK = re.compile(r"(!?)\[[^\]\n]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
LOGSEQ_TAG = re.compile(r"(?:(?<=\s)|^)#\[\[([^\[\]\n]+)\]\]")
TAG = re.compile(r"(?:(?<=\s)|^)#([^\s#\[\]{}()<>\"'`,;:!?*|\\]+)")
PROPERTY = re.compile(r"^([A-Za-z][\w-]*)::\s*(.*)$")
SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.I)


def jsonable(value):
    """YAML may produce dates, sets and other non-JSON values."""
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    if isinstance(value, (dt.date, dt.datetime, dt.time)):
        return value.isoformat()
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def split_frontmatter(text):
    """Return (frontmatter dict, body, error flag)."""
    if not text.startswith("---"):
        return {}, text, False
    lines = text.split("\n")
    if lines[0].strip() != "---":
        return {}, text, False
    for number in range(1, len(lines)):
        if lines[number].strip() in ("---", "..."):
            raw = "\n".join(lines[1:number])
            body = "\n".join(lines[number + 1 :])
            try:
                data = yaml.safe_load(raw) if raw.strip() else {}
            except yaml.YAMLError:
                return {}, body, True
            if data is None:
                data = {}
            if not isinstance(data, dict):
                return {}, body, True
            return jsonable(data), body, False
    return {}, text, False


def logseq_properties(body):
    """Logseq page properties: leading `key:: value` lines (optionally as the
    first bullet). Returns (properties, body without them)."""
    lines = body.split("\n")
    props = {}
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if line.startswith("- ") and not props:
            line = line[2:].strip()
        match = PROPERTY.match(line)
        if not match:
            break
        props[match.group(1).lower()] = match.group(2).strip()
        index += 1
    if not props:
        return {}, body
    return props, "\n".join(lines[index:])


def listish(value):
    """A frontmatter/property value as a list of strings."""
    if value is None:
        return []
    if isinstance(value, list):
        items = []
        for item in value:
            items.extend(listish(item))
        return items
    text = str(value).strip()
    if not text:
        return []
    text = text.replace("[[", "").replace("]]", "")
    parts = re.split(r"\s*,\s*", text) if "," in text else [text]
    return [part.strip() for part in parts if part.strip()]


def tag_list(value):
    tags = []
    for item in listish(value):
        # `tags: a b` is a space-separated list in Obsidian.
        for part in item.split() if " " in item and "," not in item else [item]:
            part = part.strip().lstrip("#")
            if part:
                tags.append(part)
    return tags


def unique(items):
    seen = set()
    result = []
    for item in items:
        key = item.casefold()
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def strip_code(body):
    return INLINE_CODE.sub(" ", FENCE.sub(" ", body))


def link_targets(body):
    """[(target, kind)] where kind is wikilink, embed or markdown; targets
    keep their written form minus heading/block/alias parts."""
    text = strip_code(body)
    found = []
    for bang, inner in WIKILINK.findall(text):
        target = inner.split("|", 1)[0].split("#", 1)[0].split("^", 1)[0].strip()
        if target:
            found.append((target, "embed" if bang else "wikilink"))
    for bang, href in MD_LINK.findall(text):
        href = href.strip("<>")
        if SCHEME.match(href) or href.startswith("#"):
            continue
        target = unquote(href.split("#", 1)[0])
        if target:
            found.append((target, "markdown_embed" if bang else "markdown"))
    return found


def inline_tags(body):
    text = strip_code(body)
    tags = [t.strip() for t in LOGSEQ_TAG.findall(text)]
    text = LOGSEQ_TAG.sub(" ", text)
    for raw in TAG.findall(text):
        tag = raw.rstrip(".-/")
        if tag and not tag.isdigit():
            tags.append(tag)
    return tags


def parse_note(text, *, logseq=False):
    """Everything the mapping needs from one note's text."""
    text = text.replace("\r\n", "\n").lstrip("﻿")
    frontmatter, body, error = split_frontmatter(text)
    props = {}
    if logseq or not frontmatter:
        props, body = logseq_properties(body)
    meta = {**props, **{k.lower(): v for k, v in frontmatter.items()}}
    tags = tag_list(meta.get("tags")) + tag_list(meta.get("tag"))
    tags += inline_tags(body)
    aliases = listish(meta.get("aliases")) + listish(meta.get("alias"))
    title = meta.get("title")
    return {
        "frontmatter": frontmatter,
        "properties": props,
        "frontmatter_error": error,
        "meta": meta,
        "title": str(title).strip() if isinstance(title, (str, int, float)) and str(title).strip() else None,
        "tags": unique(tags),
        "aliases": unique(aliases),
        "links": link_targets(body),
        "body": body.strip("\n"),
    }


def parse_when(value, tz):
    """A frontmatter date/datetime as an aware datetime, else None."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            day = dt.date.fromisoformat(text)
            return dt.datetime(day.year, day.month, day.day, tzinfo=tz)
        moment = dt.datetime.fromisoformat(text.replace(" ", "T", 1) if "T" not in text else text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=tz)
    return moment
