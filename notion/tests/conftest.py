import pytest


@pytest.fixture
def recording():
    from pathlib import Path

    return Path(__file__).parent / "recorded" / "export.jsonl"
