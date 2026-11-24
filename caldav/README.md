# Throughline CalDAV plugin

Calendars from any CalDAV server (iCloud, Fastmail, Nextcloud, Radicale,
Baïkal, SOGo, Zimbra, ...) and plain ICS subscription URLs (a club's public
calendar, a "secret address in iCal format", a webcal:// link), in one
plugin. Records have the same shape as the Google Calendar connector
(`plugins/calendar`) and the Microsoft Graph connector (`plugins/msgraph`).

## What it captures

| Channel | One message per | Record time (`happened_at`) | Subject |
|---|---|---|---|
| `caldav.events` | event occurrence; recurring series expanded inside the window | the occurrence start (`ended_at` = its end) | the occurrence's start and end |
| `caldav.changes` | lifecycle step: `created`, `changed`, `cancelled`, `removed`, `restored` | the event's `CREATED` / `LAST-MODIFIED` time; for changes found by snapshot diff, the fetch time | the event's start and end after the step |

Fields are the same names as `plugins/calendar`: account, calendar id and
name, event id (`<UID>` for a single event, `<UID>/<RECURRENCE-ID in UTC>` for
an occurrence), series id (the UID of a recurring event), iCalendar UID,
title, status (`confirmed`, `tentative`, `cancelled`, `removed`), all-day,
start time zone (TZID), original start of a moved instance, place, description,
organizer email and name, attendees (email, name, response normalized to
`accepted`/`declined`/`tentative`/`needsAction`, optional, resource, self),
attendee count, `my_response` (from `CALDAV_OWNER_ADDRESSES`), conference URL
(`X-GOOGLE-CONFERENCE`, Teams/Skype `X-MICROSOFT-*` properties), `URL`,
visibility (CLASS), transparency (TRANSP), created/updated, `observed_at`,
`mapping_revision`. Change records add `change`, `changed_fields` (which of
title, event_start, event_end, place, description, attendees, responses,
recurrence differ from the previous snapshot), event start/end, recurrence
lines, organizer email, `time_precision` (`source` or `observed`) and
`occurrence_count`.

The organizer is the author (`system` `email`, `kind` `email`). All-day
events start at UTC midnight with `day` precision; timed events keep their
zone's offset; floating (zone-less) times use `CALDAV_DEFAULT_TZ`.

Lifecycle rules (as in `plugins/calendar`):

- `created` once per event at its `CREATED` time (events without `CREATED`,
  common in ICS feeds, get none).
- `changed` at `LAST-MODIFIED` when tracked fields differ from the last
  snapshot; at the fetch time (`time_precision=observed`) when the time did
  not move. An event first seen more than 60 s after creation gets one
  `changed` record with `changed_fields` null, under its own id
  (`...:changed:first-seen:<time>`), so it never overwrites a diff-based
  record of the same change after a cursor reset.
- `cancelled` for `STATUS:CANCELLED`; the occurrence gets status `cancelled`.
- `removed`: an occurrence that a complete read no longer returns (deleted
  event, new EXDATE), or a deleted resource, gets status `removed` and the
  event a `removed` record (one per series per run, listing the dates).
  `restored` when it comes back.
- An event moved outside the window is not removed: its resource still
  exists, so its occurrence is updated to the new time.
- A complete answer with no event in it while live occurrences are known
  removes nothing (a misread answer is likelier than an emptied calendar),
  and neither does a full read in which any object could not be parsed (an
  HTML error page served with status 200, a broken event): the stored state
  is kept and the next run reads everything again.

Every message links (`method` `source`) to a `calendar_event` entity
`ical:<UID>` (`part_of` for occurrences, `about` for changes), the same
entity the Google and Outlook connectors use, so one meeting on several
calendars or providers shares it. `CALDAV_LINKS=false` turns links off.

## Change detection

- **Collections:** discovery (`current-user-principal` ->
  `calendar-home-set` -> PROPFIND depth 1) returns each calendar's `getctag`
  and/or `sync-token`. When it is unchanged and the window is the same as at
  the last complete read, the calendar is skipped with no REPORT at all.
- **Resources:** otherwise, when the window is unchanged, a
  `calendar-query` asking only for `getetag` lists the events in the window;
  only resources whose etag changed are downloaded (`calendar-multiget`).
  Resources that left the listing are fetched by href to tell "moved away"
  from "deleted": only an explicit 404 counts as deleted; an href the server
  does not mention stays as known. A listing truncated by the server (507) or
  carrying any non-200 entry judges nothing as gone and keeps no ctag, so the
  next run reads again. Hrefs are compared percent-decoded. The server
  time-range is padded by one day on each side (servers judge floating and
  all-day events in UTC); expansion and removal use the exact window.
- **New window:** the rolling window moves once a day; then (and on the first
  run) one `calendar-query` with `calendar-data` reads the whole window, so
  occurrences of unchanged series that newly fall inside it are picked up.
- **ICS feeds:** `If-None-Match` / `If-Modified-Since`; a 304 skips the feed.
  Otherwise the whole feed is parsed; events whose UID disappeared are removed.

`DTSTAMP` is ignored everywhere (many feeds regenerate it on every request),
so an unchanged event never becomes a new version.

## Setup

1. Get an app-specific password:

   | Server | `CALDAV_URL` | Password |
   |---|---|---|
   | iCloud | `https://caldav.icloud.com/` | appleid.apple.com, Sign-In and Security, App-Specific Passwords; username is your Apple ID email |
   | Fastmail | `https://caldav.fastmail.com/` | Settings, Privacy & Security, App passwords (CalDAV access) |
   | Nextcloud | `https://cloud.example.com/remote.php/dav/` | Personal settings, Security, Devices & sessions, create app password |
   | Radicale | `https://radicale.example.com/<user>/` | Your Radicale password |

   Any URL works: the server root (with `/.well-known/caldav`), a principal,
   a calendar home, or a single calendar collection.

2. ICS feeds go in `CALDAV_ICS_URLS` (comma-separated). Treat them as secrets:
   a private feed URL is a password. They never appear in records or logs;
   each feed is identified as `ics-<12 hex digest of the URL>` under account
   `ics`, named by its `X-WR-CALNAME` or `CALDAV_ICS_NAMES`.

```sh
export THROUGHLINE_URL=http://127.0.0.1:8765 THROUGHLINE_TOKEN=<plugin token>
export CALDAV_URL=https://caldav.fastmail.com/ CALDAV_USERNAME=you@example.com
export CALDAV_PASSWORD="$(your-secret-reader caldav-app-password)"
export CALDAV_ICS_URLS="$(your-secret-reader club-feed-url)"
export CALDAV_OWNER_ADDRESSES=you@example.com
throughline-caldav backfill --dry-run
throughline-caldav backfill
throughline-caldav sync --once     # under a timer, or: throughline-caldav sync (every 30 minutes)
```

Either `CALDAV_URL` or `CALDAV_ICS_URLS` (or both) must be set.

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `CALDAV_URL` | | CalDAV server, principal, home or calendar URL |
| `CALDAV_USERNAME` | | Login name (HTTP Basic) |
| `CALDAV_PASSWORD` | | **Secret.** Password or app password |
| `CALDAV_ICS_URLS` | | **Secret.** Comma-separated ICS feed URLs (`webcal://` is read as `https://`) |
| `CALDAV_ICS_NAMES` | | Optional names for the feeds, same order |
| `CALDAV_ACCOUNT` | `username@host` | Account label used in ids |
| `CALDAV_CALENDARS` | all | Comma-separated calendar names or paths to include |
| `CALDAV_EXCLUDE_CALENDARS` | | Calendar names or paths to skip |
| `CALDAV_OWNER_ADDRESSES` | | Your addresses (sets `my_response` and attendee `self`) |
| `CALDAV_PAST_DAYS` | `30` | Rolling window: days before today (UTC) |
| `CALDAV_FUTURE_DAYS` | `90` | Rolling window: days after today |
| `CALDAV_WINDOW_START` / `CALDAV_WINDOW_END` | | Fixed window `YYYY-MM-DD` (end exclusive), e.g. for a one-off historical backfill; used in both modes when set |
| `CALDAV_DEFAULT_TZ` | `UTC` | Zone for floating times |
| `CALDAV_LINKS` | `true` | Emit `calendar_event` entity links |
| `CALDAV_BATCH_SIZE` | `200` | Messages per acknowledged batch and checkpoint |
| `CALDAV_TIMEOUT_S` | `60` | HTTP timeout |
| `CALDAV_FIXTURE` | | Tests only: recorded HTTP answers served through `httpx.MockTransport` |

## Dependencies

`httpx` (already in the SDK) for WebDAV, plus
[`icalendar`](https://pypi.org/project/icalendar/) to parse iCalendar data and
[`recurring-ical-events`](https://pypi.org/project/recurring-ical-events/) to
expand recurrences. Expansion is the hard part of iCalendar: RRULE with
BYxxx parts, RDATE, EXDATE, overridden instances (RECURRENCE-ID, including
moved and cancelled ones), VTIMEZONE definitions and DST transitions. These
two libraries are the maintained, widely used implementations of exactly
that; a hand-written subset would silently produce wrong occurrences. Server
side `<C:expand>` is not used because support for it varies between servers.

## Run state and replay

The cursor holds, per calendar or feed, the window, the ctag/sync-token,
the HTTP ETag of a feed and the resource etags (with the UIDs each resource
contains), plus the mapping snapshot (occurrences inside the window and one
small entry per event). Snapshot entries leave the cursor once their
occurrence ends before the window start. Within a run every record is sent
once in its last state; interim checkpoints (every `CALDAV_BATCH_SIZE`
messages) keep the cursor of the start of the run plus a resume offset and
the run's observation time, so a crash re-fetches, recognizes the same
answer and continues where the acknowledged messages end. A source that
fails (auth error, HTTP error) is skipped and reported as partial coverage;
when nothing at all could be read (every calendar REPORT and feed failing)
the run fails. A crash resumes at the acknowledged offset only when the source answers exactly as before; if anything changed in between, the run starts over from its start cursor (re-sending is harmless: unchanged records stay unchanged). Event entries are dropped from the cursor
once none of their occurrences is known any more.

## Limitations

- Only the window is read; history before `CALDAV_PAST_DAYS` needs a one-off
  run with `CALDAV_WINDOW_START`/`END`.
- Exceptions (overridden instances) are tracked as occurrences of their
  series; the lifecycle channel records series-level changes (an EXDATE or a
  changed override shows as a `changed` series with `recurrence` or as a
  removed occurrence), not a separate record per override.
- ICS feeds that only publish upcoming events make past events vanish: an
  event is only marked removed when it had live occurrences in the current
  window.
- `sync-collection` (RFC 6578) is not used; the ctag/sync-token is only
  compared as a change marker and etag listings do the rest.
- Each URL in `CALDAV_ICS_URLS` is redacted from logs and refused in records
  on its own (the SDK redacts the variable's whole value only).
- Not verified against live servers (built from RFC 4791 / RFC 6764 and
  each provider's documentation): iCloud's partitioned hosts
  (`pXX-caldav.icloud.com`) are reached by following its redirects; some
  servers may not return `getctag`, which only costs an etag listing per run.

## Tests

```sh
THROUGHLINE_DEV=1 uv run pytest -q plugins/caldav
uv run ruff check plugins/caldav
```

Offline: recorded multistatus answers and an ICS feed (`tests/recorded/server.json`,
invented example.com data) through `httpx.MockTransport`. Covered: discovery,
recurrence with EXDATE and a moved/cancelled override, time zones, all-day and
floating events, ctag/sync-token skip, etag diff with changed, deleted and
moved-out-of-window resources, a new EXDATE, a feed losing an event, the
empty-answer guard, partial and total failure, secret handling, and the SDK
contract kit.
