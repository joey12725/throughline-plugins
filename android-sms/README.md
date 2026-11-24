# Throughline Android SMS plugin

Imports SMS, MMS and the call log from an Android phone. Android has no API
for reading messages from another machine, so this plugin reads the XML files
that the free **SMS Backup & Restore** app (SyncTech) writes on a schedule.
Point the app's scheduled backup at a folder that is synced to the machine
running Throughline (Syncthing, Google Drive, Dropbox, Nextcloud, a network
share); every new backup is picked up by the next run. You set it up once;
there is no manual export step.

No dependencies beyond the Throughline SDK (stdlib `xml.etree` iterparse).

## What it captures

| Channel | Contents |
|---|---|
| `android_sms.messages` | SMS and MMS, one thread per conversation (group MMS included) |
| `android_sms.calls` | Call log entries, one thread per number; not embedded by default |

`android_sms.messages` fields: `direction`, `author_kind` (`owner` / `other`),
`message_kind` (`sms` / `mms`), `box` (`inbox`, `sent`, `outbox`, `failed`,
`queued`), `conversation_kind` (`dm` / `group_dm`), `participants`,
`sender_address`, `subject`, `sent_at`, `attachments` (MMS parts other than
text: `[{ct, name, fn, cl, data_bytes}]`; media is never stored).

`android_sms.calls` fields: `direction`, `call_type` (`incoming`, `outgoing`,
`missed`, `voicemail`, `rejected`, `blocked`, `answered_externally`),
`duration_s`, `number`, `author_kind`. Answered calls have `ended_at`.

Authors are `{system: "phone", kind: "number"}` so the identity registry can
match people by number. Sent messages and placed calls are authored by
`ANDROID_SMS_OWNER_NUMBER` when set. Drafts are skipped.

### Overlapping backups and ids

A full backup repeats every older message, and the same message appears in
many files. Each record gets a stable id from its own content: time,
participants, direction and a hash of the body (calls: time, number, type),
for example `android_sms:sms:<hash>`. Values that change between backups
(read/seen flags, the phone's current contact name, the human-readable date)
are left out of the payload, so a record maps identically from every backup.

The cursor stores the fingerprint (name, size, modification time) of each
processed backup file and the newest record time per kind. A run reads only
new files and, within them, skips records older than that newest time minus
`ANDROID_SMS_OVERLAP_DAYS` (default 7). Large files are streamed and
checkpointed inside the file.

## Setup

1. Install SMS Backup & Restore on the phone. In **Set up a backup** choose
   messages (include MMS) and calls, pick a local folder or cloud destination,
   and enable **Scheduled backups** (e.g. daily). Choose XML (the default) and
   do not enable the app's encryption.
2. Sync that folder to the Throughline machine (for Syncthing: share the
   folder from the phone; Drive/Dropbox: use the desktop client or rclone).
3. Set `ANDROID_SMS_DIR` to the synced folder. Old backups can be deleted
   by the app's retention setting; that does not affect imported data.

| Environment variable | Default | Meaning |
|---|---|---|
| `THROUGHLINE_URL` / `THROUGHLINE_TOKEN` | – | Core origin and token |
| `ANDROID_SMS_DIR` | – (required) | Folder holding `sms-*.xml` / `calls-*.xml` |
| `ANDROID_SMS_OWNER_NUMBER` | – | Your number, authoring sent messages and placed calls |
| `ANDROID_SMS_COUNTRY_CODE` | – | e.g. `1`: 10-digit national numbers become `+1…` so they match E.164 forms |
| `ANDROID_SMS_OVERLAP_DAYS` | `7` | Re-read window in new backups; `all` reads every record of every new file (e.g. importing an old phone's backup) |
| `ANDROID_SMS_BATCH_SIZE` | `2000` | Records per checkpoint |

## Run

```sh
uv run --package throughline-android-sms throughline-android-sms backfill --dry-run
uv run --package throughline-android-sms throughline-android-sms backfill
uv run --package throughline-android-sms throughline-android-sms sync          # hourly
```

## Limitations

- Freshness is bounded by the backup schedule (daily backups mean messages
  arrive up to a day late). Messages deleted on the phone before a backup ran
  are never seen.
- Contact names from the backups are not imported (they change between
  backups); resolve people through phone numbers.
- A backup that fails to parse (usually still being written or synced) is
  logged and retried next run. Encrypted or zipped backups are not supported.
- RCS chats are only included as far as the app backs them up (it reads the
  Android SMS/MMS provider).
- Numbers without a country code and with one stay separate unless
  `ANDROID_SMS_COUNTRY_CODE` is set.
