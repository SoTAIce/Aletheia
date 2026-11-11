"""Deterministic stage 3a delegation contracts; no external services."""
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.delegation import DelegatingLoopRunner, DelegationOptions, READ_TOOLS
from app.agent.step_models import ModelTurn, ToolMessage, StepOutcome
from app.agent.team.models import TeamError
from app.agent.tool_calling_step_runner import ToolCallingStepRunner
from test_sandbox_step1 import _context
from test_sandbox_step3 import FakeTool, call
from app.tools.tool_registry import ToolRegistry

SANDBOX = DelegationOptions(allowed_tools=READ_TOOLS | {'write_file', 'run_tests', 'run_python'},
                            require_passing_run=True)


def delegate(identity, task='write parse_duration.py with tests', **extra):
    return call(identity, 'delegate', task=task, **extra)


class Seq(FakeTool):
    """Fake tool answering from a list of contents in order."""

    def __init__(self, name, *contents):
        super().__init__(name, contents[0])
        self.contents = list(contents)

    async def invoke(self, arguments):
        self.content = self.contents.pop(0) if len(self.contents) > 1 else self.contents[0]
        return await super().invoke(arguments)


def _registry(*tools):
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


def _sandbox_tools(*runs):
    return (FakeTool('read_file', 'x = 1'), FakeTool('write_file', 'File written successfully.'),
            Seq('run_tests', *(runs or ('exit code 0; 1.0s\n3 passed',))))
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


async def test_service_uses_delegating_loop_factory_and_one_step_runtime(monkeypatch):
    from app.services.rag_agent_service import RagAgentService
    from app.models.taskstate import TaskStatus
    import app.services.rag_agent_service as service_module
    captured = {}
    # Stage 4b: a team task runs in LoopRun with the same factory and limits as before.
    original = service_module.LoopRun
    def runtime(*args, **kwargs):
        instance = original(*args, **kwargs)
        captured.update(kwargs, instance=instance)
        return instance
    monkeypatch.setattr(service_module, 'LoopRun', runtime)
    model = AsyncMock()
    model.generate.return_value = text('answer')
    async with RagAgentService(model=model, planner_model=AsyncMock(), mcp_servers=(),
            include_knowledge=False, memory_directory='', execution_mode='team',
            team_options=DelegationOptions(timeout_seconds=3)) as service:
        result = await service.query_result('Explain this', 'test')
    assert isinstance(captured['instance']._runner.runner, DelegatingLoopRunner)
    assert captured['step_timeout_seconds'] >= 8 and captured['max_replans'] == 0
    assert result.status is TaskStatus.COMPLETED and result.content == 'answer'
    assert result.execution_count == 1 and result.replan_count == 0 and model.generate.await_count == 1
    assert result.step_log[0]['runner'] == 'agent_team'


def test_removed_cli_flags_and_options_are_rejected():
    from app.agent.__main__ import parse_args
    from bench.run_bench import main
    for flag, values in [('--team-impl', ['loop']), ('--team-acceptance', [])]:
        with pytest.raises(SystemExit):
            parse_args(['goal', flag, *values])
        with pytest.raises(SystemExit):
            main([flag, *values])
    for kwargs in ({'acceptance_tests': True}, {'max_verifications': 1}):
        with pytest.raises(TypeError):
            DelegationOptions(**kwargs)


def test_bench_rows_and_child_command_drop_legacy_team_fields(monkeypatch):
    from bench import run_bench
    commands = []
    def launch(cmd, env, timeout):
        commands.append(cmd)
        return SimpleNamespace(stdout='answer\n\nStatus: completed; executions: 1; replans: 0\n',
            stderr='Team trace: {"model_calls": 9, "tool_calls": 8}\n')
    monkeypatch.setattr(run_bench, 'launch_agent', launch)
    task = next(t for t in run_bench.load_tasks(['all']) if t['id'] == 'billing_qa')
    row = run_bench.run_one(task, 'team', 1, 3)
    assert not {'team_impl', 'team_acceptance', 'team_trace', 'team_traces',
                'team_model_calls', 'team_tool_calls'} & row.keys()
    assert '--team-impl' not in commands[0] and '--team-acceptance' not in commands[0]
    assert run_bench.summary([row])['billing_qa/team']['runs'] == 1
    assert run_bench.split_summary([row])['tune/team']['runs'] == 1


@pytest.mark.parametrize('raw,valid', [
    (None, True), ('read_file, run_tests', True), (['read_file'], True),
    (17, False), (['read_file', 3], False), (['write_file'], True), (['admin'], False),
])
def test_grants_keep_argument_contract(raw, valid):
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


async def test_tools_missing_from_the_parent_registry_are_never_granted():
    runner = DelegatingLoopRunner(Scripted(), _registry(FakeTool('read_file', 'x')), SANDBOX)
    assert runner.worker_tools == {'read_file'} and runner.review_tools == {'read_file'}


def test_options_are_validated():
    for bad in ({'max_delegations': 0}, {'review': 1}, {'timeout_seconds': float('inf')},
                {'allowed_tools': {'read_file'}}):
        with pytest.raises((ValueError, TypeError)):
            DelegationOptions(**bad)


def test_cli_builds_delegation_options_for_team_mode():
    from app.agent.__main__ import team_options_for
    options = team_options_for('team', 'w', True, 400)
    assert isinstance(options, DelegationOptions) and options.require_passing_run
    assert {'write_file', 'run_tests', 'run_python'} <= options.allowed_tools and options.timeout_seconds == 400
    assert team_options_for('team', None).allowed_tools == READ_TOOLS
    assert team_options_for('single', 'w', True) is None


async def test_single_step_planner_never_calls_the_planning_model():
    from app.agent.agent_loop import TeamPlanner
    from app.models.taskstate import TaskState
    from app.models.working_memory import WorkingMemory

    class NoModel:
        async def generate(self, prompt):
            raise AssertionError('planning model must not be called')
    memory = WorkingMemory(TaskState('u', 'a long goal ' * 100))
    memory.task_state.start_planning()
    planner = TeamPlanner(NoModel(), max_steps=1)
    proposal = await planner.plan(planner.build_context(memory))
    assert proposal.steps == (TeamPlanner.STEP,) and len(TeamPlanner.STEP) <= planner.max_step_length
    with pytest.raises(ValueError, match='use plan'):
        await planner.replan(planner.build_context(memory))


def test_bench_recognizes_planning_failures(monkeypatch):
    from types import SimpleNamespace
    from bench import run_bench
    monkeypatch.setattr(run_bench, 'launch_agent', lambda cmd, env, timeout: SimpleNamespace(
        stdout='Planning failed: ValueError: steps must contain at most 1 items\n\n'
               'Status: failed; executions: 0; replans: 0\n', stderr=''))
    task = next(t for t in run_bench.load_tasks(['all']) if t['id'] == 'parse_duration')
    row = run_bench.run_one(task, 'team', 1, 5)
    assert row['failure'] == 'Planning failed: ValueError: steps must contain at most 1 items'


def test_v1_and_its_version_switch_are_gone(monkeypatch):
    # D-025: V1 was removed; the command line and the bench no longer have a team version switch.
    import importlib.util
    from types import SimpleNamespace
    from app.agent.__main__ import build_parser
    from bench import run_bench
    with pytest.raises(SystemExit):
        build_parser().parse_args(['goal', '--team-version', '1'])
    for name in ('app.agent.agent_team_runner', 'app.agent.team.coordinator', 'app.agent.team.session',
                 'app.agent.team_poc'):
        assert importlib.util.find_spec(name) is None
    seen = []
    monkeypatch.setattr(run_bench, 'launch_agent', lambda cmd, env, timeout: seen.append(cmd) or SimpleNamespace(
        stdout='x\n\nStatus: failed; executions: 1; replans: 0\n', stderr=''))
    task = next(t for t in run_bench.load_tasks(['all']) if t['id'] == 'parse_duration')
    run_bench.run_one(task, 'team', 1, 5)
    assert '--team-version' not in seen[0]


def test_bench_rows_name_the_static_guard_reason(monkeypatch):
    # Live run 2026-11-05: a delivery was refused by the static guard and the row did not say why.
    from types import SimpleNamespace
    from bench import run_bench
    from bench.graders import static_guard
    assert {static_guard(c) for c in ('import os', 'open("x")', 'x.__class__', 'def (')} == run_bench._GUARD_REASONS
    answer = 'import os\n\ndef parse_duration(text):\n    return 0\n'
    monkeypatch.setattr(run_bench, 'launch_agent', lambda cmd, env, timeout: SimpleNamespace(
        stdout=answer + '\nStatus: completed; executions: 1; replans: 0\n', stderr=''))
    task = next(t for t in run_bench.load_tasks(['all']) if t['id'] == 'parse_duration')
    row = run_bench.run_one(task, 'single', 1, 5)
    assert row['passed'] is False and row['guard_reason'] == 'forbidden_import'


async def test_every_delegation_starts_fresh_and_rework_follows_the_report():
    model = Scripted(delegate('a'), call('w', 'write_file', path='a.py', content='FIRST-ATTEMPT-BODY'),
        call('r', 'run_tests', path='t.py'), text('wrote it'),
        delegate('b', task='read a.py and fix the failing test'), call('w2', 'write_file', path='a.py', content='fixed'),
        call('r2', 'run_tests', path='t.py'), text('fixed'))
    output = await DelegatingLoopRunner(model, _registry(*_sandbox_tools(
        'exit code 1; 1.0s\n1 failed', 'exit code 0; 1.0s\n3 passed')), SANDBOX).run(_context())
    assert output.content == 'fixed'
    assert 'FIRST-ATTEMPT-BODY' not in str(model.requests[5])
    report = reports(model)[0]
    assert report['files_written'] == ['a.py'] and not report['passing_run_after_last_write']
    assert report['last_run'] == {'tool': 'run_tests', 'summary': 'exit code 1; 1.0s'}
    assert report['delegations_remaining'] == SANDBOX.max_delegations - 1


async def test_failed_member_without_writes_preserves_previous_passing_run():
    from app.agent.delegation import _Run
    from app.agent.tool_calling_model import ModelToolArgumentsError
    bad = ModelToolArgumentsError('invalid_json', tool_name='read_file', argument_chars=20)
    model = Scripted(call('w', 'write_file', path='a.py'), call('r', 'run_tests'), text('first'), bad, bad, bad)
    runner = DelegatingLoopRunner(model, _registry(*_sandbox_tools()), SANDBOX)
    run = _Run()
    first = await runner._delegate(run, _context(), 'first', runner.worker_tools, 'worker')
    second = await runner._delegate(run, _context(), 'second', runner.worker_tools, 'worker')
    assert first['passing_run_after_last_write'] and second['passing_run_after_last_write']
    assert second['status'] == 'error' and 'files_written' not in second
    assert second['failure'] == 'StepModelError -> ModelToolArgumentsError'


@pytest.mark.parametrize('late_write', [False, True])
async def test_member_failure_keeps_observed_delivery_and_run_order(late_write):
    from app.agent.tool_calling_model import ToolCallingModelError
    actions = [call('w', 'write_file', path='a.py'), call('r', 'run_tests')]
    if late_write:
        actions.reverse()
    model = Scripted(delegate('d'), *actions, ToolCallingModelError('PRIVATE'), text('answer'))
    # Read-only final handling is tested elsewhere; inspect the failed member's retained evidence directly.
    from app.agent.delegation import _Run
    runner = DelegatingLoopRunner(model, _registry(*_sandbox_tools()), SANDBOX)
    model.items.pop(0)
    report = await runner._delegate(_Run(), _context(), 'work', runner.worker_tools, 'worker')
    assert report['status'] == 'error' and report['files_written'] == ['a.py']
    assert report['last_run'] == {'tool': 'run_tests', 'summary': 'exit code 0; 1.0s'}
    assert report['passing_run_after_last_write'] is (not late_write)
    assert 'host kept' in report['note'] and 'PRIVATE' not in json.dumps(report)


async def test_interrupted_write_stays_unknown_and_resets_the_gate():
    from app.agent.delegation import _Run
    class Raising(FakeTool):
        async def invoke(self, arguments):
            raise ConnectionError('PRIVATE')
    model = Scripted(call('r', 'run_tests'), text('ran'), call('w', 'write_file', path='a.py'))
    runner = DelegatingLoopRunner(model, _registry(Raising('write_file', ''),
        FakeTool('run_tests', 'exit code 0; 1.0s')), SANDBOX)
    run = _Run()
    first = await runner._delegate(run, _context(), 'run', {'run_tests'}, 'worker')
    second = await runner._delegate(run, _context(), 'write', {'write_file'}, 'worker')
    assert first['passing_run_after_last_write'] and not second['passing_run_after_last_write']
    assert second['status'] == 'error' and 'files_written' not in second
    assert runner._state(run)['writes'] == 1


async def test_unknown_lead_tool_and_empty_task_get_host_replies():
    model = Scripted(call('u', 'write_file', path='a.py'), call('d', 'delegate', task=' '), text('answer'))
    await DelegatingLoopRunner(model, _registry(*_sandbox_tools()), SANDBOX).run(_context())
    replies = [m.content for m in model.requests[-1] if isinstance(m, ToolMessage)]
    assert 'tool write_file is not available' in json.loads(replies[0])['error']
    assert replies[1] == 'HOST: task must be a nonempty string.'


async def test_reviewer_session_only_sees_read_tools():
    seen = []
    class Spy(Scripted):
        async def generate(self, messages, tools):
            seen.append(sorted(t.name for t in tools))
            return await super().generate(messages, tools)
    model = Spy(call('v', 'review', focus='check'),
        call('ok', 'review_decision', decision='approve', problems=[]), text('approved'), text('answer'))
    await DelegatingLoopRunner(model, _registry(*_sandbox_tools()), replace(SANDBOX, review=True)).run(_context())
    assert seen[1] == ['read_file', 'review_decision', 'todo']


async def test_lead_argument_errors_are_repaired_then_terminal():
    from app.agent.tool_calling_model import ModelToolArgumentsError
    from app.agent.step_runner import StepModelError
    bad = ModelToolArgumentsError('invalid_json', tool_name='delegate', argument_chars=20)
    runner = DelegatingLoopRunner(Scripted(bad, bad, bad), _registry(), DelegationOptions())
    with pytest.raises(StepModelError):
        await runner.run(_context())
    assert runner.format_repair_count == 2


async def test_team_turn_limit_uses_loop_budget():
    from app.agent.step_runner import ToolRoundLimitError
    runner = DelegatingLoopRunner(Scripted(delegate('a'), text('notes'), delegate('b'), delegate('c'), delegate('d')),
        _registry(), DelegationOptions(max_delegations=1, max_lead_turns=2))
    with pytest.raises(ToolRoundLimitError):
        await runner.run(_context())


import asyncio
from bench import run_bench
from app.agent.planner import Planner
from app.agent.runtime import AgentRuntime
from app.agent.step_models import StepRunOutput
from app.models.taskstate import TaskState, TaskStatus, StepStatus
from app.models.working_memory import WorkingMemory, StaleExecutionError
from app.services.context_manager import ContextManager

def runtime_fixture(**kwargs):
    model, planner_model = AsyncMock(), AsyncMock()
    model.generate.return_value = ModelTurn('{"status":"succeeded","content":"done","reason":null}')
    planner_model.generate.return_value = '{"steps":["Do work"],"reason":null}'
    memory = WorkingMemory(TaskState('u', 'goal'))
    runtime = AgentRuntime(Planner(planner_model), model, memory, ToolRegistry(), **kwargs)
    return runtime, memory, model, planner_model


def fake_factory(run):
    return lambda deps: SimpleNamespace(runner_id='tool_calling', run=run)


def test_init_and_rebind_use_same_factory_inputs_and_atomic_replacement():
    seen = []
    reject = False
    def factory(deps):
        seen.append(deps)
        if reject:
            raise ValueError('binding rejected')
        return SimpleNamespace(runner_id='tool_calling', run=AsyncMock(return_value=StepRunOutput('done')))
    runtime, memory, model, planner = runtime_fixture(runner_factory=factory, max_tool_rounds=2, max_total_tool_calls=3)
    old = runtime._runner
    replacement_model, tools, manager = AsyncMock(), ToolRegistry(), ContextManager()
    reject = True
    with pytest.raises(ValueError):
        runtime.rebind(Planner(planner), replacement_model, tools, context_manager=manager)
    assert runtime._runner is old
    reject = False
    runtime.rebind(Planner(planner), replacement_model, tools, context_manager=manager)
    assert seen[-1].model is replacement_model and seen[-1].tool_registry.inner is tools   # side-effect watch (D-032)
    assert seen[-1].context_manager is manager and seen[-1].limits == seen[0].limits
    assert seen[-1].limits.max_tool_rounds == 2
    assert seen[-1].limits.max_total_tool_calls == 3
    assert old is not runtime._runner


async def test_rebind_after_blocked_uses_new_clients_preserves_counts_and_diagnostics():
    runtime, memory, model, planner = runtime_fixture()
    model.generate.return_value = ModelTurn('{"status":"blocked","content":"Which file?","reason":"Need file"}')
    blocked = await runtime.run()
    assert blocked.status is TaskStatus.BLOCKED
    question = memory.task_state.scratchpad.open_questions[0]
    memory.task_state.resolve_question(question.question_id, 'report.txt')
    replacement = AsyncMock()
    replacement.generate.return_value = ModelTurn('{"status":"succeeded","content":"new client result","reason":null}')
    runtime.rebind(Planner(planner), replacement, ToolRegistry(), context_manager=ContextManager())
    result = await runtime.run()
    assert result.status is TaskStatus.COMPLETED and result.execution_count == 2 and result.replan_count == 0   # answering a question replans without spending the budget (D-033)
    assert model.generate.await_count == 1 and replacement.generate.await_count == 1
    from app.agent.tool_calling_step_runner import ToolCallingStepRunner
    assert isinstance(runtime._runner.runner, ToolCallingStepRunner)
    assert [s['runner'] for s in result.step_log] == ['tool_calling', 'tool_calling']


@pytest.mark.parametrize('late_failure', [False, True])
async def test_cancelled_runner_cannot_commit_even_when_it_swallows_cancellation(late_failure):
    from app.agent.step_runner import StepToolError
    entered, release = asyncio.Event(), asyncio.Event()
    async def run(context):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        if late_failure:
            raise StepToolError('late failed output')
        return StepRunOutput('late success')
    runtime, memory, model, planner = runtime_fixture(runner_factory=fake_factory(run))
    task = asyncio.create_task(runtime.run())
    await entered.wait()
    with pytest.raises(RuntimeError):
        runtime.rebind(Planner(planner), AsyncMock(), ToolRegistry(), context_manager=ContextManager())
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert memory.task_state.status is TaskStatus.FAILED
    assert memory.task_state.plan[0].status is StepStatus.FAILED
    assert memory.task_state.active_step_id is None
    assert memory.task_state.scratchpad.intermediate_results == ()


async def test_timeout_rejects_suppressed_cancellation_result():
    entered = asyncio.Event()
    async def run(context):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return StepRunOutput('must not commit timed-out output')
    runtime, memory, model, planner = runtime_fixture(runner_factory=fake_factory(run),
                                            step_timeout_seconds=.02, max_replans=0)
    result = await runtime.run()
    assert result.status is TaskStatus.FAILED
    assert memory.task_state.scratchpad.intermediate_results == ()
    assert memory.task_state.active_step_id is None


@pytest.mark.parametrize('mode', ['success', 'cancel', 'declared_failure', 'exception', 'invalid_output'])
async def test_stale_output_or_cancel_cleanup_never_fails_new_goal(mode):
    entered, release = asyncio.Event(), asyncio.Event()
    async def run(context):
        entered.set()
        await release.wait()
        if mode == 'declared_failure':
            from app.agent.step_runner import StepToolError
            raise StepToolError('old declared failure')
        if mode == 'exception':
            raise ValueError('old unexpected failure')
        if mode == 'invalid_output':
            return object()
        return StepRunOutput('old goal result')
    runtime, memory, model, planner = runtime_fixture(runner_factory=fake_factory(run))
    task = asyncio.create_task(runtime.run())
    await entered.wait()
    memory.record_failure('Old execution superseded')
    memory.update_goal('new goal')
    before = memory.snapshot()
    if mode == 'cancel':
        task.cancel()
    release.set()
    with pytest.raises(StaleExecutionError):
        await task
    assert memory.snapshot() == before


async def test_service_stale_cleanup_preserves_new_task_and_records_old_turn(tmp_path, monkeypatch):
    import app.services.rag_agent_service as module
    from app.models.conversation_memory import TurnStatus
    entered, release = asyncio.Event(), asyncio.Event()
    captured = []
    class Runtime(AgentRuntime):
        def __init__(self, *args, **kwargs):
            kwargs['runner_factory'] = fake_factory(run)
            super().__init__(*args, **kwargs)
            captured.append(self.memory)
    async def run(context):
        entered.set()
        await release.wait()
        raise ValueError('late invalid implementation result')
    monkeypatch.setattr(module, 'AgentRuntime', Runtime)
    planner = AsyncMock()
    planner.generate.return_value = '{"steps":["Do work"],"reason":null}'
    async with module.RagAgentService(model=AsyncMock(), planner_model=planner, mcp_servers=(),
                                      include_knowledge=False, memory_directory=tmp_path) as service:
        task = asyncio.create_task(service.query_result('original goal', 's', request_id='old'))
        await entered.wait()
        memory = captured[0]
        memory.record_failure('Superseded by the application')
        memory.update_goal('new goal')
        before = memory.snapshot()
        release.set()
        with pytest.raises(StaleExecutionError):
            await task
        assert memory.snapshot() == before
        turn = service._conversations['s'].archive.read_turn('request:old')
        assert turn.status is TurnStatus.FAILED and turn.messages[0].content == 'original goal'


def test_failure_types_descend_nested_groups():
    from app.agent.team.diagnostics import failure_types
    nested = {'type': 'ExceptionGroup', 'exceptions': [
        {'type': 'ExceptionGroup', 'exceptions': [
            {'type': 'ModelToolArgumentsError', 'cause': {'type': 'JSONDecodeError'}}]}]}
    assert failure_types(nested) == 'ExceptionGroup -> ModelToolArgumentsError -> JSONDecodeError'


def test_failed_run_keeps_cli_failure_even_when_a_file_was_written(monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace

    def fake(cmd, env, timeout):
        root = Path(cmd[cmd.index('--filesystem-root') + 1])
        (root / 'parse_duration.py').write_text('def parse_duration(text):\n    return 0\n', encoding='utf-8')
        return SimpleNamespace(stdout='TeamError: team member limit reached\n\nStatus: failed; executions: 1; replans: 0\n',
                               stderr='')
    monkeypatch.setattr(run_bench, 'launch_agent', fake)
    task = next(t for t in run_bench.load_tasks(['all']) if t['id'] == 'parse_duration')
    row = run_bench.run_one(task, 'team', 1, 5, sandbox=True)
    assert row['deliverable_written'] is True
    assert row['failure'] == 'TeamError: team member limit reached'


def test_timeout_keeps_team_headroom_and_partial_file_diagnostics(monkeypatch):
    import subprocess
    from pathlib import Path
    from bench import run_bench
    seen = []
    def launch(cmd, env, timeout):
        seen.append(timeout)
        work = Path(cmd[cmd.index('--filesystem-root') + 1])
        (work / 'parse_duration.py').write_text('def parse_duration(text):\n    return 0\n', encoding='utf-8')
        raise subprocess.TimeoutExpired(cmd, timeout)
    monkeypatch.setattr(run_bench, 'launch_agent', launch)
    task = next(t for t in run_bench.load_tasks(['all']) if t['id'] == 'parse_duration')
    row = run_bench.run_one(task, 'team', 1, 600, sandbox=True, team_timeout=400)
    assert seen == [700] and row['status'] == 'timeout' and row['passed'] is False
    assert row['file_checks_total'] == 21 and row['file_checks_passed'] < 21
    run_bench.run_one(task, 'single', 1, 600, sandbox=True, team_timeout=400)
    assert seen == [700, 600]


def _worker_writes(request):
    from app.agent.step_models import AssistantMessage
    return {c.arguments['path']: c.arguments['content'] for m in request if isinstance(m, AssistantMessage)
            for c in m.tool_calls if c.tool_name == 'write_file'}

def _four_writes():
    return [call(f'w{i}', 'write_file', path=f'{n}.py', content=n * 3000) for i, n in enumerate('abcd', 1)]


async def test_plain_worker_overflows_on_whole_file_writes():
    # Reproduces the live failure: prepare() never shrinks write_file arguments.
    from app.agent.step_runner import StepModelError
    from app.services.context_manager import ContextManager
    model = Scripted(*_four_writes(), call('w5', 'run_tests', path='t.py'), text('done'))
    runner = ToolCallingStepRunner(model, _registry(*_sandbox_tools()), 12, 24, require_step_outcome=True,
                                   context_manager=ContextManager(window_tokens=7000))
    with pytest.raises(StepModelError, match='ContextBudgetError'):
        await runner.run(_context())


async def test_worker_view_keeps_only_the_latest_file_body_when_near_the_window():
    from app.services.context_manager import ContextManager
    model = Scripted(delegate('l1'), *_four_writes(), call('w5', 'run_tests', path='t.py'), text('done'))
    runner = DelegatingLoopRunner(model, _registry(*_sandbox_tools()), SANDBOX, ContextManager(window_tokens=7000))
    output = await runner.run(_context())
    assert output.outcome is StepOutcome.SUCCEEDED and not model.items
    view = _worker_writes(model.requests[5])                  # worker request after the 4th write
    assert view['d.py'] == 'd' * 3000
    for name in 'abc':
        assert view[f'{name}.py'] == '[host: 3000 chars omitted; read_file this path for its current content]'
    record = {c.arguments['path']: c.arguments['content'] for c in output.tool_calls if c.tool_name == 'write_file'}
    assert record == {f'{n}.py': n * 3000 for n in 'abcd'}   # evidence keeps the full arguments


async def test_worker_view_drops_superseded_bodies_first():
    from app.services.context_manager import ContextManager
    model = Scripted(delegate('l1'),
                     call('w1', 'write_file', path='a.py', content='a' * 3000),
                     call('w2', 'write_file', path='a.py', content='b' * 3000),
                     call('w3', 'run_tests', path='t.py'), text('done'))
    runner = DelegatingLoopRunner(model, _registry(*_sandbox_tools()), SANDBOX, ContextManager(window_tokens=6500))
    await runner.run(_context())
    from app.agent.step_models import AssistantMessage
    bodies = [c.arguments['content'] for m in model.requests[3] if isinstance(m, AssistantMessage)
              for c in m.tool_calls if c.tool_name == 'write_file']
    assert bodies == ['[host: 3000 chars omitted; a later write_file replaced this file]', 'b' * 3000]


async def test_strong_tier_also_drops_old_code_arguments_and_long_outputs():
    # Live run: strong compaction of write bodies alone still overflowed (run_python code, full outputs).
    from app.agent.step_models import AssistantMessage
    from app.services.context_manager import ContextManager
    big_out = 'exit code 0; 1.0s\n' + 'o' * 3000
    tools = (FakeTool('read_file', 'x'), FakeTool('write_file', 'ok'), FakeTool('run_tests', big_out),
             FakeTool('run_python', big_out))
    model = Scripted(delegate('l1'),
                     call('w1', 'run_python', code='p' * 3000),
                     call('w2', 'run_python', code='q' * 3000),
                     call('w3', 'run_python', code='r' * 3000),
                     call('w4', 'write_file', path='a.py', content='a' * 3000),
                     call('w5', 'run_tests', path='t.py'), text('done'))
    # A window that lands on the strong tier; 7000 became too small when the worker prompt grew (D-043).
    runner = DelegatingLoopRunner(model, _registry(*tools), SANDBOX, ContextManager(window_tokens=7500))
    output = await runner.run(_context())
    assert output.outcome is StepOutcome.SUCCEEDED and not model.items
    request = model.requests[5]                                  # after the write, before run_tests
    codes = [c.arguments.get('code') for m in request if isinstance(m, AssistantMessage)
             for c in m.tool_calls if c.tool_name == 'run_python']
    assert codes == ['[host: 3000 chars omitted from an earlier call]'] * 3
    assert _worker_writes(request) == {'a.py': 'a' * 3000}      # the latest turn stays whole
    assert [c.arguments['code'] for c in output.tool_calls if c.tool_name == 'run_python'] == [
        'p' * 3000, 'q' * 3000, 'r' * 3000]                      # evidence keeps full arguments


async def test_default_factory_preserves_exact_output_and_model_messages():
    from app.agent.runtime import _tool_calling_factory
    from app.agent.step_runner import RunnerDependencies, RunnerLimits
    from app.agent.tool_calling_step_runner import ToolCallingStepRunner
    from app.agent.step_models import ModelTurn, ToolCall, ToolDefinition, ToolOutput
    from app.services.context_manager import ContextManager
    from app.tools.tool_registry import ToolRegistry
    class Tool:
        definition = ToolDefinition('lookup', 'Lookup', {'type': 'object'})
        async def invoke(self, args):
            return ToolOutput(True, 'fixed evidence')
    tools = ToolRegistry()
    tools.register(Tool())
    replies = [ModelTurn(tool_calls=(ToolCall('lookup-once', 'lookup', {}),)),
               ModelTurn('{"status":"succeeded","content":"fixed result","reason":null}')]
    a, b = AsyncMock(), AsyncMock()
    a.generate.side_effect, b.generate.side_effect = list(replies), list(replies)
    direct = ToolCallingStepRunner(a, tools, 2, 3, require_step_outcome=True, context_manager=ContextManager())
    routed = _tool_calling_factory(RunnerDependencies(b, tools, ContextManager(), RunnerLimits(2, 3)))
    ctx = _context()
    assert await direct.run(ctx) == await routed.run(ctx)
    assert a.generate.await_args_list == b.generate.await_args_list


@pytest.mark.parametrize('args', [(True, 1), (1, False), (-1, 1), (1, -1)])
def test_limits_keep_original_strict_validation(args):
    from app.agent.step_runner import RunnerLimits
    with pytest.raises((TypeError, ValueError)):
        RunnerLimits(*args)


async def test_format_repair_count_comes_from_current_runner():
    def factory(deps):
        return SimpleNamespace(runner_id='agent_loop', format_repair_count=7,
                               run=AsyncMock(return_value=StepRunOutput('done')))
    runtime, memory, model, planner = runtime_fixture(runner_factory=factory)
    assert runtime.format_repair_count == runtime._planner.format_repair_count + 7


async def test_team_outer_timeout_does_not_replay_without_tool_calls():
    async def run(context):
        await asyncio.Event().wait()
    runtime, memory, model, planner = runtime_fixture(step_timeout_seconds=.01,
        runner_factory=lambda deps: SimpleNamespace(runner_id='agent_team', run=run))
    result = await runtime.run()
    assert result.status is TaskStatus.FAILED and result.failure_kind == 'timeout'
    assert result.execution_count == 1 and result.replan_count == 0
    assert result.step_log[0]['runner'] == 'agent_team'


async def test_old_binding_is_invalid_after_rebind():
    """Migrated from tests/agent/test_runtime_routing.py: the guard now lives in runtime._BoundRunner."""
    from app.models.working_memory import StaleExecutionError
    runtime, memory, model, planner = runtime_fixture()
    old = runtime._runner
    runtime.rebind(Planner(planner), AsyncMock(), ToolRegistry(), context_manager=ContextManager())
    with pytest.raises(StaleExecutionError):
        await old.run(None)


@pytest.mark.parametrize('late_failure', [False, True])
async def test_a_binding_invalidated_while_running_discards_its_late_result(late_failure):
    """Migrated from tests/agent/test_runner_routing.py (late success or failure of an invalidated binding)."""
    from app.agent.runtime import _BoundRunner
    from app.agent.step_runner import StepToolError
    from app.models.working_memory import StaleExecutionError
    entered, release = asyncio.Event(), asyncio.Event()
    async def run(context):
        entered.set()
        await release.wait()
        if late_failure:
            raise StepToolError('late failure')
        return StepRunOutput('late success')
    bound = _BoundRunner(SimpleNamespace(runner_id='agent_loop', run=run))
    task = asyncio.create_task(bound.run(None))
    await entered.wait()
    bound.invalidate()
    release.set()
    with pytest.raises(StaleExecutionError):
        await task
    assert bound.runner_id == 'agent_loop'
