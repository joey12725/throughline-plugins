"""Small, defensive converters shared by both readers: timestamps, ProseMirror
documents and HTML fragments to plain text."""

import re
from datetime import UTC, datetime
from html.parser import HTMLParser


def moment(value):
    """An offset-aware datetime from an ISO string or epoch number, else None.

    Naive strings are read as UTC (Granola writes UTC with a trailing Z)."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 1e11 else value
        try:
            return datetime.fromtimestamp(seconds, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        text += "T00:00:00"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def iso(value):
    when = moment(value) if not isinstance(value, datetime) else value
    return when.isoformat() if when else None


def _inline(node):
    """Inline text of a node; hard breaks become newlines."""
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return str(node.get("text") or "")
    if node.get("type") == "hardBreak":
        return "\n"
    return "".join(_inline(c) for c in node.get("content") or [])


def _blocks(node, depth=0):
    """Lines of a block node."""
    if isinstance(node, list):
        return [line for child in node for line in _blocks(child, depth)]
    if not isinstance(node, dict):
        return []
    kind = node.get("type")
    children = node.get("content") or []
    if kind in ("bulletList", "orderedList", "taskList"):
        lines = []
        for number, item in enumerate(children, 1):
            marker = f"{number}. " if kind == "orderedList" else "- "
            inner = _blocks((item or {}).get("content") if isinstance(item, dict) else [], depth + 1)
            inner = inner or [""]
            lines.append("  " * depth + marker + inner[0].lstrip())
            lines.extend(inner[1:])
        return lines
    if kind == "heading":
        level = (node.get("attrs") or {}).get("level") or 1
        return ["#" * int(level) + " " + _inline(node)]
    if kind in ("paragraph", "codeBlock"):
        return ("  " * depth + _inline(node)).split("\n") if depth else _inline(node).split("\n")
    if kind == "text":
        return [_inline(node)]
    return _blocks(children, depth)


def prosemirror_text(node):
    """Plain text of a ProseMirror/TipTap JSON document (Granola's editor)."""
    return "\n".join(line.rstrip() for line in _blocks(node) if line.strip()).strip()


class _Html(HTMLParser):
    BREAKS = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "hr",
              "ul", "ol", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in self.BREAKS:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.parts.append("#" * int(tag[1]) + " ")

    def handle_endtag(self, tag):
        if tag in self.BREAKS:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_text(value):
    if not isinstance(value, str) or not value.strip():
        return ""
    parser = _Html()
    parser.feed(value)
    parser.close()
    text = "".join(parser.parts)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def clean(value):
    return value.strip() if isinstance(value, str) and value.strip() else None
