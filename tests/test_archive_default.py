"""Conversation archiving is on by default, anchored at the project root, and kept out of tests and the bench."""

import json
from pathlib import Path
from types import SimpleNamespace

from app.config import PROJECT_ROOT, Settings, config
from bench import run_bench

ROOT = Path(__file__).resolve().parents[1]


def _settings(monkeypatch, value=None):
    monkeypatch.delenv('CONVERSATION_STORAGE_DIR', raising=False)
    if value is not None:
        monkeypatch.setenv('CONVERSATION_STORAGE_DIR', value)
    return Settings(_env_file=None)


def test_default_resolves_to_data_conversations_under_the_project_root(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)                       # the working directory must not matter
    assert PROJECT_ROOT == ROOT
    assert Path(_settings(monkeypatch).conversation_storage_dir) == ROOT / 'data' / 'conversations'


def test_empty_value_turns_archiving_off(monkeypatch):
    assert _settings(monkeypatch, '').conversation_storage_dir == ''
    assert _settings(monkeypatch, '   ').conversation_storage_dir == ''


def test_relative_value_resolves_from_the_project_root(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert Path(_settings(monkeypatch, 'var/archive').conversation_storage_dir) == ROOT / 'var' / 'archive'


def test_absolute_value_is_kept(monkeypatch, tmp_path):
    assert Path(_settings(monkeypatch, str(tmp_path)).conversation_storage_dir) == tmp_path


def test_bench_agent_processes_do_not_archive(monkeypatch):
    task = next(t for t in run_bench.load_tasks(['all']) if t['id'] == 'billing_qa')
    seen = {}

    def launch(cmd, env, timeout):
        seen['env'] = env
        return SimpleNamespace(stdout=json.dumps({}) + '\n\nStatus: completed; executions: 1; replans: 0\n',
                               stderr='')
    monkeypatch.setenv('CONVERSATION_STORAGE_DIR', 'data/conversations')
    monkeypatch.setattr(run_bench, 'launch_agent', launch)
    run_bench.run_one(task, 'single', 1, 5)
    assert seen['env']['CONVERSATION_STORAGE_DIR'] == ''


def test_conftest_disables_the_default_archive_for_tests():
    assert config.conversation_storage_dir == ''
