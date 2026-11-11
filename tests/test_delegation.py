"""Deterministic stage 3a delegation contracts; no external services."""
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.delegation import DelegatingLoopRunner, DelegationOptions
from app.agent.step_models import ModelTurn, ToolMessage
from app.agent.team.models import TeamError
from test_sandbox_step1 import _context
from test_sandbox_step3 import FakeTool, call
from test_team_v2 import SANDBOX, _registry, _sandbox_tools, delegate
from test_tool_argument_repair import Scripted


def text(value='done'):
    return ModelTurn(value)


def reports(model):
    request = [r for r in model.requests if 'Team Lead' in r[0].content][-1]
    return [json.loads(m.content) for m in request if isinstance(m, ToolMessage)
            and '"member"' in m.content]


async def test_host_finishes_after_write_and_passing_run():
    model = Scripted(delegate('d'), call('w', 'write_file', path='a.py', content='private'),
                     call('r', 'run_tests', path='t.py'), text('worker report'))
    output = await DelegatingLoopRunner(model, _registry(*_sandbox_tools()), SANDBOX).run(_context())
    assert output.content == 'worker report' and not model.items
    assert [c.call_id for c in output.tool_calls] == ['worker-1/w', 'worker-1/r']
    assert 'Assignment:' in model.requests[1][0].content


async def test_no_writes_leaves_final_answer_to_lead():
    model = Scripted(delegate('d'), text('member notes'), text('requested answer'))
    output = await DelegatingLoopRunner(model, _registry(), SANDBOX).run(_context())
    assert output.content == 'requested answer' and reports(model)[0]['status'] == 'succeeded'


async def test_authorization_rejects_escalation_without_starting_member():
    model = Scripted(delegate('d', tools=['write_file']), text('answer'))
    tool = FakeTool('write_file', 'ok')
    await DelegatingLoopRunner(model, _registry(tool), DelegationOptions()).run(_context())
    assert 'not permitted' in model.requests[-1][-1].content
    assert len(model.requests) == 2


async def test_delegation_limit_is_shared_with_review():
    model = Scripted(delegate('a'), text('notes'), call('b', 'review', focus='check'),
                     text('early'), text('again'))
    with pytest.raises(TeamError):
        await DelegatingLoopRunner(model, _registry(),
            DelegationOptions(max_delegations=1, review=True)).run(_context())
    assert any('delegation limit reached' in m.content for m in model.requests[-1]
               if isinstance(m, ToolMessage))


async def test_failed_member_keeps_observed_write_and_safe_unavailable_tool():
    model = Scripted(delegate('a'), call('w', 'write_file', path='a.py', content='secret'),
                     call('u1', 'missing'), call('u2', 'missing'), call('u3', 'missing'),
                     delegate('b'), call('r', 'run_tests', path='t.py'), text('fixed'))
    output = await DelegatingLoopRunner(model, _registry(*_sandbox_tools()), SANDBOX).run(_context())
    assert output.content == 'fixed'
    report = reports(model)[0]
    assert report['status'] == 'error' and report['files_written'] == ['a.py']
    assert report['unavailable_tool'] == 'missing' and report['failure'] == 'StepRunError'
    assert not report['passing_run_after_last_write']


async def test_lead_cannot_finish_after_unverified_write():
    model = Scripted(delegate('a'), call('w', 'write_file', path='a.py', content='secret'),
                     text('notes'), text('early'), text('again'))
    with pytest.raises(TeamError, match='team acceptance gate not met'):
        await DelegatingLoopRunner(model, _registry(*_sandbox_tools()), SANDBOX).run(_context())
    assert 'HOST:' in model.requests[-1][-1].content


async def test_review_without_explicit_decision_is_revise():
    options = replace(SANDBOX, review=True)
    model = Scripted(delegate('d'), call('w', 'write_file', path='a.py'),
                     call('r', 'run_tests'), text('worker'), call('v', 'review', focus='check'),
                     text('looks fine'), text('early'), text('again'))
    with pytest.raises(TeamError):
        await DelegatingLoopRunner(model, _registry(*_sandbox_tools()), options).run(_context())
    assert reports(model)[-1]['decision'] == 'revise'


async def test_review_approval_finishes_with_worker_report():
    model = Scripted(delegate('d'), call('w', 'write_file', path='a.py'), call('r', 'run_tests'),
                     text('worker'), call('v', 'review', focus='check'),
                     call('decision', 'review_decision', decision='approve', problems=[]), text('reviewer'))
    output = await DelegatingLoopRunner(model, _registry(*_sandbox_tools()),
                                        replace(SANDBOX, review=True)).run(_context())
    assert output.content == 'worker'


async def test_members_have_todo_but_no_ask_user_and_only_granted_tools():
    seen = []
    turns = [delegate('d', tools=['read_file']), text('notes'), text('answer')]
    async def generate(messages, tools):
        seen.append([d.name for d in tools])
        return turns.pop(0)
    model = SimpleNamespace(generate=generate)
    await DelegatingLoopRunner(model, _registry(*_sandbox_tools()), SANDBOX).run(_context())
    assert seen[0] == ['todo', 'delegate']
    assert seen[1] == ['read_file', 'todo']


def test_loop_rejects_independent_acceptance_tests():
    with pytest.raises(ValueError, match='acceptance_tests'):
        DelegatingLoopRunner(Scripted(), _registry(), DelegationOptions(acceptance_tests=True))


async def test_member_calls_keep_trace_parents_and_finish_event_has_no_model_text(tmp_path):
    from app.agent.hooks import HookSet, HookedModel, HookedToolRegistry, trace_scope, using_hooks
    from app.agent.builtin_hooks import TraceFileHook
    from app.agent.trace_view import build_trace
    events = []
    trace = TraceFileHook(tmp_path)
    model = Scripted(delegate('d', tools=['read_file']), call('r', 'read_file', path='a.py'),
                     text('private member text'), text('private lead text'))
    registry = HookedToolRegistry()
    registry.register(FakeTool('read_file', 'private file body'))
    with using_hooks(HookSet((SimpleNamespace(observe=events.append), trace))):
        with trace_scope('step', index=0):
            await DelegatingLoopRunner(HookedModel(model), registry, SANDBOX).run(_context())
        trace.observe({'type': 'task_end', 'task_id': 'test', 'status': 'completed'})
    member = next(e for e in events if e['type'] == 'scope_start' and e['kind'] == 'member')
    children = [e for e in events if e['type'] in ('before_model', 'before_tool')
                and e['parent_id'] == member['span_id']]
    assert len(children) == 3
    finish = [e for e in events if e['type'] == 'before_finish']
    assert len(finish) == 1 and finish[0]['writes'] == 0
    assert finish[0]['parent_id'] == member['parent_id']
    assert 'private' not in json.dumps(events)
    records = [json.loads(line) for line in trace.path.read_text(encoding='utf-8').splitlines()]
    tree = build_trace(records)
    assert tree['totals']['model_calls'] == 4 and tree['totals']['tool_calls'] == 1


async def test_write_after_passing_run_requires_another_run():
    model = Scripted(delegate('d'), call('r', 'run_tests'), call('w', 'write_file', path='a.py'),
                     text('notes'), text('early'), text('again'))
    with pytest.raises(TeamError):
        await DelegatingLoopRunner(model, _registry(*_sandbox_tools()), SANDBOX).run(_context())
    assert reports(model)[0]['passing_run_after_last_write'] is False


async def test_new_run_does_not_reuse_previous_writes_or_review():
    model = Scripted(delegate('d'), call('w', 'write_file', path='a.py'), call('r', 'run_tests'),
                     text('written'), text('read only answer'))
    runner = DelegatingLoopRunner(model, _registry(*_sandbox_tools()), SANDBOX)
    assert (await runner.run(_context())).content == 'written'
    assert (await runner.run(_context())).content == 'read only answer'


async def test_team_timeout_and_cancellation_are_not_member_reports():
    import asyncio
    async def stalled(messages, tools):
        await asyncio.sleep(10)
    runner = DelegatingLoopRunner(SimpleNamespace(generate=stalled), _registry(),
                                  DelegationOptions(timeout_seconds=0.001))
    with pytest.raises(TimeoutError):
        await runner.run(_context())
    async def cancelled(messages, tools):
        raise asyncio.CancelledError
    runner = DelegatingLoopRunner(SimpleNamespace(generate=cancelled), _registry(), DelegationOptions())
    with pytest.raises(asyncio.CancelledError):
        await runner.run(_context())


@pytest.mark.parametrize('impl', ['v2', 'loop'])
async def test_service_selects_team_impl_and_one_step_runtime(impl, monkeypatch):
    from app.services.rag_agent_service import RagAgentService
    from app.models.taskstate import TaskStatus
    import app.services.rag_agent_service as service_module
    from app.agent.team_v2 import DelegatingTeamRunner
    from app.agent.runner_registry import RunnerDependencies, RunnerLimits
    captured = {}
    original = service_module.AgentRuntime
    def runtime(*args, **kwargs):
        captured.update(kwargs)
        return original(*args, **kwargs)
    monkeypatch.setattr(service_module, 'AgentRuntime', runtime)
    model = AsyncMock()
    model.generate.return_value = text('answer') if impl == 'loop' else text(
        '{"status":"succeeded","content":"answer","reason":null}')
    async with RagAgentService(model=model, planner_model=AsyncMock(), mcp_servers=(),
            include_knowledge=False, memory_directory='', execution_mode='team', team_impl=impl,
            team_options=DelegationOptions(timeout_seconds=3)) as service:
        bindings = service._team_registry().bind(RunnerDependencies(model, _registry(), None, RunnerLimits()))
        team = next(b.runner for b in bindings if b.definition.runner_id == 'agent_team')
        assert type(team) is (DelegatingLoopRunner if impl == 'loop' else DelegatingTeamRunner)
        result = await service.query_result('Explain this', 'test')
    assert captured['step_timeout_seconds'] >= 8
    assert captured['max_replans'] == (0 if impl == 'loop' else 2)
    assert result.status is TaskStatus.COMPLETED and result.content == 'answer'
    assert result.execution_count == 1 and result.replan_count == 0 and model.generate.await_count == 1


def test_team_impl_defaults_validation_and_cli():
    from app.services.rag_agent_service import RagAgentService
    from app.agent.__main__ import parse_args
    assert RagAgentService(model=AsyncMock(), mcp_servers=())._team_impl == 'v2'
    assert parse_args(['goal']).team_impl == 'v2'
    assert parse_args(['goal', '--team-impl', 'loop']).team_impl == 'loop'
    with pytest.raises(ValueError, match='team_impl'):
        RagAgentService(team_impl='other')
    with pytest.raises(ValueError, match='acceptance_tests'):
        RagAgentService(team_impl='loop', team_options=DelegationOptions(acceptance_tests=True))


def test_bench_dry_run_records_team_impl(capsys):
    from bench.run_bench import main
    assert main(['--tasks', 'billing_qa', '--modes', 'team', '--team-impl', 'loop']) == 2
    assert 'team_impl=loop' in capsys.readouterr().out
    assert main(['--tasks', 'billing_qa', '--modes', 'team']) == 2
    assert 'team_impl=v2' in capsys.readouterr().out


@pytest.mark.parametrize('impl', ['v2', 'loop'])
def test_bench_rows_and_child_command_record_team_impl(monkeypatch, impl):
    from bench import run_bench
    commands = []
    def launch(cmd, env, timeout):
        commands.append(cmd)
        return SimpleNamespace(stdout='answer\n\nStatus: completed; executions: 1; replans: 0\n', stderr='')
    monkeypatch.setattr(run_bench, 'launch_agent', launch)
    task = next(t for t in run_bench.load_tasks(['all']) if t['id'] == 'billing_qa')
    row = run_bench.run_one(task, 'team', 1, 3, team_impl=impl)
    assert row['team_impl'] == impl
    assert commands[0][commands[0].index('--team-impl') + 1] == impl


@pytest.mark.parametrize('raw,valid', [
    (None, True), ('read_file, run_tests', True), (['read_file'], True),
    (17, False), (['read_file', 3], False), (['write_file'], True), (['admin'], False),
])
def test_grants_keep_v2_argument_contract(raw, valid):
    runner = DelegatingLoopRunner(Scripted(), _registry(*_sandbox_tools()), SANDBOX)
    granted, problem = runner._grant({'tools': raw}, 'worker')
    assert (problem is None) is valid
    if valid:
        assert granted <= SANDBOX.allowed_tools
    reviewer, problem = runner._grant({'tools': ['write_file']}, 'reviewer')
    assert reviewer is None and 'not permitted' in problem


async def test_review_approval_expires_after_a_new_write():
    model = Scripted(delegate('a'), text('first report'),
        call('v1', 'review', focus='check'), call('ok', 'review_decision', decision='approve', problems=[]),
        text('reviewed'), delegate('b'), call('w', 'write_file', path='a.py'), call('r', 'run_tests'),
        text('new report'), text('early'), text('again'))
    with pytest.raises(TeamError, match='team acceptance gate not met'):
        await DelegatingLoopRunner(model, _registry(*_sandbox_tools()), replace(SANDBOX, review=True)).run(_context())


async def test_member_model_error_is_reported_by_type_without_provider_text(caplog):
    from app.agent.tool_calling_model import ToolCallingModelError
    model = Scripted(delegate('d'), ToolCallingModelError('private provider error'), text('answer'))
    await DelegatingLoopRunner(model, _registry(), SANDBOX).run(_context())
    report = reports(model)[0]
    assert report['status'] == 'error' and 'ToolCallingModelError' in report['failure']
    assert 'private provider error' not in json.dumps(report) + caplog.text
