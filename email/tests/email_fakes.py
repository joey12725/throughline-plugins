"""Invented Gmail mailboxes behind a fake Composio (REST and MCP) on localhost.

Everything here is synthetic: example.invalid addresses, invented ids and
text. No test reaches a real network.
"""

import base64
import copy
import json
import re
import threading
from datetime import UTC, datetime
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


API_KEY = "fake-composio-key-canary"
MCP_TOKEN = "fake-mcp-token-canary"
MCP_PATH = "/mcp/fake-mcp-path-canary"


def b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def at_ms(text):
    return int(datetime.fromisoformat(text).timestamp() * 1000)


def gmail_message(
    ident,
    thread,
    when,
    sender,
    to,
    subject,
    *,
    plain=None,
    markup=None,
    cc=None,
    labels=("INBOX",),
    attachments=(),
    history=100,
):
    """A Gmail API style message (format=full) with a MIME part tree."""
    moment = datetime.fromisoformat(when)
    headers = [
        {"name": "From", "value": sender},
        {"name": "To", "value": to},
        {"name": "Subject", "value": subject},
        {"name": "Date", "value": format_datetime(moment)},
        {"name": "Message-ID", "value": f"<{ident}@mail.example.invalid>"},
    ]
    if cc:
        headers.append({"name": "Cc", "value": cc})
    bodies = []
    if plain is not None:
        bodies.append(
            {
                "partId": "0.0",
                "mimeType": "text/plain",
                "filename": "",
                "headers": [
                    {"name": "Content-Type", "value": "text/plain; charset=UTF-8"}
                ],
                "body": {"size": len(plain), "data": b64(plain)},
            }
        )
    if markup is not None:
        bodies.append(
            {
                "partId": "0.1",
                "mimeType": "text/html",
                "filename": "",
                "headers": [
                    {"name": "Content-Type", "value": "text/html; charset=UTF-8"}
                ],
                "body": {"size": len(markup), "data": b64(markup)},
            }
        )
    alternative = {
        "partId": "0",
        "mimeType": "multipart/alternative",
        "filename": "",
        "headers": [],
        "body": {"size": 0},
        "parts": bodies,
    }
    parts = [alternative]
    for number, (filename, mime, size) in enumerate(attachments, 1):
        parts.append(
            {
                "partId": str(number),
                "mimeType": mime,
                "filename": filename,
                "headers": [
                    {
                        "name": "Content-Disposition",
                        "value": f'attachment; filename="{filename}"',
                    }
                ],
                "body": {"size": size, "attachmentId": f"att-{ident}-{number}"},
            }
        )
    snippet = (plain or re.sub(r"<[^>]+>", " ", markup or ""))[:60].strip()
    return {
        "id": ident,
        "threadId": thread,
        "labelIds": list(labels),
        "snippet": snippet.replace("'", "&#39;"),
        "historyId": str(history),
        "internalDate": str(at_ms(when)),
        "sizeEstimate": 1000,
        "payload": {
            "partId": "",
            "mimeType": "multipart/mixed",
            "filename": "",
            "headers": headers,
            "body": {"size": 0},
            "parts": parts,
        },
    }


def flattened(message):
    """The flattened shape Composio returns without include_payload."""
    headers = {h["name"].lower(): h["value"] for h in message["payload"]["headers"]}
    text = ""
    for part in message["payload"]["parts"][0]["parts"]:
        if part["mimeType"] == "text/plain":
            text = base64.urlsafe_b64decode(part["body"]["data"] + "==").decode()
    return {
        "messageId": message["id"],
        "threadId": message["threadId"],
        "messageTimestamp": datetime.fromtimestamp(
            int(message["internalDate"]) / 1000, UTC
        )
        .isoformat()
        .replace("+00:00", "Z"),
        "labelIds": message["labelIds"],
        "sender": headers["from"],
        "to": headers["to"],
        "subject": headers["subject"],
        "messageText": text,
        "preview": {"subject": headers["subject"], "body": message["snippet"]},
    }


def mailboxes():
    """Two invented accounts; every date is historical."""
    alpha = [
        gmail_message(
            "a1",
            "ta",
            "2021-03-01T09:00:00+00:00",
            "Robin Example <robin@example.invalid>",
            "alpha@example.invalid",
            "Garden plans",
            plain="Shall we plant the invented tulips?\r\nRobin",
        ),
        gmail_message(
            "a2",
            "ta",
            "2021-03-01T10:30:00+00:00",
            "alpha@example.invalid",
            "Robin Example <robin@example.invalid>",
            "Re: Garden plans",
            plain="Yes, Saturday works.",
            labels=("SENT",),
        ),
        gmail_message(
            "a3",
            "tb",
            "2021-03-02T08:15:00+00:00",
            '"=?utf-8?q?Caf=C3=A9_Owner?=" <owner@cafe.example.invalid>',
            "alpha@example.invalid",
            "=?utf-8?q?Men=C3=BC_update?=",
            markup="<html><head><style>p{}</style></head><body><p>New menu</p><ul><li>Soup</li><li>Bread &amp; butter</li></ul></body></html>",
            labels=("INBOX", "UNREAD", "Label_7"),
        ),
        gmail_message(
            "a4",
            "tc",
            "2021-03-03T12:00:00+00:00",
            "Billing <billing@shop.example.invalid>",
            "alpha@example.invalid",
            "Invoice 42",
            plain="Invoice attached.",
            markup="<p>Invoice attached.</p>",
            cc="Ledger <ledger@example.invalid>, other@example.invalid",
            labels=("INBOX", "STARRED", "IMPORTANT"),
            attachments=[("invoice-42.pdf", "application/pdf", 2048)],
        ),
        gmail_message(
            "a5",
            "td",
            "2021-03-04T07:00:00+00:00",
            "Spammer <spam@junk.example.invalid>",
            "alpha@example.invalid",
            "Win",
            plain="Invented junk.",
            labels=("SPAM",),
        ),
    ]
    beta = [
        gmail_message(
            "b1",
            "tx",
            "2021-02-27T18:00:00-05:00",
            "Sam Sample <sam@example.invalid>",
            "beta@example.invalid",
            "Book club",
            plain="Next book is invented.",
        ),
        gmail_message(
            "b2",
            "tx",
            "2021-02-28T09:00:00-05:00",
            "beta@example.invalid",
            "Sam Sample <sam@example.invalid>",
            "RE: Book club",
            plain="Sounds good.",
            labels=("SENT",),
        ),
        gmail_message(
            "b3",
            "ty",
            "2021-03-05T11:00:00+00:00",
            "News <news@example.invalid>",
            "beta@example.invalid",
            "Weekly digest",
            plain="Invented news.",
            labels=("INBOX", "CATEGORY_UPDATES"),
        ),
    ]
    return {
        "ca_alpha": {
            "address": "Alpha@Example.invalid",
            "messages": alpha,
            "labels": [
                {"id": "INBOX", "name": "INBOX", "type": "system"},
                {"id": "Label_7", "name": "Food", "type": "user"},
                {"id": "Label_8", "name": "Unused", "type": "user"},
            ],
        },
        "ca_beta": {"address": "beta@example.invalid", "messages": beta, "labels": []},
    }  # fmt: skip


class FakeComposio:
    """Composio v3 tools execute and an MCP endpoint over invented mailboxes."""

    def __init__(self):
        self.accounts = mailboxes()
        for box in self.accounts.values():
            box.setdefault("history", [])
            box.setdefault("history_id", 500)
        self.calls = []
        self.failures = []  # [(tool, status, headers, body)] consumed in order
        self.expired = set()
        self.server = None

    # Mailbox changes -----------------------------------------------------

    def add(self, account, message):
        box = self.accounts[account]
        box["history_id"] += 1
        box["messages"].append(message)
        box["history"].append(
            {
                "id": str(box["history_id"]),
                "messages": [{"id": message["id"], "threadId": message["threadId"]}],
                "messagesAdded": [
                    {
                        "message": {
                            "id": message["id"],
                            "threadId": message["threadId"],
                            "labelIds": message["labelIds"],
                        }
                    }
                ],
            }
        )

    def relabel(self, account, ident, labels):
        box = self.accounts[account]
        box["history_id"] += 1
        message = next(m for m in box["messages"] if m["id"] == ident)
        message["labelIds"] = list(labels)
        box["history"].append(
            {
                "id": str(box["history_id"]),
                "messages": [{"id": ident, "threadId": message["threadId"]}],
                "labelsAdded": [
                    {
                        "message": {"id": ident, "threadId": message["threadId"]},
                        "labelIds": list(labels),
                    }
                ],
            }
        )

    def delete(self, account, ident):
        box = self.accounts[account]
        box["history_id"] += 1
        message = next(m for m in box["messages"] if m["id"] == ident)
        box["messages"].remove(message)
        box["history"].append(
            {
                "id": str(box["history_id"]),
                "messages": [{"id": ident, "threadId": message["threadId"]}],
                "messagesDeleted": [
                    {"message": {"id": ident, "threadId": message["threadId"]}}
                ],
            }
        )

    # Tool semantics -------------------------------------------------------

    def run_tool(self, account, tool, args):
        box = self.accounts.get(account)
        if box is None:
            return False, None, "Connected account not found"
        if tool == "GMAIL_GET_PROFILE":
            return (
                True,
                {
                    "emailAddress": box["address"],
                    "historyId": str(box["history_id"]),
                    "messagesTotal": len(box["messages"]),
                    "threadsTotal": len({m["threadId"] for m in box["messages"]}),
                },
                None,
            )
        if tool == "GMAIL_LIST_LABELS":
            return True, {"labels": box["labels"]}, None
        if tool == "GMAIL_FETCH_EMAILS":
            rows = list(box["messages"])
            query = args.get("query") or ""
            for op, value in re.findall(r"(after|before):(\d+)", query):
                bound = int(value) * 1000
                rows = [
                    m
                    for m in rows
                    if (
                        int(m["internalDate"]) >= bound
                        if op == "after"
                        else int(m["internalDate"]) < bound
                    )
                ]
            if not args.get("include_spam_trash"):
                rows = [m for m in rows if not {"SPAM", "TRASH"} & set(m["labelIds"])]
            rows.sort(key=lambda m: (-int(m["internalDate"]), m["id"]))
            offset = int((args.get("page_token") or "p0")[1:])
            size = int(args.get("max_results") or 10)
            page = rows[offset : offset + size]
            shape = copy.deepcopy if args.get("include_payload") else flattened
            data = {
                "messages": [shape(m) for m in page],
                "resultSizeEstimate": len(rows),
            }
            if offset + size < len(rows):
                data["nextPageToken"] = f"p{offset + size}"
            return True, data, None
        if tool == "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID":
            for message in box["messages"]:
                if message["id"] == args["message_id"]:
                    return True, copy.deepcopy(message), None
            return False, None, "Requested entity was not found. (404)"
        if tool == "GMAIL_LIST_HISTORY":
            if account in self.expired:
                return False, None, "Requested entity was not found. (HTTP 404)"
            start = int(args["start_history_id"])
            entries = [h for h in box["history"] if int(h["id"]) > start]
            offset = int((args.get("page_token") or "h0")[1:])
            size = int(args.get("max_results") or 100)
            data = {
                "history": entries[offset : offset + size],
                "historyId": str(box["history_id"]),
            }
            if offset + size < len(entries):
                data["nextPageToken"] = f"h{offset + size}"
            return True, data, None
        return False, None, f"unknown tool {tool}"

    def failure(self, tool):
        for index, item in enumerate(self.failures):
            if item[0] in (tool, "*"):
                return self.failures.pop(index)
        return None

    # Server ----------------------------------------------------------------

    def start(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, body, headers=(), content_type="application/json"):
                data = (
                    body.encode()
                    if isinstance(body, str)
                    else json.dumps(body).encode()
                )
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                for key, value in headers:
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path.startswith("/api/v3/tools/execute/"):
                    return self.rest(self.path.rsplit("/", 1)[1], body)
                if self.path == MCP_PATH:
                    return self.mcp(body)
                self.reply(404, {"error": "no route"})

            def rest(self, tool, body):
                if self.headers.get("x-api-key") != API_KEY:
                    return self.reply(401, {"error": {"message": "invalid api key"}})
                fake.calls.append(
                    (
                        "rest",
                        tool,
                        body.get("connected_account_id"),
                        body.get("arguments"),
                    )
                )
                failed = fake.failure(tool)
                if failed:
                    _, status, headers, payload = failed
                    return self.reply(status, payload, headers)
                ok, data, error = fake.run_tool(
                    body.get("connected_account_id"), tool, body.get("arguments") or {}
                )
                self.reply(
                    200,
                    {
                        "data": data or {},
                        "successful": ok,
                        "error": error,
                        "log_id": "log-fake",
                    },
                )

            def mcp(self, body):
                if self.headers.get("Authorization") != "Bearer " + MCP_TOKEN:
                    return self.reply(401, {"error": "unauthorized"})
                method = body.get("method")
                if method == "initialize":
                    return self.reply(
                        200,
                        {
                            "jsonrpc": "2.0",
                            "id": body["id"],
                            "result": {
                                "protocolVersion": "2025-03-26",
                                "capabilities": {},
                                "serverInfo": {"name": "fake"},
                            },
                        },
                        headers=[("Mcp-Session-Id", "fake-session")],
                    )
                if method == "notifications/initialized":
                    return self.reply(202, "")
                if self.headers.get("Mcp-Session-Id") != "fake-session":
                    return self.reply(400, {"error": "missing session"})
                spec = body["params"]["arguments"]["tools"][0]
                fake.calls.append(
                    ("mcp", spec["tool_slug"], spec["account"], spec["arguments"])
                )
                failed = fake.failure(spec["tool_slug"])
                if failed:
                    _, status, headers, payload = failed
                    return self.reply(status, payload, headers)
                ok, data, error = fake.run_tool(
                    spec["account"], spec["tool_slug"], spec["arguments"]
                )
                result = {
                    "successful": True,
                    "data": {
                        "results": [
                            {
                                "tool_slug": spec["tool_slug"],
                                "response": {
                                    "successful": ok,
                                    "data": truncate(data or {}),
                                    "error": error,
                                },
                            }
                        ]
                    },
                }
                message = {
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(result)}]
                    },
                }
                stream = "event: message\ndata: " + json.dumps(message) + "\n\n"
                self.reply(200, stream, content_type="text/event-stream")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def tools(self, account=None):
        return [c[1] for c in self.calls if account in (None, c[2])]


def truncate(value):
    """The Composio MCP proxy: lists over two items become a lossy preview."""
    if isinstance(value, list):
        items = [truncate(v) for v in value[:2]]
        return items + ["..."] if len(value) > 2 else items
    if isinstance(value, dict):
        return {k: truncate(v) for k, v in value.items()}
    return value


def live_env(fake, **extra):
    env = {
        "EMAIL_SOURCE": "live",
        "EMAIL_ACCOUNTS": "ca_alpha,ca_beta",
        "COMPOSIO_BASE_URL": fake.url,
        "COMPOSIO_API_KEY": API_KEY,
        "EMAIL_PAGE_SIZE": "2",
        "EMAIL_MIN_INTERVAL_S": "0",
        "EMAIL_RETRY_BASE_S": "0",
    }
    env.update(extra)
    return env
