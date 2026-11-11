"""Deterministic tests for per-task routing: host rules, then one model call (route-auto-001, DECISIONS D-026)."""

import asyncio
import json
import logging

import pytest

from app.agent.route_selection import ModeChoice, allowed_modes, choose_mode
from app.agent.runtime import AgentRunResult
from app.agent.step_models import ModelTurn, ToolCall
from app.agent.delegation import READ_TOOLS, DelegationOptions
from app.agent.agent_loop import TeamPlanner
from app.models.taskstate import TaskStatus

SANDBOX = DelegationOptions(allowed_tools=READ_TOOLS | {'write_file', 'run_tests', 'run_python'},
                            require_passing_run=True)
SECRET = 'SECRET-ROUTE-7c1'


class Model:
    """Scripted model; an exception in the script is raised instead of returned."""

    def __init__(self, *turns):
        self.turns, self.requests = list(turns), []

    async def generate(self, messages, tools):
        self.requests.append((tuple(messages), tools))
        item = self.turns.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class Slow:
    async def generate(self, messages, tools):
        await asyncio.sleep(5)


def reply(route, **extra):
    return ModelTurn(content=json.dumps({'route': route, 'reason': SECRET, **extra}))


def test_rules_leave_the_choice_to_the_model_only_for_code_delivery_in_the_sandbox():
    assert allowed_modes(None) == (frozenset({'single'}), 'team_unavailable')
    assert allowed_modes(DelegationOptions()) == (frozenset({'single'}), 'read_only')
    write_only = DelegationOptions(allowed_tools=READ_TOOLS | {'write_file'})
    assert allowed_modes(write_only) == (frozenset({'single'}), 'no_sandbox')
    assert allowed_modes(SANDBOX) == (frozenset({'single', 'team'}), 'code_delivery_with_sandbox')


async def test_a_rule_decision_never_calls_the_model():
    model = Model()
    choice = await choose_mode('what time is it', DelegationOptions(), model)
    assert choice == ModeChoice('single', 'rule', 'read_only') and model.requests == []


@pytest.mark.parametrize('route', ['team', 'single'])
async def test_the_model_chooses_inside_the_rules(route):
    model = Model(reply(route))
    choice = await choose_mode('implement parse_duration with tests', SANDBOX, model)
    assert choice == ModeChoice(route, 'model', 'code_delivery_with_sandbox')
    (messages, tools), = model.requests
    assert tools == ()                                               # tool-free call
    assert 'json' in ' '.join(m.content for m in messages).lower()   # provider JSON mode needs it
    assert json.loads(messages[1].content) == {'task': 'implement parse_duration with tests'}


async def test_long_goals_are_truncated_for_the_routing_call():
    model = Model(reply('single'))
    await choose_mode('x' * 9000, SANDBOX, model)
    assert len(json.loads(model.requests[0][0][1].content)['task']) == 4000


@pytest.mark.parametrize('turn, failure', [
    (ModelTurn(content='team'), 'JsonFormatError -> JSONDecodeError'),
    (ModelTurn(content='```json\n{"route": "parallel"}\n```'), 'ValueError'),
    (reply('team', plan='do everything'), 'ValueError'),
    (ModelTurn(content='{"route": "team"}', tool_calls=(ToolCall('c', 'write_file', {}),)), 'ValueError'),
    (ConnectionError(SECRET), 'ConnectionError'),
])
async def test_any_model_failure_falls_back_to_single(turn, failure, caplog):
    caplog.set_level(logging.INFO, logger='app.agent.route_selection')
    choice = await choose_mode('implement it', SANDBOX, Model(turn))
    assert choice == ModeChoice('single', 'fallback', 'code_delivery_with_sandbox', failure)
    assert SECRET not in caplog.text


async def test_a_slow_model_times_out_to_single():
    choice = await choose_mode('implement it', SANDBOX, Slow(), timeout_seconds=0.05)
    assert (choice.mode, choice.source, choice.failure) == ('single', 'fallback', 'TimeoutError -> CancelledError')


async def test_cancellation_is_not_swallowed():
    task = asyncio.ensure_future(choose_mode('implement it', SANDBOX, Slow()))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_the_decision_log_has_host_values_only_and_is_loud_on_request(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger='app.agent.route_selection')
    monkeypatch.delenv('ALETHEIA_TEAM_TRACE', raising=False)
    await choose_mode('implement it', SANDBOX, Model(reply('team')))
    monkeypatch.setenv('ALETHEIA_TEAM_TRACE', '1')
    await choose_mode('implement it', SANDBOX, Model(reply('team')))
    records = [r for r in caplog.records if r.getMessage().startswith('Route decision: ')]
    assert [r.levelname for r in records] == ['INFO', 'WARNING']
    assert json.loads(records[0].getMessage()[len('Route decision: '):]) == {
        'mode': 'team', 'source': 'model', 'rule': 'code_delivery_with_sandbox'}
    assert SECRET not in caplog.text


def test_cli_auto_gets_team_options_only_for_code_delivery_in_the_sandbox():
    from app.agent.__main__ import build_parser, team_options_for
    assert build_parser().parse_args(['goal', '--execution-mode', 'auto']).execution_mode == 'auto'
    options = team_options_for('auto', 'w', True, 300)
    assert isinstance(options, DelegationOptions) and options.require_passing_run and options.timeout_seconds == 300
    assert team_options_for('auto', 'w', False) is None and team_options_for('auto', None, False) is None


def test_bench_passes_team_flags_to_auto_and_reads_the_decision(monkeypatch):
    from types import SimpleNamespace
    from bench import run_bench
    seen = []
    decision = 'Route decision: ' + json.dumps({'mode': 'team', 'source': 'model', 'rule': 'r'})
    monkeypatch.setattr(run_bench, 'launch_agent', lambda cmd, env, timeout: seen.append((cmd, timeout)) or
                        SimpleNamespace(stdout='x\n\nStatus: failed; executions: 1; replans: 0\n',
                                        stderr='noise\n' + decision + '\n'))
    task = next(t for t in run_bench.load_tasks(['all']) if t['id'] == 'parse_duration')
    row = run_bench.run_one(task, 'auto', 1, 5, team_timeout=200)
    cmd, timeout = seen[0]
    assert cmd[cmd.index('--execution-mode') + 1] == 'auto'
    assert cmd[cmd.index('--team-timeout') + 1] == '200' and timeout == 500
    assert row['route'] == {'mode': 'team', 'source': 'model', 'rule': 'r'}
    with pytest.raises(SystemExit):
        run_bench.main(['--team-version', '2'])                      # removed with V1 (D-025)


# ---- service ------------------------------------------------------------------------------

class FakeRuntime:
    created = []

    def __init__(self, planner, model, memory, registry, **options):
        self.planner, self.memory, self.registry, self.options = planner, memory, registry, options
        FakeRuntime.created.append(self)

    async def run(self, **kwargs):
        status = FakeRuntime.status
        return AgentRunResult(self.memory.task_state.task_id, status, 'done', (), 1, 0)


def _service(monkeypatch, model, status=TaskStatus.COMPLETED, **kw):
    from app.services import rag_agent_service as module
    FakeRuntime.created, FakeRuntime.status = [], status
    monkeypatch.setattr(module, 'AgentRuntime', FakeRuntime)
    return module.RagAgentService(model=model, mcp_servers=(), include_knowledge=False, **kw)


async def test_auto_service_routes_each_task_and_binds_the_chosen_runner(monkeypatch):
    model = Model(reply('team'), reply('single'))
    service = _service(monkeypatch, model, execution_mode='auto', team_options=SANDBOX)
    async with service:
        await service.query_result('implement parse_duration with tests', 's')
        await service.query_result('what does parse_duration do?', 's')
    team, single = FakeRuntime.created
    assert isinstance(team.planner, TeamPlanner) and not isinstance(single.planner, TeamPlanner)
    from app.agent.step_runner import RunnerDependencies, RunnerLimits
    from app.tools.tool_registry import ToolRegistry
    runner = team.options['runner_factory'](RunnerDependencies(model, ToolRegistry(), None, RunnerLimits()))
    assert runner.runner_id == 'agent_team'
    assert 'runner_factory' not in single.options
    assert service._routes == {}                                     # finished tasks forget their route


async def test_auto_without_team_options_never_asks_the_model(monkeypatch):
    service = _service(monkeypatch, Model(), execution_mode='auto')
    async with service:
        await service.query_result('implement it', 's')
    assert not isinstance(FakeRuntime.created[0].planner, TeamPlanner)


async def test_a_blocked_task_keeps_its_route_for_the_resume(monkeypatch):
    service = _service(monkeypatch, Model(reply('team')), TaskStatus.BLOCKED,
                       execution_mode='auto', team_options=SANDBOX)
    async with service:
        result = await service.query_result('implement it', 's')
        assert [c.mode for c in service._routes.values()] == ['team']
        assert list(service._routes) == [(service._session_key(None, 's'), result.task_id)]


async def test_fixed_modes_keep_their_runner_and_log_a_user_decision(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger='app.agent.route_selection')
    service = _service(monkeypatch, Model(), execution_mode='team', team_options=SANDBOX)
    async with service:
        await service.query_result('anything', 's')
    assert isinstance(FakeRuntime.created[0].planner, TeamPlanner)
    assert '"source": "user", "rule": "team"' in caplog.text


def test_service_rejects_invalid_execution_mode():
    from app.services.rag_agent_service import RagAgentService
    with pytest.raises(ValueError, match='single, team or auto'):
        RagAgentService(execution_mode='parallel', mcp_servers=())


# ---- read-only tasks and the final answer (D-027) -------------------------------------------

@pytest.mark.parametrize('extra, read_only', [({'writes': False}, True), ({'writes': True}, False), ({}, False)])
async def test_the_model_can_mark_a_single_task_read_only(extra, read_only, caplog):
    caplog.set_level(logging.INFO, logger='app.agent.route_selection')
    choice = await choose_mode('what does late_fee return?', SANDBOX, Model(reply('single', **extra)))
    assert choice == ModeChoice('single', 'model', 'code_delivery_with_sandbox', read_only=read_only)
    assert ('"read_only": true' in caplog.text) is read_only


@pytest.mark.parametrize('turn', [reply('team', writes=False), reply('single', writes='no')])
async def test_contradictory_or_invalid_writes_fall_back_with_every_tool(turn):
    choice = await choose_mode('implement it', SANDBOX, Model(turn))
    assert (choice.mode, choice.source, choice.read_only) == ('single', 'fallback', False)


class FakeTool:
    def __init__(self, name):
        from app.agent.step_models import ToolDefinition
        self.definition = ToolDefinition(name, f'{name} tool', {'type': 'object'})

    async def invoke(self, call):
        raise AssertionError('not called')


def _tools(*names):
    from app.tools.tool_registry import ToolRegistry
    registry = ToolRegistry()
    for name in names:
        registry.register(FakeTool(name))
    return registry


def test_the_read_only_registry_hides_write_and_run_tools_only():
    from app.agent.route_selection import read_only_registry
    full = _tools('read_file', 'write_file', 'run_tests', 'run_python', 'list_dir')
    view = read_only_registry(full)
    assert [d.name for d in view.list_definitions()] == ['read_file', 'list_dir']
    assert view.get('read_file') is full.get('read_file')
    assert len(full.list_definitions()) == 5                          # the shared registry is untouched


async def test_a_read_only_task_runs_without_write_tools(monkeypatch):
    from contextlib import asynccontextmanager
    from app.services import rag_agent_service as module

    @asynccontextmanager
    async def registry(memory, **kw):
        yield _tools('read_file', 'write_file', 'run_python', 'run_python_code')
    monkeypatch.setattr(module, 'open_tool_registry', registry)
    service = _service(monkeypatch, Model(reply('single', writes=False), reply('single')),
                       execution_mode='auto', team_options=SANDBOX)
    async with service:
        await service.query_result('what does late_fee return?', 's')
        await service.query_result('fix late_fee', 's')
    read_only, writer = FakeRuntime.created
    # D-028: the read-only task keeps a read-only run tool; the writing task never sees it.
    assert [d.name for d in read_only.registry.list_definitions()] == ['read_file', 'run_python_code']
    assert [d.name for d in writer.registry.list_definitions()] == ['read_file', 'write_file', 'run_python']


async def test_single_mode_answers_with_the_last_step_result():
    from datetime import datetime, timezone
    from app.agent.planner import Planner, PlanProposal
    from app.agent.runtime import AgentRuntime
    from app.models.taskstate import TaskState
    from app.models.working_memory import WorkingMemory

    class TwoSteps(Planner):
        async def plan(self, context):
            return PlanProposal(steps=('read billing.py', 'answer as one JSON object'),
                                goal_revision=context.goal_revision, base_state_version=context.state_version,
                                reason=None, created_at=datetime.now(timezone.utc), task_id=context.task_id,
                                base_resource_context_version=context.resource_context_version)

    def done(content):
        return ModelTurn(content=json.dumps({'status': 'succeeded', 'content': content, 'reason': None}))
    model = Model(done('read billing.py: MAX_RETRIES = 3'), done('{"max_retries": 3}'))
    memory = WorkingMemory(TaskState('u', 'how many retries?'))
    result = await AgentRuntime(TwoSteps(None), model, memory, _tools('read_file')).run()
    assert result.status is TaskStatus.COMPLETED and result.content == '{"max_retries": 3}'
    assert [r.content for r in memory.task_state.scratchpad.intermediate_results] == [
        'read billing.py: MAX_RETRIES = 3', '{"max_retries": 3}']     # earlier results are kept


def test_task_registry_keeps_the_read_only_run_tool_for_read_only_tasks_only():
    from app.agent.route_selection import task_registry
    full = _tools('read_file', 'write_file', 'run_tests', 'run_python', 'run_python_code')
    assert [d.name for d in task_registry(full, True).list_definitions()] == ['read_file', 'run_python_code']
    assert [d.name for d in task_registry(full, False).list_definitions()] == [
        'read_file', 'write_file', 'run_tests', 'run_python']
    plain = _tools('read_file', 'write_file')
    assert task_registry(plain, False) is plain                       # nothing to hide: same registry
