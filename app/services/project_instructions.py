"""Working-directory project instructions (D-096, docs/project_instructions_plan.md).

Reads at most one project instruction file from the working directory root
(.aletheia/instructions.md, then AGENTS.md), checking that it is a regular UTF-8 file
inside the working directory, not a symlink/junction, and not inside a closed folder.
Every directory + filename + content SHA-256 digest must be confirmed by the user before
its text is added as a task constraint.

Nothing here logs file text or paths; only exception type names are logged (D-033, D-059).
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import stat
import threading

from app.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

INSTRUCTION_FILES: tuple[str, ...] = ('.aletheia/instructions.md', 'AGENTS.md')
MAX_INSTRUCTION_CHARS = 8000
MAX_INSTRUCTION_BYTES = 1_000_000
DEFAULT_TRUST_PATH = Path(PROJECT_ROOT) / 'data' / 'instructions_confirmed.json'

PROJECT_INSTRUCTIONS_PREAMBLE = (
    "Project instructions from the working directory, confirmed by the user. "
    "Follow them unless the user's current message, the user's persona instructions "
    "or the host rules say otherwise:"
)
TRUNCATED_NOTE = (
    f"\n\n[Host note: the project instructions file exceeded {MAX_INSTRUCTION_CHARS} "
    "characters; the remainder was truncated.]"
)

REASON_LABELS: dict[str, str] = {
    'link': '是符号链接或目录联接',
    'not_file': '不是普通文件',
    'outside': '真实路径不在工作目录内',
    'closed': '在禁止读写的目录里',
    'too_large': '文件超过大小上限',
    'not_utf8': '不是 UTF-8 编码',
    'error': '读取失败',
}

_REPARSE_POINT_FLAG = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)


class InstructionsChanged(ValueError):
    """Raised when confirming a project instructions file whose content changed or disappeared."""

    def __init__(self, message: str = '项目说明文件已经变化，请刷新查看最新内容后再确认。') -> None:
        super().__init__('instructions_changed')
        self.code = 'instructions_changed'
        self.message = message


@dataclass(frozen=True, slots=True)
class ProjectInstructions:
    name: str
    text: str
    chars: int
    truncated: bool
    digest: str
    status: str                 # loaded | unconfirmed | changed | unreadable
    reason: str | None = None   # link | not_file | outside | closed | too_large | not_utf8 | error

    def constraint_text(self) -> str:
        """Host-wrapped constraint text sent to the model when status == 'loaded'."""
        body = PROJECT_INSTRUCTIONS_PREAMBLE + '\n\n' + self.text
        if self.truncated:
            body += TRUNCATED_NOTE
        return body

    def event(self) -> dict:
        """Progress event payload; carries neither file text nor directory path."""
        return {
            'type': 'instructions',
            'file': self.name,
            'chars': self.chars,
            'truncated': self.truncated,
            'status': self.status,
            'reason': self.reason,
        }

    def api_view(self) -> dict:
        """Web API response payload; includes text only when confirmation is needed."""
        data = {
            'status': self.status,
            'file': self.name,
            'chars': self.chars,
            'truncated': self.truncated,
            'reason': self.reason,
            'digest': self.digest,
        }
        if self.status in ('unconfirmed', 'changed'):
            data['text'] = self.text
        return data


def instructions_notice(event: dict) -> str:
    """Single-line Chinese notice for an 'instructions' progress event (CLI)."""
    name = event.get('file') or ''
    status = event.get('status')
    if status == 'loaded':
        line = f"本次任务带了项目说明 {name}（{event.get('chars', 0)} 字）"
        if event.get('truncated'):
            line += f'，超过 {MAX_INSTRUCTION_CHARS} 字的部分没带上'
        return line
    if status == 'unconfirmed':
        return f'这个目录有项目说明 {name}，还没确认，这次没带上（交互模式会问你，单次任务加 --trust-instructions）'
    if status == 'changed':
        return f'项目说明 {name} 改过了，需要重新确认，这次没带上'
    if status == 'unreadable':
        reason = REASON_LABELS.get(event.get('reason') or '', '读取失败')
        return f'项目说明 {name} 读不了（{reason}），这次没带上'
    return ''


def _norm_workspace(workspace: str | os.PathLike) -> str:
    return os.path.normcase(str(Path(workspace).resolve()))


def _inside(path: Path, folder: Path) -> bool:
    left = os.path.normcase(str(path))
    right = os.path.normcase(str(folder))
    try:
        return os.path.commonpath([left, right]) == right
    except ValueError:
        return False


def _is_link_or_reparse(path: Path) -> bool:
    if os.path.islink(path):
        return True
    is_junction = getattr(path, 'is_junction', None)
    if callable(is_junction):
        try:
            if is_junction():
                return True
        except OSError:
            pass
    if os.name == 'nt':
        try:
            st = os.lstat(path)
        except OSError:
            return False
        if getattr(st, 'st_file_attributes', 0) & _REPARSE_POINT_FLAG:
            return True
    return False


def _exists_lstat(path: Path) -> bool:
    try:
        os.lstat(path)
        return True
    except OSError:
        return False


def _unreadable(name: str, reason: str) -> ProjectInstructions:
    return ProjectInstructions(
        name=name,
        text='',
        chars=0,
        truncated=False,
        digest='',
        status='unreadable',
        reason=reason,
    )


class InstructionsTrust:
    """Machine-local JSON record of confirmed (workspace, filename) -> SHA-256 digest."""

    def __init__(self, path: str | os.PathLike | None = None) -> None:
        self.path = DEFAULT_TRUST_PATH if path is None else Path(path)
        self._lock = threading.Lock()

    @staticmethod
    def _key(workspace: str | os.PathLike, name: str) -> str:
        return f'{_norm_workspace(workspace)}|{name}'

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            raw = self.path.read_text(encoding='utf-8')
            data = json.loads(raw)
        except Exception as exc:
            logger.warning('Project instructions trust file unreadable: %s', type(exc).__name__)
            return {}
        if not isinstance(data, dict):
            logger.warning('Project instructions trust file invalid format')
            return {}
        return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}

    def status_of(self, workspace: str | os.PathLike, name: str, digest: str) -> str:
        with self._lock:
            records = self._load()
        key = self._key(workspace, name)
        if key not in records:
            return 'unconfirmed'
        return 'loaded' if records[key] == digest else 'changed'

    def confirm(self, workspace: str | os.PathLike, name: str, digest: str) -> None:
        key = self._key(workspace, name)
        with self._lock:
            records = self._load()
            records[key] = digest
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f'{self.path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp')
            try:
                tmp.write_text(
                    json.dumps(records, ensure_ascii=False, sort_keys=True, indent=2) + '\n',
                    encoding='utf-8',
                )
                os.replace(tmp, self.path)
            except Exception:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                raise


def find_instructions(
    workspace: str | os.PathLike | None,
    closed_folders,
    trust: InstructionsTrust,
) -> ProjectInstructions | None:
    """Locate, validate, read once, hash, truncate and check trust for a workspace's instructions."""
    if not workspace:
        return None
    try:
        root = Path(workspace).resolve(strict=True)
    except OSError:
        return None
    if not root.is_dir():
        return None

    closed = tuple(Path(folder).resolve() for folder in (closed_folders or ()))

    for name in INSTRUCTION_FILES:
        candidate = root / name
        if not _exists_lstat(candidate):
            continue

        # Check every component from workspace root down to the candidate for links/junctions.
        current = root
        for part in Path(name).parts:
            current = current / part
            if _is_link_or_reparse(current):
                return _unreadable(name, 'link')

        try:
            st = os.lstat(candidate)
        except OSError:
            return _unreadable(name, 'error')
        if not stat.S_ISREG(st.st_mode):
            return _unreadable(name, 'not_file')

        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            return _unreadable(name, 'error')
        if not _inside(resolved, root):
            return _unreadable(name, 'outside')
        if any(_inside(resolved, folder) for folder in closed):
            return _unreadable(name, 'closed')

        if st.st_size > MAX_INSTRUCTION_BYTES:
            return _unreadable(name, 'too_large')

        try:
            with open(candidate, 'rb') as fh:
                raw = fh.read(MAX_INSTRUCTION_BYTES + 1)
        except OSError:
            return _unreadable(name, 'error')
        if len(raw) > MAX_INSTRUCTION_BYTES:
            return _unreadable(name, 'too_large')

        digest = hashlib.sha256(raw).hexdigest()
        try:
            decoded = raw.decode('utf-8')
        except UnicodeDecodeError:
            return _unreadable(name, 'not_utf8')

        chars = len(decoded)
        truncated = chars > MAX_INSTRUCTION_CHARS
        text = decoded[:MAX_INSTRUCTION_CHARS] if truncated else decoded
        status = trust.status_of(root, name, digest)
        return ProjectInstructions(
            name=name,
            text=text,
            chars=chars,
            truncated=truncated,
            digest=digest,
            status=status,
            reason=None,
        )

    return None
