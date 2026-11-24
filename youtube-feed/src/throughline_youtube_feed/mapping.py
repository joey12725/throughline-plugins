"""Deterministic mapping of fetch.py raw records to Throughline messages.

Uploads: one message per video (external ID = video ID) in youtube.uploads,
threaded per YouTube channel, at the published time. Picks: one message per
(video, scorer) decision in youtube.picks, at the scoring time, with the
video's published time as subject and a `pick_of` link to the upload.
"""

from . import fetch

MAPPING_REVISION = 1
UPLOADS = "youtube.uploads"
PICKS = "youtube.picks"
PAYLOAD_DROP = ("fetched_at", "observed", "details_error")


def _fields(**values):
    return [dict(name=k, type=v[0], description=v[1]) for k, v in values.items()]


COMMON = dict(
    mapping_revision=("integer", "Mapping revision; a change re-versions records"),
    video_id=("text", "YouTube video ID"),
    url=("text", "Watch or Shorts URL"),
    title=("text", "Video title"),
    youtube_channel_id=("text", "Publishing channel ID"),
    channel_title=("text", "Publishing channel title"),
    duration_s=("integer", "Video length in seconds when known"),
)
UPLOAD_FIELDS = _fields(
    **COMMON,
    description=("text", "Video description when the source supplied one"),
    is_short=("boolean", "Shorts flag; null when unknown"),
    is_live=("boolean", "Live or upcoming broadcast when known"),
    transport=(
        "text",
        "rss, page (channel videos page) or prediction (imported score only)",
    ),
    time_precision=(
        "text",
        "exact or day (published time), approximate (estimated from relative page text) or scored (no published time; scoring time used)",
    ),
)
PICK_FIELDS = _fields(
    **COMMON,
    verdict=("text", "pick or skip"),
    picked=("boolean", "verdict is pick"),
    watch_probability=("number", "Model probability of watching (0-1)"),
    progress_if_watched=("number", "Expected percent watched if started (0-100)"),
    junk_food_probability=("number", "Model probability of low-value content (0-1)"),
    watch_later_probability=("number", "Model probability of saving for later (0-1)"),
    is_junk_food=("boolean", "Scorer's junk-food flag"),
    reason=("text", "Model's short reason when given"),
    scorer=("text", "Model or imported scorer name"),
    origin=("text", "router, predictions or flagged"),
    scored_at=("timestamp", "When the decision was made"),
    thresholds=(
        "json",
        "Thresholds used for the verdict; null when the import supplied the verdict",
    ),
    playlist_id=("text", "Playlist the video was added to by this plugin"),
    playlist_added_at=("timestamp", "When it was added"),
)


def payload(record):
    return {k: v for k, v in record.items() if k not in PAYLOAD_DROP}


def best_uploads(records):
    """The most complete upload record of each video (latest on ties)."""
    best = {}
    for record in records:
        if record.get("kind") != "upload" or not record.get("id"):
            continue
        key = (
            fetch.upload_rank(record),
            record.get("fetched_at") or "",
            fetch.digest(payload(record)),
        )
        current = best.get(record["id"])
        if current is None or key > current[0]:
            best[record["id"]] = (key, record)
    return {vid: item[1] for vid, item in best.items()}


def synthesized_upload(decision):
    """An upload record from what a scorer saw, when no feed entry exists."""
    video = decision.get("video") or {}
    return {
        "kind": "upload",
        "id": video.get("video_id"),
        "channel_id": video.get("channel_id"),
        "transport": "prediction",
        "raw": {
            "video_id": video.get("video_id"),
            "channel_id": video.get("channel_id"),
            "channel_name": video.get("channel_title"),
            "title": video.get("title"),
            "url": video.get("url"),
            "published_at": video.get("published_at"),
            "duration_seconds": video.get("duration_s"),
            "scored_at": decision.get("scored_at"),
        },
    }


def _short(value):
    if not value:
        return None
    hours, rest = divmod(int(value), 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"


def upload_message(record):
    """(message, links) for one upload record, or None without any time."""
    video = fetch.normalize_upload(record)
    vid = video["video_id"]
    at, precision = video["published_at"], video["time_precision"]
    if not at:
        at = fetch.iso(fetch.parse_time((record.get("raw") or {}).get("scored_at")))
        precision = "scored"
    if not at:
        return None
    cid = video["channel_id"]
    channel = video["channel_title"] or cid or "Unknown channel"
    title = video["title"] or vid
    head = f"{channel} uploaded {title}"
    extras = [
        x
        for x in ("Short" if video["is_short"] else None, _short(video["duration_s"]))
        if x
    ]
    if extras:
        head += f" ({', '.join(extras)})"
    content = head
    if video["description"]:
        content += "\n\n" + video["description"][:2000]
    message = dict(
        channel=UPLOADS,
        external_id=vid,
        thread={
            "external_id": "channel:" + (cid or "unknown"),
            "title": video["channel_title"] or cid or "Unknown channel",
        },
        happened_at=at,
        content=content,
        fields=dict(
            mapping_revision=MAPPING_REVISION,
            video_id=vid,
            url=video["url"],
            title=video["title"],
            youtube_channel_id=cid,
            channel_title=video["channel_title"],
            duration_s=video["duration_s"],
            description=video["description"],
            is_short=video["is_short"],
            is_live=video["is_live"],
            transport=record.get("transport"),
            time_precision=precision,
        ),
        payload=payload(record),
    )
    if cid:
        message["author"] = {
            "system": "youtube",
            "kind": "channel_id",
            "value": cid,
            "display_name": video["channel_title"] or cid,
        }
    work = {"kind": "work", "external_ref": "youtube:" + vid, "name": title}
    links = [
        {
            "type": "upload_of",
            "from": {"channel": UPLOADS, "external_id": vid},
            "to": work,
            "method": "source",
        }
    ]
    if cid:
        links.append(
            {
                "type": "published_by",
                "from": work,
                "to": {
                    "kind": "organization",
                    "external_ref": "youtube:" + cid,
                    "name": video["channel_title"] or cid,
                },
                "method": "source",
            }
        )
    return message, links


def _pct(value, scale=100):
    return "?" if value is None else f"{value * scale:.0f}%"


def pick_message(pick_id, decision, sources, adds, linked):
    """(message, links) for one decision, or None without a scoring time."""
    video = decision.get("video") or {}
    vid = video.get("video_id")
    at = decision.get("scored_at") or video.get("published_at")
    if not at or not vid:
        return None
    add = sorted(adds, key=lambda a: (a.get("added_at") or "", a.get("id") or ""))
    add = add[0] if add else None
    picked = decision.get("verdict") == "pick"
    channel = video.get("channel_title") or video.get("channel_id") or "unknown channel"
    content = (
        f"{'Picked' if picked else 'Skipped'} {video.get('title') or vid} by {channel}: "
        f"watch {_pct(decision.get('watch_probability'))}, "
        f"progress {_pct(decision.get('progress_if_watched'), 1)}, "
        f"junk {_pct(decision.get('junk_food_probability'))}"
    )
    if decision.get("reason"):
        content += f". {decision['reason']}"
    if add:
        content += (
            f". Added to playlist {add.get('playlist') or add.get('playlist_id')}"
        )
    message = dict(
        channel=PICKS,
        external_id=pick_id,
        thread={
            "external_id": "scorer:" + str(decision.get("scorer")),
            "title": f"Picks by {decision.get('scorer')}",
        },
        happened_at=at,
        content=content,
        fields=dict(
            mapping_revision=MAPPING_REVISION,
            video_id=vid,
            url=video.get("url"),
            title=video.get("title"),
            youtube_channel_id=video.get("channel_id"),
            channel_title=video.get("channel_title"),
            duration_s=video.get("duration_s"),
            verdict=decision.get("verdict"),
            picked=picked,
            watch_probability=decision.get("watch_probability"),
            progress_if_watched=decision.get("progress_if_watched"),
            junk_food_probability=decision.get("junk_food_probability"),
            watch_later_probability=decision.get("watch_later_probability"),
            is_junk_food=decision.get("is_junk_food"),
            reason=decision.get("reason"),
            scorer=decision.get("scorer"),
            origin=decision.get("origin"),
            scored_at=decision.get("scored_at"),
            thresholds=decision.get("thresholds"),
            playlist_id=add.get("playlist_id") if add else None,
            playlist_added_at=add.get("added_at") if add else None,
        ),
        payload={
            "sources": sorted(
                (payload(s) for s in sources), key=lambda s: fetch.digest(s)
            ),
            "playlist_adds": sorted(
                (payload(a) for a in adds), key=lambda a: str(a.get("id"))
            ),
        },
    )
    published = video.get("published_at")
    if published and published != at:
        message["subject"] = {"start": published}
        if video.get("time_precision") in ("day", "approximate"):
            message["subject"]["start_precision"] = video["time_precision"]
    links = []
    if linked:
        links.append(
            {
                "type": "pick_of",
                "from": {"channel": PICKS, "external_id": pick_id},
                "to": {"channel": UPLOADS, "external_id": vid},
                "method": "source",
            }
        )
    return message, links
