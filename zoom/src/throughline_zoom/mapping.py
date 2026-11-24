"""Zoom recording objects to Throughline messages (pure functions)."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from throughline_sdk import Record

MEETINGS = "zoom.meetings"
TRANSCRIPTS = "zoom.transcripts"
UNKNOWN = "Unknown speaker"
# Keys never kept in raw payloads: meeting and recording passcodes, the
# short-lived download token, and the account id (a declared secret).
SENSITIVE = {
    "password",
    "recording_play_passcode",
    "download_access_token",
    "access_token",
    "account_id",
}


def scrub(value):
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items() if k not in SENSITIVE}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


def parse_time(value):
    when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if when.tzinfo is None:
        raise ValueError("naive Zoom timestamp")
    return when


def zone(name):
    try:
        return ZoneInfo(name) if name else timezone.utc
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def local(when, tz):
    return when.astimezone(tz).isoformat()


def transcript_files(meeting):
    files = [
        f
        for f in meeting.get("recording_files") or []
        if str(f.get("file_type", "")).upper() == "TRANSCRIPT"
    ]
    return sorted(files, key=lambda f: (f.get("recording_start") or "", f.get("id") or ""))


class People:
    """Who a speaker label is: the owner (the account behind /users/me,
    which hosts every listed recording), and an email when the participant
    report names exactly one address for that display name."""

    def __init__(self, me=None, extra_names=(), participants=(), host_id=None):
        me = me or {}
        self.owner_email = (me.get("email") or "").strip().lower() or None
        names = {me.get("display_name"), " ".join(
            filter(None, [me.get("first_name"), me.get("last_name")])
        )} | set(extra_names)
        emails = {}
        for entry in participants or ():
            name = (entry.get("name") or "").strip()
            if not name:
                continue
            emails.setdefault(name, set()).add((entry.get("user_email") or "").strip().lower())
            if host_id and entry.get("id") == host_id:
                names.add(name)
        self.owner_names = {n.strip().casefold() for n in names if n and n.strip()}
        self.emails = {
            name: next(iter(found))
            for name, found in emails.items()
            if len(found) == 1 and next(iter(found)) and "@" in next(iter(found))
        }
        self.known = bool(self.owner_email or self.owner_names)

    def email(self, label):
        return self.emails.get(label)

    def is_owner(self, label):
        if not self.known:
            return None
        email = self.email(label)
        if email and self.owner_email:
            return email == self.owner_email
        return label.casefold() in self.owner_names

    def author(self, uuid, label):
        email = self.email(label)
        if self.is_owner(label) and self.owner_email:
            email = self.owner_email
        if email:
            return {"system": "email", "kind": "email", "value": email, "display_name": label}
        return {
            "system": "zoom",
            "kind": "speaker_label",
            "value": f"{uuid}:{label}",
            "display_name": label,
        }


def participant_rows(participants):
    """Distinct participants (rejoins collapse) in join order."""
    seen, out = set(), []
    for entry in participants or ():
        key = ((entry.get("name") or "").strip(), (entry.get("user_email") or "").lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": key[0] or None, "email": key[1] or None})
    return out


def meeting_message(meeting, me, turn_list, status, participants, participants_status, label=None):
    uuid = meeting["uuid"]
    tz = zone(meeting.get("timezone"))
    start = parse_time(meeting["start_time"])
    duration = meeting.get("duration")
    topic = (meeting.get("topic") or "").strip() or "Untitled Zoom meeting"
    rows = participant_rows(participants) if participants is not None else None
    speakers = []
    for turn in turn_list:
        if turn["speaker"] not in speakers:
            speakers.append(turn["speaker"])
    lines = [topic, f"Zoom meeting, {duration} min" if duration is not None else "Zoom meeting"]
    if rows:
        lines.append("Participants: " + ", ".join(r["name"] or r["email"] or "?" for r in rows))
    if speakers:
        lines.append("Speakers: " + ", ".join(speakers))
    lines.append(f"Transcript: {status}")
    host_email = (me or {}).get("email")
    if host_email:
        author = {
            "system": "email",
            "kind": "email",
            "value": host_email.strip().lower(),
            "display_name": (me or {}).get("display_name"),
        }
    else:
        author = {
            "system": "zoom",
            "kind": "user_id",
            "value": str(meeting.get("host_id") or "unknown-host"),
            "display_name": None,
        }
    message = dict(
        channel=MEETINGS,
        external_id=uuid,
        thread={"external_id": uuid, "title": topic},
        happened_at=local(start, tz),
        ended_at=local(start + timedelta(minutes=int(duration)), tz)
        if isinstance(duration, int) and duration >= 0
        else None,
        author=author,
        content="\n".join(lines),
        fields={
            "meeting_id": str(meeting["id"]) if meeting.get("id") is not None else None,
            "topic": topic,
            "duration_min": duration if isinstance(duration, int) else None,
            "timezone": meeting.get("timezone"),
            "host_id": meeting.get("host_id"),
            "host_email": host_email,
            "recording_count": meeting.get("recording_count"),
            "transcript_status": status,
            "turn_count": len(turn_list),
            "speakers": speakers,
            "participant_count": len(rows) if rows is not None else None,
            "participants": rows,
            "participants_status": participants_status,
        },
        payload=scrub({"meeting": meeting, "participants": participants}),
    )
    if label:
        message["access_label"] = label
    return message


def turn_messages(meeting, people, turn_list, label=None):
    """turn_list items: {speaker, text, start_s, end_s, cue_count, file}."""
    uuid = meeting["uuid"]
    tz = zone(meeting.get("timezone"))
    topic = (meeting.get("topic") or "").strip() or "Untitled Zoom meeting"
    out = []
    for number, turn in enumerate(turn_list, 1):
        base = parse_time(turn["file"].get("recording_start") or meeting["start_time"])
        speaker = turn["speaker"]
        owner = people.is_owner(speaker) if speaker != UNKNOWN else False
        message = dict(
            channel=TRANSCRIPTS,
            external_id=f"{uuid}:{number}",
            thread={"external_id": uuid, "title": topic},
            happened_at=local(base + timedelta(seconds=turn["start_s"]), tz),
            ended_at=local(base + timedelta(seconds=turn["end_s"]), tz)
            if turn["end_s"] >= turn["start_s"]
            else None,
            author=people.author(uuid, speaker),
            content=turn["text"],
            fields={
                "speaker_label": speaker,
                "start_s": turn["start_s"],
                "end_s": turn["end_s"],
                "cue_count": turn["cue_count"],
                "is_owner": owner,
                "author_kind": "owner" if owner else "other",
                "meeting_id": str(meeting["id"]) if meeting.get("id") is not None else None,
            },
            payload=scrub(
                {
                    "meeting_uuid": uuid,
                    "recording_file_id": turn["file"].get("id"),
                    "recording_start": turn["file"].get("recording_start"),
                    "turn": {k: v for k, v in turn.items() if k != "file"},
                }
            ),
        )
        if label:
            message["access_label"] = label
        out.append(message)
    return out


def part_of(turn, uuid):
    return Record(
        kind="link",
        data={
            "type": "part_of",
            "method": "source",
            "from": {"channel": TRANSCRIPTS, "external_id": turn["external_id"]},
            "to": {"channel": MEETINGS, "external_id": uuid},
        },
    )
