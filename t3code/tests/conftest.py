import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from t3code_fake import Home  # noqa: E402


@pytest.fixture
def home(tmp_path):
    return Home(tmp_path / "t3")


@pytest.fixture
def env(monkeypatch, home, tmp_path):
    monkeypatch.setenv("T3CODE_HOMES", f"test={home.root}")
    monkeypatch.setenv("T3CODE_CODEX_HOMES", "none")
    for key in ("T3CODE_SOURCE", "T3CODE_SINCE", "T3CODE_ARCHIVE_DIR"):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch
