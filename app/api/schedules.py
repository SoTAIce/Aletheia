"""Scheduled read-only tasks for the web backend (D-102, docs/scheduled_tasks_plan.md).

Configuration is loaded from SCHEDULES_FILE (default schedules.toml) at startup, confirmed once
per task fingerprint (persisted in data/schedules_confirmed.json), and run on an asyncio loop
attached to the FastAPI lifespan. Goal text never appears in errors, logs or health responses.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, time, timedelta
import hashlib
import json
import logging
import math
from pathlib import Path
import re
import sys
import tomllib
from typing import Any

from app.api.runs import ApiError
from app.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

MAX_SCHEDULES = 20
MAX_GOAL_CHARS = 4000
DEFAULT_MAX_SECONDS = 300.0
MIN_MAX_SECONDS = 1.0
MAX_MAX_SECONDS = 3600.0
DEFAULT_CATCH_UP_SECONDS = 12 * 3600
DEFAULT_RETRY_SECONDS = 60.0
DEFAULT_MAX_ATTEMPTS = 10
MAX_SLEEP_SLICE_SECONDS = 60.0

DEFAULT_CONFIRM_PATH = PROJECT_ROOT / 'data' / 'schedules_confirmed.json'

_NAME_RE = re.compile(r'^[A-Za-z0-9_-]{1,40}$')
_AT_RE = re.compile(r'^([01]\d|2[0-3]):([0-5]\d)$')
_DAY_MAP = {'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3, 'fri': 4, 'sat': 5, 'sun': 6}
_DAY_LABELS = ('周一', '周二', '周三', '周四', '周五', '周六', '周日')
_SCHEDULE_KEYS = frozenset({
    'name', 'at', 'days', 'goal', 'workspace', 'max_tokens', 'max_seconds', 'enabled',
})
_REQUIRED_KEYS = ('name', 'at', 'days', 'goal', 'max_tokens')


class ScheduleError(ValueError):
    """A schedule configuration or startup check error; Chinese message, never goal text."""


@dataclass(frozen=True, slots=True)
class Schedule:
    name: str
    at: time
    days: frozenset[int]
    goal: str
    max_tokens: int
    workspace: str | None = None
    max_seconds: float = DEFAULT_MAX_SECONDS
    enabled: bool = True

    def fingerprint(self) -> str:
        """SHA-256 of canonical JSON over all fields except enabled."""
        payload = {
            'name': self.name,
            'at': self.at.strftime('%H:%M'),
            'days': sorted(self.days),
            'goal': self.goal,
            'workspace': self.workspace,
            'max_tokens': self.max_tokens,
            'max_seconds': float(self.max_seconds),
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def _parse_at(raw: Any, *, idx: int) -> time:
    if not isinstance(raw, str):
        raise ScheduleError(f'第 {idx} 个定时任务的 at 必须是 "HH:MM" 字符串')
    match = _AT_RE.fullmatch(raw)
    if not match:
        raise ScheduleError(f'第 {idx} 个定时任务的 at 格式不对（应为 "HH:MM"，00:00～23:59）')
    return time(int(match.group(1)), int(match.group(2)))


def _parse_days(raw: Any, *, idx: int) -> frozenset[int]:
    if isinstance(raw, str):
        if raw == 'daily':
            return frozenset(range(7))
        if raw == 'weekdays':
            return frozenset(range(5))
        raise ScheduleError(
            f'第 {idx} 个定时任务的 days 取值不对（字符串只认 "daily" 或 "weekdays"）'
        )
    if isinstance(raw, list):
        if not raw:
            raise ScheduleError(f'第 {idx} 个定时任务的 days 列表不能为空')
        seen: set[int] = set()
        for item in raw:
            if not isinstance(item, str) or item not in _DAY_MAP:
                raise ScheduleError(
                    f'第 {idx} 个定时任务的 days 列表只认 mon/tue/wed/thu/fri/sat/sun'
                )
            day = _DAY_MAP[item]
            if day in seen:
                raise ScheduleError(f'第 {idx} 个定时任务的 days 列表包含重复的星期 {item}')
            seen.add(day)
        return frozenset(seen)
    raise ScheduleError(
        f'第 {idx} 个定时任务的 days 必须是 "daily"、"weekdays" 或星期列表'
    )


def parse_schedules(data: dict) -> tuple[Schedule, ...]:
    """Validate a parsed TOML mapping and return a tuple of Schedule objects."""
    if not isinstance(data, dict):
        raise ScheduleError('定时任务配置文件内容必须是表结构')
    unknown_top = set(data) - {'schedule'}
    if unknown_top:
        raise ScheduleError(
            f'定时任务配置文件里有不认识的段：{", ".join(sorted(unknown_top))}（只认 [[schedule]]）'
        )
    raw_items = data.get('schedule', [])
    if not isinstance(raw_items, list):
        raise ScheduleError('定时任务配置文件的 schedule 必须是数组表（[[schedule]]）')
    if len(raw_items) > MAX_SCHEDULES:
        raise ScheduleError(f'定时任务最多 {MAX_SCHEDULES} 个（当前 {len(raw_items)} 个）')

    schedules: list[Schedule] = []
    seen_names: set[str] = set()
    for idx, body in enumerate(raw_items, start=1):
        if not isinstance(body, dict):
            raise ScheduleError(f'第 {idx} 个定时任务必须是表结构（[[schedule]]）')
        extra = set(body) - _SCHEDULE_KEYS
        if extra:
            raise ScheduleError(f'第 {idx} 个定时任务有不认识的字段：{", ".join(sorted(extra))}')
        for key in _REQUIRED_KEYS:
            if key not in body:
                raise ScheduleError(f'第 {idx} 个定时任务缺少必填字段 {key}')

        name = body['name']
        if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
            raise ScheduleError(
                f'第 {idx} 个定时任务的 name 不合法（只含字母、数字、-、_，长度 1～40）'
            )
        if name in seen_names:
            raise ScheduleError(f'第 {idx} 个定时任务的 name 重复：{name}')
        seen_names.add(name)

        at = _parse_at(body['at'], idx=idx)
        days = _parse_days(body['days'], idx=idx)

        goal = body['goal']
        if not isinstance(goal, str) or not goal.strip() or '\x00' in goal:
            raise ScheduleError(f'第 {idx} 个定时任务的 goal 必须是非空字符串')
        if len(goal) > MAX_GOAL_CHARS:
            raise ScheduleError(f'第 {idx} 个定时任务的 goal 超过 {MAX_GOAL_CHARS} 字符上限')

        workspace: str | None = None
        if 'workspace' in body:
            raw_ws = body['workspace']
            if not isinstance(raw_ws, str) or not raw_ws.strip() or '\x00' in raw_ws:
                raise ScheduleError(f'第 {idx} 个定时任务的 workspace 必须是非空路径字符串')
            workspace = raw_ws.strip()

        max_tokens = body['max_tokens']
        if type(max_tokens) is not int or max_tokens < 1:
            raise ScheduleError(f'第 {idx} 个定时任务的 max_tokens 必须是正整数')

        raw_seconds = body.get('max_seconds', DEFAULT_MAX_SECONDS)
        if (
            type(raw_seconds) not in (int, float)
            or not math.isfinite(raw_seconds)
            or not (MIN_MAX_SECONDS <= raw_seconds <= MAX_MAX_SECONDS)
        ):
            raise ScheduleError(
                f'第 {idx} 个定时任务的 max_seconds 必须在 {int(MIN_MAX_SECONDS)}～{int(MAX_MAX_SECONDS)} 之间'
            )
        max_seconds = float(raw_seconds)

        enabled = body.get('enabled', True)
        if type(enabled) is not bool:
            raise ScheduleError(f'第 {idx} 个定时任务的 enabled 必须是布尔值')

        schedules.append(Schedule(
            name=name,
            at=at,
            days=days,
            goal=goal,
            max_tokens=max_tokens,
            workspace=workspace,
            max_seconds=max_seconds,
            enabled=enabled,
        ))
    return tuple(schedules)


def load_schedules(path: str | Path) -> tuple[Schedule, ...]:
    """Read and parse a schedules TOML file; errors are Chinese ScheduleError."""
    source = Path(path).resolve()
    try:
        data = tomllib.loads(source.read_text(encoding='utf-8'))
    except FileNotFoundError:
        raise ScheduleError(f'找不到定时任务配置文件 {source}') from None
    except (OSError, UnicodeDecodeError) as exc:
        raise ScheduleError(f'读不了定时任务配置文件 {source}（{type(exc).__name__}）') from None
    except tomllib.TOMLDecodeError:
        raise ScheduleError(f'定时任务配置文件 {source} 不是合法的 TOML') from None
    return parse_schedules(data)


def next_run(schedule: Schedule, after: datetime) -> datetime:
    """Earliest scheduled local naive datetime strictly later than after."""
    base_date = after.date()
    for offset in range(8):
        day = base_date + timedelta(days=offset)
        if day.weekday() in schedule.days:
            candidate = datetime.combine(day, schedule.at)
            if candidate > after:
                return candidate
    raise ScheduleError(f'定时任务 {schedule.name} 没有可调度的星期')


def latest_due(
    schedule: Schedule,
    now: datetime,
    window_seconds: float = DEFAULT_CATCH_UP_SECONDS,
) -> datetime | None:
    """Most recent scheduled datetime <= now if within window_seconds, else None."""
    base_date = now.date()
    for offset in range(8):
        day = base_date - timedelta(days=offset)
        if day.weekday() in schedule.days:
            candidate = datetime.combine(day, schedule.at)
            if candidate <= now:
                if (now - candidate).total_seconds() <= window_seconds:
                    return candidate
                return None
    return None


def format_days(days: frozenset[int]) -> str:
    if days == frozenset(range(7)):
        return '每天'
    if days == frozenset(range(5)):
        return '工作日'
    return '、'.join(_DAY_LABELS[d] for d in sorted(days))


def format_schedule_table(schedules, *, now: datetime | None = None) -> str:
    """Chinese startup summary table for enabled schedules; never includes goal text."""
    enabled = [s for s in schedules if s.enabled]
    if not enabled:
        return ''
    current = datetime.now() if now is None else now
    lines = [f'定时任务（已启用 {len(enabled)} 个）：']
    for s in enabled:
        nxt = next_run(s, current).strftime('%Y-%m-%d %H:%M')
        lines.append(
            f'  - {s.name}｜{s.at.strftime("%H:%M")}（{format_days(s.days)}）｜'
            f'下次：{nxt}｜上限：{s.max_tokens} tokens / {s.max_seconds:g}s'
        )
    return '\n'.join(lines)


def load_confirmed_fingerprints(confirm_path: str | Path | None = None) -> dict[str, str]:
    """Read stored {schedule_name: fingerprint} records; missing or corrupt files return {}."""
    target = DEFAULT_CONFIRM_PATH if confirm_path is None else Path(confirm_path)
    try:
        raw = json.loads(target.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}


def _save_confirmed_fingerprints(records: dict[str, str], confirm_path: Path) -> None:
    confirm_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = confirm_path.with_name(confirm_path.name + '.tmp')
    tmp.write_text(json.dumps(records, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    tmp.replace(confirm_path)


def _format_schedule_summary(schedule: Schedule) -> str:
    preview = ' '.join(schedule.goal.split())
    if len(preview) > 80:
        preview = preview[:80] + '…'
    lines = [
        f'即将启用定时任务：{schedule.name}',
        f'  时间：{schedule.at.strftime("%H:%M")}（{format_days(schedule.days)}）',
        f'  目标：{preview}',
        f'  目录：{schedule.workspace or "（默认目录）"}',
        f'  预算：max_tokens={schedule.max_tokens}，max_seconds={schedule.max_seconds:g}',
    ]
    return '\n'.join(lines)


def confirm_schedules(
    schedules,
    *,
    confirmed: bool = False,
    confirm_path: str | Path | None = None,
    write=print,
    ask=input,
    interactive: bool | None = None,
) -> str | None:
    """Confirm each enabled schedule whose fingerprint is not yet stored (D-102).

    Returns a Chinese refusal reason, or None when every enabled schedule is confirmed.
    Confirmed fingerprints are saved to confirm_path (default data/schedules_confirmed.json).
    """
    enabled = [s for s in schedules if s.enabled]
    if not enabled:
        return None
    target = DEFAULT_CONFIRM_PATH if confirm_path is None else Path(confirm_path)
    stored = load_confirmed_fingerprints(target)
    pending = [s for s in enabled if stored.get(s.name) != s.fingerprint()]
    if not pending:
        return None

    is_interactive = sys.stdin.isatty() if interactive is None else interactive
    updated = dict(stored)
    for schedule in pending:
        write(_format_schedule_summary(schedule))
        if not confirmed:
            if not is_interactive:
                return '有未确认的定时任务，不能交互确认；确认要启动时加 --confirm-schedules。'
            try:
                answer = ask(f'允许启用定时任务 {schedule.name} 吗？输入 y 确认，其他任意输入取消：')
            except EOFError:
                answer = ''
            if answer.strip().lower() != 'y':
                return f'已取消：没有确认定时任务 {schedule.name}。'
        updated[schedule.name] = schedule.fingerprint()

    _save_confirmed_fingerprints(updated, target)
    return None


class Scheduler:
    """Asyncio loop that dispatches enabled schedules through RunManager.start_scheduled."""

    def __init__(
        self,
        runs,
        schedules,
        *,
        clock=datetime.now,
        sleep=asyncio.sleep,
        retry_seconds: float = DEFAULT_RETRY_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        catch_up_seconds: float = DEFAULT_CATCH_UP_SECONDS,
    ) -> None:
        self._runs = runs
        self._schedules = tuple(s for s in schedules if s.enabled)
        self._clock = clock
        self._sleep = sleep
        self._retry_seconds = float(retry_seconds)
        self._max_attempts = int(max_attempts)
        self._catch_up_seconds = float(catch_up_seconds)
        now = self._clock()
        self._next_at: dict[str, datetime] = {s.name: next_run(s, now) for s in self._schedules}
        self._last_status: dict[str, str] = {s.name: 'never' for s in self._schedules}
        self._loop_task: asyncio.Task | None = None
        self._bg_tasks: set[asyncio.Task] = set()
        self._closing = False

    def start(self) -> None:
        if not self._schedules or self._loop_task is not None or self._closing:
            return
        now = self._clock()
        for s in self._schedules:
            self._next_at[s.name] = next_run(s, now)
        self._loop_task = asyncio.create_task(self._run_loop(now), name='schedule-loop')

    def status(self) -> list[dict]:
        """Public status for /api/health; never contains goal text."""
        return [
            {
                'name': s.name,
                'next_run': self._next_at[s.name].isoformat(),
                'last_status': self._last_status[s.name],
            }
            for s in self._schedules
        ]

    def _track_task(self, coro, *, name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    def _dispatch(self, schedule: Schedule, planned_at: datetime) -> None:
        self._track_task(
            self._submit_and_watch(schedule, planned_at),
            name=f'schedule-dispatch-{schedule.name}-{planned_at:%Y%m%d%H%M}',
        )

    async def _submit_and_watch(self, schedule: Schedule, planned_at: datetime) -> None:
        run = None
        for attempt in range(1, self._max_attempts + 1):
            if self._closing:
                return
            try:
                run, _created = self._runs.start_scheduled(schedule, planned_at)
                break
            except ApiError as exc:
                if exc.code == 'shutting_down':
                    return
                if exc.code in ('session_busy', 'too_many_runs'):
                    if attempt < self._max_attempts:
                        await self._sleep(self._retry_seconds)
                        continue
                    self._last_status[schedule.name] = 'skipped'
                    logger.warning(
                        'Scheduled task %s skipped for %s (%s)',
                        schedule.name,
                        planned_at.strftime('%Y-%m-%d %H:%M'),
                        exc.code,
                    )
                    return
                self._last_status[schedule.name] = 'failed'
                logger.warning(
                    'Scheduled task %s rejected for %s (%s)',
                    schedule.name,
                    planned_at.strftime('%Y-%m-%d %H:%M'),
                    exc.code,
                )
                return
        if run is None:
            return
        while run.finished_at is None:
            changed = run._changed
            if run.finished_at is not None:
                break
            await changed.wait()
        self._last_status[schedule.name] = (
            run.status if run.status in ('completed', 'failed', 'cancelled') else 'failed'
        )

    async def _run_loop(self, initial_now: datetime) -> None:
        try:
            for s in self._schedules:
                due = latest_due(s, initial_now, window_seconds=self._catch_up_seconds)
                if due is not None:
                    self._dispatch(s, due)

            while not self._closing:
                now = self._clock()
                for s in self._schedules:
                    planned = self._next_at[s.name]
                    if planned <= now:
                        due = latest_due(s, now, window_seconds=self._catch_up_seconds)
                        if due is not None and due >= planned:
                            self._dispatch(s, due)
                        self._next_at[s.name] = next_run(s, now)
                earliest = min(self._next_at.values())
                delay = (earliest - now).total_seconds()
                wait = min(max(delay, 0.0), MAX_SLEEP_SLICE_SECONDS)
                await self._sleep(wait)
        except asyncio.CancelledError:
            return

    async def aclose(self) -> None:
        """Stop the loop and pending retries; in-flight runs finish via RunManager.aclose."""
        self._closing = True
        tasks = [t for t in self._bg_tasks if not t.done()]
        if self._loop_task is not None and not self._loop_task.done():
            tasks.append(self._loop_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._loop_task = None
        self._bg_tasks.clear()
