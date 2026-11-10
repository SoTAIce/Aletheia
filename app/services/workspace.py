"""Which directory the agent may work in (workspace-001, D-074; docs/workspace_choice_plan.md).

Pure checks: nothing here starts a tool server or lists a directory. Every working directory,
chosen on the web page, with /dir or given at startup, passes check(); a directory on the
system drive (C: on most Windows machines) is allowed only after the user confirms it.

Two kinds of folders are closed to the agent:
- refused as a working directory: drive roots of the system drive, network paths, system
  folders (Windows, Program Files, ProgramData, AppData except the temp folder), the user
  profile itself, and anything inside a closed folder;
- closed folders (the user's WORKSPACE_DENY_DIRS plus this project's data directory): a working
  directory inside one is refused; one inside the working directory is hidden from the file
  tools and masked in the sandbox (the tool servers get them as --deny).
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path, PureWindowsPath
import sys
import tempfile

MAX_PATH_CHARS = 500


class WorkspaceError(ValueError):
    """A refused directory; code and message (Chinese) are for the caller to show."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code)
        self.code, self.message = code, message


class WorkspaceConfirmationRequired(WorkspaceError):
    """A directory on the system drive that the user has not confirmed."""


@dataclass(frozen=True, slots=True)
class WorkspaceCheck:
    path: str | None            # the resolved absolute path; None when refused
    system_drive: bool
    problem: str | None = None  # a code, see docs/workspace_choice_plan.md 4.1
    message: str | None = None

    def view(self) -> dict:
        return {'path': self.path, 'system_drive': self.system_drive, 'problem': self.problem,
                'message': self.message}


def system_drive() -> str | None:
    """The system drive on Windows ('C:'), None elsewhere."""
    if os.name != 'nt':
        return None
    return (os.environ.get('SystemDrive') or 'C:').rstrip('\\/').upper()


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None


def _inside(path: Path, folder: Path) -> bool:
    return path == folder or folder in path.parents


def _system_refusals() -> tuple[tuple[Path, ...], tuple[Path, ...], tuple[Path, ...]]:
    """(folders a workspace may not be in, folders a workspace may not be, allowed exceptions)."""
    if os.name == 'nt':
        inside = [_env_path(n) for n in ('SystemRoot', 'ProgramFiles', 'ProgramFiles(x86)', 'ProgramData')]
        profile = _env_path('USERPROFILE')
        if profile is not None:
            inside.append(profile / 'AppData')
        exact = [profile]
    else:
        inside = [Path(p) for p in ('/etc', '/usr', '/bin', '/sbin', '/boot', '/proc', '/sys', '/dev', '/var/lib')]
        exact = [Path.home()]
    exceptions = (Path(tempfile.gettempdir()),)        # temp subfolders are fine (the benchmark uses them)
    clean = lambda items: tuple(p.resolve() for p in items if p is not None)
    return clean(inside), clean(exact), clean(exceptions)


def _paths(value) -> tuple[Path, ...]:
    if isinstance(value, str):
        value = [part for part in value.split(';')]
    return tuple(Path(str(p).strip()).expanduser().resolve() for p in (value or ()) if str(p).strip())


@dataclass(frozen=True)
class WorkspacePolicy:
    deny: tuple[Path, ...] = ()          # the user's closed folders (WORKSPACE_DENY_DIRS)
    protected: tuple[Path, ...] = ()     # this project's data directories
    allowed_roots: tuple[Path, ...] = () # when set, a workspace must be inside one of these

    @classmethod
    def from_config(cls, settings=None) -> 'WorkspacePolicy':
        from app.config import PROJECT_ROOT, config
        settings = settings or config
        protected = [Path(PROJECT_ROOT) / 'data']
        if settings.conversation_storage_dir:
            protected.append(Path(settings.conversation_storage_dir))
        trace_dir = os.environ.get('ALETHEIA_TRACE_DIR')
        if trace_dir:
            protected.append(Path(trace_dir))
        return cls(_paths(getattr(settings, 'workspace_deny_dirs', '')), _paths(protected),
                   _paths(getattr(settings, 'workspace_allowed_roots', '')))

    def closed_folders(self) -> tuple[Path, ...]:
        """Folders the tool servers hide inside any working directory (--deny)."""
        return tuple(dict.fromkeys((*self.deny, *self.protected)))

    def check(self, raw) -> WorkspaceCheck:
        def refuse(code, message, system=False):
            return WorkspaceCheck(None, system, code, message)
        if not isinstance(raw, (str, os.PathLike)):
            return refuse('invalid', '目录必须是一个路径。')
        text = os.fspath(raw).strip().strip('"')
        if not text or len(text) > MAX_PATH_CHARS or '\x00' in text:
            return refuse('invalid', f'目录不能为空，最长 {MAX_PATH_CHARS} 个字符。')
        given = Path(text).expanduser()
        if not given.is_absolute():
            return refuse('invalid', '请填写完整的绝对路径，例如 D:\\projects\\demo。')
        if text.startswith(('\\\\', '//')) or PureWindowsPath(text).drive.startswith('\\\\'):
            return refuse('network', '不支持网络路径，请使用本机磁盘上的目录。')
        try:
            path = given.resolve(strict=True)
        except (OSError, RuntimeError):
            return refuse('not_found', '这个目录不存在。')
        if not path.is_dir():
            return refuse('not_found', '这不是一个目录。')
        if str(path).startswith('\\\\'):
            return refuse('network', '这个目录实际指向网络位置，不支持。')
        drive = system_drive()
        on_system = drive is not None and any(
            PureWindowsPath(str(p)).drive.upper() == drive for p in (given, path))
        for candidate in (given.absolute(), path):
            if candidate.parent == candidate:
                if on_system or os.name != 'nt':
                    return refuse('drive_root', '不能把系统盘的根目录作为工作目录。', on_system)
        inside, exact, exceptions = _system_refusals()
        for candidate in (given.absolute(), path):
            if any(_inside(candidate, e) and candidate != e for e in exceptions):
                continue
            if any(_inside(candidate, folder) for folder in inside) or any(candidate == e for e in exact):
                return refuse('system', '这是系统或程序目录，不能作为工作目录。', on_system)
        for candidate in (given.absolute(), path):
            if any(_inside(candidate, folder) for folder in self.closed_folders()):
                return refuse('closed', '这个目录在禁止读写的目录里。', on_system)
        if self.allowed_roots and not any(_inside(path, root) for root in self.allowed_roots):
            return refuse('not_allowed', '这个目录不在允许的范围内（WORKSPACE_ALLOWED_ROOTS）。', on_system)
        return WorkspaceCheck(str(path), on_system)

    def require(self, raw, *, confirmed: bool = False) -> str:
        """The resolved path, or raise WorkspaceError / WorkspaceConfirmationRequired."""
        result = self.check(raw)
        if result.problem is not None:
            raise WorkspaceError(result.problem, result.message)
        if result.system_drive and not confirmed:
            raise WorkspaceConfirmationRequired(
                'workspace_confirmation_required',
                f'这个目录在系统盘（{system_drive()}）上，需要你确认后才能使用。')
        return result.path


SYSTEM_DRIVE_WARNING = ('这个目录在系统盘（{drive}）上：{path}\n'
                        '开启写文件后，智能体可以在这里新建和覆盖文件、在沙盒里运行代码；'
                        '系统盘上常有系统和程序文件，误改可能影响电脑使用，建议换到其他盘。')


def confirm_startup(policy: WorkspacePolicy, paths, *, confirmed: bool, write=print,
                    ask=input, interactive=None) -> str | None:
    """Check the directories given at startup (D-074). Returns a reason to refuse, or None.

    A directory on the system drive needs --confirm-system-drive, or a 'y' typed at an
    interactive terminal."""
    interactive = sys.stdin.isatty() if interactive is None else interactive
    for raw in dict.fromkeys(p for p in paths if p):
        result = policy.check(raw)
        if result.problem is not None:
            return f'{raw}：{result.message}'
        if not result.system_drive or confirmed:
            continue
        write(SYSTEM_DRIVE_WARNING.format(drive=system_drive(), path=result.path))
        if not interactive:
            return '目录在系统盘上，不能交互确认；确认要用它时加 --confirm-system-drive。'
        try:
            answer = ask('仍然使用这个目录吗？输入 y 确认，其他任意输入取消：')
        except EOFError:
            answer = ''
        if answer.strip().lower() != 'y':
            return '已取消：没有确认使用系统盘上的目录。'
    return None
