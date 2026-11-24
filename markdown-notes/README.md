# Markdown notes

Reads one or more folders of Markdown — Obsidian vaults, Logseq graphs or any
plain folder of `.md` files — and keeps every note as a versioned page in
Throughline. It is a live connector: point it at the folder your editor (or
Obsidian Sync, iCloud, Syncthing, Dropbox, git) keeps up to date and run it on
a timer. Nothing is exported or imported by hand, and the plugin never writes
to your notes.

## What it captures

Channel `markdown_notes.notes` (`record_kind: page_revision`, reference,
embedded). One record per note file, external id `<root label>:<path>`:

- **Revisions.** A new immutable version whenever the file's bytes change
  (SHA-256). Touching a file without changing it creates nothing.
- **Deletions.** When a file disappears a tombstone version is recorded
  (`deleted: true`). When a new file has exactly the same bytes, the
  tombstone names it in `renamed_to` (a rename or move).
- **Frontmatter.** YAML frontmatter is parsed: `title`, `tags`/`tag`,
  `aliases`/`alias`, `created` and `modified`/`updated` become fields; the
  full frontmatter is kept as JSON. Logseq page properties (`title::`,
  `tags::`, `alias::`) are read the same way.
- **Links.** `[[wikilinks]]` (with `|alias`, `#heading`, `^block`),
  `![[embeds]]` and relative Markdown links `[text](other.md)` that resolve
  to another note become `links` links between the two notes (resolution
  follows Obsidian/Logseq: exact path, path suffix, then file name; Logseq
  namespaces `a___b.md` / `a%2Fb.md` are page `a/b`). Code blocks are
  ignored.
- **Tags.** Frontmatter tags and inline `#tags` / `#[[multi word]]` become
  `about` links to `tag` entities (`external_ref` `tag:<lowercased>`), shared
  across roots.
- **Daily notes.** Obsidian daily notes (folder and format from
  `.obsidian/daily-notes.json`, moment.js tokens), Logseq journals
  (`journals/yyyy_MM_dd.md`, or the `:journal/file-name-format` in
  `logseq/config.edn`) and any file named like `2025-03-14`, `2025_03_14` or
  `2025.03.14` get `is_daily`, `daily_date` and a day-precision `subject`, so
  "what did I write about last March" finds them by the day they describe
  rather than the day they were edited. For other notes, a frontmatter
  `date`/`event_date` (configurable) becomes the subject.

`happened_at` is the file modification time of the revision. Content is the
title followed by the Markdown body (frontmatter removed); passages are taken
from `/content/title` and `/content/body`.

| Field | Type | Meaning |
| --- | --- | --- |
| vault, vault_format | text | Root label; `obsidian`, `logseq` or `markdown` |
| path, title | text | Path inside the root; title |
| aliases, tags, frontmatter, properties | json | Parsed metadata |
| link_targets | json | Link targets as written |
| is_daily, daily_date | boolean, text | Daily/journal note and its day |
| created, modified, mtime | timestamp | Frontmatter dates; file mtime |
| size_bytes, content_sha256 | integer, text | Revision size and hash |
| content_truncated, frontmatter_error | boolean | Body cut at the cap; invalid YAML |
| deleted, renamed_to | boolean, text | Tombstone and rename target |
| author_kind | text | Always `owner` |

## Setup

```sh
export MARKDOWN_NOTES_ROOTS="vault=$HOME/Obsidian/Main,graph=$HOME/logseq,$HOME/notes"
throughline token create markdown-notes --kind plugin --plugin markdown_notes --json
uv run throughline-markdown-notes backfill      # first import
uv run throughline-markdown-notes sync --once   # on a timer (e.g. every 15 minutes)
```

Ignored by default: every dot-folder and dot-file (`.obsidian/`, `.trash/`,
`.git/`, ...), `logseq/bak/`, `logseq/.recycle/`, `logseq/version-files/`
and `node_modules/`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `MARKDOWN_NOTES_ROOTS` | required | `label=path` or bare paths, comma separated (label defaults to the folder name; keep labels stable, they are part of the ids) |
| `MARKDOWN_NOTES_IGNORE` | | Extra ignore globs relative to each root (`Templates/**,*.excalidraw.md`) |
| `MARKDOWN_NOTES_EXTENSIONS` | `.md,.markdown` | Note file extensions |
| `MARKDOWN_NOTES_INCLUDE_HIDDEN` | `false` | Also read dot-files/folders (default ignore globs still apply) |
| `MARKDOWN_NOTES_MAX_BYTES` | `2000000` | Larger files are skipped and counted |
| `MARKDOWN_NOTES_MAX_CHARS` | `200000` | Body characters kept per revision |
| `MARKDOWN_NOTES_SUBJECT_KEYS` | `date,event_date` | Frontmatter keys used as the subject of non-daily notes (empty to disable) |
| `MARKDOWN_NOTES_TZ` | system zone | Zone for daily-note days and naive frontmatter dates |
| `MARKDOWN_NOTES_CHECKPOINT_EVERY` | `200` | Changed notes per checkpoint |

## How it syncs

Each run stats every file (no reads), compares modification time and size
with the cursor, and reads and hashes only files that moved. The cursor holds
`{path: [mtime_ns, size, sha256[:16]]}` per root, gzip+base64 packed (a
10,000-note vault is a few hundred kilobytes). Note-to-note links are sent
after the root's notes are stored, so both ends always exist; notes whose
links are still owed are kept in the cursor and replayed after a crash.

A root that is missing, unreadable or suddenly empty (an unmounted drive, a
sync client mid-reset) is reported as `partial` coverage and **no deletions
are recorded** for it; when every root is unreadable the run fails.

## Dependencies

`pyyaml` — frontmatter is real YAML (lists, dates, quoting, block scalars);
a hand-rolled subset parser would mis-read common vaults. Everything else is
the standard library.

## Limitations

- Links and tag links are only added: removing a `[[link]]` or `#tag` from a
  note does not retire the earlier link (the note's current `link_targets`
  and `tags` fields are always accurate).
- A rename is a tombstone plus a new note (linked by `renamed_to` only when
  the bytes are identical); a renamed-and-edited file is not matched.
- Attachments (images, PDFs, canvases) and Excalidraw drawings are not read.
- Block references, embeds of headings and Logseq block properties are kept
  as text; Logseq journal links written as dates (`[[Mar 14th, 2025]]`) are
  not resolved to the journal file.
- Deletion times are the time the plugin noticed, not when the file was
  removed.
