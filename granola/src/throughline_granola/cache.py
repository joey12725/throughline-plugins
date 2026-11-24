"""Read the Granola desktop app's local cache (legacy, plaintext versions only).

The cache is undocumented. The shape assumed here is the one community tools
read from ``cache-v3.json`` (2024 to early 2026):

    {"cache": "<JSON string>"}            the outer file; the value may also be
                                          an object in some builds
    inner = {"state": {...}, "version": N}
    state.documents        {doc_id: document}
    state.transcripts      {doc_id: [segment, ...]}
    state.documentPanels   {doc_id: {panel_id: panel}}
    state.meetingsMetadata {doc_id: {...}}            optional
    state.documentLists    {list_id: [doc_id, ...]}   folders, optional
    state.documentListsMetadata {list_id: {title}}    optional

document: id, title, created_at, updated_at, deleted_at, type, notes (ProseMirror),
notes_markdown, notes_plain, people {creator {name, email}, attendees [...]},
google_calendar_event {id, summary, start {dateTime}, end {dateTime},
attendees [{email, displayName, self, organizer}], organizer {email}, htmlLink}.
segment: id, document_id, text, source (microphone | system), start_timestamp,
end_timestamp, is_final. panel: id, title, content (ProseMirror),
original_content (HTML), created_at, updated_at, template_slug, deleted_at.

Every key is optional here: a missing key yields less data, never a crash.
The file is opened read-only and never written. Newer Granola releases
encrypt the cache (``cache-v6.json.enc``); that cannot be read, use the API.
"""

import glob
import json
import os
import re
from pathlib import Path

from .text import clean, html_text, iso, moment, prosemirror_text

DEFAULT_DIR = "~/Library/Application Support/Granola"


class CacheError(RuntimeError):
    """The cache file is missing, encrypted or not in a known shape."""


def find_cache(path=None):
    """The cache file to read: an explicit path (file or directory), else the
    newest plaintext ``cache-v*.json`` in the default macOS location."""
    target = Path(os.path.expanduser(path or DEFAULT_DIR))
    if target.is_file():
        return target
    if not target.is_dir():
        raise CacheError(f"Granola cache not found at {target}")
    found = []
    for name in glob.glob(str(target / "cache-v*.json")):
        match = re.search(r"cache-v(\d+)\.json$", name)
        if match:
            found.append((int(match.group(1)), name))
    if not found:
        encrypted = glob.glob(str(target / "cache-v*.json.enc"))
        if encrypted:
            raise CacheError(
                "this Granola version encrypts its local cache "
                f"({Path(encrypted[0]).name}); use GRANOLA_SOURCE=api with an API key"
            )
        raise CacheError(f"no cache-v*.json file in {target}")
    return Path(max(found)[1])


def load_state(path):
    """The cache's state object (documents, transcripts, panels, ...)."""
    try:
        with open(path, "rb") as handle:  # read-only; the app rewrites the file
            raw = handle.read()
    except OSError as error:
        raise CacheError(f"cannot read the Granola cache: {error}") from None
    if raw[:1] not in (b"{", b"[", b" ", b"\n", b"\r", b"\t"):
        raise CacheError("the Granola cache is not JSON (encrypted?)")
    try:
        outer = json.loads(raw)
    except ValueError as error:
        # The app may be mid-write; the next run reads the finished file.
        raise CacheError(f"the Granola cache is not valid JSON: {error}") from None
    inner = outer.get("cache", outer) if isinstance(outer, dict) else None
    if isinstance(inner, str):
        try:
            inner = json.loads(inner)
        except ValueError as error:
            raise CacheError(f"the cache's inner JSON is invalid: {error}") from None
    if not isinstance(inner, dict):
        raise CacheError("the Granola cache has no cache object")
    state = inner.get("state", inner)
    if not isinstance(state, dict) or not isinstance(state.get("documents"), dict):
        keys = sorted(state)[:20] if isinstance(state, dict) else []
        raise CacheError(
            "unrecognised Granola cache shape (no state.documents); "
            f"version {inner.get('version')!r}, keys {keys}"
        )
    return state


def as_dict(value):
    return value if isinstance(value, dict) else {}


def as_list(value):
    return value if isinstance(value, list) else []


def calendar_of(doc):
    event = as_dict(doc.get("google_calendar_event"))
    if not event:
        return None

    def when(side):
        side = event.get(side)
        if isinstance(side, dict):
            return iso(side.get("dateTime") or side.get("date"))
        return iso(side)

    organizer = event.get("organizer")
    organizer = organizer.get("email") if isinstance(organizer, dict) else organizer
    return {
        "event_id": clean(event.get("id")) or clean(event.get("iCalUID")),
        "title": clean(event.get("summary")),
        "start": when("start"),
        "end": when("end"),
        "organizer": clean(organizer),
        "link": clean(event.get("htmlLink")),
    }


def person_name(entry):
    details = as_dict(as_dict(entry.get("details")).get("person"))
    full = as_dict(details.get("name")).get("fullName")
    return clean(entry.get("name")) or clean(entry.get("displayName")) or clean(full)


def attendees_of(doc, metadata):
    merged = {}

    def add(entry, organizer=False, me=False):
        if not isinstance(entry, dict):
            return
        email = clean(entry.get("email"))
        if not email or "@" not in email:
            return
        key = email.lower()
        row = merged.setdefault(key, {"email": email, "name": None, "organizer": False, "self": False})
        row["name"] = row["name"] or person_name(entry)
        row["organizer"] = row["organizer"] or organizer or bool(entry.get("organizer"))
        row["self"] = row["self"] or me or bool(entry.get("self"))

    people = as_dict(doc.get("people"))
    for entry in as_list(people.get("attendees")):
        add(entry)
    event = as_dict(doc.get("google_calendar_event"))
    for entry in as_list(event.get("attendees")):
        add(entry)
    for entry in as_list(as_dict(metadata).get("attendees")):
        add(entry)
    organizer = event.get("organizer")
    if isinstance(organizer, dict) and organizer.get("email"):
        add(organizer, organizer=True)
    return sorted(merged.values(), key=lambda r: r["email"].lower())


def typed_notes_of(doc):
    markdown = clean(doc.get("notes_markdown"))
    if markdown:
        return {"text": markdown, "format": "markdown"}
    plain = clean(doc.get("notes_plain"))
    if plain:
        return {"text": plain, "format": "plain"}
    notes = doc.get("notes")
    if isinstance(notes, dict):
        text = prosemirror_text(notes)
        if text:
            return {"text": text, "format": "plain"}
    return None


def segments_of(doc_id, segments):
    out = []
    for index, segment in enumerate(as_list(segments)):
        if not isinstance(segment, dict):
            continue
        if segment.get("is_final") is False:
            continue  # an interim hypothesis the app later replaces
        text = clean(segment.get("text"))
        start = iso(segment.get("start_timestamp") or segment.get("start_time"))
        if not text or not start:
            continue
        source = clean(segment.get("source")) or "unknown"
        attribution = {"microphone": "me", "system": "them"}.get(source)
        out.append({
            "id": clean(segment.get("id")) or f"{index:05d}",
            "text": text,
            "start": start,
            "end": iso(segment.get("end_timestamp") or segment.get("end_time")),
            "source": source,
            "attribution": attribution,
            "speaker_name": clean(segment.get("speaker")) if isinstance(segment.get("speaker"), str) else None,
            "diarization_label": None,
            "raw": segment,
        })
    out.sort(key=lambda u: (moment(u["start"]), u["id"]))
    return out


def panels_of(panels):
    out = []
    for panel_id, panel in sorted(as_dict(panels).items()):
        if not isinstance(panel, dict) or panel.get("deleted_at"):
            continue
        text = ""
        html = panel.get("original_content")
        if isinstance(html, str) and html.strip() and not html.strip().startswith("<hr"):
            text = html_text(html)
        if not text and isinstance(panel.get("content"), dict):
            text = prosemirror_text(panel["content"])
        if not text and isinstance(panel.get("content"), str):
            text = html_text(panel["content"]) or panel["content"].strip()
        if not text:
            continue
        out.append({
            "id": clean(panel.get("id")) or str(panel_id),
            "title": clean(panel.get("title")) or "Summary",
            "text": text,
            "created_at": iso(panel.get("created_at")) or iso(panel.get("updated_at")),
            "template": clean(panel.get("template_slug")),
            "raw": {k: v for k, v in panel.items() if k != "content"},
        })
    return out


def folders_by_document(state):
    lists = as_dict(state.get("documentLists"))
    meta = as_dict(state.get("documentListsMetadata"))
    out = {}
    for list_id, members in lists.items():
        title = clean(as_dict(meta.get(list_id)).get("title")) or str(list_id)
        if isinstance(members, dict):
            members = list(members)
        for doc_id in as_list(members):
            if isinstance(doc_id, dict):
                doc_id = doc_id.get("id")
            if isinstance(doc_id, str):
                out.setdefault(doc_id, set()).add(title)
    return {k: sorted(v) for k, v in out.items()}


PAYLOAD_DROP = ("notes", "notes_markdown", "notes_plain", "last_viewed_panel", "chapters")


def meetings(state):
    """Normalized meetings of a cache state, skipping deleted documents."""
    transcripts = as_dict(state.get("transcripts"))
    panels = as_dict(state.get("documentPanels"))
    metadata = as_dict(state.get("meetingsMetadata"))
    folders = folders_by_document(state)
    out = []
    for doc_id, doc in as_dict(state.get("documents")).items():
        if not isinstance(doc, dict) or doc.get("deleted_at"):
            continue
        doc_id = clean(doc.get("id")) or str(doc_id)
        creator = as_dict(as_dict(doc.get("people")).get("creator"))
        segments = transcripts.get(doc_id)
        if isinstance(segments, dict):  # some builds wrap segments
            segments = segments.get("segments") or segments.get("transcript")
        out.append({
            "id": doc_id,
            "mode": "cache",
            "title": clean(doc.get("title")),
            "created_at": iso(doc.get("created_at")),
            "updated_at": iso(doc.get("updated_at")),
            "creator": {"email": clean(creator.get("email")), "name": person_name(creator)}
            if creator else None,
            "calendar": calendar_of(doc),
            "attendees": attendees_of(doc, metadata.get(doc_id)),
            "folders": folders.get(doc_id, []),
            "web_url": None,
            "typed_notes": typed_notes_of(doc),
            "utterances": segments_of(doc_id, segments),
            "summaries": panels_of(panels.get(doc_id)),
            "transcript_status": "ok" if segments else "missing",
            "raw": {k: v for k, v in doc.items() if k not in PAYLOAD_DROP},
        })
    return out
