# Throughline Home Assistant location plugin

Delivers a person's GPS fixes from [Home Assistant](https://www.home-assistant.io)
as track points under the source `homeassistant`. The core places every life
event at the nearest point within its channel's location tolerance.

Two layers (see [the remote-fetch pattern](../README.md#remote-fetch-pattern)):

1. `src/throughline_homeassistant/remote_fetch.py` uses only the Python
   standard library. Copy it to a host that can reach Home Assistant and holds
   a long-lived access token, and run it there:

   ```sh
   export HOMEASSISTANT_URL=http://homeassistant.local:8123
   export HOMEASSISTANT_TOKEN=...            # a long-lived access token
   python3 remote_fetch.py --person person.alex --since 2026-01-01T00:00:00Z \
       --out ha-$(date -u +%Y%m%dT%H%M%SZ).jsonl
   ```

   Instead of the two variables you can set `HOMEASSISTANT_SECRET_COMMAND`, a
   command that prints a named secret (it is run with `HOMEASSISTANT_URL` or
   `HOMEASSISTANT_TOKEN` as its last argument), for a secret manager on that
   host. `--person` is required: the person entity whose device trackers to
   read. It writes one line per GPS fix (`at`, `lat`, `lon`, `accuracy_m`,
   `entity`, `source_type`); no token, URL or other attribute is written.

2. The plugin reads that file: set `HOMEASSISTANT_RECORDING` to it and run
   `throughline-homeassistant sync --once`.

Home Assistant's recorder keeps only a limited history (10 days by default),
so run the export often enough to stay inside it.

## Tests

```sh
THROUGHLINE_DEV=1 uv run pytest -q plugins/homeassistant
```
