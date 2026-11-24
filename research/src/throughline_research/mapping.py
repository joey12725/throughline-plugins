"""Deterministic mapping from raw research records to Throughline messages.

Pure functions only: the same raw record always maps to the same messages,
links and provenance, so reruns and replays create no new versions. Raise
MAPPING_REVISION whenever the mapping output changes; every message carries it
so stored records show which mapping produced them.
"""

import hashlib
import json
import re
from datetime import UTC, date, datetime, time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

MAPPING_REVISION = 2
RESULTS = "research.results"
ITEMS = "research.items"
CONTENT_BUDGET = 200_000
TRUNCATED = "\n\n[Truncated for indexing; the payload holds the full text]"
EMPTY_MARKERS = (
    "no verifiable events found",
    "no verifiable results found",
    "no results found",
    "nothing found",
)
URL = re.compile(r"https?://[^\s<>\"'\])}]+", re.I)
MARKDOWN_LINK = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)\)", re.I)
HEADING = re.compile(r"^\s{0,3}#{2,4}\s+(.*\S)\s*$")
LABELED = re.compile(
    r"^\s*(?:[-*+]\s+|\d+[.)]\s+)?\**_?([A-Za-z][A-Za-z0-9 /&'()-]{0,60}?)_?\**\s*:\s*\**\s*(.*?)\s*$"
)
NULLS = {
    "",
    "n/a",
    "na",
    "none",
    "unknown",
    "-",
    "--",
    "tbd",
    "tba",
    "null",
    "not listed",
    "not stated",
}
TRACKING = re.compile(r"^(utm_.*|fbclid|gclid|mc_cid|mc_eid|ref|ref_src)$", re.I)

# Labeled line -> item field. Labels are compared after lowercasing and
# collapsing punctuation to single spaces.
LABELS = {
    "event": "title",
    "title": "title",
    "name": "title",
    "event name": "title",
    "occurrence start": "occurrence_start",
    "start": "occurrence_start",
    "starts": "occurrence_start",
    "start time": "occurrence_start",
    "date": "occurrence_start",
    "when": "occurrence_start",
    "occurrence end": "occurrence_end",
    "end": "occurrence_end",
    "ends": "occurrence_end",
    "end time": "occurrence_end",
    "what the event is": "description",
    "description": "description",
    "summary": "description",
    "what": "description",
    "venue": "venue",
    "location": "venue",
    "where": "venue",
    "venue details": "address",
    "address": "address",
    "city": "city",
    "hosting organization": "organizer",
    "host": "organizer",
    "organizer": "organizer",
    "organiser": "organizer",
    "door set time": "door_time",
    "door time": "door_time",
    "doors": "door_time",
    "canonical url": "canonical_url",
    "url": "canonical_url",
    "link": "canonical_url",
    "event url": "canonical_url",
    "evidence url": "evidence_url",
    "source url": "evidence_url",
    "source": "evidence_url",
    "ticket rsvp signup url": "ticket_url",
    "ticket url": "ticket_url",
    "tickets": "ticket_url",
    "rsvp": "ticket_url",
    "price": "price",
    "cost": "price",
    "audience participation details": "audience",
    "audience": "audience",
    "source tier": "source_tier",
    "primary confirmed": "primary_confirmed",
    "date certainty": "date_certainty",
    "notes": "notes",
    "note": "notes",
}
TEXT_FIELDS = (
    "title",
    "description",
    "venue",
    "address",
    "city",
    "organizer",
    "door_time",
    "price",
    "audience",
    "source_tier",
    "date_certainty",
    "notes",
)
URL_FIELDS = ("canonical_url", "evidence_url", "ticket_url")
BOOLEAN_FIELDS = ("primary_confirmed",)


def digest(value, length=16):
    if not isinstance(value, str):
        value = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(value.encode()).hexdigest()[:length]


def tz_of(name):
    if not name or str(name).upper() == "UTC":
        return UTC
    try:
        return ZoneInfo(name)
    except Exception:
        return UTC


def fit(text):
    if len(text) <= CONTENT_BUDGET:
        return text
    return text[: CONTENT_BUDGET - len(TRUNCATED)] + TRUNCATED


def label_key(label):
    return re.sub(r"[^a-z0-9]+", " ", label.lower()).strip()


def clean(value):
    if value is None:
        return None
    if not isinstance(value, str):
        value = (
            json.dumps(value, ensure_ascii=False)
            if isinstance(value, (dict, list))
            else str(value)
        )
    value = re.sub(r"\*\*|__", "", value).strip().strip("`").strip()
    return None if value.lower().rstrip(".") in NULLS else value


def canonical_url(value):
    """Normalized URL for keys: lowercase scheme/host, no fragment or tracking."""
    if not value:
        return None
    value = str(value).strip().strip("<>").rstrip(".,;")
    parts = urlsplit(value)
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return None
    query = urlencode(
        [(k, v) for k, v in parse_qsl(parts.query) if not TRACKING.match(k)]
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


def first_url(value):
    if not value:
        return None
    value = str(value)
    link = MARKDOWN_LINK.search(value)
    if link:
        return link.group(2).rstrip(".,;")
    found = URL.search(value)
    return found.group(0).rstrip(".,;") if found else None


def boolean(value):
    value = (clean(value) or "").lower()
    if value.startswith(("yes", "true", "confirmed", "y ")) or value in ("y",):
        return True
    if value.startswith(("no", "false", "unconfirmed", "n ")) or value in ("n",):
        return False
    return None


def when(value, tz):
    """(ISO timestamp with offset, precision) for a date or date-time string."""
    value = clean(value)
    if not value:
        return None, None
    text = value.strip()
    match = re.match(
        r"(\d{4}-\d{2}-\d{2})(?:[T ](\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?)\s*(Z|[+-]\d{2}:?\d{2})?)?",
        text,
    )
    if not match:
        return None, None
    day_text, clock, offset = match.groups()
    try:
        day = date.fromisoformat(day_text)
        if not clock:
            return datetime.combine(day, time(0), tz).isoformat(), "day"
        stamp = f"{day_text}T{clock}"
        if offset:
            offset = "+00:00" if offset == "Z" else offset
            if ":" not in offset:
                offset = offset[:3] + ":" + offset[3:]
            return datetime.fromisoformat(stamp + offset).isoformat(), "exact"
        return datetime.fromisoformat(stamp).replace(tzinfo=tz).isoformat(), "exact"
    except ValueError:
        return None, None


def instant(value):
    """An ISO timestamp with offset (naive input is read as UTC)."""
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.isoformat()


# ------------------------------------------------------------ extraction


def message_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    return ""


def citation(entry):
    if isinstance(entry, str):
        return {"url": entry, "title": None}
    if not isinstance(entry, dict):
        return None
    inner = (
        entry.get("url_citation")
        if isinstance(entry.get("url_citation"), dict)
        else entry
    )
    url = inner.get("url") or inner.get("uri") or inner.get("link") or inner.get("href")
    if not url:
        return None
    return {"url": url, "title": inner.get("title") or inner.get("name")}


def from_chat(response):
    """(text, citations, usage) from a chat completion or a responses object."""
    text, cites = "", []
    if isinstance(response.get("choices"), list) and response["choices"]:
        message = response["choices"][0].get("message") or {}
        text = message_text(message.get("content"))
        cites += [citation(a) for a in message.get("annotations") or []]
    elif isinstance(response.get("output"), list) or "output_text" in response:
        parts = []
        for item in response.get("output") or []:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for piece in item.get("content") or []:
                if isinstance(piece, dict) and piece.get("type") in (
                    "output_text",
                    "text",
                ):
                    parts.append(piece.get("text") or "")
                    cites += [citation(a) for a in piece.get("annotations") or []]
        text = "\n".join(parts) or message_text(response.get("output_text"))
    cites += [citation(c) for c in response.get("citations") or []]
    cites += [citation(c) for c in response.get("search_results") or []]
    usage = response.get("usage") or {}
    return (
        text,
        [c for c in cites if c],
        {
            "prompt_tokens": usage.get("prompt_tokens", usage.get("input_tokens")),
            "completion_tokens": usage.get(
                "completion_tokens", usage.get("output_tokens")
            ),
        },
    )


def from_import(imported, fields):
    document = imported.get("json") or {}
    text = imported.get("text")
    cites, usage = [], {}
    if isinstance(document.get("choices"), list) or isinstance(
        document.get("output"), list
    ):
        inner_text, cites, usage = from_chat(document)
        text = text or inner_text
    if not text:
        names = fields.get("text") or [
            "text",
            "markdown",
            "content",
            "answer",
            "output_text",
            "findings",
            "result",
            "response",
        ]
        for name in [names] if isinstance(names, str) else names:
            value = document.get(name)
            if isinstance(value, dict) and (
                value.get("choices") or value.get("output")
            ):
                text, cites, usage = from_chat(value)
                break
            if isinstance(value, str) and value.strip():
                text = value
                break
    names = fields.get("citations") or [
        "citations",
        "annotations",
        "urls",
        "sources",
        "references",
    ]
    for name in [names] if isinstance(names, str) else names:
        value = document.get(name)
        if isinstance(value, list):
            cites += [citation(c) for c in value]
    return text or "", [c for c in cites if c], usage


def merge_citations(cites, text):
    """Ordered unique citations: provider annotations first, then URLs in text."""
    merged, seen = [], set()
    candidates = list(cites)
    candidates += [
        {"url": m.group(2), "title": m.group(1) or None}
        for m in MARKDOWN_LINK.finditer(text)
    ]
    candidates += [{"url": m.group(0), "title": None} for m in URL.finditer(text)]
    for entry in candidates:
        url = str(entry["url"]).strip().rstrip(".,;")
        key = canonical_url(url)
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append({"url": url, "title": clean(entry.get("title"))})
    return merged


def is_empty(text):
    stripped = text.strip().lower()
    return not stripped or any(stripped.startswith(m) for m in EMPTY_MARKERS)


# ------------------------------------------------------------------ items


def labeled_sections(text):
    """### sections whose lines are 'Label: value' pairs, as label->value dicts."""
    sections, current = [], None
    for line in text.splitlines():
        heading = HEADING.match(line)
        if heading:
            current = {"__heading": heading.group(1).strip("*# ")}
            sections.append(current)
            continue
        if current is None:
            continue
        match = LABELED.match(line)
        if match and match.group(2):
            current.setdefault(match.group(1).strip(), match.group(2))
    return [s for s in sections if len(s) >= 3]


def json_document(text):
    stripped = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", stripped, re.S)
    if fence:
        stripped = fence.group(1).strip()
    for opener in ("{", "["):
        start = stripped.find(opener)
        if start == -1:
            continue
        try:
            return json.loads(stripped[start:])
        except ValueError:
            try:
                return json.JSONDecoder().raw_decode(stripped[start:])[0]
            except ValueError:
                continue
    return None


def json_items(text):
    document = json_document(text)
    summary = None
    if isinstance(document, dict):
        summary = document.get("summary") or document.get("findings")
        for key in ("items", "events", "results", "occurrences"):
            if isinstance(document.get(key), list):
                document = document[key]
                break
        else:
            document = []
    entries = [e for e in document or [] if isinstance(e, dict)]
    return entries, summary if isinstance(summary, str) else None


def item_fields(entry, tz):
    """Normalized item fields from labeled text or a JSON object."""
    fields, details = {}, {}
    for label, value in entry.items():
        if label == "__heading":
            continue
        key = LABELS.get(label_key(label)) or LABELS.get(label)
        if key is None:
            details[re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")] = value
        elif key not in fields:
            fields[key] = value
    if not fields.get("title") and entry.get("__heading"):
        fields["title"] = entry["__heading"]
    out = {}
    for key in TEXT_FIELDS:
        out[key] = clean(fields.get(key))
    for key in URL_FIELDS:
        out[key] = first_url(fields.get(key))
    for key in BOOLEAN_FIELDS:
        value = fields.get(key)
        out[key] = value if isinstance(value, bool) else boolean(value)
    start, start_precision = when(fields.get("occurrence_start"), tz)
    end, end_precision = when(fields.get("occurrence_end"), tz)
    if start and end and end < start:
        end = end_precision = None
    out.update(
        occurrence_start=start,
        occurrence_end=end,
        start_precision=start_precision,
        end_precision=end_precision,
    )
    for key in ("occurrence_start", "occurrence_end"):
        if fields.get(key) is not None and out[key] is None:
            details[key + "_text"] = clean(fields[key])
    out["details"] = {k: v for k, v in details.items() if v not in (None, "")} or None
    return out


def item_key(fields):
    url = canonical_url(fields.get("canonical_url")) or canonical_url(
        fields.get("ticket_url")
    )
    day = (fields.get("occurrence_start") or "")[:16]
    if url:
        return digest(f"{url}|{day}")
    title = re.sub(r"\W+", " ", (fields.get("title") or "").lower()).strip()
    venue = re.sub(r"\W+", " ", (fields.get("venue") or "").lower()).strip()
    return digest(f"{title}|{day}|{venue}")


def extract(raw):
    """Text, citations, usage, status and parsed items of one raw record."""
    if raw.get("response"):
        text, cites, usage = from_chat(raw["response"])
    elif raw.get("imported"):
        text, cites, usage = from_import(
            raw["imported"], (raw.get("provider") or {}).get("fields") or {}
        )
    else:
        text, cites, usage = "", [], {}
    text = text.strip()
    search = raw.get("search") or {}
    tz = tz_of((raw.get("stream") or {}).get("timezone"))
    output = search.get("output") or "markdown"
    entries, summary = [], None
    if output == "json" or (
        output != "markdown" and text.lstrip().startswith(("{", "[", "```"))
    ):
        entries, summary = json_items(text)
    if not entries and output in ("labeled_markdown", "json"):
        entries = labeled_sections(text)
    status = "empty" if is_empty(text) else "ok"
    items = [] if status == "empty" else [item_fields(e, tz) for e in entries]
    items = [i for i in items if i["title"] or i["canonical_url"]]
    return {
        "text": text,
        "summary": summary,
        "citations": merge_citations(cites, text),
        "usage": usage,
        "status": status,
        "items": items,
    }


# --------------------------------------------------------------- messages


def subject_days(start_day, end_day, tz):
    start = datetime.combine(date.fromisoformat(start_day), time(0), tz).isoformat()
    end = datetime.combine(date.fromisoformat(end_day), time(0), tz).isoformat()
    return {
        "start": start,
        "end": end,
        "start_precision": "day",
        "end_precision": "day",
    }


def display_time(value, precision):
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if precision == "day":
        return parsed.strftime("%a %Y-%m-%d")
    return parsed.strftime("%a %Y-%m-%d %H:%M")


def result_records(raw):
    """The result message, its links, then each item message with its links
    and provenance, in a fixed order. Returns a list of (kind, data).

    The output depends only on the raw record, never on how it was read (live
    or from an export), so switching routes creates no new versions."""
    stream = raw.get("stream") or {}
    search = raw.get("search") or {}
    provider = raw.get("provider") or {}
    tz = tz_of(stream.get("timezone"))
    found = extract(raw)
    rid = raw["id"]
    stream_title = stream.get("title") or stream.get("id") or "Research"
    label = search.get("label") or search.get("id")
    heading = f"{stream_title} / {label} ({raw['run_date']})"
    body = found["text"] or "No findings returned."
    if search.get("output") == "json" and (found["summary"] or found["items"]):
        lines = [found["summary"]] if found["summary"] else []
        for item in found["items"]:
            moment = display_time(item["occurrence_start"], item["start_precision"])
            parts = [
                item["title"] or item["canonical_url"],
                moment,
                item["venue"],
                item["canonical_url"],
            ]
            lines.append("- " + " | ".join(p for p in parts if p))
        body = "\n".join(lines)
    urls = [c["url"] for c in found["citations"]]
    happened = instant(raw["completed_at"])
    span = raw.get("window") or {}
    message = {
        "channel": RESULTS,
        "external_id": rid,
        "thread": {
            "external_id": f"{stream.get('id')}/{search.get('id')}",
            "title": f"{stream_title}: {label}",
        },
        "happened_at": happened,
        "ended_at": None,
        "content": fit(f"{heading}\n\n{body}"),
        "fields": {
            "stream": stream.get("id"),
            "stream_title": stream_title,
            "search_id": search.get("id"),
            "search_label": label,
            "cluster": search.get("cluster"),
            "variant": search.get("variant"),
            "search_kind": search.get("kind"),
            "run_date": raw["run_date"],
            "window_start": span.get("start"),
            "window_end": span.get("end"),
            "provider": provider.get("type"),
            "model": provider.get("model"),
            "import_path": provider.get("path"),
            "prompt": raw.get("prompt"),
            "prompt_hash": raw.get("prompt_hash"),
            "status": found["status"],
            "cited_urls": urls,
            "citations": found["citations"],
            "citation_count": len(urls),
            "item_count": len(found["items"]),
            "prompt_tokens": found["usage"].get("prompt_tokens"),
            "completion_tokens": found["usage"].get("completion_tokens"),
            "stream_revision": stream.get("revision"),
            "mapping_revision": MAPPING_REVISION,
        },
        "payload": raw,
    }
    if span.get("start") and span.get("end"):
        message["subject"] = subject_days(span["start"], span["end"], tz)
    out = [("message", message)]
    reference = {"channel": RESULTS, "external_id": rid}
    for url in urls:
        out.append(
            (
                "link",
                {
                    "type": "cites",
                    "from": reference,
                    "to": {"kind": "web_page", "external_ref": url},
                    "method": "source",
                },
            )
        )
    used = {}
    for index, item in enumerate(found["items"]):
        key = item_key(item)
        used[key] = used.get(key, 0) + 1
        iid = f"{rid}#{key}" + (f"-{used[key]}" if used[key] > 1 else "")
        item_type = search.get("item_type") or "item"
        title = item["title"] or item["canonical_url"]
        lines = [title]
        moment = display_time(item["occurrence_start"], item["start_precision"])
        place = ", ".join(p for p in (item["venue"], item["city"]) if p)
        if moment or place:
            lines.append(" at ".join(p for p in (moment, place) if p))
        for extra in (
            item["description"],
            item["price"],
            item["canonical_url"] or item["evidence_url"],
        ):
            if extra:
                lines.append(extra)
        fields = {
            "stream": stream.get("id"),
            "search_id": search.get("id"),
            "result_id": rid,
            "run_date": raw["run_date"],
            "item_type": item_type,
            "item_key": key,
            "position": index + 1,
            **{k: item[k] for k in TEXT_FIELDS},
            **{k: item[k] for k in URL_FIELDS},
            "primary_confirmed": item["primary_confirmed"],
            "occurrence_start": item["occurrence_start"],
            "occurrence_end": item["occurrence_end"],
            "date_precision": item["start_precision"],
            "details": item["details"],
            "mapping_revision": MAPPING_REVISION,
        }
        record = {
            "channel": ITEMS,
            "external_id": iid,
            "thread": {
                "external_id": f"items:{stream.get('id')}/{search.get('id')}",
                "title": f"{stream_title}: {label} (items)",
            },
            "happened_at": happened,
            "ended_at": None,
            "content": fit("\n".join(lines)),
            "fields": fields,
            "payload": {
                "result_id": rid,
                "position": index + 1,
                "item": item,
                "mapping_revision": MAPPING_REVISION,
            },
        }
        if item["occurrence_start"]:
            record["subject"] = {
                "start": item["occurrence_start"],
                "start_precision": item["start_precision"],
            }
            if item["occurrence_end"]:
                record["subject"]["end"] = item["occurrence_end"]
                record["subject"]["end_precision"] = item["end_precision"]
        out.append(("message", record))
        own = {"channel": ITEMS, "external_id": iid}
        out.append(
            (
                "link",
                {"type": "found_in", "from": own, "to": reference, "method": "source"},
            )
        )
        out.append(
            (
                "link",
                {
                    "type": "about",
                    "from": own,
                    "to": {
                        "kind": item_type,
                        "external_ref": f"research:{item_type}:{key}",
                        "name": title,
                    },
                    "method": "source",
                },
            )
        )
        out.append(("provenance", {"derived": own, "sources": [reference]}))
    return out
