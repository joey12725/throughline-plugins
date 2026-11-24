"""Android SMS, MMS and call log from "SMS Backup & Restore" scheduled backups.

Android has no API for reading SMS from another machine. The free SMS Backup &
Restore app can back up messages and calls on a schedule (for example every
night) into a folder that Syncthing, Google Drive, Dropbox or Nextcloud keeps
in sync with the machine running this plugin. Each run reads only backup
files it has not processed yet (tracked by name, size and modification time),
streaming them with iterparse. Backups overlap (a full backup repeats every
older message), so record ids come from the record itself (time, addresses,
direction and a body hash) and records older than the newest one already
delivered, minus an overlap window, are skipped.
"""

import logging
from xml.etree.ElementTree import ParseError

from throughline_sdk import Channel, Checkpoint, Declaration, Manifest, Plugin, run

from .backup import discover, fingerprint, iterate
from .mapping import CALLS, MESSAGES, map_record, millis, normalize_address

__all__ = ["AndroidSMS", "main"]
log = logging.getLogger("throughline_android_sms")


def fields(*rows):
    return [dict(name=n, type=t, description=d) for n, t, d in rows]


MESSAGE_FIELDS = fields(
    ("direction", "text", "incoming or outgoing relative to the phone's owner"),
    ("author_kind", "text", "owner (sent from your phone) or other"),
    ("message_kind", "text", "sms or mms"),
    ("box", "text", "inbox, sent, outbox, failed or queued"),
    ("conversation_kind", "text", "dm or group_dm (group MMS)"),
    ("participants", "json", "Other participants' normalized addresses"),
    ("sender_address", "text", "Sender's number for incoming messages"),
    ("subject", "text", "SMS/MMS subject when set"),
    ("sent_at", "timestamp", "Sender's send time (date_sent) when recorded"),
    ("attachments", "json", "MMS parts other than text: [{ct, name, fn, cl, data_bytes}], metadata only"),
)
CALL_FIELDS = fields(
    ("direction", "text", "incoming or outgoing"),
    ("call_type", "text", "incoming, outgoing, missed, voicemail, rejected, blocked or answered_externally"),
    ("duration_s", "integer", "Call duration in seconds"),
    ("number", "text", "Other party's normalized number"),
    ("author_kind", "text", "owner (you placed the call) or other"),
)


class AndroidSMS(Plugin):
    manifest = Manifest(
        name="android_sms",
        version="0.1.0",
        channels=[
            Channel(
                name=MESSAGES,
                description="SMS and MMS from an Android phone (SMS Backup & Restore backups), one thread per conversation",
                fields=MESSAGE_FIELDS,
            ),
            Channel(
                name=CALLS,
                description="Android call log entries (SMS Backup & Restore backups), one thread per number",
                fields=CALL_FIELDS,
                conversation_gap_s=86400,
                embed=False,
            ),
        ],
        settings={
            "dir": Declaration(
                env="ANDROID_SMS_DIR",
                description="Folder the app's scheduled backups are synced into (sms-*.xml, calls-*.xml)",
            ),
            "owner_number": Declaration(
                env="ANDROID_SMS_OWNER_NUMBER",
                required=False,
                description="Your own phone number, used as the author of sent messages and placed calls",
            ),
            "country_code": Declaration(
                env="ANDROID_SMS_COUNTRY_CODE",
                required=False,
                description="Calling code (e.g. 1) added to 10-digit national numbers so they match +E.164 forms",
            ),
            "overlap_days": Declaration(
                env="ANDROID_SMS_OVERLAP_DAYS",
                default="7",
                description="Re-read records this many days older than the newest delivered one in new backups; 'all' reads every record of every new backup",
            ),
            "batch_size": Declaration(
                env="ANDROID_SMS_BATCH_SIZE",
                default="2000",
                description="Records per checkpointed batch",
            ),
        },
        default_interval_s=3600,
    )

    def records(self, cursor, mode):
        cursor = {
            "done": dict((cursor or {}).get("done") or {}),
            "hw": dict((cursor or {}).get("hw") or {}),
            "partial": (cursor or {}).get("partial"),
        }
        batch = max(1, int(self.settings["batch_size"] or 2000))
        owner = normalize_address(
            self.settings.get("owner_number") or "", self.settings.get("country_code")
        ) or None
        country = self.settings.get("country_code") or None
        overlap = (self.settings.get("overlap_days") or "7").strip().lower()
        overlap_ms = None if overlap == "all" else int(float(overlap) * 86400000)
        files = discover(self.settings["dir"])
        if not files:
            raise RuntimeError(f"no SMS Backup & Restore files in {self.settings['dir']}")
        present = {fingerprint(p) for p, _ in files}
        # Forget files that were deleted or rotated away.
        cursor["done"] = {k: v for k, v in cursor["done"].items() if k in present}
        seen = set()
        failures = attempted = 0
        for path, kind in files:
            fp = fingerprint(path)
            if fp in cursor["done"]:
                continue
            attempted += 1
            partial = cursor["partial"] or {}
            skip = int(partial.get("n") or 0) if partial.get("fp") == fp else 0
            hw = cursor["hw"].get(kind)
            floor = None if hw is None or overlap_ms is None else int(hw) - overlap_ms
            newest = int(hw) if hw is not None else None
            count = 0
            try:
                for record in iterate(path):
                    count += 1
                    stamp = millis(record)
                    newest = stamp if newest is None else max(newest, stamp)
                    if count <= skip:
                        continue
                    if floor is not None and stamp < floor:
                        continue
                    message = map_record(record, owner, country)
                    if message is not None and message["external_id"] not in seen:
                        seen.add(message["external_id"])
                        yield message
                    if count % batch == 0:
                        yield Checkpoint(
                            cursor=dict(cursor, partial={"fp": fp, "n": count})
                        )
            except (ParseError, OSError) as error:
                # Usually a backup still being written or synced: retried next run.
                failures += 1
                log.warning("skipping unreadable backup %s: %s", path.name, error)
                continue
            cursor["done"][fp] = kind
            if newest is not None:
                cursor["hw"][kind] = newest
            cursor["partial"] = None
            yield Checkpoint(cursor=dict(cursor))
        if attempted and failures == attempted and not cursor["done"]:
            raise RuntimeError("no backup file could be read")


def main():
    run(AndroidSMS)
