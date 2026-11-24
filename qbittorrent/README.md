# Throughline qBittorrent

Read-only qBittorrent lifecycle history for Throughline. The connector writes
`downloads.qbittorrent` messages for torrent additions, completions, removals,
category changes and tag changes. It never adds, pauses, resumes, rechecks or
deletes a torrent.

## Two layers

`src/throughline_qbittorrent/fetch.py` uses only the Python standard library.
It logs into the WebUI API v2, reads only:

- `GET /api/v2/app/version`
- `GET /api/v2/torrents/info` for the first complete snapshot
- `GET /api/v2/sync/maindata?rid=...` for incremental changes/removals

It writes a secret-free JSONL export plus an incremental state file. The SDK
plugin maps either that export or a live fetch into Throughline messages:

```sh
QBITTORRENT_SOURCE=export:/path/qbittorrent.jsonl.gz \
  uv run throughline-qbittorrent sync --once
```

For a direct connection, use `QBITTORRENT_SOURCE=live` (the default) and set
`QBITTORRENT_URL`, `QBITTORRENT_USERNAME`, and `QBITTORRENT_PASSWORD`.

## History and identity

The first fetch creates `added` and, when applicable, `completed` events for
every torrent still present. qBittorrent does not retain a general history of
already-removed torrents, so removals can only be captured after the connector
starts. A removed hash that is later re-added begins a new generation. Stable
external IDs are based on the info hash, generation, event type and a local
category/tag change sequence, so retries are idempotent.

Event times use `added_on` and `completion_on` when qBittorrent has them.
Removal and category/tag change times are the poll time and carry
`time_precision=observed`.

## Privacy and media joins

Only a tracker hostname is retained. Announce URLs (which can contain passkeys),
magnet URIs, WebUI credentials and session cookies are never exported. Raw
payloads are a field whitelist, not qBittorrent responses.

`media_kind` is derived only from exact category/save-path cues (`sonarr`/TV,
`radarr`/movie, `lidarr`/music, audiobook, book/manga and arcade/ROM). This is a
cheap join hint for later Plex correlation; it deliberately does not fuzzy-match
a torrent name to a Plex work.

## Standalone export

```sh
python3 fetch.py export \
  --out qbittorrent.jsonl.gz \
  --state qbittorrent-state.json
```

The state file must persist between runs. Advance it only after the export was
successfully ingested into Throughline; the installed sync script follows that
rule.

## Tests

```sh
uv run --no-sync pytest plugins/qbittorrent/tests -q
```
