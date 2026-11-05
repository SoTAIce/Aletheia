"""Escape tests against the real sandbox image (design section 8, sandbox-step2-001).

Opt-in: set ALETHEIA_DOCKER_TESTS=1 with Docker running and the image built
(docker build -t aletheia-sandbox:py313 deploy/sandbox). Skipped otherwise, so
init.ps1 never depends on Docker.
"""

import os

import pytest

from app.tools.sandbox.executor import DockerSandbox, SandboxLimits

pytestmark = pytest.mark.skipif(os.environ.get('ALETHEIA_DOCKER_TESTS') != '1',
                                reason='set ALETHEIA_DOCKER_TESTS=1 to run real-container tests')


@pytest.fixture
def box(tmp_path_factory):
    outside = tmp_path_factory.mktemp('outside')
    (outside / 'HOST_MARKER_7f3a.txt').write_text('host secret', encoding='utf-8')
    work = tmp_path_factory.mktemp('work')
    sandbox = DockerSandbox(work, limits=SandboxLimits(timeout_seconds=8))
    if not sandbox.available():
        pytest.skip('sandbox image not built')

    def run(code, *, limits=None):
        (work / 'probe.py').write_text(code, encoding='utf-8')
        if limits:
            return DockerSandbox(work, limits=limits).run_python('probe.py')
        return sandbox.run_python('probe.py')
    yield SimpleBox(sandbox, work, run)
    assert sandbox.leftover_containers() == []


class SimpleBox:
    def __init__(self, sandbox, work, run):
        self.sandbox, self.work, self.run = sandbox, work, run


def test_runs_as_unprivileged_user_with_python(box):
    result = box.run('import os, sys; print(os.getuid(), os.getgid(), sys.version_info[:2])')
    assert result.exit_code == 0 and result.output.strip() == '1000 1000 (3, 13)'


def test_network_is_blocked(box):
    result = box.run(
        'import socket\n'
        'for host in ("1.1.1.1", "8.8.8.8"):\n'
        '    try:\n'
        '        socket.create_connection((host, 53), timeout=3); print("CONNECTED")\n'
        '    except OSError as e: print("BLOCKED", type(e).__name__)\n'
        'print([n for _, n in socket.if_nameindex()])')
    assert 'CONNECTED' not in result.output and result.output.count('BLOCKED') == 2
    assert "['lo']" in result.output


def test_host_files_outside_workspace_are_invisible(box):
    result = box.run(
        'import os\n'
        'hits = []\n'
        'for root, dirs, files in os.walk("/"):\n'
        '    dirs[:] = [d for d in dirs if os.path.join(root, d) not in ("/proc", "/sys", "/dev")]\n'
        '    hits += [f for f in files if "HOST_MARKER" in f or f in ("AGENTS.md", ".env")]\n'
        'print("HITS", hits)\n'
        'print(sorted(os.listdir("/work")))')
    assert 'HITS []' in result.output and "['probe.py']" in result.output


def test_only_workspace_and_tmp_are_writable(box):
    result = box.run(
        'import os, subprocess\n'
        'for path in ("/etc/x", "/usr/x", "/x", "/home/sandbox/x"):\n'
        '    try:\n'
        '        open(path, "w").write("x"); print("WROTE", path)\n'
        '    except OSError as e: print("DENIED", path)\n'
        'open("/work/ok.txt", "w").write("ok"); open("/tmp/t.sh", "w").write("#!/bin/sh\\necho ran\\n")\n'
        'os.chmod("/tmp/t.sh", 0o755)\n'
        'try:\n'
        '    subprocess.run(["/tmp/t.sh"], check=True); print("EXEC_TMP")\n'
        'except OSError as e: print("NOEXEC", type(e).__name__)')
    assert 'WROTE' not in result.output and result.output.count('DENIED') == 4
    assert 'NOEXEC' in result.output and (box.work / 'ok.txt').read_text() == 'ok'


def test_host_environment_is_not_passed(box, monkeypatch):
    monkeypatch.setenv('ALETHEIA_SECRET_PROBE', 'leak-123')
    result = box.run('import os; print(sorted(os.environ))')
    assert 'ALETHEIA_SECRET_PROBE' not in result.output and 'leak-123' not in result.output
    for name in ('DASHSCOPE_API_KEY', 'TAVILY_API_KEY', 'ALETHEIA_TEST_API_KEY', 'USERPROFILE'):
        assert name not in result.output


def test_infinite_loop_times_out_and_container_is_removed(box):
    result = box.run('while True: pass', limits=SandboxLimits(timeout_seconds=4))
    assert result.timed_out and result.exit_code is None and result.seconds < 20
    assert box.sandbox.leftover_containers() == []


def test_memory_limit_stops_large_allocation(box):
    result = box.run('b = bytearray(1024 * 1024 * 1024); print("ALLOCATED", len(b))')
    assert 'ALLOCATED' not in result.output
    assert result.killed or 'MemoryError' in result.output


def test_process_bomb_is_capped(box):
    result = box.run(
        'import os, time\n'
        'n = 0\n'
        'try:\n'
        '    while n < 1000:\n'
        '        if os.fork() == 0:\n'
        '            time.sleep(30); os._exit(0)\n'
        '        n += 1\n'
        'except OSError as e: print("CAPPED", n, type(e).__name__)\n'
        'else: print("UNCAPPED", n)\n'
        'os._exit(0)')
    assert 'UNCAPPED' not in result.output and 'CAPPED' in result.output
    capped = int(result.output.split('CAPPED')[1].split()[0])
    assert capped < 128


def test_huge_output_is_truncated(box):
    result = box.run('import sys\nfor _ in range(200): sys.stdout.write("x" * 50000)')
    assert result.exit_code == 0 and result.truncated and len(result.output) == 20_000


def test_pytest_runs_and_reports(box):
    (box.work / 'test_demo.py').write_text('def test_ok(): assert 1\n\ndef test_bad(): assert 0\n', encoding='utf-8')
    result = box.sandbox.run_tests('test_demo.py')
    assert result.exit_code == 1 and '1 failed, 1 passed' in result.output
    assert not any(p.name in ('__pycache__', '.pytest_cache') for p in box.work.rglob('*'))
