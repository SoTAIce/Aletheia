"""Personas (docs/persona_plan.md): named sets of the user's own instructions and run defaults.

Each user has a directory under PERSONAS_DIR named by a hash of the user id; each persona is one
file ``<id>.md``: a TOML header between ``+++`` lines, then the instructions as the body. The
user may edit the files by hand; they are read again at the start of every task. ``state.json``
next to them records which persona each session uses, the last one used, and the line each
persona's last /btw added. Every write is an atomic replace under one lock per store.

Nothing here is logged: names and instructions are the user's own text (D-059).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_ID = 'default'
DEFAULT_NAME = '默认'
NAME_LIMIT = 40
TEXT_LIMIT = 4000
MAX_PERSONAS = 20
MAX_SESSIONS = 500
FILE_LIMIT = 64 * 1024
MODES = ('single', 'team', 'auto')
OPTION_KEYS = ('mode', 'write', 'use_memory', 'workspace')
# Words /persona takes as subcommands; a persona may not be named after one.
RESERVED_NAMES = frozenset({'new', 'show', 'set', 'rename', 'delete'})
# Host-written lead of the constraint that carries a persona's instructions; the name stays out.
PERSONA_PREAMBLE = ("Instructions the user wrote for the persona of this session. Follow them unless the "
                    "user's current message or the host rules say otherwise:")
_FENCE = '+++'
_ID_CHARS = frozenset('abcdefghijklmnopqrstuvwxyz0123456789-')


class PersonaError(ValueError):
    """A refused persona operation; code is fixed English, message is Chinese for the user."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code)
        self.code, self.message = code, message


@dataclass(frozen=True, slots=True)
class Persona:
    persona_id: str
    name: str
    instructions: str = ''
    options: dict = field(default_factory=dict)   # only the keys the persona sets, see OPTION_KEYS

    @property
    def chars(self) -> int:
        return len(self.instructions)

    def view(self, *, text: bool = True) -> dict:
        """The persona for the user's own page or terminal; without text, numbers only."""
        data = {'persona_id': self.persona_id, 'name': self.name, 'chars': self.chars,
                'options': dict(self.options)}
        if text:
            data['instructions'] = self.instructions
        return data


def _check_name(name) -> str:
    if not isinstance(name, str):
        raise PersonaError('invalid_name', '人设名字必须是文字。')
    name = name.strip()
    if not name or len(name) > NAME_LIMIT:
        raise PersonaError('invalid_name', f'人设名字不能为空，最多 {NAME_LIMIT} 个字。')
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise PersonaError('invalid_name', '人设名字不能包含换行或控制字符。')
    if name.startswith('/') or name.casefold() in RESERVED_NAMES or name.startswith('--'):
        raise PersonaError('invalid_name', '这个名字和命令冲突，请换一个。')
    return name


def _check_text(text) -> str:
    if not isinstance(text, str):
        raise PersonaError('invalid_text', '指令必须是文字。')
    text = text.replace('\r\n', '\n').strip()
    if '\x00' in text:
        raise PersonaError('invalid_text', '指令里不能有空字符。')
    if len(text) > TEXT_LIMIT:
        raise PersonaError('too_long', f'指令最多 {TEXT_LIMIT} 字，现在是 {len(text)} 字。请删减后再保存。')
    return text


def check_option(key, value):
    """One default option as stored; raises PersonaError. The workspace is checked by the caller."""
    if key not in OPTION_KEYS:
        raise PersonaError('invalid_option', f'不认识的设置：{key}。可以设置 {"、".join(OPTION_KEYS)}。')
    if key == 'mode':
        if value not in MODES:
            raise PersonaError('invalid_option', 'mode 只能是 single、team 或 auto。')
    elif key in ('write', 'use_memory'):
        if type(value) is not bool:
            raise PersonaError('invalid_option', f'{key} 只能是 true 或 false。')
    else:
        if not isinstance(value, str) or not value.strip() or len(value) > 1000 or '\x00' in value:
            raise PersonaError('invalid_option', 'workspace 必须是一个目录路径。')
        if not Path(value).is_absolute():
            raise PersonaError('invalid_option', 'workspace 必须是完整路径，例如 D:\\practice。')
    return value


def parse_option_text(key: str, raw: str):
    """A value typed after /persona set: true/false/开/关 for the switches, else the text."""
    raw = raw.strip()
    if key in ('write', 'use_memory'):
        lowered = raw.lower()
        if lowered in ('true', 'on', 'yes', '开', '是'):
            return True
        if lowered in ('false', 'off', 'no', '关', '否'):
            return False
        raise PersonaError('invalid_option', f'{key} 只能是 true 或 false。')
    return raw


def _toml_value(value) -> str:
    if type(value) is bool:
        return 'true' if value else 'false'
    # A JSON string is a valid TOML basic string (json never writes the one escape TOML lacks, \/).
    return json.dumps(value, ensure_ascii=False)


def render(persona: Persona) -> str:
    lines = [_FENCE, f'name = {_toml_value(persona.name)}']
    for key in OPTION_KEYS:
        if key in persona.options:
            lines.append(f'{key} = {_toml_value(persona.options[key])}')
    lines.append(_FENCE)
    body = persona.instructions
    return '\n'.join(lines) + '\n' + (body + '\n' if body else '')


def parse(persona_id: str, text: str) -> Persona:
    """A persona file's content; raises PersonaError naming the problem, never quoting the file."""
    text = text.lstrip('\ufeff').replace('\r\n', '\n')
    lines = text.split('\n')
    if not lines or lines[0].strip() != _FENCE:
        raise PersonaError('bad_file', '文件开头必须是一行 +++，后面跟设置，再用一行 +++ 结束。')
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == _FENCE)
    except StopIteration:
        raise PersonaError('bad_file', '设置部分缺少结尾的 +++ 行。') from None
    try:
        header = tomllib.loads('\n'.join(lines[1:end]))
    except tomllib.TOMLDecodeError:
        raise PersonaError('bad_file', '设置部分不是合法的 TOML。') from None
    unknown = sorted(set(header) - {'name', *OPTION_KEYS})
    if unknown:
        raise PersonaError('bad_file', f'设置里有不认识的键：{"、".join(unknown)}。')
    if persona_id == DEFAULT_ID and 'name' not in header:
        header['name'] = DEFAULT_NAME
    name = DEFAULT_NAME if persona_id == DEFAULT_ID else _check_name(header.get('name'))
    options = {key: check_option(key, header[key]) for key in OPTION_KEYS if key in header}
    return Persona(persona_id, name, _check_text('\n'.join(lines[end + 1:])), options)


def _user_key(user_id: str | None) -> str:
    return hashlib.sha256((user_id or 'local').encode('utf-8')).hexdigest()[:16]


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{secrets.token_hex(4)}.tmp')
    try:
        with open(temporary, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


_UNSET = object()


class PersonaStore:
    """Personas of every user under one directory. workspace_policy, when given, checks a
    persona's working directory as it is set; without one a persona cannot have a workspace."""

    def __init__(self, directory, *, workspace_policy=None) -> None:
        self._root = Path(directory)
        self._policy = workspace_policy
        self._lock = threading.RLock()

    @property
    def root(self) -> Path:
        return self._root

    # ---- files ----------------------------------------------------------------------------

    def _dir(self, user_id) -> Path:
        return self._root / _user_key(user_id)

    def _path(self, user_id, persona_id: str) -> Path:
        if not isinstance(persona_id, str) or not persona_id or not set(persona_id) <= _ID_CHARS \
                or len(persona_id) > 32:
            raise PersonaError('not_found', '没有这个人设。')
        return self._dir(user_id) / f'{persona_id}.md'

    def file_path(self, user_id, persona_id: str) -> Path:
        """Where the persona's file is (or will be), for the user to open in an editor."""
        return self._path(user_id, persona_id)

    def _read(self, path: Path, persona_id: str) -> Persona:
        try:
            if path.is_symlink() or not path.is_file():
                raise PersonaError('bad_file', '人设文件不是普通文件。')
            if path.stat().st_size > FILE_LIMIT:
                raise PersonaError('too_long', '人设文件太大了。')
            raw = path.read_bytes()
        except OSError:
            raise PersonaError('unreadable', '人设文件读不出来。') from None
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            raise PersonaError('bad_file', '人设文件不是 UTF-8 编码。') from None
        return parse(persona_id, text)

    def _state(self, user_id) -> dict:
        path = self._dir(user_id) / 'state.json'
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        sessions = data.get('sessions') if isinstance(data.get('sessions'), dict) else {}
        last_btw = data.get('last_btw') if isinstance(data.get('last_btw'), dict) else {}
        last = data.get('last') if isinstance(data.get('last'), str) else None
        return {'sessions': {k: v for k, v in sessions.items() if isinstance(k, str) and isinstance(v, str)},
                'last_btw': {k: v for k, v in last_btw.items() if isinstance(k, str) and isinstance(v, str)},
                'last': last}

    def _save_state(self, user_id, state: dict) -> None:
        sessions = state['sessions']
        while len(sessions) > MAX_SESSIONS:
            sessions.pop(next(iter(sessions)))
        _write_atomic(self._dir(user_id) / 'state.json',
                      json.dumps(state, ensure_ascii=False, indent=1) + '\n')

    def _ids(self, user_id) -> list[str]:
        directory = self._dir(user_id)
        try:
            names = sorted(p.stem for p in directory.glob('*.md'))
        except OSError:
            names = []
        ids = [n for n in names if n != DEFAULT_ID and set(n) <= _ID_CHARS and len(n) <= 32]
        return [DEFAULT_ID, *ids]

    # ---- reading --------------------------------------------------------------------------

    def get(self, user_id, persona_id: str) -> Persona:
        """One persona; the default one exists even before its file does."""
        with self._lock:
            path = self._path(user_id, persona_id)
            if not path.exists():
                if persona_id == DEFAULT_ID:
                    return Persona(DEFAULT_ID, DEFAULT_NAME)
                raise PersonaError('not_found', '没有这个人设。')
            return self._read(path, persona_id)

    def list_personas(self, user_id) -> list[dict]:
        """Every persona's view without its text; a broken file is listed with its problem."""
        with self._lock:
            entries = []
            for persona_id in self._ids(user_id):
                try:
                    entries.append(self.get(user_id, persona_id).view(text=False))
                except PersonaError as exc:
                    entries.append({'persona_id': persona_id, 'name': None, 'problem': exc.code,
                                    'message': exc.message})
            return entries

    def find(self, user_id, name_or_id: str) -> Persona:
        """A persona by its name (case and surrounding spaces ignored) or its id."""
        wanted = (name_or_id or '').strip()
        with self._lock:
            for persona_id in self._ids(user_id):
                if persona_id == wanted:
                    return self.get(user_id, persona_id)
                try:
                    persona = self.get(user_id, persona_id)
                except PersonaError:
                    continue
                if persona.name.casefold() == wanted.casefold():
                    return persona
        raise PersonaError('not_found', f'没有叫「{wanted}」的人设。用 /persona 查看现有的人设。')

    def session_persona_id(self, user_id, session_id: str) -> str:
        """The persona a session is bound to; an unbound session takes the last one used."""
        with self._lock:
            state = self._state(user_id)
            chosen = state['sessions'].get(session_id) or state['last'] or DEFAULT_ID
            if chosen != DEFAULT_ID and not self._path(user_id, chosen).exists():
                return DEFAULT_ID       # deleted by hand
            return chosen

    def is_bound(self, user_id, session_id: str) -> bool:
        with self._lock:
            return session_id in self._state(user_id)['sessions']

    # ---- writing --------------------------------------------------------------------------

    def bind(self, user_id, session_id: str, persona_id: str) -> Persona:
        """Bind a session to a persona for its next tasks; it also becomes the last one used."""
        if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 256:
            raise PersonaError('invalid_session', '会话编号不对。')
        with self._lock:
            persona = self.get(user_id, persona_id)
            state = self._state(user_id)
            state['sessions'].pop(session_id, None)
            state['sessions'][session_id] = persona.persona_id
            state['last'] = persona.persona_id
            self._save_state(user_id, state)
            return persona

    def _write(self, user_id, persona: Persona) -> Persona:
        _write_atomic(self._path(user_id, persona.persona_id), render(persona))
        return persona

    def _names(self, user_id, *, skip: str | None = None) -> set[str]:
        names = set()
        for persona_id in self._ids(user_id):
            if persona_id == skip:
                continue
            try:
                names.add(self.get(user_id, persona_id).name.casefold())
            except PersonaError:
                continue
        return names

    def create(self, user_id, name: str, instructions: str = '', options: dict | None = None) -> Persona:
        with self._lock:
            name = _check_name(name)
            if len(self._ids(user_id)) >= MAX_PERSONAS:
                raise PersonaError('too_many', f'最多 {MAX_PERSONAS} 个人设。')
            if name.casefold() in self._names(user_id) or name == DEFAULT_NAME:
                raise PersonaError('duplicate', f'已经有叫「{name}」的人设了。')
            persona_id = 'p-' + secrets.token_hex(4)
            while self._path(user_id, persona_id).exists():
                persona_id = 'p-' + secrets.token_hex(4)
            checked = {key: self._check_option(key, value) for key, value in (options or {}).items()}
            return self._write(user_id, Persona(persona_id, name, _check_text(instructions), checked))

    def _check_option(self, key, value):
        value = check_option(key, value)
        if key == 'workspace':
            if self._policy is None:
                raise PersonaError('workspace_unavailable', '这个服务不能更换工作目录，人设里不能设工作目录。')
            result = self._policy.check(value)
            if result.problem is not None:
                raise PersonaError('workspace_invalid', '不能使用这个目录：' + (result.message or ''))
            if result.system_drive:
                raise PersonaError('workspace_system_drive',
                                   '人设里不能放系统盘上的目录；要用的话，每次任务前用 /dir 选择并确认。')
            value = result.path
        return value

    def update(self, user_id, persona_id: str, *, name=_UNSET, instructions=_UNSET,
               options: dict | None = None) -> Persona:
        """Change a persona; in options a value of None removes that default."""
        with self._lock:
            current = self.get(user_id, persona_id)
            new_name = current.name
            if name is not _UNSET:
                if persona_id == DEFAULT_ID:
                    raise PersonaError('default_fixed', '“默认”人设不能改名。')
                new_name = _check_name(name)
                if new_name.casefold() != current.name.casefold() and (
                        new_name.casefold() in self._names(user_id, skip=persona_id) or new_name == DEFAULT_NAME):
                    raise PersonaError('duplicate', f'已经有叫「{new_name}」的人设了。')
            text = current.instructions if instructions is _UNSET else _check_text(instructions)
            merged = dict(current.options)
            for key, value in (options or {}).items():
                if value is None:
                    if key not in OPTION_KEYS:
                        check_option(key, None)      # raises: an unknown key
                    merged.pop(key, None)
                else:
                    merged[key] = self._check_option(key, value)
            persona = Persona(persona_id, new_name, text, {k: merged[k] for k in OPTION_KEYS if k in merged})
            self._write(user_id, persona)
            if instructions is not _UNSET:
                state = self._state(user_id)
                if state['last_btw'].pop(persona_id, None) is not None:
                    self._save_state(user_id, state)
            return persona

    def delete(self, user_id, persona_id: str) -> None:
        """Delete a persona; sessions that used it go back to the default one."""
        if persona_id == DEFAULT_ID:
            raise PersonaError('default_fixed', '“默认”人设不能删除。')
        with self._lock:
            path = self._path(user_id, persona_id)
            if not path.exists():
                raise PersonaError('not_found', '没有这个人设。')
            path.unlink()
            state = self._state(user_id)
            state['sessions'] = {k: v for k, v in state['sessions'].items() if v != persona_id}
            state['last_btw'].pop(persona_id, None)
            if state['last'] == persona_id:
                state['last'] = DEFAULT_ID
            self._save_state(user_id, state)

    def btw(self, user_id, persona_id: str, text: str) -> Persona:
        """Add one line to the end of a persona's instructions (/btw)."""
        if not isinstance(text, str) or not text.strip():
            raise PersonaError('invalid_text', '用法：/btw 要加进指令的一句话。')
        line = ' '.join(text.split())
        with self._lock:
            current = self.get(user_id, persona_id)
            joined = f'{current.instructions}\n{line}' if current.instructions else line
            if len(joined) > TEXT_LIMIT:
                raise PersonaError('too_long', f'加上这一行就超过 {TEXT_LIMIT} 字了。请先在编辑器里整理：'
                                   + str(self._path(user_id, persona_id)))
            persona = Persona(persona_id, current.name, joined, current.options)
            self._write(user_id, persona)
            state = self._state(user_id)
            state['last_btw'][persona_id] = line
            self._save_state(user_id, state)
            return persona

    def undo_btw(self, user_id, persona_id: str) -> Persona:
        """Remove the line the last /btw added, only while it is still the last line."""
        with self._lock:
            current = self.get(user_id, persona_id)
            state = self._state(user_id)
            line = state['last_btw'].get(persona_id)
            if line is None:
                raise PersonaError('nothing_to_undo', '没有可以撤掉的 /btw。')
            if current.instructions == line:
                text = ''
            elif current.instructions.endswith('\n' + line):
                text = current.instructions[:-len(line) - 1].rstrip()
            else:
                raise PersonaError('changed', '指令已经改过，不能自动撤销，请直接编辑文件：'
                                   + str(self._path(user_id, persona_id)))
            persona = Persona(persona_id, current.name, text, current.options)
            self._write(user_id, persona)
            del state['last_btw'][persona_id]
            self._save_state(user_id, state)
            return persona
