"""Personal commands (docs/personal_commands_plan.md): task templates that belong to a persona.

Each persona has its own commands in ``<user dir>/commands/<persona id>/<name>.md``, beside the
persona files: a TOML header between ``+++`` lines, then the template as the body. The first time
a persona's commands are used, the six presets of ``command_presets/`` are copied in; after that
the files are the user's own, and a deleted preset does not come back unless reset. Whether the
copy happened is the directory's existence.

A command only narrows what a task may do: ``write = false`` forces a read-only task, ``write =
true`` refuses to start when writing is off; it never turns writing on. ``mode`` fixes the mode.

Nothing here is logged: templates are the user's own text (D-059).
"""

from __future__ import annotations

import re
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path

from app.services.personas import FILE_LIMIT, PersonaError, _toml_value, _write_atomic

PRESET_DIR = Path(__file__).with_name('command_presets')
PRESET_NAMES = ('explain', 'quiz', 'note', 'review', 'test', 'fix')
NAME_LIMIT = 20
DESCRIPTION_LIMIT = 60
USAGE_LIMIT = 100
TEXT_LIMIT = 4000
MAX_COMMANDS = 30
ARG_KINDS = ('required', 'optional', 'none')
COMMAND_MODES = ('single', 'team')
FIELDS = ('description', 'usage', 'args', 'mode', 'write')
PLACEHOLDER = '{{args}}'
NO_ARGS = '（未指定）'
# The interactive mode's own commands; a personal command may not take one of these names.
RESERVED = frozenset({'help', 'new', 'exit', 'quit', 'dir', 'remember', 'memories', 'forget',
                      'persona', 'btw', 'commands'})
_NAME = re.compile(r'[a-z][a-z0-9-]{0,19}')
_LINE = re.compile(r'/([a-z][a-z0-9-]{0,19})(?:\s+(.*))?', re.DOTALL)
# Host-written lead of the constraint that carries a command's expanded template.
COMMAND_PREAMBLE = ('The user ran the personal command /{name}. What it asks for, written by the user, '
                    'with the user\'s arguments filled in; it is the request for this task:')
_FENCE = '+++'


class CommandError(PersonaError):
    """A refused command operation; code is fixed English, message is Chinese for the user."""


@dataclass(frozen=True, slots=True)
class Command:
    name: str
    description: str
    template: str
    args: str = 'optional'
    usage: str | None = None
    mode: str | None = None
    write: bool | None = None

    @property
    def chars(self) -> int:
        return len(self.template)

    @property
    def usage_text(self) -> str:
        return self.usage or f'/{self.name} 参数'

    @property
    def fixed(self) -> list[str]:
        """Which run choices the command fixes, by key."""
        return [key for key in ('mode', 'write') if getattr(self, key) is not None]

    def view(self, *, text: bool = True) -> dict:
        data = {'name': self.name, 'description': self.description, 'usage': self.usage_text,
                'args': self.args, 'mode': self.mode, 'write': self.write, 'chars': self.chars,
                'preset': self.name in PRESET_NAMES}
        if text:
            data['template'] = self.template
            data['own_usage'] = self.usage
        return data


def check_name(name) -> str:
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise CommandError('invalid_name', f'命令名要用小写字母开头，只能有小写字母、数字和 -，最多 {NAME_LIMIT} 个字符。')
    if name in RESERVED:
        raise CommandError('invalid_name', f'/{name} 是已有的命令，请换一个名字。')
    return name


def _one_line(key: str, value, limit: int, *, required: bool) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise CommandError('invalid_field', f'{key} 必须是文字。')
    value = value.strip()
    if not value:
        if required:
            raise CommandError('invalid_field', f'{key} 不能为空。')
        return None
    if len(value) > limit or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise CommandError('invalid_field', f'{key} 只能是一行，最多 {limit} 字。')
    return value


def check_field(key: str, value):
    """One header field as stored; None means not set (not allowed for description and args)."""
    if key == 'description':
        return _one_line('description', value, DESCRIPTION_LIMIT, required=True)
    if key == 'usage':
        return _one_line('usage', value, USAGE_LIMIT, required=False)
    if key == 'args':
        if value not in ARG_KINDS:
            raise CommandError('invalid_field', 'args 只能是 required、optional 或 none。')
        return value
    if key == 'mode':
        if value is not None and value not in COMMAND_MODES:
            raise CommandError('invalid_field', 'mode 只能是 single 或 team，不写表示不固定。')
        return value
    if key == 'write':
        if value is not None and type(value) is not bool:
            raise CommandError('invalid_field', 'write 只能是 true 或 false，不写表示不管。')
        return value
    raise CommandError('invalid_field', f'不认识的设置：{key}。可以设置 {"、".join(FIELDS)}。')


def check_template(text) -> str:
    if not isinstance(text, str):
        raise CommandError('invalid_text', '模板必须是文字。')
    text = text.replace('\r\n', '\n').strip()
    if '\x00' in text:
        raise CommandError('invalid_text', '模板里不能有空字符。')
    if not text:
        raise CommandError('invalid_text', '模板不能为空。')
    if len(text) > TEXT_LIMIT:
        raise CommandError('too_long', f'模板最多 {TEXT_LIMIT} 字，现在是 {len(text)} 字。请删减后再保存。')
    return text


def render(command: Command) -> str:
    lines = [_FENCE]
    for key in FIELDS:
        value = getattr(command, key)
        if value is not None:
            lines.append(f'{key} = {_toml_value(value)}')
    lines.append(_FENCE)
    return '\n'.join(lines) + '\n' + command.template + '\n'


def parse(name: str, text: str) -> Command:
    """A command file's content; raises CommandError naming the problem, never quoting the file."""
    text = text.lstrip('﻿').replace('\r\n', '\n')
    lines = text.split('\n')
    if not lines or lines[0].strip() != _FENCE:
        raise CommandError('bad_file', '文件开头必须是一行 +++，后面跟设置，再用一行 +++ 结束。')
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == _FENCE)
    except StopIteration:
        raise CommandError('bad_file', '设置部分缺少结尾的 +++ 行。') from None
    try:
        header = tomllib.loads('\n'.join(lines[1:end]))
    except tomllib.TOMLDecodeError:
        raise CommandError('bad_file', '设置部分不是合法的 TOML。') from None
    unknown = sorted(set(header) - set(FIELDS))
    if unknown:
        raise CommandError('bad_file', f'设置里有不认识的键：{"、".join(unknown)}。')
    if 'description' not in header:
        raise CommandError('bad_file', '设置里缺少 description（列表里显示的一句话说明）。')
    fields = {key: check_field(key, header[key]) for key in FIELDS if key in header}
    fields.setdefault('args', 'optional')
    return Command(check_name(name), template=check_template('\n'.join(lines[end + 1:])), **fields)


def split_line(line: str) -> tuple[str, str] | None:
    """('review', 'app/api') for '/review app/api'; None when the line is not a command shape."""
    found = _LINE.fullmatch(line.strip()) if isinstance(line, str) else None
    if found is None:
        return None
    return found.group(1), (found.group(2) or '').strip()


def expand(command: Command, args: str) -> str:
    """The template with the arguments filled in; raises when they do not fit the command."""
    args = (args or '').strip()
    if command.args == 'required' and not args:
        raise CommandError('missing_args', f'/{command.name} 后面要写参数。用法：{command.usage_text}')
    if command.args == 'none' and args:
        raise CommandError('unexpected_args', f'/{command.name} 不收参数。用法：{command.usage_text}')
    if PLACEHOLDER in command.template:
        return command.template.replace(PLACEHOLDER, args or NO_ARGS)
    return command.template + ('\n' + args if args else '')


def constraint(command: Command, args: str) -> str:
    return COMMAND_PREAMBLE.format(name=command.name) + '\n\n' + expand(command, args)


def for_task(book: 'CommandBook', user_id, persona_id: str, name: str, line: str, modes) -> tuple[Command, str]:
    """The command a new task runs and its constraint text; line is the user's whole input."""
    parts = split_line(line)
    if parts is None or parts[0] != name:
        raise CommandError('mismatch', f'这条消息不是以 /{name} 开头的。')
    command = book.get(user_id, persona_id, name)
    if command.mode is not None and command.mode not in modes:
        raise CommandError('mode_unavailable', f'/{name} 固定用团队模式，但服务启动时没有准备团队能力。')
    return command, constraint(command, parts[1])


def needs_write(command: Command) -> CommandError:
    return CommandError('needs_write', f'/{command.name} 要改文件，但这次是只读：网页上打开“写文件”，'
                        '或者把人设默认设置里的写文件改成开，再试一次。')


def preset(name: str) -> Command:
    return parse(name, (PRESET_DIR / f'{name}.md').read_text(encoding='utf-8'))


def skeleton(name: str) -> Command:
    """What /commands new writes: a valid command for the user to fill in."""
    return Command(name, description='新命令，还没写说明', args='optional',
                   template='在这里写这个命令要做什么。命令后面写的参数会替换下面这个记号：\n' + PLACEHOLDER)


class CommandBook:
    """The commands of every persona of every user, under a PersonaStore's directory and lock."""

    def __init__(self, personas) -> None:
        self._personas = personas
        self._lock = personas._lock

    def directory(self, user_id, persona_id: str) -> Path:
        """The persona's command directory; the persona must exist."""
        self._personas.get(user_id, persona_id)            # raises not_found
        return self._personas._dir(user_id) / 'commands' / persona_id

    def _ready(self, user_id, persona_id: str) -> Path:
        """The directory, with the presets copied in the first time it is used."""
        directory = self.directory(user_id, persona_id)
        if directory.is_symlink():
            raise CommandError('bad_file', '命令目录不是普通目录。')
        if not directory.exists():
            directory.mkdir(parents=True)
            for name in PRESET_NAMES:
                _write_atomic(directory / f'{name}.md', render(preset(name)))
        return directory

    def file_path(self, user_id, persona_id: str, name: str) -> Path:
        return self.directory(user_id, persona_id) / f'{check_name(name)}.md'

    def _names(self, directory: Path) -> list[str]:
        try:
            stems = sorted(p.stem for p in directory.glob('*.md'))
        except OSError:
            stems = []
        return [s for s in stems if _NAME.fullmatch(s) and s not in RESERVED]

    def _read(self, path: Path, name: str) -> Command:
        try:
            if path.is_symlink() or not path.is_file():
                raise CommandError('not_found', f'没有 /{name} 这个命令。')
            if path.stat().st_size > FILE_LIMIT:
                raise CommandError('too_long', '命令文件太大了。')
            raw = path.read_bytes()
        except OSError:
            raise CommandError('unreadable', '命令文件读不出来。') from None
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            raise CommandError('bad_file', '命令文件不是 UTF-8 编码。') from None
        return parse(name, text)

    # ---- reading --------------------------------------------------------------------------

    def list_commands(self, user_id, persona_id: str) -> list[dict]:
        """Every command's view without its template; a broken file is listed with its problem."""
        with self._lock:
            directory = self._ready(user_id, persona_id)
            entries = []
            for name in self._names(directory):
                try:
                    entries.append(self._read(directory / f'{name}.md', name).view(text=False))
                except CommandError as exc:
                    entries.append({'name': name, 'problem': exc.code, 'message': exc.message,
                                    'preset': name in PRESET_NAMES})
            return entries

    def get(self, user_id, persona_id: str, name: str) -> Command:
        with self._lock:
            directory = self._ready(user_id, persona_id)
            return self._read(directory / f'{check_name(name)}.md', name)

    def has(self, user_id, persona_id: str, name: str) -> bool:
        """Whether the persona has a command of this name (a broken file counts: running it says why)."""
        if not isinstance(name, str) or not _NAME.fullmatch(name) or name in RESERVED:
            return False
        with self._lock:
            path = self._ready(user_id, persona_id) / f'{name}.md'
            return path.is_file() and not path.is_symlink()

    def count(self, user_id, persona_id: str) -> int:
        """How many command files the persona has; none before its first use (no copy is made)."""
        with self._lock:
            directory = self.directory(user_id, persona_id)
            return len(self._names(directory)) if directory.is_dir() else 0

    # ---- writing --------------------------------------------------------------------------

    def create(self, user_id, persona_id: str, name: str, fields: dict | None = None,
               template: str | None = None) -> Command:
        """A new command; without fields and template, the skeleton for the user to fill in."""
        name = check_name(name)
        with self._lock:
            directory = self._ready(user_id, persona_id)
            if (directory / f'{name}.md').exists():
                raise CommandError('duplicate', f'已经有 /{name} 这个命令了。')
            if len(self._names(directory)) >= MAX_COMMANDS:
                raise CommandError('too_many', f'一个人设最多 {MAX_COMMANDS} 个命令。')
            base = skeleton(name)
            command = self._merged(base, fields or {}, template)
            _write_atomic(directory / f'{name}.md', render(command))
            return command

    @staticmethod
    def _merged(current: Command, fields: dict, template) -> Command:
        values = {key: getattr(current, key) for key in FIELDS}
        for key, value in fields.items():
            if key in ('description', 'args') and value is None:
                raise CommandError('invalid_field', f'{key} 不能为空。')
            values[key] = check_field(key, value)
        text = current.template if template is None else check_template(template)
        return Command(current.name, template=text, **values)

    def update(self, user_id, persona_id: str, name: str, fields: dict | None = None,
               template: str | None = None) -> Command:
        """Change a command; in fields, None removes usage, mode or write."""
        with self._lock:
            current = self.get(user_id, persona_id, name)
            command = self._merged(current, fields or {}, template)
            _write_atomic(self.directory(user_id, persona_id) / f'{command.name}.md', render(command))
            return command

    def delete(self, user_id, persona_id: str, name: str) -> None:
        with self._lock:
            path = self._ready(user_id, persona_id) / f'{check_name(name)}.md'
            if not path.is_file() or path.is_symlink():
                raise CommandError('not_found', f'没有 /{name} 这个命令。')
            path.unlink()

    def reset(self, user_id, persona_id: str, name: str) -> Command:
        """Put a preset back as the repository has it."""
        if name not in PRESET_NAMES:
            raise CommandError('not_preset', f'/{name} 不是预置命令，没有原样可以恢复。预置命令：'
                               + '、'.join('/' + n for n in PRESET_NAMES) + '。')
        with self._lock:
            directory = self._ready(user_id, persona_id)
            command = preset(name)
            _write_atomic(directory / f'{name}.md', render(command))
            return command

    def remove_persona(self, user_id, persona_id: str) -> None:
        """Delete a persona's command directory (the persona is being deleted)."""
        with self._lock:
            directory = self._personas._dir(user_id) / 'commands' / persona_id
            if directory.is_symlink():
                directory.unlink()
            elif directory.is_dir():
                shutil.rmtree(directory)
