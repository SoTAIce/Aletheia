"""Blocked task TTL expiry: in-memory eviction, active-resume safety, and web error (D-101)."""

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from loop_fakes import loop_reply

from app.agent.run_limits import RunLimits
from app.agent.step_models import ModelTurn, ToolCall
from app.api.app import create_app
from app.config import Settings
from app.models.taskstate import TaskStatus
from app.services.rag_agent_service import RagAgentService

TOKEN = 'test-token'
AUTH = {'Authorization': f'Bearer {TOKEN}'}


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _WebClient:
    def __init__(self, service, **options):
        self.service = service
        self.app = create_app(service, user_id='local', access_token=TOKEN, **options)

    async def __aenter__(self):
        await self.service.__aenter__()
        self._lifespan = self.app.router.lifespan_context(self.app)
        await self._lifespan.__aenter__()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url='http://test', headers=AUTH,
        )
        return self

    async def __aexit__(self, *exc):
        await self.client.aclose()
        await self._lifespan.__aexit__(*exc)
        await self.service.__aexit__(*exc)

    async def send(self, session, content, request_id, **fields):
        return await self.client.post(
            f'/api/sessions/{session}/messages',
            json={'content': content, 'request_id': request_id, **fields},
        )

    async def events(self, run_id):
        response = await self.client.get(f'/api/runs/{run_id}/events')
        return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]


def _service(tmp_path, turns, *, ttl=100.0, clock=None):
    model = AsyncMock()
    model.generate.side_effect = list(turns)
    return RagAgentService(
        model=model,
        mcp_servers=(),
        include_knowledge=False,
        memory_directory=tmp_path,
        blocked_task_ttl_seconds=ttl,
        clock=clock or FakeClock(),
    )


async def test_blocked_task_is_resumable_within_ttl(tmp_path):
    clock = FakeClock(1000.0)
    turns = [loop_reply('need info', 'blocked', 'Which file?'), loop_reply('Done')]
    async with _service(tmp_path, turns, ttl=100.0, clock=clock) as service:
        first = await service.query_result('Fix bug', 's1', user_id='u1')
        assert first.status is TaskStatus.BLOCKED
        clock.advance(100.0)   # exact boundary: not expired yet (uses >)
        assert service.resumable('s1', first.task_id, user_id='u1') is True
        second = await service.query_result('app.py', 's1', user_id='u1', resume_task_id=first.task_id)
        assert second.status is TaskStatus.COMPLETED
        assert second.content == 'Done'


async def test_expired_blocked_task_is_not_resumable_and_web_returns_task_not_resumable(tmp_path):
    clock = FakeClock(1000.0)
    turns = [loop_reply('need info', 'blocked', 'Which file?')]
    async with _WebClient(_service(tmp_path, turns, ttl=60.0, clock=clock)) as web:
        await web.send('s1', 'Fix bug', 'r1')
        blocked = (await web.events('r1'))[-1]
        assert blocked['type'] == 'blocked'
        task_id = blocked['task_id']
        assert web.service.resumable('s1', task_id, user_id='local') is True

        clock.advance(61.0)
        assert web.service.resumable('s1', task_id, user_id='local') is False

        # Service-level resume raises the existing ValueError.
        with pytest.raises(ValueError, match='No resumable task'):
            await web.service.query_result('app.py', 's1', user_id='local', resume_task_id=task_id)

        # Web API returns 409 task_not_resumable, and the archived turn stays blocked (not resumable).
        resp = await web.send('s1', 'app.py', 'r2', resume_task_id=task_id)
        assert resp.status_code == 409
        assert resp.json()['code'] == 'task_not_resumable'
        [turn] = (await web.client.get('/api/sessions/s1/turns')).json()['turns']
        assert turn['status'] == 'blocked'
        assert turn['resumable'] is False


async def test_in_progress_resume_is_not_evicted_when_clock_advances_past_ttl(tmp_path):
    clock = FakeClock(1000.0)
    entered = asyncio.Event()
    release = asyncio.Event()
    call_count = 0

    async def model_side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return loop_reply('ask', 'blocked', 'Confirm?')
        if call_count == 2:
            entered.set()
            await release.wait()
            return loop_reply('Resumed ok')
        return loop_reply('Other session ok')

    model = AsyncMock()
    model.generate.side_effect = model_side_effect
    async with RagAgentService(
        model=model, mcp_servers=(), include_knowledge=False, memory_directory=tmp_path,
        blocked_task_ttl_seconds=60.0, clock=clock,
    ) as service:
        first = await service.query_result('Start', 's1', user_id='u1')
        assert first.status is TaskStatus.BLOCKED

        # Start resuming while within TTL; pause inside model.generate.
        clock.advance(30.0)
        resume_task = asyncio.create_task(
            service.query_result('Yes', 's1', user_id='u1', resume_task_id=first.task_id)
        )
        await entered.wait()

        # Clock advances far past TTL and another session triggers expiry checks.
        clock.advance(500.0)
        assert service.resumable('s1', first.task_id, user_id='u1') is False
        other = await service.query_result('Hi', 's2', user_id='u1')
        assert other.status is TaskStatus.COMPLETED

        # The in-progress resume completes normally.
        release.set()
        resumed = await resume_task
        assert resumed.status is TaskStatus.COMPLETED
        assert resumed.content == 'Resumed ok'


async def test_blocking_again_after_resume_resets_ttl_timer(tmp_path):
    clock = FakeClock(1000.0)
    turns = [
        ModelTurn(tool_calls=(ToolCall('ask-1', 'ask_user', {'question': 'First question?'}),)),
        ModelTurn(tool_calls=(ToolCall('ask-2', 'ask_user', {'question': 'Second question?'}),)),
        loop_reply('All done'),
    ]
    async with _service(tmp_path, turns, ttl=100.0, clock=clock) as service:
        first = await service.query_result('Start', 's1', user_id='u1')
        assert first.status is TaskStatus.BLOCKED

        clock.advance(80.0)
        second = await service.query_result('Answer 1', 's1', user_id='u1', resume_task_id=first.task_id)
        assert second.status is TaskStatus.BLOCKED
        assert second.task_id == first.task_id

        # 80 + 80 = 160 > 100 (past original TTL), but only 80 since re-blocking (< 100).
        clock.advance(80.0)
        assert service.resumable('s1', first.task_id, user_id='u1') is True

        third = await service.query_result('Answer 2', 's1', user_id='u1', resume_task_id=first.task_id)
        assert third.status is TaskStatus.COMPLETED
        assert third.content == 'All done'


async def test_expiry_only_evicts_expired_task_leaving_other_sessions_and_tasks_intact(tmp_path):
    clock = FakeClock(1000.0)
    turns = [
        loop_reply('qA', 'blocked', 'Question A?'),
        loop_reply('qB', 'blocked', 'Question B?'),
        loop_reply('qC', 'blocked', 'Question C?'),
        loop_reply('Done B'),
        loop_reply('Done C'),
    ]
    async with _service(tmp_path, turns, ttl=100.0, clock=clock) as service:
        task_a = await service.query_result('Task A', 's1', user_id='u1')
        clock.advance(60.0)
        task_b = await service.query_result('Task B', 's1', user_id='u1')   # another task in same session
        task_c = await service.query_result('Task C', 's2', user_id='u2')   # another session & user

        # Advance 50s: task_a is at 110s (expired), task_b and task_c are at 50s (still live).
        clock.advance(50.0)
        assert service.resumable('s1', task_a.task_id, user_id='u1') is False
        assert service.resumable('s1', task_b.task_id, user_id='u1') is True
        assert service.resumable('s2', task_c.task_id, user_id='u2') is True

        res_b = await service.query_result('Ans B', 's1', user_id='u1', resume_task_id=task_b.task_id)
        res_c = await service.query_result('Ans C', 's2', user_id='u2', resume_task_id=task_c.task_id)
        assert res_b.status is TaskStatus.COMPLETED and res_b.content == 'Done B'
        assert res_c.status is TaskStatus.COMPLETED and res_c.content == 'Done C'


def test_blocked_task_ttl_setting_and_show_limits_formatting():
    default_settings = Settings(
        _env_file=None, dashscope_api_key='k', agent_model_base_url='',
        agent_model_api_key='', agent_model='',
    )
    assert default_settings.blocked_task_ttl_seconds == 86400.0
    rows = dict((name, value) for _, name, value in RunLimits.current(settings=default_settings).rows())
    assert rows['阻塞任务保留时间（BLOCKED_TASK_TTL_SECONDS）'] == '24 小时'

    non_hour = Settings(
        _env_file=None, dashscope_api_key='k', agent_model_base_url='',
        agent_model_api_key='', agent_model='',
        blocked_task_ttl_seconds=1800.0,
    )
    rows_sec = dict((name, value) for _, name, value in RunLimits.current(settings=non_hour).rows())
    assert rows_sec['阻塞任务保留时间（BLOCKED_TASK_TTL_SECONDS）'] == '1800 秒'

    with pytest.raises(ValidationError):
        Settings(_env_file=None, blocked_task_ttl_seconds=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, blocked_task_ttl_seconds=31 * 86400)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, blocked_task_ttl_seconds=float('inf'))
