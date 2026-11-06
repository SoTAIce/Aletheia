"""Shared test setup: keep the default conversation archive out of the repository's data directory."""

import pytest

from app.config import config


@pytest.fixture(autouse=True)
def _no_default_conversation_archive(monkeypatch):
    # Archiving is on by default (data/conversations). Tests that need an archive pass tmp_path explicitly.
    monkeypatch.setattr(config, 'conversation_storage_dir', '')
