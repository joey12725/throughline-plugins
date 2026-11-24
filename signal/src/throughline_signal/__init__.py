"""Signal messages through signal-cli running as a linked device.

Each run (1) drains signal-cli's JSON-RPC `receive` into an append-only
JSONL spool, fsyncing before mapping anything, because receive acknowledges
envelopes to the Signal server and they are never delivered again; then (2)
indexes the whole spool and maps the lines added since the last checkpoint.
Without SIGNAL_RPC_URL the plugin only maps a spool that something else
appends to (for example `signal-cli -o json receive >> spool/x.jsonl`).
"""

import json
import logging
from pathlib import Path

from throughline_sdk import Channel, Checkpoint, Declaration, Manifest, Plugin, Record, run

from .mapping import CHANNEL, Index, build, classify, reply_link
from .spool import SignalRPC, drain, read_lines, spool_files

__all__ = ["Signal", "main"]
log = logging.getLogger("throughline_signal")

FIELDS = [
    ("direction", "text", "incoming or outgoing relative to your account"),
    ("author_kind", "text", "owner (sent from one of your devices) or other"),
    ("conversation_kind", "text", "dm or group_dm"),
    ("group_id", "text", "Signal group v2 id (base64)"),
    ("sender_uuid", "text", "Sender's Signal ACI (uuid)"),
    ("sender_device", "integer", "Sending device id"),
    ("reply_to", "text", "External ID of the quoted message"),
    ("quote_text", "text", "Quoted text as the sender's app included it"),
    ("edited_at", "timestamp", "Time of the latest edit"),
    ("edit_count", "integer", "Number of edits received"),
    ("deleted_at", "timestamp", "When the sender deleted the message for everyone; the received text is kept"),
    ("attachments", "json", "[{contentType, filename, size, width, height, caption, id, voiceNote}] metadata only"),
    ("sticker", "json", "Sticker metadata {packId, stickerId, emoji?}"),
    ("reaction_details", "json", "Current reactions: [{emoji, sender_id, sender_name? | sender_kind: owner, at}]"),
    ("expires_in_s", "integer", "Disappearing-message timer in seconds"),
    ("view_once", "boolean", "View-once media"),
    ("mentions", "json", "Mentions [{uuid, number?, name?, start, length}]"),
]


class Signal(Plugin):
    manifest = Manifest(
        name="signal",
        version="0.1.0",
        channels=[
            Channel(
                name=CHANNEL,
                description="Signal messages received by a linked signal-cli device, one thread per chat or group",
                fields=[dict(name=n, type=t, description=d) for n, t, d in FIELDS],
            )
        ],
        settings={
            "rpc_url": Declaration(
                env="SIGNAL_RPC_URL",
                default="http://127.0.0.1:8080",
                description="signal-cli daemon --http origin; 'none' (or empty) only maps the spool",
            ),
            "account": Declaration(
                env="SIGNAL_ACCOUNT",
                required=False,
                description="Your Signal number (+E.164); required when the daemon serves several accounts",
            ),
            "multi_account": Declaration(
                env="SIGNAL_MULTI_ACCOUNT",
                default="0",
                description="1 when the daemon was started without -a (multi-account mode): SIGNAL_ACCOUNT is sent with every request",
            ),
            "state_dir": Declaration(
                env="SIGNAL_STATE_DIR",
                default="~/.local/state/throughline-signal",
                description="Directory for the spool and group names",
            ),
            "spool_dir": Declaration(
                env="SIGNAL_SPOOL_DIR",
                required=False,
                description="Spool directory, default <state>/spool",
            ),
            "receive_timeout": Declaration(
                env="SIGNAL_RECEIVE_TIMEOUT",
                default="5",
                description="Seconds one receive call waits for new envelopes",
            ),
            "max_rounds": Declaration(
                env="SIGNAL_MAX_ROUNDS",
                default="20",
                description="Receive calls per run at most",
            ),
            "batch_size": Declaration(
                env="SIGNAL_BATCH_SIZE",
                default="500",
                description="Spool lines per checkpointed batch",
            ),
        },
        secrets={
            "rpc_token": Declaration(
                env="SIGNAL_RPC_TOKEN",
                required=False,
                description="Bearer token when the daemon sits behind an authenticating reverse proxy",
            ),
        },
        default_interval_s=300,
        link_types=["reply_to"],
    )

    transport = None  # tests inject an httpx.MockTransport

    @property
    def state_dir(self):
        return Path(self.settings["state_dir"]).expanduser()

    @property
    def spool_dir(self):
        configured = self.settings.get("spool_dir")
        return Path(configured).expanduser() if configured else self.state_dir / "spool"

    def receive(self):
        url = (self.settings.get("rpc_url") or "").strip()
        if not url or url.lower() == "none":
            return None
        rpc = SignalRPC(
            url,
            account=(self.settings.get("account") or None)
            if self.settings.get("multi_account") == "1"
            else None,
            token=self.secrets.get("rpc_token") or None,
            transport=self.transport,
        )
        try:
            received = drain(
                rpc,
                self.spool_dir,
                timeout=int(self.settings.get("receive_timeout") or 5),
                max_messages=int(self.settings.get("batch_size") or 500),
                max_rounds=int(self.settings.get("max_rounds") or 20),
                account=self.settings.get("account") or None,
            )
            try:
                groups = {g["id"]: g.get("name") for g in rpc.groups() if g.get("id")}
            except Exception as error:  # names are cosmetic
                log.warning("listGroups failed: %s", type(error).__name__)
                groups = None
            if groups:
                self.state_dir.mkdir(parents=True, exist_ok=True)
                (self.state_dir / "groups.json").write_text(json.dumps(groups, sort_keys=True))
            return received
        finally:
            rpc.close()

    def groups(self):
        try:
            return json.loads((self.state_dir / "groups.json").read_text())
        except (OSError, ValueError):
            return {}

    def records(self, cursor, mode):
        # Spool first: an error here (daemon down) fails the run before any
        # mapping, and nothing received is ever lost.
        self.receive()
        account = self.settings.get("account") or None
        files = spool_files(self.spool_dir)
        index = Index()
        for path in files:
            for _, item in read_lines(path):
                event = classify(item, account)
                if event:
                    index.add(event)
        groups = self.groups()
        offsets = dict((cursor or {}).get("files") or {})
        batch = max(1, int(self.settings.get("batch_size") or 500))
        emitted = set()
        for path in files:
            start = int(offsets.get(path.name) or 0)
            if start > path.stat().st_size:
                start = 0  # the file was replaced
            pending = 0
            for offset, item in read_lines(path, start):
                event = classify(item, account)
                key = (event.key or event.target) if event else None
                if key and key in index.base and key not in emitted:
                    emitted.add(key)
                    message = build(key, index, groups, account)
                    yield message
                    target = message["fields"].get("reply_to")
                    if target and target in index.base:
                        yield Record(kind="link", data=reply_link(key, target))
                offsets[path.name] = offset
                pending += 1
                if pending >= batch:
                    pending = 0
                    yield Checkpoint(cursor={"files": dict(offsets)})
            yield Checkpoint(cursor={"files": dict(offsets)})


def main():
    run(Signal)
