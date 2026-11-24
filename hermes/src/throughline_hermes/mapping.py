"""Raw Hermes records to Throughline messages: deterministic and side-effect free.

The same raw record always maps to the same message: nothing here reads the
clock, the run id or the fetch time, and every list is ordered by the source.
"""

import hashlib
import json
from datetime import UTC, datetime

from .fetch import call_ids, epoch

MAPPING_REVISION = 1
MESSAGES = "hermes.messages"
AUTOMATION = "hermes.automation"
SESSIONS = "hermes.sessions"
# Characters of a tool result shown in the message content; the bounded full
# text is in the tool_output field.
PREVIEW_CHARS = 500
ARGS_CHARS = 160
CALL_ARGUMENT_CHARS = 2000
# Chat types whose user turns may come from several people.
MULTI_USER_CHATS = frozenset({"group", "supergroup", "channel", "thread", "forum"})
# Most tool calls of one row listed in its content and fields.
MAX_LISTED_CALLS = 50
# Characters of message content and of the tool_output field.
CONTENT_LIMIT = 100_000


def moment(value):
    """ISO time with offset (UTC) from a stored epoch; None when unreadable."""
    seconds = epoch(value)
    if seconds is None:
        return None
    return datetime.fromtimestamp(seconds, UTC).isoformat()


class Settings:
    """The mapping-relevant settings, parsed once."""

    def __init__(
        self,
        *,
        agent_name="hermes",
        agent_display_name=None,
        owner_sources=(),
        owner_user_ids=(),
        owner_identifier="hermes:owner:owner",
        owner_name=None,
        automation_sources=("cron",),
        preview_chars=PREVIEW_CHARS,
    ):
        self.agent_name = agent_name.strip().lower() or "hermes"
        self.agent_display_name = agent_display_name or self.agent_name.capitalize()
        self.owner_sources = {s.lower() for s in owner_sources}
        self.owner_user_ids = set(owner_user_ids)
        parts = owner_identifier.split(":", 2)
        if len(parts) != 3 or not all(p.strip() for p in parts):
            raise ValueError("HERMES_OWNER_IDENTIFIER must be system:kind:value")
        self.owner = dict(system=parts[0], kind=parts[1], value=parts[2])
        self.owner_name = owner_name
        self.automation_sources = {s.lower() for s in automation_sources}
        if preview_chars < 50:
            raise ValueError("HERMES_PREVIEW_CHARS must be at least 50")
        self.preview_chars = preview_chars

    def fingerprint(self):
        """Changes when a setting changes what an existing record maps to."""
        text = json.dumps(
            [
                self.agent_name,
                self.agent_display_name,
                sorted(self.owner_sources),
                sorted(self.owner_user_ids),
                self.owner,
                self.owner_name,
                sorted(self.automation_sources),
                self.preview_chars,
            ],
            sort_keys=True,
        )
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    def agent(self):
        return {
            "system": "hermes",
            "kind": "agent",
            "value": self.agent_name,
            "display_name": self.agent_display_name,
        }


def source_of(session):
    return str((session or {}).get("source") or "unknown").strip().lower() or "unknown"


def origin_of(session):
    origin = (session or {}).get("origin_json")
    return origin if isinstance(origin, dict) else {}


def is_automated(session_id, session, settings):
    return source_of(session) in settings.automation_sources or str(
        session_id
    ).startswith("cron_")


def is_owner_session(session, settings):
    """The owner's own chat: an owner surface, or a direct chat with an owner user id.

    A group, channel or thread chat is never the owner's own chat even when the
    owner started it: its user turns may come from anyone, so they keep the
    platform user id the session names.
    """
    if source_of(session) in settings.owner_sources:
        return True
    user = (session or {}).get("user_id")
    if user is None:
        return False
    if str((session or {}).get("chat_type") or "dm").lower() in MULTI_USER_CHATS:
        return False
    user = str(user)
    return user in settings.owner_user_ids or (
        f"{source_of(session)}:{user}" in settings.owner_user_ids
    )


def session_title(session_id, session):
    title = (session or {}).get("title") or (session or {}).get("display_name")
    if title:
        return str(title)[:300]
    started = moment((session or {}).get("started_at"))
    day = started[:10] if started else "undated"
    return f"Hermes {source_of(session)} session {day}"


def thread_for(session_id, session):
    return {
        "external_id": str(session_id),
        "title": session_title(session_id, session),
        "metadata": {"kind": "hermes_session", "source": source_of(session)},
    }


def message_external_id(session_id, message_id, uid=None):
    """`<session>:<message_uid>`; `<session>:<row id, zero padded>` for rows without one.

    Hermes keeps a row's message_uid when it rewrites a transcript (compaction,
    retry, undo delete and re-insert rows under new ids), so the uid names the
    turn across rewrites; the padded id of legacy rows sorts in source order.
    """
    if uid:
        return f"{session_id}:{uid}"
    return f"{session_id}:{int(message_id):010d}"


def capped(text, limit=CONTENT_LIMIT):
    return text if len(text) <= limit else text[:limit]


def text_of(content):
    """Readable text of stored content: a string or multimodal parts."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        content = [content]
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                kind = str(part.get("type") or "part")
                if isinstance(part.get("text"), str):
                    parts.append(part["text"])
                elif kind in ("image_url", "image", "input_image"):
                    parts.append("[image]")
                elif kind in ("input_audio", "audio"):
                    parts.append("[audio]")
                else:
                    parts.append(f"[{kind}]")
        return "\n".join(p for p in parts if p)
    return json.dumps(content, ensure_ascii=False, sort_keys=True)


def short(text, limit):
    text = " ".join(str(text).replace("\x00", "\\u0000").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def short_args(arguments):
    """`key=value, ...` of a tool call's arguments, one line, bounded."""
    value = arguments
    if isinstance(arguments, str):
        try:
            value = json.loads(arguments)
        except ValueError:
            return short(arguments, ARGS_CHARS)
    if isinstance(value, dict):
        pairs = []
        for key, item in value.items():
            shown = (
                item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
            )
            pairs.append(f"{key}={short(shown, 60)}")
        return short(", ".join(pairs), ARGS_CHARS)
    if value is None:
        return ""
    return short(json.dumps(value, ensure_ascii=False), ARGS_CHARS)


def calls_of(tool_calls):
    """[(id, name, arguments)] of an assistant row's tool calls, in order."""
    calls = []
    for call in tool_calls if isinstance(tool_calls, list) else []:
        if not isinstance(call, dict):
            continue
        function = (
            call.get("function") if isinstance(call.get("function"), dict) else {}
        )
        name = function.get("name") or call.get("name") or "unknown"
        arguments = function.get("arguments", call.get("arguments"))
        calls.append((str(call.get("id") or ""), str(name), arguments))
    return calls


def visibility(row):
    if row.get("active", 1):
        return "active"
    return "compacted" if row.get("compacted") else "rewound"


def as_int(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def as_float(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def as_text(value):
    return None if value is None else str(value)


def author_for(role, session, settings):
    if role in ("assistant", "tool"):
        return settings.agent()
    if role != "user":
        return None
    if is_owner_session(session, settings):
        return dict(settings.owner, display_name=settings.owner_name)
    user = (session or {}).get("user_id")
    if user is None or not str(user).strip():
        return None
    name = origin_of(session).get("user_name")
    return {
        "system": source_of(session),
        "kind": "user_id",
        "value": str(user),
        "display_name": str(name) if name else None,
    }


def map_message(record, session, settings):
    """(message, reply_to link or None) for one raw message record."""
    row = record["payload"]
    sid = record["session_id"]
    role = str(row.get("role") or "unknown")
    automated = is_automated(sid, session, settings)
    channel = AUTOMATION if automated else MESSAGES
    happened = moment(row.get("timestamp"))
    if happened is None:
        return None, None
    text = capped(text_of(row.get("content")).strip())
    every_call = calls_of(row.get("tool_calls"))
    calls = every_call[:MAX_LISTED_CALLS]
    fields = dict(
        role=role,
        platform=source_of(session),
        session_id=sid,
        message_id=int(record["id"]),
        message_uid=as_text(row.get("message_uid")),
        tool_call_count=len(every_call) or None,
        model=as_text((session or {}).get("model")),
        token_count=as_int(row.get("token_count")),
        finish_reason=as_text(row.get("finish_reason")),
        tool_name=as_text(row.get("tool_name")),
        tool_call_id=as_text(row.get("tool_call_id")),
        tool_names=[name for _, name, _ in calls] or None,
        tool_call_ids=[cid for cid, _, _ in calls if cid] or None,
        tool_calls=[
            {
                "id": cid or None,
                "name": name,
                "arguments": arguments
                if not isinstance(arguments, str)
                else arguments[:CALL_ARGUMENT_CHARS],
            }
            for cid, name, arguments in calls
        ]
        or None,
        tool_output=None,
        tool_output_length=None,
        tool_output_truncated=None,
        content_length=as_int((record.get("sizes") or {}).get("content")),
        content_truncated="content" in (record.get("truncated") or [])
        or any(p.startswith("content[") for p in record.get("truncated") or []),
        reasoning_present=bool(record.get("reasoning_present")),
        visibility=visibility(row),
        compressed_summary=bool(row.get("_compressed_summary")),
        display_kind=as_text(row.get("display_kind")),
        platform_message_id=as_text(row.get("platform_message_id")),
        automated=automated,
        mapping_revision=MAPPING_REVISION,
    )
    if role == "tool":
        name = row.get("tool_name") or "unknown"
        fields["tool_output"] = text or None
        fields["tool_output_length"] = fields["content_length"]
        fields["tool_output_truncated"] = fields["content_truncated"]
        preview = short(text, settings.preview_chars) if text else "(no output)"
        content = f"tool {name} result: {preview}"
    else:
        lines = [text] if text else []
        for _, name, arguments in calls:
            shown = short_args(arguments)
            lines.append(f"tool {name}: {shown}" if shown else f"tool {name}")
        if len(every_call) > len(calls):
            lines.append(f"... and {len(every_call) - len(calls)} more tool calls")
        content = capped("\n".join(lines)) or f"({role} turn without text)"
    content = content.replace("\x00", "\\u0000")
    message = dict(
        channel=channel,
        external_id=message_external_id(sid, record["id"], row.get("message_uid")),
        thread=thread_for(sid, session),
        happened_at=happened,
        author=author_for(role, session, settings),
        content=content,
        fields=fields,
        images=[],
        payload={k: v for k, v in record.items() if k != "run"},
    )
    link = None
    target = record.get("reply_to")
    if role == "tool" and isinstance(target, dict) and target.get("id") is not None:
        to = message_external_id(sid, target["id"], target.get("uid"))
        link = {
            "type": "reply_to",
            "from": {"channel": channel, "external_id": message["external_id"]},
            "to": {"channel": channel, "external_id": to},
            "confidence": 1,
            "method": "source",
            "evidence": {"tool_call_id": str(row.get("tool_call_id"))},
        }
    return message, link


def session_fields(record, settings):
    row = record["payload"]
    sid = record["id"]
    prompt = record.get("system_prompt") or {}
    origin = origin_of(row)
    return dict(
        session_id=sid,
        platform=source_of(row),
        created_source=as_text(row.get("created_source")),
        user_id=as_text(row.get("user_id")),
        user_name=as_text(origin.get("user_name")),
        chat_id=as_text(row.get("chat_id")),
        chat_type=as_text(row.get("chat_type")),
        chat_name=as_text(origin.get("chat_name")),
        owner_session=is_owner_session(row, settings),
        automated=is_automated(sid, row, settings),
        model=as_text(row.get("model")),
        provider=as_text(row.get("billing_provider")),
        title=as_text(row.get("title")),
        parent_session_id=as_text(row.get("parent_session_id")),
        started=moment(row.get("started_at")),
        ended=moment(row.get("ended_at")),
        end_reason=as_text(row.get("end_reason")),
        message_count=as_int(row.get("message_count")),
        tool_call_count=as_int(row.get("tool_call_count")),
        api_call_count=as_int(row.get("api_call_count")),
        input_tokens=as_int(row.get("input_tokens")),
        output_tokens=as_int(row.get("output_tokens")),
        cache_read_tokens=as_int(row.get("cache_read_tokens")),
        cache_write_tokens=as_int(row.get("cache_write_tokens")),
        reasoning_tokens=as_int(row.get("reasoning_tokens")),
        estimated_cost_usd=as_float(row.get("estimated_cost_usd")),
        actual_cost_usd=as_float(row.get("actual_cost_usd")),
        profile_name=as_text(row.get("profile_name")),
        system_prompt_sha256=as_text(prompt.get("sha256")),
        system_prompt_length=as_int(prompt.get("length")),
        system_prompt_hermes_hash=as_text(prompt.get("hermes_hash")),
        system_prompt=as_text(prompt.get("text")),
        mapping_revision=MAPPING_REVISION,
    )


def map_session(record, settings):
    """The hermes.sessions reference record of one raw session record."""
    row = record["payload"]
    fields = session_fields(record, settings)
    started = fields["started"]
    if started is None:
        return None
    ended = fields["ended"]
    if ended is not None and epoch(row.get("ended_at")) < epoch(row.get("started_at")):
        ended = None
    counts = []
    if fields["message_count"] is not None:
        counts.append(f"{fields['message_count']} messages")
    if fields["tool_call_count"]:
        counts.append(f"{fields['tool_call_count']} tool calls")
    tokens = (fields["input_tokens"] or 0) + (fields["output_tokens"] or 0)
    if tokens:
        counts.append(f"{tokens} tokens")
    lines = [
        f"Hermes session on {fields['platform']}: {session_title(record['id'], row)}",
        "Started "
        + started[:16].replace("T", " ")
        + " UTC"
        + (", ended " + ended[:16].replace("T", " ") + " UTC" if ended else ""),
    ]
    if fields["model"]:
        lines.append(f"Model: {fields['model']}")
    if counts:
        lines.append(", ".join(counts))
    if fields["end_reason"]:
        lines.append(f"End reason: {fields['end_reason']}")
    if fields["automated"]:
        lines.append("Automated run")
    message = dict(
        channel=SESSIONS,
        external_id=str(record["id"]),
        thread={
            "external_id": f"sessions:{fields['platform']}",
            "title": f"Hermes sessions ({fields['platform']})",
            "metadata": {"kind": "hermes_sessions", "source": fields["platform"]},
        },
        happened_at=started,
        author=None,
        content="\n".join(lines),
        fields={k: v for k, v in fields.items() if k not in ("started", "ended")},
        images=[],
        payload={k: v for k, v in record.items() if k != "run"},
    )
    if ended is not None:
        message["ended_at"] = ended
    return message


__all__ = [
    "AUTOMATION",
    "MAPPING_REVISION",
    "MESSAGES",
    "SESSIONS",
    "Settings",
    "call_ids",
    "map_message",
    "map_session",
]
