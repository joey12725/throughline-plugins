# throughline-letterboxd

Live Letterboxd connector: the owner's film diary, ratings, likes and reviews,
read from the member's public RSS feed. Letterboxd's API is not open to personal
use, but every profile publishes `https://letterboxd.com/<user>/rss/`, which is
updated as soon as an entry is logged.

## What it captures

| Channel | Kind | What |
| --- | --- | --- |
| `letterboxd.diary` | life event, embedded | One message per diary entry or review: "Watched Example Film (2019) - rated ★★★½ (3.5/5), liked", followed by the review text when there is one. |

Fields use the shared media vocabulary (`service`=`letterboxd`, `source_kind`
`diary`/`review`, `event_type` `play` (watched) or `reviewed` (a review without a
diary date), `media_type`=`movie`, `title`, `year`, `rating` (0-10 scale, i.e.
stars x 2), `rating_raw` (`3.5/5`), `account`, `native_id` (`tmdb:movie:<id>`),
`native_ids`, `url`, `timing_basis`, `time_precision`, `source`=`rss`,
`author_kind`=`owner`, `mapping_revision`) plus `watched_date`, `logged_at`,
`is_rewatch`, `liked`, `review`, `has_spoilers`.

`happened_at` is the diary's watched date. Letterboxd records no time of day, so
it is noon in the time zone of the log time (`time_precision: day`); the exact
log time is in `logged_at`. Entries without a watched date use the publish time.
Each watched entry links `consumed` to a `work` entity keyed by TMDB ID
(`tmdb:movie:<id>`), and `liked` when the film is liked. Threads are per day.
List posts in the feed are skipped.

## Setup

No credentials. The profile must be public.

```sh
export LETTERBOXD_USER=your_user_name
throughline-letterboxd backfill
throughline-letterboxd sync --once     # hourly is plenty
```

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `LETTERBOXD_USER` | yes | | Letterboxd user name |
| `LETTERBOXD_FEED_URL` | no | `https://letterboxd.com/<user>/rss/` | Override the feed URL |
| `LETTERBOXD_FEED_CAP` | no | `50` | Entries the feed holds at most |
| `LETTERBOXD_BATCH_SIZE` | no | `50` | Entries per checkpoint |
| `LETTERBOXD_TEST_TRANSPORT` | no | | Testing only: `module:factory` returning an httpx transport |

## Coverage and limitations

- **The feed only holds the newest ~50 entries.** The first run imports those and
  reports `best_effort` coverage with reason `source_cap` and the oldest date
  available; older history cannot be fetched live. (A one-time Letterboxd data
  export is the only way to get it; this connector does not import exports.)
- If more than a feed's worth of entries are logged between two runs (for example
  during a long outage), the feed no longer reaches the newest entry delivered
  before. The connector then reports a `partial` coverage gap with reason
  `source_cap` from that entry to the oldest one in the feed, so recall over that
  window says entries may be missing. Run it at least daily to avoid this.
- Every run re-reads the whole feed, so edited ratings and reviews arrive as new
  versions while they are still in the feed. Deleted entries are not removed.
- Likes of films without a diary entry, watchlist and lists are not in the feed.
