"""Email (Gmail through Composio): one message per email, threaded per Gmail thread.

Two layers: fetch.py (standard library only) reads Composio and writes raw
JSONL records; this plugin maps those records, either live (running the
fetcher in-process, EMAIL_SOURCE=live) or from export files written on
another host (EMAIL_SOURCE=export:<file or directory>).
"""

import copy
import hashlib
import json
import logging
from datetime import UTC, datetime

from throughline_sdk import Channel, Checkpoint, Declaration, Manifest, Plugin, run

from . import fetch, mapping, sources

LOG = logging.getLogger("throughline_email")
MESSAGE_KIND = "gmail.message"


def fetched_rank(record):
    """Sort key for a record's fetched_at: the instant, not the text (an ISO
    stamp without microseconds would otherwise sort after a later one with
    them). Unparseable stamps rank below every parseable one."""
    value = str(record.get("fetched_at") or "")
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return (0, value)
    if at.utcoffset() is None:
        at = at.replace(tzinfo=UTC)
    return (1, at.astimezone(UTC).isoformat(timespec="microseconds"))


class Email(Plugin):
    manifest = Manifest(
        name="email",
        version="0.1.0",
        default_interval_s=900,
        channels=[
            Channel(
                name=mapping.CHANNEL,
                description=(
                    "Email messages (Gmail through Composio), one version per fetched "
                    "state; threads are Gmail threads; happened_at is Gmail's internal date"
                ),
                fields=[
                    dict(name=n, type=t, description=d) for n, t, d in mapping.FIELDS
                ],
                conversation_whole_thread=True,
            )
        ],
        settings={
            "source": Declaration(
                env="EMAIL_SOURCE",
                required=False,
                default="live",
                description="live, or export:<JSONL file or directory written by fetch.py export>",
            ),
            "body_limit": Declaration(
                env="EMAIL_BODY_LIMIT",
                required=False,
                default=str(mapping.DEFAULT_BODY_LIMIT),
                description="Body characters kept in content (0 = all); when truncated the payload keeps the full body text",
            ),
            "export_batch": Declaration(
                env="EMAIL_EXPORT_BATCH",
                required=False,
                default="500",
                description="Messages per checkpoint when reading export files",
            ),
            **{
                key: Declaration(
                    env=env, required=False, default=default, description=text
                )
                for key, env, default, text in fetch.SETTINGS
            },
        },
        secrets={
            key: Declaration(env=env, required=False, description=text)
            for key, env, text in fetch.SECRETS
        },
        link_types=[],
    )

    def body_limit(self):
        return fetch._int(self.settings.get("body_limit") or "0", "EMAIL_BODY_LIMIT")

    def map(self, record):
        if record.get("kind") != MESSAGE_KIND:
            return None
        try:
            return mapping.map_record(record, self.body_limit())
        except ValueError as error:
            # One malformed email must not block a mailbox; it is reported.
            LOG.warning("email record skipped: %s", error)
            return None

    def records(self, cursor, mode):
        kind, path = sources.parse_source(self.settings.get("source"))
        if kind == "export":
            yield from self.exported(path, cursor)
        else:
            yield from self.live(cursor)

    def live(self, cursor):
        state = copy.deepcopy(cursor)
        values = {key: self.settings.get(key) for key, _, _, _ in fetch.SETTINGS}
        values.update(self.secrets)
        fetcher = fetch.Fetcher(fetch.Config(values), state.get("live"))
        for event, item in fetcher.run():
            if event == "record":
                message = self.map(item)
                if message:
                    yield message
            else:
                state["live"] = item
                yield Checkpoint(cursor=copy.deepcopy(state))

    def exported(self, root, cursor):
        """Map export files. Files already imported (same name and digest)
        are skipped; for each (account, message) the record with the latest
        fetched_at (then file and line order) wins over all files, so a new
        file reproduces exactly what a fresh import of every file gives."""
        state = copy.deepcopy(cursor)
        progress = state.setdefault("export", {})
        done = progress.setdefault("files", {})
        batch = fetch._int(
            self.settings.get("export_batch") or "500", "EMAIL_EXPORT_BATCH", 1
        )
        files = sources.export_files(root)
        digests = {name: sources.digest(path) for name, path in files}
        new = {name: d for name, d in digests.items() if done.get(name) != d}
        if not new:
            progress.pop("pending", None)
            yield Checkpoint(cursor=state)
            return
        signature = hashlib.sha256(json.dumps(sorted(new.items())).encode()).hexdigest()
        pending = progress.get("pending") or {}
        skip = pending.get("offset", 0) if pending.get("signature") == signature else 0
        winners = {}
        for index, (name, path) in enumerate(files):
            for number, record in sources.read(path, name):
                if record.get("kind") != MESSAGE_KIND:
                    continue
                key = mapping.record_key(record)
                rank = (fetched_rank(record), index, number)
                if key not in winners or rank > winners[key]:
                    winners[key] = rank
        emitted = 0
        for index, (name, path) in enumerate(files):
            if name not in new:
                continue
            for number, record in sources.read(path, name):
                if record.get("kind") != MESSAGE_KIND:
                    continue
                if winners[mapping.record_key(record)][1:] != (index, number):
                    continue
                emitted += 1
                if emitted <= skip:
                    continue
                message = self.map(record)
                if message:
                    yield message
                if emitted % batch == 0:
                    progress["pending"] = {"signature": signature, "offset": emitted}
                    yield Checkpoint(cursor=copy.deepcopy(state))
        progress.pop("pending", None)
        done.update(new)
        yield Checkpoint(cursor=state)


def main():
    run(Email)
