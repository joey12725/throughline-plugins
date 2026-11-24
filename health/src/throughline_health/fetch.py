#!/usr/bin/env python3
"""Standard-library health fetcher: Oura v2 and the Weight Gurus scale to JSONL.

    python fetch.py export --out FILE_OR_DIR [--sources oura,weightgurus]
                           [--start YYYY-MM-DD] [--end YYYY-MM-DD] [--state FILE]
    python fetch.py check

Runs on Python 3.12+ with nothing but the standard library, so it can be copied
to the host that holds the credentials. Credentials come only from environment
variables (never argv) and are never printed or written: see README.md.

Every output line is one JSON object:
    {"kind": "oura.daily_sleep", "id": "<native id>", "provider": "oura",
     "fetched_at": "<UTC ISO>", "payload": {...unchanged API document...}}
Heart rate samples are grouped into one record per UTC day (kind
"oura.heartrate", id = the day). Weight Gurus operations are "weightgurus.operation".
"export.window" records mark a completed fetch window; "export.end" closes a
file and says whether the export finished or stopped at a request limit.
Records are written only for complete windows, so a day is never half-fetched.
"""

import argparse
import contextlib
import http.client
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

FORMAT_VERSION = 1
USER_AGENT = "throughline-health-fetch/1"
OURA_BASE = "https://api.ouraring.com"
OURA_TOKEN_URL = "https://api.ouraring.com/oauth/token"
WEIGHTGURUS_BASE = "https://api.weightgurus.com"
WEIGHTGURUS_START = "1970-01-01T01:00:00.504Z"
DEFAULT_START = "2015-01-01"
# Daily documents keyed by "day"; merged into one daily summary per day.
OURA_DAILY = (
    "daily_sleep",
    "daily_readiness",
    "daily_activity",
    "daily_stress",
    "daily_spo2",
    "daily_resilience",
    "daily_cardiovascular_age",
    "vO2_max",
)
OURA_EVENTS = ("sleep", "session", "workout")
OURA_COLLECTIONS = OURA_DAILY + OURA_EVENTS + ("heartrate",)
HEARTRATE_MAX_DAYS = 30
MAX_PAGES = 1000


class FetchError(Exception):
    """A failure whose message never contains a credential."""


class HttpError(FetchError):
    def __init__(self, status, where):
        super().__init__(f"{where} failed (HTTP {status})")
        self.status = status


class AuthError(FetchError):
    pass


class BudgetExhausted(FetchError):
    pass


def log(message):
    print(message, file=sys.stderr, flush=True)


def utc_now():
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def check_base(url, name):
    """HTTPS only, except a loopback address used by local fakes and tunnels."""
    parsed = urllib.parse.urlsplit(url)
    local = parsed.hostname in ("127.0.0.1", "localhost", "::1")
    if (
        parsed.scheme not in ("https", "http")
        or (parsed.scheme == "http" and not local)
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise FetchError(f"{name} must be an https URL without credentials or query")
    return url.rstrip("/")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: a redirect could carry the bearer token elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Http:
    """JSON over urllib with a request budget, pacing and retry with backoff.

    429, 5xx and transport failures are retried with exponential backoff,
    honouring Retry-After (capped). Every attempt counts against the budget.
    """

    def __init__(
        self,
        *,
        max_requests=None,
        min_interval=0.0,
        retries=5,
        backoff=1.0,
        max_backoff=60.0,
        timeout=60.0,
        sleep=time.sleep,
        clock=time.monotonic,
    ):
        self.max_requests = max_requests
        self.min_interval = float(min_interval)
        self.retries = int(retries)
        self.backoff = float(backoff)
        self.max_backoff = float(max_backoff)
        self.timeout = float(timeout)
        self.sleep = sleep
        self.clock = clock
        self.count = 0
        self._last = None
        self.opener = urllib.request.build_opener(_NoRedirect)

    def _pace(self):
        if self._last is not None and self.min_interval > 0:
            wait = self.min_interval - (self.clock() - self._last)
            if wait > 0:
                self.sleep(wait)
        self._last = self.clock()

    def call(self, method, url, *, headers=None, json_body=None, form=None, where):
        body = None
        headers = dict(headers or {})
        headers.setdefault("Accept", "application/json")
        headers["User-Agent"] = USER_AGENT
        if json_body is not None:
            body = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        elif form is not None:
            body = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        for attempt in range(self.retries + 1):
            if self.max_requests is not None and self.count >= self.max_requests:
                raise BudgetExhausted(f"request limit reached before {where}")
            self._pace()
            self.count += 1
            request = urllib.request.Request(
                url, data=body, headers=headers, method=method
            )
            retry_after = None
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    status, data = response.status, response.read()
            except urllib.error.HTTPError as error:
                status = error.code
                retry_after = (
                    error.headers.get("Retry-After") if error.headers else None
                )
                with contextlib.suppress(Exception):
                    error.read()
                error.close()
            except (
                urllib.error.URLError,
                http.client.HTTPException,
                ConnectionError,
                TimeoutError,
                socket.timeout,
            ):
                status = None
            if status is not None and 200 <= status < 300:
                try:
                    return json.loads(data) if data.strip() else None
                except ValueError:
                    raise FetchError(f"{where} returned invalid JSON") from None
            if status is None or status == 429 or status >= 500:
                if attempt < self.retries:
                    self.sleep(self._delay(attempt, retry_after))
                    continue
                if status is None:
                    raise FetchError(f"{where} failed (transport error)")
            raise HttpError(status, where)
        raise FetchError(f"{where} failed")  # pragma: no cover

    def _delay(self, attempt, retry_after):
        delay = min(self.max_backoff, self.backoff * 2**attempt)
        try:
            if retry_after is not None:
                delay = max(delay, min(float(retry_after), self.max_backoff * 2))
        except ValueError:
            pass
        return delay


# --------------------------------------------------------------------- Oura


class OuraAuth:
    """Bearer token for Oura without ever rotating someone else's refresh token.

    OURA_TOKEN_FILE (re-read at run time and after a 401, so a rotation by the
    owning app is picked up) takes precedence over OURA_ACCESS_TOKEN. Refreshing
    happens only with OURA_ALLOW_REFRESH=1 plus this installation's own
    OURA_CLIENT_ID and OURA_CLIENT_SECRET; the new pair is then written back to
    the token file atomically under a lock file, or kept in memory only.
    """

    def __init__(self, env, http):
        self.http = http
        self.env_token = env.get("OURA_ACCESS_TOKEN") or None
        self.file = env.get("OURA_TOKEN_FILE") or None
        self.allow_refresh = (env.get("OURA_ALLOW_REFRESH") or "0").strip() == "1"
        self.client_id = env.get("OURA_CLIENT_ID") or None
        self.client_secret = env.get("OURA_CLIENT_SECRET") or None
        self.env_refresh = env.get("OURA_REFRESH_TOKEN") or None
        self.token_url = check_base(
            env.get("OURA_TOKEN_URL") or OURA_TOKEN_URL, "OURA_TOKEN_URL"
        )
        self.current = None
        self.refreshed = None

    def configured(self):
        return bool(self.env_token or self.file)

    def _read_file(self):
        try:
            text = Path(self.file).read_text(encoding="utf-8").strip()
        except OSError:
            raise AuthError("OURA_TOKEN_FILE could not be read") from None
        if text.startswith("{"):
            try:
                data = json.loads(text)
            except ValueError:
                raise AuthError("OURA_TOKEN_FILE is not valid JSON") from None
            return data
        return {"access_token": text}

    def token(self):
        if self.current is None:
            if self.refreshed:
                self.current = self.refreshed
            elif self.file:
                data = self._read_file()
                self.current = data.get("access_token") or None
                expires = data.get("expires_at")
                if self.allow_refresh and _expired(expires):
                    self.current = self.refresh()
            else:
                self.current = self.env_token
            if not self.current:
                raise AuthError(
                    "no Oura access token: set OURA_ACCESS_TOKEN or OURA_TOKEN_FILE"
                )
        return self.current

    def unauthorized(self, used):
        """A replacement token after a 401, or None when there is no safe one."""
        if self.file:
            fresh = self._read_file().get("access_token")
            if fresh and fresh != used:
                self.current = fresh
                return fresh
        if self.allow_refresh:
            self.current = self.refresh()
            return self.current
        return None

    def refresh(self):
        if not (self.client_id and self.client_secret):
            raise AuthError(
                "OURA_ALLOW_REFRESH=1 needs this installation's own OURA_CLIENT_ID and OURA_CLIENT_SECRET"
            )
        with _lock(self.file + ".lock" if self.file else None):
            data = self._read_file() if self.file else {}
            if (
                self.file
                and data.get("access_token")
                and data.get("access_token") != self.current
                and not _expired(data.get("expires_at"))
            ):
                # Another process refreshed while we waited for the lock.
                return data["access_token"]
            refresh_token = data.get("refresh_token") or self.env_refresh
            if not refresh_token:
                raise AuthError(
                    "Oura refresh needs a refresh token (OURA_REFRESH_TOKEN or the token file)"
                )
            try:
                answer = self.http.call(
                    "POST",
                    self.token_url,
                    form={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                    },
                    where="Oura token refresh",
                )
            except HttpError as error:
                raise AuthError(
                    f"Oura token refresh failed (HTTP {error.status})"
                ) from None
            access = (answer or {}).get("access_token")
            if not access:
                raise AuthError("Oura token refresh returned no access token")
            if self.file:
                data = dict(data)
                data["access_token"] = access
                if answer.get("refresh_token"):
                    data["refresh_token"] = answer["refresh_token"]
                if answer.get("expires_in"):
                    data["expires_in"] = answer["expires_in"]
                    data["expires_at"] = int(time.time()) + int(answer["expires_in"])
                data["updated_at"] = utc_now()
                _atomic_write(self.file, json.dumps(data, indent=2) + "\n")
            else:
                self.env_refresh = answer.get("refresh_token") or self.env_refresh
            self.refreshed = access
            return access


def _expired(value):
    try:
        return value is not None and float(value) <= time.time() + 60
    except (TypeError, ValueError):
        return False


@contextlib.contextmanager
def _lock(path, timeout=30.0):
    if not path:
        yield
        return
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError:
            with contextlib.suppress(OSError):
                if time.time() - os.path.getmtime(path) > 120:
                    os.unlink(path)
                    continue
            if time.monotonic() > deadline:
                raise AuthError(
                    "timed out waiting for the Oura token file lock"
                ) from None
            time.sleep(0.2)
    try:
        yield
    finally:
        os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(path)


def _atomic_write(path, text):
    temporary = f"{path}.{os.getpid()}.tmp"
    fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(temporary, path)


class Oura:
    def __init__(self, http, auth, base=OURA_BASE):
        self.http = http
        self.auth = auth
        self.base = check_base(base, "OURA_API_BASE")

    def get(self, path, params):
        url = f"{self.base}/v2/usercollection/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        where = f"Oura {path.split('?')[0]}"
        token = self.auth.token()
        try:
            return self.http.call(
                "GET", url, headers={"Authorization": "Bearer " + token}, where=where
            )
        except HttpError as error:
            if error.status != 401:
                raise
        replacement = self.auth.unauthorized(token)
        if not replacement:
            raise AuthError(
                "Oura rejected the access token (HTTP 401); refresh is disabled, so update "
                "OURA_ACCESS_TOKEN or OURA_TOKEN_FILE (or set OURA_ALLOW_REFRESH=1 with your own client)"
            )
        try:
            return self.http.call(
                "GET",
                url,
                headers={"Authorization": "Bearer " + replacement},
                where=where,
            )
        except HttpError as error:
            if error.status == 401:
                raise AuthError(
                    "Oura rejected the replacement access token (HTTP 401)"
                ) from None
            raise

    def pages(self, collection, params):
        params = dict(params)
        seen = set()
        for _ in range(MAX_PAGES):
            page = self.get(collection, params) or {}
            yield from page.get("data") or []
            token = page.get("next_token")
            if not token:
                return
            if token in seen:
                # A repeated token would loop; stopping would silently drop
                # the rest of the window, so fail it instead.
                raise FetchError(f"Oura {collection} pagination repeated a page token")
            seen.add(token)
            params["next_token"] = token
        raise FetchError(f"Oura {collection} pagination exceeded {MAX_PAGES} pages")

    def documents(self, collection, start, end):
        """Documents of one collection whose day lies in [start, end] (inclusive).

        end_date is sent one day later and results are filtered by day, so the
        window is complete whether the API treats end_date as inclusive or not.
        """
        if collection == "heartrate":
            items = []
            cursor = start
            while cursor <= end:
                stop = min(end, cursor + timedelta(days=HEARTRATE_MAX_DAYS - 1))
                items.extend(
                    self.pages(
                        "heartrate",
                        {
                            "start_datetime": f"{cursor.isoformat()}T00:00:00+00:00",
                            "end_datetime": f"{(stop + timedelta(days=1)).isoformat()}T00:00:00+00:00",
                        },
                    )
                )
                cursor = stop + timedelta(days=1)
            return [
                i
                for i in items
                if start.isoformat() <= utc_day(i.get("timestamp")) <= end.isoformat()
            ]
        docs = self.pages(
            collection,
            {
                "start_date": start.isoformat(),
                "end_date": (end + timedelta(days=1)).isoformat(),
            },
        )
        return [
            d
            for d in docs
            if start.isoformat() <= str(d.get("day", "")) <= end.isoformat()
        ]


def utc_day(value):
    """The UTC date (YYYY-MM-DD) of an ISO timestamp, or "" when unusable."""
    try:
        at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return ""
    if at.tzinfo is not None:
        at = at.astimezone(UTC)
    return at.date().isoformat()


def heartrate_days(items):
    """Group samples by UTC day; samples sorted and unique within a day."""
    days = {}
    for item in items:
        days.setdefault(utc_day(item.get("timestamp")), {})[
            (str(item.get("timestamp")), str(item.get("source")))
        ] = item
    return {
        day: [samples[k] for k in sorted(samples)]
        for day, samples in sorted(days.items())
    }


def record(kind, native_id, provider, payload, fetched_at):
    return {
        "kind": kind,
        "id": str(native_id),
        "provider": provider,
        "fetched_at": fetched_at,
        "payload": payload,
    }


def oura_window(oura, collections, start, end, skipped):
    """All records of one window, or an exception; never a partial window.

    A collection refused with 403/404 (missing scope, endpoint not offered for
    this account) is skipped for the rest of the run and named in the marker.
    """
    fetched_at = utc_now()
    out = []
    for collection in collections:
        if collection in skipped:
            continue
        try:
            docs = oura.documents(collection, start, end)
        except HttpError as error:
            if error.status in (403, 404):
                skipped.add(collection)
                log(f"oura: skipping {collection} (HTTP {error.status})")
                continue
            raise
        if collection == "heartrate":
            for day, samples in heartrate_days(docs).items():
                out.append(
                    record(
                        "oura.heartrate",
                        day,
                        "oura",
                        {"day": day, "items": samples},
                        fetched_at,
                    )
                )
            continue
        for doc in docs:
            native = doc.get("id") or f"{collection}:{doc.get('day')}"
            out.append(record("oura." + collection, native, "oura", doc, fetched_at))
    out.append(
        record(
            "export.window",
            f"oura:{start.isoformat()}:{end.isoformat()}",
            "oura",
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "collections": [c for c in collections if c not in skipped],
                "skipped": sorted(skipped & set(collections)),
            },
            fetched_at,
        )
    )
    return out


# -------------------------------------------------------------- Weight Gurus


class WeightGurus:
    """Unofficial Weight Gurus API: password login, token kept in memory only."""

    def __init__(self, http, email, password, base=WEIGHTGURUS_BASE):
        self.http = http
        self.email = email
        self.password = password
        self.base = check_base(base, "WEIGHTGURUS_API_BASE")

    def login(self):
        try:
            answer = self.http.call(
                "POST",
                self.base + "/v3/account/login",
                json_body={
                    "email": self.email,
                    "password": self.password,
                    "web": "true",
                },
                where="Weight Gurus login",
            )
        except HttpError as error:
            if error.status in (400, 401, 403):
                raise AuthError(
                    f"Weight Gurus login failed (HTTP {error.status}); check WEIGHTGURUS_EMAIL and WEIGHTGURUS_PASSWORD"
                ) from None
            raise
        token = (answer or {}).get("accessToken")
        if not token:
            raise AuthError("Weight Gurus login returned no access token")
        return token

    def operations(self, start=WEIGHTGURUS_START):
        token = self.login()
        answer = self.http.call(
            "GET",
            self.base + "/v3/operation/?" + urllib.parse.urlencode({"start": start}),
            headers={"Authorization": "Bearer " + token},
            where="Weight Gurus operations",
        )
        if isinstance(answer, dict):
            answer = answer.get("operations")
        if not isinstance(answer, list):
            raise FetchError("Weight Gurus operations response has no operations list")
        return answer


def operation_id(op):
    return "|".join(
        str(op.get(k) or "")
        for k in ("entryTimestamp", "operationType", "serverTimestamp")
    )


def weightgurus_window(client, start=WEIGHTGURUS_START):
    fetched_at = utc_now()
    ops = client.operations(start)
    out = [
        record("weightgurus.operation", operation_id(op), "weightgurus", op, fetched_at)
        for op in ops
        if isinstance(op, dict)
    ]
    out.append(
        record(
            "export.window",
            f"weightgurus:{start}",
            "weightgurus",
            {"start": start, "operations": len(out)},
            fetched_at,
        )
    )
    return out


# ------------------------------------------------------------- composition


def windows(start, end, size):
    size = max(1, int(size))
    cursor = start
    while cursor <= end:
        stop = min(end, cursor + timedelta(days=size - 1))
        yield cursor, stop
        cursor = stop + timedelta(days=1)


def parse_list(value, allowed, name):
    items = [v.strip() for v in (value or "").split(",") if v.strip()]
    unknown = [v for v in items if v not in allowed]
    if unknown:
        raise FetchError(f"{name}: unknown value(s) {', '.join(unknown)}")
    return tuple(items)


def providers_from(value, env, auth):
    if (value or "auto").strip() in ("", "auto"):
        chosen = []
        if auth.configured():
            chosen.append("oura")
        if env.get("WEIGHTGURUS_EMAIL") and env.get("WEIGHTGURUS_PASSWORD"):
            chosen.append("weightgurus")
        return tuple(chosen)
    return parse_list(value, ("oura", "weightgurus"), "sources")


def iterate(
    env, http, *, providers, collections, start, end, window_days, progress=None
):
    """Yield records window by window (Weight Gurus first, then Oura).

    progress(provider, end_day) is called after each complete window. Raises
    BudgetExhausted when the request budget runs out; already-yielded windows
    stay valid. A Weight Gurus failure (other than the budget) does not block
    Oura: it is logged, Oura is fetched, and the error is raised at the end.
    """
    deferred = None
    if "weightgurus" in providers:
        try:
            batch = weightgurus_records(env, http)
        except BudgetExhausted:
            raise
        except FetchError as exc:
            log(f"weightgurus: {exc}; continuing with the other sources")
            deferred = exc
        else:
            yield from batch
            if progress:
                progress("weightgurus", None)
    if "oura" in providers:
        auth = OuraAuth(env, http)
        oura = Oura(http, auth, env.get("OURA_API_BASE") or OURA_BASE)
        skipped = set()
        for low, high in windows(start, end, window_days):
            yield from oura_window(oura, collections, low, high, skipped)
            if progress:
                progress("oura", high)
    if deferred is not None:
        raise deferred


def weightgurus_records(env, http):
    """The full Weight Gurus history as one complete window of records."""
    client = WeightGurus(
        http,
        env.get("WEIGHTGURUS_EMAIL"),
        env.get("WEIGHTGURUS_PASSWORD"),
        env.get("WEIGHTGURUS_API_BASE") or WEIGHTGURUS_BASE,
    )
    if not (client.email and client.password):
        raise AuthError("Weight Gurus needs WEIGHTGURUS_EMAIL and WEIGHTGURUS_PASSWORD")
    return weightgurus_window(client)


def _load_state(path):
    if not path:
        return {}
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise FetchError("--state file is unreadable") from None


def export(args, env, today=None):
    today = today or date.today()
    state = _load_state(args.state)
    day = today.isoformat()
    used = (
        state.get("requests", {}).get("used", 0)
        if state.get("requests", {}).get("day") == day
        else 0
    )
    budget = args.max_requests
    if args.max_requests_per_day is not None:
        budget = (
            min(budget, max(0, args.max_requests_per_day - used))
            if budget is not None
            else max(0, args.max_requests_per_day - used)
        )
    http = Http(
        max_requests=budget,
        min_interval=args.min_interval,
        retries=args.retries,
        backoff=args.backoff,
    )
    auth = OuraAuth(env, http)
    providers = providers_from(args.sources, env, auth)
    if not providers:
        raise FetchError(
            "no source configured: set Oura and/or Weight Gurus credentials, or --sources"
        )
    collections = parse_list(
        args.collections or ",".join(OURA_COLLECTIONS), OURA_COLLECTIONS, "collections"
    )
    if args.start:
        start = date.fromisoformat(args.start)
    elif state.get("oura_next"):
        start = date.fromisoformat(state["oura_next"]) - timedelta(
            days=args.overlap_days
        )
    else:
        start = date.fromisoformat(env.get("HEALTH_BACKFILL_START") or DEFAULT_START)
    end = date.fromisoformat(args.end) if args.end else today
    if end < start:
        raise FetchError("--end is before --start")
    out = Path(args.out)
    if out.is_dir() or args.out.endswith(("/", "\\")):
        out.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out = out / f"health-{stamp}.jsonl"
        serial = 1
        while out.exists() or out.with_name(out.name + ".partial").exists():
            # Never replace an earlier export made in the same second.
            serial += 1
            out = out.with_name(f"health-{stamp}-{serial}.jsonl")
    partial = out.with_name(out.name + ".partial")
    counts = {}
    reached = {}
    error = None

    def progress(provider, high):
        reached[provider] = high

    fd = os.open(partial, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        try:
            for item in iterate(
                env,
                http,
                providers=providers,
                collections=collections,
                start=start,
                end=end,
                window_days=args.window_days,
                progress=progress,
            ):
                handle.write(
                    json.dumps(item, sort_keys=True, ensure_ascii=False) + "\n"
                )
                counts[item["kind"]] = counts.get(item["kind"], 0) + 1
        except BudgetExhausted as exc:
            error = str(exc)
        except FetchError as exc:
            error = str(exc)
            log(f"error: {exc}")
        complete = error is None
        next_start = None
        if "oura" in providers:
            last = reached.get("oura")
            next_start = (
                (last + timedelta(days=1)).isoformat() if last else start.isoformat()
            )
        summary = {
            "format": FORMAT_VERSION,
            "complete": complete,
            "stopped": error,
            "providers": list(providers),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "oura_next": next_start,
            "requests": http.count,
            "counts": counts,
        }
        handle.write(
            json.dumps(
                record("export.end", utc_now(), "export", summary, utc_now()),
                sort_keys=True,
            )
            + "\n"
        )
    os.replace(partial, out)
    if args.state:
        if next_start and reached.get("oura"):
            state["oura_next"] = max(state.get("oura_next") or "", next_start)
        state["requests"] = {"day": day, "used": used + http.count}
        _atomic_write(args.state, json.dumps(state, indent=2, sort_keys=True) + "\n")
    summary["out"] = str(out)
    log(json.dumps(summary, sort_keys=True))
    if complete:
        return 0
    return 3 if error and error.startswith("request limit") else 1


def check(args, env):
    http = Http(
        max_requests=6,
        min_interval=args.min_interval,
        retries=args.retries,
        backoff=args.backoff,
    )
    auth = OuraAuth(env, http)
    providers = providers_from(args.sources, env, auth)
    if not providers:
        log("no source configured")
        return 1
    status = 0
    for provider in providers:
        try:
            if provider == "oura":
                Oura(http, auth, env.get("OURA_API_BASE") or OURA_BASE).get(
                    "personal_info", None
                )
            else:
                WeightGurus(
                    http,
                    env.get("WEIGHTGURUS_EMAIL"),
                    env.get("WEIGHTGURUS_PASSWORD"),
                    env.get("WEIGHTGURUS_API_BASE") or WEIGHTGURUS_BASE,
                ).login()
            log(f"{provider}: ok")
        except FetchError as exc:
            log(f"{provider}: {exc}")
            status = 1
    return status


def parser():
    env = os.environ
    p = argparse.ArgumentParser(
        description="Fetch Oura and Weight Gurus history as JSONL (credentials from environment only)."
    )
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("export", "check"):
        c = sub.add_parser(name)
        c.add_argument(
            "--sources",
            default=env.get("HEALTH_PROVIDERS", "auto"),
            help="auto, or a comma list of oura,weightgurus",
        )
        c.add_argument(
            "--min-interval",
            type=float,
            default=float(env.get("HEALTH_MIN_INTERVAL_S") or 0.2),
            help="seconds between requests",
        )
        c.add_argument("--retries", type=int, default=5)
        c.add_argument(
            "--backoff", type=float, default=1.0, help="first retry delay in seconds"
        )
        if name == "export":
            c.add_argument(
                "--out",
                required=True,
                help="output .jsonl file, or a directory for a timestamped file",
            )
            c.add_argument(
                "--start",
                help="first day (YYYY-MM-DD); default: state file, else HEALTH_BACKFILL_START or 2015-01-01",
            )
            c.add_argument("--end", help="last day (YYYY-MM-DD); default today")
            c.add_argument(
                "--collections",
                default=env.get("HEALTH_OURA_COLLECTIONS"),
                help="comma list of Oura collections; default all",
            )
            c.add_argument(
                "--window-days",
                type=int,
                default=int(env.get("HEALTH_WINDOW_DAYS") or 60),
            )
            c.add_argument(
                "--overlap-days",
                type=int,
                default=int(env.get("HEALTH_SYNC_OVERLAP_DAYS") or 3),
            )
            c.add_argument(
                "--max-requests",
                type=int,
                default=int(env.get("HEALTH_MAX_REQUESTS_PER_RUN") or 1500),
            )
            c.add_argument(
                "--max-requests-per-day",
                type=int,
                default=int(env.get("HEALTH_MAX_REQUESTS_PER_DAY") or 6000),
                help="enforced with --state",
            )
            c.add_argument(
                "--state",
                help="JSON file remembering the next Oura day and today's request count",
            )
    return p


def main(argv=None, env=None):
    env = os.environ if env is None else env
    args = parser().parse_args(argv)
    if args.command == "export" and not args.state:
        args.max_requests_per_day = None
    try:
        return export(args, env) if args.command == "export" else check(args, env)
    except FetchError as exc:
        log(f"error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
