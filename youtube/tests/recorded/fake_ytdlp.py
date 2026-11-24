"""Invented executable replacement; never accesses the internet or cookies."""

import json
import sys
from pathlib import Path

data = json.loads(Path(__file__).with_name("live.json").read_text(encoding="utf-8"))
assert "--flat-playlist" in sys.argv
assert "--ignore-config" in sys.argv
assert "--dump-single-json" in sys.argv
print(json.dumps({"entries": data[sys.argv[-1]]}))
