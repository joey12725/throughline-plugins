"""Invented Granola data: a legacy cache-v3 file and recorded API responses.

Every person, address and meeting here is made up (example.com). Run
`python granola_fixtures.py` to rewrite tests/recorded/ from these builders.
"""

import copy
import json
from pathlib import Path

RECORDED = Path(__file__).parent / "recorded"
OWNER = {"name": "Invented Robin Owner", "email": "robin@example.com"}
AVERY = {"name": "Invented Avery Example", "email": "avery@example.com"}
BLAKE = {"name": "Invented Blake Sample", "email": "blake@example.com"}
CASEY = {"name": "Invented Casey Placeholder", "email": "casey@example.com"}
DOC_A = "0b6c2d1e-aaaa-4c3e-9f00-000000000001"
DOC_B = "0b6c2d1e-bbbb-4c3e-9f00-000000000002"
DOC_C = "0b6c2d1e-cccc-4c3e-9f00-000000000003"
DOC_D = "0b6c2d1e-dddd-4c3e-9f00-000000000004"
DOC_E = "0b6c2d1e-eeee-4c3e-9f00-000000000005"
DOC_F = "0b6c2d1e-ffff-4c3e-9f00-000000000006"
FOLDER = "list-invented-0001"


def event(event_id, title, start, end, attendees, organizer):
    return {
        "id": event_id,
        "summary": title,
        "start": {"dateTime": start, "timeZone": "America/Winnipeg"},
        "end": {"dateTime": end, "timeZone": "America/Winnipeg"},
        "attendees": [
            {"email": p["email"], "displayName": p["name"], **({"self": True} if p is OWNER else {})}
            for p in attendees
        ],
        "organizer": {"email": organizer["email"]},
        "htmlLink": "https://calendar.example.com/event/" + event_id,
    }


def segments(doc_id, start_minute, lines, hour="15", day="04"):
    out = []
    for index, (source, text) in enumerate(lines):
        second = index * 7
        out.append({
            "id": f"seg-{doc_id[9:13]}-{index:03d}",
            "document_id": doc_id,
            "start_timestamp": f"2025-03-{day}T{hour}:{start_minute:02d}:{second:02d}.000Z",
            "end_timestamp": f"2025-03-{day}T{hour}:{start_minute:02d}:{second + 6:02d}.000Z",
            "text": text,
            "source": source,
            "is_final": True,
        })
    return out


def pm(*paragraphs):
    return {"type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": p}]} for p in paragraphs
    ]}


def cache_state():
    documents = {
        DOC_A: {
            "id": DOC_A,
            "title": "Invented one-to-one: orchard budget",
            "created_at": "2025-03-04T14:58:00.000Z",
            "updated_at": "2025-03-04T15:40:00.000Z",
            "deleted_at": None,
            "type": "meeting",
            "notes_markdown": "- Ask Avery about the invented orchard budget\n- Follow up Friday",
            "notes_plain": "Ask Avery about the invented orchard budget\nFollow up Friday",
            "people": {"creator": OWNER, "attendees": [AVERY]},
            "google_calendar_event": event(
                "evt-invented-a", "Orchard budget", "2025-03-04T09:00:00-06:00",
                "2025-03-04T09:30:00-06:00", [OWNER, AVERY], OWNER),
        },
        DOC_B: {
            "id": DOC_B,
            "title": "Invented weekly sync",
            "created_at": "2025-03-05T16:00:00.000Z",
            "updated_at": "2025-03-05T17:00:00.000Z",
            "deleted_at": None,
            "type": "meeting",
            "notes": pm("Typed in the editor only"),
            "people": {"creator": OWNER, "attendees": [
                {"email": BLAKE["email"], "details": {"person": {"name": {"fullName": BLAKE["name"]}}}},
                CASEY,
            ]},
            "google_calendar_event": event(
                "evt-invented-b", "Weekly sync", "2025-03-05T10:00:00-06:00",
                "2025-03-05T11:00:00-06:00", [OWNER, AVERY, BLAKE, CASEY], BLAKE),
        },
        DOC_C: {
            "id": DOC_C,
            "title": "Invented deleted note",
            "created_at": "2025-03-06T16:00:00.000Z",
            "updated_at": "2025-03-06T16:10:00.000Z",
            "deleted_at": "2025-03-06T16:20:00.000Z",
            "notes_plain": "never delivered",
        },
        DOC_D: {
            "id": DOC_D,
            "title": None,
            "created_at": "2025-03-07T12:00:00.000Z",
            "updated_at": "2025-03-07T12:05:00.000Z",
            "notes_plain": "Invented thoughts typed without a meeting",
        },
        DOC_E: {
            "id": DOC_E,
            "title": "Invented vendor call",
            "created_at": "2025-03-08T18:00:00.000Z",
            "updated_at": "2025-03-08T18:45:00.000Z",
            "people": {"creator": OWNER, "attendees": [CASEY]},
        },
        DOC_F: {
            "id": DOC_F,
            "title": "Invented hallway chat",
            "created_at": "2025-03-09T20:00:00.000Z",
            "updated_at": "2025-03-09T20:20:00.000Z",
        },
    }
    a = segments(DOC_A, 0, [
        ("microphone", "Morning Avery, shall we look at the invented orchard budget?"),
        ("system", "Sure, the invented numbers came in yesterday."),
        ("microphone", "Great, what changed since last month?"),
        ("system", "Irrigation went up by an invented amount."),
        ("microphone", "Let's follow up on Friday then."),
    ])
    b = segments(DOC_B, 1, [
        ("system", "Welcome everyone to the invented weekly sync."),
        ("microphone", "Thanks Blake, I have two invented updates."),
        ("system", "Casey here, the invented launch is on track."),
        ("system", "Avery, any blockers on the invented report?"),
    ], hour="16", day="05")
    e = segments(DOC_E, 2, [
        ("microphone", "Hello, this is the invented vendor call."),
        ("system", "Hi, thanks for the invented time."),
        ("system", "partial hypo"),
        ("microphone", ""),
    ], hour="18", day="08")
    e[2]["is_final"] = False
    f = segments(DOC_F, 3, [
        ("microphone", "Quick invented hallway question."),
        ("system", "Invented answer: yes."),
    ], hour="20", day="09")
    for seg in f:
        del seg["id"]
    panels = {
        DOC_A: {
            "panel-a-1": {
                "id": "panel-a-1",
                "title": "Summary",
                "template_slug": "meeting-summary",
                "created_at": "2025-03-04T15:35:00.000Z",
                "original_content": "<h3>Invented budget</h3><ul><li>Irrigation up</li>"
                                    "<li>Follow up Friday</li></ul>",
                "content": pm("Invented budget", "Irrigation up"),
            },
        },
        DOC_B: {
            "panel-b-1": {
                "id": "panel-b-1",
                "title": "Action items",
                "created_at": "2025-03-05T17:01:00.000Z",
                "content": pm("Invented launch on track", "Avery to share the invented report"),
            },
            "panel-b-2": {"id": "panel-b-2", "title": "Gone", "deleted_at": "2025-03-05T17:02:00Z",
                          "original_content": "<p>deleted</p>"},
        },
        DOC_E: {
            "panel-e-1": {"id": "panel-e-1", "title": "Links", "original_content": "<hr><a>x</a>"},
            "panel-e-2": {"id": "panel-e-2", "title": "Summary", "created_at": "2025-03-08T18:40:00Z",
                          "original_content": "<p>Invented vendor agreed to the invented terms.</p>"},
        },
    }
    return {
        "documents": documents,
        "transcripts": {DOC_A: a, DOC_B: b, DOC_E: e, DOC_F: f},
        "documentPanels": panels,
        "meetingsMetadata": {DOC_E: {"attendees": [{"email": CASEY["email"], "name": CASEY["name"]}]}},
        "documentLists": {FOLDER: [DOC_B]},
        "documentListsMetadata": {FOLDER: {"title": "Invented Team"}},
        "people": [],
    }


def cache_file(state=None):
    return {"cache": json.dumps({"state": state or cache_state(), "version": 3})}


# ---------------------------------------------------------------- API ----

NOTE_1 = "not_Invented00001a"
NOTE_2 = "not_Invented00002b"
NOTE_3 = "not_Invented00003c"
NOTE_4 = "not_Invented00004d"


def summary(note_id, updated, deleted=None, title="Invented"):
    return {"id": note_id, "object": "note", "title": title, "owner": OWNER,
            "created_at": updated, "updated_at": updated, "deleted_at": deleted}


def item(text, start, end, source, attribution=None, name=None, label=None):
    speaker = {"source": source}
    if attribution:
        speaker["attribution"] = attribution
    if name:
        speaker["name"] = name
    if label:
        speaker["diarization_label"] = label
    return {"speaker": speaker, "text": text, "start_time": start, "end_time": end}


def note(note_id, title, updated, **extra):
    base = {
        "id": note_id, "object": "note", "title": title, "owner": OWNER,
        "created_at": updated, "updated_at": updated, "deleted_at": None,
        "web_url": "https://notes.example.com/d/" + note_id,
        "calendar_event": None, "attachments": [], "attendees": [],
        "folder_membership": [], "space_membership": [],
        "summary_text": "", "summary_markdown": None,
        "private_notes_text": None, "private_notes_markdown": None, "transcript": None,
    }
    base.update(extra)
    return base


def api_notes():
    one = note(
        NOTE_1, "Invented design review", "2025-04-01T16:00:00Z",
        attendees=[OWNER, AVERY, BLAKE],
        calendar_event={
            "event_title": "Design review", "invitees": [{"email": e["email"]} for e in (OWNER, AVERY, BLAKE)],
            "organiser": AVERY["email"], "calendar_event_id": "evt-invented-api-1",
            "scheduled_start_time": "2025-04-01T15:00:00Z", "scheduled_end_time": "2025-04-01T15:45:00Z",
        },
        folder_membership=[{"id": "fol_Invented000001", "object": "folder", "name": "Invented Design",
                            "parent_folder_id": None}],
        attachments=[{"id": "att_Invented000001", "object": "attachment", "type": "photo",
                      "url": "https://files.example.com/secret-ish", "width": 10, "height": 10}],
        summary_text="Invented summary", summary_markdown="## Invented summary\n- Ship the invented mockups",
        private_notes_text="mockups", private_notes_markdown="- invented mockups look good",
        transcript=[
            item("Let's review the invented mockups.", "2025-04-01T15:01:00Z", "2025-04-01T15:01:05Z",
                 "microphone", "me"),
            item("I like the invented header.", "2025-04-01T15:01:06Z", "2025-04-01T15:01:09Z",
                 "speaker", "them", name=AVERY["name"]),
            item("Footer needs invented work.", "2025-04-01T15:01:10Z", "2025-04-01T15:01:14Z",
                 "speaker", "them"),
        ],
    )
    two = note(
        NOTE_2, "Invented phone memo", "2025-04-02T09:00:00Z",
        summary_text="Invented memo summary", summary_markdown=None,
    )
    two_transcript = [
        item("Invented memo first line.", "2025-04-02T08:00:00Z", "2025-04-02T08:00:04Z", "microphone",
             label="Speaker A"),
        item("Invented reply from the other voice.", "2025-04-02T08:00:05Z", "2025-04-02T08:00:09Z",
             "microphone", label="Speaker B"),
        item("Invented memo closing line.", "2025-04-02T08:00:10Z", "2025-04-02T08:00:14Z", "microphone",
             label="Speaker A"),
    ]
    three = note(
        NOTE_3, "Invented transcript-less note", "2025-04-03T12:00:00Z",
        attendees=[OWNER, CASEY],
        summary_text="Invented summary without transcript access",
        summary_markdown="Invented summary without transcript access",
    )
    return one, two, two_transcript, three


def api_recording():
    one, two, two_transcript, three = api_notes()
    two_bare = copy.deepcopy(two)
    return [
        {"method": "GET", "path": "/v1/notes", "params": {"cursor": "page-2"},
         "json": {"notes": [summary(NOTE_3, three["updated_at"]),
                            summary(NOTE_4, "2025-04-04T12:00:00Z", deleted="2025-04-04T13:00:00Z")],
                  "hasMore": False, "cursor": None}},
        {"method": "GET", "path": "/v1/notes",
         "json": {"notes": [summary(NOTE_2, two["updated_at"]), summary(NOTE_1, one["updated_at"])],
                  "hasMore": True, "cursor": "page-2"}},
        {"method": "GET", "path": f"/v1/notes/{NOTE_1}", "params": {"include": "transcript"}, "json": one},
        {"method": "GET", "path": f"/v1/notes/{NOTE_2}", "params": {"include": "transcript"},
         "status": 413, "json": {"error": "TRANSCRIPT_TOO_LARGE"}},
        {"method": "GET", "path": f"/v1/notes/{NOTE_2}", "json": two_bare},
        {"method": "GET", "path": f"/v1/notes/{NOTE_2}/transcript", "params": {"cursor": "t-2"},
         "json": {"transcript": two_transcript[2:], "hasMore": False, "cursor": None}},
        {"method": "GET", "path": f"/v1/notes/{NOTE_2}/transcript",
         "json": {"transcript": two_transcript[:2], "hasMore": True, "cursor": "t-2"}},
        {"method": "GET", "path": f"/v1/notes/{NOTE_3}", "params": {"include": "transcript"}, "json": three},
    ]


def write():
    RECORDED.mkdir(exist_ok=True)
    (RECORDED / "cache-v3.json").write_text(json.dumps(cache_file(), indent=1) + "\n", encoding="utf-8")
    (RECORDED / "api.json").write_text(json.dumps(api_recording(), indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    write()
