"""Beeper Desktop and legacy archive share chat channels and normalization."""

import copy
import json
import logging
import os
from collections import Counter
from datetime import datetime, timezone

import httpx
from throughline_sdk import (
    Channel,
    Checkpoint,
    Declaration,
    Manifest,
    Plugin,
    Record,
    coverage,
    run,
)
from throughline_media import transcribe as stt
from throughline_media import youtube
from throughline_media.pacing import parse_limit, parse_since
from throughline_media.settings import apply_aliases, parse_flags
from throughline_sdk.plugin import Redactor, contains_secret

from .enrich import Enricher, truthy
from .mapping import (
    archive_message,
    archive_resolver,
    decoded,
    default_namespace,
    live_message,
    network,
    reply_link,
    timestamp,
)
from .media import transcriber_from
from .sources import Archive, Desktop, SourceError

# Flush an ingest request early once enriched content grows this large.
REQUEST_BYTES = 16 * 1024 * 1024
TRANSCRIBERS = ("auto", "faster-whisper", "openai", "openai-compatible", "none")

FIELDS = [
    ("direction", "text", "incoming, outgoing, or unknown relative to the owner"),
    (
        "time_precision",
        "text",
        "Source time precision; inferred when taken from a neighbouring message, unknown when import time substitutes for sent time",
    ),
    (
        "time_inferred_from",
        "json",
        "Absent when the source supplied the time; otherwise the method, and the message and time it was inferred from",
    ),
    ("reply_to", "text", "External ID of the replied-to message when supplied"),
    ("edited_at", "timestamp", "Source edit time with its original UTC offset"),
    (
        "attachments",
        "json",
        "Original attachment metadata, unchanged; conversions are in attachment_files",
    ),
    ("reactions", "json", "Original source reaction metadata"),
    (
        "reaction_details",
        "json",
        "Absent without reactions; otherwise [{emoji, sender_id?, sender_kind?, sender_name?, at?, count?}] in source order, reactors resolved to sender identifiers where the source allows",
    ),
    (
        "attachment_files",
        "json",
        "Absent until enrichment handled a document attachment; otherwise [{name, kind, status, file?, sha256?, bytes?, pages?, renderer?, assets?: [{role, page|index, link, sha256}], error?, truncated?, text?}]",
    ),
    (
        "instagram_links",
        "json",
        "Absent until enrichment handled an Instagram link; otherwise [{url, kind, shortcode, status, items?: [{type, index, link?, audio?, frames?: [{at_s, link}], transcript_status?, transcriber?}], error?, truncated?, text?}]",
    ),
    (
        "youtube_links",
        "json",
        "Absent until enrichment handled a YouTube link; otherwise [{url, kind, video_id, status, title?, duration_s?, transcript_status?, transcript_source?, caption_kind?, language?, transcriber?, model?, timestamps?, transcript?, frames?: [{at_s, link}], error?, truncated?, text?}]",
    ),
    (
        "identity_quality",
        "text",
        "Source confidence or provenance of sender identification",
    ),
    ("conversation_kind", "text", "dm, group_dm, channel, or unknown"),
    ("guild_id", "text", "Native guild/server ID when known"),
]


ENRICHMENT_SETTINGS = [
    (
        "attachments",
        "BEEPER_ATTACHMENTS",
        "0",
        "1 converts document attachments (--attachments)",
    ),
    (
        "instagram_links",
        "BEEPER_INSTAGRAM_LINKS",
        "0",
        "1 fetches Instagram links (--instagram-links)",
    ),
    (
        "state_dir",
        "BEEPER_STATE_DIR",
        None,
        "Plugin state directory: enrichment cache and default asset store",
    ),
    (
        "asset_dir",
        "BEEPER_ASSET_DIR",
        None,
        "Content-addressed asset directory, default <state>/assets",
    ),
    (
        "asset_base_url",
        "BEEPER_ASSET_BASE_URL",
        "throughline-asset://beeper",
        "Base of rendered asset links",
    ),
    (
        "attachment_limit",
        "BEEPER_ATTACHMENT_LIMIT",
        "200",
        "New attachment downloads/conversions per run",
    ),
    (
        "instagram_limit",
        "BEEPER_INSTAGRAM_LIMIT",
        "25",
        "New Instagram links fetched per run",
    ),
    (
        "attachment_max_bytes",
        "BEEPER_ATTACHMENT_MAX_BYTES",
        "26214400",
        "Size cap per attachment file",
    ),
    (
        "instagram_max_bytes",
        "BEEPER_INSTAGRAM_MAX_BYTES",
        "209715200",
        "Size cap per Instagram video",
    ),
    (
        "instagram_max_seconds",
        "BEEPER_INSTAGRAM_MAX_SECONDS",
        "1800",
        "Longest Instagram video processed",
    ),
    (
        "instagram_max_frames",
        "BEEPER_INSTAGRAM_MAX_FRAMES",
        "200",
        "Most frames kept per video (one per 3 s)",
    ),
    ("ytdlp_cookies", "BEEPER_YTDLP_COOKIES", None, "Netscape cookies.txt for yt-dlp"),
    (
        "ytdlp_cookies_from_browser",
        "BEEPER_YTDLP_COOKIES_FROM_BROWSER",
        None,
        "Browser for yt-dlp --cookies-from-browser",
    ),
    (
        "transcriber",
        "BEEPER_TRANSCRIBER",
        "auto",
        "auto, faster-whisper, openai, or none",
    ),
    ("whisper_model", "BEEPER_WHISPER_MODEL", "small", "faster-whisper model name"),
    (
        "youtube_links",
        "BEEPER_YOUTUBE_LINKS",
        "0",
        "1 transcribes YouTube links and keeps one frame per minute (--youtube-links)",
    ),
    (
        "youtube_limit",
        "BEEPER_YOUTUBE_LIMIT",
        "10",
        "New YouTube videos processed per run",
    ),
    (
        "youtube_max_seconds",
        "BEEPER_YOUTUBE_MAX_SECONDS",
        "7200",
        "Longest YouTube video processed; longer ones are skipped until raised",
    ),
    (
        "youtube_frame_seconds",
        "BEEPER_YOUTUBE_FRAME_EVERY",
        "60",
        "Seconds between kept YouTube frames",
    ),
    (
        "youtube_max_bytes",
        "BEEPER_YOUTUBE_MAX_BYTES",
        str(youtube.MAX_DOWNLOAD_BYTES),
        "Download cap per YouTube audio or video stream (temporary files only)",
    ),
    (
        "youtube_transcript_source",
        "BEEPER_YOUTUBE_TRANSCRIPT_SOURCE",
        "auto",
        "auto (transcriber when configured, else captions), endpoint or captions",
    ),
    (
        "youtube_caption_langs",
        "BEEPER_YOUTUBE_CAPTION_LANGS",
        "en",
        "Comma-separated preferred caption languages",
    ),
    (
        "enrich",
        "BEEPER_ENRICH",
        None,
        "Comma-separated enrichments to switch on without flags (scheduled syncs): attachments, instagram, youtube",
    ),
    (
        "backlog",
        "BEEPER_BACKLOG",
        "0",
        "1 also enriches messages sent before each enrichment was first enabled (--backlog)",
    ),
    (
        "enrich_since",
        "BEEPER_ENRICH_SINCE",
        None,
        "ISO date or time that replaces the stored enable time of every enabled enrichment (--enrich-since)",
    ),
    (
        "attachments_per_day",
        "BEEPER_ATTACHMENTS_PER_DAY",
        "500",
        "Attachment downloads in any 24 hours, across runs; 0 disables, unlimited lifts (--attachments-per-day)",
    ),
    (
        "instagram_per_day",
        "BEEPER_INSTAGRAM_PER_DAY",
        "20",
        "Instagram reels/posts fetched in any 24 hours, across runs; 0 disables, unlimited lifts (--instagram-per-day)",
    ),
    (
        "youtube_per_day",
        "BEEPER_YOUTUBE_PER_DAY",
        "20",
        "YouTube videos fetched in any 24 hours, across runs; 0 disables, unlimited lifts (--youtube-per-day)",
    ),
    *stt.SETTINGS,
    (
        "beeper_transcribe_url",
        "BEEPER_TRANSCRIBE_URL",
        None,
        "Alias of THROUGHLINE_TRANSCRIBE_URL",
    ),
    (
        "beeper_transcribe_model",
        "BEEPER_TRANSCRIBE_MODEL",
        None,
        "Alias of THROUGHLINE_TRANSCRIBE_MODEL",
    ),
]
# (setting, alias) pairs: the THROUGHLINE_ name wins when both are set.
ALIASES = [
    ("transcribe_url", "beeper_transcribe_url"),
    ("transcribe_model", "beeper_transcribe_model"),
]
SECRET_ALIASES = [("transcribe_api_key", "beeper_transcribe_api_key")]
# BEEPER_ENRICH names and the switch each one sets.
ENRICH_NAMES = {
    "attachments": "attachments",
    "instagram": "instagram_links",
    "instagram_links": "instagram_links",
    "youtube": "youtube_links",
    "youtube_links": "youtube_links",
}


# The beginning of a chat's history when a failed load has no earlier bound.
EPOCH = "1970-01-01T00:00:00+00:00"


def quietly(what, function, *args):
    """Call a coverage or feature endpoint; a failure is logged, never raised."""
    try:
        return function(*args)
    except Exception as error:
        logging.getLogger("throughline_beeper").warning(
            "%s not delivered: %s", what, type(error).__name__
        )
        return None


def core_features(client):
    """Subjects and functions GET /v1/describe advertises (empty when the core
    is older or the token cannot read it), so newer features degrade."""
    answer = quietly("describe", lambda: client.request("GET", "describe"))
    if not isinstance(answer, dict):
        return set()
    names = {
        item.get("subject")
        for item in answer.get("subjects") or []
        if isinstance(item, dict)
    }
    return names | {f for f in answer.get("functions") or [] if isinstance(f, str)}


def caps(value):
    """BEEPER_LIVE_HISTORY_CAPS: network=count pairs, comma-separated."""
    result = {}
    for part in (value or "").replace(";", ",").split(","):
        name, _, count = part.partition("=")
        if not part.strip():
            continue
        if not count.strip().isdigit():
            raise ValueError("BEEPER_LIVE_HISTORY_CAPS takes network=count pairs")
        result[network(name)] = int(count)
    return result


def names(value):
    return {network(n) for n in (value or "").replace(";", ",").split(",") if n.strip()}


def later(a, b):
    """The later of two ISO times (either may be None)."""
    if not a or not b:
        return a or b
    return a if datetime.fromisoformat(a) >= datetime.fromisoformat(b) else b


def earlier(a, b):
    if not a or not b:
        return a or b
    return a if datetime.fromisoformat(a) <= datetime.fromisoformat(b) else b


def source_status(conversation):
    """(status word, detail) the archive recorded for a conversation's history
    load, from history_status/history_detail on the row or in metadata_json."""
    conversation = conversation or {}
    try:
        metadata = json.loads(conversation.get("metadata_json") or "{}")
    except (ValueError, TypeError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    word = conversation.get("history_status") or metadata.get("history_status")
    detail = conversation.get("history_detail", metadata.get("history_detail"))
    return (word if isinstance(word, str) and word else None), detail


def enrich_switches(settings):
    """Turn BEEPER_ENRICH=attachments,instagram,youtube into the switches."""
    for name in (settings.get("enrich") or "").replace(";", ",").split(","):
        name = name.strip().lower()
        if not name:
            continue
        if name not in ENRICH_NAMES:
            raise ValueError(
                "BEEPER_ENRICH takes attachments, instagram and youtube, comma-separated"
            )
        settings[ENRICH_NAMES[name]] = "1"


def channel(net):
    return Channel(
        name="chat." + net,
        description="Chat messages on " + net,
        fields=[dict(name=n, type=t, description=d) for n, t, d in FIELDS],
    )


class Beeper(Plugin):
    manifest = Manifest(
        name="beeper",
        version="0.1.0",
        channels=[
            channel(n) for n in ("discord", "instagram", "sms", "reddit", "snapchat")
        ],
        settings={
            "source": Declaration(
                env="BEEPER_SOURCE", default="live", description="live or archive"
            ),
            "base_url": Declaration(
                env="BEEPER_BASE_URL",
                default="http://localhost:23373",
                description="Beeper Desktop API origin",
            ),
            "recording": Declaration(
                env="BEEPER_RECORDING",
                required=False,
                description="Invented source fixture JSON (offline testing only)",
            ),
            "batch_size": Declaration(
                env="BEEPER_BATCH_SIZE",
                default="2000",
                description="Archive streaming batch size, 1 to 5000",
            ),
            "live_history_caps": Declaration(
                env="BEEPER_LIVE_HISTORY_CAPS",
                required=False,
                default="instagram=1000",
                description="network=count: Desktop history caps; a chat whose loaded history reaches its cap is reported partial (source_cap)",
            ),
            "no_history_networks": Declaration(
                env="BEEPER_NO_HISTORY_NETWORKS",
                required=False,
                default="snapchat",
                description="Comma-separated networks whose Desktop chats have no history listing; reported best_effort",
            ),
            **{
                name: Declaration(
                    env=env, required=False, default=default, description=description
                )
                for name, env, default, description in ENRICHMENT_SETTINGS
            },
        },
        secrets={
            "token": Declaration(
                env="BEEPER_ACCESS_TOKEN",
                required=False,
                description="Desktop API Bearer token, required for live",
            ),
            "dsn": Declaration(
                env="BEEPER_ARCHIVE_DSN",
                required=False,
                description="Read-only Postgres archive DSN, required for archive",
            ),
            stt.SECRET[0]: Declaration(
                env=stt.SECRET[1], required=False, description=stt.SECRET[2]
            ),
            "beeper_transcribe_api_key": Declaration(
                env="BEEPER_TRANSCRIBE_API_KEY",
                required=False,
                description="Alias of THROUGHLINE_TRANSCRIBE_API_KEY",
            ),
        },
        default_interval_s=300,
        link_types=["reply_to"],
    )

    def __init__(self):
        super().__init__()
        self.manifest = self.manifest.model_copy(deep=True)
        self.source = self.settings["source"]
        self.size = int(self.settings["batch_size"])
        if not 1 <= self.size <= 5000:
            raise ValueError("BEEPER_BATCH_SIZE must be between 1 and 5000")
        if self.source not in {"live", "archive"}:
            raise ValueError("BEEPER_SOURCE must be live or archive")
        if (
            not self.settings["recording"]
            and not self.secrets["token" if self.source == "live" else "dsn"]
        ):
            raise ValueError("selected Beeper source requires its declared credential")
        for name in ENRICHMENT_SETTINGS:
            if name[0].endswith(("_limit", "_bytes", "_seconds", "_frames")):
                if not str(self.settings[name[0]] or "").isdigit():
                    raise ValueError(name[1] + " must be a non-negative integer")
            if name[0].endswith("_per_day"):
                parse_limit(self.settings[name[0]], name[1])
        parse_since(self.settings["enrich_since"], "BEEPER_ENRICH_SINCE")
        enrich_switches(self.settings)
        apply_aliases(self.settings, ALIASES)
        apply_aliases(self.secrets, SECRET_ALIASES)
        stt.validate(self.settings)
        if int(self.settings["youtube_frame_seconds"]) < 1:
            raise ValueError("BEEPER_YOUTUBE_FRAME_EVERY must be at least 1")
        if (self.settings["transcriber"] or "auto").lower() not in TRANSCRIBERS:
            raise ValueError(
                "BEEPER_TRANSCRIBER must be one of " + ", ".join(TRANSCRIBERS)
            )
        source = (self.settings["youtube_transcript_source"] or "auto").lower()
        if source not in youtube.SOURCES:
            raise ValueError(
                "BEEPER_YOUTUBE_TRANSCRIPT_SOURCE must be auto, endpoint or captions"
            )
        self.settings["youtube_transcript_source"] = source
        if truthy(self.settings["instagram_links"]) or truthy(
            self.settings["youtube_links"]
        ):
            # Fail fast on bad setup.
            chosen = transcriber_from(self.settings, self.secrets)
            if (
                truthy(self.settings["youtube_links"])
                and source == "endpoint"
                and chosen.name == "none"
            ):
                raise ValueError(
                    "BEEPER_YOUTUBE_TRANSCRIPT_SOURCE=endpoint needs a transcriber:"
                    " THROUGHLINE_TRANSCRIBE_URL or faster-whisper"
                )
        self.caps = caps(self.settings["live_history_caps"])
        self.no_history = names(self.settings["no_history_networks"])
        self.dry_run = False
        self.aliases_on = False
        self.open_gaps = {}
        self.aliased = 0
        self.enrichment = Counter()
        self.pacing = {}
        # Test seam: transport, instagram fetcher, transcriber or asset store.
        self.enrich_options = {}

    def enricher(self, desktop=None):
        return Enricher(
            self.settings,
            self.secrets,
            dry_run=self.dry_run,
            desktop=desktop,
            live=desktop is not None,
            **self.enrich_options,
        )

    def records(self, cursor, mode):
        self.quarantined = 0
        self.unlinked = 0
        self.aliased = 0
        self.enrichment = Counter()
        self.pacing = {}
        cursor = copy.deepcopy(cursor)
        # Keep independent progress when switching sources with the same token.
        if self.source == "archive":
            yield from self.archive_records(cursor)
        else:
            yield from self.live_records(cursor, mode)

    def declare(self, networks):
        existing = {c.name for c in self.manifest.channels}
        self.manifest.channels.extend(
            channel(n) for n in sorted(networks) if "chat." + n not in existing
        )

    def archive_records(self, cursor):
        source = Archive(self.secrets["dsn"], self.settings["recording"])
        enricher = None
        try:
            enricher = self.enricher()
            if enricher.working:
                # Every run streams the whole table: spend the allowance on
                # the newest due items rather than on stream order.
                enricher.plan(self.candidates(source, enricher))
            self.declare(source.networks())
            conversations = {r["id"]: r for r in source.table("conversations")}
            identities = {}
            for row in source.table("identities"):
                identities.setdefault((row["platform"], row["native_id"]), set()).add(
                    row["namespace"]
                )
            state = cursor.get("archive", {})
            if state.get("done"):
                state = {}  # Reconcile edits and inserts with lexically older IDs.
            if isinstance(state.get("after"), str):
                state = {}  # Old ID checkpoints cannot describe the new ordering.
            quarantined = set(state.get("quarantined", []))
            ids, aliases = source.reply_targets()
            # Earlier or later ids of the same message (native or live forms),
            # sent only when the core advertises message_aliases. Identity is
            # written only through ingest identifiers (message authors); the
            # archive's people crosswalk is never read.
            alias_map = source.message_aliases() if self.aliases_on else {}
            chats = {}
            neighbours = source.first_dated()
            senders = source.senders()

            def kind_of(row):
                def kind(sender):
                    namespaces = identities.get((row.get("platform"), sender), set())
                    if len(namespaces) == 1:
                        return next(iter(namespaces))
                    return default_namespace(network(row.get("platform")))

                return kind

            def target_for(row):
                net = network(row.get("platform"))
                target = row.get("reply_to")
                if target not in ids:
                    target = aliases.get((net, target))
                return (
                    target
                    if target and ids.get(target) == net and target not in quarantined
                    else None
                )

            if state.get("phase", "messages") == "messages":
                for batch in source.batches(state.get("after", ""), self.size):
                    for row in batch:
                        namespaces = identities.get(
                            (row.get("platform"), row.get("sender_id")), set()
                        )
                        stats = chats.setdefault(
                            (
                                "chat." + network(row.get("platform")),
                                row.get("conversation_id") or "archive:unknown",
                            ),
                            Counter(),
                        )
                        try:
                            mapped = archive_message(
                                row,
                                conversations.get(row.get("conversation_id")),
                                next(iter(namespaces))
                                if len(namespaces) == 1
                                else None,
                                None
                                if row.get("sent_at") or not row.get("conversation_id")
                                else neighbours.get(row["conversation_id"]),
                                archive_resolver(
                                    senders.get(row.get("conversation_id")),
                                    kind_of(row),
                                ),
                                enricher,
                            )
                        except (ValueError, TypeError, KeyError, AttributeError):
                            quarantined.add(row["id"])
                            self.quarantined += 1
                            stats["quarantined"] += 1
                            logging.warning("Beeper quarantined id=%s", row.get("id"))
                            continue
                        mapped["fields"]["reply_to"] = target_for(row)
                        if alias_map.get(row["id"]):
                            # Outside the payload hash: aliases never version.
                            mapped["aliases"] = list(alias_map[row["id"]])
                            self.aliased += 1
                        stats["rows"] += 1
                        if mapped["fields"]["conversation_kind"] == "unknown":
                            stats["unknown_kind"] += 1
                        stats.setdefault("first", mapped["happened_at"])
                        stats["first"] = earlier(stats["first"], mapped["happened_at"])
                        stats["last"] = later(stats.get("last"), mapped["happened_at"])
                        yield mapped
                    cursor["archive"] = {
                        "phase": "messages",
                        "after": source.position(batch[-1]),
                        "quarantined": sorted(quarantined),
                    }
                    yield Checkpoint(cursor=copy.deepcopy(cursor))
                cursor["archive"] = {
                    "phase": "links",
                    "quarantined": sorted(quarantined),
                }
                yield Checkpoint(cursor=copy.deepcopy(cursor))
            for batch in source.batches(
                cursor.get("archive", {}).get("after", ""), self.size, links=True
            ):
                for row in batch:
                    net = network(row.get("platform"))
                    target = target_for(row)
                    if target and row["id"] not in quarantined:
                        yield Record(
                            kind="link",
                            data=reply_link("chat." + net, row["id"], target),
                        )
                cursor["archive"] = {
                    "phase": "links",
                    "after": source.position(batch[-1]),
                    "quarantined": sorted(quarantined),
                }
                yield Checkpoint(cursor=copy.deepcopy(cursor))
            yield from self.archive_coverage(chats, conversations)
            logging.warning("Beeper archive quarantined=%d", self.quarantined)
            cursor["archive"] = {"done": True}
            yield Checkpoint(cursor=copy.deepcopy(cursor))
        finally:
            self.close_enricher(enricher)
            source.close()

    def archive_coverage(self, chats, conversations):
        """One report per (network, chat) read by this pass over the archive.

        A chat read fully is complete; quarantined rows make it partial with
        quarantined_rows (their count in detail, no window, so no gap opens
        for rows that cannot be mapped). An unknown conversation kind is
        detail, never a failure. A history status the archive recorded for
        the chat (history_status) is sent as the status when the read itself
        was complete: the core keeps unexpected words raw and degrades them.
        """
        for (channel, chat), stats in sorted(chats.items()):
            conversation = conversations.get(chat) or {}
            detail = {"rows": stats["rows"]}
            reasons = []
            status = "complete"
            if stats["unknown_kind"]:
                detail["conversation_kind"] = "unknown"
                detail["unknown_kind_rows"] = stats["unknown_kind"]
            if stats["quarantined"]:
                status = "partial"
                reasons.append("quarantined_rows")
                detail["quarantined"] = stats["quarantined"]
            word, source_detail = source_status(conversation)
            if word is not None:
                detail["source_status"] = word
                if source_detail is not None:
                    detail["source_detail"] = source_detail
                if status == "complete":
                    status = word
            closes = [
                g["id"] for g in self.open_gaps.get((channel, chat), []) if not reasons
            ]
            yield coverage(
                channel,
                scope_key=chat,
                status=status,
                reasons=reasons,
                window=(stats["first"], stats["last"])
                if status == "complete" and stats.get("first")
                else None,
                high_water=stats.get("last"),
                rows_delivered=stats["rows"],
                closes=closes,
                detail=detail,
            )

    @staticmethod
    def candidates(source, enricher):
        """(time, network, ID, body, attachments) of rows that may need work."""
        for row in source.candidates(
            links=enricher.instagram_on or enricher.youtube_on,
            attachments=enricher.attachments_on,
        ):
            try:
                at = timestamp(row.get("sent_at") or row.get("first_imported_at"))
                attachments = decoded(row.get("attachments_json"), [])
            except (ValueError, TypeError):
                continue  # Quarantined by the stream.
            net = network(row.get("platform"))
            yield at, net, row["id"], row.get("body"), attachments

    def close_enricher(self, enricher):
        if enricher is not None:
            self.enrichment.update(enricher.stats)
            if enricher.stats:
                logging.warning(
                    "Beeper enrichment %s", dict(sorted(enricher.stats.items()))
                )
            self.pacing = enricher.report()
            if self.pacing:
                logging.warning("Beeper enrichment pacing %s", self.pacing)
            enricher.close()

    def revisit(self, enricher, cursor):
        """Finish live messages a limit held back on earlier runs, newest first.

        A queued message is mapped again from its stored source row; it is
        sent when this run did its enrichment or nothing is left to do (core
        keeps an unchanged message at its version), and stays queued while a
        limit still holds part of it back.
        """
        done = []
        for external_id, row, chat, account in enricher.queued():
            if not enricher.has_allowance():
                break
            try:
                mapped = live_message(row, chat, account, enricher)
            except (ValueError, TypeError, KeyError, AttributeError):
                done.append(external_id)
                continue
            if enricher.last["changed"] or not enricher.last["deferred"]:
                self.declare({mapped["channel"].removeprefix("chat.")})
                yield mapped
            if not enricher.last["deferred"]:
                done.append(external_id)
            if len(done) >= 500:
                yield Checkpoint(cursor=copy.deepcopy(cursor))
                enricher.forget(done)
                done = []
        if done:
            yield Checkpoint(cursor=copy.deepcopy(cursor))
            enricher.forget(done)

    def live_records(self, cursor, mode):
        source = Desktop(
            self.settings["base_url"], self.secrets["token"], self.settings["recording"]
        )
        enricher = None
        try:
            enricher = self.enricher(source)
            accounts = {a["accountID"]: a for a in source.accounts()}
            self.declare(
                {
                    network(a.get("network") or a.get("bridge", {}).get("type"))
                    for a in accounts.values()
                }
            )
            states = cursor.setdefault("live", {})
            for chat in source.chats():
                account = accounts.get(
                    chat["accountID"], {"network": chat.get("network")}
                )
                net = network(account.get("network") or chat.get("network"))
                self.declare({net})
                key = chat["id"]
                state = states.setdefault(key, {})
                incremental = state.get("syncing") or (
                    state.get("complete") and state.get("newest") and mode == "sync"
                )
                if incremental:
                    state["syncing"] = True
                direction = "after" if incremental else "before"
                position = state.get("newest") if incremental else state.get("older")
                # Only replies whose target is not yet known to be stored stay in
                # the cursor. A link is sent once both of its ends were
                # acknowledged by an earlier checkpoint of this run.
                replies = state.setdefault("replies", [])
                stored, fresh = set(), set()
                delivered = 0
                failure = None
                try:
                    for page in source.pages(key, position, direction):
                        for ch, mid, target in replies:
                            if target in stored:
                                yield Record(
                                    kind="link", data=reply_link(ch, mid, target)
                                )
                        replies[:] = [r for r in replies if r[2] not in stored]
                        for row in page["items"]:
                            try:
                                mapped = live_message(row, chat, account, enricher)
                            except (ValueError, TypeError, KeyError, AttributeError):
                                self.quarantined += 1
                                logging.warning(
                                    "Beeper quarantined id=%s", row.get("id")
                                )
                                continue
                            if enricher.last["deferred"]:
                                enricher.remember(
                                    mapped["external_id"],
                                    mapped["happened_at"],
                                    row,
                                    chat,
                                    account,
                                )
                            else:
                                enricher.forget([mapped["external_id"]])
                            yield mapped
                            delivered += 1
                            state["newest_at"] = later(
                                state.get("newest_at"), mapped["happened_at"]
                            )
                            state["oldest_at"] = earlier(
                                state.get("oldest_at"), mapped["happened_at"]
                            )
                            if not incremental:
                                state["history_rows"] = state.get("history_rows", 0) + 1
                            fresh.add(mapped["external_id"])
                            if mapped["fields"]["reply_to"]:
                                replies.append(
                                    [
                                        mapped["channel"],
                                        mapped["external_id"],
                                        mapped["fields"]["reply_to"],
                                    ]
                                )
                        if incremental:
                            state["newest"] = (
                                page["newestCursor"]
                                if page["items"]
                                else state.get("newest")
                            )
                        else:
                            if not state.get("newest"):
                                state["newest"] = page["newestCursor"]
                            state["older"] = page["oldestCursor"]
                        if not incremental:
                            state["complete"] = not page["hasMore"]
                        yield Checkpoint(cursor=copy.deepcopy(cursor))
                        stored |= fresh
                        fresh.clear()
                except SourceError as error:
                    if error.auth:
                        raise
                    # History loading failed for this chat only (SMS history
                    # loads fail intermittently): its cursor stays where the
                    # last acknowledged page left it, the window is reported
                    # as a gap and the next run loads it again.
                    logging.warning(
                        "Beeper history load failed for a %s chat: %s", net, error
                    )
                    failure = error
                if failure is None:
                    # The rest target this run's last page, an earlier run, or a
                    # message the destination never received; execute drops the last.
                    for ch, mid, target in replies:
                        yield Record(kind="link", data=reply_link(ch, mid, target))
                    state.pop("replies", None)
                    state.pop("syncing", None)
                # Reported after the chat's messages, before its checkpoint.
                yield self.live_coverage(
                    net, "beeper:" + key, state, incremental, delivered, failure
                )
                yield Checkpoint(cursor=copy.deepcopy(cursor))
            yield from self.revisit(enricher, cursor)
        finally:
            self.close_enricher(enricher)
            source.close()

    def live_coverage(self, net, chat, state, incremental, delivered, failure):
        """The coverage report of one Desktop chat after this run.

        - A failed history load: partial with source_error and retry_pending,
          pending_retries and an explicit gap over the window that did not
          load (newer than the newest stored message for an incremental load,
          everything older than the oldest one for a history load).
        - A network without history listing: best_effort (no gap opens).
        - A history that ended at the network's cap: partial with source_cap.
        - Otherwise complete. Every report except a failure or an unfinished
          history (partial_history) closes the open gaps of the chat by id:
          their window loaded in this run.
        Every chat is reported on every run, also when nothing changed.
        """
        channel = "chat." + net
        open_gaps = self.open_gaps.get((channel, chat), [])
        detail = {"direction": "after" if incremental else "before"}
        if failure is not None:
            now = datetime.now(timezone.utc).isoformat()
            if incremental:
                window = {"from": state.get("newest_at") or EPOCH, "to": now}
            else:
                window = {"from": EPOCH, "to": state.get("oldest_at") or now}
            detail["error"] = str(failure)[:200]
            data = coverage(
                channel,
                scope_key=chat,
                status="partial",
                reasons=["source_error", "retry_pending"],
                high_water=state.get("newest_at"),
                rows_delivered=delivered,
                gaps=[dict(window, reason="source_error")],
                detail=detail,
            ).data
            data["pending_retries"] = len(open_gaps) + 1
            return Record(kind="coverage", data=data)
        closes = [g["id"] for g in open_gaps]
        reasons = []
        status = "complete"
        if net in self.no_history:
            status = "best_effort"
            detail["history_listing"] = False
        elif (
            state.get("complete")
            and net in self.caps
            and state.get("history_rows", 0) >= self.caps[net]
        ):
            status = "partial"
            reasons.append("source_cap")
            detail["cap"] = self.caps[net]
            detail["history_rows"] = state.get("history_rows", 0)
        elif not state.get("complete"):
            # Possible only when the source stopped paging without an error.
            status = "partial"
            reasons.append("partial_history")
            # The older window has not loaded yet: open gaps stay open.
            closes = []
        return coverage(
            channel,
            scope_key=chat,
            status=status,
            reasons=reasons,
            high_water=state.get("newest_at"),
            rows_delivered=delivered,
            closes=closes,
            detail=detail,
        )

    def deliver(self, client, links):
        """Store links; drop, by bisection, those whose message is absent."""
        try:
            client.ingest([], links=links)
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 400:
                raise
            if len(links) == 1:
                try:
                    detail = error.response.json().get("detail")
                except (ValueError, AttributeError):
                    detail = None
                if detail != "link message does not exist":
                    raise
                # Deleted, outside the bridged history, or quarantined here.
                self.unlinked += 1
                logging.warning(
                    "Beeper dropped reply link from id=%s: an end is not stored",
                    links[0]["from"]["external_id"],
                )
                return
            half = len(links) // 2
            self.deliver(client, links[:half])
            self.deliver(client, links[half:])

    def execute(self, client, mode, dry_run=False):
        # Unlike the SDK default, batch source links instead of issuing one HTTP
        # request per reply. Discover channels before registration via first next.
        # Dry-run renders cached enrichment but never downloads or converts.
        # Coverage follows the SDK runner: an attempt before the run, reports
        # after the messages yielded before them, and partial run_failed for
        # every channel when the run fails (all best effort, never masking it).
        self.dry_run = dry_run
        features = set() if dry_run else core_features(client)
        self.aliases_on = "message_aliases" in features
        self.open_gaps = {}
        if not dry_run:
            # The plugin keeps no gap state of its own: open gaps are read back
            # from the core and closed by id once their window reloads.
            for gap in quietly("coverage gaps", lambda: client.coverage_gaps()) or []:
                if isinstance(gap, dict) and gap.get("id") is not None:
                    key = (gap.get("channel"), gap.get("scope_key") or "")
                    self.open_gaps.setdefault(key, []).append(gap)
        covered = Counter()
        gaps_opened = []
        try:
            return self._execute(client, mode, dry_run, covered, gaps_opened)
        except BaseException as error:
            if not dry_run:
                redactor = Redactor(
                    list(self.secrets.values()) + [os.getenv("THROUGHLINE_TOKEN", "")]
                )
                detail = {
                    "error": type(error).__name__,
                    "message": redactor.clean(str(error))[:500],
                }
                failed = [
                    {
                        "channel": c.name,
                        "status": "partial",
                        "reasons": ["run_failed"],
                        "detail": detail,
                    }
                    for c in self.manifest.channels
                ]
                quietly("coverage failure report", lambda: client.coverage(failed))
            raise

    def _execute(self, client, mode, dry_run, covered, gaps_opened):
        records = iter(self.records({} if dry_run else client.cursor(), mode))
        first = next(records, None)
        client.register(self.manifest)
        registered = {c.name for c in self.manifest.channels}
        if not dry_run:
            quietly(
                "coverage attempt",
                lambda: client.coverage_attempt(sorted(registered)),
            )
        counts, messages, links, reports = Counter(), [], [], []
        start = end = None
        total = pending_bytes = 0

        def report_coverage():
            # Reports name only channels registered before them.
            while reports:
                batch = reports[:500]
                del reports[:500]
                answer = quietly("coverage report", lambda: client.coverage(batch))
                if not isinstance(answer, dict):
                    continue
                covered["records"] += len(batch)
                for key in ("stored", "refused"):
                    covered[key] += int(answer.get(key) or 0)
                covered["degraded"] += len(answer.get("degraded") or [])
                covered["gaps_closed"] += len(answer.get("gaps_closed") or [])
                gaps_opened.extend(
                    g["id"]
                    for g in answer.get("gaps_opened") or []
                    if isinstance(g, dict) and g.get("id") is not None
                )

        def flush():
            nonlocal total, pending_bytes
            pending_bytes = 0
            if messages or links:
                try:
                    client.ingest(messages, links=links, dry_run=dry_run)
                except httpx.HTTPStatusError as error:
                    if not links or error.response.status_code != 400:
                        raise
                    # A link with a missing end fails its whole batch. Store
                    # the messages, then every link that can be stored.
                    if messages:
                        client.ingest(messages, dry_run=dry_run)
                    self.deliver(client, list(links))
                total += len(messages)
                messages.clear()
                links.clear()
            report_coverage()

        from itertools import chain

        try:
            for item in chain([] if first is None else [first], records):
                value = (
                    item.model_dump()
                    if isinstance(item, (Record, Checkpoint))
                    else item
                )
                if contains_secret(value, self.secrets.values()):
                    raise ValueError("declared secret found in generated record")
                if isinstance(item, Checkpoint):
                    flush()
                    if not dry_run:
                        client.checkpoint(item.cursor)
                elif isinstance(item, Record):
                    if dry_run:
                        continue
                    if item.kind == "coverage":
                        if item.data.get("channel") not in registered:
                            flush()
                            client.register(self.manifest)
                            registered = {c.name for c in self.manifest.channels}
                        reports.append(item.data)
                        if len(reports) >= 500:
                            flush()
                    else:
                        links.append(item.data)
                else:
                    if item["channel"] not in registered:
                        flush()
                        client.register(self.manifest)
                        registered = {c.name for c in self.manifest.channels}
                    messages.append(item)
                    counts[item["channel"]] += 1
                    at = datetime.fromisoformat(item["happened_at"])
                    start = min(start, at) if start else at
                    end = max(end, at) if end else at
                    pending_bytes += len(item["content"])
                    for name in (
                        "attachment_files",
                        "instagram_links",
                        "youtube_links",
                    ):
                        if name in item["fields"]:
                            pending_bytes += len(json.dumps(item["fields"][name]))
                if (
                    len(messages) >= 5000
                    or len(links) >= 2000
                    or pending_bytes >= REQUEST_BYTES
                ):
                    flush()
            flush()
            if not dry_run:
                client.heartbeat(
                    interval_s=self.manifest.default_interval_s, messages_ingested=total
                )
            result = dict(
                counts=dict(counts),
                start=start.isoformat() if start else None,
                end=end.isoformat() if end else None,
                estimated_model_cost=0,
                quarantined=self.quarantined,
                unlinked=self.unlinked,
                **(
                    {"enrichment": dict(sorted(self.enrichment.items()))}
                    if self.enrichment
                    else {}
                ),
                **self.pacing,
            )
            if covered or gaps_opened:
                result["coverage"] = dict(
                    {
                        key: covered[key]
                        for key in (
                            "records",
                            "stored",
                            "degraded",
                            "refused",
                            "gaps_closed",
                        )
                    },
                    gaps_opened=list(gaps_opened),
                )
            if self.aliased:
                result["aliased"] = self.aliased
            return result
        finally:
            records.close()


FLAGS = {
    "--attachments": ("BEEPER_ATTACHMENTS", None),
    "--instagram-links": ("BEEPER_INSTAGRAM_LINKS", None),
    "--youtube-links": ("BEEPER_YOUTUBE_LINKS", None),
    "--attachment-limit": ("BEEPER_ATTACHMENT_LIMIT", int),
    "--instagram-limit": ("BEEPER_INSTAGRAM_LIMIT", int),
    "--youtube-limit": ("BEEPER_YOUTUBE_LIMIT", int),
    "--attachments-per-day": ("BEEPER_ATTACHMENTS_PER_DAY", str),
    "--instagram-per-day": ("BEEPER_INSTAGRAM_PER_DAY", str),
    "--youtube-per-day": ("BEEPER_YOUTUBE_PER_DAY", str),
    "--transcribe-minutes-per-day": ("THROUGHLINE_TRANSCRIBE_MINUTES_PER_DAY", str),
    "--backlog": ("BEEPER_BACKLOG", None),
    "--enrich-since": ("BEEPER_ENRICH_SINCE", str),
    "--transcript-source": ("BEEPER_YOUTUBE_TRANSCRIPT_SOURCE", str),
    "--cookies": ("BEEPER_YTDLP_COOKIES", str),
    "--cookies-from-browser": ("BEEPER_YTDLP_COOKIES_FROM_BROWSER", str),
    "--transcriber": ("BEEPER_TRANSCRIBER", str),
}


def enrichment_flags(argv):
    """Map the plugin's own flags onto its environment settings.

    The SDK runner owns the remaining arguments (command, --dry-run, --once).
    """
    return parse_flags(argv, FLAGS)


def main(argv=None):
    import os
    import sys

    env, rest = enrichment_flags(sys.argv[1:] if argv is None else argv)
    os.environ.update(env)
    run(Beeper, rest)
