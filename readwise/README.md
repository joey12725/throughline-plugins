# throughline-readwise

Live Readwise connector: every highlight and note the owner keeps in Readwise
(Kindle, Apple Books, Instapaper, Reader, podcasts, tweets, ...) and the documents
they save to Readwise Reader, with reading progress. It polls the Readwise export
API (v2) and the Reader API (v3); no manual export is involved.

## What it captures

| Channel | Kind | What |
| --- | --- | --- |
| `readwise.highlights` | life event, embedded | One message per highlight, plus one per owner note (highlight notes and book-level document notes). One thread per book/article. |
| `readwise.books` | reference | One entry per book, article, tweet thread, podcast or supplemental source the owner has highlights from. |
| `readwise.reader` | life event | One message per document saved to Reader (`event_type: saved`), updated as progress and location change. |

### Whose words are whose

- **Highlights** are passages written by the book's author and selected by the
  owner. The message author is the book's author (`system: readwise`, `kind: author`,
  `value: <author name>`), `author_kind: other`, `selected_by: owner`,
  `entry_type: highlight`. Content is the highlighted text.
- **Notes** the owner wrote (a highlight's `note`, a book's `document_note`) are
  separate messages in the same book thread: `entry_type: note`, `author_kind: owner`,
  authored by the owner account (`READWISE_OWNER_IDENTIFIER`). External IDs
  `readwise:highlight:<id>:note` and `readwise:book:<user_book_id>:note`. Each note
  links `annotates` to its highlight message (or to the book's `work` entity).
  Notes keep `event_type: highlight` (they belong to the highlighting activity);
  filter on `entry_type` to separate them.
- **Reader summaries** can be written by Ghostreader (an AI model) or be the
  publisher's description. They never appear in `content`; they are kept in the
  `summary` field with `summary_author_kind: agent`. Treat them as model-generated,
  never as the owner's words.

### Fields

Shared names follow the unified `media.consumption` vocabulary, so "what did I read"
looks alike across sources: `service` (`readwise`), `source_kind`
(`highlight` / `highlight_note` / `document_note` / `book` / `reader_document`),
`event_type` (`highlight` / `library_added` / `saved`), `media_type`, `title`,
`creator`, `creators`, `native_id`, `native_ids`, `url`, `detail`, `timing_basis`,
`time_precision`, `source` (`api`), `author_kind`, `mapping_revision`.

- `readwise.highlights` also: `entry_type`, `selected_by`, `book_id`, `highlight_id`,
  `origin` (Readwise's `source`, e.g. `kindle`), `location`, `location_type`, `color`,
  `tags`, `is_favorite`, `is_discard`, `has_note`.
- `readwise.books` also: `book_id`, `origin`, `category`, `tags`, `highlight_count`,
  `has_document_note`. `media_type`: books→`book`, articles→`article`,
  tweets→`tweet`, podcasts→`podcast`, supplementals→`other`.
- `readwise.reader` also: `progress` (`reading_progress`, 0–1), `is_finished`,
  `added_at`, `location` (new/later/shortlist/archive/feed), `category`, `site_name`,
  `word_count`, `tags`, `first_opened_at`, `last_opened_at`, `summary`,
  `summary_author_kind`. `media_type`: article/rss→`article`, email→`email`,
  pdf→`document`, epub→`book`, tweet→`tweet`, video→`video`.
  `is_finished` is true when the document is archived or `reading_progress ≥ 0.99`.

Timing: a highlight is dated `highlighted_at` (falling back to `created_at`); a
highlight note shares its highlight's time (Readwise does not date notes); a
document note and a book entry are dated by the book's earliest highlight (Readwise
has no book-level timestamp), so they stay stable across runs. Reader documents are
dated `saved_at` (falling back to `created_at`).

Links: highlight `part_of` the book's `work` entity (`readwise:book:<id>`), work
`created_by` an `author` entity, book entry `describes` the work, Reader document
`saved` its own work entity (`readwise:reader:<id>`).

## Setup

1. Get an access token at <https://readwise.io/access_token>. The same token works
   for the Reader API if you have a Reader subscription.
2. Set the environment and run:

```sh
export READWISE_TOKEN=...
throughline-readwise backfill       # full history
throughline-readwise sync --once    # incremental, e.g. from an hourly timer
```

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `READWISE_TOKEN` | yes (secret) | | Readwise access token (sent as `Authorization: Token …`) |
| `READWISE_READER` | no | `true` | Also import Reader documents; set `false` without a Reader subscription |
| `READWISE_READER_FEED` | no | `false` | Include Reader feed items (RSS/email the owner never saved) |
| `READWISE_PAGE_SIZE` | no | `100` | Books/documents per checkpointed batch |
| `READWISE_REQUEST_DELAY_S` | no | `3` | Pause between requests (both list endpoints allow 20 requests/minute; `429 Retry-After` is honoured) |
| `READWISE_OWNER_IDENTIFIER` | no | `readwise:account:owner` | `system:kind:value` author of the owner's notes and saves (e.g. `email:address:you@example.com` to join your identity) |
| `READWISE_OWNER_NAME` | no | | Display name on the owner's messages |
| `READWISE_TEST_TRANSPORT` | no | | Testing only: `module:factory` returning an httpx transport |

Plus the standard `THROUGHLINE_URL`, `THROUGHLINE_TOKEN` and, under a timer,
`THROUGHLINE_INTERVAL_S`. Dependencies: the SDK and httpx only.

## How sync works

Both endpoints page with an opaque `pageCursor` and filter with `updatedAfter`, but
pages are not ordered by update time. So the high-water mark (the newest
`updated_at` seen) advances only when a pass has read its last page. Mid-pass the
cursor stores the pass's `updatedAfter`, the next `pageCursor` and the running
maximum, so a crash resumes at the page it was on (a partly delivered page is read
again; stable external IDs deduplicate it). Each pass asks for updates since the
high-water mark minus 10 minutes, so items edited while a pass was running are not
missed.

An incremental export page carries only the highlights that changed, so the books it
names are re-read whole (`ids=`) before mapping: book entries, highlight counts and
document-note dates come out exactly as in a full export. Edited highlights, notes,
tags, favourites and discards arrive as new versions of the same message.

## Limitations

- **Deletions are not propagated.** Neither API reports deleted highlights or
  Reader documents; messages already imported stay. Books flagged `is_deleted` in
  the export are skipped.
- **Discarded highlights** (`is_discard`, from Daily Review) are imported, with
  `is_discard: true`, because a highlight discarded after import could not be
  removed anyway; filter on `is_discard = false` for the curated set.
- A book with no highlights has nothing to date it by and is not imported (its
  document note neither).
- Highlight notes are not timestamped by Readwise; they take their highlight's time.
- Reader child records (`parent_id` set: Reader highlights and notes) are skipped
  in `readwise.reader`; the export API delivers them, with their notes, in
  `readwise.highlights`.
- Reader feed items are skipped unless `READWISE_READER_FEED=true`; once the owner
  moves one to the library its update brings it in.
- Readwise and Reader IDs are not cross-referenced by the APIs, so a Reader document
  and the export "book" holding its highlights are separate `work` entities.
- Field shapes follow Readwise's published API documentation; the tests run against
  invented recordings, not a live account.
