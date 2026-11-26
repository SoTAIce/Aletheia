"""Offline tests for scheduled tasks (D-102, docs/scheduled_tasks_plan.md section 3.3)."""

from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from loop_fakes import loop_reply

from app.agent.permission_rules import PermissionRules, RuleGate, parse_permission_rules
from app.agent.run_meter import current_meter, record_model_call
from app.api.__main__ import build_parser, serve
from app.api.app import _check_id, create_app
from app.api.approvals import DenyAsker
from app.api.runs import RunManager
from app.api.schedules import (
    Schedule,
    ScheduleError,
    Scheduler,
    confirm_schedules,
    format_schedule_table,
    latest_due,
    load_confirmed_fingerprints,
    load_schedules,
    next_run,
    parse_schedules,
)
from app.config import PROJECT_ROOT, Settings, config
from app.models.taskstate import TaskStatus
from app.services.rag_agent_service import RagAgentService

TOKEN = 'test-token'
AUTH = {'Authorization': f'Bearer {TOKEN}'}


def _make_schedule(**overrides) -> Schedule:
    base = dict(
        name='daily-check',
        at=time(9, 0),
        days=frozenset(range(7)),
        goal='SECRET_GOAL_TEXT_CHECK_LOGS',
        max_tokens=10000,
        workspace=None,
        max_seconds=300.0,
        enabled=True,
    )
    base.update(overrides)
    return Schedule(**base)


class _MeteredModel:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0
        self.seen_tool_names: list[list[str]] = []
        self.meters = []

    async def generate(self, messages, tools, **kwargs):
        self.calls += 1
        self.meters.append(current_meter())
        names = []
        for t in (tools or []):
            if hasattr(t, 'name'):
                names.append(t.name)
            elif isinstance(t, dict):
                names.append(t.get('function', {}).get('name') or t.get('name') or '')
        self.seen_tool_names.append(names)
        item = self._responses.pop(0)
        if callable(item):
            return await item(messages, tools)
        turn, pt, ct = item
        record_model_call(SimpleNamespace(prompt_tokens=pt, completion_tokens=ct))
        return turn


class _FakeClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now = self.now + timedelta(**kwargs)


# ---- Configuration parsing -----------------------------------------------------------------


def test_example_schedules_file_parses():
    example = PROJECT_ROOT / 'schedules.example.toml'
    schedules = load_schedules(example)
    assert len(schedules) == 2
    assert schedules[0].name == 'daily-log-summary'
    assert schedules[0].at == time(9, 0)
    assert schedules[0].days == frozenset(range(5))
    assert schedules[1].name == 'weekly-changes'
    assert schedules[1].days == frozenset({0})


def test_parse_schedules_valid_variants_and_disabled():
    data = {
        'schedule': [
            {
                'name': 'a_1-b',
                'at': '00:05',
                'days': 'daily',
                'goal': 'Check daily status',
                'max_tokens': 5000,
            },
            {
                'name': 'mon_thu',
                'at': '23:59',
                'days': ['mon', 'thu'],
                'goal': 'Check twice a week',
                'workspace': 'D:/some/dir',
                'max_tokens': 8000,
                'max_seconds': 120,
                'enabled': False,
            },
        ]
    }
    items = parse_schedules(data)
    assert len(items) == 2
    assert items[0].days == frozenset(range(7))
    assert items[0].max_seconds == 300.0
    assert items[0].enabled is True
    assert items[0].workspace is None
    assert items[1].days == frozenset({0, 3})
    assert items[1].max_seconds == 120.0
    assert items[1].enabled is False
    assert items[1].workspace == 'D:/some/dir'


@pytest.mark.parametrize(
    'payload, expected_fragment',
    [
        ({'other': []}, 'other'),
        ({'schedule': 'not-a-list'}, 'schedule'),
        ({'schedule': ['not-a-dict']}, '第 1 个'),
        ({'schedule': [{'name': 'x', 'at': '09:00', 'days': 'daily', 'goal': 'g', 'max_tokens': 10, 'extra': 1}]}, 'extra'),
        ({'schedule': [{'name': 'x', 'at': '09:00', 'days': 'daily', 'goal': 'g'}]}, 'max_tokens'),
        ({'schedule': [{'name': 'bad name!', 'at': '09:00', 'days': 'daily', 'goal': 'g', 'max_tokens': 10}]}, 'name'),
        ({'schedule': [{'name': 'x', 'at': '9:00', 'days': 'daily', 'goal': 'g', 'max_tokens': 10}]}, 'at'),
        ({'schedule': [{'name': 'x', 'at': '24:00', 'days': 'daily', 'goal': 'g', 'max_tokens': 10}]}, 'at'),
        ({'schedule': [{'name': 'x', 'at': '09:00', 'days': 'weekends', 'goal': 'g', 'max_tokens': 10}]}, 'days'),
        ({'schedule': [{'name': 'x', 'at': '09:00', 'days': [], 'goal': 'g', 'max_tokens': 10}]}, 'days'),
        ({'schedule': [{'name': 'x', 'at': '09:00', 'days': ['mon', 'mon'], 'goal': 'g', 'max_tokens': 10}]}, '重复'),
        ({'schedule': [{'name': 'x', 'at': '09:00', 'days': ['funday'], 'goal': 'g', 'max_tokens': 10}]}, 'days'),
        ({'schedule': [{'name': 'x', 'at': '09:00', 'days': 'daily', 'goal': '   ', 'max_tokens': 10}]}, 'goal'),
        ({'schedule': [{'name': 'x', 'at': '09:00', 'days': 'daily', 'goal': 'g', 'max_tokens': True}]}, 'max_tokens'),
        ({'schedule': [{'name': 'x', 'at': '09:00', 'days': 'daily', 'goal': 'g', 'max_tokens': 0}]}, 'max_tokens'),
        ({'schedule': [{'name': 'x', 'at': '09:00', 'days': 'daily', 'goal': 'g', 'max_tokens': 10, 'max_seconds': 0}]}, 'max_seconds'),
        ({'schedule': [{'name': 'x', 'at': '09:00', 'days': 'daily', 'goal': 'g', 'max_tokens': 10, 'max_seconds': 3601}]}, 'max_seconds'),
        ({'schedule': [{'name': 'x', 'at': '09:00', 'days': 'daily', 'goal': 'g', 'max_tokens': 10, 'max_seconds': True}]}, 'max_seconds'),
        ({'schedule': [{'name': 'x', 'at': '09:00', 'days': 'daily', 'goal': 'g', 'max_tokens': 10, 'enabled': 'yes'}]}, 'enabled'),
        ({'schedule': [{'name': 'x', 'at': '09:00', 'days': 'daily', 'goal': 'g', 'max_tokens': 10, 'workspace': ''}]}, 'workspace'),
        # Disabled items are still validated.
        ({'schedule': [{'name': 'x', 'at': '99:99', 'days': 'daily', 'goal': 'g', 'max_tokens': 10, 'enabled': False}]}, 'at'),
    ],
)
def test_parse_schedules_rejects_invalid_entries(payload, expected_fragment):
    with pytest.raises(ScheduleError) as exc_info:
        parse_schedules(payload)
    assert expected_fragment in str(exc_info.value)


def test_parse_schedules_rejects_duplicates_over_20_and_never_leaks_goal():
    with pytest.raises(ScheduleError, match='重复'):
        parse_schedules({
            'schedule': [
                {'name': 'dup', 'at': '09:00', 'days': 'daily', 'goal': 'a', 'max_tokens': 10},
                {'name': 'dup', 'at': '10:00', 'days': 'daily', 'goal': 'b', 'max_tokens': 10},
            ]
        })

    too_many = [
        {'name': f't{i}', 'at': '09:00', 'days': 'daily', 'goal': 'g', 'max_tokens': 10}
        for i in range(21)
    ]
    with pytest.raises(ScheduleError, match='最多 20 个'):
        parse_schedules({'schedule': too_many})

    secret = 'TOP_SECRET_GOAL_CONTENT_' + ('x' * 4001)
    with pytest.raises(ScheduleError) as exc_info:
        parse_schedules({'schedule': [
            {'name': 'long', 'at': '09:00', 'days': 'daily', 'goal': secret, 'max_tokens': 10},
        ]})
    assert 'TOP_SECRET_GOAL_CONTENT' not in str(exc_info.value)


def test_settings_schedules_missing_default_vs_explicit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    default_settings = Settings(_env_file=None)
    assert default_settings.schedules() == ()

    empty_settings = Settings(_env_file=None, schedules_file='')
    assert empty_settings.schedules() == ()

    explicit_missing = Settings(_env_file=None, schedules_file=str(tmp_path / 'missing.toml'))
    with pytest.raises(ScheduleError, match='找不到定时任务配置文件'):
        explicit_missing.schedules()


# ---- Confirmation & fingerprint ------------------------------------------------------------


def test_confirm_schedules_interactive_and_persistence(tmp_path):
    confirm_file = tmp_path / 'schedules_confirmed.json'
    s = _make_schedule(goal='Check logs ' + ('x' * 90))
    written: list[str] = []

    # Declined -> returns reason, does not save.
    refusal = confirm_schedules(
        (s,), confirm_path=confirm_file, write=written.append, ask=lambda _: 'n', interactive=True,
    )
    assert refusal is not None
    assert not confirm_file.exists()
    assert any('daily-check' in line for line in written)

    # Accepted -> returns None and saves fingerprint.
    written.clear()
    assert confirm_schedules(
        (s,), confirm_path=confirm_file, write=written.append, ask=lambda _: 'y', interactive=True,
    ) is None
    assert load_confirmed_fingerprints(confirm_file)[s.name] == s.fingerprint()

    # Second run -> already confirmed, does not prompt.
    ask_mock = AsyncMock()
    assert confirm_schedules(
        (s,), confirm_path=confirm_file, write=written.append, ask=ask_mock, interactive=True,
    ) is None
    assert ask_mock.call_count == 0

    # Changing content changes fingerprint -> requires re-confirmation.
    s_changed = _make_schedule(goal='Different goal')
    assert s_changed.fingerprint() != s.fingerprint()
    refusal2 = confirm_schedules(
        (s_changed,), confirm_path=confirm_file, write=written.append, ask=lambda _: 'n', interactive=True,
    )
    assert refusal2 is not None

    # Toggling enabled does not change fingerprint.
    s_disabled = _make_schedule(goal=s.goal, enabled=False)
    assert s_disabled.fingerprint() == s.fingerprint()


def test_confirm_schedules_non_interactive_requires_flag(tmp_path):
    confirm_file = tmp_path / 'schedules_confirmed.json'
    s = _make_schedule()

    refusal = confirm_schedules(
        (s,), confirmed=False, confirm_path=confirm_file, write=lambda _: None, interactive=False,
    )
    assert refusal is not None and '--confirm-schedules' in refusal
    assert not confirm_file.exists()

    assert confirm_schedules(
        (s,), confirmed=True, confirm_path=confirm_file, write=lambda _: None, interactive=False,
    ) is None
    assert load_confirmed_fingerprints(confirm_file)[s.name] == s.fingerprint()


# ---- Time calculation ----------------------------------------------------------------------


def test_next_run_daily_weekdays_and_list():
    # 2026-11-27 is Friday (weekday == 4).
    fri_morning = datetime(2026, 11, 27, 8, 30)
    fri_exact = datetime(2026, 11, 27, 9, 0)
    fri_late = datetime(2026, 11, 27, 9, 1)

    daily = _make_schedule(at=time(9, 0), days=frozenset(range(7)))
    assert next_run(daily, fri_morning) == datetime(2026, 11, 27, 9, 0)
    assert next_run(daily, fri_exact) == datetime(2026, 11, 28, 9, 0)
    assert next_run(daily, fri_late) == datetime(2026, 11, 28, 9, 0)

    weekdays = _make_schedule(at=time(9, 0), days=frozenset(range(5)))
    assert next_run(weekdays, fri_morning) == datetime(2026, 11, 27, 9, 0)
    # Cross-weekend: Friday after 09:00 -> Monday 2026-11-30 09:00.
    assert next_run(weekdays, fri_exact) == datetime(2026, 11, 30, 9, 0)

    mon_thu = _make_schedule(at=time(10, 0), days=frozenset({0, 3}))
    # Thursday 2026-11-26 10:00 -> next is Monday 2026-11-30 10:00.
    assert next_run(mon_thu, datetime(2026, 11, 26, 10, 0)) == datetime(2026, 11, 30, 10, 0)
    # Monday 2026-11-30 10:00 -> next is Thursday 2026-12-03 10:00.
    assert next_run(mon_thu, datetime(2026, 11, 30, 10, 0)) == datetime(2026, 12, 3, 10, 0)


def test_latest_due_window():
    daily = _make_schedule(at=time(9, 0), days=frozenset(range(7)))
    assert latest_due(daily, datetime(2026, 11, 27, 9, 0)) == datetime(2026, 11, 27, 9, 0)
    assert latest_due(daily, datetime(2026, 11, 27, 20, 59)) == datetime(2026, 11, 27, 9, 0)
    assert latest_due(daily, datetime(2026, 11, 27, 21, 0)) == datetime(2026, 11, 27, 9, 0)
    assert latest_due(daily, datetime(2026, 11, 27, 21, 0, 1)) is None


# ---- DenyAsker & RuleGate ------------------------------------------------------------------


async def test_deny_asker_denies_immediately_without_waiting():
    rules = parse_permission_rules({
        'rule': [{'tool': 'read_file', 'action': 'ask'}],
    })
    gate = RuleGate(
        rules,
        asker=DenyAsker(),
        default_ask_tools=frozenset({'write_file'}),
        ask_external=True,
    )
    # Rule-based ask -> immediately denied.
    d1 = await asyncio.wait_for(gate.check({'tool': 'read_file', 'effect': 'read'}), timeout=1.0)
    assert not d1.allow and d1.rule == 1
    # Default ask tool -> immediately denied.
    d2 = await asyncio.wait_for(gate.check({'tool': 'write_file', 'effect': 'write'}), timeout=1.0)
    assert not d2.allow and d2.rule is None
    # External tool -> immediately denied.
    d3 = await asyncio.wait_for(gate.check({'tool': 'ext__call', 'effect': 'external'}), timeout=1.0)
    assert not d3.allow and d3.rule is None


# ---- RunManager.start_scheduled options, IDs, budget & replay ------------------------------


async def test_start_scheduled_passes_strict_options_and_valid_ids(tmp_path):
    captured = {}

    class _SpyService:
        permission_rules = None
        user_mcp_servers = ()

        async def query_result(self, question, session_id, **kwargs):
            captured['question'] = question
            captured['session_id'] = session_id
            captured['kwargs'] = kwargs
            captured['meter'] = current_meter()
            return SimpleNamespace(
                status=TaskStatus.COMPLETED, task_id='t1', content='ok',
                usage={'total_tokens': 1}, failure_kind=None, user_message=None,
            )

    runs = RunManager(_SpyService(), user_id='local', max_concurrent=2, retention_seconds=60.0)
    sched = _make_schedule(
        name='log_scan-1',
        goal='SECRET_DO_NOT_LEAK',
        workspace=str(tmp_path),
        max_tokens=1234,
        max_seconds=45.0,
    )
    planned = datetime(2026, 11, 27, 9, 0)
    run, created = runs.start_scheduled(sched, planned)
    assert created is True
    await run.task

    # IDs conform to _check_id rules.
    _check_id('session_id', run.session_id)
    _check_id('request_id', run.run_id)
    assert run.session_id == 'schedule-log_scan-1'
    assert run.run_id == 'schedule-log_scan-1-202611270900'

    # Accepted event has schedule metadata and no goal text.
    accepted = run.events[0]
    assert accepted['type'] == 'accepted'
    assert accepted['schedule'] == {'name': 'log_scan-1', 'planned_at': planned.isoformat()}
    assert 'SECRET_DO_NOT_LEAK' not in str(accepted)

    # Strict options passed to query_result.
    kw = captured['kwargs']
    assert kw['execution_mode'] == 'single'
    assert kw['allow_writes'] is False
    assert kw['use_memory'] is False
    assert kw['allow_ask_user'] is False
    assert kw['workspace'] == str(tmp_path)
    assert kw['workspace_confirmed'] is True
    assert kw['hooks'] is not None
    assert len(kw['hooks'].gates) == 1
    assert isinstance(kw['hooks'].gates[0]._asker, DenyAsker)
    assert captured['meter'].max_total_tokens == 1234
    assert captured['meter'].timeout_seconds == 45.0


async def test_scheduled_run_readonly_no_ask_user_and_budget_enforced(tmp_path):
    from app.agent.step_models import ModelTurn, ToolCall
    # Turn 1 uses 120 tokens (> 100 budget) and requests a tool; before turn 2 budget fails.
    call_turn = ModelTurn(
        content='',
        tool_calls=(ToolCall('c1', 'nonexistent_tool', {}),),
    )
    model = _MeteredModel([
        (call_turn, 80, 40),
        (loop_reply('never'), 10, 5),
    ])
    async with RagAgentService(
        model=model, mcp_servers=(), include_knowledge=False, memory_directory=tmp_path,
    ) as service:
        runs = RunManager(service, user_id='local', max_concurrent=2, retention_seconds=60.0)
        sched = _make_schedule(max_tokens=100, max_seconds=60.0)
        run, _ = runs.start_scheduled(sched, datetime(2026, 11, 27, 9, 0))
        await run.task
        assert model.calls == 1
        assert 'ask_user' not in model.seen_tool_names[0]
        assert 'write_file' not in model.seen_tool_names[0]
        assert run.status == 'failed'
        assert run.events[-1]['failure_kind'] == 'budget'


async def test_scheduled_run_archive_replay_after_restart(tmp_path):
    planned = datetime(2026, 11, 27, 9, 0)
    sched = _make_schedule(name='daily-replay', goal='Summarize logs', max_tokens=5000)

    # First process run.
    model1 = _MeteredModel([(loop_reply('All clear.'), 50, 10)])
    async with RagAgentService(
        model=model1, mcp_servers=(), include_knowledge=False, memory_directory=tmp_path,
    ) as svc1:
        runs1 = RunManager(svc1, user_id='local', max_concurrent=2, retention_seconds=60.0)
        run1, created1 = runs1.start_scheduled(sched, planned)
        assert created1 is True
        await run1.task
        assert run1.status == 'completed'
        assert model1.calls == 1

    # Simulated restart: new service & new RunManager sharing the same archive directory.
    model2 = _MeteredModel([(loop_reply('Should not be called'), 50, 10)])
    async with RagAgentService(
        model=model2, mcp_servers=(), include_knowledge=False, memory_directory=tmp_path,
    ) as svc2:
        runs2 = RunManager(svc2, user_id='local', max_concurrent=2, retention_seconds=60.0)
        run2, created2 = runs2.start_scheduled(sched, planned)
        assert created2 is True
        await run2.task
        assert run2.status == 'completed'
        assert run2.events[-1]['content'] == 'All clear.'
        assert model2.calls == 0


# ---- Scheduler loop: trigger, catch-up, sleep wakeup, retries, aclose ----------------------


async def test_scheduler_triggers_on_time_and_updates_status(tmp_path):
    clock = _FakeClock(datetime(2026, 11, 27, 8, 59, 30))
    model = _MeteredModel([(loop_reply('Done'), 20, 5)])
    stop_loop = asyncio.Event()

    async def fake_sleep(seconds):
        if clock.now < datetime(2026, 11, 27, 9, 0):
            clock.advance(seconds=seconds)
            await asyncio.sleep(0)
        else:
            await stop_loop.wait()

    async with RagAgentService(
        model=model, mcp_servers=(), include_knowledge=False, memory_directory=tmp_path,
    ) as service:
        runs = RunManager(service, user_id='local', max_concurrent=2, retention_seconds=60.0)
        sched = _make_schedule(at=time(9, 0), days=frozenset(range(7)))
        scheduler = Scheduler(runs, (sched,), clock=clock, sleep=fake_sleep)
        scheduler.start()
        try:
            for _ in range(100):
                if scheduler.status()[0]['last_status'] == 'completed':
                    break
                await asyncio.sleep(0.01)
            assert scheduler.status()[0]['last_status'] == 'completed'
            assert model.calls == 1
        finally:
            stop_loop.set()
            await scheduler.aclose()


async def test_scheduler_catch_up_within_12h_and_skip_older(tmp_path):
    # Startup at 15:00 -> 09:00 is 6h ago (within 12h) -> catches up once.
    # Second task at 02:00 is 13h ago (> 12h) -> skipped.
    clock = _FakeClock(datetime(2026, 11, 27, 15, 0))
    dispatched: list[tuple[str, datetime]] = []

    class _StubRuns:
        def start_scheduled(self, schedule, planned_at):
            dispatched.append((schedule.name, planned_at))
            run = SimpleNamespace(status='completed', finished_at=1.0, _changed=asyncio.Event())
            return run, True

    gate = asyncio.Event()

    async def pause_sleep(_seconds):
        await gate.wait()

    s_recent = _make_schedule(name='recent', at=time(9, 0))
    s_old = _make_schedule(name='old', at=time(2, 0))
    scheduler = Scheduler(_StubRuns(), (s_recent, s_old), clock=clock, sleep=pause_sleep)
    scheduler.start()
    try:
        for _ in range(20):
            if dispatched and scheduler.status()[0]['last_status'] == 'completed':
                break
            await asyncio.sleep(0)
        assert dispatched == [('recent', datetime(2026, 11, 27, 9, 0))]
        assert scheduler.status()[0]['last_status'] == 'completed'
        assert scheduler.status()[1]['last_status'] == 'never'
    finally:
        gate.set()
        await scheduler.aclose()


async def test_scheduler_wakeup_after_sleep_skips_too_old_and_runs_within_window():
    # Start at 08:50 (no catch-up). Next is 09:00.
    # Case 1: Wake up at 22:00 (13h after 09:00) -> skip 09:00.
    clock = _FakeClock(datetime(2026, 11, 27, 8, 50))
    dispatched: list[datetime] = []

    class _StubRuns:
        def start_scheduled(self, schedule, planned_at):
            dispatched.append(planned_at)
            return SimpleNamespace(status='completed', finished_at=1.0, _changed=asyncio.Event()), True

    sleep_calls = 0
    done_event = asyncio.Event()

    async def jump_sleep(_seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls == 1:
            clock.now = datetime(2026, 11, 27, 22, 0)   # 13h late -> skip
        elif sleep_calls == 2:
            clock.now = datetime(2026, 11, 28, 12, 0)   # 3h late for next day's 09:00 -> run!
        else:
            await done_event.wait()
        await asyncio.sleep(0)

    sched = _make_schedule(name='wake', at=time(9, 0), days=frozenset(range(7)))
    scheduler = Scheduler(_StubRuns(), (sched,), clock=clock, sleep=jump_sleep)
    scheduler.start()
    try:
        for _ in range(50):
            if dispatched:
                break
            await asyncio.sleep(0)
        assert dispatched == [datetime(2026, 11, 28, 9, 0)]
    finally:
        done_event.set()
        await scheduler.aclose()


async def test_scheduler_retries_when_busy_and_marks_skipped_after_max_attempts():
    clock = _FakeClock(datetime(2026, 11, 27, 9, 0))
    attempts = 0
    retry_sleeps: list[float] = []
    block_loop = asyncio.Event()

    class _BusyRuns:
        def start_scheduled(self, schedule, planned_at):
            nonlocal attempts
            attempts += 1
            from app.api.runs import ApiError
            raise ApiError(429, 'too_many_runs', 'busy')

    async def fake_sleep(seconds):
        if seconds == 7.0:
            retry_sleeps.append(seconds)
            await asyncio.sleep(0)
        else:
            await block_loop.wait()

    sched = _make_schedule(name='busy-task', at=time(9, 0))
    scheduler = Scheduler(
        _BusyRuns(), (sched,), clock=clock, sleep=fake_sleep, retry_seconds=7.0, max_attempts=10,
    )
    scheduler.start()
    try:
        for _ in range(100):
            if scheduler.status()[0]['last_status'] == 'skipped':
                break
            await asyncio.sleep(0)
        assert attempts == 10
        assert len(retry_sleeps) == 9
        assert scheduler.status()[0]['last_status'] == 'skipped'
    finally:
        block_loop.set()
        await scheduler.aclose()


async def test_scheduler_aclose_stops_loop_and_pending_retries():
    clock = _FakeClock(datetime(2026, 11, 27, 9, 0))
    retry_started = asyncio.Event()

    class _BusyRuns:
        def start_scheduled(self, schedule, planned_at):
            from app.api.runs import ApiError
            raise ApiError(409, 'session_busy', 'busy')

    async def slow_sleep(seconds):
        if seconds == 12.0:
            retry_started.set()
        await asyncio.sleep(3600)

    sched = _make_schedule(name='cancel-me', at=time(9, 0))
    scheduler = Scheduler(
        _BusyRuns(), (sched,), clock=clock, sleep=slow_sleep, retry_seconds=12.0, max_attempts=10,
    )
    scheduler.start()
    await asyncio.wait_for(retry_started.wait(), timeout=1.0)
    await asyncio.wait_for(scheduler.aclose(), timeout=1.0)
    assert scheduler._loop_task is None
    assert not scheduler._bg_tasks


# ---- FastAPI /api/health & lifespan --------------------------------------------------------


async def test_health_with_and_without_schedules_never_leaks_goal(tmp_path):
    async with RagAgentService(
        model=AsyncMock(), mcp_servers=(), include_knowledge=False, memory_directory=tmp_path,
    ) as service:
        # Without schedules -> empty list.
        app_empty = create_app(service, user_id='local', access_token=TOKEN)
        async with app_empty.router.lifespan_context(app_empty):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_empty), base_url='http://test', headers=AUTH,
            ) as client:
                health = (await client.get('/api/health')).json()
                assert health['schedules'] == []

        # With schedules -> includes name/next_run/last_status, no goal text.
        clock = _FakeClock(datetime(2026, 11, 27, 8, 0))
        sched = _make_schedule(name='morning', at=time(9, 0), goal='ULTRA_SECRET_GOAL_TEXT')
        app_sched = create_app(
            service, user_id='local', access_token=TOKEN, schedules=(sched,), schedule_clock=clock,
        )
        async with app_sched.router.lifespan_context(app_sched):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_sched), base_url='http://test', headers=AUTH,
            ) as client:
                resp = await client.get('/api/health')
                assert 'ULTRA_SECRET_GOAL_TEXT' not in resp.text
                data = resp.json()['schedules']
                assert data == [{
                    'name': 'morning',
                    'next_run': datetime(2026, 11, 27, 9, 0).isoformat(),
                    'last_status': 'never',
                }]
        # After lifespan exits, scheduler is closed.
        assert app_sched.state.scheduler._loop_task is None


def test_format_schedule_table_excludes_goal():
    sched = _make_schedule(name='morning', at=time(9, 0), goal='SECRET_GOAL_NEVER_PRINT')
    table = format_schedule_table((sched,), now=datetime(2026, 11, 27, 8, 0))
    assert 'morning' in table
    assert '09:00' in table
    assert '2026-11-27 09:00' in table
    assert '10000' in table
    assert 'SECRET_GOAL_NEVER_PRINT' not in table


# ---- CLI startup validation (app/api/__main__.py) ------------------------------------------


async def test_serve_rejects_bad_schedules_missing_archive_bad_workspace_and_unconfirmed(tmp_path, monkeypatch, capsys):
    # Restore __pydantic_fields_set__ last on teardown so monkeypatch.setattr(config, ...)
    # does not leave default file fields marked as explicitly set for later tests.
    monkeypatch.setattr(config, '__pydantic_fields_set__', set(config.model_fields_set))
    monkeypatch.setattr(config, 'web_access_token', 'tok')
    monkeypatch.setattr(config, 'web_allow_no_token', False)
    monkeypatch.setattr(config, 'longterm_memory_dsn', '')
    monkeypatch.setattr(config, 'permissions_file', '')
    monkeypatch.setattr(config, 'mcp_servers_file', '')
    monkeypatch.setattr(config, 'model_profiles_file', '')

    args = build_parser().parse_args(['--no-knowledge'])

    # 1. Invalid schedules file -> exit 2.
    bad_file = tmp_path / 'bad.toml'
    bad_file.write_text('[[schedule]]\nname = "a"\n', encoding='utf-8')
    monkeypatch.setattr(config, 'schedules_file', str(bad_file))
    config.model_fields_set.add('schedules_file')
    assert await serve(args) == 2
    assert 'ScheduleError:' in capsys.readouterr().out

    # 2. Valid schedule, but CONVERSATION_STORAGE_DIR is empty -> exit 2.
    good_file = tmp_path / 'good.toml'
    good_file.write_text(
        '[[schedule]]\nname = "ok"\nat = "09:00"\ndays = "daily"\ngoal = "check"\nmax_tokens = 1000\n',
        encoding='utf-8',
    )
    monkeypatch.setattr(config, 'schedules_file', str(good_file))
    monkeypatch.setattr(config, 'conversation_storage_dir', '')
    assert await serve(args) == 2
    assert 'CONVERSATION_STORAGE_DIR' in capsys.readouterr().out

    # 3. Valid schedule with nonexistent workspace -> exit 2.
    ws_file = tmp_path / 'bad_ws.toml'
    ws_file.write_text(
        '[[schedule]]\nname = "ok"\nat = "09:00"\ndays = "daily"\ngoal = "check"\n'
        f'workspace = "{(tmp_path / "does_not_exist").as_posix()}"\nmax_tokens = 1000\n',
        encoding='utf-8',
    )
    monkeypatch.setattr(config, 'schedules_file', str(ws_file))
    monkeypatch.setattr(config, 'conversation_storage_dir', str(tmp_path / 'conv'))
    assert await serve(args) == 2
    assert '工作目录不合格' in capsys.readouterr().out

    # 3b. A workspace while the page offers no workspace choice -> exit 2 (the service would refuse it).
    monkeypatch.setattr(config, 'workspace_choice', False)
    assert await serve(args) == 2
    assert '没有开启工作目录选择' in capsys.readouterr().out
    monkeypatch.setattr(config, 'workspace_choice', True)

    # 4. Valid schedule, non-interactive without --confirm-schedules -> exit 2.
    monkeypatch.setattr(config, 'schedules_file', str(good_file))
    monkeypatch.setattr('app.api.schedules.DEFAULT_CONFIRM_PATH', tmp_path / 'confirm.json')
    monkeypatch.setattr('sys.stdin', SimpleNamespace(isatty=lambda: False))
    assert await serve(args) == 2
    assert '--confirm-schedules' in capsys.readouterr().out
