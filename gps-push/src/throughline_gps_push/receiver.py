"""Push receiver: a small standard-library HTTP server that phone GPS apps
post to. It authenticates every request with a shared secret, validates the
body, and appends it to the spool directory the plugin reads.

Endpoints (all accept the shared secret as ``Authorization: Bearer <secret>``
or as the password of HTTP Basic auth; with GPS_PUSH_ALLOW_QUERY_TOKEN=1 also
as ``?token=<secret>``):

- ``POST /owntracks``  OwnTracks HTTP mode (answers ``[]``)
- ``POST /overland``   Overland (answers ``{"result":"ok"}``)
- ``GET|POST /gpslogger``  GPSLogger for Android custom URL (query string,
  form body or JSON body)
- ``POST /``           any of the above, detected from the body
- ``GET /healthz``     unauthenticated liveness check

Run it behind a TLS reverse proxy (see README); it listens on 127.0.0.1 by
default.
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

log = logging.getLogger("throughline_gps_push.receiver")

PREFIX = "gps"
DEFAULT_LISTEN = "127.0.0.1:8765"
DEFAULT_MAX_BODY = 5 * 1024 * 1024


class Rejected(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def detect(path, body, params):
    """The tracker format of a request: owntracks, overland or gpslogger."""
    route = path.rstrip("/").rsplit("/", 1)[-1].lower()
    if route in ("owntracks", "overland", "gpslogger"):
        return route
    if isinstance(body, dict) and "_type" in body:
        return "owntracks"
    if isinstance(body, list) and body and all(isinstance(b, dict) and "_type" in b for b in body):
        return "owntracks"
    if isinstance(body, dict) and isinstance(body.get("locations"), list):
        return "overland"
    merged = dict(params)
    if isinstance(body, dict):
        merged.update(body)
    if any(k in merged for k in ("lat", "latitude")):
        return "gpslogger"
    raise Rejected(400, "unrecognised tracker payload")


def validate(kind, body, params):
    """Raise Rejected unless the body has the shape of its format; return the
    body to spool."""
    if kind == "owntracks":
        items = body if isinstance(body, list) else [body]
        if not items or not all(isinstance(i, dict) and isinstance(i.get("_type"), str) for i in items):
            raise Rejected(400, "OwnTracks payload needs _type")
        return body
    if kind == "overland":
        if not isinstance(body, dict) or not isinstance(body.get("locations"), list):
            raise Rejected(400, "Overland payload needs a locations array")
        if not all(isinstance(f, dict) for f in body["locations"]):
            raise Rejected(400, "Overland locations must be GeoJSON features")
        return body
    merged = dict(params)
    if isinstance(body, dict):
        merged.update(body)
    lat = _float(merged.get("lat", merged.get("latitude")))
    lon = _float(merged.get("lon", merged.get("lng", merged.get("longitude"))))
    if lat is None or lon is None:
        raise Rejected(400, "GPSLogger request needs numeric lat and lon")
    return merged


RESPONSES = {
    "owntracks": ("application/json", b"[]"),
    "overland": ("application/json", b'{"result":"ok"}'),
    "gpslogger": ("text/plain", b"OK"),
}


class Receiver:
    def __init__(self, spool_dir, secret, *, allow_query_token=False, max_body=DEFAULT_MAX_BODY, clock=None):
        if not secret:
            raise ValueError("GPS_PUSH_RECEIVER_SECRET must be set")
        self.spool = SpoolWriter(spool_dir, PREFIX)
        self.secret = secret
        self.allow_query_token = allow_query_token
        self.max_body = max_body
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def authenticate(self, headers, params):
        """The Basic-auth user name ('' for Bearer/query); Rejected(401) otherwise."""
        header = headers.get("Authorization") or ""
        scheme, _, value = header.partition(" ")
        if scheme.lower() == "bearer" and hmac.compare_digest(value.strip().encode(), self.secret.encode()):
            return ""
        if scheme.lower() == "basic":
            try:
                user, _, password = base64.b64decode(value.strip()).decode().partition(":")
            except (ValueError, UnicodeDecodeError):
                raise Rejected(401, "bad credentials") from None
            if hmac.compare_digest(password.encode(), self.secret.encode()):
                return user
        token = params.get("token")
        if self.allow_query_token and token and hmac.compare_digest(token.encode(), self.secret.encode()):
            return ""
        raise Rejected(401, "bad credentials")

    def handle(self, method, target, headers, raw):
        """Process one request; returns (status, content type, body bytes)."""
        parts = urlsplit(target)
        path = parts.path or "/"
        params = dict(parse_qsl(parts.query, keep_blank_values=True))
        if path.rstrip("/") == "/healthz" and method == "GET":
            return 200, "text/plain", b"ok"
        try:
            user = self.authenticate(headers, params)
            params.pop("token", None)
            if len(raw) > self.max_body:
                raise Rejected(413, "body too large")
            body = None
            ctype = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if raw.strip():
                if ctype == "application/x-www-form-urlencoded":
                    body = dict(parse_qsl(raw.decode("utf-8", "replace"), keep_blank_values=True))
                    body.pop("token", None)
                else:
                    try:
                        body = json.loads(raw)
                    except ValueError:
                        raise Rejected(400, "body is not JSON") from None
            if method not in ("POST", "PUT") and not (method == "GET" and path.rstrip("/").endswith("gpslogger")):
                raise Rejected(405, "method not allowed")
            kind = detect(path, body, params)
            spooled = validate(kind, body, params)
            now = self.clock()
            record = {
                "v": 1,
                "received_at": now.isoformat(),
                "format": kind,
                "user": headers.get("X-Limit-U") or user or None,
                "device": headers.get("X-Limit-D") or None,
                "body": spooled,
            }
            self.spool.append(record, now.date().isoformat())
        except Rejected as error:
            return error.status, "application/json", json.dumps({"error": str(error)}).encode()
        content_type, response = RESPONSES[kind]
        return 200, content_type, response


def make_handler(receiver):
    class Handler(BaseHTTPRequestHandler):
        server_version = "throughline-gps-push"

        def _serve(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > receiver.max_body:
                self._reply(413, "application/json", b'{"error":"body too large"}')
                return
            raw = self.rfile.read(length) if length else b""
            status, ctype, body = receiver.handle(self.command, self.path, self.headers, raw)
            self._reply(status, ctype, body)

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
            # The request line can carry ?token=; log only method, path and status.
            log.info("%s %s", self.command, urlsplit(self.path).path)

    return Handler


def make_server(receiver, host="127.0.0.1", port=0):
    return ThreadingHTTPServer((host, port), make_handler(receiver))


def main(argv=None):
    parser = argparse.ArgumentParser(description="GPS push receiver for Throughline")
    parser.add_argument("--listen", default=os.getenv("GPS_PUSH_LISTEN", DEFAULT_LISTEN))
    parser.add_argument("--spool", default=os.getenv("GPS_PUSH_SPOOL_DIR"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if not args.spool:
        sys.exit("set GPS_PUSH_SPOOL_DIR or pass --spool")
    host, _, port = args.listen.rpartition(":")
    receiver = Receiver(
        args.spool,
        os.getenv("GPS_PUSH_RECEIVER_SECRET"),
        allow_query_token=os.getenv("GPS_PUSH_ALLOW_QUERY_TOKEN", "0") == "1",
        max_body=int(os.getenv("GPS_PUSH_MAX_BODY_BYTES") or DEFAULT_MAX_BODY),
    )
    server = make_server(receiver, host or "127.0.0.1", int(port))
    log.info("listening on %s:%s, spooling to %s", *server.server_address[:2], args.spool)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
