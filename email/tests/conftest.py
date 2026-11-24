"""Fixtures for the email plugin tests; helpers live in email_fakes.py."""

import sys
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).parent))  # only email_fakes is imported from here

from email_fakes import FakeComposio  # noqa: E402


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Host settings must not leak into tests or contract subprocesses."""
    from throughline_email import fetch

    names = [env for _, env, _, _ in fetch.SETTINGS] + [
        env for _, env, _ in fetch.SECRETS
    ]
    for name in names + ["EMAIL_SOURCE", "EMAIL_BODY_LIMIT", "EMAIL_EXPORT_BATCH"]:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def composio():
    fake = FakeComposio().start()
    yield fake
    fake.stop()
