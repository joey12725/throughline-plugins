# Throughline IMAP plugin

Email from any IMAP mailbox over TLS: Fastmail, iCloud Mail, Proton Mail
(through Proton Mail Bridge), Gmail, Yahoo, Dovecot, and so on. It uses only
the Python standard library (`imaplib`, `email`). One Throughline message per
email in `imap.message`, with the same field names, thread model and author
shape as the Gmail connector (`plugins/email`) and the Microsoft Graph
connector (`plugins/msgraph`), so an agent sees mail from every provider the
same way.

Access is read-only: folders are opened with EXAMINE and bodies fetched with
`BODY.PEEK[]`, so nothing is marked as read and no flag is changed.

## What it captures

| Item | Where |
|---|---|
| Record id | external id `<account>:<Message-ID>` (without angle brackets). A message without a Message-ID gets `<account>:sha256:<hash of Date, From, To, Subject>`. The same email in two folders (a note to yourself in Inbox and Sent) is one record |
| Thread | Gmail: `<account>:gm:<thread id>` from `X-GM-THRID`. Elsewhere: `<account>:mid:<root>`, the root being the first id in `References`, else `In-Reply-To`, else the message itself (`thread_source` says which). Titled by the subject without `Re:`/`Fwd:` |
| From | author `{system: "email", kind: "email", value: <address>, display_name}`; `from_address`, `from_name` |
| To, Cc, Bcc, Reply-To | `to`, `cc`, `bcc`, `reply_to` as `[{address, name}]`; `participants` lists every address |
| Subject, Date header | `subject` (RFC 2047 decoded), `date_header` as sent |
| Time | `happened_at` is the server's INTERNALDATE (when the message arrived or was saved), else the Date header |
| Folders and flags | `folders` (where it was found in this fetch), `imap_uids` (`{folder: UID}`), `labels` (Gmail labels plus IMAP keywords), `is_unread` (no `\Seen`), `is_starred` (`\Flagged`), `is_important` (Gmail `\Important` or `$Important`) |
| Direction | `direction`: `draft` (`\Draft` flag or Drafts folder), `sent` (a Sent folder, Gmail `\Sent`, or From is one of the owner addresses), else `received`; `author_kind` `owner` or `other` |
| Body | content = Subject/From/To lines, then text/plain when present, otherwise HTML converted to text (`body_format`, `body_chars`, `body_truncated`); `snippet` = first 200 characters |
| Attachments | metadata only: `attachments` `[{filename, mime_type, size, content_id, inline}]`, `attachment_count`. Forwarded messages (message/rfc822) count as attachments |
| Mailing lists, size | `list_id` (List-Id), `size_estimate` (RFC822.SIZE) |
| Gmail ids | `gmail_thread_id`, `gmail_message_id` in hex, as the Gmail API (and `plugins/email`) writes them |
| Payload | a compact summary: key, decoded headers (From, To, Cc, Bcc, Reply-To, Subject, Date, Message-ID, In-Reply-To, References, List-Id), folders, flags, UIDs, attachment metadata, body format; the full body text only when content was truncated. No raw MIME is stored |

A flag change found through CONDSTORE (see below) creates a new version of
the same record. Deleted mail is not mapped: earlier versions stay.

## Setup

1. Create an app-specific password (all of these refuse your normal password
   for IMAP when two-factor authentication is on):

   | Provider | `IMAP_HOST` | Port / security | Password |
   |---|---|---|---|
   | Fastmail | `imap.fastmail.com` | 993 / `ssl` | Settings, Privacy & Security, App passwords (IMAP access) |
   | iCloud Mail | `imap.mail.me.com` | 993 / `ssl` | appleid.apple.com, Sign-In and Security, App-Specific Passwords. `IMAP_USERNAME` is your iCloud address |
   | Proton Mail | `127.0.0.1` | 1143 / `starttls` | Run Proton Mail Bridge; use the IMAP username and password the Bridge shows. Export its certificate (Bridge settings, Export TLS certificates) and set `IMAP_CA_FILE` to it |
   | Gmail | `imap.gmail.com` | 993 / `ssl` | Google Account, Security, App passwords. All Mail is detected and read instead of Inbox + Sent |
   | Outlook.com / Microsoft 365 | | | Microsoft has turned off password (basic) authentication for IMAP. Use the `msgraph` plugin instead |

2. Run it with a plugin token:

```sh
export THROUGHLINE_URL=http://127.0.0.1:8765 THROUGHLINE_TOKEN=<plugin token>
export IMAP_HOST=imap.fastmail.com IMAP_USERNAME=you@example.com
export IMAP_PASSWORD="$(your-secret-reader imap-app-password)"
throughline-imap backfill --dry-run
throughline-imap backfill
throughline-imap sync --once     # under a timer, or: throughline-imap sync (every 15 minutes)
```

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `IMAP_HOST` | | Server host name |
| `IMAP_PORT` | `993` | Port |
| `IMAP_SECURITY` | `ssl` | `ssl` (implicit TLS) or `starttls`. Plain-text connections are not offered |
| `IMAP_CA_FILE` | | Extra CA bundle (PEM) to trust, e.g. the Proton Bridge certificate. Certificates are always verified |
| `IMAP_USERNAME` | | Login name |
| `IMAP_PASSWORD` | | **Secret.** Password or app-specific password |
| `IMAP_ACCOUNT` | username | Mailbox label used in record and thread ids |
| `IMAP_OWNER_ADDRESSES` | username | Comma-separated addresses that are you (aliases): mail from them is `sent` / `owner` |
| `IMAP_FOLDERS` | auto | Comma-separated folder names or special-use roles (`\Sent`, `\All`, `\Archive`, ...). Auto: the `\All` folder when the server has one (Gmail), else INBOX plus the Sent folder (`\Sent`, or a folder named Sent, Sent Items, Sent Messages) |
| `IMAP_EXCLUDE_FOLDERS` | | Folders to skip |
| `IMAP_SINCE` | | First read of a folder only: skip mail before `YYYY-MM-DD` |
| `IMAP_MAX_MESSAGES` | `0` | First read of a folder only: the newest N messages (0 = all) |
| `IMAP_FLAG_UPDATES` | `true` | Re-read messages whose flags changed, when the server has CONDSTORE |
| `IMAP_BATCH_SIZE` | `100` | Messages per body fetch and checkpoint |
| `IMAP_BODY_LIMIT` | `50000` | Body characters kept in content (0 = all) |
| `IMAP_TIMEOUT_S` | `60` | Socket timeout |
| `IMAP_FIXTURE` | | Tests only: a JSON mailbox served by the in-memory fake (`fake.py`) |

## How sync works

- **Cursor per folder:** `{uidvalidity, uid, modseq}`. Each run asks STATUS
  for UIDVALIDITY/UIDNEXT (and HIGHESTMODSEQ with CONDSTORE), then
  `UID SEARCH UID <last+1>:*`. A changed UIDVALIDITY means the server
  renumbered the folder: it is read again from the start, and because ids
  come from Message-ID the records stay the same.
- **Flag changes:** with CONDSTORE, `UID FETCH 1:<last> (FLAGS)
  (CHANGEDSINCE <modseq>)` finds older messages whose flags changed; they are
  read again and get a new version.
- **Planning pass, then bodies:** the key headers (Message-ID, Date, From,
  To, Subject) of every new UID in every folder are fetched first, so copies
  of one email in several folders become one record whose primary copy is in
  the first folder. Bodies are then fetched `IMAP_BATCH_SIZE` at a time with a
  checkpoint after each batch; a crash resumes at the last acknowledged batch.
  Folder cursors only move once the whole run is delivered.
- **Failures:** a login or connection failure, or every folder failing, fails
  the run (failure heartbeat). One folder failing is skipped, keeps its
  cursor, and is reported as `coverage(status="partial", reasons=["source_error"])`.

## Limitations

- Threading without Gmail thread ids is by the `References` root. A reply
  that carries only `In-Reply-To` (no `References`) to a message that is not
  the root starts a new thread under its parent id; most mail clients send
  `References`, so this is rare.
- When a run finds a message in a folder read incrementally, the other
  folders are searched for the same Message-ID (`UID SEARCH HEADER
  Message-ID`), so every version lists all folders the message is in, as a
  fresh read of everything would. Messages without a Message-ID cannot be
  looked up this way and list only the folders seen in that run.
- A crash resumes at the acknowledged offset only when the source answers exactly as before; if anything changed in between, the run starts over from its start cursor (re-sending is harmless: unchanged records stay unchanged).
- Flag changes need CONDSTORE (Fastmail, Gmail, Dovecot and iCloud have it;
  capabilities are read after login, when servers announce it); without it
  the flags are as first fetched.
- Deletions and moves are not recorded.
- Attachments are metadata only; no content is downloaded or stored.
- OAuth (XOAUTH2) is not implemented; providers that require it for IMAP
  (Microsoft) are covered by the `msgraph` plugin.

## Tests

```sh
THROUGHLINE_DEV=1 uv run pytest -q plugins/imap
uv run ruff check plugins/imap
```

All tests are offline. `throughline_imap.fake.FakeIMAP` is an in-memory
imaplib-compatible mailbox loaded from `tests/recorded/mailbox.json`
(invented addresses at example.com); it answers in imaplib's shapes
(literal tuples, attributes after literals, quoted and literal folder
names). The tests cover the response parser, folder choice, mapping,
cross-folder dedupe, CONDSTORE flag updates, UIDVALIDITY resets, crash
resume, partial and total failure, Gmail extensions, and the SDK contract
kit (`IMAP_FIXTURE`).
