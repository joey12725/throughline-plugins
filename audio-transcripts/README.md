# Audio transcripts

Watches folders of recordings and transcribes every new audio or video file
through an OpenAI-compatible speech-to-text endpoint. Point it at whatever
something else keeps filling:

- phone voice memos synced to a computer (iCloud Drive, Syncthing, Dropbox),
- a meeting recorder's or dictaphone's export folder,
- the sync folder of a wearable recorder (Plaud, Limitless-style pendants,
  Bee, ... — any device that drops audio files in a folder),
- local OBS or Zoom/Teams recordings.

Live: each run scans the folders and transcribes files it has not seen. The
endpoint can be OpenAI (`https://api.openai.com/v1`, `whisper-1`) or a
self-hosted server implementing the same `POST /v1/audio/transcriptions`
route (faster-whisper-server/speaches, whisper.cpp server, LocalAI, ...).

## Records

The shape follows the other transcript connectors (see the Wave plugin): one
recording record per file and one record per transcript segment.

| Channel | What |
|---|---|
| `audio_transcripts.recordings` | One record per file: start time, end time (start + duration), folder, file name, and the first 500 characters of the transcript. Raw payload keeps the full transcript with all segments. |
| `audio_transcripts.segments` | One record per timed segment the endpoint returns, threaded by recording (`conversation_whole_thread`, so a recording is one conversation however long its pauses). Each has a `part_of` link to its recording record. |

- **Ids are content hashes**: the recording id is `sha256-` + the first 32 hex
  digits of the SHA-256 of the file's bytes; segments are `<recording id>:00000`,
  `:00001`, ... Renaming, moving or copying a file never creates a second record.
- **Recording time, with honest precision** (first usable wins):
  1. `com.apple.quicktime.creationdate` metadata (iPhone Voice Memos, many
     phone apps; local time with offset) — exact;
  2. a date and time in the file name (`2025-03-04 10-15-00`,
     `REC_20250304_101500`, `20250304T1015`), read in `AUDIO_TRANSCRIPTS_TIMEZONE` — exact;
  3. `creation_time` metadata (UTC; can be a later conversion time, hence
     below the name) — exact;
  4. a date alone in the file name — day precision;
  5. the file's modification time minus the duration (recorders usually
     write the file when they stop) — approximate.

  `time_source` and `time_precision` say which was used; day and approximate
  times also set the record's `subject` precision. Metadata needs `ffprobe`.
- **Segment times** are recording start + segment offset, on whole seconds;
  the exact offsets are in `start_s` / `end_s`.
- **Speakers**: Whisper-style endpoints do not diarize, so speakers are
  unknown (`author_kind=other`, no author). Folders listed in
  `AUDIO_TRANSCRIPTS_OWNER_FOLDERS` are your own voice memos: their records get
  `author_kind=owner` and your identifier as author. A `speaker` key on
  segments (diarizing servers) is kept as `speaker_label`.
- The text is machine transcription of what people said, not model-written
  prose, so the channels are ordinary life events; `model` records which
  model produced it.

Recording fields: `folder`, `file_name`, `content_hash`, `author_kind`,
`time_source`, `time_precision`, `relative_path`, `size_bytes`, `duration_s`,
`language`, `model`, `segment_count`, `chunks`, `timestamps`.
Segment fields: `folder`, `file_name`, `content_hash`, `author_kind`,
`time_source`, `time_precision`, `segment_no`, `start_s`, `end_s`,
`speaker_label`, `model`.

## Capped and cached

Transcription costs money or GPU time, so it is bounded and never repeated:

- **Per run**: at most `AUDIO_TRANSCRIPTS_MAX_FILES` files (default 20) and
  `AUDIO_TRANSCRIPTS_MAX_MINUTES` audio minutes (default 120) go to the
  endpoint; the rest wait for the next run and the run reports `partial`
  coverage with reason `run_cap`. Oldest files go first.
- **Cache**: every transcript is stored in
  `AUDIO_TRANSCRIPTS_CACHE_DIR/transcripts/<sha256>.json` (default
  `$XDG_CACHE_HOME/throughline/audio_transcripts`). Resetting the cursor,
  rebuilding the store, a dry run or re-running after a crash reads the cache
  instead of the endpoint. A file the endpoint *refused* (4xx) is cached as a
  failure and not sent again; delete its cache file to retry.
  `index.json` remembers file hashes by path, size and modification time so
  large files are not re-hashed every run.
- Files modified less than `AUDIO_TRANSCRIPTS_MIN_AGE_S` (default 300) seconds
  ago are skipped until a later run, so a file still being synced is not
  transcribed half-written.
- `backfill --dry-run` does transcribe (and caches) within the same caps:
  it is the Throughline writes that are dry, not the endpoint calls.

## ffmpeg (optional)

Without ffmpeg, files the endpoint accepts directly (`wav`, `mp3`, `m4a`,
`mp4`, `ogg`, `webm`, `flac`, ...) are uploaded as they are, when they fit
one request. With `ffmpeg` (and `ffprobe`) on PATH:

- long or large files are converted to mono 16 kHz Opus and split into
  `AUDIO_TRANSCRIPTS_CHUNK_SECONDS` (default 1200 s) pieces under
  `AUDIO_TRANSCRIPTS_MAX_BYTES` (default 24 MiB), and the segments are
  re-timed onto one timeline;
- other containers (`mov`, `mkv`, `amr`, `caf`, `wma`, ...) are converted;
- metadata creation times and durations are read.

Files that need ffmpeg when it is missing are left for later with `partial`
coverage, reason `needs_ffmpeg`. Cuts are at fixed positions (no silence
search), so a word spoken across a cut may be split or garbled.

## Setup

```sh
export THROUGHLINE_URL=http://localhost:8000
export THROUGHLINE_TOKEN=<token from: throughline token create audio_transcripts --kind plugin --plugin audio_transcripts --json>
export AUDIO_TRANSCRIPTS_DIRS="memos=$HOME/VoiceMemos:meetings=$HOME/Recorder/Export"
export AUDIO_TRANSCRIPTS_OWNER_FOLDERS=memos
export AUDIO_TRANSCRIPTS_URL=https://api.openai.com/v1
export AUDIO_TRANSCRIPTS_API_KEY=sk-...           # omit for a local server without auth
uv run --package throughline-audio-transcripts throughline-audio-transcripts backfill
uv run --package throughline-audio-transcripts throughline-audio-transcripts sync --once   # on a timer
```

On Windows separate folders with `;`.

| Variable | Default | Meaning |
|---|---|---|
| `AUDIO_TRANSCRIPTS_DIRS` | required | Folders, separated by the OS path separator or newlines; `label=path` or a bare path (label = folder name) |
| `AUDIO_TRANSCRIPTS_URL` | required | API base ending in `/v1` |
| `AUDIO_TRANSCRIPTS_API_KEY` | — | Secret: bearer key |
| `AUDIO_TRANSCRIPTS_MODEL` | `whisper-1` | Model name |
| `AUDIO_TRANSCRIPTS_LANGUAGE` | detect | ISO-639-1 hint |
| `AUDIO_TRANSCRIPTS_MAX_FILES` | 20 | Files sent per run (0 = no cap) |
| `AUDIO_TRANSCRIPTS_MAX_MINUTES` | 120 | Audio minutes sent per run (0 = no cap) |
| `AUDIO_TRANSCRIPTS_MIN_AGE_S` | 300 | Skip files modified more recently |
| `AUDIO_TRANSCRIPTS_EXTENSIONS` | common audio/video | Comma-separated extensions |
| `AUDIO_TRANSCRIPTS_RECURSIVE` | `true` | Include subfolders (hidden ones are skipped) |
| `AUDIO_TRANSCRIPTS_OWNER_FOLDERS` | — | Folder labels that are your own voice |
| `AUDIO_TRANSCRIPTS_OWNER_IDENTIFIER` | `audio_transcripts:owner:owner` | Your author identifier, `system:kind:value` |
| `AUDIO_TRANSCRIPTS_OWNER_NAME` | — | Display name on your recordings |
| `AUDIO_TRANSCRIPTS_TIMEZONE` | this machine's | Zone of times in file names |
| `AUDIO_TRANSCRIPTS_CACHE_DIR` | `~/.cache/throughline/audio_transcripts` | Transcript cache |
| `AUDIO_TRANSCRIPTS_FFMPEG` | `auto` | `auto`, a path, or `off` |
| `AUDIO_TRANSCRIPTS_CHUNK_SECONDS` | 1200 | Longest audio per request |
| `AUDIO_TRANSCRIPTS_MAX_BYTES` | 25165824 | Upload limit per request |
| `AUDIO_TRANSCRIPTS_TIMEOUT` | 600 | Seconds per request |
| `AUDIO_TRANSCRIPTS_RETRIES` | 3 | Retries after 429, 5xx, connection errors |
| `AUDIO_TRANSCRIPTS_FIXTURE` | — | Tests only: recorded endpoint answers |
| `THROUGHLINE_INTERVAL_S` | 900 | Set to your timer's cadence |

## Failure handling

- Endpoint unreachable, overloaded after retries, or refusing the key
  (401/403): the remaining files wait; if nothing could be transcribed in the
  run, the run fails so source health shows it.
- Endpoint refuses a file (other 4xx): cached as a failure, reported as
  `partial` coverage (`source_error`), never retried automatically.
- A watched folder that does not exist: `partial` coverage; all missing: the
  run fails.
- Servers that do not support `verbose_json` (e.g. `gpt-4o-transcribe`) are
  asked for `json` instead; such transcripts have one untimed segment per
  request (`timestamps=false`).

## Limitations

- Files are only ever read. Deleting a file does not delete its records.
- Re-encoding a recording (a different file with the same audio) is a
  different hash and therefore a second recording.
- No diarization or voice identification; no summaries (add those
  downstream).
- This plugin is self-contained (it does not depend on any other Throughline
  media package); its transcription client mirrors the request/response shape
  those packages use.

## Development

No dependencies beyond `throughline-sdk` and `httpx`; ffmpeg is optional at
runtime and in tests (the chunking test is skipped without it). Tests generate
tiny WAV files with the standard library and answer transcription requests
with an `httpx.MockTransport`:

```sh
THROUGHLINE_DEV=1 uv run pytest -q plugins/audio-transcripts
```
