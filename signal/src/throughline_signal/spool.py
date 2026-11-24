"""signal-cli JSON-RPC receive and the append-only JSONL spool.

`receive` on a signal-cli daemon started with `--receive-mode=manual` hands
over queued envelopes and acknowledges them to the Signal server: they will
never be delivered again. Every envelope is therefore appended (and fsynced)
to a JSONL spool before anything else happens, and Throughline records are
mapped from the spool only.
"""

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx


class RPCError(RuntimeError):
    pass


class SignalRPC:
    def __init__(self, url, account=None, token=None, transport=None, timeout=60):
        self.url = url.rstrip("/") + "/api/v1/rpc"
        self.account = account
        headers = {"Authorization": "Bearer " + token} if token else {}
        self.http = httpx.Client(transport=transport, timeout=timeout, headers=headers)

    def close(self):
        self.http.close()

    def call(self, method, **params):
        if self.account:
            params.setdefault("account", self.account)
        body = {"jsonrpc": "2.0", "method": method, "params": params, "id": str(uuid.uuid4())}
        response = self.http.post(self.url, json=body)
        response.raise_for_status()
        answer = response.json()
        if answer.get("error"):
            error = answer["error"]
            raise RPCError(f"signal-cli {method} failed: {error.get('message') or error}")
        return answer.get("result")

    def receive(self, timeout, max_messages):
        result = self.call("receive", timeout=timeout, maxMessages=max_messages)
        return result if isinstance(result, list) else []

    def groups(self):
        result = self.call("listGroups")
        return result if isinstance(result, list) else []


def wanted(item):
    envelope = (item or {}).get("envelope") or {}
    if any(k in envelope for k in ("dataMessage", "editMessage")):
        return True
    return bool((envelope.get("syncMessage") or {}).get("sentMessage"))


def append(spool_dir, items, account=None, now=None):
    """Append message envelopes to this month's spool file; fsync before return."""
    keep = [i for i in items if wanted(i)]
    if not keep:
        return 0
    spool = Path(spool_dir)
    spool.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now(timezone.utc)
    path = spool / f"signal-{now:%Y-%m}.jsonl"
    with open(path, "a", encoding="utf-8") as handle:
        for item in keep:
            if account and not item.get("account"):
                item = dict(item, account=account)
            handle.write(json.dumps(item, sort_keys=True, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return len(keep)


def drain(rpc, spool_dir, timeout=5, max_messages=500, max_rounds=20, account=None):
    """Receive until the queue is empty (or max_rounds), spooling each round."""
    total = 0
    for _ in range(max_rounds):
        items = rpc.receive(timeout, max_messages)
        total += append(spool_dir, items, account)
        if not items:
            break
    return total


def spool_files(spool_dir):
    path = Path(spool_dir)
    if not path.is_dir():
        return []
    return sorted(p for p in path.glob("*.jsonl") if p.is_file() and not p.name.startswith("."))


def read_lines(path, start=0):
    """(offset_after_line, parsed_json) for each complete line from start.

    A trailing line without a newline is still being written and is left for
    the next run; unparseable lines are skipped.
    """
    with open(path, "rb") as handle:
        handle.seek(start)
        offset = start
        for raw in handle:
            if not raw.endswith(b"\n"):
                break
            offset += len(raw)
            try:
                item = json.loads(raw)
            except ValueError:
                continue
            if isinstance(item, dict):
                yield offset, item
