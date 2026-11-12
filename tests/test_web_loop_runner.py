"""The single-mode runner option through the real service and ASGI API (D-065).

Since stage 4c (D-076) the loop is the only runner: the option is kept for old callers, takes only
'loop', and 'plan' is refused at every entry point. Scripted models and local tools only; no model HTTP
requests, databases or MCP.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.agent.agent_loop import ASK_USER, LOOP_STEP, TEAM_STEP
from app.agent.runtime import AgentRunResult
from app.agent.step_models import ModelTurn, ToolCall, ToolMessage
from app.api.app import create_app
from app.api.runs import RunManager
from app.models.taskstate import TaskStatus
from app.services.rag_agent_service import RagAgentService


def make_service(*, turns=(), **kwargs):
    model = AsyncMock()
    model.generate.side_effect = list(turns)
    service = RagAgentService(model=model, mcp_servers=(),
                              include_knowledge=False, memory_directory='', **kwargs)
    return service, model


@asynccontextmanager
async def web_service(service):
    async with service:
        app = create_app(service, user_id='local', access_token='test')
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url='http://test',
                                         headers={'Authorization': 'Bearer test'}) as client:
                yield client, app.state.runs


async def finished(runs, run_id):
    run = runs.get(run_id)
    await asyncio.wait_for(asyncio.shield(run.task), 5)
    return run


@pytest.mark.parametrize('requested', [None, 'loop'])
async def test_each_task_runs_as_the_loop(requested):
    service, _ = make_service(turns=[ModelTurn('done')])
    events = []
    async with service:
        output = await service.query_result('Answer', 's', single_runner=requested, on_event=events.append)
    assert output.status is TaskStatus.COMPLETED and output.content == 'done'
    assert events[0]['options']['runner'] == 'loop'
    assert 'runner' not in events[0]  # one host-owned options envelope, no duplicate field
    assert events[1]['steps'] == [LOOP_STEP]


def test_the_service_refuses_plan_mode_at_startup():
    with pytest.raises(ValueError, match='removed in stage 4c'):
        RagAgentService(model=AsyncMock(), mcp_servers=(), include_knowledge=False, single_runner='plan')


@pytest.mark.parametrize('requested', ['plan', 'steps', '', False, 1])
async def test_invalid_runner_is_rejected_before_model_or_history(requested):
    service, model = make_service()
    async with service:
        with pytest.raises(ValueError, match='single_runner'):
            await service.query_result('Answer', 's', single_runner=requested)
        assert await service.list_sessions() == []
    model.generate.assert_not_called()


async def test_resume_retains_loop_choice_and_completed_tool_messages():
    service, model = make_service(turns=[
        ModelTurn(tool_calls=(ToolCall('clock', 'get_current_time', {'timezone': 'UTC'}),)),
        ModelTurn(tool_calls=(ToolCall('ask', ASK_USER, {'question': 'Which city?'}),)),
        ModelTurn('Paris'),
    ])
    events = []
    async with service:
        blocked = await service.query_result('Time?', 's', single_runner='loop')
        assert blocked.status is TaskStatus.BLOCKED
        assert service.task_options('s', blocked.task_id)['runner'] == 'loop'
        output = await service.query_result('Paris', 's', resume_task_id=blocked.task_id, on_event=events.append)
    assert output.status is TaskStatus.COMPLETED and output.replan_count == 0
    assert output.execution_count == 2 and model.generate.await_count == 3
    assert events[0]['options']['runner'] == 'loop'
    resumed = model.generate.await_args_list[-1].args[0]
    replies = [m for m in resumed if isinstance(m, ToolMessage)]
    assert [m.call_id for m in replies] == ['clock', 'ask']
    assert json.loads(replies[-1].content)['content'] == 'Paris'


async def test_health_exposes_the_one_runner():
    service, _ = make_service()
    async with web_service(service) as (client, _):
        options = (await client.get('/api/health')).json()['options']
    assert options['runners'] == ['loop'] and options['default_runner'] == 'loop'


async def test_auto_that_routes_single_runs_the_loop():
    service, _ = make_service(execution_mode='auto', turns=[ModelTurn('done')])
    events = []
    async with service:
        result = await service.query_result('Answer', 's', allow_writes=False, on_event=events.append)
    assert result.status is TaskStatus.COMPLETED
    assert events[0]['mode'] == 'single' and events[0]['options']['mode'] == 'auto'
    assert events[1]['steps'] == [LOOP_STEP]


async def test_explicit_team_runs_the_team_lead_loop(monkeypatch):
    from app.services import rag_agent_service as module
    seen = []

    def runtime(memory, model, registry, **options):
        seen.append((options.pop('step_description'), options))
        return SimpleNamespace(run=AsyncMock(return_value=AgentRunResult(
            memory.task_state.task_id, TaskStatus.COMPLETED, 'done', (), 1, 0)))

    monkeypatch.setattr(module, 'LoopRun', runtime)
    service, _ = make_service(execution_mode='team')
    async with service:
        result = await service.query_result('Review', 's', single_runner='loop')
    assert result.status is TaskStatus.COMPLETED
    step, options = seen[0]
    assert step == TEAM_STEP
    from app.agent.delegation import DelegatingLoopRunner
    from app.agent.step_runner import RunnerDependencies, RunnerLimits
    from app.tools.tool_registry import ToolRegistry
    assert isinstance(options['runner_factory'](RunnerDependencies(AsyncMock(), ToolRegistry(), None, RunnerLimits())),
                      DelegatingLoopRunner)


async def test_web_loop_choice_reaches_service_and_resume_keeps_it():
    service, _ = make_service(turns=[
        ModelTurn(tool_calls=(ToolCall('ask', ASK_USER, {'question': 'Which?'}),)), ModelTurn('chosen'),
    ])
    original = service.query_result
    service.query_result = AsyncMock(wraps=original)
    async with web_service(service) as (client, runs):
        response = await client.post('/api/sessions/s/messages', json={
            'content': 'Answer', 'request_id': 'first', 'options': {'runner': 'loop'}})
        assert response.status_code == 202
        first = await finished(runs, 'first')
        assert first.status == 'blocked' and first.options['runner'] == 'loop'
        assert service.query_result.await_args_list[0].kwargs['single_runner'] == 'loop'
        response = await client.post('/api/sessions/s/messages', json={
            'content': 'choice', 'request_id': 'second', 'resume_task_id': first.snapshot()['result']['task_id']})
        assert response.status_code == 202
        second = await finished(runs, 'second')
        assert second.status == 'completed' and second.options['runner'] == 'loop'


@pytest.mark.parametrize('options', [{'runner': 'plan'}, {'runner': 'steps'}, {'runner': ''}])
async def test_web_rejects_other_runners(options):
    service, model = make_service()
    async with web_service(service) as (client, _):
        response = await client.post('/api/sessions/s/messages', json={
            'content': 'Answer', 'request_id': 'r', 'options': options})
        assert response.status_code == 400 and response.json()['code'] == 'invalid_input'
    model.generate.assert_not_called()


async def test_legacy_run_options_without_runner_use_the_loop():
    service, _ = make_service(turns=[ModelTurn('done')])
    async with service:
        runs = RunManager(service, user_id='local', max_concurrent=1, retention_seconds=60)
        run, _ = runs.start('s', 'Answer', 'r', options={'mode': 'single', 'write': False, 'memory': False})
        await finished(runs, run.run_id)
    assert run.status == 'completed' and run.options['runner'] == 'loop'


def test_the_entry_points_take_only_the_loop():
    import inspect
    from app.agent.__main__ import parse_args
    from app.api.__main__ import build_parser
    from bench.run_bench import run_one
    assert parse_args(['goal']).single_runner == 'loop'
    assert build_parser().parse_args([]).single_runner == 'loop'
    assert inspect.signature(run_one).parameters['single_runner'].default == 'loop'
    assert inspect.signature(RagAgentService).parameters['single_runner'].default == 'loop'
    for parse in (lambda: parse_args(['goal', '--single-runner', 'plan']),
                  lambda: build_parser().parse_args(['--single-runner', 'plan'])):
        with pytest.raises(SystemExit):
            parse()
