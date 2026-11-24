"""Generic IMAP mail (Fastmail, iCloud, Proton Mail Bridge, Gmail, Dovecot, ...).

One Throughline message per email in `imap.message`, with the same field
names, thread modelling and author shape as the Gmail connector
(plugins/email). Read-only: mailboxes are EXAMINEd and bodies fetched with
BODY.PEEK[], so nothing is marked read.

Each run, per folder: UIDVALIDITY + last UID cursor (a changed UIDVALIDITY
re-reads the folder), optionally CONDSTORE flag changes. A planning pass
fetches only the key headers of new UIDs, so the same email found in two
folders (Inbox and Sent when you mail yourself) is one record with both
folders; the bodies are then fetched in batches with a checkpoint after
each.
"""

import copy
import hashlib
import imaplib
import json
import logging
import ssl
from datetime import date

from throughline_sdk import Channel, Checkpoint, Declaration, Manifest, Plugin, coverage, run

from . import mailmap, parse
from .protocol import ImapError, Session, internal_date, quote, search_date

LOG = logging.getLogger("throughline_imap")
CHANNEL = "imap.message"
# 1: first mapping.
MAPPING_REVISION = 1

EXTRA_FIELDS = [
    ("imap_uids", "json", "{folder: UID} where the message was found"),
    ("gmail_thread_id", "text", "Gmail thread id (hex, as in the Gmail API) when the server is Gmail"),
    ("gmail_message_id", "text", "Gmail message id (hex, as in the Gmail API) when the server is Gmail"),
]
SENT_NAMES = {"sent", "sent items", "sent messages", "sent mail", "[gmail]/sent mail", "inbox.sent", "inbox/sent"}
DRAFT_NAMES = {"drafts", "draft", "inbox.drafts", "[gmail]/drafts"}

SETTINGS = [
    ("host", "IMAP_HOST", None, "IMAP server host name (e.g. imap.fastmail.com)"),
    ("port", "IMAP_PORT", "993", "IMAP port (993 for TLS, 143 or 1143 for STARTTLS)"),
    ("security", "IMAP_SECURITY", "ssl", "ssl (implicit TLS) or starttls"),
    ("ca_file", "IMAP_CA_FILE", None, "Extra CA bundle (PEM), e.g. Proton Mail Bridge's exported certificate"),
    ("username", "IMAP_USERNAME", None, "Login name, usually the email address"),
    ("account", "IMAP_ACCOUNT", None, "Mailbox label used in ids (default: the username, lower case)"),
    ("owner", "IMAP_OWNER_ADDRESSES", None, "Comma-separated addresses that are you (default: the username)"),
    ("folders", "IMAP_FOLDERS", None, "Comma-separated folders, or special-use roles like \\Sent (default: All Mail when the server has one, else INBOX + Sent)"),
    ("exclude", "IMAP_EXCLUDE_FOLDERS", None, "Comma-separated folders to skip"),
    ("since", "IMAP_SINCE", None, "First run only: skip mail before this date (YYYY-MM-DD)"),
    ("max_messages", "IMAP_MAX_MESSAGES", "0", "First run only: newest N messages per folder (0 = all)"),
    ("flag_updates", "IMAP_FLAG_UPDATES", "true", "Re-read messages whose flags changed (needs CONDSTORE)"),
    ("batch_size", "IMAP_BATCH_SIZE", "100", "Messages per body fetch and checkpoint"),
    ("body_limit", "IMAP_BODY_LIMIT", str(mailmap.DEFAULT_BODY_LIMIT), "Body characters kept in content (0 = all)"),
    ("timeout", "IMAP_TIMEOUT_S", "60", "Socket timeout in seconds"),
    ("fixture", "IMAP_FIXTURE", None, "Tests only: JSON mailbox served by an in-memory fake instead of a server"),
]


def _split(value):
    return [v.strip() for v in str(value or "").split(",") if v.strip()]


def _truthy(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _role(folder):
    name = folder["name"].lower()
    if folder.get("special") == "\\Sent" or name in SENT_NAMES:
        return "sent"
    if folder.get("special") == "\\Drafts" or name in DRAFT_NAMES:
        return "drafts"
    return folder.get("special") or ("inbox" if name == "inbox" else None)


def choose_folders(listing, configured=(), exclude=()):
    """Folders to read, in priority order (the first holds a message's primary copy)."""
    excluded = {e.lower() for e in exclude}

    def keep(f):
        return f["name"].lower() not in excluded and f["display"].lower() not in excluded

    listing = [f for f in listing if keep(f)]
    chosen = []
    if configured:
        for token in configured:
            for f in listing:
                hit = (
                    (token.startswith("\\") and (f.get("special") or "").lower() == token.lower())
                    or f["name"] == token
                    or f["display"] == token
                    or (token.upper() == "INBOX" and f["name"].upper() == "INBOX")
                )
                if hit and f not in chosen:
                    chosen.append(f)
        return chosen
    every = [f for f in listing if f.get("special") == "\\All"]
    if every:
        return every[:1]
    inbox = [f for f in listing if f["name"].upper() == "INBOX"]
    sent = [f for f in listing if f.get("special") == "\\Sent"] or [
        f for f in listing if f["name"].lower() in SENT_NAMES
    ]
    return inbox[:1] + sent[:1]


def _hex(value):
    try:
        return format(int(str(value)), "x")
    except (TypeError, ValueError):
        return None


def _labels(value):
    if not isinstance(value, list):
        return []
    return [v.decode() if isinstance(v, bytes) else str(v) for v in value if v is not None]


def _header_block(attrs):
    for key, value in attrs.items():
        if key.startswith("BODY[HEADER.FIELDS") and isinstance(value, bytes):
            return value
    return None


class Imap(Plugin):
    manifest = Manifest(
        name="imap",
        version="0.1.0",
        default_interval_s=900,
        channels=[
            Channel(
                name=CHANNEL,
                description=(
                    "Email from an IMAP mailbox, one record per message (deduplicated across "
                    "folders by Message-ID); threads follow Gmail thread ids or the "
                    "References/In-Reply-To root; happened_at is the server's received time"
                ),
                fields=[
                    dict(name=n, type=t, description=d)
                    for n, t, d in mailmap.COMMON_FIELDS + EXTRA_FIELDS
                ],
                conversation_whole_thread=True,
            )
        ],
        settings={
            key: Declaration(env=env, required=False, default=default, description=text)
            for key, env, default, text in SETTINGS
        },
        secrets={
            "password": Declaration(
                env="IMAP_PASSWORD",
                required=False,
                description="Password or app-specific password (Fastmail, iCloud, Gmail) or the Bridge password",
            )
        },
        run_mode="poll",
    )

    # Test seam: a callable returning an imaplib.IMAP4-like object.
    connector = None

    def __init__(self):
        super().__init__()
        s = self.settings
        self.batch_size = int(s["batch_size"])
        if self.batch_size < 1:
            raise ValueError("IMAP_BATCH_SIZE must be positive")
        self.body_limit = int(s["body_limit"])
        self.max_messages = int(s["max_messages"])
        self.since = date.fromisoformat(s["since"]) if s.get("since") else None
        username = (s.get("username") or "").strip()
        self.account = (s.get("account") or username or "imap").strip().lower()
        owner = _split(s.get("owner")) or ([username] if "@" in username else [])
        self.owner = [o.lower() for o in owner]
        if s["security"] not in ("ssl", "starttls"):
            raise ValueError("IMAP_SECURITY must be ssl or starttls")

    # --- connection ------------------------------------------------------------

    def connect(self):
        s = self.settings
        if self.connector is not None:
            conn = self.connector()
        elif s.get("fixture"):
            from .fake import FakeIMAP

            conn = FakeIMAP.from_file(s["fixture"])
        else:
            if not s.get("host") or not s.get("username"):
                raise ValueError("IMAP_HOST and IMAP_USERNAME are required")
            context = ssl.create_default_context(cafile=s.get("ca_file") or None)
            timeout = float(s["timeout"])
            if s["security"] == "ssl":
                conn = imaplib.IMAP4_SSL(s["host"], int(s["port"]), ssl_context=context, timeout=timeout)
            else:
                conn = imaplib.IMAP4(s["host"], int(s["port"]), timeout=timeout)
                conn.starttls(ssl_context=context)
        try:
            conn.login(s.get("username") or "", self.secrets.get("password") or "")
        except imaplib.IMAP4.error as error:
            raise ImapError(f"IMAP login failed for {self.account}: {error}") from None
        return Session(conn)

    # --- run ---------------------------------------------------------------------

    def records(self, cursor, mode):
        state = copy.deepcopy(cursor or {})
        if state.get("mapping_revision") != MAPPING_REVISION:
            # A new mapping re-reads every folder; unchanged mail stays unchanged.
            state = {"mapping_revision": MAPPING_REVISION}
        resume = state.pop("resume", None)
        base = copy.deepcopy(state)
        session = self.connect()
        try:
            yield from self._run(session, state, base, resume)
        finally:
            session.close()

    def _folder_plan(self, session, folder, previous):
        """(uids to read, new folder state) for one folder."""
        status = session.status(folder["name"])
        validity = status.get("UIDVALIDITY")
        if previous and previous.get("uidvalidity") != validity:
            LOG.warning("UIDVALIDITY of %s changed; re-reading it", folder["display"])
            previous = None
        session.examine(folder["name"])
        if previous:
            last = int(previous.get("uid") or 0)
            new = [u for u in session.search("UID", f"{last + 1}:*") if u > last]
            changed = []
            modseq, known = status.get("HIGHESTMODSEQ"), previous.get("modseq")
            if _truthy(self.settings["flag_updates"]) and session.condstore and known and modseq and modseq != known:
                changed = session.changed_since(last, known)
            uids = sorted(set(new) | set(changed))
            top = max([last] + new)
        else:
            criteria = ("SINCE", search_date(self.since)) if self.since else ("ALL",)
            uids = session.search(*criteria)
            if self.max_messages > 0:
                uids = uids[-self.max_messages :]
            top = max(uids) if uids else max(0, status.get("UIDNEXT", 1) - 1)
        return uids, {
            "uidvalidity": validity,
            "uid": top,
            "modseq": status.get("HIGHESTMODSEQ"),
        }, previous is None

    def _run(self, session, state, base, resume):
        configured = _split(self.settings.get("folders"))
        chosen = choose_folders(session.folders(), configured, _split(self.settings.get("exclude")))
        if not chosen:
            raise ImapError("no IMAP folder matched IMAP_FOLDERS")
        known = state.setdefault("folders", {})
        items = "(UID FLAGS INTERNALDATE BODY.PEEK[HEADER.FIELDS (" + " ".join(parse.KEY_HEADERS) + ")])"
        plan, updated, failed, incremental = {}, {}, [], []
        for order, folder in enumerate(chosen):
            try:
                uids, new_state, fresh = self._folder_plan(session, folder, known.get(folder["name"]))
                meta = session.fetch(uids, items) if uids else {}
            except (ImapError, imaplib.IMAP4.error) as error:
                LOG.warning("IMAP folder %s skipped: %s", folder["display"], error)
                failed.append(folder)
                continue
            for uid in sorted(meta):
                header = _header_block(meta[uid])
                if header is None:
                    continue
                key, _ = parse.record_key(header)
                at = internal_date(meta[uid].get("INTERNALDATE"))
                plan.setdefault(key, []).append((order, uid, at.isoformat() if at else ""))
            updated[folder["name"]] = new_state
            if not fresh:
                incremental.append(order)
        if failed and len(failed) == len(chosen):
            raise ImapError("every IMAP folder failed; see the warnings above")
        if len(chosen) > 1:
            self._other_copies(session, chosen, incremental, plan, failed)
        entries = sorted(
            ((key, sorted(locs)) for key, locs in plan.items()),
            key=lambda entry: (entry[1][0][2], entry[0]),
        )
        signature = hashlib.sha256(
            json.dumps([base.get("folders"), [[k, [loc[:2] for loc in locs]] for k, locs in entries]]).encode()
        ).hexdigest()
        offset = resume.get("offset", 0) if resume and resume.get("signature") == signature else 0
        for start in range(offset, len(entries), self.batch_size):
            chunk = entries[start : start + self.batch_size]
            bodies = self._bodies(session, chosen, chunk)
            for key, locs in chunk:
                message = self.map(key, locs, chosen, bodies)
                if message is not None:
                    yield message
            if start + self.batch_size < len(entries):
                interim = copy.deepcopy(base)
                interim["resume"] = {"signature": signature, "offset": start + self.batch_size}
                yield Checkpoint(cursor=interim)
        for folder in failed:
            yield coverage(
                CHANNEL,
                scope_key=f"{self.account}:{folder['display']}",
                status="partial",
                reasons=["source_error"],
            )
        known.update(updated)
        yield Checkpoint(cursor=state)

    def _other_copies(self, session, chosen, incremental, plan, failed):
        """Find earlier copies of this run's messages in folders read only
        incrementally, so a version lists every folder the message is in (a
        fresh read of a folder already put its copies in the plan)."""
        items = "(UID INTERNALDATE BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])"
        for order in incremental:
            folder = chosen[order]
            if folder in failed:
                continue
            wanted = sorted(
                key for key, locs in plan.items() if not key.startswith("sha256:") and all(loc[0] != order for loc in locs)
            )
            if not wanted:
                continue
            try:
                session.examine(folder["name"])
                hits = set()
                for key in wanted:
                    hits.update(session.search("HEADER", "Message-ID", quote("<" + key + ">")))
                meta = session.fetch(hits, items) if hits else {}
            except (ImapError, imaplib.IMAP4.error) as error:
                LOG.warning("IMAP folder %s: looking up other copies failed: %s", folder["display"], error)
                failed.append(folder)
                continue
            for uid in sorted(meta):
                header = _header_block(meta[uid])
                key = parse.record_key(header)[0] if header else None
                if key in plan and all(loc[0] != order for loc in plan[key]):
                    at = internal_date(meta[uid].get("INTERNALDATE"))
                    plan[key].append((order, uid, at.isoformat() if at else ""))

    def _bodies(self, session, chosen, chunk):
        """{(order, uid): attrs} with the full message of each primary copy."""
        wanted = {}
        for _, locs in chunk:
            order, uid, _ = locs[0]
            wanted.setdefault(order, []).append(uid)
        items = "(UID FLAGS INTERNALDATE RFC822.SIZE BODY.PEEK[]"
        if session.gmail:
            items += " X-GM-THRID X-GM-MSGID X-GM-LABELS"
        items += ")"
        result = {}
        for order in sorted(wanted):
            session.examine(chosen[order]["name"])
            for uid, attrs in session.fetch(wanted[order], items).items():
                result[(order, uid)] = attrs
        return result

    def map(self, key, locs, chosen, bodies):
        order, uid, _ = locs[0]
        attrs = bodies.get((order, uid))
        raw = (attrs or {}).get("BODY[]")
        if not isinstance(raw, bytes):
            LOG.warning("message vanished before its body was read; skipped")
            return None
        try:
            mail = parse.normalize(
                raw,
                account=self.account,
                key=key,
                internal=internal_date(attrs.get("INTERNALDATE")),
                size=int(attrs["RFC822.SIZE"]) if str(attrs.get("RFC822.SIZE", "")).isdigit() else None,
            )
        except ValueError as error:
            LOG.warning("message skipped: %s", error)
            return None
        # \Recent is per session: keeping it would make every read a new version.
        flags = [f for f in _labels(attrs.get("FLAGS")) if f.lower() != "\\recent"]
        lowered = {f.lower() for f in flags}
        gm_labels = _labels(attrs.get("X-GM-LABELS"))
        folders = [chosen[o] for o, _, _ in locs]
        roles = {_role(f) for f in folders}
        keywords = [f for f in flags if not f.startswith("\\")]
        mail.update(
            folders=[f["display"] for f in folders],
            labels=gm_labels + keywords,
            is_unread="\\seen" not in lowered,
            is_starred="\\flagged" in lowered,
            is_important="\\Important" in gm_labels or "$important" in lowered,
            is_draft="\\draft" in lowered or "drafts" in roles or "\\Draft" in gm_labels,
            in_sent="sent" in roles or "\\Sent" in gm_labels,
            thread_id=("gm:" + _hex(attrs["X-GM-THRID"])) if attrs.get("X-GM-THRID") and _hex(attrs["X-GM-THRID"]) else None,
        )
        extra = dict(
            imap_uids={chosen[o]["display"]: u for o, u, _ in locs},
            gmail_thread_id=_hex(attrs.get("X-GM-THRID")) if attrs.get("X-GM-THRID") else None,
            gmail_message_id=_hex(attrs.get("X-GM-MSGID")) if attrs.get("X-GM-MSGID") else None,
        )
        return mailmap.map_mail(
            mail,
            channel=CHANNEL,
            revision=MAPPING_REVISION,
            owner=self.owner,
            body_limit=self.body_limit,
            extra_fields=extra,
            extra_payload={"flags": flags, "imap_uids": extra["imap_uids"]},
        )


def main():
    run(Imap)
