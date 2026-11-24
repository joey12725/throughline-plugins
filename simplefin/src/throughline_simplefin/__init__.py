"""SimpleFIN Bridge: bank, card and investment accounts and their transactions.

One access URL (claimed once from a setup token with
`throughline-simplefin-claim`) reads every account connected to the owner's
SimpleFIN Bridge. Each run asks `GET <access-url>/accounts` for transactions
in bounded date windows (pending ones included), walking forward from the
configured start date on the first run and re-reading a short overlap on
later runs, so a pending charge that posts is delivered again under the same
id as a new version. Accounts are reference records whose balance is
re-versioned every run it changes.

This is the most sensitive data a Throughline hub holds. Every record carries
`access_label` (default `financial`, a seeded label kind): non-owner askers do
not see it unless granted the label, and labelled content is held back from
remote model providers (see docs/v11/access.md and docs/v11/encryption.md).
"""

import logging
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx
from throughline_sdk import (
    Channel,
    Checkpoint,
    Declaration,
    Manifest,
    Plugin,
    coverage,
    run,
)

from . import recorded

__all__ = ["SimpleFIN", "main", "split_access_url"]

ACCOUNTS = "simplefin.accounts"
TRANSACTIONS = "simplefin.transactions"
DAY = 86400
log = logging.getLogger("throughline_simplefin")


def fields(**values):
    return [dict(name=k, type=v[0], description=v[1]) for k, v in values.items()]


ACCOUNT_REF = dict(
    account_id=("text", "SimpleFIN account id (unique on the Bridge)"),
    account_name=("text", "Account name as the institution reports it"),
    org_name=("text", "Institution name"),
    org_domain=("text", "Institution domain"),
    currency=("text", "ISO 4217 code, or a URL for a custom currency"),
)

ACCOUNT_FIELDS = fields(
    **ACCOUNT_REF,
    balance=("number", "Balance at balance_date"),
    available_balance=("number", "Available balance (credit left, cleared funds), if reported"),
    balance_date=("timestamp", "When the institution reported the balance"),
    holdings=("json", "Investment holdings when the account reports them"),
)

TRANSACTION_FIELDS = fields(
    **ACCOUNT_REF,
    transaction_id=("text", "Institution transaction id (unique within the account)"),
    amount=("number", "Amount: negative leaves the account, positive enters it"),
    direction=("text", "debit (money out) or credit (money in)"),
    payee=("text", "Payee, when the institution names one"),
    description=("text", "Institution description of the transaction"),
    memo=("text", "Memo, when present"),
    state=(
        "text",
        "pending, posted, or dropped (a pending transaction the Bridge no longer "
        "returns: declined, reversed, or posted under a new id)",
    ),
    posted_at=("timestamp", "When the transaction posted (empty while pending)"),
    transacted_at=("timestamp", "When the transaction happened, if reported"),
    time_source=(
        "text",
        "Which time happened_at is: transacted_at, posted, or balance_date (a "
        "pending transaction without a date; approximate)",
    ),
)

SETTINGS = {
    "access_url_file": Declaration(
        env="SIMPLEFIN_ACCESS_URL_FILE",
        required=False,
        description="File holding the access URL (written by throughline-simplefin-claim); "
        "alternative to SIMPLEFIN_ACCESS_URL",
    ),
    "start_date": Declaration(
        env="SIMPLEFIN_START_DATE",
        required=False,
        description="Earliest day (YYYY-MM-DD) the first run asks for; default 90 days ago",
    ),
    "window_days": Declaration(
        env="SIMPLEFIN_WINDOW_DAYS",
        required=False,
        default="60",
        description="Days per /accounts request (the Bridge limits a request's range)",
    ),
    "overlap_days": Declaration(
        env="SIMPLEFIN_OVERLAP_DAYS",
        required=False,
        default="14",
        description="Days before the last scanned day each later run reads again, so "
        "pending transactions that post are re-versioned",
    ),
    "accounts": Declaration(
        env="SIMPLEFIN_ACCOUNTS",
        required=False,
        description="Comma-separated account ids to read; default every account",
    ),
    "access_label": Declaration(
        env="SIMPLEFIN_ACCESS_LABEL",
        required=False,
        default="financial",
        description="access_label on every record (empty for none; not recommended)",
    ),
    "timezone": Declaration(
        env="SIMPLEFIN_TIMEZONE",
        required=False,
        default="UTC",
        description="IANA zone used to present times (SimpleFIN sends UNIX seconds)",
    ),
    "fixture": Declaration(
        env="SIMPLEFIN_FIXTURE",
        required=False,
        description="Tests only: JSON file served by an offline Bridge instead of the network",
    ),
}


class SourceUnavailable(RuntimeError):
    """The Bridge could not be read at all."""


def split_access_url(url):
    """(base URL without credentials, (user, password)) of an access URL."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ValueError("the SimpleFIN access URL is not an http(s) URL")
    if not parts.username:
        raise ValueError("the SimpleFIN access URL carries no credentials")
    host = parts.hostname + (f":{parts.port}" if parts.port else "")
    base = urlunsplit((parts.scheme, host, parts.path.rstrip("/"), "", ""))
    from urllib.parse import unquote

    return base, (unquote(parts.username), unquote(parts.password or ""))


def number(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class Mapper:
    def __init__(self, zone, access_label):
        self.zone = zone
        self.access_label = access_label or None

    def at(self, seconds):
        return datetime.fromtimestamp(int(seconds), tz=timezone.utc).astimezone(self.zone)

    def iso(self, seconds):
        return self.at(seconds).isoformat() if seconds else None

    @staticmethod
    def account_ref(account):
        org = account.get("org") or {}
        return dict(
            account_id=account["id"],
            account_name=account.get("name"),
            org_name=org.get("name") or org.get("domain"),
            org_domain=org.get("domain"),
            currency=account.get("currency"),
        )

    @staticmethod
    def thread(account):
        org = account.get("org") or {}
        label = org.get("name") or org.get("domain") or "SimpleFIN"
        return {
            "external_id": "account:" + account["id"],
            "title": f"{label} · {account.get('name') or account['id']}"[:300],
            "metadata": {"kind": "simplefin_account", "account_id": account["id"]},
        }

    @staticmethod
    def account_core(account):
        """The account without balance or transactions: a transaction's payload
        must not change when only the balance does."""
        keep = ("id", "name", "currency", "org")
        return {k: account[k] for k in keep if k in account}

    def account(self, account, fetched_at):
        ref = self.account_ref(account)
        when = account.get("balance-date") or fetched_at
        balance = number(account.get("balance"))
        available = number(account.get("available-balance"))
        currency = account.get("currency") or ""
        text = (
            f"{ref['account_name'] or ref['account_id']} at {ref['org_name'] or 'unknown institution'}: "
            f"balance {account.get('balance')} {currency}".rstrip()
        )
        if account.get("available-balance") not in (None, ""):
            text += f", available {account.get('available-balance')}"
        text += f" as of {self.at(when).date().isoformat()}"
        payload = {k: v for k, v in account.items() if k != "transactions"}
        return dict(
            channel=ACCOUNTS,
            external_id=account["id"],
            thread={"external_id": "accounts", "title": "SimpleFIN accounts"},
            happened_at=self.iso(when),
            content=text,
            fields=dict(
                ref,
                balance=balance,
                available_balance=available,
                balance_date=self.iso(account.get("balance-date")),
                holdings=account.get("holdings"),
            ),
            access_label=self.access_label,
            payload=payload,
        )

    def transaction(self, account, txn, dropped=False):
        ref = self.account_ref(account)
        posted = txn.get("posted") or 0
        transacted = txn.get("transacted_at") or 0
        if transacted:
            when, source = transacted, "transacted_at"
        elif posted:
            when, source = posted, "posted"
        else:
            when, source = account.get("balance-date") or 0, "balance_date"
        if not when:
            return None
        pending = bool(txn.get("pending")) or not posted
        state = "dropped" if dropped else ("pending" if pending else "posted")
        amount = number(txn.get("amount"))
        currency = account.get("currency") or ""
        payee = (txn.get("payee") or "").strip() or None
        description = (txn.get("description") or "").strip() or None
        memo = (txn.get("memo") or "").strip() or None
        head = payee or description or "Transaction"
        text = f"{head}: {txn.get('amount')} {currency}".rstrip()
        text += f" ({ref['account_name'] or ref['account_id']}"
        text += f" at {ref['org_name']})" if ref["org_name"] else ")"
        if state != "posted":
            text += f" [{state}]"
        for extra in (description, memo):
            if extra and extra != head:
                text += "\n" + extra
        payload = {"account": self.account_core(account), "transaction": txn}
        if dropped:
            payload["dropped"] = True
        message = dict(
            channel=TRANSACTIONS,
            external_id=f"{account['id']}:{txn['id']}",
            thread=self.thread(account),
            happened_at=self.iso(when),
            content=text,
            fields=dict(
                ref,
                transaction_id=str(txn["id"]),
                amount=amount,
                direction=None if amount is None else ("debit" if amount < 0 else "credit"),
                payee=payee,
                description=description,
                memo=memo,
                state=state,
                posted_at=self.iso(posted),
                transacted_at=self.iso(transacted),
                time_source=source,
            ),
            access_label=self.access_label,
            payload=payload,
        )
        if source == "balance_date":
            message["subject"] = {"start": message["happened_at"], "start_precision": "approximate"}
        return message


class SimpleFIN(Plugin):
    manifest = Manifest(
        name="simplefin",
        version="0.1.0",
        channels=[
            Channel(
                name=ACCOUNTS,
                description=(
                    "One reference record per account on the owner's SimpleFIN Bridge "
                    "(bank, card, loan, investment): institution, currency and balance; "
                    "a new version whenever the balance changes, so versions are the "
                    "balance history. Labelled financial."
                ),
                fields=ACCOUNT_FIELDS,
                is_reference=True,
                record_kind="reference",
                embed=False,
            ),
            Channel(
                name=TRANSACTIONS,
                description=(
                    "Every transaction of every SimpleFIN account: amount, currency, "
                    "payee, description and state (pending, posted, dropped); a pending "
                    "transaction that posts becomes a new version of the same record. "
                    "One thread per account. Labelled financial."
                ),
                fields=TRANSACTION_FIELDS,
                conversation_gap_s=6 * 3600,
                embed=False,
            ),
        ],
        settings=SETTINGS,
        secrets={
            "access_url": Declaration(
                env="SIMPLEFIN_ACCESS_URL",
                required=False,
                description="SimpleFIN access URL (contains credentials)",
            )
        },
        run_mode="poll",
        default_interval_s=6 * 3600,
        link_types=[],
    )

    def __init__(self, transport=None):
        super().__init__()
        self.transport = transport
        if self.settings["fixture"] and transport is None:
            self.transport = recorded.from_file(self.settings["fixture"])
        if not self.secrets.get("access_url") and self.settings["access_url_file"]:
            path = Path(self.settings["access_url_file"]).expanduser()
            self.secrets["access_url"] = path.read_text(encoding="utf-8").strip()
        if not self.secrets.get("access_url"):
            if self.transport is None:
                raise ValueError(
                    "set SIMPLEFIN_ACCESS_URL or SIMPLEFIN_ACCESS_URL_FILE "
                    "(claim one with throughline-simplefin-claim)"
                )
            self.secrets["access_url"] = "https://fixture:fixture@bridge.example.com/simplefin"
        self.base, self.auth = split_access_url(self.secrets["access_url"])
        self.window = timedelta(days=max(1, int(self.settings["window_days"] or 60)))
        self.overlap = timedelta(days=max(0, int(self.settings["overlap_days"] or 0)))
        zone = self.settings["timezone"] or "UTC"
        self.mapper = Mapper(ZoneInfo(zone), self.settings["access_label"])
        self.only = [a.strip() for a in (self.settings["accounts"] or "").split(",") if a.strip()]
        self.now = None  # Test seam.
        self.sleep = time.sleep

    # -- HTTP ---------------------------------------------------------------

    def fetch(self, http, start, end):
        params = [
            ("start-date", str(int(start.timestamp()))),
            ("end-date", str(int(end.timestamp()))),
            ("pending", "1"),
        ] + [("account", a) for a in self.only]
        for attempt in range(3):
            try:
                response = http.get(self.base + "/accounts", params=params)
            except httpx.TransportError as error:
                if attempt == 2:
                    raise SourceUnavailable(
                        f"SimpleFIN Bridge unreachable ({type(error).__name__})"
                    ) from None
                self.sleep(2**attempt)
                continue
            if response.status_code in (401, 403):
                raise SourceUnavailable(
                    "SimpleFIN refused the access URL (HTTP %d): it was revoked or "
                    "mistyped; claim a new one" % response.status_code
                )
            if response.status_code == 402:
                raise SourceUnavailable("SimpleFIN Bridge subscription inactive (HTTP 402)")
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == 2:
                    raise SourceUnavailable(f"SimpleFIN Bridge HTTP {response.status_code}")
                self.sleep(5 * 2**attempt)
                continue
            if response.is_error:
                raise SourceUnavailable(f"SimpleFIN Bridge HTTP {response.status_code}")
            try:
                return response.json()
            except ValueError:
                raise SourceUnavailable("SimpleFIN Bridge answered with non-JSON") from None
        raise SourceUnavailable("SimpleFIN Bridge retry limit reached")

    # -- run ----------------------------------------------------------------

    def first_start(self):
        if self.settings["start_date"]:
            day = date.fromisoformat(self.settings["start_date"])
            return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        return self.clock() - timedelta(days=90)

    def clock(self):
        return self.now or datetime.now(timezone.utc)

    def records(self, cursor, mode):
        now = self.clock()
        scanned = cursor.get("scanned_to")
        if scanned:
            start = datetime.fromisoformat(scanned) - self.overlap
        else:
            start = self.first_start()
        pending = dict(cursor.get("pending") or {})
        high_water = cursor.get("high_water")
        accounts, errors = {}, []
        seen = set()
        scan_from = start
        end_of_scan = now + timedelta(days=1)
        with httpx.Client(
            auth=self.auth, timeout=120, transport=self.transport, follow_redirects=False
        ) as http:
            window_start = start
            while window_start < end_of_scan:
                window_end = min(window_start + self.window, end_of_scan)
                answer = self.fetch(http, window_start, window_end)
                errors.extend(answer.get("errors") or [])
                batch = []
                for account in answer.get("accounts") or []:
                    if not isinstance(account, dict) or not account.get("id"):
                        continue
                    accounts[account["id"]] = account
                    for txn in account.get("transactions") or []:
                        if not isinstance(txn, dict) or txn.get("id") in (None, ""):
                            continue
                        message = self.mapper.transaction(account, txn)
                        if message is None or message["external_id"] in seen:
                            continue
                        seen.add(message["external_id"])
                        batch.append((message, account, txn))
                batch.sort(key=lambda item: (item[0]["happened_at"], item[0]["external_id"]))
                for message, account, txn in batch:
                    yield message
                    if message["fields"]["state"] == "pending":
                        pending[message["external_id"]] = {
                            "account": self.mapper.account_core(account),
                            "transaction": txn,
                        }
                    else:
                        pending.pop(message["external_id"], None)
                    if message["fields"]["posted_at"]:
                        posted = message["fields"]["posted_at"]
                        high_water = max(high_water, posted) if high_water else posted
                scanned_to = min(window_end, now)
                yield Checkpoint(
                    cursor={
                        "scanned_to": scanned_to.isoformat(),
                        "high_water": high_water,
                        "pending": pending,
                    }
                )
                window_start = window_end
        # A pending transaction inside the window read again that the Bridge
        # no longer returns was declined, reversed or posted under a new id.
        for external_id, kept in sorted(pending.items()):
            if external_id in seen:
                continue
            txn = kept["transaction"]
            when = txn.get("transacted_at") or txn.get("posted") or 0
            if when and when >= int(scan_from.timestamp()):
                message = self.mapper.transaction(kept["account"], txn, dropped=True)
                if message:
                    yield message
                pending.pop(external_id)
        fetched_at = int(now.timestamp())
        for account in sorted(accounts.values(), key=lambda a: a["id"]):
            yield self.mapper.account(account, fetched_at)
        if errors:
            text = [str(e.get("message") if isinstance(e, dict) else e)[:200] for e in errors]
            log.warning("SimpleFIN reported %d connection problem(s)", len(text))
            for name in (ACCOUNTS, TRANSACTIONS):
                yield coverage(
                    channel=name,
                    status="partial",
                    reasons=["source_error"],
                    detail={"errors": sorted(set(text))[:5]},
                )
        yield Checkpoint(
            cursor={
                "scanned_to": min(end_of_scan, now).isoformat(),
                "high_water": high_water,
                "pending": pending,
            }
        )


def main(argv=None):
    run(SimpleFIN, argv)
