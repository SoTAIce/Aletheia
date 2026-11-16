"""Shared test setup: keep the default conversation archive out of the repository's data directory."""

import os
from pathlib import Path
import re

import pytest

from app.config import config


@pytest.fixture(autouse=True)
def _no_default_conversation_archive(monkeypatch):
    # Archiving is on by default (data/conversations). Tests that need an archive pass tmp_path explicitly.
    monkeypatch.setattr(config, 'conversation_storage_dir', '')
    # A personal models.toml in the repository must not change any test (stage 4d-2).
    monkeypatch.setattr(config, 'model_profiles_file', '')


_ENVIRONMENT_AT_START = pytest.StashKey[frozenset[str]]()
_REPOSITORY_ENV_NAMES = pytest.StashKey[frozenset[str]]()


def pytest_configure(config):
    config.stash[_ENVIRONMENT_AT_START] = frozenset(os.environ)
    env_file = Path(__file__).resolve().parents[1] / '.env'
    names = set()
    if env_file.is_file():
        with env_file.open(encoding='utf-8-sig') as lines:
            for line in lines:
                match = re.match(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=', line)
                if match:
                    name = match.group(1)
                    names.add(name.upper() if os.name == 'nt' else name)
    config.stash[_REPOSITORY_ENV_NAMES] = frozenset(names)


def _remove_repository_dotenv_additions(config):
    # pymilvus.settings calls load_dotenv() on import, including during collection.
    added_names = (config.stash[_REPOSITORY_ENV_NAMES]
                   - config.stash[_ENVIRONMENT_AT_START])
    for name in added_names:
        os.environ.pop(name, None)


def pytest_collection_finish(session):
    _remove_repository_dotenv_additions(session.config)


@pytest.fixture(autouse=True)
def _isolate_repository_dotenv(pytestconfig):
    _remove_repository_dotenv_additions(pytestconfig)
