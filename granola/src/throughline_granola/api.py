"""Granola's official public API (https://docs.granola.ai, OpenAPI 1.7.0).

    GET /v1/notes?updated_after&cursor&page_size     NoteSummary pages
    GET /v1/notes/{id}?include=transcript            Note (413 when the transcript
                                                     is too large to inline)
    GET /v1/notes/{id}/transcript?cursor&page_size   transcript pages

Bearer API key (``grn_...``), Business or Enterprise plan, 5 requests/second
sustained. Only notes that already have an AI summary and transcript are
listed. A recorded-response file (GRANOLA_FIXTURE) replaces the network for
tests.
"""

import json
import time
from pathlib import Path

import httpx

from .text import clean, iso, moment

BASE_URL = "https://public-api.granola.ai"


class ApiError(RuntimeError):
    """The Granola API could not be read."""


def fixture_transport(path):
    """An httpx.MockTransport serving recorded responses.

    The file is a JSON list of {method, path, params?, status?, json | text,
    headers?}; the first entry whose method and path match and whose params
    are a subset of the request's query wins. The host is ignored."""
    entries = json.loads(Path(path).read_text(encoding="utf-8"))

    def handle(request):
        query = dict(request.url.params)
        for entry in entries:
            if entry.get("method", "GET").upper() != request.method:
                continue
            if entry["path"] != request.url.path:
                continue
            params = {k: str(v) for k, v in (entry.get("params") or {}).items()}
            if any(query.get(k) != v for k, v in params.items()):
                continue
            headers = entry.get("headers") or {}
            if "json" in entry:
                return httpx.Response(entry.get("status", 200), json=entry["json"], headers=headers)
            return httpx.Response(entry.get("status", 200), text=entry.get("text", ""), headers=headers)
        return httpx.Response(404, json={"error": "no recorded response"})

    return httpx.MockTransport(handle)


class Api:
    def __init__(self, key, transport=None, base_url=BASE_URL, pause_s=0.25, retries=4, sleep=time.sleep):
        self.http = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
            transport=transport,
            timeout=60,
        )
        self.pause_s = pause_s
        self.retries = retries
        self.sleep = sleep
        self.requests = 0

    def get(self, path, params=None, allow=()):
        for attempt in range(self.retries + 1):
            if self.requests and self.pause_s:
                self.sleep(self.pause_s)
            self.requests += 1
            try:
                response = self.http.get(path, params=params)
            except httpx.HTTPError as error:
                if attempt == self.retries:
                    raise ApiError(f"Granola API {path}: {type(error).__name__}") from None
                self.sleep(min(30, 2**attempt))
                continue
            if response.status_code in allow:
                return response
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == self.retries:
                    raise ApiError(f"Granola API {path}: HTTP {response.status_code}")
                wait = response.headers.get("Retry-After")
                try:
                    wait = float(wait)
                except (TypeError, ValueError):
                    wait = 2**attempt
                self.sleep(min(60, max(wait, 0)))
                continue
            if response.status_code == 401:
                raise ApiError("Granola API rejected the API key (HTTP 401)")
            if response.status_code >= 400:
                raise ApiError(f"Granola API {path}: HTTP {response.status_code}")
            return response
        raise ApiError(f"Granola API {path}: retries exhausted")

    def list_notes(self, updated_after=None, page_size=30):
        cursor = None
        while True:
            params = {"page_size": page_size}
            if updated_after:
                params["updated_after"] = updated_after
            if cursor:
                params["cursor"] = cursor
            body = self.get("/v1/notes", params).json()
            for note in body.get("notes") or []:
                if isinstance(note, dict) and note.get("id"):
                    yield note
            cursor = body.get("cursor")
            if not body.get("hasMore") or not cursor:
                return

    def note(self, note_id):
        """The full note with its transcript (paged when too large to inline).
        transcript is None when a workspace admin turned transcript access off."""
        response = self.get(f"/v1/notes/{note_id}", {"include": "transcript"}, allow=(413, 404))
        if response.status_code == 404:
            return None
        if response.status_code == 413:
            note = self.get(f"/v1/notes/{note_id}").json()
            note["transcript"] = self.transcript(note_id)
            return note
        return response.json()

    def transcript(self, note_id):
        items, cursor = [], None
        while True:
            params = {"page_size": 100}
            if cursor:
                params["cursor"] = cursor
            response = self.get(f"/v1/notes/{note_id}/transcript", params, allow=(403,))
            if response.status_code == 403:
                return None
            body = response.json()
            items.extend(i for i in body.get("transcript") or [] if isinstance(i, dict))
            cursor = body.get("cursor")
            if not body.get("hasMore") or not cursor:
                return items


def as_dict(value):
    return value if isinstance(value, dict) else {}


def utterances_of(items):
    out = []
    for index, item in enumerate(items or []):
        text = clean(item.get("text"))
        start = iso(item.get("start_time"))
        if not text or not start:
            continue
        speaker = as_dict(item.get("speaker"))
        source = clean(speaker.get("source")) or "unknown"
        attribution = clean(speaker.get("attribution"))
        if attribution not in ("me", "them"):
            # Desktop "speaker" audio is the other side; phone and watch
            # recordings are all "microphone" and stay unknown.
            attribution = "them" if source == "speaker" else None
        out.append({
            "id": f"{index:05d}",
            "text": text,
            "start": start,
            "end": iso(item.get("end_time")),
            "source": source,
            "attribution": attribution,
            "speaker_name": clean(speaker.get("name")),
            "diarization_label": clean(speaker.get("diarization_label")),
            "raw": item,
        })
    # Stable: the API order is the spoken order; ties keep it.
    out.sort(key=lambda u: moment(u["start"]))
    return out


def meeting_of(note):
    """A normalized meeting from a full API note."""
    owner = as_dict(note.get("owner"))
    event = note.get("calendar_event")
    calendar = None
    if isinstance(event, dict):
        calendar = {
            "event_id": clean(event.get("calendar_event_id")),
            "title": clean(event.get("event_title")),
            "start": iso(event.get("scheduled_start_time")),
            "end": iso(event.get("scheduled_end_time")),
            "organizer": clean(event.get("organiser")),
            "link": None,
        }
    attendees = {}
    for entry in (note.get("attendees") or []) + ((event or {}).get("invitees") or []):
        if isinstance(entry, dict) and clean(entry.get("email")):
            row = attendees.setdefault(
                entry["email"].lower(),
                {"email": entry["email"].strip(), "name": None, "organizer": False, "self": False},
            )
            row["name"] = row["name"] or clean(entry.get("name"))
    if calendar and calendar["organizer"] and calendar["organizer"].lower() in attendees:
        attendees[calendar["organizer"].lower()]["organizer"] = True
    owner_email = clean(owner.get("email"))
    if owner_email and owner_email.lower() in attendees:
        attendees[owner_email.lower()]["self"] = True
    notes = clean(note.get("private_notes_markdown"))
    typed = {"text": notes, "format": "markdown"} if notes else None
    if not typed and clean(note.get("private_notes_text")):
        typed = {"text": clean(note["private_notes_text"]), "format": "plain"}
    summaries = []
    summary = clean(note.get("summary_markdown")) or clean(note.get("summary_text"))
    if summary:
        summaries.append({
            "id": "summary",
            "title": "Summary",
            "text": summary,
            "created_at": iso(note.get("updated_at")),
            "template": None,
            "raw": {k: note.get(k) for k in ("id", "summary_markdown", "summary_text", "updated_at")},
        })
    transcript = note.get("transcript")
    drop = ("transcript", "summary_markdown", "summary_text", "private_notes_markdown",
            "private_notes_text", "attachments")
    raw = {k: v for k, v in note.items() if k not in drop}
    # Attachment URLs are bearer links: keep only their ids and kinds.
    raw["attachments"] = [
        {k: a.get(k) for k in ("id", "type", "captured_at", "description") if k in a}
        for a in note.get("attachments") or [] if isinstance(a, dict)
    ]
    return {
        "id": note["id"],
        "mode": "api",
        "title": clean(note.get("title")),
        "created_at": iso(note.get("created_at")),
        "updated_at": iso(note.get("updated_at")),
        "creator": {"email": owner_email, "name": clean(owner.get("name"))} if owner else None,
        "calendar": calendar,
        "attendees": sorted(attendees.values(), key=lambda r: r["email"].lower()),
        "folders": sorted(
            clean(f.get("name")) for f in note.get("folder_membership") or []
            if isinstance(f, dict) and clean(f.get("name"))
        ),
        "web_url": clean(note.get("web_url")),
        "typed_notes": typed,
        "utterances": utterances_of(transcript),
        "summaries": summaries,
        "transcript_status": "disabled" if transcript is None else ("ok" if transcript else "missing"),
        "raw": raw,
    }
