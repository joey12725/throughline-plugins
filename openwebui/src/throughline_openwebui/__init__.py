"""Open WebUI chats as a Throughline source.

Polls an Open WebUI server's REST API with your API key. Every message node
of every chat is recorded, on every branch (regenerations and edits create
sibling nodes in Open WebUI's message tree), and `is_current_path` marks the
branch the chat currently shows. Your messages carry author_kind `owner`,
model replies author_kind `agent` with the model id and name.

Incremental: chats are listed newest-updated first and fetched when their
`updated_at` is at or after the high-water mark of the last checkpoint; all
nodes of a changed chat are re-sent (unchanged ones are no-ops in the core,
changed ones - a new current branch, an edit - become new versions).
"""

import copy
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse

from throughline_sdk import Channel, Checkpoint, Declaration, Manifest, Plugin, run

from .api import NotFound, OpenWebUI, fixture_transport

__all__ = ["OpenWebUIPlugin", "MAPPING_REVISION", "main"]

MAPPING_REVISION = 1
CHANNEL = "openwebui.messages"
TEXT_LIMIT = 100_000
log = logging.getLogger("throughline_openwebui")


def fields(**values):
    return [dict(name=k, type=v[0], description=v[1]) for k, v in values.items()]


MESSAGE_FIELDS = fields(
    role=("text", "user, assistant or system"),
    author_kind=("text", "owner (you wrote it) or agent (a model's reply)"),
    model=("text", "Model id that answered (assistant) or that the message was sent to (user)"),
    model_name=("text", "Model display name Open WebUI recorded"),
    chat_id=("text", "Open WebUI chat id"),
    message_id=("text", "Open WebUI message (tree node) id"),
    parent_id=("text", "Parent node id in the chat's message tree"),
    is_current_path=("boolean", "On the branch the chat currently shows"),
    branch_index=("integer", "Position among sibling nodes (0 = first answer or first version)"),
    sibling_count=("integer", "Sibling nodes including this one (more than 1 = regenerated or edited)"),
    chat_title=("text", "Chat title"),
    tags=("json", "Chat tags"),
    archived=("boolean", "The chat is archived"),
    pinned=("boolean", "The chat is pinned"),
    folder_id=("text", "Open WebUI folder id"),
    rating=("integer", "Your rating of the reply (1 or -1), if any"),
    has_error=("boolean", "The reply ended in an error"),
    file_count=("integer", "Files and images attached to the message"),
    input_tokens=("integer", "Prompt tokens reported for the reply"),
    output_tokens=("integer", "Completion tokens reported for the reply"),
    text_length=("integer", "Characters of the message text"),
    text_truncated=("boolean", "The text was cut to 100,000 characters"),
    mapping_revision=("integer", "Mapping revision that produced this record"),
)

SETTINGS = [
    ("url", "OPENWEBUI_URL", None, "Base URL of your Open WebUI server, e.g. https://chat.example.com"),
    ("include_archived", "OPENWEBUI_INCLUDE_ARCHIVED", "true", "Also import archived chats"),
    ("batch_size", "OPENWEBUI_BATCH_SIZE", "25", "Chats per checkpoint"),
    ("timeout_s", "OPENWEBUI_TIMEOUT_S", "30", "HTTP timeout in seconds"),
    ("owner_identifier", "OPENWEBUI_OWNER_IDENTIFIER", "openwebui:owner:owner",
     "Author identifier of your own messages, system:kind:value (e.g. email:address:you@example.com)"),
    ("owner_name", "OPENWEBUI_OWNER_NAME", None, "Display name on your messages"),
    ("fixture", "OPENWEBUI_FIXTURE", None, "Testing: answer from a recorded JSON file instead of the network"),
]


def truthy(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def moment(value):
    """Aware UTC datetime from epoch seconds (or ms/ns) or ISO text."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return moment(float(text))
        except ValueError:
            pass
        try:
            when = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        return when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        while value > 1e11:  # ms, us or ns
            value = value / 1000
        if value <= 0:
            return None
        return datetime.fromtimestamp(value, timezone.utc)
    return None


def epoch(value):
    when = moment(value)
    return when.timestamp() if when else 0.0


def text_of(content):
    """Message text: a string, or OpenAI-style content parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
            if isinstance(part, (dict, str))
        )
    return ""


def tree(chat):
    """{id: node} and the current node id, from either the message tree
    (`history`) or, in old releases, a linear `messages` list."""
    history = chat.get("history") if isinstance(chat.get("history"), dict) else {}
    nodes = history.get("messages")
    if isinstance(nodes, dict) and nodes:
        clean = {str(k): v for k, v in nodes.items() if isinstance(v, dict)}
        for key, node in clean.items():
            node.setdefault("id", key)
        return clean, history.get("currentId")
    linear = chat.get("messages")
    if isinstance(linear, list) and linear:
        clean, parent = {}, None
        for index, node in enumerate(linear):
            if not isinstance(node, dict):
                continue
            node = dict(node)
            node.setdefault("id", f"m{index}")
            node.setdefault("parentId", parent)
            clean[str(node["id"])] = node
            if parent is not None and parent in clean:
                clean[parent].setdefault("childrenIds", []).append(node["id"])
            parent = str(node["id"])
        return clean, parent
    return {}, None


def current_path(nodes, current):
    path, node_id = set(), current
    while node_id is not None and str(node_id) in nodes and str(node_id) not in path:
        path.add(str(node_id))
        node_id = nodes[str(node_id)].get("parentId")
    return path


class OpenWebUIPlugin(Plugin):
    manifest = Manifest(
        name="openwebui",
        version="0.1.0",
        channels=[
            Channel(
                name=CHANNEL,
                description=(
                    "Open WebUI chats: every message on every branch of each chat's "
                    "message tree, your messages (author_kind owner) and model replies "
                    "(agent, with the model); is_current_path marks the branch the chat "
                    "shows; one thread per chat"
                ),
                fields=MESSAGE_FIELDS,
                conversation_gap_s=1800,
            )
        ],
        settings={
            key: Declaration(env=env, required=key == "url", default=default, description=description)
            for key, env, default, description in SETTINGS
        },
        secrets={
            "api_key": Declaration(
                env="OPENWEBUI_API_KEY",
                description="API key from Open WebUI: Settings > Account > API keys",
            )
        },
        run_mode="poll",
        default_interval_s=900,
    )

    def __init__(self, transport=None):
        super().__init__()
        url = self.settings["url"].strip()
        if urlparse(url).scheme not in ("http", "https"):
            raise ValueError("OPENWEBUI_URL must be an http(s) URL")
        self.url = url
        self.transport = transport or (
            fixture_transport(self.settings["fixture"]) if self.settings["fixture"] else None
        )
        self.batch_size = max(1, int(self.settings["batch_size"] or 25))
        self.include_archived = truthy(self.settings["include_archived"])
        parts = (self.settings["owner_identifier"] or "openwebui:owner:owner").split(":", 2)
        if len(parts) != 3 or not all(parts):
            raise ValueError("OPENWEBUI_OWNER_IDENTIFIER must be system:kind:value")
        self.owner = dict(system=parts[0], kind=parts[1], value=parts[2], display_name=self.settings["owner_name"])
        self.stats = {}

    def execute(self, client, mode, dry_run=False):
        report = super().execute(client, mode, dry_run)
        if self.stats:
            report["openwebui"] = self.stats
        return report

    def api(self):
        return OpenWebUI(
            self.url,
            self.secrets["api_key"],
            transport=self.transport,
            timeout=float(self.settings["timeout_s"] or 30),
        )

    # -- listing ---------------------------------------------------------------

    def changed_chats(self, api, high_water):
        """{id: (updated, archived)} of chats updated at or after high_water."""
        found = {}

        def take(items, archived):
            fresh = False
            for item in items:
                if not isinstance(item, dict) or not item.get("id"):
                    self.stats["unknown_list_items"] = self.stats.get("unknown_list_items", 0) + 1
                    continue
                updated = epoch(item.get("updated_at"))
                if updated >= high_water:
                    fresh = True
                    old = found.get(item["id"])
                    found[item["id"]] = (max(updated, old[0]) if old else updated, archived)
            return fresh

        for page in api.chat_pages("/api/v1/chats/"):
            if not take(page, False):
                break  # Newest first: nothing further down changed.
        take(api.pinned(), False)
        if self.include_archived:
            try:
                for page in api.chat_pages("/api/v1/chats/archived"):
                    if not take(page, True):
                        break
            except NotFound:
                self.stats["archived_unsupported"] = True
        return found

    # -- mapping ---------------------------------------------------------------

    def author(self, role, model, model_name):
        if role == "assistant":
            return dict(system="openwebui", kind="agent", value=model or "assistant",
                        display_name=model_name or model or "Open WebUI assistant")
        return dict(self.owner)

    def map_chat(self, record, archived):
        chat = record.get("chat") if isinstance(record.get("chat"), dict) else {}
        chat_id = str(record.get("id") or chat.get("id"))
        title = record.get("title") or chat.get("title") or "New Chat"
        nodes, current = tree(chat)
        if not nodes:
            self.stats["empty_chats"] = self.stats.get("empty_chats", 0) + 1
            return []
        on_path = current_path(nodes, current)
        created = moment(record.get("created_at")) or moment(chat.get("timestamp")) or moment(record.get("updated_at"))
        tags = chat.get("tags") if isinstance(chat.get("tags"), list) else record.get("tags")
        tags = [t.get("name", t.get("id")) if isinstance(t, dict) else t for t in tags or []]
        pinned = bool(record.get("pinned"))
        archived = bool(record.get("archived", archived))
        thread = {
            "external_id": chat_id,
            "title": str(title)[:300],
            "metadata": {
                "kind": "openwebui_chat",
                "archived": archived,
                "pinned": pinned,
                "tags": tags,
                "folder_id": record.get("folder_id"),
                "models": chat.get("models") if isinstance(chat.get("models"), list) else None,
            },
        }
        roots = sorted(
            (n for n in nodes.values() if n.get("parentId") is None or str(n.get("parentId")) not in nodes),
            key=lambda n: (epoch(n.get("timestamp")), str(n["id"])),
        )
        out = []
        for node in nodes.values():
            role = node.get("role")
            if role not in ("user", "assistant", "system"):
                self.stats["unknown_roles"] = self.stats.get("unknown_roles", 0) + 1
                continue
            node_id = str(node["id"])
            parent = node.get("parentId")
            siblings = (
                [str(c) for c in nodes[str(parent)].get("childrenIds") or []]
                if parent is not None and str(parent) in nodes
                else [str(n["id"]) for n in roots]
            )
            if node_id not in siblings:
                siblings.append(node_id)
            when = moment(node.get("timestamp")) or created
            if when is None:
                self.stats["untimed_messages"] = self.stats.get("untimed_messages", 0) + 1
                continue
            text = text_of(node.get("content")).strip()
            error = node.get("error")
            if not text and isinstance(error, dict):
                text = str(error.get("content") or "").strip()
            truncated = len(text) > TEXT_LIMIT
            content = text[:TEXT_LIMIT] or f"(empty {role} message)"
            if role == "assistant":
                model = node.get("model")
                model_name = node.get("modelName")
            else:
                models = node.get("models") if isinstance(node.get("models"), list) else []
                model = ", ".join(str(m) for m in models) or None
                model_name = None
            usage = node.get("usage") if isinstance(node.get("usage"), dict) else {}
            info = node.get("info") if isinstance(node.get("info"), dict) else {}
            input_tokens = usage.get("prompt_tokens") or usage.get("input_tokens") or info.get("prompt_eval_count")
            output_tokens = usage.get("completion_tokens") or usage.get("output_tokens") or info.get("eval_count")
            annotation = node.get("annotation") if isinstance(node.get("annotation"), dict) else {}
            rating = annotation.get("rating")
            files = node.get("files") if isinstance(node.get("files"), list) else []
            payload = {
                "chat_id": chat_id,
                "id": node_id,
                "parentId": None if parent is None else str(parent),
                "childrenIds": [str(c) for c in node.get("childrenIds") or []],
                "role": role,
                "content": content,
                "model": model,
                "modelName": model_name,
                "timestamp": node.get("timestamp"),
                "done": node.get("done"),
                "is_current_path": node_id in on_path,
                "files": len(files),
            }
            out.append(
                dict(
                    channel=CHANNEL,
                    external_id=f"{chat_id}:{node_id}",
                    thread=thread,
                    happened_at=when.isoformat(),
                    author=self.author(role, model, model_name),
                    content=content,
                    fields=dict(
                        role=role,
                        author_kind="agent" if role == "assistant" else "owner",
                        model=model,
                        model_name=model_name,
                        chat_id=chat_id,
                        message_id=node_id,
                        parent_id=payload["parentId"],
                        is_current_path=node_id in on_path,
                        branch_index=siblings.index(node_id),
                        sibling_count=len(siblings),
                        chat_title=str(title),
                        tags=tags,
                        archived=archived,
                        pinned=pinned,
                        folder_id=record.get("folder_id"),
                        rating=rating if isinstance(rating, int) and not isinstance(rating, bool) else None,
                        has_error=bool(error),
                        file_count=len(files),
                        input_tokens=input_tokens if isinstance(input_tokens, int) else None,
                        output_tokens=output_tokens if isinstance(output_tokens, int) else None,
                        text_length=len(text),
                        text_truncated=truncated,
                        mapping_revision=MAPPING_REVISION,
                    ),
                    payload=payload,
                )
            )
        out.sort(key=lambda m: (m["happened_at"], m["external_id"]))
        return out

    def records(self, cursor, mode):
        state = copy.deepcopy(cursor or {})
        if state.get("mapping_revision") != MAPPING_REVISION or state.get("url") != self.url:
            state = {"mapping_revision": MAPPING_REVISION, "url": self.url}
        high_water = float(state.get("high_water") or 0)
        self.stats = dict(chats=0, messages=0)
        api = self.api()
        try:
            listed = self.changed_chats(api, high_water)
            order = sorted(listed.items(), key=lambda item: (item[1][0], item[0]))
            self.stats["changed_chats"] = len(order)
            done = 0
            for chat_id, (updated, archived) in order:
                try:
                    record = api.chat(chat_id)
                except NotFound:
                    self.stats["vanished_chats"] = self.stats.get("vanished_chats", 0) + 1
                    continue
                if not isinstance(record, dict):
                    self.stats["unknown_chats"] = self.stats.get("unknown_chats", 0) + 1
                    continue
                messages = self.map_chat(record, archived)
                yield from messages
                self.stats["chats"] += 1
                self.stats["messages"] += len(messages)
                state["high_water"] = max(high_water, updated)
                done += 1
                if done % self.batch_size == 0:
                    yield Checkpoint(cursor=copy.deepcopy(state))
            self.stats["requests"] = api.requests
        finally:
            api.close()
        yield Checkpoint(cursor=copy.deepcopy(state))


def main(argv=None):
    run(OpenWebUIPlugin, argv)
