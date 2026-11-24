#!/usr/bin/env python3
"""Read Notion through Composio and write lossless-enough JSON Lines exports.

This module intentionally uses only the Python standard library. It can be
copied into the machine or container that holds COMPOSIO_MCP_TOKEN; only its
JSONL output and non-secret cursor state need to leave that environment.

The fetcher performs a complete, paginated metadata scan on every run. On the
first run it fetches every accessible page's Markdown. Later runs refetch page
content only when ``last_edited_time`` overlaps the stored high-water mark or
when metadata changed. A page that vanishes from a complete scan is emitted as
``missing`` (not claimed deleted), because Notion cannot distinguish trash,
revoked integration access, and a hard deletion once the object is invisible.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import logging
import os
import random
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path


VERSION = 1
DEFAULT_MCP_URL = "https://connect.composio.dev/mcp"
PAYLOAD_PREFIX = "THROUGHLINE_NOTION_EXPORT_B64="
READ_TOOLS = frozenset(
    {
        "NOTION_FETCH_DATA",
        "NOTION_FETCH_ALL_BLOCK_CONTENTS",
        "NOTION_GET_PAGE_MARKDOWN",
        "NOTION_LIST_USERS",
        "NOTION_WHO_AM_I",
    }
)

SETTINGS = [
    ("account", "NOTION_ACCOUNT", None, "Optional Composio Notion account alias or id"),
    ("page_size", "NOTION_PAGE_SIZE", "100", "Metadata records per Composio page (1-100)"),
    ("content_batch", "NOTION_CONTENT_BATCH", "8", "Page Markdown actions per workbench request"),
    ("overlap_s", "NOTION_OVERLAP_S", "3600", "Incremental last-edited overlap in seconds"),
    ("content_limit", "NOTION_CONTENT_LIMIT", "500000", "Markdown characters exported per page (0 = unlimited)"),
    ("max_calls", "NOTION_MAX_CALLS_PER_RUN", "0", "Composio actions per run (0 = unlimited)"),
    ("min_interval_s", "NOTION_MIN_INTERVAL_S", "0.5", "Minimum seconds between MCP requests"),
    ("retry_base_s", "NOTION_RETRY_BASE_S", "2", "Initial retry delay"),
    ("max_retries", "NOTION_MAX_RETRIES", "5", "Retries for rate limits and transient failures"),
    ("timeout_s", "NOTION_TIMEOUT_S", "240", "MCP HTTP timeout"),
]

SECRETS = [
    ("mcp_url", "COMPOSIO_MCP_URL", "Composio MCP URL; treat as secret because it may embed a token"),
    ("mcp_token", "COMPOSIO_MCP_TOKEN", "Composio MCP bearer token"),
]

LOG = logging.getLogger("throughline_notion.fetch")


class FetchError(RuntimeError):
    pass


class LimitReached(FetchError):
    pass


def utc_now():
    return datetime.now(UTC)


def iso(value):
    if isinstance(value, str):
        return value
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def integer(value, name, low=0, high=None):
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer") from None
    if result < low or (high is not None and result > high):
        limit = f" to {high}" if high is not None else " or greater"
        raise ValueError(f"{name} must be {low}{limit}")
    return result


def number(value, name, low=0):
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None
    if result < low:
        raise ValueError(f"{name} must be at least {low}")
    return result


def digest(value):
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def title_of(item):
    if item.get("object") == "database":
        values = item.get("title") or []
    else:
        values = []
        for prop in (item.get("properties") or {}).values():
            if isinstance(prop, dict) and prop.get("type") == "title":
                values = prop.get("title") or []
                break
    text = "".join(
        str(part.get("plain_text") or ((part.get("text") or {}).get("content")) or "")
        for part in values
        if isinstance(part, dict)
    ).strip()
    return text or "Untitled"


def page_summary(item):
    return {
        "id": item.get("id"),
        "title": title_of(item),
        "url": item.get("url"),
        "parent": item.get("parent"),
        "created_time": item.get("created_time"),
        "last_edited_time": item.get("last_edited_time"),
        "created_by": item.get("created_by"),
        "last_edited_by": item.get("last_edited_by"),
        "archived": bool(item.get("archived") or item.get("is_archived")),
        "in_trash": bool(item.get("in_trash")),
    }


def metadata_fingerprint(item):
    # request_id and transient Composio annotations never belong in an edit signature.
    stable = {k: v for k, v in item.items() if k not in {"request_id"}}
    return digest(stable)


def page_revision_changed(item, before):
    """Whether Notion identifies a new page revision.

    Page properties can contain short-lived signed file URLs, so hashing the
    complete search result creates false revisions. Notion's durable edit
    timestamp plus lifecycle/parent state is the authoritative cursor.
    """
    if not before or before.get("missing"):
        return True
    prior = before.get("summary") or {}
    return any(
        (
            before.get("last_edited_time") != item.get("last_edited_time"),
            prior.get("parent") != item.get("parent"),
            bool(prior.get("archived"))
            != bool(item.get("archived") or item.get("is_archived")),
            bool(prior.get("in_trash")) != bool(item.get("in_trash")),
        )
    )


def _plain_text(parts):
    if not isinstance(parts, list):
        return ""
    return "".join(
        str(part.get("plain_text") or ((part.get("text") or {}).get("content")) or "")
        for part in parts
        if isinstance(part, dict)
    ).strip()


def blocks_to_text(data):
    """Render the useful text of a Notion block tree without HTML or models."""
    lines = []

    def visit(block, depth=0):
        if not isinstance(block, dict):
            return
        kind = block.get("type")
        body = block.get(kind) if isinstance(kind, str) else None
        body = body if isinstance(body, dict) else {}
        text = _plain_text(body.get("rich_text"))
        prefix = ""
        if kind in {"heading_1", "heading_2", "heading_3"}:
            prefix = "#" * int(kind[-1]) + " "
        elif kind == "bulleted_list_item":
            prefix = "- "
        elif kind == "numbered_list_item":
            prefix = "1. "
        elif kind == "to_do":
            prefix = "- [x] " if body.get("checked") else "- [ ] "
        elif kind == "quote":
            prefix = "> "
        elif kind == "code":
            language = str(body.get("language") or "")
            text = f"```{language}\n{text}\n```"
        elif kind == "equation":
            text = str((body.get("expression") or block.get("expression") or "")).strip()
        elif kind == "divider":
            text = "---"
        elif kind in {"child_page", "child_database"}:
            text = str(body.get("title") or "").strip()
        elif kind == "table_row":
            cells = body.get("cells") or []
            text = " | ".join(_plain_text(cell) for cell in cells)
        elif kind in {"bookmark", "embed", "link_preview"}:
            text = str(body.get("url") or "").strip()
        elif kind in {"image", "video", "audio", "file", "pdf"}:
            caption = _plain_text(body.get("caption"))
            media = body.get(body.get("type")) if isinstance(body.get("type"), str) else None
            url = (media or {}).get("url") if isinstance(media, dict) else ""
            text = " — ".join(part for part in (caption, str(url or "")) if part)
        if text:
            lines.append("  " * depth + prefix + text)
        children = block.get("children") or body.get("children") or []
        if isinstance(children, dict):
            children = children.get("results") or children.get("blocks") or []
        for child in children if isinstance(children, list) else []:
            visit(child, depth + 1)

    if isinstance(data, list):
        roots = data
    elif isinstance(data, dict):
        roots = data.get("results") or data.get("blocks") or data.get("children") or []
        if isinstance(roots, dict):
            roots = roots.get("results") or roots.get("blocks") or []
    else:
        roots = []
    for root in roots if isinstance(roots, list) else []:
        visit(root)
    return "\n".join(lines).strip()


@dataclass
class Config:
    account: str | None
    page_size: int
    content_batch: int
    overlap_s: int
    content_limit: int
    max_calls: int
    min_interval_s: float
    retry_base_s: float
    max_retries: int
    timeout_s: float
    mcp_url: str
    mcp_token: str

    @classmethod
    def from_env(cls, env=None, **overrides):
        env = env or os.environ
        values = {}
        for key, name, default, _ in SETTINGS:
            values[key] = overrides.get(key)
            if values[key] is None:
                values[key] = env.get(name, default)
        mcp_url = overrides.get("mcp_url") or env.get("COMPOSIO_MCP_URL") or DEFAULT_MCP_URL
        mcp_token = overrides.get("mcp_token") or env.get("COMPOSIO_MCP_TOKEN") or ""
        token_command = env.get("COMPOSIO_MCP_TOKEN_COMMAND")
        if not mcp_token and token_command:
            # A command that prints the token, e.g. a secret manager on the
            # host that holds it; the token never touches argv or disk.
            try:
                mcp_token = subprocess.check_output(
                    shlex.split(token_command),
                    text=True,
                    stderr=subprocess.DEVNULL,
                    timeout=60,
                ).strip()
            except (OSError, subprocess.SubprocessError):
                mcp_token = ""
        if not mcp_token:
            raise ValueError("COMPOSIO_MCP_TOKEN is required")
        return cls(
            account=(values["account"] or "").strip() or None,
            page_size=integer(values["page_size"], "NOTION_PAGE_SIZE", 1, 100),
            content_batch=integer(values["content_batch"], "NOTION_CONTENT_BATCH", 1, 50),
            overlap_s=integer(values["overlap_s"], "NOTION_OVERLAP_S"),
            content_limit=integer(values["content_limit"], "NOTION_CONTENT_LIMIT"),
            max_calls=integer(values["max_calls"], "NOTION_MAX_CALLS_PER_RUN"),
            min_interval_s=number(values["min_interval_s"], "NOTION_MIN_INTERVAL_S"),
            retry_base_s=number(values["retry_base_s"], "NOTION_RETRY_BASE_S"),
            max_retries=integer(values["max_retries"], "NOTION_MAX_RETRIES"),
            timeout_s=number(values["timeout_s"], "NOTION_TIMEOUT_S", 1),
            mcp_url=mcp_url,
            mcp_token=mcp_token,
        )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise FetchError("Composio MCP redirect refused")


class Workbench:
    """Small MCP client that executes an allow-listed batch in Remote Workbench."""

    def __init__(self, config, opener=None, sleep=time.sleep, clock=time.monotonic):
        self.config = config
        self.opener = opener or urllib.request.build_opener(_NoRedirect()).open
        self.sleep = sleep
        self.clock = clock
        self.session = "throughline-notion-" + uuid.uuid4().hex
        self.last_request = None
        self.calls = 0

    def scrub(self, text):
        result = str(text)
        for secret in sorted((self.config.mcp_token, self.config.mcp_url), key=len, reverse=True):
            if secret:
                result = result.replace(secret, "[redacted]")
        return result

    def _pace(self):
        if self.last_request is not None:
            remaining = self.config.min_interval_s - (self.clock() - self.last_request)
            if remaining > 0:
                self.sleep(remaining)
        self.last_request = self.clock()

    @staticmethod
    def _rpc_message(text):
        if text.lstrip().startswith("{"):
            return json.loads(text)
        messages = []
        for line in text.splitlines():
            if line.startswith("data:") and line[5:].strip() not in {"", "[DONE]"}:
                messages.append(json.loads(line[5:].strip()))
        for message in messages:
            if isinstance(message, dict) and ("result" in message or "error" in message):
                return message
        raise FetchError("Composio MCP returned no JSON-RPC result")

    def _decode(self, text):
        message = self._rpc_message(text)
        if message.get("error"):
            raise FetchError("Composio MCP returned a JSON-RPC error")
        try:
            content = (message.get("result") or {})["content"]
            workbench = json.loads(next(x["text"] for x in content if x.get("type") == "text"))
            data = workbench.get("data") or {}
            if workbench.get("successful") is not True or data.get("error") or data.get("stderr"):
                raise ValueError
            stdout = data.get("stdout") or ""
            payloads = [line[len(PAYLOAD_PREFIX) :] for line in stdout.splitlines() if line.startswith(PAYLOAD_PREFIX)]
            if len(payloads) != 1:
                raise ValueError
            decoded = json.loads(zlib.decompress(base64.b64decode(payloads[0], validate=True)))
            if not isinstance(decoded, dict):
                raise ValueError
            return decoded
        except (KeyError, StopIteration, TypeError, ValueError, zlib.error):
            raise FetchError("Composio workbench response was invalid") from None

    def call_many(self, calls):
        checked = []
        for label, tool, arguments in calls:
            if tool not in READ_TOOLS:
                raise ValueError(f"Notion tool {tool} is not read-only allow-listed")
            checked.append((label, tool, arguments))
        if self.config.max_calls and self.calls + len(checked) > self.config.max_calls:
            raise LimitReached("NOTION_MAX_CALLS_PER_RUN reached")
        self.calls += len(checked)
        account = repr(self.config.account)
        code = (
            "import base64, json, zlib\n"
            f"_specs = json.loads({json.dumps(checked)!r})\n"
            "_out = {}\n"
            "for _label, _tool, _arguments in _specs:\n"
            f"    _result, _error = run_composio_tool(_tool, _arguments, account={account})\n"
            "    _out[_label] = {'result': _result, 'error': _error}\n"
            "_blob = zlib.compress(json.dumps(_out, ensure_ascii=False).encode())\n"
            f"print({PAYLOAD_PREFIX!r} + base64.b64encode(_blob).decode())\n"
        )
        request_body = {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex,
            "method": "tools/call",
            "params": {
                "name": "COMPOSIO_REMOTE_WORKBENCH",
                "arguments": {
                    "code_to_execute": code,
                    "session_id": self.session,
                    "current_step": "SYNC_NOTION_READ_ONLY",
                    "thought": "Read paginated Notion metadata and page text without changing the workspace.",
                },
            },
        }
        encoded = json.dumps(request_body).encode()
        last = None
        for attempt in range(self.config.max_retries + 1):
            self._pace()
            request = urllib.request.Request(
                self.config.mcp_url,
                data=encoded,
                headers={
                    "Authorization": "Bearer " + self.config.mcp_token,
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
            )
            try:
                with self.opener(request, timeout=self.config.timeout_s) as response:
                    decoded = self._decode(response.read().decode())
                # Per-action errors stay attached to their labels. Callers can
                # isolate a bad page from healthy peers in the same workbench
                # request instead of discarding the entire batch.
                return decoded
            except urllib.error.HTTPError as error:
                last = FetchError(f"Composio MCP HTTP {error.code}")
                if error.code not in {429, 500, 502, 503, 504} or attempt >= self.config.max_retries:
                    raise last from None
                retry = error.headers.get("Retry-After")
                try:
                    delay = float(retry) if retry is not None else self.config.retry_base_s * (2**attempt)
                except ValueError:
                    delay = self.config.retry_base_s * (2**attempt)
                self.sleep(min(120.0, max(0.0, delay)))
                continue
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                last = FetchError("Composio MCP transport unavailable: " + self.scrub(error)[:200])
                if attempt >= self.config.max_retries:
                    raise last from None
            delay = min(120.0, self.config.retry_base_s * (2**attempt))
            self.sleep(delay + random.random() * min(1.0, delay / 4))
        raise last or FetchError("Composio retry budget exhausted")

    def call(self, tool, arguments):
        last = None
        for attempt in range(self.config.max_retries + 1):
            entry = self.call_many([("one", tool, arguments)])["one"]
            if not entry.get("error"):
                return tool_data(entry.get("result"))
            text = self.scrub(str(entry.get("error")))[:300]
            last = FetchError("Notion action failed: " + text)
            transient = any(
                word in text.lower()
                for word in ("429", "rate", "timeout", "tempor", "500", "502", "503", "504")
            )
            if not transient or attempt >= self.config.max_retries:
                raise last
            delay = min(120.0, self.config.retry_base_s * (2**attempt))
            self.sleep(delay + random.random() * min(1.0, delay / 4))
        raise last or FetchError("Notion action retry budget exhausted")


def tool_data(value):
    """Remove Composio result envelopes while preserving actual Notion lists."""
    for _ in range(8):
        if not isinstance(value, dict):
            raise FetchError("Notion action returned an unexpected shape")
        if value.get("successful") is False or value.get("error"):
            raise FetchError("Notion action was unsuccessful")
        if isinstance(value.get("response_data"), dict):
            value = value["response_data"]
            continue
        if isinstance(value.get("data"), dict) and not any(
            key in value for key in ("results", "has_more", "markdown", "id", "object")
        ):
            value = value["data"]
            continue
        return value
    raise FetchError("Notion action response was nested too deeply")


class Fetcher:
    def __init__(self, config, state=None, *, client=None, now=utc_now):
        self.config = config
        self.state = copy.deepcopy(state or {})
        if self.state.get("version") not in (None, VERSION):
            # A fetcher upgrade safely re-reads the accessible workspace.
            self.state = {}
        self.state.setdefault("version", VERSION)
        self.state.setdefault("pages", {})
        self.state.setdefault("databases", {})
        self.client = client or Workbench(config)
        self.now = now
        self.errors = []
        self.truncated = []
        self.records = []
        self.window_from = None
        self.window_to = None

    def record(self, kind, raw, **extra):
        item = {"kind": kind, "fetched_at": iso(self.run_at), "raw": raw}
        item.update(extra)
        self.records.append(item)

    def paged(self, tool, base, result_key="results"):
        cursor = None
        seen = set()
        while True:
            arguments = dict(base, page_size=self.config.page_size)
            if cursor:
                arguments["start_cursor"] = cursor
            data = self.client.call(tool, arguments)
            items = data.get(result_key) or []
            if not isinstance(items, list):
                raise FetchError(f"{tool} returned a non-list {result_key}")
            for item in items:
                if isinstance(item, dict):
                    yield item
            cursor = data.get("next_cursor")
            if not data.get("has_more"):
                return
            if not cursor or cursor in seen:
                raise FetchError(f"{tool} pagination did not advance")
            seen.add(cursor)

    def users(self):
        users = list(self.paged("NOTION_LIST_USERS", {}))
        for user in users:
            self.record("notion.user", user, id=str(user.get("id") or ""))
        return users

    def databases(self):
        current = {}
        for item in self.paged("NOTION_FETCH_DATA", {"fetch_type": "databases"}):
            ident = str(item.get("id") or "")
            if not ident:
                continue
            current[ident] = item
            signature = metadata_fingerprint(item)
            before = self.state["databases"].get(ident) or {}
            if signature != before.get("fingerprint"):
                self.record("notion.database", item, id=ident)
            self.state["databases"][ident] = {
                "fingerprint": signature,
                "last_edited_time": item.get("last_edited_time"),
                "title": title_of(item),
            }
        for ident in sorted(set(self.state["databases"]) - set(current)):
            before = self.state["databases"][ident]
            if not before.get("missing"):
                raw = {
                    "object": "database",
                    "id": ident,
                    "title_text": before.get("title") or "Unavailable database",
                    "last_edited_time": iso(self.run_at),
                    "archived": False,
                    "in_trash": False,
                }
                self.record("notion.database", raw, id=ident, missing=True, observed_at=iso(self.run_at))
            before["missing"] = True

    def _content(self, pages):
        results = {}
        for start in range(0, len(pages), self.config.content_batch):
            chunk = pages[start : start + self.config.content_batch]
            calls = [
                (str(i), "NOTION_GET_PAGE_MARKDOWN", {"page_id": page["id"]})
                for i, page in enumerate(chunk)
            ]
            try:
                answer = self.client.call_many(calls)
            except LimitReached:
                raise
            except FetchError as error:
                # An oversized/invalid combined workbench response has no
                # per-page results. Retry every page alone below; those calls
                # can then fall back to recursive blocks independently.
                answer = {
                    str(i): {"result": None, "error": str(error)[:300]}
                    for i in range(len(chunk))
                }
            for i, page in enumerate(chunk):
                entry = answer.get(str(i)) or {}
                try:
                    try:
                        if entry.get("error"):
                            # Retry this page alone, so one inaccessible object
                            # does not turn every healthy neighbor into a gap.
                            data = self.client.call(
                                "NOTION_GET_PAGE_MARKDOWN", {"page_id": page["id"]}
                            )
                        else:
                            data = tool_data(entry.get("result"))
                        markdown = data.get("markdown") or ""
                        if not isinstance(markdown, str):
                            raise FetchError("Notion page Markdown was not text")
                    except FetchError:
                        # Some Composio versions put the tool failure inside the
                        # result envelope rather than entry.error. Either shape
                        # falls back to raw recursive block reads.
                        arguments = {
                            "block_id": page["id"],
                            "max_depth": 10,
                            "page_size": self.config.page_size,
                        }
                        if page.get("url"):
                            arguments["page_url"] = page["url"]
                        blocks = self.client.call(
                            "NOTION_FETCH_ALL_BLOCK_CONTENTS", arguments
                        )
                        data = {
                            "markdown": blocks_to_text(blocks),
                            "truncated": bool(blocks.get("has_more")),
                            "unknown_block_ids": [],
                            "content_source": "blocks",
                        }
                        markdown = data["markdown"]
                    source_chars = len(markdown)
                    local_cut = bool(self.config.content_limit and source_chars > self.config.content_limit)
                    if local_cut:
                        markdown = markdown[: self.config.content_limit]
                    notion_cut = bool(data.get("truncated"))
                    if local_cut or notion_cut:
                        self.truncated.append(page["id"])
                    results[page["id"]] = {
                        "markdown": markdown,
                        "source_chars": source_chars,
                        "content_truncated": local_cut,
                        "notion_truncated": notion_cut,
                        "unknown_block_ids": data.get("unknown_block_ids") or [],
                        "content_source": data.get("content_source") or "markdown",
                    }
                except FetchError as error:
                    self.errors.append({"id": page["id"], "at": page.get("last_edited_time"), "error": str(error)[:300]})
        return results

    def pages(self):
        previous = self.state["pages"]
        current = {}
        changed = []
        highest = parse_time(self.state.get("high_water"))
        cutoff = highest - timedelta(seconds=self.config.overlap_s) if highest else None
        maximum = highest
        for item in self.paged(
            "NOTION_FETCH_DATA",
            {
                "fetch_type": "pages",
                "sort_direction": "descending",
                "sort_timestamp": "last_edited_time",
            },
        ):
            ident = str(item.get("id") or "")
            if not ident:
                continue
            current[ident] = item
            edited = parse_time(item.get("last_edited_time"))
            if edited and (self.window_from is None or edited < self.window_from):
                self.window_from = edited
            if edited and (self.window_to is None or edited > self.window_to):
                self.window_to = edited
            if edited and (maximum is None or edited > maximum):
                maximum = edited
            before = previous.get(ident) or {}
            overlaps = bool(edited and cutoff and edited >= cutoff)
            if page_revision_changed(item, before) or overlaps:
                changed.append(item)
            elif before.get("pending_content"):
                changed.append(item)

        readable = [
            page
            for page in changed
            if not (page.get("archived") or page.get("is_archived") or page.get("in_trash"))
        ]
        try:
            contents = self._content(readable)
        except LimitReached:
            contents = {}

        for item in changed:
            ident = str(item["id"])
            unavailable = bool(item.get("archived") or item.get("is_archived") or item.get("in_trash"))
            content = {
                "markdown": "",
                "source_chars": 0,
                "content_truncated": False,
                "notion_truncated": False,
                "unknown_block_ids": [],
                "content_source": "metadata",
                "content_unavailable": False,
            } if unavailable else contents.get(ident)
            if content is None:
                # Preserve the page's searchable metadata without claiming its
                # body was read. Keep it pending so a future run can upgrade the
                # same stable UUID to a full-content version.
                before = previous.get(ident) or {}
                metadata_hash = metadata_fingerprint(item)
                if page_revision_changed(item, before):
                    self.record(
                        "notion.page",
                        item,
                        id=ident,
                        markdown="",
                        source_chars=0,
                        content_truncated=False,
                        notion_truncated=False,
                        unknown_block_ids=[],
                        content_source="unavailable",
                        content_unavailable=True,
                    )
                previous[ident] = {
                    "fingerprint": metadata_hash,
                    "content_hash": None,
                    "last_edited_time": item.get("last_edited_time"),
                    "summary": page_summary(item),
                    "missing": False,
                    "pending_content": True,
                }
                continue
            content_hash = digest(content)
            before = previous.get(ident) or {}
            metadata_hash = metadata_fingerprint(item)
            if page_revision_changed(item, before) or before.get("pending_content"):
                self.record("notion.page", item, id=ident, **content)
            previous[ident] = {
                "fingerprint": metadata_hash,
                "content_hash": content_hash,
                "last_edited_time": item.get("last_edited_time"),
                "summary": page_summary(item),
                "missing": False,
                "pending_content": False,
            }

        for ident in sorted(set(previous) - set(current)):
            before = previous[ident]
            if not before.get("missing"):
                summary = copy.deepcopy(before.get("summary") or {"id": ident, "title": "Unavailable page"})
                summary.update(
                    {
                        "object": "page",
                        "id": ident,
                        "last_edited_time": iso(self.run_at),
                        "archived": False,
                        "in_trash": False,
                    }
                )
                self.record(
                    "notion.page",
                    summary,
                    id=ident,
                    missing=True,
                    observed_at=iso(self.run_at),
                    markdown="",
                    source_chars=0,
                    content_truncated=False,
                    notion_truncated=False,
                    unknown_block_ids=[],
                )
            before["missing"] = True
        if maximum:
            self.state["high_water"] = iso(maximum)

    def run(self):
        self.run_at = self.now()
        first = not bool(self.state.get("initialized"))
        # WHO_AM_I both validates the active Notion connection and identifies the
        # accessible workspace without putting credentials in the export.
        identity = self.client.call("NOTION_WHO_AM_I", {})
        self.record("notion.workspace", identity, id=str(identity.get("id") or "workspace"))
        self.users()
        self.databases()
        self.pages()
        self.state["initialized"] = True
        meta = {
            "kind": "export.meta",
            "fetched_at": iso(self.run_at),
            "raw": {
                "fetcher_version": VERSION,
                "mode": "backfill" if first else "incremental",
                "high_water": self.state.get("high_water"),
                "window_from": iso(self.window_from) if self.window_from else None,
                "window_to": iso(self.window_to) if self.window_to else None,
                "full_metadata_scan": True,
                "accessible_scope": "pages shared with the Notion integration",
                "page_errors": self.errors,
                "truncated_page_ids": self.truncated,
                "calls": self.client.calls,
            },
        }
        self.records.append(meta)
        return self.records, copy.deepcopy(self.state)


def write_state(path, state):
    target = Path(path)
    temporary = target.with_name("." + target.name + "." + uuid.uuid4().hex)
    temporary.write_text(json.dumps(state, sort_keys=True, indent=1), encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, target)


def export(args, env=None):
    out = Path(args.out)
    if out.exists() and not args.append:
        raise ValueError("output file exists; pass --append or choose a new file")
    state = {}
    if args.state and Path(args.state).exists():
        state = json.loads(Path(args.state).read_text(encoding="utf-8"))
    config = Config.from_env(env)
    records, next_state = Fetcher(config, state).run()
    mode = "a" if args.append else "w"
    with out.open(mode, encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    if args.state:
        write_state(args.state, next_state)
    counts = {}
    for record in records:
        counts[record["kind"]] = counts.get(record["kind"], 0) + 1
    meta = records[-1]["raw"]
    return {
        "out": str(out),
        "counts": counts,
        "mode": meta["mode"],
        "high_water": meta["high_water"],
        "calls": meta["calls"],
        "errors": len(meta["page_errors"]),
        "truncated": len(meta["truncated_page_ids"]),
    }


def main(argv=None, env=None):
    parser = argparse.ArgumentParser(prog="throughline-notion-fetch", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("export", help="write a Notion JSONL export")
    command.add_argument("--out", required=True)
    command.add_argument("--state")
    command.add_argument("--append", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    try:
        summary = export(args, env)
    except (FetchError, ValueError, OSError, json.JSONDecodeError) as error:
        print(f"error: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
