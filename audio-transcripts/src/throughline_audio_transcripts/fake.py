"""An offline transcription endpoint for tests and the contract kit.

`AUDIO_TRANSCRIPTS_FIXTURE` names a JSON file
`{"responses": {"<uploaded file name>": <verbose_json body>, ...}, "default": <body>}`.
The transport answers POST .../audio/transcriptions with the body recorded
for the uploaded file's name (the multipart `filename`).
"""

import json
import re
from pathlib import Path

import httpx

NAME = re.compile(rb'filename="([^"]*)"')


def endpoint(data, calls=None):
    responses = data.get("responses") or {}

    def handler(request):
        if not request.url.path.endswith("/audio/transcriptions"):
            return httpx.Response(404)
        body = request.read()
        match = NAME.search(body)
        name = match.group(1).decode("utf-8", "replace") if match else ""
        if calls is not None:
            calls.append(name)
        answer = responses.get(name, data.get("default"))
        if answer is None:
            return httpx.Response(400, json={"error": {"message": "invented: unknown file"}})
        return httpx.Response(200, json=answer)

    return httpx.MockTransport(handler)


def from_file(path):
    return endpoint(json.loads(Path(path).read_text(encoding="utf-8")))
