"""WebVTT parsing for Zoom audio transcripts.

Zoom writes one cue per utterance chunk with the speaker as a "Name: text"
prefix. `<v Name>` voice spans (other VTT producers) are understood too.
Consecutive cues by one speaker are merged into turns.
"""

import re

TIMING = re.compile(
    r"^\s*(?P<start>(?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})\s+-->\s+"
    r"(?P<end>(?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})"
)
VOICE = re.compile(r"^<v(?:\.[^ >]*)?\s+([^>]+)>(.*?)(?:</v>)?$", re.S)
SPEAKER = re.compile(r"^([^:\n]{1,80}?):\s+(.+)$", re.S)
TAG = re.compile(r"</?[a-zA-Z][^>]*>")


def seconds(stamp):
    parts = stamp.replace(",", ".").split(":")
    total = 0.0
    for part in parts:
        total = total * 60 + float(part)
    return round(total, 3)


def cues(text):
    """[{start_s, end_s, speaker, text}] in file order. Header, NOTE,
    STYLE and REGION blocks are skipped; empty cues are dropped."""
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    if not text.startswith("WEBVTT"):
        raise ValueError("not a WebVTT file")
    out = []
    for block in re.split(r"\n\s*\n", text)[1:]:
        lines = [line for line in block.split("\n") if line.strip()]
        if not lines or lines[0].startswith(("NOTE", "STYLE", "REGION")):
            continue
        index = next((i for i, line in enumerate(lines) if TIMING.match(line)), None)
        if index is None:
            continue
        timing = TIMING.match(lines[index])
        body = "\n".join(lines[index + 1 :]).strip()
        speaker = None
        voice = VOICE.match(body)
        if voice:
            speaker, body = voice.group(1).strip(), voice.group(2)
        else:
            named = SPEAKER.match(body)
            if named and not named.group(1).strip().startswith(("http", "-")):
                speaker, body = named.group(1).strip(), named.group(2)
        body = " ".join(TAG.sub("", body).split())
        if not body:
            continue
        out.append(
            dict(
                start_s=seconds(timing.group("start")),
                end_s=seconds(timing.group("end")),
                speaker=speaker,
                text=body,
            )
        )
    return out


def turns(parsed):
    """Merge consecutive cues of one speaker. A cue with no speaker label
    continues the previous turn (Zoom only omits it on wrapped lines)."""
    out = []
    for cue in parsed:
        last = out[-1] if out else None
        if last and (cue["speaker"] is None or cue["speaker"] == last["speaker"]):
            last["text"] += " " + cue["text"]
            last["end_s"] = max(last["end_s"], cue["end_s"])
            last["cue_count"] += 1
            continue
        out.append(dict(cue, cue_count=1))
    return out
