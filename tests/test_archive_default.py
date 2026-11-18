"""Conversation archiving is on by default, anchored at the project root, kept out of tests, and per run in the bench (D-086)."""

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


def _bench_env(monkeypatch, **kwargs):
    task = next(t for t in run_bench.load_tasks(['all']) if t['id'] == 'billing_qa')
    seen = {}

    def launch(cmd, env, timeout):
        seen['env'], seen['root'] = env, Path(cmd[cmd.index('--filesystem-root') + 1])
        archive = env['CONVERSATION_STORAGE_DIR']
        seen['archive_existed'] = bool(archive) and Path(archive).is_dir()
        return SimpleNamespace(stdout=json.dumps({}) + '\n\nStatus: completed; executions: 1; replans: 0\n',
                               stderr='')
    monkeypatch.setenv('CONVERSATION_STORAGE_DIR', 'data/conversations')
    monkeypatch.setattr(run_bench, 'launch_agent', launch)
    row = run_bench.run_one(task, 'single', 1, 5, **kwargs)
    return seen, row


def test_bench_agent_processes_archive_under_their_temp_dir(monkeypatch):
    """D-086: each run has its own archive next to (never inside) the agent's root; it goes with the run."""
    seen, row = _bench_env(monkeypatch)
    archive = Path(seen['env']['CONVERSATION_STORAGE_DIR'])
    assert seen['archive_existed'] and archive.parent == seen['root'].parent
    assert archive.name == 'archive' and seen['root'] not in archive.parents
    assert ROOT not in archive.parents and not archive.exists() and not seen['root'].exists()
    assert row['archive'] is True and row['archived_tool_results'] == 0


def test_the_bench_control_runs_without_an_archive(monkeypatch):
    seen, row = _bench_env(monkeypatch, archive=False)
    assert seen['env']['CONVERSATION_STORAGE_DIR'] == ''
    assert row['archive'] is False and row['archived_tool_results'] == 0


def test_conftest_disables_the_default_archive_for_tests():
    assert config.conversation_storage_dir == ''
