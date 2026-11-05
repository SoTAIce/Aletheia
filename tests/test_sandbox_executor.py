"""Deterministic tests for the Docker sandbox executor (sandbox-step2-001).

No Docker needed: the process launcher is faked. Real-container escape tests
live in tests/test_sandbox_docker.py and are opt-in.
"""

import io
import subprocess
import time
from types import SimpleNamespace

import pytest

from app.tools.sandbox.executor import DockerSandbox, LABEL, SandboxError, SandboxLimits


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / 'main.py').write_text('print(1)\n', encoding='utf-8')
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests' / 'test_x.py').write_text('def test_x(): pass\n', encoding='utf-8')
    (tmp_path / 'notes.txt').write_text('x', encoding='utf-8')
    return tmp_path


def test_command_carries_every_limit_and_mounts_only_workspace(workspace):
    cmd = DockerSandbox(workspace).build_command(['python', '/work/main.py'], 'aletheia-sbx-t')
    joined = ' '.join(cmd)
    for flag in ('--rm', '--network none', '--read-only', '--user 1000:1000', '--cap-drop ALL',
                 '--security-opt no-new-privileges', '--memory 512m', '--memory-swap 512m', '--cpus 1',
                 '--pids-limit 128', f'--label {LABEL}', '--workdir /work'):
        assert flag in joined, flag
    assert '/tmp:rw,noexec,nosuid,size=64m' in cmd
    volumes = [cmd[i + 1] for i, part in enumerate(cmd) if part == '--volume']
    assert volumes == [f'{workspace.resolve()}:/work:rw']
    envs = [cmd[i + 1] for i, part in enumerate(cmd) if part == '--env']
    assert envs == ['PYTHONDONTWRITEBYTECODE=1', 'HOME=/tmp']
    assert cmd[-3:] == ['aletheia-sandbox:py313', 'python', '/work/main.py']


@pytest.mark.parametrize('path', ['/etc/passwd', 'C:/Windows/win.ini', '../x.py', 'a/../../x.py', '', 'missing.py'])
def test_paths_outside_or_missing_are_refused(workspace, path):
    with pytest.raises(SandboxError):
        DockerSandbox(workspace, popen=_never).run_python(path)


def test_only_python_files_and_plain_args(workspace):
    sandbox = DockerSandbox(workspace, popen=_never)
    with pytest.raises(SandboxError, match='.py'):
        sandbox.run_python('notes.txt')
    for bad in (['$(rm -rf /)'], ['a;b'], ['x'] * 17, [1]):
        with pytest.raises(SandboxError, match='arguments'):
            sandbox.run_python('main.py', bad)


def test_symlink_inside_workspace_is_refused(workspace, tmp_path_factory):
    outside = tmp_path_factory.mktemp('outside') / 'evil.py'
    outside.write_text('print(2)\n', encoding='utf-8')
    try:
        (workspace / 'link.py').symlink_to(outside)
    except OSError:
        pytest.skip('creating symlinks needs extra privileges on this Windows account')
    with pytest.raises(SandboxError, match='links'):
        DockerSandbox(workspace, popen=_never).run_python('link.py')


def _never(*args, **kwargs):
    raise AssertionError('docker must not start for a refused request')


class FakeProc:
    def __init__(self, data=b'', returncode=0, hang=False):
        self.stdout = io.BytesIO(data)
        self.returncode = None if hang else returncode
        self._final, self.hang, self.killed = returncode, hang, False

    def wait(self, timeout=None):
        if self.hang:
            self.hang = False
            raise subprocess.TimeoutExpired('docker', timeout)
        self.returncode = self._final
        return self.returncode

    def kill(self):
        self.killed = True


def _sandbox(workspace, proc, **limits):
    removed = []
    calls = []

    def popen(cmd, **kwargs):
        calls.append(cmd)
        return proc

    def run(cmd, **kwargs):
        removed.append(cmd)
        return SimpleNamespace(returncode=0, stdout='')
    return DockerSandbox(workspace, popen=popen, run=run, limits=SandboxLimits(**limits)), calls, removed


def test_output_is_capped_while_reading(workspace):
    sandbox, calls, _ = _sandbox(workspace, FakeProc(b'y' * 500_000, 0), output_chars=1000)
    result = sandbox.run_python('main.py')
    assert result.exit_code == 0 and result.truncated and len(result.output) == 1000
    assert '[output truncated]' in result.summary()


def test_timeout_removes_container_and_reports_flag(workspace):
    proc = FakeProc(b'partial', returncode=137, hang=True)
    sandbox, calls, removed = _sandbox(workspace, proc, timeout_seconds=0.01)
    result = sandbox.run_tests('tests')
    assert result.timed_out and result.exit_code is None
    name = calls[0][calls[0].index('--name') + 1]
    assert removed == [['docker', 'rm', '-f', name]] and name.startswith('aletheia-sbx-')
    assert calls[0][-1] == '/work/tests' and 'timed out' in result.summary()


def test_exit_137_is_reported_as_killed(workspace):
    sandbox, _, _ = _sandbox(workspace, FakeProc(b'', 137))
    assert 'likely memory limit' in sandbox.run_python('main.py').summary()


def test_workspace_must_be_a_directory(tmp_path):
    with pytest.raises(SandboxError):
        DockerSandbox(tmp_path / 'nope')
