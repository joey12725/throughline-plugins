"""An offline SimpleFIN Bridge for tests and the contract kit.

`SIMPLEFIN_FIXTURE` names a JSON file shaped like one /accounts answer that
holds every transaction (`{"errors": [...], "accounts": [...]}`). The
transport answers GET .../accounts the way the Bridge does: transactions are
filtered to the requested [start-date, end-date) window (pending ones by
`transacted_at`), and `pending=1` keeps pending transactions. POST to any
other path answers a claim with an invented access URL.
"""

import copy
import json
from pathlib import Path

import httpx


def _when(txn):
    return txn.get("posted") or txn.get("transacted_at") or 0


def bridge(data):
    """httpx.MockTransport serving `data` (a parsed fixture)."""

    def handler(request):
        if request.method == "POST":
            return httpx.Response(
                200, text="https://invented-user:invented-pass@bridge.example.com/simplefin"
            )
        if not request.url.path.endswith("/accounts"):
            return httpx.Response(404, text="not found")
        query = request.url.params
        start = int(query.get("start-date") or 0)
        end = int(query.get("end-date") or 2**62)
        pending = query.get("pending") == "1"
        wanted = set(query.get_list("account"))
        answer = copy.deepcopy(data)
        accounts = []
        for account in answer.get("accounts", []):
            if wanted and account["id"] not in wanted:
                continue
            if query.get("balances-only") == "1":
                account["transactions"] = []
            else:
                account["transactions"] = [
                    t
                    for t in account.get("transactions", [])
                    if start <= _when(t) < end and (pending or not t.get("pending"))
                ]
            accounts.append(account)
        answer["accounts"] = accounts
        return httpx.Response(200, json=answer)

    return httpx.MockTransport(handler)


def from_file(path):
    return bridge(json.loads(Path(path).read_text(encoding="utf-8")))
