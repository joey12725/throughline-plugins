# SimpleFIN Bridge

Bank, credit card, loan and investment accounts, and every transaction on
them, from a [SimpleFIN Bridge](https://beta-bridge.simplefin.org) account
(the hosted service that connects to thousands of institutions through MX and
hands out a read-only "access URL"). Live: every run asks the Bridge for new
and changed transactions; nothing is exported by hand.

## This is the most sensitive data in your hub

Account names, balances, payees and spending patterns reveal more about a
person than almost anything else Throughline stores. Before you turn this on:

- **Labels.** Every record carries `access_label` = `financial` (a label kind
  seeded by the core; change it with `SIMPLEFIN_ACCESS_LABEL`). Labelled rows
  are denied by default to every non-owner asker, *even inside a granted
  channel*: a work or friend agent only sees them with an explicit
  `--labels financial` grant. See [docs/v11/access.md](../../docs/v11/access.md)
  ("Sensitivity labels and disclosure"). Do not clear the label.
- **Egress.** Labelled content is held back from remote model providers by
  default (no remote embeddings, no remote model judgements): see
  [docs/v11/encryption.md](../../docs/v11/encryption.md). Both channels also
  register with `embed=false`. Keyword and SQL search still work.
- **Encryption at rest.** Follow docs/v11/encryption.md for an encrypted
  database volume and encrypted backups before importing.
- **The access URL is a credential.** It embeds a username and password that
  read every connected account. It is declared as a secret (the runner
  redacts it from logs and refuses any record containing it); keep it in a
  `0600` file or a secret store, never in shell history, and revoke it on the
  Bridge if it leaks. This plugin never logs it, never puts it in records or
  error messages, and only ever *reads* (SimpleFIN has no write API).

## Setup

1. Create a Bridge account, connect your institutions, and create a **setup
   token** (Bridge → Apps → *New connection*).
2. Exchange it, once, for an access URL. The token is read from stdin or a
   hidden prompt, never from the command line:

   ```sh
   uv run --package throughline-simplefin throughline-simplefin-claim \
       --output ~/.config/throughline/simplefin-access-url
   ```

   A setup token can be claimed only once. If the claim is refused with 403
   and you did not claim it yourself, someone else did: disable that
   connection on the Bridge.
3. Create a plugin token on the Throughline box:
   `throughline token create simplefin --kind plugin --plugin simplefin --json`.
4. Run:

   ```sh
   export THROUGHLINE_URL=http://localhost:8000 THROUGHLINE_TOKEN=<plugin token>
   export SIMPLEFIN_ACCESS_URL_FILE=~/.config/throughline/simplefin-access-url
   export SIMPLEFIN_START_DATE=2025-01-01        # first run only; default 90 days ago
   uv run --package throughline-simplefin throughline-simplefin backfill --dry-run
   uv run --package throughline-simplefin throughline-simplefin backfill
   uv run --package throughline-simplefin throughline-simplefin sync --once   # on a timer
   ```

## Channels

| Channel | Kind | What |
|---|---|---|
| `simplefin.accounts` | reference, `embed=false` | One record per account (external id = SimpleFIN account id). A new version whenever the balance or balance date changes, so the version history is the balance history. |
| `simplefin.transactions` | life event, `embed=false` | One record per transaction (external id `account_id:transaction_id`), one thread per account. |

Account fields: `account_id`, `account_name`, `org_name`, `org_domain`,
`currency`, `balance`, `available_balance`, `balance_date`, `holdings` (json).

Transaction fields: the account fields above (minus balances), plus
`transaction_id`, `amount` (negative = money out), `direction`
(`debit`/`credit`), `payee`, `description`, `memo`, `state`
(`pending`/`posted`/`dropped`), `posted_at`, `transacted_at`, `time_source`.

- `happened_at` is `transacted_at` when the institution reports it (the time
  of the purchase), else `posted`. It stays the same when a pending
  transaction posts, so the record is simply re-versioned with
  `state=posted`. A pending transaction with no date at all takes the
  balance date with an `approximate` subject precision.
- Raw payloads keep the Bridge's transaction object and the account's id,
  name, currency and institution — but not its balance, so a balance change
  never re-versions old transactions.
- No author is set: these are the owner's own accounts.

## How a run works

1. Windows of `SIMPLEFIN_WINDOW_DAYS` (default 60) days are requested with
   `GET <access-url>/accounts?start-date&end-date&pending=1`, oldest first,
   with HTTP Basic credentials taken from the access URL. A checkpoint
   follows every window, so a crash resumes at the last finished window.
2. Later runs start `SIMPLEFIN_OVERLAP_DAYS` (default 14) before the last
   scanned day. The overlap is what catches pending → posted changes and
   transactions the institution reports late.
3. A pending transaction inside the re-read span that the Bridge no longer
   returns gets a final version with `state=dropped` (declined, reversed, or
   posted under a new id — the Bridge does not say which).
4. Accounts are written after the transactions, once per run.
5. Connection problems the Bridge reports in `errors` (an institution that
   needs re-authentication) become `partial` coverage with reason
   `source_error`; a refused access URL (401/403), an inactive subscription
   (402) or an unreachable Bridge fails the run.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `SIMPLEFIN_ACCESS_URL` | — | Secret. The access URL itself (prefer the file below) |
| `SIMPLEFIN_ACCESS_URL_FILE` | — | File holding the access URL (written by the claim helper) |
| `SIMPLEFIN_START_DATE` | 90 days ago | Earliest day (`YYYY-MM-DD`) of the first run |
| `SIMPLEFIN_WINDOW_DAYS` | 60 | Days per request |
| `SIMPLEFIN_OVERLAP_DAYS` | 14 | Days re-read on every later run |
| `SIMPLEFIN_ACCOUNTS` | all | Comma-separated account ids to read |
| `SIMPLEFIN_ACCESS_LABEL` | `financial` | `access_label` of every record (empty: none — not recommended) |
| `SIMPLEFIN_TIMEZONE` | `UTC` | IANA zone used to present times (SimpleFIN sends UNIX seconds) |
| `SIMPLEFIN_FIXTURE` | — | Tests only: JSON served by an offline Bridge |
| `THROUGHLINE_INTERVAL_S` | 21600 | Set to your timer's cadence |

## Limitations

- **Request quota.** The Bridge asks clients to stay around 24 requests per
  day per access URL and may disable a token that hammers it. The default
  interval (6 hours, one request per run after the first) is well inside
  that; a long first backfill costs one request per window.
- **History depth** depends on the institution and the Bridge (often only
  60–90 days). Older history cannot be fetched; set `SIMPLEFIN_START_DATE`
  accordingly.
- Data is as fresh as the Bridge's own sync with each institution (usually
  once a day), not real time.
- Some institutions give a pending transaction a new id when it posts: you
  then see the pending record end as `dropped` and a new posted record.
- Investment `holdings` are captured as the account's json field, not as
  separate records; no positions history beyond account versions.
- API shape taken from the public protocol spec (simplefin.org/protocol.html);
  not verified against a live Bridge account. Fixtures are invented.

## Development

No dependencies beyond `throughline-sdk` and `httpx`. Tests use an offline
Bridge (`throughline_simplefin.recorded`, an `httpx.MockTransport` that
filters an invented fixture by the requested window) and the contract kit:

```sh
THROUGHLINE_DEV=1 uv run pytest -q plugins/simplefin
```
