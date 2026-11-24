# throughline-goodreads

Live Goodreads connector: the owner's shelves, read dates, ratings and reviews,
read from Goodreads' shelf RSS feeds. Goodreads closed its public API in 2020, but
every shelf still publishes
`https://www.goodreads.com/review/list_rss/<user_id>?shelf=<shelf>`, updated as
soon as a book is shelved, rated or reviewed.

## What it captures

| Channel | Kind | What |
| --- | --- | --- |
| `goodreads.books` | reference | One entry per book on the owner's shelves: "Example Book by Some Author (2001) - on read, favorites, rated 4/5". |
| `goodreads.reading` | life event | The owner's reading: `finished` (read shelf, at the read date, with rating and review text), `progress` ("Started reading", currently-reading shelf) and `watchlist_add` (want to read). |

Fields use the shared media vocabulary (`service`=`goodreads`, `source_kind`,
`event_type`, `media_type`=`book`, `title`, `creator`, `creators`, `year`,
`is_finished`, `rating` (0-10 scale, i.e. stars x 2), `rating_raw` (`4/5`),
`account`, `added_at`, `native_id` (`goodreads:book:<id>`), `native_ids` (book ID,
review ID, ISBN), `url`, `timing_basis`, `time_precision`, `source`=`rss`,
`author_kind`=`owner`, `mapping_revision`) plus `shelf`, `shelves` (custom shelves
too), `read_at`, `started_at`, `review`, `num_pages`, `average_rating`, `isbn`.

Messages are authored by the owner's Goodreads account (`system: goodreads`,
`kind: user_id`). The book entry links `saved` to a `work` entity
(`goodreads:book:<id>`), a finished event links `consumed` to it, and the work
links `created_by` an `author` entity. Reading events are threaded per book, so a
book's started and finished events sit together.

## Setup

1. Find your numeric user ID: it is the number in your profile URL
   (`https://www.goodreads.com/user/show/12345678-name` -> `12345678`).
2. Public profiles need nothing else. For a private profile, open a shelf, copy the
   RSS link at the bottom of the page and set `GOODREADS_FEED_KEY` to its `key=`
   value (treated as a secret).

```sh
export GOODREADS_USER_ID=12345678
throughline-goodreads backfill
throughline-goodreads sync --once      # every few hours is plenty
```

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `GOODREADS_USER_ID` | yes | | Numeric user ID |
| `GOODREADS_FEED_KEY` | no (secret) | | `key=` of a private profile's RSS link |
| `GOODREADS_SHELVES` | no | `read,currently-reading,to-read` | Exclusive shelves to read |
| `GOODREADS_MAX_PAGES` | no | `50` | Most RSS pages per shelf per run |
| `GOODREADS_FEED_CAP` | no | `100` | Items per RSS page |
| `GOODREADS_BATCH_SIZE` | no | `100` | Books per checkpoint |
| `GOODREADS_REQUEST_DELAY_S` | no | `1` | Pause between requests |
| `GOODREADS_FEED_URL` | no | Goodreads | Base of the RSS URL (for a proxy or mirror) |
| `GOODREADS_TEST_TRANSPORT` | no | | Testing only: `module:factory` returning an httpx transport |

## How sync works

Each shelf feed is read newest-added first, page by page. A backfill reads every
page; a sync stops at the first page that reaches the newest book delivered
before. Books are then delivered oldest first with a checkpoint every batch, and
the cursor keeps the newest `added` date per shelf. Stable IDs (book ID, review ID)
make re-reads no-ops, and changed ratings, shelves or reviews arrive as new
versions.

## Coverage and limitations

- **Feeds may be capped.** If a feed ignores the `page` parameter (it repeats page
  1) or hits `GOODREADS_MAX_PAGES`, the first run reports `best_effort` coverage
  with reason `source_cap` and the oldest date available. On later runs, if a
  capped feed no longer reaches the newest book delivered before, the connector
  reports a `partial` coverage gap (`source_cap`) for the window in between.
- Goodreads keeps only the **latest** read date of a book: re-reads replace the
  earlier date, so earlier reads are not recoverable.
- The feeds carry no reading-progress updates (page or percent) and the start date
  is often missing; then "started" uses the date the book was shelved, stated in
  `timing_basis`.
- A book's `finished` event appears when it reaches the read shelf; a book later
  moved off it keeps its event. Books removed from all shelves are not deleted.
- Sync only re-reads the newest pages, so an edit to an old book's rating or review
  is picked up by the next `backfill` rather than by `sync`.
