"""Normalized Granola meetings to Throughline messages.

Both readers (cache.py, api.py) produce the same meeting dict; this module
never reads the clock or the network, so backfill and sync map identically.
"""

import hashlib
import json

from throughline_sdk import provenance

from .text import moment

MAPPING_REVISION = 1
MEETINGS = "granola.meetings"
TRANSCRIPT = "granola.transcript"
MY_NOTES = "granola.my_notes"
SUMMARIES = "granola.summaries"
CHANNEL_ORDER = (MEETINGS, MY_NOTES, TRANSCRIPT, SUMMARIES)
AGENT = {"system": "granola", "kind": "agent", "value": "granola-ai", "display_name": "Granola AI"}
PROVENANCE_CHUNK = 1000


def fingerprint(value):
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def email_author(email, name=None):
    if not email or "@" not in email:
        return None
    return {"system": "email", "kind": "email", "value": email, "display_name": name}


class Mapper:
    def __init__(self, owner_email=None, owner_name=None, access_label=None):
        self.owner_email = (owner_email or "").strip() or None
        self.owner_name = owner_name
        self.access_label = (access_label or "").strip() or None

    # -- who -------------------------------------------------------------
    def creator(self, meeting):
        """The note-taker: the note's creator, else the attendee marked self,
        else the configured owner."""
        creator = meeting.get("creator") or {}
        if creator.get("email"):
            return creator["email"], creator.get("name")
        for row in meeting["attendees"]:
            if row.get("self"):
                return row["email"], row.get("name")
        return self.owner_email, self.owner_name

    def is_own(self, meeting):
        email, _ = self.creator(meeting)
        if not self.owner_email or not email:
            return True  # a personal key / your own cache: every note is yours
        return email.lower() == self.owner_email.lower()

    def owner_author(self, meeting):
        email, name = self.creator(meeting)
        author = email_author(email, name or (self.owner_name if self.is_own(meeting) else None))
        return author or {"system": "granola", "kind": "owner", "value": "owner",
                          "display_name": self.owner_name}

    def others(self, meeting):
        email, _ = self.creator(meeting)
        mine = {(email or "").lower(), (self.owner_email or "").lower()}
        return [r for r in meeting["attendees"] if r["email"].lower() not in mine and not r.get("self")]

    def speaker(self, meeting, utterance):
        """(author, speaker_label, speaker_name, inferred) of one utterance."""
        if utterance["attribution"] == "me":
            author = self.owner_author(meeting)
            return author, None, author.get("display_name"), False
        others = self.others(meeting)
        name = utterance.get("speaker_name")
        if utterance["attribution"] == "them":
            if name:
                match = [r for r in others if (r.get("name") or "").lower() == name.lower()]
                if len(match) == 1:
                    return email_author(match[0]["email"], match[0].get("name")), None, name, False
            elif len(others) == 1:
                # A one-to-one meeting: the other side is the one other attendee.
                other = others[0]
                return email_author(other["email"], other.get("name")), None, other.get("name"), True
            label = utterance.get("diarization_label") or name or "Others"
        else:
            label = utterance.get("diarization_label") or name or "Unknown speaker"
        author = {"system": "granola", "kind": "speaker_label",
                  "value": f"{meeting['id']}:{label}", "display_name": label}
        return author, label, name, False

    # -- when ------------------------------------------------------------
    @staticmethod
    def span(meeting):
        calendar = meeting.get("calendar") or {}
        utterances = meeting["utterances"]
        start = (utterances[0]["start"] if utterances else None) or calendar.get("start") \
            or meeting.get("created_at")
        if calendar.get("start") and utterances:
            # A recording that starts long before the scheduled time is an
            # ad hoc meeting reusing the event; trust the recording.
            start = min(calendar["start"], utterances[0]["start"], key=moment)
        if not start:
            start = meeting.get("updated_at")
        ends = [u["end"] or u["start"] for u in utterances]
        end = max(ends, key=moment) if ends else None
        if calendar.get("end") and (not end or moment(calendar["end"]) > moment(end)):
            end = calendar["end"]
        if end and moment(end) < moment(start):
            end = None
        return start, end

    # -- messages --------------------------------------------------------
    def base(self, meeting, channel, external_id, happened_at, content, fields, payload):
        message = {
            "channel": channel,
            "external_id": external_id,
            "thread": {"external_id": meeting["id"], "title": meeting.get("title") or "Untitled meeting"},
            "happened_at": moment(happened_at).isoformat(),
            "content": content,
            "fields": {"meeting_id": meeting["id"], "source_mode": meeting["mode"], **fields},
            "payload": payload,
        }
        if self.access_label:
            message["access_label"] = self.access_label
        return message

    def messages(self, meeting):
        """Messages and provenance records of one meeting, in delivery order.
        Returns [] for a meeting with no usable time."""
        start, end = self.span(meeting)
        if not start or not moment(start):
            return []
        own = self.is_own(meeting)
        creator_email, _ = self.creator(meeting)
        calendar = meeting.get("calendar") or {}
        attendees = meeting["attendees"]
        title = meeting.get("title") or calendar.get("title") or "Untitled meeting"
        out = []

        lines = [title]
        names = [r.get("name") or r["email"] for r in attendees]
        if names:
            lines.append("Attendees: " + ", ".join(names))
        if meeting["folders"]:
            lines.append("Folders: " + ", ".join(meeting["folders"]))
        record = self.base(
            meeting, MEETINGS, meeting["id"], start, "\n".join(lines),
            {
                "meeting_title": title,
                "calendar_event_id": calendar.get("event_id"),
                "calendar_title": calendar.get("title"),
                "scheduled_start": calendar.get("start"),
                "scheduled_end": calendar.get("end"),
                "organizer_email": calendar.get("organizer"),
                "note_owner_email": creator_email,
                "is_own_note": own,
                "attendee_emails": [r["email"] for r in attendees],
                "attendees": attendees,
                "attendee_count": len(attendees),
                "folders": meeting["folders"],
                "transcript_status": meeting["transcript_status"],
                "utterance_count": len(meeting["utterances"]),
                "web_url": meeting.get("web_url"),
                "note_created_at": meeting.get("created_at"),
                "note_updated_at": meeting.get("updated_at"),
            },
            meeting["raw"],
        )
        if end:
            record["ended_at"] = moment(end).isoformat()
        organizer = next((r for r in attendees if r.get("organizer")), None)
        record["author"] = (email_author(organizer["email"], organizer.get("name")) if organizer
                            else self.owner_author(meeting))
        out.append(record)

        sources = []
        typed = meeting.get("typed_notes")
        if typed:
            notes = self.base(
                meeting, MY_NOTES, f"{meeting['id']}:notes", start, typed["text"],
                {
                    "meeting_title": title,
                    "author_kind": "owner" if own else "other",
                    "note_format": typed["format"],
                    "note_updated_at": meeting.get("updated_at"),
                },
                {"format": typed["format"], "text": typed["text"]},
            )
            notes["author"] = self.owner_author(meeting)
            out.append(notes)
            sources.append({"channel": MY_NOTES, "external_id": notes["external_id"]})

        first = moment(meeting["utterances"][0]["start"]) if meeting["utterances"] else None
        for utterance in meeting["utterances"]:
            author, label, name, inferred = self.speaker(meeting, utterance)
            begin = moment(utterance["start"])
            finish = moment(utterance["end"]) if utterance["end"] else None
            if finish and finish < begin:
                finish = None
            attribution = utterance["attribution"] or "unknown"
            message = self.base(
                meeting, TRANSCRIPT, f"{meeting['id']}:u:{utterance['id']}", utterance["start"],
                utterance["text"],
                {
                    "speaker_label": label,
                    "speaker_name": name,
                    "speaker_source": utterance["source"],
                    "speaker_attribution": attribution,
                    "speaker_inferred": inferred,
                    "author_kind": "owner" if attribution == "me" and own else "other",
                    "start_s": round((begin - first).total_seconds(), 3),
                    "end_s": round((finish - first).total_seconds(), 3) if finish else None,
                    "segment_id": utterance["id"],
                },
                utterance["raw"],
            )
            if finish:
                message["ended_at"] = finish.isoformat()
            message["author"] = author
            out.append(message)
            sources.append({"channel": TRANSCRIPT, "external_id": message["external_id"]})

        for summary in meeting["summaries"]:
            at = summary.get("created_at") or end or start
            if moment(at) < moment(start):
                at = end or start
            message = self.base(
                meeting, SUMMARIES, f"{meeting['id']}:panel:{summary['id']}", at, summary["text"],
                {
                    "meeting_title": title,
                    "author_kind": "agent",
                    "panel_id": summary["id"],
                    "panel_title": summary["title"],
                    "template": summary.get("template"),
                    "source_count": len(sources),
                },
                summary["raw"],
            )
            message["author"] = dict(AGENT)
            out.append(message)
            derived = {"channel": SUMMARIES, "external_id": message["external_id"]}
            for offset in range(0, len(sources), PROVENANCE_CHUNK):
                out.append(provenance(derived, sources[offset:offset + PROVENANCE_CHUNK]))
        return out
