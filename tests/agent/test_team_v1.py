import asyncio
from contextlib import asynccontextmanager
import json

import pytest

from app.agent.step_models import ModelTurn, ToolCall
from app.agent.team.session import TeamSession
from app.agent.team.contracts import JsonContract
from test_team_session_poc import ScriptedModel, ReadTool, factory, call


async def test_checkpoint_closes_resources_rebinds_and_preserves_completed_read():
    owners, read, events = [], ReadTool(), asyncio.Queue()
    contract = JsonContract('answer', (('answer', str),))
    member = TeamSession('worker', ScriptedModel(call('read'),
        call('ask', 'request_coordination', issue='need format')),
        factory(read, owners), events, 'task', output_contract=contract, pause_at_boundary=True)
    result = await member.run()
    assert result.kind == 'coordination_requested' and member.status == 'paused'
    assert len(owners) == 2
    member.rebind(ScriptedModel(call('submit', 'submit_result', payload={'answer': '73'})), factory(read, owners))
    member.revise(1, 'use retained evidence')
    result = await member.run()
    assert result.kind == 'candidate_submitted' and json.loads(result.content) == {'answer': '73'}
    assert len(read.calls) == 1 and member.model_calls == 3 and len(owners) == 4


async def test_checkpoint_cannot_replay_candidate_without_new_assignment():
    member = TeamSession('worker', ScriptedModel(call('s', 'submit_result', payload={'answer': 'done'})),
        factory(), asyncio.Queue(), 'task', output_contract=JsonContract('answer', (('answer', str),)),
        pause_at_boundary=True)
    await member.run()
    with pytest.raises(Exception, match='revision'):
        await member.run()


def action(identity, name, member='', target='', instruction='', tools='', version=0):
    return call(identity, 'submit_result', payload={'action': name, 'member_id': member,
        'target_id': target, 'instruction': instruction, 'tools': tools, 'expected_version': version})


@pytest.mark.parametrize('member_revisions', [1, 12])
async def test_coordinator_dynamic_members_feedback_independent_review_and_finish(member_revisions):
    from app.agent.team.coordinator import TeamCoordinator
    from app.agent.team.models import TeamOptions, TeamStore, TeamEnvironment
    from test_runner_routing import context
    from app.tools.tool_registry import ToolRegistry
    @asynccontextmanager
    async def tools(memory, manager):
        yield ToolRegistry()
    sequence = ScriptedModel(
        action('l1', 'create_worker', 'writer', instruction='draft'),
        action('l2', 'run_member', 'writer'),
        call('ask', 'request_coordination', issue='need authorization'),
        action('l3', 'assign', 'writer', instruction='authorized beta', version=1),
        action('l4', 'run_member', 'writer'),
        call('draft', 'submit_result', payload={'answer': 'beta'}),
        action('l5', 'create_reviewer', 'reviewer', instruction='check'),
        action('l6', 'review', 'reviewer', 'writer', instruction='check draft', version=2),
        call('review', 'submit_result', payload={'candidate_version': 2, 'decision': 'approve', 'feedback': 'verified'}),
        action('l7', 'finish', 'writer', version=2))
    ctx = context()
    state = TeamStore().get(ctx, TeamOptions(max_revisions=member_revisions))
    coordinator = TeamCoordinator(state, sequence, TeamEnvironment('u', tools, lambda m: ()), ctx)
    output = await coordinator.run()
    assert json.loads(output.content) == {'answer': 'beta'}
    assert set(state.members) == {'writer', 'reviewer'}
    assert state.budget.model_calls == 10 and state.actions == 7


async def test_team_cannot_grant_tools_outside_host_allowlist():
    from app.agent.team.coordinator import TeamCoordinator
    from app.agent.team.models import TeamOptions, TeamStore, TeamEnvironment, TeamError
    from test_runner_routing import context
    ctx = context()
    coordinator = TeamCoordinator(TeamStore().get(ctx, TeamOptions()),
        ScriptedModel(action('l', 'create_worker', 'bad', instruction='delete files', tools='delete_everything')),
        TeamEnvironment('u', None, None), ctx)
    with pytest.raises(TeamError, match='permission'):
        await coordinator.run()


async def test_service_routes_team_pauses_rebinds_and_resumes_without_repeating_clock(tmp_path):
    from unittest.mock import AsyncMock
    from app.services.rag_agent_service import RagAgentService
    from app.models.taskstate import TaskStatus
    first_model = ScriptedModel(
        action('l1', 'create_worker', 'writer', instruction='Read time then ask which format', tools='get_current_time'),
        action('l2', 'run_member', 'writer'),
        call('clock', 'get_current_time', timezone='UTC'),
        call('ask', 'request_coordination', issue='Which output format?'),
        action('l3', 'ask_user', instruction='Which output format?'))
    planner = AsyncMock()
    planner.generate.side_effect = [
        '{"steps":["Collect time and produce report"],"reason":null}',
        '{"steps":["Finish report using saved time and answer"],"reason":null}']
    service = RagAgentService(model=first_model, planner_model=planner, execution_mode='team',
        mcp_servers=(), include_knowledge=False, memory_directory=str(tmp_path))
    try:
        blocked = await service.query_result('Report current time', 's', user_id='alice', request_id='first')
        assert blocked.status is TaskStatus.BLOCKED
        state = next(iter(service._team_store.states.values()))
        service.model = ScriptedModel(
            action('l4', 'assign', 'writer', instruction='Use retained time, JSON approved by user', version=1),
            action('l5', 'run_member', 'writer'),
            call('final', 'submit_result', payload={'answer': 'Time evidence retained'}),
            action('l6', 'create_reviewer', 'reviewer', instruction='check retained evidence'),
            action('l7', 'review', 'reviewer', 'writer', instruction='verify', version=2),
            call('checked', 'submit_result', payload={'candidate_version': 2, 'decision': 'approve', 'feedback': 'checked'}),
            action('l8', 'finish', 'writer', version=2))
        result = await service.query_result('JSON', 's', user_id='alice', resume_task_id=blocked.task_id,
                                            request_id='second')
        assert result.status is TaskStatus.COMPLETED
        assert result.task_id == blocked.task_id and result.replan_count == 1
        assert sum(r.tool_name == 'get_current_time' for r in state.members['writer'].session.tool_results) == 1
        assert state.status == 'completed' and not service._team_store.states
        assert all(r.call_id.startswith(('writer/', 'reviewer/', 'lead/')) for r in result.steps[-1].tool_results)
        assert len(first_model.requests) == 5
    finally:
        await service.aclose()


async def test_team_rag_evidence_is_reviewed_privately_then_imported_for_parent_commit(tmp_path):
    from unittest.mock import AsyncMock
    from app.services.rag_agent_service import RagAgentService
    from app.models.taskstate import TaskStatus
    search = AsyncMock(return_value=('source fact 73', {'status': 'ok', 'citations': []}))
    model = ScriptedModel(
        action('l1', 'create_worker', 'writer', instruction='retrieve source', tools='retrieve_knowledge'),
        action('l2', 'run_member', 'writer'),
        call('rag', 'retrieve_knowledge', query='fact'),
        call('draft', 'submit_result', payload={'answer': 'source fact 73'}),
        action('l3', 'create_reviewer', 'reviewer', instruction='check evidence'),
        action('l4', 'review', 'reviewer', 'writer', instruction='verify actual evidence', version=1),
        call('review', 'submit_result', payload={'candidate_version': 1, 'decision': 'approve', 'feedback': 'source verified'}),
        action('l5', 'finish', 'writer', version=1))
    service = RagAgentService(model=model, planner_model=AsyncMock(generate=AsyncMock(
        return_value='{"steps":["Retrieve fact and review"],"reason":null}')),
        execution_mode='team', mcp_servers=(), knowledge_search=search, memory_directory=str(tmp_path))
    try:
        result = await service.query_result('Find fact', 's', user_id='alice')
        assert result.status is TaskStatus.COMPLETED
        assert result.steps[0].source_refs
        rag = next(r for r in result.steps[0].tool_results if r.tool_name == 'retrieve_knowledge')
        assert rag.source_refs == result.steps[0].source_refs
        assert any('source fact 73' in (m.content or '') and 'tool_evidence' in (m.content or '')
                   for m in model.requests[6])
    finally:
        await service.aclose()


@pytest.mark.parametrize('options', [
    {'max_model_calls': 1}, {'max_actions': 1}, {'max_members': 1},
    {'max_estimated_tokens': 1}, {'max_tool_calls': 1}])
async def test_shared_team_budgets_are_finite_and_terminal(options, tmp_path):
    from unittest.mock import AsyncMock
    from app.services.rag_agent_service import RagAgentService
    from app.agent.team.models import TeamOptions
    from app.models.taskstate import TaskStatus
    model = ScriptedModel(
        action('l1', 'create_worker', 'writer', instruction='read twice', tools='get_current_time'),
        action('l2', 'run_member', 'writer'),
        ModelTurn(tool_calls=(
            ToolCall('one', 'get_current_time', {'timezone': 'UTC'}),
            ToolCall('two', 'get_current_time', {'timezone': 'UTC'}))),
        action('l3', 'create_reviewer', 'reviewer', instruction='check'))
    if 'max_members' in options:
        model = ScriptedModel(action('l1', 'create_worker', 'writer', instruction='draft'),
                              action('l2', 'create_reviewer', 'reviewer', instruction='check'))
    service = RagAgentService(model=model, planner_model=AsyncMock(generate=AsyncMock(
        return_value='{"steps":["Do team work"],"reason":null}')), execution_mode='team',
        team_options=TeamOptions(**options), mcp_servers=(), include_knowledge=False, memory_directory=str(tmp_path))
    try:
        result = await service.query_result('work', 's')
        assert result.status is TaskStatus.FAILED and result.replan_count == 0
        assert not service._team_store.states
    finally:
        await service.aclose()


async def test_outer_team_timeout_is_terminal_not_replan(tmp_path):
    from unittest.mock import AsyncMock
    from app.services.rag_agent_service import RagAgentService
    from app.models.taskstate import TaskStatus
    cancelled = asyncio.Event()
    async def wait(messages):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    service = RagAgentService(model=ScriptedModel(wait), planner_model=AsyncMock(generate=AsyncMock(
        return_value='{"steps":["Work"],"reason":null}')), execution_mode='team',
        step_timeout_seconds=0.05, mcp_servers=(), include_knowledge=False, memory_directory=str(tmp_path))
    try:
        result = await asyncio.wait_for(service.query_result('work', 's'), 3)
        assert result.status is TaskStatus.FAILED and result.replan_count == 0
        assert cancelled.is_set() and not service._team_store.states
    finally:
        await service.aclose()


async def test_stale_parent_during_team_model_never_imports_private_evidence():
    from unittest.mock import Mock
    from app.agent.team.coordinator import TeamCoordinator
    from app.agent.team.models import TeamOptions, TeamStore, TeamEnvironment
    from app.models.working_memory import StaleExecutionError
    from test_runner_routing import context
    entered, release = asyncio.Event(), asyncio.Event()
    valid = [True]
    async def hold(messages):
        entered.set()
        await release.wait()
        return action('late', 'fail', instruction='old task')
    ctx = context()
    importer = Mock()
    state = TeamStore().get(ctx, TeamOptions())
    environment = TeamEnvironment('u', None, importer, lambda c: lambda: valid[0])
    task = asyncio.create_task(TeamCoordinator(state, ScriptedModel(hold), environment, ctx).run())
    await entered.wait()
    valid[0] = False
    release.set()
    with pytest.raises(StaleExecutionError):
        await task
    importer.assert_not_called()


async def test_external_cancellation_poisoned_team_cannot_replay(tmp_path):
    from unittest.mock import AsyncMock
    from app.services.rag_agent_service import RagAgentService
    entered, cancelled = asyncio.Event(), asyncio.Event()
    async def hold(messages):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    service = RagAgentService(model=ScriptedModel(hold), planner_model=AsyncMock(generate=AsyncMock(
        return_value='{"steps":["Work"],"reason":null}')), execution_mode='team',
        mcp_servers=(), include_knowledge=False, memory_directory=str(tmp_path))
    try:
        task = asyncio.create_task(service.query_result('work', 's'))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set() and not service._team_store.states
    finally:
        await service.aclose()


async def test_host_business_rejection_returns_to_lead_for_reviewed_rework():
    from app.agent.team.coordinator import TeamCoordinator
    from app.agent.team.models import TeamOptions, TeamStore, TeamEnvironment
    from app.agent.team.contracts import ContractError
    from test_runner_routing import context
    from app.tools.tool_registry import ToolRegistry
    def host_check(payload):
        if payload['answer'] != 'correct':
            raise ContractError('wrong facts')
    @asynccontextmanager
    async def tools(memory, manager):
        yield ToolRegistry()
    model = ScriptedModel(
        action('l1', 'create_worker', 'writer', instruction='draft'),
        action('l2', 'run_member', 'writer'),
        call('wrong', 'submit_result', payload={'answer': 'wrong'}),
        action('l3', 'create_reviewer', 'reviewer', instruction='check'),
        action('l4', 'review', 'reviewer', 'writer', version=1),
        call('r1', 'submit_result', payload={'candidate_version': 1, 'decision': 'approve', 'feedback': 'mistaken approval'}),
        action('l5', 'finish', 'writer', version=1),
        action('l6', 'assign', 'writer', instruction='fix facts', version=1),
        action('l7', 'run_member', 'writer'),
        call('correct', 'submit_result', payload={'answer': 'correct'}),
        action('l8', 'review', 'reviewer', 'writer', version=2),
        call('r2', 'submit_result', payload={'candidate_version': 2, 'decision': 'approve', 'feedback': 'checked new version'}),
        action('l9', 'finish', 'writer', version=2))
    ctx = context()
    options = TeamOptions(
        output_contract=JsonContract('host', (('answer', str),), host_check))
    state = TeamStore().get(ctx, options)
    output = await TeamCoordinator(state, model, TeamEnvironment('u', tools, lambda m: ()), ctx).run()
    assert json.loads(output.content)['answer'] == 'correct'
    assert state.members['writer'].session.format_repairs == 0
    assert any('host_rejected' in (m.content or '') for m in model.requests[7])


def test_production_runner_requires_parent_ownership_guard():
    from app.agent.agent_team_runner import AgentTeamRunner
    from app.agent.team.models import TeamOptions, TeamStore, TeamEnvironment
    with pytest.raises(ValueError, match='ownership guard'):
        AgentTeamRunner(ScriptedModel(), TeamOptions(), TeamStore(), TeamEnvironment('u', None, None))


def test_default_host_contract_rejects_empty_answer():
    from app.agent.team.models import TeamOptions
    from app.agent.team.contracts import ContractError
    with pytest.raises(ContractError):
        TeamOptions().output_contract.validate({'answer': '  '})


async def test_contracted_session_declares_json_without_relying_on_user_goal():
    async def provider(messages):
        # DashScope JSON mode rejects messages without an explicit JSON mention.
        assert any('json' in (m.content or '').lower() for m in messages)
        return call('submit', 'submit_result', payload={'answer': 'review delivered'})
    member = TeamSession('reviewer', ScriptedModel(provider), factory(), asyncio.Queue(),
        'Review the code and report findings.', output_contract=JsonContract('answer', (('answer', str),)),
        pause_at_boundary=True)
    result = await member.run()
    assert json.loads(result.content)['answer'] == 'review delivered'


@pytest.mark.parametrize('bad_command', [
    action('bad', 'run_member', 'checker'),
    action('bad', 'review', 'checker', 'writer', version=2),
    action('bad', 'finish', 'writer', version=1),
])
async def test_lead_corrects_rejected_command_without_replaying_completed_work(bad_command):
    from app.agent.team.coordinator import TeamCoordinator
    from app.agent.team.models import TeamOptions, TeamStore, TeamEnvironment
    from test_runner_routing import context
    read, owners = ReadTool(), []
    @asynccontextmanager
    async def tools(memory, manager):
        async with factory(read, owners)() as registry:
            yield registry
    async def correct(messages):
        packet = json.loads(messages[-1].content.split('HOST TEAM EVENT: ', 1)[1])
        assert packet['event']['event'] == 'command_rejected'
        assert packet['event']['reason']
        assert packet['members'][0]['version'] == 1
        assert packet['members'][0]['has_candidate'] is True
        # The invalid action must never start a reviewer or import a result.
        assert packet['members'][1]['status'] == 'created'
        return action('fixed', 'review', 'checker', 'writer', version=1)
    model = ScriptedModel(
        action('l1', 'create_worker', 'writer', instruction='read', tools='read'),
        action('l2', 'run_member', 'writer'),
        call('read'), call('draft', 'submit_result', payload={'answer': '73'}),
        action('l3', 'create_reviewer', 'checker', instruction='verify'),
        bad_command, correct,
        call('review', 'submit_result', payload={'candidate_version': 1, 'decision': 'approve', 'feedback': 'checked'}),
        action('finish', 'finish', 'writer', version=1))
    ctx = context()
    state = TeamStore().get(ctx, TeamOptions(allowed_tools=frozenset({'read'})))
    output = await TeamCoordinator(state, model, TeamEnvironment('u', tools, lambda m: ()), ctx).run()
    assert json.loads(output.content) == {'answer': '73'}
    assert state.status == 'completed' and state.actions == 6
    assert state.budget.tool_calls == 1
    assert len(state.members['writer'].session.tool_results) == 2
    assert len(owners) == 4 and all(owner == asyncio.current_task() for _, owner in owners)


async def test_repeated_command_rejections_exhaust_actions_without_executing_reviewer():
    from app.agent.team.coordinator import TeamCoordinator
    from app.agent.team.models import TeamOptions, TeamStore, TeamEnvironment, TeamError
    from test_runner_routing import context
    ctx = context()
    state = TeamStore().get(ctx, TeamOptions(max_actions=3))
    model = ScriptedModel(
        action('create', 'create_reviewer', 'checker', instruction='verify'),
        action('bad1', 'run_member', 'checker'), action('bad2', 'run_member', 'checker'))
    with pytest.raises(TeamError, match='action limit'):
        await TeamCoordinator(state, model, TeamEnvironment('u', None, None), ctx).run()
    assert state.status == 'failed' and state.actions == 3
    assert state.members['checker'].session.status == 'created'
    assert state.budget.model_calls == 3 and state.budget.tool_calls == 0
