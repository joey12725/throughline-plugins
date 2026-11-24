"""Best-effort secret scrubbing for transcript text.

Coding-agent transcripts routinely contain credentials: a key pasted into a
prompt, a token echoed by a command, a private key read from disk. Every
string this plugin sends to Throughline passes through `scrub_value` first
(unless CODEX_SCRUB=false). Each match is replaced by
`[REDACTED:<kind>]`; the surrounding text is kept.

This is pattern matching, not a guarantee: it catches the common token
shapes below and `name=value` assignments whose name says secret, and will
miss credentials with no recognisable shape.
"""

import re

_VALUE_STOP = r"[^\s\"'`,;)\]}>]"

PATTERNS = [
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
            re.S,
        ),
        None,
    ),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}"), None),
    ("openai_key", re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}"), None),
    ("stripe_key", re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}"), None),
    (
        "github_token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"),
        None,
    ),
    ("gitlab_token", re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}"), None),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), None),
    ("slack_token", re.compile(r"\bxox[abprse]-[A-Za-z0-9-]{10,}"), None),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}"), None),
    ("huggingface_token", re.compile(r"\bhf_[A-Za-z0-9]{30,}"), None),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
        None,
    ),
    # Keep the scheme and prefix, drop the credential.
    ("bearer", re.compile(r"(?i)(\b(?:bearer|token)\s+)([A-Za-z0-9._~+/=\-]{16,})"), 2),
    ("url_password", re.compile(r"(\b[a-z][a-z0-9+.\-]*://[^\s:/@]+:)([^\s@/]+)(?=@)"), 2),
    (
        "assignment",
        re.compile(
            r"(?i)(\b[A-Z0-9_.\-]*(?:api[_\-]?key|secret|token|passwd|password|credential)s?"
            r"[A-Z0-9_\-]*\"?\s*[:=]\s*[\"']?)(?![$<{(\[])(" + _VALUE_STOP + r"{8,})"
        ),
        2,
    ),
]


def scrub_text(text):
    """(scrubbed text, number of replacements)."""
    if not text or not isinstance(text, str):
        return text, 0
    total = [0]
    for kind, pattern, group in PATTERNS:
        marker = f"[REDACTED:{kind}]"

        def replace(match, group=group, marker=marker):
            if group is None:
                total[0] += 1
                return marker
            if match.group(group).startswith("[REDACTED"):
                return match.group(0)
            total[0] += 1
            base, whole = match.start(0), match.group(0)
            start, end = match.span(group)
            return whole[: start - base] + marker + whole[end - base :]

        text = pattern.sub(replace, text)
    return text, total[0]


def scrub_value(value):
    """Scrub every string inside a JSON-like value. Returns (value, count)."""
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        out, total = {}, 0
        for key, item in value.items():
            out[key], count = scrub_value(item)
            total += count
        return out, total
    if isinstance(value, (list, tuple)):
        out, total = [], 0
        for item in value:
            item, count = scrub_value(item)
            out.append(item)
            total += count
        return out, total
    return value, 0
