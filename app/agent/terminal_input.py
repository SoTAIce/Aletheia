"""Unified terminal reader and CLI permission confirmation (D-094).

A single TerminalReader owns stdin so the interactive prompt and tool-call confirmations never
compete for lines. When a confirmation times out while the input thread is still blocked, the
late line is drained and discarded before the next prompt is shown.
"""

from __future__ import annotations

import asyncio
import time
from typing import Callable

from app.agent.hooks import ALLOW, Decision, extend_deadlines

MAX_LINE_CHARS = 200
STALE_INPUT_NOTICE = '刚才的输入是对已超时确认的回答，已忽略'
TIMED_OUT_NOTICE = '{seconds:g} 秒内没有回答，这次调用按拒绝处理；接下来输入的第一行会被忽略。'


def _clip(text: str, limit: int = MAX_LINE_CHARS) -> str:
    single = text.replace('\r\n', ' ').replace('\r', ' ').replace('\n', ' ')
    if len(single) <= limit:
        return single
    return single[:limit - 1] + '…'


def _consume_exception(task: asyncio.Task) -> None:
    if not task.cancelled():
        task.exception()


class TerminalReader:
    """Serialize all terminal line reads through one in-flight input thread at a time."""

    def __init__(self, read_fn: Callable[[str], str] = input, write: Callable[[str], object] = print) -> None:
        self._read_fn = read_fn
        self._write = write
        self._lock = asyncio.Lock()
        self._pending: asyncio.Task | None = None
        self._stale = False

    async def read_line(self, prompt: str = '', *, timeout: float | None = None) -> str:
        async with self._lock:
            remaining = timeout
            if self._pending is not None:
                pending = self._pending
                started = time.monotonic()
                try:
                    if remaining is None:
                        await asyncio.shield(pending)
                    else:
                        await asyncio.wait_for(asyncio.shield(pending), remaining)
                        remaining = max(0.001, remaining - (time.monotonic() - started))
                except TimeoutError:
                    raise
                except EOFError:
                    self._pending = None
                    self._stale = False
                    raise
                else:
                    self._pending = None
                    if self._stale:
                        self._stale = False
                        self._write(STALE_INPUT_NOTICE)

            task = asyncio.create_task(asyncio.to_thread(self._read_fn, prompt))
            task.add_done_callback(_consume_exception)
            if remaining is None:
                return await task
            try:
                return await asyncio.wait_for(asyncio.shield(task), remaining)
            except TimeoutError:
                self._pending = task
                self._stale = True
                raise
            except asyncio.CancelledError:
                if not task.done():
                    self._pending = task
                    self._stale = True
                raise


def format_approval_lines(event: dict, rule: int | None = None) -> list[str]:
    """Host-authored Chinese summary of a tool call for terminal approval, from args_preview only."""
    tool = str(event.get('tool') or '')
    preview = event.get('args_preview') if isinstance(event.get('args_preview'), dict) else {}
    header = f'权限规则 {rule} 要求确认：{tool}' if rule is not None else f'需要确认：{tool}'
    lines = [_clip(header)]

    if 'path' in preview:
        raw_path = preview['path']
        if isinstance(raw_path, str):
            lines.append(_clip(f'  路径：{raw_path}'))
        elif isinstance(raw_path, dict) and isinstance(raw_path.get('text'), str):
            suffix = '（已截短）' if raw_path.get('omitted_chars', 0) > 0 else ''
            lines.append(_clip(f'  路径：{raw_path["text"]}{suffix}'))
        else:
            lines.append('  路径：（非字符串）')

    if tool == 'write_file' and isinstance(preview.get('content'), dict):
        content = preview['content']
        chars = content.get('chars', 0)
        lines.append(_clip(f'  写入内容：共 {chars} 字符'))
        text = content.get('text')
        if isinstance(text, str):
            raw_lines = text.splitlines()
            for item in raw_lines[:5]:
                lines.append(_clip(f'    {item}'))
            if len(raw_lines) > 5 or content.get('omitted_chars', 0) > 0:
                lines.append('    …（其余内容已省略）')
    elif tool == 'edit_file':
        old = preview.get('old')
        new = preview.get('new')
        old_chars = old.get('chars') if isinstance(old, dict) else None
        new_chars = new.get('chars') if isinstance(new, dict) else None
        if old_chars is not None or new_chars is not None:
            lines.append(_clip(f'  修改内容：old {old_chars} 字符，new {new_chars} 字符'))
    elif tool == 'run_python' and isinstance(preview.get('args'), dict):
        args = preview['args']
        items = args.get('items')
        if isinstance(items, list):
            rendered = ' '.join(str(item) for item in items)
            if args.get('omitted_items', 0) > 0 or args.get('omitted_chars', 0) > 0:
                rendered = (rendered + ' …').strip()
            lines.append(_clip(f'  参数：{rendered}'))

    return lines


class TerminalAsker:
    """Ask the user at an interactive terminal before a tool call runs (D-094)."""

    def __init__(
        self,
        reader: TerminalReader,
        wait_seconds: float,
        *,
        write: Callable[[str], object] = print,
    ) -> None:
        self._reader = reader
        self.wait_seconds = float(wait_seconds)
        self._write = write
        self._lock = asyncio.Lock()
        self._allow_all = False

    async def ask(self, event: dict, *, rule: int | None = None) -> Decision:
        if self._allow_all:
            return ALLOW
        async with self._lock:
            if self._allow_all:
                return ALLOW
            for line in format_approval_lines(event, rule=rule):
                self._write(line)
            started = time.monotonic()
            extend_deadlines(self.wait_seconds)
            suffix = f' (permission rule {rule})' if rule is not None else ''
            try:
                answer = await self._reader.read_line(
                    '允许执行吗？输入 y 允许，a 本任务都允许，其他拒绝：',
                    timeout=self.wait_seconds,
                )
            except TimeoutError:
                self._write(TIMED_OUT_NOTICE.format(seconds=self.wait_seconds))
                return Decision.deny(
                    f'the user did not answer within {self.wait_seconds:g} seconds{suffix}',
                    rule=rule,
                )
            except EOFError:
                return Decision.deny(f'the user did not allow this call{suffix}', rule=rule)
            finally:
                extend_deadlines((time.monotonic() - started) - self.wait_seconds)

            choice = answer.strip().lower()
            if choice == 'a':
                self._allow_all = True
                return ALLOW
            if choice == 'y':
                return ALLOW
            return Decision.deny(f'the user did not allow this call{suffix}', rule=rule)
