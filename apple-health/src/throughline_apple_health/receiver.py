"""Push receiver for the Health Auto Export iOS app's "REST API" automation.

A small standard-library HTTP server: it authenticates each request with a
shared secret (``Authorization: Bearer <secret>``, HTTP Basic with the secret
as the password, or ``?token=<secret>`` when APPLE_HEALTH_ALLOW_QUERY_TOKEN=1),
checks that the body is a Health Auto Export JSON document
(``{"data": {"metrics": [...], "workouts": [...]}}``) and appends it to the
spool directory the plugin reads. ``GET /healthz`` answers ``ok`` without
authentication. Run it behind a TLS reverse proxy (see README).
"""

import argparse
import base64
import hmac
import json
import logging
import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

from .spool import SpoolWriter

log = logging.getLogger("throughline_apple_health.receiver")

PREFIX = "health"
DEFAULT_LISTEN = "127.0.0.1:8766"
DEFAULT_MAX_BODY = 100 * 1024 * 1024
SECTIONS = ("metrics", "workouts")


class Rejected(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def validate(body):
    if not isinstance(body, dict) or not isinstance(body.get("data"), dict):
        raise Rejected(400, "expected a Health Auto Export JSON document with a data object")
    data = body["data"]
    if not any(isinstance(data.get(key), list) for key in SECTIONS):
        raise Rejected(400, "data needs a metrics or workouts array")
    for key in SECTIONS:
        if key in data and not (isinstance(data[key], list) and all(isinstance(i, dict) for i in data[key])):
            raise Rejected(400, f"data.{key} must be an array of objects")
    return body


class Receiver:
    def __init__(self, spool_dir, secret, *, allow_query_token=False, max_body=DEFAULT_MAX_BODY, clock=None):
        if not secret:
            raise ValueError("APPLE_HEALTH_RECEIVER_SECRET must be set")
        self.spool = SpoolWriter(spool_dir, PREFIX)
        self.secret = secret
        self.allow_query_token = allow_query_token
        self.max_body = max_body
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def authenticate(self, headers, params):
        header = headers.get("Authorization") or ""
        scheme, _, value = header.partition(" ")
        if scheme.lower() == "bearer" and hmac.compare_digest(value.strip().encode(), self.secret.encode()):
            return
        if scheme.lower() == "basic":
            try:
                _, _, password = base64.b64decode(value.strip()).decode().partition(":")
            except (ValueError, UnicodeDecodeError):
                raise Rejected(401, "bad credentials") from None
            if hmac.compare_digest(password.encode(), self.secret.encode()):
                return
        token = params.get("token")
        if self.allow_query_token and token and hmac.compare_digest(token.encode(), self.secret.encode()):
            return
        raise Rejected(401, "bad credentials")

    def handle(self, method, target, headers, raw):
        parts = urlsplit(target)
        params = dict(parse_qsl(parts.query, keep_blank_values=True))
        if (parts.path or "/").rstrip("/") == "/healthz" and method == "GET":
            return 200, "text/plain", b"ok"
        try:
            self.authenticate(headers, params)
            if method not in ("POST", "PUT"):
                raise Rejected(405, "method not allowed")
            if len(raw) > self.max_body:
                raise Rejected(413, "body too large")
            try:
                body = json.loads(raw)
            except ValueError:
                raise Rejected(400, "body is not JSON") from None
            validate(body)
            now = self.clock()
            record = {
                "v": 1,
                "received_at": now.isoformat(),
                "format": "health_auto_export",
                # Health Auto Export can send an automation name header; keep
                # it so several automations can be told apart.
                "automation": headers.get("automation-name") or headers.get("X-Automation-Name") or None,
                "body": body,
            }
            self.spool.append(record, now.date().isoformat())
        except Rejected as error:
            return error.status, "application/json", json.dumps({"error": str(error)}).encode()
        return 200, "application/json", b'{"status":"ok"}'


def make_handler(receiver):
    class Handler(BaseHTTPRequestHandler):
        server_version = "throughline-apple-health"

        def _serve(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > receiver.max_body:
                self._reply(413, "application/json", b'{"error":"body too large"}')
                return
            raw = self.rfile.read(length) if length else b""
            self._reply(*receiver.handle(self.command, self.path, self.headers, raw))

        def _reply(self, status, ctype, body):
            self.send_response(status)
            if status == 401:
                self.send_header("WWW-Authenticate", 'Basic realm="throughline"')
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = do_PUT = _serve

        def log_message(self, format, *args):
            log.info("%s %s", self.command, urlsplit(self.path).path)

    return Handler


def make_server(receiver, host="127.0.0.1", port=0):
    return ThreadingHTTPServer((host, port), make_handler(receiver))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Health Auto Export receiver for Throughline")
    parser.add_argument("--listen", default=os.getenv("APPLE_HEALTH_LISTEN", DEFAULT_LISTEN))
    parser.add_argument("--spool", default=os.getenv("APPLE_HEALTH_SPOOL_DIR"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if not args.spool:
        sys.exit("set APPLE_HEALTH_SPOOL_DIR or pass --spool")
    host, _, port = args.listen.rpartition(":")
    receiver = Receiver(
        args.spool,
        os.getenv("APPLE_HEALTH_RECEIVER_SECRET"),
        allow_query_token=os.getenv("APPLE_HEALTH_ALLOW_QUERY_TOKEN", "0") == "1",
        max_body=int(os.getenv("APPLE_HEALTH_MAX_BODY_BYTES") or DEFAULT_MAX_BODY),
    )
    server = make_server(receiver, host or "127.0.0.1", int(port))
    log.info("listening on %s:%s, spooling to %s", *server.server_address[:2], args.spool)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
