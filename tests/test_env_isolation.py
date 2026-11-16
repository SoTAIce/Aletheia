"""Repository dotenv imports must not leak configuration into tests."""

import os
from pathlib import Path

import pymilvus  # noqa: F401 -- exercise its import-time load_dotenv during collection
import pytest

from conftest import _ENVIRONMENT_AT_START, _REPOSITORY_ENV_NAMES


def test_repository_dotenv_names_absent_at_start_stay_out_of_environment(pytestconfig):
    if not (Path(__file__).resolve().parents[1] / '.env').is_file():
        pytest.skip('No repository .env')
    injected_names = (pytestconfig.stash[_REPOSITORY_ENV_NAMES]
                      - pytestconfig.stash[_ENVIRONMENT_AT_START])
    assert not injected_names.intersection(os.environ)
