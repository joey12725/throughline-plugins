# Apple Notes

Reads macOS Apple Notes from the local Notes database
(`~/Library/Group Containers/group.com.apple.notes/NoteStore.sqlite`) and
keeps every note as a versioned page in Throughline. It is a live connector:
Notes keeps that database current (including notes synced from your iPhone
through iCloud), and the plugin re-reads it on a timer. It never opens the
live database: each run copies `NoteStore.sqlite` and its `-wal`/`-shm`
files to a temporary folder and reads the copy.

Runs on the Mac. Standard library only — the note bodies are gzipped
protobuf and are decoded by a ~150-line reader in `proto.py`.

## What it captures

Channel `apple_notes.notes` (`record_kind: page_revision`, reference,
embedded). One record per note, external id = the note's Core Data
`ZIDENTIFIER` (a UUID, the same on every Mac signed in to the account).

- **Revisions.** A new immutable version whenever a note's modification date
  changes (or it moves to another folder). `happened_at` is Apple's
  modification time.
- **Body.** The text is rendered as readable Markdown: headings, bulleted,
  dashed and numbered lists with indentation, checklists (`- [x]`),
  monospaced blocks, and placeholders for attachments (`[image]`, `[table]`,
  `[drawing]`, `[pdf]`, ...). Links, attachment types and checklist counts
  are fields.
- **Folders and accounts.** Folder name and full folder path, account name
  (iCloud, On My Mac, ...) and identifiers; each note gets a `part_of` link to
  a `notes_folder` entity.
- **Trash.** Notes in *Recently Deleted* are kept with `in_trash: true`
  (`APPLE_NOTES_INCLUDE_TRASH=false` skips them). When a note is purged from
  the store a tombstone version is recorded (`deleted: true`).
- **Locked notes** (password protected) are skipped and counted in the
  run's coverage detail (`locked_skipped`). An earlier unlocked version, if
  any, stays as it was.

| Field | Type | Meaning |
| --- | --- | --- |
| title, folder, folder_path, folder_id | text | Note title and folder |
| account, account_id | text | Account name and identifier |
| created, modified | timestamp | Apple creation/modification times |
| snippet | text | Apple's preview snippet |
| in_trash, is_pinned, is_shared | boolean | State flags |
| links, attachments | json | Body link URLs; attachment UTIs |
| checklist_total, checklist_done | integer | Checklist counts |
| content_sha256, content_truncated | text, boolean | Rendered body hash; cut at the cap |
| deleted | boolean | Tombstone |
| author_kind | text | `owner` (shared notes may contain collaborators' edits) |

## Setup

1. Give the program that runs the plugin (Terminal, iTerm, or the `uv`/Python
   binary used by launchd) **Full Disk Access** in System Settings → Privacy &
   Security; the Notes group container is otherwise unreadable.
2. Create a plugin token and run it:

```sh
throughline token create apple-notes --kind plugin --plugin apple_notes --json
uv run throughline-apple-notes backfill
uv run throughline-apple-notes sync --once   # from launchd every 15 minutes
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `APPLE_NOTES_DB` | `~/Library/Group Containers/group.com.apple.notes/NoteStore.sqlite` | Database to copy and read |
| `APPLE_NOTES_INCLUDE_TRASH` | `true` | Keep notes in Recently Deleted |
| `APPLE_NOTES_MAX_CHARS` | `200000` | Body characters kept per revision |
| `APPLE_NOTES_CHECKPOINT_EVERY` | `200` | Changed notes per checkpoint |

The cursor is `{note id: [modification time, body hash, title, folder]}`,
gzip+base64 packed. Each run reads the note table (cheap) and decodes only
notes whose modification date or folder changed.

## Limitations

- Built against the documented Core Data schema of macOS 10.13 – 15
  (`ZICCLOUDSYNCINGOBJECT`, `ZICNOTEDATA`) with column names detected at
  run time; it was tested against a synthetic store, not a live Mac. Notes
  from before macOS 10.11 (the legacy `ZNOTEBODY` HTML store) are not read.
- Locked notes cannot be read without the password and are skipped.
- Attachment contents (images, scans, PDFs, drawings) and table cell text
  are not extracted — only a placeholder and the attachment type.
- Inline styling (bold, italic, colours, fonts) is dropped.
- Deletion times are when the plugin noticed the purge.
