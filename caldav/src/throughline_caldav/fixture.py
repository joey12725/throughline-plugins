"""Recorded HTTP answers served through httpx.MockTransport.

Used by the tests and by the *_FIXTURE setting (contract kit), never in
production. A fixture file is {"routes": [...]}; each route matches on
method, URL path, optional host, query subset, request-body substring and
request headers, and answers with status, response_headers and either
json, body or body_lines. The first matching route wins; nothing matching
is a 404.
"""

import json

import httpx


def handler_for(routes, log=None):
    def handler(request):
        body = request.content.decode("utf-8", "replace")
        if log is not None:
            log.append(request)
        for route in routes:
            if route.get("method", "GET").upper() != request.method:
                continue
            if route["path"] != request.url.path:
                continue
            if route.get("host") and route["host"] != request.url.host:
                continue
            if any(request.url.params.get(k) != v for k, v in (route.get("query") or {}).items()):
                continue
            if route.get("absent_query") and any(k in request.url.params for k in route["absent_query"]):
                continue
            if route.get("body_contains") and route["body_contains"] not in body:
                continue
            if route.get("body_lacks") and route["body_lacks"] in body:
                continue
            if any(request.headers.get(k) != v for k, v in (route.get("headers") or {}).items()):
                continue
            headers = dict(route.get("response_headers") or {})
            status = route.get("status", 200)
            if "json" in route:
                return httpx.Response(status, json=route["json"], headers=headers)
            text = route.get("body")
            if text is None:
                text = "\r\n".join(route.get("body_lines") or [])
            return httpx.Response(status, text=text, headers=headers)
        return httpx.Response(404, text=f"no fixture route for {request.method} {request.url.path}")

    return handler


def transport_from_file(path, log=None):
    with open(path, encoding="utf-8") as handle:
        spec = json.load(handle)
    return httpx.MockTransport(handler_for(spec["routes"], log))
