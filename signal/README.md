# Throughline Signal plugin

Imports Signal messages through [signal-cli](https://github.com/AsamK/signal-cli)
running as a **linked device** of your phone (like Signal Desktop), using its
JSON-RPC daemon over HTTP. No Python dependencies beyond the Throughline SDK.

**Be aware before you start:**

- A newly linked device only receives messages **from the moment it is linked
  onward**. Signal does not give linked devices your earlier history, so this
  plugin cannot import messages from before linking.
- Signal Desktop's database is encrypted and is **not** read; signal-cli is a
  separate linked device with its own storage.
- Messages sent from your phone arrive as *sync messages* and are imported as
  yours. Messages received while signal-cli is offline wait on Signal's
  servers (for a limited time, about 30 days) and are fetched on the next run.

## How it works

The daemon runs with `--receive-mode=manual`, so nothing is consumed until
the plugin asks. Each run:

1. calls JSON-RPC `receive` until the queue is empty. `receive` acknowledges
   the envelopes to Signal, so they are never delivered again: each batch is
   **appended to a JSONL spool and fsynced before anything else** happens;
   receipts and typing indicators are dropped;
2. calls `listGroups` for group names (cosmetic, failures are ignored);
3. indexes the whole spool and maps the lines added since the last checkpoint
   (cursor: byte offset per spool file).

This was chosen over the daemon's push (SSE) stream because it needs no extra
long-running consumer, cannot lose envelopes between the daemon and a crashed
consumer, and leaves undelivered messages queued on Signal's side while
Throughline is down. The spool (`<state>/spool/signal-YYYY-MM.jsonl`) is the
source of truth: keep it backed up, and you can rebuild everything from it.

Reactions, edits and "delete for everyone" are folded into the message they
target, never stored as their own messages: the target gets a new version
with `reaction_details`, the edited text (`edited_at`, `edit_count`) or
`deleted_at`. A message's id is `signal:<account>:<author uuid>:<sent ms>`,
which is how Signal itself refers to messages.

## What it captures

One channel, `signal.messages`; one thread per contact
(`signal:<account>:dm:<uuid>`) or group (`signal:<account>:group:<group v2 id>`).

| Field | Type | Meaning |
|---|---|---|
| `direction` / `author_kind` | text | `incoming`/`outgoing`; `owner` (your devices) or `other` |
| `conversation_kind` | text | `dm` or `group_dm` |
| `group_id` | text | Group v2 id (base64) |
| `sender_uuid` / `sender_device` | text / integer | Sender ACI and device id |
| `reply_to` / `quote_text` | text | Quoted message id (also a `reply_to` link) and quoted text |
| `edited_at` / `edit_count` | timestamp / integer | Latest edit |
| `deleted_at` | timestamp | Sender deleted it for everyone (the received text is kept) |
| `attachments` | json | `[{contentType, filename, size, width, height, caption, id, voiceNote}]`, metadata only |
| `sticker` | json | Sticker pack/id/emoji |
| `reaction_details` | json | `[{emoji, sender_id, sender_name? \| sender_kind: "owner", at}]` |
| `expires_in_s`, `view_once`, `mentions` | | Disappearing timer, view-once flag, mentions |

Authors are `{system: "phone", kind: "number"}` when Signal shares the
sender's number, else `{system: "signal", kind: "uuid"}`.

## Setup

1. Install signal-cli (Java or native build) on the Throughline machine and
   link it to your phone:

   ```sh
   signal-cli link -n "Throughline"        # prints an sgnl://linkdevice URI
   # render it as a QR code (e.g. `qrencode -t ansi "<uri>"`) and scan it in
   # Signal → Settings → Linked devices → Link new device
   signal-cli -a +15555550100 receive      # first sync of contacts and groups
   ```

2. Run the daemon as a service, bound to localhost:

   ```ini
   # ~/.config/systemd/user/signal-cli.service
   [Unit]
   Description=signal-cli JSON-RPC daemon
   [Service]
   ExecStart=/usr/local/bin/signal-cli -a +15555550100 daemon --http 127.0.0.1:8080 --receive-mode=manual
   Restart=on-failure
   [Install]
   WantedBy=default.target
   ```

   `systemctl --user enable --now signal-cli`. The HTTP endpoint has no
   authentication: keep it on localhost, or put it behind a reverse proxy that
   requires a token (`SIGNAL_RPC_TOKEN` is sent as `Authorization: Bearer`).
3. Run the plugin on a timer (every 5 minutes by default). Run signal-cli
   regularly: a linked device that stays offline too long is unlinked by
   Signal.

Alternatively, without the daemon, set `SIGNAL_RPC_URL=none` and append to the
spool yourself, e.g. a timer running
`signal-cli -a +15555550100 -o json receive >> ~/.local/state/throughline-signal/spool/signal-cli.jsonl`;
the plugin then only maps the spool.

| Environment variable | Default | Meaning |
|---|---|---|
| `THROUGHLINE_URL` / `THROUGHLINE_TOKEN` | – | Core origin and token |
| `SIGNAL_RPC_URL` | `http://127.0.0.1:8080` | Daemon origin; `none` maps the spool only |
| `SIGNAL_ACCOUNT` | – | Your number (+E.164), recorded with spooled envelopes and in ids; recommended always |
| `SIGNAL_MULTI_ACCOUNT` | `0` | `1` when the daemon runs without `-a` (multi-account mode): `SIGNAL_ACCOUNT` is then sent as the `account` param of every request |
| `SIGNAL_STATE_DIR` | `~/.local/state/throughline-signal` | Spool and group names |
| `SIGNAL_SPOOL_DIR` | `<state>/spool` | Spool directory |
| `SIGNAL_RPC_TOKEN` | – | Secret: Bearer token for a proxied daemon |
| `SIGNAL_RECEIVE_TIMEOUT` | `5` | Seconds a receive call waits |
| `SIGNAL_MAX_ROUNDS` | `20` | Receive calls per run |
| `SIGNAL_BATCH_SIZE` | `500` | Envelopes per receive call and spool lines per checkpoint |

## Run

```sh
uv run --package throughline-signal throughline-signal backfill   # maps the whole spool
uv run --package throughline-signal throughline-signal sync --once
```

## Limitations

- No history from before linking (see above); attachments are metadata only
  (signal-cli keeps the files in its own data directory).
- The whole spool is read each run to fold reactions and edits; that is fast
  for years of text messages but grows with the spool.
- Edits sent from your own phone are assumed to arrive as
  `syncMessage.sentMessage.editMessage`; this shape and the JSON-RPC
  `receive`/`listGroups` results follow signal-cli's documentation and source
  and were not checked against a live account here.
- Group membership changes, calls, stories and payments are not imported.
