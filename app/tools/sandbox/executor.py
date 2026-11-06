"""Host-side Docker sandbox for running workspace code (sandbox-step2-001).

Every run starts a fresh container from a fixed image with fixed limits
(docs/sandbox_execution_design.md section 6.2). Models never choose limits,
images, mounts or commands: callers can only run one workspace Python file or
pytest. Only the per-task workspace is mounted. Output is capped while it is
read, a timeout kills and removes the container, and results carry no host
details beyond exit code, flags and capped output.
run_python_code runs a short inline snippet with the workspace mounted read-only,
so read-only tasks can compute values without changing any file (D-028).
Secret files and directories inside the workspace are masked with empty mounts, and
existing auto-run locations (.github, .vscode, ...) are mounted read-only (D-029).

This is isolation against faulty or ordinary malicious code, not against
kernel exploits (see design section 10).
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.tools.filesystem.file_access import is_protected_write_path, is_secret_path

DEFAULT_IMAGE = 'aletheia-sandbox:py313'
LABEL = 'aletheia.sandbox=1'
MASK_SCAN_LIMIT = 20_000                     # workspace entries checked for secrets per run
SANDBOX_TOOLS = frozenset({'run_tests', 'run_python', 'run_python_code'})
_SCAN_SKIP = frozenset({'.venv', 'venv', 'node_modules', '__pycache__'})
_ARG = re.compile(r'[\w .,:=+@/-]{0,200}')


def host_call_timeout_seconds(limits: 'SandboxLimits | None' = None) -> float:
    """How long a caller must wait for one run so the sandbox's own timeout result arrives.

    Run limit, then up to 30 s for `docker rm -f` and 10 s for the CLI to exit (see _execute),
    plus room for container start-up on Docker Desktop.
    """
    return (limits or SandboxLimits()).timeout_seconds + 30 + 10 + 20


class SandboxError(RuntimeError):
    """The request was refused before Docker ran, or the sandbox is unusable."""


@dataclass(frozen=True)
class SandboxLimits:
    timeout_seconds: float = 30
    memory: str = '512m'
    cpus: str = '1'
    pids: int = 128
    tmpfs_mb: int = 64
    output_chars: int = 20_000
    max_args: int = 16
    code_chars: int = 8_000


@dataclass(frozen=True)
class RunResult:
    exit_code: int | None
    timed_out: bool
    output: str
    truncated: bool
    seconds: float

    @property
    def killed(self) -> bool:
        """Exit 137 = SIGKILL, usually the memory limit (the container is gone, so not inspectable)."""
        return self.exit_code == 137

    def summary(self) -> str:
        if self.timed_out:
            status = 'timed out (limit reached, process killed)'
        elif self.killed:
            status = 'killed (exit 137, likely memory limit)'
        else:
            status = f'exit code {self.exit_code}'
        note = ' [output truncated]' if self.truncated else ''
        return f'{status}; {self.seconds:.1f}s{note}\n{self.output}'


class DockerSandbox:
    def __init__(self, workspace, *, image: str = DEFAULT_IMAGE, limits: SandboxLimits = SandboxLimits(),
                 docker: str = 'docker', popen=subprocess.Popen, run=subprocess.run):
        root = Path(workspace)
        if not root.is_dir() or root.is_symlink():
            raise SandboxError('workspace must be an existing directory')
        self.workspace = root.resolve(strict=True)
        self.image, self.limits, self.docker = image, limits, docker
        self._popen, self._run = popen, run

    # ---- request validation (before Docker runs) ----
    def _workspace_path(self, path: str, *, kind: str) -> str:
        if not isinstance(path, str) or not path.strip() or '\x00' in path:
            raise SandboxError('path must be a nonempty relative path')
        posix = PurePosixPath(path.replace('\\', '/'))
        if posix.is_absolute() or re.match(r'^[A-Za-z]:', path) or '..' in posix.parts:
            raise SandboxError('path must stay inside the workspace')
        host = self.workspace.joinpath(*posix.parts)
        current = self.workspace
        for part in posix.parts:  # refuse links anywhere on the way
            current = current / part
            if current.is_symlink():
                raise SandboxError('links are not allowed')
        if kind == 'file' and not host.is_file():
            raise SandboxError('file does not exist in the workspace')
        if kind == 'any' and not host.exists():
            raise SandboxError('path does not exist in the workspace')
        return str(PurePosixPath('/work', *posix.parts))

    def _args(self, args) -> list[str]:
        args = list(args or ())
        if len(args) > self.limits.max_args or any(
                not isinstance(a, str) or not _ARG.fullmatch(a) for a in args):
            raise SandboxError('arguments must be a few short plain strings')
        return args

    # ---- protected paths inside the workspace (D-029) ----
    def protected_mounts(self, empty_file: str | None, *, workspace_mode: str = 'rw') -> list[str]:
        """Mount flags that hide secrets and freeze existing auto-run paths for one run."""
        flags, seen, stack = [], 0, [(self.workspace, (), False)]
        while stack:
            directory, parts, frozen = stack.pop()   # frozen: already inside a read-only mount
            try:
                children = sorted(os.scandir(directory), key=lambda e: e.name)
            except OSError:
                continue
            for entry in children:
                seen += 1
                if seen > MASK_SCAN_LIMIT:
                    raise SandboxError('workspace has too many files to check for secrets')
                rel = (*parts, entry.name)
                target = str(PurePosixPath('/work', *rel))
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    if entry.name.lower() in _SCAN_SKIP:
                        continue
                    if is_secret_path(rel):
                        flags += ['--tmpfs', f'{target}:ro,size=4k']     # an empty, unwritable directory
                        continue
                    freeze = workspace_mode == 'rw' and not frozen and is_protected_write_path(rel)
                    if freeze:
                        flags += ['--volume', f'{entry.path}:{target}:ro']
                    stack.append((entry.path, rel, frozen or freeze))   # secrets inside still get masked
                elif entry.is_file():
                    if is_secret_path(rel):
                        if empty_file is None:
                            raise SandboxError('an empty mask file is required')
                        flags += ['--volume', f'{empty_file}:{target}:ro']
                    elif workspace_mode == 'rw' and not frozen and is_protected_write_path(rel):
                        flags += ['--volume', f'{entry.path}:{target}:ro']
        return flags

    # ---- docker command ----
    def build_command(self, argv: list[str], name: str, *, workspace_mode: str = 'rw',
                      empty_file: str | None = None) -> list[str]:
        lim = self.limits
        if workspace_mode not in ('rw', 'ro'):
            raise SandboxError('workspace mode must be rw or ro')
        masks = self.protected_mounts(empty_file, workspace_mode=workspace_mode)
        return [
            self.docker, 'run', '--rm', '--name', name, '--label', LABEL,
            '--network', 'none',
            '--read-only', '--tmpfs', f'/tmp:rw,noexec,nosuid,size={lim.tmpfs_mb}m',
            '--user', '1000:1000', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
            '--memory', lim.memory, '--memory-swap', lim.memory, '--cpus', lim.cpus,
            '--pids-limit', str(lim.pids),
            '--env', 'PYTHONDONTWRITEBYTECODE=1', '--env', 'HOME=/tmp',
            '--volume', f'{self.workspace}:/work:{workspace_mode}', *masks, '--workdir', '/work',
            self.image, *argv,
        ]

    def run_python(self, path: str, args=()) -> RunResult:
        target = self._workspace_path(path, kind='file')
        if not target.endswith('.py'):
            raise SandboxError('only .py files can be run')
        return self._execute(['python', target, *self._args(args)])

    def run_python_code(self, code: str) -> RunResult:
        """Run inline Python with the workspace read-only; workspace modules are importable."""
        if not isinstance(code, str) or not code.strip() or '\x00' in code:
            raise SandboxError('code must be nonempty text')
        if len(code) > self.limits.code_chars:
            raise SandboxError(f'code must be at most {self.limits.code_chars} characters')
        return self._execute(['python', '-c', code], workspace_mode='ro')

    def run_tests(self, path: str | None = None) -> RunResult:
        argv = ['python', '-m', 'pytest', '-q', '-p', 'no:cacheprovider', '--color=no']
        if path:
            argv.append(self._workspace_path(path, kind='any'))
        return self._execute(argv)

    # ---- execution ----
    def _execute(self, argv: list[str], *, workspace_mode: str = 'rw') -> RunResult:
        # The empty file that masks secrets lives outside the workspace and only for this run.
        with tempfile.TemporaryDirectory(prefix='aletheia-sbx-mask-') as mask_dir:
            empty = Path(mask_dir, 'empty')
            empty.write_bytes(b'')
            return self._execute_with(argv, workspace_mode, str(empty))

    def _execute_with(self, argv: list[str], workspace_mode: str, empty_file: str) -> RunResult:
        name = 'aletheia-sbx-' + uuid.uuid4().hex[:12]
        cmd = self.build_command(argv, name, workspace_mode=workspace_mode, empty_file=empty_file)
        # The docker CLI itself needs the host env (DOCKER_HOST etc.); the container gets none of it.
        started = time.monotonic()
        try:
            proc = self._popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        except OSError as exc:
            raise SandboxError('docker is not available') from exc
        kept, total = bytearray(), [0]
        cap = self.limits.output_chars * 4  # bytes; decoded and cut to chars below

        def drain():
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    return
                total[0] += len(chunk)
                if len(kept) < cap:
                    kept.extend(chunk[:cap - len(kept)])

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        timed_out = False
        try:
            proc.wait(timeout=self.limits.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            self.remove(name)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        reader.join(timeout=5)
        text = kept.decode('utf-8', errors='replace')
        truncated = total[0] > len(kept) or len(text) > self.limits.output_chars
        return RunResult(None if timed_out else proc.returncode, timed_out,
                         text[:self.limits.output_chars], truncated, time.monotonic() - started)

    def remove(self, name: str) -> None:
        self._run([self.docker, 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                  timeout=30, check=False)

    # ---- availability and cleanup ----
    def available(self) -> bool:
        try:
            done = self._run([self.docker, 'image', 'inspect', self.image], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, timeout=20, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return done.returncode == 0

    def leftover_containers(self) -> list[str]:
        done = self._run([self.docker, 'ps', '-aq', '--filter', f'label={LABEL}'], capture_output=True,
                         text=True, timeout=20, check=False)
        return done.stdout.split() if done.returncode == 0 else []
