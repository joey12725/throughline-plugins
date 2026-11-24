"""Pure, deterministic mapping of raw fetch.py post and story records to messages.

The message payload is a stable projection of the raw node: engagement
counts, view counts, pinned flags and signed (expiring) CDN media URLs are
left out, so fetching the same post or active 24h story again creates no new
version, while a changed caption, location, carousel or sticker does. The full
raw node stays in the JSONL export.
"""

import re
import urllib.parse
from datetime import datetime, timezone

CHANNEL = "instagram.feed"
# Bump when the message shape produced from the same raw record changes.
MAPPING_REVISION = 2
SOURCE = "web_profile_info"
STORY_SOURCE = "reels_media"
STORY_TTL_S = 86400
TYPES = ("image", "video", "carousel", "reel", "story")
HASHTAG = re.compile(r"(?<![\w&])#(\w+)", re.UNICODE)
MENTION = re.compile(r"(?<![\w@])@([A-Za-z0-9._]{1,30})(?<!\.)")


def fields(**values):
    return [dict(name=k, type=v[0], description=v[1]) for k, v in values.items()]


FIELDS = fields(
    url=(
        "text",
        "Canonical URL (https://www.instagram.com/p/<shortcode>/, "
        "/reel/<shortcode>/, or /stories/<handle>/<story_id>/)",
    ),
    shortcode=("text", "Instagram shortcode; the stable post id in URLs (when supplied)"),
    media_id=("text", "Instagram media id, when supplied"),
    post_type=("text", "image, video, carousel, reel or story"),
    caption=("text", "Full caption or story text"),
    account=("text", "Posting account handle (lower case)"),
    account_label=("text", "Label from the accounts config"),
    account_category=("text", "Category from the accounts config"),
    owner_id=("text", "Instagram numeric user id of the posting account"),
    carousel_count=("integer", "Number of items in a carousel; null otherwise"),
    is_video=("boolean", "The post/story (or its cover item) is a video"),
    video_duration_s=("number", "Video length in seconds when supplied"),
    location_name=("text", "Tagged location name (post location or story sticker)"),
    location_id=("text", "Tagged location id"),
    accessibility_caption=("text", "Instagram's generated image description"),
    hashtags=("json", "Hashtags in the caption or stickers, lower case, in order"),
    mentions=("json", "@handles in the caption or mention stickers, lower case, in order"),
    coauthors=("json", "Collaborator handles (co-authored posts)"),
    tagged=("json", "Handles tagged in the media"),
    story_id=("text", "Instagram numeric story item id; null for posts"),
    expires_at=("text", "ISO-8601 UTC expiry time for 24h stories; null for posts"),
    story_media_type=("text", "image or video for stories; null for posts"),
    story_links=("json", "External link sticker URLs attached to a story"),
    story_stickers=("json", "Structured non-expiring summary of stickers on a story"),
    source=("text", "Fetch path: web_profile_info, web_profile_html or reels_media"),
    mapping_revision=("integer", "Revision of this plugin's mapping"),
)


def _text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def _caption(node):
    edges = ((node.get("edge_media_to_caption") or {}).get("edges")) or []
    if edges:
        return _text(((edges[0] or {}).get("node") or {}).get("text"))
    caption = node.get("caption")
    if isinstance(caption, dict):
        return _text(caption.get("text"))
    return _text(caption)


def _taken_at(node):
    value = node.get("taken_at_timestamp", node.get("taken_at"))
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _expiring_at(node, taken):
    value = node.get("expiring_at")
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = None
    if value and value > 0:
        return value
    return taken + STORY_TTL_S if taken else None


def _children(node):
    edges = ((node.get("edge_sidecar_to_children") or {}).get("edges")) or []
    children = [e.get("node") for e in edges if isinstance(e, dict)]
    if not children and isinstance(node.get("carousel_media"), list):
        children = node["carousel_media"]
    return [c for c in children if isinstance(c, dict)]


def _is_video(node):
    if "is_video" in node:
        return bool(node["is_video"])
    return node.get("media_type") == 2 or node.get("__typename") in (
        "GraphVideo",
        "XIGPolarisVideoMedia",
    )


def post_type(node):
    product = node.get("product_type")
    if product == "story":
        return "story"
    if product == "clips":
        return "reel"
    if (
        node.get("__typename")
        in ("GraphSidecar", "XDTGraphSidecar", "XIGPolarisCarouselMedia")
        or product == "carousel_container"
        or node.get("media_type") == 8
        or _children(node)
    ):
        return "carousel"
    return "video" if _is_video(node) else "image"


def post_url(shortcode, kind):
    return f"https://www.instagram.com/{'reel' if kind == 'reel' else 'p'}/{shortcode}/"


def story_url(handle, owner_id, story_id):
    who = handle or owner_id or "story"
    return f"https://www.instagram.com/stories/{who}/{story_id}/"


def _handles(items, key="username"):
    out = []
    for item in items or []:
        if isinstance(item, dict):
            item = item.get("node", item)
            item = (
                item.get("user", item) if isinstance(item.get("user"), dict) else item
            )
            value = item.get(key)
        else:
            value = item
        if isinstance(value, str) and value and value.lower() not in out:
            out.append(value.lower())
    return out


def _tagged(node):
    edges = ((node.get("edge_media_to_tagged_user") or {}).get("edges")) or []
    users = [((e or {}).get("node") or {}).get("user") for e in edges]
    if not users and isinstance(node.get("usertags"), dict):
        users = [(t or {}).get("user") for t in node["usertags"].get("in") or []]
    return _handles([u for u in users if isinstance(u, dict)])


def _unique(values):
    out = []
    for value in values:
        if value not in out:
            out.append(value)
    return out


def _unwrap_ig_link(raw_url):
    text = _text(raw_url)
    if not text:
        return None
    parts = urllib.parse.urlsplit(text)
    if parts.hostname in ("l.instagram.com", "lm.instagram.com"):
        target = urllib.parse.parse_qs(parts.query).get("u")
        if target and target[0]:
            return _text(target[0]) or text
    return text


def _story_stickers_and_meta(node):
    """Extract stable story stickers, links, mentions, hashtags, and location."""
    links = []
    stickers = []
    mentions = []
    hashtags = []
    location = None

    for item in node.get("story_link_stickers") or []:
        if not isinstance(item, dict):
            continue
        sl = item.get("story_link") if isinstance(item.get("story_link"), dict) else item
        url = _unwrap_ig_link(sl.get("url") or sl.get("link_url") or sl.get("webUri"))
        title = _text(sl.get("link_title") or sl.get("display_url"))
        if url:
            links.append(url)
            entry = {"type": "link", "url": url}
            if title:
                entry["title"] = title
            stickers.append(entry)

    for cta in node.get("story_cta") or []:
        if not isinstance(cta, dict):
            continue
        for link in cta.get("links") or []:
            if not isinstance(link, dict):
                continue
            url = _unwrap_ig_link(
                link.get("webUri") or link.get("web_uri") or link.get("url")
            )
            if url:
                links.append(url)
                stickers.append({"type": "link", "url": url})

    for item in node.get("reel_mentions") or []:
        if not isinstance(item, dict):
            continue
        user = item.get("user") if isinstance(item.get("user"), dict) else item
        uname = _text(user.get("username"))
        if uname:
            handle = uname.lower()
            mentions.append(handle)
            stickers.append({"type": "mention", "username": handle})

    for item in node.get("story_hashtags") or []:
        if not isinstance(item, dict):
            continue
        tag = item.get("hashtag") if isinstance(item.get("hashtag"), dict) else item
        name = _text(tag.get("name"))
        if name:
            clean = name.lstrip("#").lower()
            if clean:
                hashtags.append(clean)
                stickers.append({"type": "hashtag", "name": clean})

    for item in node.get("story_locations") or []:
        if not isinstance(item, dict):
            continue
        loc = item.get("location") if isinstance(item.get("location"), dict) else item
        name = _text(loc.get("name"))
        loc_id = loc.get("pk") or loc.get("id")
        if name or loc_id is not None:
            entry = {"type": "location"}
            if name:
                entry["name"] = name
            if loc_id is not None:
                entry["id"] = str(loc_id)
            stickers.append(entry)
            if location is None:
                location = {"name": name, "id": str(loc_id) if loc_id is not None else None}

    for item in node.get("story_polls") or []:
        if not isinstance(item, dict):
            continue
        poll = (
            item.get("poll_sticker")
            if isinstance(item.get("poll_sticker"), dict)
            else item
        )
        question = _text(poll.get("question"))
        tallies = [
            _text(t.get("text"))
            for t in (poll.get("tallies") or [])
            if isinstance(t, dict) and _text(t.get("text"))
        ]
        if question or tallies:
            entry = {"type": "poll"}
            if question:
                entry["question"] = question
            if tallies:
                entry["options"] = tallies
            stickers.append(entry)

    for item in node.get("story_questions") or []:
        if not isinstance(item, dict):
            continue
        q = (
            item.get("question_sticker")
            if isinstance(item.get("question_sticker"), dict)
            else item
        )
        question = _text(q.get("question"))
        if question:
            stickers.append({"type": "question", "question": question})

    for item in node.get("story_countdowns") or []:
        if not isinstance(item, dict):
            continue
        cd = (
            item.get("countdown_sticker")
            if isinstance(item.get("countdown_sticker"), dict)
            else item
        )
        text = _text(cd.get("text"))
        end_ts = cd.get("end_ts")
        if text or end_ts:
            entry = {"type": "countdown"}
            if text:
                entry["text"] = text
            if end_ts is not None:
                entry["end_ts"] = int(end_ts)
            stickers.append(entry)

    for item in node.get("story_music_stickers") or []:
        if not isinstance(item, dict):
            continue
        info = (
            item.get("music_asset_info")
            if isinstance(item.get("music_asset_info"), dict)
            else item
        )
        title = _text(info.get("title"))
        artist = _text(info.get("display_artist") or info.get("artist"))
        if title or artist:
            entry = {"type": "music"}
            if title:
                entry["title"] = title
            if artist:
                entry["artist"] = artist
            stickers.append(entry)

    for item in node.get("story_feed_media") or []:
        if not isinstance(item, dict):
            continue
        code = _text(item.get("media_code") or item.get("code"))
        mid = item.get("media_id")
        if code or mid is not None:
            entry = {"type": "reshare"}
            if code:
                entry["code"] = code
            if mid is not None:
                entry["media_id"] = str(mid)
            stickers.append(entry)

    return _unique(links), stickers, _unique(mentions), _unique(hashtags), location


def _stable_child(child):
    keep = (
        "id",
        "pk",
        "shortcode",
        "code",
        "__typename",
        "media_type",
        "is_video",
        "accessibility_caption",
        "video_duration",
    )
    return {k: child[k] for k in keep if child.get(k) is not None}


def stable_node(node):
    """The parts of a raw post node that do not change between fetches."""
    keep = (
        "id",
        "pk",
        "shortcode",
        "code",
        "__typename",
        "media_type",
        "product_type",
        "is_video",
        "taken_at_timestamp",
        "taken_at",
        "accessibility_caption",
        "video_duration",
        "title",
        "dimensions",
        "original_width",
        "original_height",
    )
    out = {k: node[k] for k in keep if node.get(k) is not None}
    caption = _caption(node)
    if caption is not None:
        out["caption"] = caption
    location = node.get("location")
    if isinstance(location, dict):
        out["location"] = {
            k: location[k]
            for k in ("id", "pk", "name", "slug")
            if location.get(k) is not None
        }
    children = _children(node)
    if children:
        out["children"] = [_stable_child(c) for c in children]
    owner = node.get("owner") or node.get("user")
    if isinstance(owner, dict):
        out["owner"] = {
            k: owner[k] for k in ("id", "pk", "username") if owner.get(k) is not None
        }
    coauthors = _handles(node.get("coauthor_producers"))
    if coauthors:
        out["coauthors"] = coauthors
    tagged = _tagged(node)
    if tagged:
        out["tagged"] = tagged
    return out


def stable_story_node(node):
    """The parts of a raw story node that do not change between fetches."""
    keep = (
        "id",
        "pk",
        "code",
        "media_type",
        "product_type",
        "is_video",
        "taken_at",
        "taken_at_timestamp",
        "expiring_at",
        "accessibility_caption",
        "video_duration",
        "original_width",
        "original_height",
    )
    out = {k: node[k] for k in keep if node.get(k) is not None}
    caption = _caption(node)
    if caption is not None:
        out["caption"] = caption
    location = node.get("location")
    if isinstance(location, dict):
        out["location"] = {
            k: location[k]
            for k in ("id", "pk", "name", "slug")
            if location.get(k) is not None
        }
    owner = node.get("owner") or node.get("user")
    if isinstance(owner, dict):
        out["owner"] = {
            k: owner[k] for k in ("id", "pk", "username") if owner.get(k) is not None
        }
    links, stickers, _, _, _ = _story_stickers_and_meta(node)
    if links:
        out["story_links"] = links
    if stickers:
        out["story_stickers"] = stickers
    return out


def _resolve_owner(node_owner, profile, fetched):
    # Prefer numeric pk over Graph-scoped id when both are present (Polaris HTML).
    node_owner_id = node_owner.get("pk") or node_owner.get("id")
    node_owner_id = str(node_owner_id) if node_owner_id not in (None, "") else None
    profile_id = str(profile["id"]) if profile.get("id") not in (None, "") else None
    # The fetched profile describes the owner unless the node names a different
    # owner id (a collaboration post seen through another account).
    owner_is_profile = node_owner_id is None or node_owner_id == profile_id
    username = node_owner.get("username")
    if not username and owner_is_profile:
        username = profile.get("username") or fetched
    handle = str(username).lower() if username else None
    owner_id = node_owner_id
    if owner_id is None and (not node_owner or handle == fetched):
        owner_id = profile_id
    return owner_id, handle


def _thread_and_author(owner_id, handle):
    if owner_id:
        thread_id = f"instagram:user:{owner_id}"
        author = {"system": "instagram", "kind": "user_id", "value": owner_id}
    else:
        thread_id = f"instagram:username:{handle}"
        author = {"system": "instagram", "kind": "username", "value": handle}
    author["display_name"] = f"@{handle}" if handle else None
    thread = {
        "external_id": thread_id,
        "title": f"@{handle}" if handle else thread_id,
    }
    return thread, author


def map_post(record):
    """A fetch.py ``post`` record -> message dict, or None for other kinds.

    Raises ValueError for a post without a shortcode or a posted time.
    """
    if record.get("kind") != "post":
        return None
    node = record.get("raw") or {}
    shortcode = str(record.get("id") or node.get("shortcode") or node.get("code") or "")
    if not shortcode:
        raise ValueError("post without shortcode")
    taken = _taken_at(node)
    if taken is None:
        raise ValueError("post without a posted time")
    stable = stable_node(node)
    # Identity comes from the post's own owner, not from the account it was
    # fetched through, so a collaboration post fetched via two followed
    # accounts maps to one identical message. Profile details (display name,
    # verification) are deliberately not part of the message for the same
    # reason; they stay in the export's profile records.
    node_owner = stable.get("owner") or {}
    profile = record.get("owner") or {}
    fetched = str(record.get("account") or "").lower()
    owner_id, handle = _resolve_owner(node_owner, profile, fetched)
    owner = {"id": owner_id, "username": handle}
    config = record.get("account_config") or {}
    kind = post_type(node)
    url = post_url(shortcode, kind)
    caption = stable.get("caption")
    location = stable.get("location") or {}
    children = stable.get("children") or []
    duration = node.get("video_duration")
    try:
        duration = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration = None
    media_id = node.get("pk") or node.get("id")
    loc_id = location.get("pk") if location.get("pk") is not None else location.get("id")
    values = dict(
        url=url,
        shortcode=shortcode,
        media_id=str(media_id) if media_id is not None else None,
        post_type=kind,
        caption=caption,
        account=handle,
        account_label=config.get("label"),
        account_category=config.get("category"),
        owner_id=owner_id,
        carousel_count=len(children) if kind == "carousel" and children else None,
        is_video=_is_video(node),
        video_duration_s=duration,
        location_name=_text(location.get("name")),
        location_id=str(loc_id) if loc_id is not None else None,
        accessibility_caption=_text(node.get("accessibility_caption")),
        hashtags=_unique(h.lower() for h in HASHTAG.findall(caption or "")),
        mentions=_unique(m.lower() for m in MENTION.findall(caption or "")),
        coauthors=stable.get("coauthors") or [],
        tagged=stable.get("tagged") or [],
        story_id=None,
        expires_at=None,
        story_media_type=None,
        story_links=[],
        story_stickers=[],
        source=record.get("source") or SOURCE,
        mapping_revision=MAPPING_REVISION,
    )
    article = "an" if kind == "image" else "a"
    who = f"@{handle}" if handle else "An account"
    if config.get("label"):
        who += f" ({config['label']})"
    head = f"{who} posted {article} {kind}"
    if values["location_name"]:
        head += f" at {values['location_name']}"
    content = (
        f"{head}:\n{caption}\n{url}" if caption else f"{head} (no caption)\n{url}"
    )
    thread, author = _thread_and_author(owner_id, handle)
    payload = {
        "kind": "post",
        "shortcode": shortcode,
        "account": handle,
        "account_config": {k: config[k] for k in sorted(config)},
        "owner": owner,
        "source": values["source"],
        "node": stable,
    }
    return dict(
        channel=CHANNEL,
        external_id=shortcode,
        thread=thread,
        happened_at=datetime.fromtimestamp(taken, timezone.utc).isoformat(),
        author=author,
        content=content,
        fields=values,
        payload=payload,
    )


def _format_sticker_summary(stickers):
    parts = []
    for s in stickers:
        stype = s.get("type")
        if stype == "mention" and s.get("username"):
            parts.append(f"@{s['username']}")
        elif stype == "hashtag" and s.get("name"):
            parts.append(f"#{s['name']}")
        elif stype == "location" and s.get("name"):
            parts.append(f"location: {s['name']}")
        elif stype == "poll" and s.get("question"):
            opts = s.get("options") or []
            suffix = f" ({' / '.join(opts)})" if opts else ""
            parts.append(f"poll: {s['question']}{suffix}")
        elif stype == "question" and s.get("question"):
            parts.append(f"question: {s['question']}")
        elif stype == "countdown" and s.get("text"):
            parts.append(f"countdown: {s['text']}")
        elif stype == "music":
            label = " - ".join(x for x in (s.get("artist"), s.get("title")) if x)
            if label:
                parts.append(f"music: {label}")
        elif stype == "reshare" and s.get("code"):
            parts.append(f"reshare: https://www.instagram.com/p/{s['code']}/")
    return "; ".join(_unique(parts))


def map_story(record):
    """A fetch.py ``story`` record -> message dict, or None for other kinds.

    Uses deterministic ``external_id = "story:<story_id>"`` so 24h stories are
    preserved permanently in Throughline without duplicates across polls.
    Raises ValueError for a story without a story id or posted time.
    """
    if record.get("kind") != "story":
        return None
    node = record.get("raw") or {}
    raw_sid = str(record.get("id") or node.get("pk") or node.get("id") or "").strip()
    sid = raw_sid.split("_", 1)[0] if raw_sid else ""
    if not sid:
        raise ValueError("story without story id")
    taken = _taken_at(node)
    if taken is None:
        raise ValueError("story without a posted time")
    expiring = _expiring_at(node, taken)
    expires_iso = (
        datetime.fromtimestamp(expiring, timezone.utc).isoformat()
        if expiring is not None
        else None
    )
    stable = stable_story_node(node)
    node_owner = stable.get("owner") or {}
    profile = record.get("owner") or {}
    fetched = str(record.get("account") or "").lower()
    owner_id, handle = _resolve_owner(node_owner, profile, fetched)
    owner = {"id": owner_id, "username": handle}
    config = record.get("account_config") or {}
    is_vid = _is_video(node)
    media_kind = "video" if is_vid else "image"
    url = story_url(handle, owner_id, sid)
    caption = stable.get("caption")
    links, stickers, sticker_mentions, sticker_hashtags, sticker_loc = (
        _story_stickers_and_meta(node)
    )
    location = stable.get("location") or {}
    loc_name = _text(location.get("name")) or (
        sticker_loc.get("name") if sticker_loc else None
    )
    raw_loc_id = (
        location.get("pk")
        if location.get("pk") is not None
        else location.get("id")
    )
    if raw_loc_id is None and sticker_loc:
        raw_loc_id = sticker_loc.get("id")
    duration = node.get("video_duration")
    try:
        duration = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration = None
    shortcode = _text(node.get("code") or node.get("shortcode"))
    media_id = node.get("pk") or node.get("id") or sid
    hashtags = _unique(
        [*(h.lower() for h in HASHTAG.findall(caption or "")), *sticker_hashtags]
    )
    mentions = _unique(
        [*(m.lower() for m in MENTION.findall(caption or "")), *sticker_mentions]
    )
    values = dict(
        url=url,
        shortcode=shortcode,
        media_id=str(media_id),
        post_type="story",
        caption=caption,
        account=handle,
        account_label=config.get("label"),
        account_category=config.get("category"),
        owner_id=owner_id,
        carousel_count=None,
        is_video=is_vid,
        video_duration_s=duration,
        location_name=loc_name,
        location_id=str(raw_loc_id) if raw_loc_id is not None else None,
        accessibility_caption=_text(node.get("accessibility_caption")),
        hashtags=hashtags,
        mentions=mentions,
        coauthors=[],
        tagged=[],
        story_id=sid,
        expires_at=expires_iso,
        story_media_type=media_kind,
        story_links=links,
        story_stickers=stickers,
        source=record.get("source") or STORY_SOURCE,
        mapping_revision=MAPPING_REVISION,
    )
    who = f"@{handle}" if handle else "An account"
    if config.get("label"):
        who += f" ({config['label']})"
    head = f"{who} posted a {media_kind} story"
    if loc_name:
        head += f" at {loc_name}"
    body_lines = []
    if caption:
        body_lines.append(caption)
    if links:
        body_lines.append("Links: " + ", ".join(links))
    sticker_summary = _format_sticker_summary(stickers)
    if sticker_summary:
        body_lines.append("Stickers: " + sticker_summary)
    if body_lines:
        content = f"{head}:\n" + "\n".join(body_lines) + f"\n{url}"
    else:
        content = f"{head} (no caption)\n{url}"
    thread, author = _thread_and_author(owner_id, handle)
    payload = {
        "kind": "story",
        "story_id": sid,
        "account": handle,
        "account_config": {k: config[k] for k in sorted(config)},
        "owner": owner,
        "source": values["source"],
        "node": stable,
    }
    return dict(
        channel=CHANNEL,
        external_id=f"story:{sid}",
        thread=thread,
        happened_at=datetime.fromtimestamp(taken, timezone.utc).isoformat(),
        author=author,
        content=content,
        fields=values,
        payload=payload,
    )


def map_record(record):
    """Map a fetch.py ``post`` or ``story`` record to a Throughline message."""
    kind = record.get("kind")
    if kind == "post":
        return map_post(record)
    if kind == "story":
        return map_story(record)
    return None
