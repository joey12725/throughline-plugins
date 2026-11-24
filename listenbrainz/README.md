# throughline-listenbrainz

A live Throughline source for [ListenBrainz](https://listenbrainz.org) (or a
self-hosted ListenBrainz server). It polls the public API for your full listen
history and your loved/hated recordings. No data export is needed.

## Channels

| Channel | Kind | What it holds |
| --- | --- | --- |
| `listenbrainz.listens` | `life_event`, not embedded | Every listen, oldest first. Each day of listens is one thread (`ListenBrainz YYYY-MM-DD`). |
| `listenbrainz.feedback` | `reference` (`is_reference`) | Recordings you loved (score 1, `event_type` `liked`) or hated (score -1, `event_type` `hated`). |

Both channels use the shared media field names (the same ones as `lastfm` and
`media.consumption`), so one query works across music sources:

`service` (`listenbrainz`), `source_kind` (`listen` / `recording_feedback`),
`event_type` (`play` / `liked` / `hated`), `media_type` (`track`), `title`,
`creator`, `creators` (credited artists from the MusicBrainz mapping when there
is one), `album`, `duration_s`, `client` (media player), `account`, `native_id`
(the recording MSID, or the MBID if there is no MSID), `native_ids` (recording, release and artist MBIDs,
MSID, origin URL), `url` (origin URL, or the MusicBrainz recording page),
`detail` (ISRC, Spotify ID, track number, client versions, ...), `timing_basis`,
`time_precision`, `source` (`api`), `author_kind` (always `owner`) and
`mapping_revision`.

Each channel also has its own fields:

- listens: `music_service`, `submission_client`
- feedback: `feedback_score` (1 or -1)

`author` is your ListenBrainz account (`system: "listenbrainz"`).

**Links.** A listen writes `consumed` to a work; feedback writes `liked` or
`hated` to a work. Each work then gets `created_by` links to each credited
artist and a `part_of` link to its album. Entity refs are
`musicbrainz:recording:<mbid>`, `musicbrainz:artist:<mbid>` and
`musicbrainz:release:<mbid>` when MBIDs are known, which are the same refs the
Last.fm plugin uses, so the same track joins across both sources. When there
is no MBID, refs fall back to `listenbrainz:track:<artist>|<title>`,
`listenbrainz:artist:<name>` and `listenbrainz:album:<artist>|<album>`
(case-folded).

**External IDs** are `listenbrainz:<user>:listen:<listened_at>:<recording_msid>`
for listens and `listenbrainz:<user>:feedback:msid:<msid>` (or `mbid:<mbid>`)
for feedback.

## Setup

1. Copy your user token from <https://listenbrainz.org/settings/> (or from the
   same page on your own server).
2. Create a scoped plugin token:
   `throughline token create listenbrainz --kind plugin --plugin listenbrainz --json`.
3. Set the environment and run:

```sh
export LISTENBRAINZ_USER=your_name LISTENBRAINZ_TOKEN=...
throughline-listenbrainz backfill          # full history, resumable
throughline-listenbrainz sync --once       # from a timer, e.g. every 15 min
```

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `LISTENBRAINZ_TOKEN` | yes (secret) | | User token, sent as `Authorization: Token ...`. It is validated with `/1/validate-token` at the start of every run. |
| `LISTENBRAINZ_USER` | yes | | The ListenBrainz user name whose listens are read. |
| `LISTENBRAINZ_API_URL` | no | `https://api.listenbrainz.org` | API root of a self-hosted server. |
| `LISTENBRAINZ_PAGE_SIZE` | no | `1000` | Listens or feedback per request (max 1000). There is one checkpoint per page. |
| `LISTENBRAINZ_LOOKBACK_S` | no | `86400` | How far before the cursor each run starts reading again, to pick up listens submitted late with older timestamps. |
| `LISTENBRAINZ_REQUEST_DELAY_S` | no | `0.2` | Pause between requests. |
| `LISTENBRAINZ_FEEDBACK` | no | `true` | Set to `false` to skip the feedback channel. |
| `LISTENBRAINZ_TEST_TRANSPORT` | no | | Testing only: `module:factory` that returns an httpx transport. |

The plugin has no dependencies beyond the SDK and httpx. Requests are retried
on network errors, 429 and 5xx responses, waiting as long as `Retry-After` or
`X-RateLimit-Reset-In` says. When `X-RateLimit-Remaining` reaches 0, the plugin
pauses before its next request. An invalid token, a 401, an unknown user (404)
or an unreachable server makes the run fail, so source health marks the
source as failing.

## How syncing works

Listens are read forward in time with `min_ts` only. Per the API docs, the
server rejects requests that set both `min_ts` and `max_ts`. With only
`min_ts`, the server's listen store returns the `count` listens just after
that time, newest first, and the plugin reverses each page. The cursor
(`listens_ts`) means "every listen at or before this second has been
delivered", and it only moves forward.

`min_ts` is exclusive, so a full page can end partway through the listens of
one second. When that happens, the plugin holds back that last second and
reads it again on the next page. Only one page is held in memory, and a crash
resumes from the last checkpoint. Listens that are read again keep the same
external IDs, so they deduplicate.

Feedback is read newest first with `offset`/`count`. A sync stops once a page
reaches feedback older than the stored high-water mark.

## Limitations

- **Deletions are not applied here.** A listen you delete on ListenBrainz
  stays in Throughline. Feedback you remove (score set back to 0) also stays,
  because the API no longer returns it. If you change a love to a hate, the
  same record is updated (new version), because the external ID comes from the
  recording, not the score.
- **Late listens older than the lookback are missed.** If you import old
  history (for example from Last.fm) after the cursor has passed that time,
  either raise `LISTENBRAINZ_LOOKBACK_S` for one run or run `backfill` again,
  which re-reads everything and deduplicates.
- **Full-history paging.** The ListenBrainz docs advise against paginating a
  whole history through the listens endpoint and point to their export
  instead. This plugin pages politely: up to 1000 listens per request, a delay
  between requests, and it honours the rate-limit headers. A backfill of a very
  large history takes about one request per 1000 listens.
- **Timestamps** are what the submitting client sent (`listened_at`, usually
  when playback started), at one-second resolution. If more than `page_size`
  listens share one second, some may be skipped. A warning is logged when that
  happens.
- **MusicBrainz mapping** (`mbid_mapping`) is filled in by the server
  asynchronously. A listen read before it was mapped keeps only the MBIDs its
  client submitted (if any), until a later re-read updates it.
- **Now playing** is not read. Only submitted listens are.
- **Feedback metadata** depends on `metadata=true` resolving the recording.
  Feedback on a recording the server can't resolve becomes "Unknown track".
- The API response shapes come from the ListenBrainz documentation and server
  source. The tests run against an invented fake that follows those rules, not
  against a live account.

## Tests

```sh
THROUGHLINE_DEV=1 uv run pytest -q plugins/listenbrainz
```

`tests/listenbrainz_fake.py` follows the real API's rules (exclusive
`min_ts`/`max_ts`, newest-first pages, `count` capped at 1000, feedback
paged by offset, 401 for a bad token). It serves invented recordings from
`tests/recorded/`. The contract-kit test runs the plugin in a subprocess with
`LISTENBRAINZ_TEST_TRANSPORT=listenbrainz_fake:transport`.
