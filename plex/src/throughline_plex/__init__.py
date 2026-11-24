"""Read-only Plex consumption events. No Plex mutation endpoints are reachable."""

import copy
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from throughline_sdk import (
    Channel,
    Checkpoint,
    Declaration,
    Manifest,
    Plugin,
    Record,
    run,
)


def integer(value):
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def seconds(value):
    value = integer(value)
    return max(0, value) / 1000 if value is not None else None


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def first(value):
    return (value[0] if value else {}) if isinstance(value, list) else (value or {})


class Source:
    def __init__(self, settings, token, transport=None):
        self.fixture = (
            json.loads(Path(settings["recording"]).read_text())
            if settings.get("recording")
            else None
        )
        self.page_size = int(settings.get("page_size") or 100)
        if not 1 <= self.page_size <= 1000:
            raise ValueError("PLEX_PAGE_SIZE must be between 1 and 1000")
        url = settings["server_url"]
        parsed = urlsplit(url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.netloc
            or parsed.username
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "PLEX_SERVER_URL must be an HTTP(S) server URL without credentials or query"
            )
        self.client = httpx.Client(
            base_url=url.rstrip("/") + "/",
            headers={
                "Accept": "application/json",
                "X-Plex-Token": token,
                "X-Plex-Client-Identifier": "throughline-plex",
                "X-Plex-Product": "Throughline",
            },
            timeout=30,
            transport=transport,
            follow_redirects=False,
        )

    def get(self, path, params=None, missing=False):
        if not (
            path
            in (
                "/identity",
                "/accounts",
                "/devices",
                "/status/sessions",
                "/status/sessions/history/all",
            )
            or re.fullmatch(r"/library/metadata/\d+", path)
        ):
            raise ValueError("Plex endpoint is not allowlisted")
        if self.fixture is not None:
            if path == "/status/sessions/history/all":
                records = self.fixture["history"]["MediaContainer"]["Metadata"]
                lower = int((params or {}).get("viewedAt>", 0))
                records = sorted(
                    (r for r in records if int(r["viewedAt"]) >= lower),
                    key=lambda r: (int(r["viewedAt"]), r["historyKey"]),
                )
                offset = int(params["X-Plex-Container-Start"])
                return {
                    "totalSize": len(records),
                    "Metadata": records[offset : offset + self.page_size],
                }
            return copy.deepcopy(
                self.fixture.get(path, {"MediaContainer": {}})["MediaContainer"]
            )
        for attempt in range(4):
            try:
                response = self.client.get(path.lstrip("/"), params=params)
            except httpx.TransportError:
                if attempt == 3:
                    raise RuntimeError("Plex transport failed") from None
            else:
                if response.status_code == 404 and missing:
                    return {}
                if response.status_code != 429 and response.status_code < 500:
                    if response.is_error or response.is_redirect:
                        raise RuntimeError(
                            f"Plex read failed (HTTP {response.status_code})"
                        )
                    try:
                        return response.json()["MediaContainer"]
                    except (ValueError, KeyError):
                        raise RuntimeError(
                            "Plex returned invalid JSON MediaContainer"
                        ) from None
                if attempt == 3:
                    raise RuntimeError("Plex read retries exhausted")
            time.sleep(0.25 * 2**attempt)

    def close(self):
        self.client.close()


FIELD_TYPES = {
    "media_type": "text",
    "title": "text",
    "show_title": "text",
    "season": "integer",
    "episode": "integer",
    "year": "integer",
    "duration_s": "number",
    "view_offset_s": "number",
    "device": "text",
    "player": "text",
    "library": "text",
    "plex_rating_key": "text",
    "plex_guid": "text",
    "account_name": "text",
    "timing_basis": "text",
    "time_precision": "text",
    "initial_offset_s": "number",
    "playback_state": "text",
    "position_observed_at": "timestamp",
}
DESCRIPTIONS = {
    "duration_s": "Work runtime in seconds, not measured viewing duration",
    "view_offset_s": "Last observed playback position in seconds; unknown for ordinary history",
    "timing_basis": "Explicit description of estimated playback start/end timestamps",
    "position_observed_at": "UTC time at which view_offset_s was polled; null for history",
}


class Plex(Plugin):
    manifest = Manifest(
        name="plex",
        version="0.1.0",
        default_interval_s=30,
        channels=[
            Channel(
                name="media.plex",
                description="Plex viewing and listening events, including polled playback positions",
                fields=[
                    {
                        "name": k,
                        "type": v,
                        "description": DESCRIPTIONS.get(
                            k, k.replace("_", " ").capitalize()
                        ),
                    }
                    for k, v in FIELD_TYPES.items()
                ],
            )
        ],
        settings={
            "server_url": Declaration(
                env="PLEX_SERVER_URL", description="Plex server HTTP(S) base URL"
            ),
            "accounts": Declaration(
                env="PLEX_ACCOUNTS",
                required=False,
                description="Comma separated account IDs or names; empty means all",
            ),
            "libraries": Declaration(
                env="PLEX_LIBRARIES",
                required=False,
                description="Comma separated library IDs or names; empty means all",
            ),
            "page_size": Declaration(
                env="PLEX_PAGE_SIZE",
                default="100",
                description="History page size, 1..1000",
            ),
            "recording": Declaration(
                env="PLEX_RECORDING",
                required=False,
                description="Invented JSON response bundle; disables all source network calls",
            ),
        },
        secrets={
            "token": Declaration(
                env="PLEX_TOKEN",
                description="Plex server owner token (sent only in header)",
            )
        },
        link_types=["consumed", "part_of"],
    )

    @staticmethod
    def allowed(setting, *values):
        choices = {
            s.strip().casefold() for s in (setting or "").split(",") if s.strip()
        }
        return not choices or bool(
            choices & {str(v).casefold() for v in values if v is not None}
        )

    def normalize(
        self, raw, metadata, server, accounts, devices, *, active=None, external_id=None
    ):
        item = dict(metadata, **raw)
        user = first(item.get("User"))
        account = str(item.get("accountID", user.get("id", "unknown")))
        name = accounts.get(account, {}).get("name") or user.get("title") or account
        library_id = item.get("librarySectionID")
        library = item.get("librarySectionTitle") or (
            str(library_id) if library_id is not None else None
        )
        if not self.allowed(
            self.settings.get("accounts"), account, name
        ) or not self.allowed(self.settings.get("libraries"), library_id, library):
            return None
        kind = item.get("type", "unknown")
        title = item.get("title") or "Unknown title"
        duration = seconds(item.get("duration"))
        player = first(item.get("Player"))
        device = devices.get(str(item.get("deviceID")), {})
        fields = dict(
            media_type=kind,
            title=title,
            show_title=item.get("grandparentTitle") if kind == "episode" else None,
            season=integer(item.get("parentIndex")) if kind == "episode" else None,
            episode=integer(item.get("index")) if kind == "episode" else None,
            year=integer(item.get("year")),
            duration_s=duration,
            view_offset_s=seconds(raw.get("viewOffset")),
            device=player.get("title") or device.get("name"),
            player=player.get("product") or device.get("platform"),
            library=library,
            plex_rating_key=str(item["ratingKey"])
            if item.get("ratingKey") is not None
            else None,
            plex_guid=item.get("guid"),
            account_name=name,
            position_observed_at=iso(active["seen"]) if active else None,
            playback_state=player.get("state") if active else "history",
        )
        if active:
            start, end = active["start"], active.get("end")
            fields["timing_basis"] = (
                "first observed poll; end last observed poll if session disappears"
            )
            fields["time_precision"] = "first_seen"
            fields["initial_offset_s"] = active.get("initial_offset_s")
        else:
            marked = integer(raw.get("viewedAt"))
            if marked is None:
                raise ValueError("Plex history entry has no viewedAt")
            start, end = marked, None
            fields["timing_basis"] = (
                "viewedAt played-mark timestamp; start and stop unknown"
            )
            fields["time_precision"] = "source_mark"
        if kind == "episode":
            numbers = (
                f"S{fields['season']:02}E{fields['episode']:02}"
                if fields["season"] is not None and fields["episode"] is not None
                else ""
            )
            content = (
                f"Watched {fields['show_title'] or 'Unknown show'} {numbers} - {title}"
            )
        elif kind == "track":
            content = f"Listened to {title} - {item.get('grandparentTitle') or item.get('originalTitle') or 'Unknown artist'}"
        else:
            content = f"Watched {title}" + (
                f" ({fields['year']})" if fields["year"] is not None else ""
            )
        if external_id is None:
            if not raw.get("historyKey"):
                raise ValueError("Plex history entry has no stable historyKey")
            external_id = f"{server}:{raw['historyKey']}"
        message = dict(
            channel="media.plex",
            external_id=external_id,
            thread={
                "external_id": f"{server}:account:{account}:library:{library_id or 'unknown'}",
                "title": f"{name} / {library or 'Unknown library'}",
            },
            happened_at=iso(start),
            ended_at=iso(end) if end is not None else None,
            author={
                "system": "plex",
                "kind": "account_id",
                "value": account,
                "display_name": name,
            },
            content=content,
            fields=fields,
            payload={"source": raw, "metadata": metadata},
        )
        work = {
            "kind": "work",
            "external_ref": item.get("guid")
            or f"plex:{server}:{item.get('ratingKey') or external_id}",
            "name": title,
        }
        links = [
            dict(
                type="consumed",
                **{
                    "from": {"channel": "media.plex", "external_id": external_id},
                    "to": work,
                },
                method="source",
            )
        ]
        if kind == "episode" and item.get("grandparentRatingKey"):
            show = {
                "kind": "work",
                "external_ref": item.get("grandparentGuid")
                or f"plex:{server}:{item['grandparentRatingKey']}",
                "name": fields["show_title"],
            }
            links.append(
                dict(type="part_of", **{"from": work, "to": show}, method="source")
            )
            if item.get("parentRatingKey"):
                season = {
                    "kind": "work",
                    "external_ref": item.get("parentGuid")
                    or f"plex:{server}:{item['parentRatingKey']}",
                    "name": item.get("parentTitle") or f"Season {fields['season']}",
                }
                links.extend(
                    [
                        dict(
                            type="part_of",
                            **{"from": work, "to": season},
                            method="source",
                        ),
                        dict(
                            type="part_of",
                            **{"from": season, "to": show},
                            method="source",
                        ),
                    ]
                )
        return message, links

    def records(self, cursor, mode):
        state = copy.deepcopy(cursor)
        source = Source(self.settings, self.secrets["token"])
        try:
            server = source.get("/identity").get("machineIdentifier")
            if not server:
                raise ValueError("Plex identity is missing machineIdentifier")
            if state.get("server", server) != server:
                raise ValueError(
                    "Cursor belongs to another Plex server; use a separate Throughline plugin installation"
                )
            state["server"] = server
            accounts = {
                str(x["id"]): x for x in source.get("/accounts").get("Account", [])
            }
            devices = {
                str(x["id"]): x for x in source.get("/devices").get("Device", [])
            }
            cache = {}

            def metadata(raw):
                key = str(raw.get("ratingKey", ""))
                if not key.isdigit():
                    return {}
                if key not in cache:
                    cache[key] = first(
                        source.get("/library/metadata/" + key, missing=True).get(
                            "Metadata"
                        )
                    )
                return cache[key]

            def account_allowed(raw):
                user = first(raw.get("User"))
                account = str(raw.get("accountID", user.get("id", "unknown")))
                name = accounts.get(account, {}).get("name") or user.get("title") or account
                return self.allowed(self.settings.get("accounts"), account, name)

            def emit(result):
                if result:
                    message, links = result
                    yield message
                    for link in links:
                        yield Record(kind="link", data=link)

            def observed(observation, history=None):
                """Message for a polled viewing, keeping its observed interval.

                A matched history row is evidence about the same viewing, not a
                replacement for it: the played mark is a point, the polls are
                the interval. Closed observations no longer hold the session
                object, so the history row is then the source object.
                """
                history = history or observation.get("history")
                raw = observation.get("raw")
                if raw is None and history is None:
                    return None
                result = self.normalize(
                    raw if raw is not None else history,
                    observation["metadata"] if raw is not None else metadata(history),
                    server,
                    accounts,
                    devices,
                    active=observation,
                    external_id=observation["external_id"],
                )
                if result and history is not None:
                    message = result[0]
                    message["fields"]["timing_basis"] = (
                        "first observed poll to last observed playing poll; "
                        "history viewedAt played mark kept in payload"
                    )
                    if raw is not None:
                        message["payload"]["history"] = history
                    else:
                        message["fields"]["view_offset_s"] = observation.get("position")
                        message["fields"]["playback_state"] = "history"
                return result

            def close(observation):
                # A paused session can stay listed for hours: the viewing ended
                # at the last poll that still saw it playing.
                observation["end"] = observation.get("played", observation["seen"])

            # Fixed lower bound during a paged pass; ascending order prevents new
            # history appended at the tail from shifting previously read offsets.
            lower = state.get("lower", max(0, state.get("watermark", 0) - 86400))
            offset = state.get("offset", 0)
            state["lower"] = lower
            state.setdefault("observed", {})
            state.setdefault("aliases", {})
            state.setdefault("history_seen", {})
            now = int(time.time())
            # Keep only the reconciliation horizon, never lifetime session payloads.
            for key, value in list(state["history_seen"].items()):
                if value["at"] < lower:
                    del state["history_seen"][key]
            state["aliases"] = {
                k: v for k, v in state["aliases"].items() if k in state["history_seen"]
            }
            # An aliased observation outlives the horizon with its alias, so a
            # revised history row still reports the observed interval.
            # A viewing still open (the plugin was down past the horizon) is kept:
            # this run closes it at its last playing poll and emits that end.
            aliased = set(state["aliases"].values())
            for key, value in list(state["observed"].items()):
                if (
                    "end" in value
                    and value["seen"] < now - 86400
                    and value["external_id"] not in aliased
                ):
                    del state["observed"][key]
            live = source.get("/status/sessions").get("Metadata", [])
            current = set()
            for raw in live:
                if not account_allowed(raw):
                    continue
                user, session = first(raw.get("User")), first(raw.get("Session"))
                session_id = session.get("id") or raw.get("sessionKey")
                if session_id is None:
                    raise ValueError("Plex active session has no stable session ID")
                key = f"{session_id}:{raw.get('ratingKey')}:{user.get('id')}"
                current.add(key)
                observation = state["observed"].get(key)
                position = seconds(raw.get("viewOffset")) or 0
                # A backward seek stays one viewing. Only a position that fits
                # playback begun from zero since the previous poll is a restart
                # (repeat, or the same item started over in a reused session).
                restarted = bool(
                    observation
                    and position + 2 < observation.get("position", position)
                    and position <= now - observation["seen"] + 2
                )
                if observation and (
                    "end" in observation or now - observation["seen"] > 120 or restarted
                ):
                    # Retain the old playback for delayed history reconciliation.
                    if "end" not in observation:
                        close(observation)
                        yield from emit(observed(observation))
                    state["observed"][observation["external_id"]] = observation
                    observation = None
                if observation is None:
                    observation = dict(
                        start=now,
                        initial_offset_s=position,
                        external_id=f"{server}:session:{key}:{now}",
                        account=str(user.get("id", "unknown")),
                        rating=str(raw.get("ratingKey")),
                    )
                    state["observed"][key] = observation
                if first(raw.get("Player")).get(
                    "state"
                ) != "paused" or position != observation.get("position"):
                    observation["played"] = now
                observation.update(
                    seen=now, position=position, raw=raw, metadata=metadata(raw)
                )
            while True:
                page = source.get(
                    "/status/sessions/history/all",
                    {
                        "sort": "viewedAt:asc",
                        "viewedAt>": lower,
                        "X-Plex-Container-Start": offset,
                        "X-Plex-Container-Size": source.page_size,
                    },
                )
                rows = page.get("Metadata", [])
                for raw in rows:
                    state["watermark"] = max(
                        state.get("watermark", 0), int(raw["viewedAt"])
                    )
                    if not account_allowed(raw):
                        continue
                    history_id = f"{server}:{raw['historyKey']}"
                    signature = hashlib.sha256(
                        json.dumps(raw, sort_keys=True).encode()
                    ).hexdigest()
                    previous = state["history_seen"].get(history_id)
                    if previous and previous["hash"] == signature:
                        continue
                    alias = state["aliases"].get(history_id)
                    target = next(
                        (
                            v
                            for v in state["observed"].values()
                            if v["external_id"] == alias
                        ),
                        None,
                    )
                    if alias is None:
                        candidates = [
                            (k, v)
                            for k, v in state["observed"].items()
                            if str(raw.get("accountID")) == v["account"]
                            and str(raw.get("ratingKey")) == v["rating"]
                            and v["start"] <= int(raw["viewedAt"]) <= v["seen"] + 120
                            and not v.get("matched")
                        ]
                        if len(candidates) == 1:
                            target = candidates[0][1]
                            alias = target["external_id"]
                            state["aliases"][history_id] = alias
                            target["matched"] = True
                    if target is None:
                        yield from emit(
                            self.normalize(
                                raw,
                                metadata(raw),
                                server,
                                accounts,
                                devices,
                                external_id=alias,
                            )
                        )
                    elif "end" in target:
                        yield from emit(observed(target, raw))
                    else:
                        # Still open: the pass below emits it once, merged.
                        target["history"] = raw
                    state["history_seen"][history_id] = dict(
                        hash=signature, at=int(raw["viewedAt"])
                    )
                    if len(state["history_seen"]) > 4096:
                        oldest = min(
                            state["history_seen"],
                            key=lambda k: state["history_seen"][k]["at"],
                        )
                        del state["history_seen"][oldest]
                        state["aliases"].pop(oldest, None)
                offset += len(rows)
                state["offset"] = offset
                yield Checkpoint(cursor=copy.deepcopy(state))
                if not rows or offset >= int(page.get("totalSize", offset + 1)):
                    break
            state.pop("offset", None)
            state.pop("lower", None)
            for key in current:
                yield from emit(observed(state["observed"][key]))
            for key, observation in state["observed"].items():
                if key not in current and "end" not in observation:
                    close(observation)
                    yield from emit(observed(observation))
            for observation in state["observed"].values():
                if "end" in observation:
                    observation.pop("raw", None)
                    observation.pop("metadata", None)
                    observation.pop("history", None)
            while len(state["observed"]) > 2048:
                oldest = min(
                    state["observed"], key=lambda k: state["observed"][k]["seen"]
                )
                del state["observed"][oldest]
            yield Checkpoint(cursor=state)
        finally:
            source.close()


def main():
    run(Plex)
