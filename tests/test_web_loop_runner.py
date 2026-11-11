"""Per-task plan/loop selection through the real service and ASGI API (D-065).

Scripted models and local tools only; no model HTTP requests, databases or MCP.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.agent.agent_loop import ASK_USER, LoopPlanner
from app.agent.runtime import AgentRunResult
from app.agent.step_models import ModelTurn, ToolCall, ToolMessage
from app.api.app import create_app
from app.api.runs import RunManager
from app.models.taskstate import TaskStatus
from app.services.rag_agent_service import RagAgentService


def make_service(*, turns=(), **kwargs):
    planner, model = AsyncMock(), AsyncMock()
    planner.generate.return_value = json.dumps({'steps': ['Answer'], 'reason': None})
    model.generate.side_effect = list(turns)
    service = RagAgentService(model=model, planner_model=planner, mcp_servers=(),
                              include_knowledge=False, memory_directory='', **kwargs)
    return service, planner, model


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


@pytest.mark.parametrize('default,requested,expected', [
    ('plan', None, 'plan'), ('plan', 'loop', 'loop'),
    ('loop', None, 'loop'), ('loop', 'plan', 'plan'),
])
async def test_each_task_can_select_runner_or_keep_service_default(default, requested, expected):
    result = 'done' if expected == 'loop' else json.dumps(
        {'status': 'succeeded', 'content': 'done', 'reason': None})
    service, planner, _ = make_service(single_runner=default, turns=[ModelTurn(result)])
    events = []
    async with service:
        output = await service.query_result('Answer', 's', single_runner=requested, on_event=events.append)
    assert output.status is TaskStatus.COMPLETED and output.content == 'done'
    assert events[0]['options']['runner'] == expected
    assert 'runner' not in events[0]  # one host-owned options envelope, no duplicate field
    if expected == 'loop':
        planner.generate.assert_not_called()
        assert events[1]['steps'] == [LoopPlanner.STEP]
    else:
        planner.generate.assert_awaited_once()


@pytest.mark.parametrize('requested', ['steps', '', False, 1])
async def test_invalid_runner_is_rejected_before_model_or_history(requested):
    service, planner, model = make_service()
    async with service:
        with pytest.raises(ValueError, match='single_runner'):
            await service.query_result('Answer', 's', single_runner=requested)
        assert await service.list_sessions() == []
    planner.generate.assert_not_called()
    model.generate.assert_not_called()


async def test_resume_retains_loop_choice_and_completed_tool_messages():
    service, planner, model = make_service(turns=[
        ModelTurn(tool_calls=(ToolCall('clock', 'get_current_time', {'timezone': 'UTC'}),)),
        ModelTurn(tool_calls=(ToolCall('ask', ASK_USER, {'question': 'Which city?'}),)),
        ModelTurn('Paris'),
    ])
    events = []
    async with service:
        blocked = await service.query_result('Time?', 's', single_runner='loop')
        assert blocked.status is TaskStatus.BLOCKED
        assert service.task_options('s', blocked.task_id)['runner'] == 'loop'
        output = await service.query_result('Paris', 's', resume_task_id=blocked.task_id,
                                            single_runner='plan', on_event=events.append)
    assert output.status is TaskStatus.COMPLETED and output.replan_count == 0
    assert output.execution_count == 2 and model.generate.await_count == 3
    assert events[0]['options']['runner'] == 'loop'
    planner.generate.assert_not_called()
    resumed = model.generate.await_args_list[-1].args[0]
    replies = [m for m in resumed if isinstance(m, ToolMessage)]
    assert [m.call_id for m in replies] == ['clock', 'ask']
    assert json.loads(replies[-1].content)['content'] == 'Paris'


async def test_health_exposes_runner_choices_and_configured_default():
    service, _, _ = make_service(single_runner='loop')
    async with web_service(service) as (client, _):
        options = (await client.get('/api/health')).json()['options']
    assert options['runners'] == ['plan', 'loop'] and options['default_runner'] == 'loop'


async def test_plan_resume_ignores_new_loop_choice():
    service, _, _ = make_service(turns=[
        ModelTurn(json.dumps({'status': 'blocked', 'content': 'Need answer', 'reason': 'Which?'})),
        ModelTurn(json.dumps({'status': 'succeeded', 'content': 'done', 'reason': None})),
    ])
    async with service:
        blocked = await service.query_result('Answer', 's')
        assert blocked.status is TaskStatus.BLOCKED
        result = await service.query_result('chosen', 's', resume_task_id=blocked.task_id, single_runner='loop')
    assert result.status is TaskStatus.COMPLETED


async def test_loop_override_does_not_change_the_next_tasks_plan_default():
    service, planner, _ = make_service(turns=[ModelTurn('loop answer'), ModelTurn(json.dumps(
        {'status': 'succeeded', 'content': 'plan answer', 'reason': None}))])
    events = []
    async with service:
        await service.query_result('First', 's', single_runner='loop')
        second = await service.query_result('Second', 's', on_event=events.append)
    assert second.content == 'plan answer' and events[0]['options']['runner'] == 'plan'
    planner.generate.assert_awaited_once()


async def test_auto_that_routes_single_honours_the_loop_choice():
    service, planner, _ = make_service(execution_mode='auto', turns=[ModelTurn('done')])
    events = []
    async with service:
        result = await service.query_result('Answer', 's', allow_writes=False,
                                            single_runner='loop', on_event=events.append)
    assert result.status is TaskStatus.COMPLETED
    assert events[0]['mode'] == 'single' and events[0]['options']['mode'] == 'auto'
    planner.generate.assert_not_called()


async def test_explicit_team_does_not_use_loop_even_if_runner_choice_is_loop(monkeypatch):
    from app.agent.agent_loop import TeamPlanner
    from app.services import rag_agent_service as module
    seen = []

    def runtime(planner, model, memory, registry, **options):
        seen.append((planner, options))
        return SimpleNamespace(run=AsyncMock(return_value=AgentRunResult(
            memory.task_state.task_id, TaskStatus.COMPLETED, 'done', (), 1, 0)))

    monkeypatch.setattr(module, 'AgentRuntime', runtime)
    service, _, _ = make_service(execution_mode='team')
    async with service:
        result = await service.query_result('Review', 's', single_runner='loop')
    assert result.status is TaskStatus.COMPLETED
    planner, options = seen[0]
    assert type(planner) is TeamPlanner
    from app.agent.delegation import DelegatingLoopRunner
    from app.agent.step_runner import RunnerDependencies, RunnerLimits
    from app.tools.tool_registry import ToolRegistry
    assert isinstance(options['runner_factory'](RunnerDependencies(AsyncMock(), ToolRegistry(), None, RunnerLimits())),
                      DelegatingLoopRunner)


async def test_web_loop_choice_reaches_service_and_resume_keeps_original_choice():
    service, planner, _ = make_service(turns=[
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
            'content': 'choice', 'request_id': 'second', 'resume_task_id': first.snapshot()['result']['task_id'],
            'options': {'runner': 'plan'}})
        assert response.status_code == 202
        second = await finished(runs, 'second')
        assert second.status == 'completed' and second.options['runner'] == 'loop'
    planner.generate.assert_not_called()


@pytest.mark.parametrize('options', [{'runner': 'steps'}, {'runner': ''}])
async def test_web_rejects_invalid_runner(options):
    service, _, model = make_service()
    async with web_service(service) as (client, _):
        response = await client.post('/api/sessions/s/messages', json={
            'content': 'Answer', 'request_id': 'r', 'options': options})
        assert response.status_code == 400 and response.json()['code'] == 'invalid_input'
    model.generate.assert_not_called()


async def test_legacy_run_options_without_runner_use_service_default():
    service, planner, _ = make_service(single_runner='loop', turns=[ModelTurn('done')])
    async with service:
        runs = RunManager(service, user_id='local', max_concurrent=1, retention_seconds=60)
        run, _ = runs.start('s', 'Answer', 'r', options={'mode': 'single', 'write': False, 'memory': False})
        await finished(runs, run.run_id)
    assert run.status == 'completed' and run.options['runner'] == 'loop'
    planner.generate.assert_not_called()


async def test_request_id_conflicts_when_only_runner_choice_changes():
    service, _, model = make_service(turns=[ModelTurn('done')])
    async with web_service(service) as (client, runs):
        body = {'content': 'Answer', 'request_id': 'r', 'options': {'runner': 'loop'}}
        assert (await client.post('/api/sessions/s/messages', json=body)).status_code == 202
        await finished(runs, 'r')
        body['options'] = {'runner': 'plan'}
        response = await client.post('/api/sessions/s/messages', json=body)
        assert response.status_code == 409 and response.json()['code'] == 'request_conflict'
    assert model.generate.await_count == 1


def test_web_parser_defaults_to_loop_and_accepts_plan():
    from app.api.__main__ import build_parser
    assert build_parser().parse_args([]).single_runner == 'loop'
    assert build_parser().parse_args(['--single-runner', 'plan']).single_runner == 'plan'


def test_entry_points_default_to_loop_but_the_service_class_keeps_plan():
    import inspect
    from app.agent.__main__ import parse_args
    from bench.run_bench import run_one
    assert parse_args(['goal']).single_runner == 'loop'
    assert inspect.signature(run_one).parameters['single_runner'].default == 'loop'
    assert inspect.signature(RagAgentService).parameters['single_runner'].default == 'plan'
