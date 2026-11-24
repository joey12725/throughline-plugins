"""URL filtering and redaction applied before anything leaves the machine."""

from __future__ import annotations

import fnmatch
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

DEFAULT_EXCLUDE = ("localhost", "127.0.0.1", "0.0.0.0", "[::1]", "::1")
DEFAULT_REDACT = (
    "access_token", "auth", "authorization", "code", "id_token", "key", "api_key", "apikey",
    "password", "passwd", "pwd", "refresh_token", "secret", "client_secret", "session", "sessionid",
    "sid", "sig", "signature", "token", "otp", "x-amz-signature", "x-amz-credential", "x-goog-signature",
)
REDACTED = "REDACTED"


class Privacy:
    def __init__(self, *, schemes=("http", "https"), exclude=(), redact=DEFAULT_REDACT, strip_query=False):
        self.schemes = {s.lower() for s in schemes}
        self.exclude = [d.strip().lower().lstrip(".") for d in exclude if d.strip()]
        self.redact = {p.strip().lower() for p in redact if p.strip()}
        self.strip_query = strip_query

    @staticmethod
    def host(url):
        try:
            return (urlsplit(url).hostname or "").lower()
        except ValueError:
            return ""

    def excluded_host(self, host):
        for rule in self.exclude:
            if "*" in rule or "?" in rule:
                if fnmatch.fnmatchcase(host, rule):
                    return True
            elif host == rule or host.endswith("." + rule) or rule.strip("[]") == host:
                return True
        return False

    def allowed(self, url):
        if not url:
            return False
        try:
            parts = urlsplit(url)
        except ValueError:
            return False
        if parts.scheme.lower() not in self.schemes:
            return False
        return not self.excluded_host((parts.hostname or "").lower())

    def _params(self, text):
        if not text or "=" not in text:
            return text
        pairs = parse_qsl(text, keep_blank_values=True)
        if not pairs:
            return text
        changed = False
        out = []
        for name, value in pairs:
            if name.lower() in self.redact and value:
                out.append((name, REDACTED))
                changed = True
            else:
                out.append((name, value))
        return urlencode(out, safe="/:@") if changed else text

    def clean(self, url):
        """Drop userinfo, redact sensitive query/fragment parameters (or the
        whole query when strip_query is set)."""
        try:
            parts = urlsplit(url)
        except ValueError:
            return url
        netloc = parts.netloc.rsplit("@", 1)[-1] if "@" in parts.netloc else parts.netloc
        query = "" if self.strip_query else self._params(parts.query)
        fragment = "" if self.strip_query else parts.fragment
        if re.search(r"(^|[&?])[\w.-]+=", fragment or ""):
            fragment = "" if self.strip_query else self._params(fragment.lstrip("?"))
        return urlunsplit((parts.scheme, netloc, parts.path, query, fragment))


def domain(url):
    host = Privacy.host(url)
    return host[4:] if host.startswith("www.") else host
