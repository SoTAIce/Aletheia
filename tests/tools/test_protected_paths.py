"""Secrets, VCS internals and auto-run files stay out of the model's reach (D-029)."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.tools.filesystem.file_access import FileAccess, PROTECTED_MESSAGE
from app.tools.sandbox.executor import DockerSandbox, SandboxError, host_call_timeout_seconds


@pytest.fixture
def workspace(tmp_path):
    files = {
        '.env': 'API_KEY=secret', '.env.local': 'x', '.env.example': 'API_KEY=', 'sub/.env': 'x',
        '.git/config': '[core]', '.git/hooks/pre-commit': '#!/bin/sh', 'server.pem': 'x', 'sub/deploy.key': 'x',
        '.github/workflows/ci.yml': 'on: push', '.vscode/tasks.json': '{}', 'sitecustomize.py': '',
        'lib.pth': '', 'conftest.py': '', 'main.py': 'print(1)', '.gitignore': '*.log',
        'node_modules/.env': 'skipped by the sandbox scan',
    }
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
    return tmp_path


def _access(root):
    return FileAccess(root, write_root=root)


@pytest.mark.parametrize('path', ['.env', '.env.local', 'sub/.env', '.git/config', 'server.pem',
                                  'sub/deploy.key', 'SUB/.ENV'])
def test_secret_files_cannot_be_read(workspace, path):
    with pytest.raises(PermissionError, match='protected'):
        _access(workspace).read_file(path)


@pytest.mark.parametrize('path', ['.env.example', '.github/workflows/ci.yml', 'conftest.py', '.gitignore'])
def test_ordinary_and_auto_run_files_stay_readable(workspace, path):
    assert isinstance(_access(workspace).read_file(path), str)


def test_listing_hides_secrets_and_refuses_secret_directories(workspace):
    access = _access(workspace)
    names = {e['name'] for e in access.list_directory('.', limit=100)['entries']}
    assert {'.env', '.env.local', '.git', 'server.pem'}.isdisjoint(names)
    assert {'.env.example', '.github', 'main.py', 'sub', 'conftest.py'} <= names
    assert {e['name'] for e in access.list_directory('sub')['entries']} == set()
    with pytest.raises(PermissionError, match='protected'):
        access.list_directory('.git')


@pytest.mark.parametrize('path', ['.env', 'sub/.env', '.git/hooks/pre-commit', '.git/new', '.github/workflows/ci.yml',
                                  '.vscode/tasks.json', 'sitecustomize.py', 'lib.pth', 'new.pem', '.idea/x.xml'])
def test_secret_and_auto_run_paths_cannot_be_written(workspace, path):
    with pytest.raises(PermissionError, match='protected'):
        _access(workspace).write_file(path, 'payload', overwrite=True)


def test_ordinary_writes_still_work(workspace):
    access = _access(workspace)
    access.write_file('conftest.py', 'import pytest\n', overwrite=True)    # tests may need fixtures
    access.write_file('notes.md', 'ok')
    assert (workspace / 'notes.md').read_text(encoding='utf-8') == 'ok'
    assert (workspace / '.env').read_text(encoding='utf-8') == 'API_KEY=secret'


def _flags(cmd):
    return [(cmd[i], cmd[i + 1]) for i, part in enumerate(cmd)
            if part in ('--volume', '--tmpfs') and not cmd[i + 1].startswith('/tmp:')]


def test_sandbox_masks_secrets_and_freezes_existing_auto_run_paths(workspace):
    root = workspace.resolve()
    cmd = DockerSandbox(workspace).build_command(['python'], 'n', empty_file='EMPTY')
    flags = _flags(cmd)
    assert flags[0] == ('--volume', f'{root}:/work:rw')
    assert set(flags[1:]) == {
        ('--tmpfs', '/work/.git:ro,size=4k'),
        ('--volume', 'EMPTY:/work/.env:ro'), ('--volume', 'EMPTY:/work/.env.local:ro'),
        ('--volume', 'EMPTY:/work/server.pem:ro'), ('--volume', 'EMPTY:/work/sub/.env:ro'),
        ('--volume', 'EMPTY:/work/sub/deploy.key:ro'),
        ('--volume', f'{root / ".github"}:/work/.github:ro'), ('--volume', f'{root / ".vscode"}:/work/.vscode:ro'),
        ('--volume', f'{root / "sitecustomize.py"}:/work/sitecustomize.py:ro'),
        ('--volume', f'{root / "lib.pth"}:/work/lib.pth:ro'),
    }
    assert cmd.index('--workdir') > cmd.index('--volume')               # masks come after the workspace mount


def test_read_only_runs_only_need_the_secret_masks(workspace):
    cmd = DockerSandbox(workspace).build_command(['python'], 'n', workspace_mode='ro', empty_file='EMPTY')
    targets = {value.rsplit(':', 2)[-2] if kind == '--volume' else value for kind, value in _flags(cmd)[1:]}
    assert not any('.github' in t or 'sitecustomize' in t for t in targets)
    assert ('--volume', 'EMPTY:/work/.env:ro') in _flags(cmd)


def test_a_secret_file_without_a_mask_file_is_refused(workspace):
    with pytest.raises(SandboxError, match='mask'):
        DockerSandbox(workspace).build_command(['python'], 'n')


def test_runs_create_and_remove_their_own_mask_file(workspace):
    seen = {}

    def popen(cmd, **kw):
        seen['empty'] = next(v.split(':/work/.env:')[0] for v in cmd if v.endswith(':/work/.env:ro'))
        raise OSError('stop before docker')
    with pytest.raises(SandboxError, match='docker'):
        DockerSandbox(workspace, popen=popen).run_python('main.py')
    from pathlib import Path
    assert seen['empty'] and not Path(seen['empty']).exists()


def test_sandbox_tools_wait_long_enough_for_the_sandbox_timeout():
    from app.tools import mcp_client
    assert host_call_timeout_seconds() > 30 + 30 + 10
    calls = []

    class Session:
        async def call_tool(self, name, arguments=None, read_timeout_seconds=None):
            calls.append((name, read_timeout_seconds))
            return SimpleNamespace()
    client = mcp_client.MCPClient(Session())
    asyncio.run(client.call_tool('run_tests', {}))
    asyncio.run(client.call_tool('run_python_code', {'code': '1'}))
    asyncio.run(client.call_tool('read_file', {'path': 'a'}))
    assert calls == [('run_tests', timedelta(seconds=host_call_timeout_seconds())),
                     ('run_python_code', timedelta(seconds=host_call_timeout_seconds())), ('read_file', None)]

