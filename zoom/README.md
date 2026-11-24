# Zoom transcripts

A Throughline plugin that polls Zoom for your **cloud recording transcripts**.
It turns each transcript into speaker turns, adds one record per recorded
meeting, and links every turn `part_of` its meeting. This is the same shape
as `plugins/wave`. It reads text only: no audio or video is downloaded or
stored, and no model is called.

## What it captures

| Channel | One message per | Fields |
| --- | --- | --- |
| `zoom.transcripts` | speaker turn (consecutive WebVTT cues by one speaker are merged). The external id is `<meeting uuid>:<turn no>` and the thread is the meeting. | `speaker_label`, `start_s`, `end_s` (offsets into the recording), `cue_count`, `is_owner` (boolean, null when the owner is unknown), `author_kind` (`owner` or `other`), `meeting_id` |
| `zoom.meetings` | recorded meeting instance. The external id is the meeting UUID, the time span is `start_time` to `start_time + duration` and the author is the host. | `meeting_id`, `topic`, `duration_min`, `timezone`, `host_id`, `host_email`, `recording_count`, `transcript_status` (`complete`, `processing`, `none`, `failed`), `turn_count`, `speakers` (json), `participant_count`, `participants` (json `[{name,email}]`), `participants_status` (`ok`, `disabled`, `not_found`, `unavailable`) |

- `zoom.transcripts` sets `conversation_whole_thread=true`, so each meeting
  stays one conversation.
- Turn times are absolute: the transcript file's `recording_start` plus the
  cue offset. Times are given in the meeting's time zone, so each one keeps
  its real UTC offset. A meeting that was recorded in several parts has
  several transcript files. Each part is timed from its own start, and turn
  numbers continue across the parts.
- Speaker authors default to `{system: "zoom", kind: "speaker_label", value:
  "<meeting uuid>:<label>"}`, which means a label is only unique within one
  meeting. The author becomes `{system: "email", kind: "email"}` in two cases:
  - the turn is yours (we know your email from `/users/me`);
  - the participant report gives exactly one email address for that display
    name.

  Email authors resolve through the identity registry.
- A turn is the owner's (`is_owner`, `author_kind=owner`) when its label or
  email matches one of these: the `/users/me` profile (display name, first and
  last name, email), the host's own entry in the participant report, or a name
  in `ZOOM_OWNER_NAMES`. Everything is human speech transcribed by Zoom, so no
  record is `agent`.
- Raw payloads keep the Zoom meeting and recording-file objects, minus
  passcodes, download tokens and `account_id`.

## Setup

1. Go to the Zoom App Marketplace, then Develop, then Build App, and create a
   **Server-to-Server OAuth** app. An account admin, or a user with that role
   permission, has to do this.
2. Add these scopes:
   - `cloud_recording:read:list_user_recordings` (or the older
     `recording:read`) and `user:read:user` (for `/users/me`);
   - optionally `report:read:list_meeting_participants` (or the older
     `report:read:admin`) for participants.
3. Activate the app. Copy the **Account ID**, **Client ID** and
   **Client Secret**.
4. Under Settings, then Recording, turn on cloud recording and **Create audio
   transcript**. Otherwise Zoom produces no TRANSCRIPT file.

The plugin gets a token from `POST https://zoom.us/oauth/token?grant_type=account_credentials&account_id=…`
using HTTP Basic auth. The one-hour token is cached in memory only, renewed a
minute before it expires (or after a 401), and never written or logged.

| Environment | Default | Meaning |
| --- | --- | --- |
| `THROUGHLINE_URL`, `THROUGHLINE_TOKEN` | | Throughline core URL and plugin token |
| `ZOOM_ACCOUNT_ID` | (secret) | Server-to-Server OAuth account ID |
| `ZOOM_CLIENT_ID` | (secret) | OAuth client ID |
| `ZOOM_CLIENT_SECRET` | (secret) | OAuth client secret |
| `ZOOM_EARLIEST` | `2020-01-01` | Oldest meeting date a backfill walks back to |
| `ZOOM_OVERLAP_HOURS` | `72` | How far back each sync lists again (transcripts appear hours after a meeting) |
| `ZOOM_PARTICIPANTS` | `0` | `1` reads the participant report (paid plan + report scope) |
| `ZOOM_OWNER_NAMES` | | Comma-separated extra speaker labels that are you |
| `ZOOM_ACCESS_LABEL` | | Access label for every record, for example `work` |
| `ZOOM_PAGE_SIZE` | `300` | Meetings per list page (Zoom's maximum is 300) |
| `ZOOM_BATCH_SIZE` | `200` | Turns per checkpointed ingest batch |
| `ZOOM_FIXTURE` | | Recorded-response file used instead of the network (tests) |
| `ZOOM_NOW` | | Clock override, as an ISO instant (tests) |

## Run

```sh
uv sync --all-packages
throughline-zoom backfill          # walks ZOOM_EARLIEST .. today in 30-day windows
throughline-zoom sync --once       # re-lists the overlap window, for a timer
throughline-zoom sync              # polls forever (default interval 1 hour)
```

## How it reads

- **Backfill** calls `GET /v2/users/me/recordings?from=&to=&page_size=&next_page_token=`.
  It uses consecutive 30-day windows (Zoom allows about one month per request)
  from `ZOOM_EARLIEST` to today, and follows `next_page_token` within each
  window. It checkpoints after every meeting, every `ZOOM_BATCH_SIZE` turns
  and every page. A resumed checkpoint holds Zoom's page token, which expires
  after about 15 minutes. If the token is refused, that window restarts from
  page one. Replays are idempotent because ids are stable.
- **Sync**, once a full scan has completed, lists from
  `catch_up_since(ZOOM_OVERLAP_HOURS)` to today. That start moves back to
  cover an outage. It also moves back to the oldest meeting whose transcript
  was still processing or failed. Such meetings are kept in the cursor for 30
  days. A meeting with no TRANSCRIPT file at all counts as processing for its
  first 2 days. Sync with an empty cursor does a full backfill.
- **Transcripts**: each TRANSCRIPT file whose `status` is `completed` is
  fetched from its `download_url` with the bearer token. Redirects are
  followed, and httpx drops the Authorization header on a cross-host
  redirect. The file is parsed as WebVTT. Zoom's `Name: text` cue
  convention gives the speaker; `<v Name>` voice spans are understood too. A
  cue with no label continues the previous turn.
- **Participants** (optional) come from
  `GET /v2/report/meetings/{uuid}/participants`, with paging. A UUID that
  begins with `/` or contains `//` is double URL-encoded, as Zoom requires. A
  404 marks that meeting `not_found`. Any other refusal (free plan, missing
  scope) turns participant reading off for the rest of the run and marks the
  meeting `unavailable`. Either way the run yields a `partial` coverage
  report for scope `participants` and still imports everything else.
- A failed transcript download likewise yields `partial` coverage for scope
  `transcripts`, and the meeting is retried on the next syncs.
- An unreachable Zoom, or a refused token, fails the run. HTTP 429 waits for
  `Retry-After` and retries.

## Limitations

- API shapes come from Zoom's public API reference, not a live account. The
  fixtures in `tests/recorded/` are invented from the documented response
  shapes. Unverified details:
  - whether the `from`/`to` filter uses the meeting's UTC start date (the
    overlap absorbs edge cases);
  - the exact error for an expired `next_page_token`;
  - the scope names, which differ between Zoom's older and granular scopes.
- Only recordings of the user who owns the app's token (`/users/me`) are
  listed. Account-wide or other users' recordings are not imported.
- Zoom deletes cloud recordings under its retention settings. Anything
  deleted before the first backfill cannot be fetched, and the plugin cannot
  prove where that gap is, so it reports no `source_cap` gap.
- Only Zoom's `TRANSCRIPT` (audio transcript) files are read. Closed-caption
  (`CC`) files, chat files, AI Companion summaries and local recordings are
  not.
- A speaker label is whatever display name Zoom put in the transcript.
  Without the participant report, people resolve per meeting only. Two
  participants with the same display name are not split apart.
- If Zoom re-transcribes a meeting, turns are numbered again. Changed turns
  become new versions, but surplus old turns are not deleted, because
  Throughline is append-only.
- Edits older than the sync overlap (for example a renamed topic) are only
  picked up by an explicit `backfill`.

## Tests

```sh
THROUGHLINE_DEV=1 uv run pytest -q plugins/zoom
uv run ruff check plugins/zoom
```

The unit tests inject an `httpx.MockTransport`. The contract test
(`check_plugin`) runs the plugin as a subprocess against
`tests/recorded/api.json`, which it reads through `ZOOM_FIXTURE`. That file
holds 5 meetings across 3 windows, a second page, a cross-host download
redirect, a double-encoded UUID, a participant 404, a meeting with two
transcript parts and a transcript that is still processing.
