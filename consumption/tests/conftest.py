"""Fixtures for the consumption tests; the fakes live in consumption_fakes."""

import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from consumption_fakes import Fake, World, make_handler  # noqa: E402


@pytest.fixture
def world():
    return World()


@pytest.fixture
def fake(world):
    state = Fake(world)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
