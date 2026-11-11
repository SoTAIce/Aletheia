"""Differential contract tests: the installed legacy loop is the oracle."""
import asyncio
from dataclasses import asdict, is_dataclass
from datetime import datetime, UTC
from enum import Enum
import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.agent_loop import AgentLoopRunner, LoopOptions, LoopPlanner, TeamPlanner, LoopResumeStore
from app.agent.runtime import AgentRuntime
from app.agent.step_models import ModelTurn, ToolCall, ToolDefinition, ToolOutput
from app.models.taskstate import TaskState
from app.models.working_memory import WorkingMemory
from app.models.run_state import RunMemory, RunState
from app.tools.tool_registry import ToolRegistry
from app.services.context_manager import ContextManager


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 11, 11, tzinfo=UTC)


@pytest.fixture(autouse=True)
def stable_dates(monkeypatch):
    import app.models.taskstate as legacy
    import app.models.run_state as modern
    monkeypatch.setattr(legacy, 'datetime', FrozenDateTime)
    monkeypatch.setattr(modern, 'datetime', FrozenDateTime)
    import app.agent.loop_run as loop_run
    monkeypatch.setattr(loop_run, 'datetime', FrozenDateTime)


def call(identity, name='read_file', **arguments):
    return ModelTurn(tool_calls=(ToolCall(identity, name, arguments),))


def normalized(value, *, omit=()):
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, dict):
        return {k: normalized(v, omit=omit) for k, v in value.items() if k not in omit}
    if isinstance(value, (list, tuple)):
        return [normalized(v, omit=omit) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, str):
        return re.sub(r'[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}', '<uuid>', value)
    return value


def snapshot(result):
    return {
        'result': [result.status.value, result.content, result.failure_kind,
                   result.execution_count, result.replan_count],
        'log': normalized(result.step_log, omit=('seconds', 'step_id')),
        'receipts': normalized(result.steps, omit=('step_id', 'result_id', 'state_version')),
    }


EVENTS = {'task_start', 'plan', 'step', 'task_end', 'scope_start', 'scope_end'}
EVENT_OMIT = ('span_id', 'parent_id', 't', 'seconds', 'step_id', 'step_ids', 'task_id')


def build(new, turns, *, mode='loop', options=None, limits=None, invoke=None, wrap=None):
    from app.agent.loop_run import LoopRun
    model = SimpleNamespace(generate=AsyncMock(side_effect=list(turns)))
    from app.agent.hooks import HookedTool
    registry = ToolRegistry()
    for name in ('read_file', 'write_file', 'run_tests', 'run_python'):
        registry.register(HookedTool(SimpleNamespace(
            definition=ToolDefinition(name, name, {'type': 'object'}),
            invoke=AsyncMock(side_effect=invoke or (lambda args: ToolOutput(True, 'tool evidence'))))))
    memory = RunMemory(RunState('user', 'explain the module')) if new else WorkingMemory(TaskState('user', 'explain the module'))
    manager = ContextManager()
    options = options or LoopOptions()
    store = LoopResumeStore()
    tally = {'delegations': 0}
    def assemble(deps):
        if mode == 'explore':
            from app.agent.delegation import ExploreLoopRunner
            return ExploreLoopRunner(deps.model, deps.tool_registry, options, deps.context_manager, store, tally=tally)
        if mode == 'team':
            from app.agent.delegation import DelegatingLoopRunner
            return DelegatingLoopRunner(deps.model, deps.tool_registry, options, deps.context_manager)
        return AgentLoopRunner(deps.model, deps.tool_registry, options, deps.context_manager, store)
    def factory(deps):
        runner = assemble(deps)
        return wrap(runner, deps) if wrap is not None else runner
    planner_type = TeamPlanner if mode == 'team' else LoopPlanner
    kwargs = dict(runner_factory=factory, max_replans=0,
                  step_timeout_seconds=max(60, options.timeout_seconds + 5), context_manager=manager)
    kwargs.update(limits or {})
    if new:
        runtime = LoopRun(memory, model, registry, step_description=planner_type.STEP, **kwargs)
    else:
        runtime = AgentRuntime(planner_type(AsyncMock(), max_steps=1, context_manager=manager), model, memory, registry, **kwargs)
    return runtime, model, store


async def observed_run(runtime, events):
    from app.agent.hooks import current_hooks, using_hooks
    progress = []
    start = len(events)
    with using_hooks(current_hooks().with_observers(SimpleNamespace(observe=events.append))):
        try:
            return await runtime.run(on_event=progress.append)
        finally:
            assert progress == [e for e in events[start:] if e['type'] in ('plan', 'step')]


async def capture(runtime, model, *, resume=False, lose_store=None):
    events, results = [], []
    results.append(snapshot(await observed_run(runtime, events)))
    if resume:
        # An unanswered repeated run must pause without making another model call.
        results.append(snapshot(await observed_run(runtime, events)))
        question = runtime.memory.task_state.scratchpad.open_questions[-1]
        runtime.memory.task_state.resolve_question(question.question_id, 'parser.py')
        if lose_store is not None:
            lose_store._paused = None
        runtime.rebind(LoopPlanner(AsyncMock()), model, runtime._registry, context_manager=ContextManager())
        results.append(snapshot(await observed_run(runtime, events)))
    requests = [normalized({'messages': args.args[0], 'tools': [t.name for t in args.args[1]]})
                for args in model.generate.await_args_list]
    return results, normalized([e for e in events if e['type'] in EVENTS], omit=EVENT_OMIT), requests


@pytest.mark.parametrize('turns,resume', [
    ([ModelTurn('  answer  ')], False),
    ([call('r', path='parser.py'), ModelTurn('answer')], False),
    ([call('q', 'ask_user', question='Which file?'), ModelTurn('answer')], True),
], ids=['plain', 'read-only', 'blocked-resume'])
async def test_initial_equivalence(turns, resume):
    old, om, _ = build(False, turns)
    new, nm, _ = build(True, turns)
    assert await capture(new, nm, resume=resume) == await capture(old, om, resume=resume)
    assert all('available_tools' not in m.content for args in nm.generate.await_args_list
               for m in args.args[0] if m.role == 'user')


from app.agent.step_models import StepRunOutput, StepOutcome, ToolResult
from app.agent.step_runner import (StepRunError, StepBlockedError, StepFormatError, StepModelError, StepToolError,
                                  ToolRoundLimitError, StepBusinessError, StepTimeoutError)
from app.agent.tool_calling_model import ModelRequestError, ModelTimeoutError
from app.agent.run_meter import RunMeter, metering
from app.agent.team.models import TeamError
from app.agent.delegation import DelegationOptions as TeamOptions
from app.agent.hooks import PolicyDeniedError


@pytest.mark.parametrize('scenario', [
    'tool-round-limit', 'unavailable-tool', 'read-error', 'write-error', 'run-error',
    'empty-format', 'provider-error', 'provider-timeout', 'policy-denial',
    'model-contract', 'lost-resume-store', 'execution-limit', 'budget', 'explore',
    'team-read-only', 'team-passing-run', 'team-gate-reject',
])
async def test_model_scenarios(scenario):
    def script():
        kwargs, resume, lose, budget = {}, False, False, False
        turns = [ModelTurn('answer')]
        if scenario == 'tool-round-limit':
            kwargs['options'] = LoopOptions(max_tool_rounds=1)
            turns = [call('a'), call('b'), call('c')]
        elif scenario == 'unavailable-tool':
            turns = [call('a', 'missing'), call('b', 'missing'), call('c', 'missing')]
        elif scenario in ('read-error', 'write-error', 'run-error'):
            turns = [call('a', {'read-error': 'read_file', 'write-error': 'write_file',
                               'run-error': 'run_tests'}[scenario])]
            kwargs['invoke'] = lambda args: (_ for _ in ()).throw(OSError('private'))
        elif scenario == 'empty-format':
            turns = [ModelTurn(''), ModelTurn(''), ModelTurn('')]
        elif scenario == 'provider-error':
            turns = [ModelRequestError('private')]
        elif scenario == 'provider-timeout':
            turns = [ModelTimeoutError('private')]
        elif scenario == 'policy-denial':
            turns = [PolicyDeniedError('test-policy', 'host refusal')]
        elif scenario == 'model-contract':
            turns = [object()]
        elif scenario in ('lost-resume-store', 'execution-limit'):
            turns = [call('q', 'ask_user', question='Which file?'), ModelTurn('answer')]
            resume = True
            lose = scenario == 'lost-resume-store'
            if scenario == 'execution-limit':
                kwargs['limits'] = {'max_executions': 1}
        elif scenario == 'budget':
            budget = True
        elif scenario == 'explore':
            kwargs['mode'] = 'explore'
            turns = [call('r'), call('d', 'delegate', task='find the parser'),
                     call('m', path='parser.py'), ModelTurn('member report'), ModelTurn('answer')]
        elif scenario.startswith('team-'):
            kwargs.update(mode='team', options=TeamOptions(
                allowed_tools=frozenset({'read_file', 'write_file', 'run_tests'}),
                require_passing_run=True))
            if scenario == 'team-read-only':
                turns = [call('d', 'delegate', task='read'), call('r'), ModelTurn('report'), ModelTurn('answer')]
            elif scenario == 'team-passing-run':
                turns = [call('d', 'delegate', task='fix'), call('w', 'write_file', path='a.py'),
                         call('t', 'run_tests'), ModelTurn('report')]
                kwargs['invoke'] = lambda args: ToolOutput(True, 'exit code 0; passed')
            else:
                turns = [call('d', 'delegate', task='fix'), call('w', 'write_file', path='a.py'),
                         ModelTurn('report'), ModelTurn('premature'), ModelTurn('still premature')]
        return turns, kwargs, resume, lose, budget

    outputs = []
    for new in (False, True):
        turns, kwargs, resume, lose, budget = script()
        runtime, model, store = build(new, turns, **kwargs)
        with metering(RunMeter(max_total_tokens=1)) as meter:
            if budget:
                meter.prompt_tokens = 1
            outputs.append(await capture(runtime, model, resume=resume, lose_store=store if lose else None))
    assert outputs[1] == outputs[0]


def override(action):
    def wrap(runner, deps):
        async def run(context):
            return await action(context, deps, runner)
        return SimpleNamespace(run=run, runner_id=runner.runner_id, format_repair_count=0)
    return wrap


@pytest.mark.parametrize('kind', [
    'run-error', 'tool-limit', 'business', 'format', 'model', 'team-error', 'blocked',
    'failed-output', 'invalid-output', 'timeout-read', 'timeout-write', 'timeout-overflow',
    'timeout-team', 'resource-selected', 'stale-result', 'stale-error', 'stale-cancel',
])
async def test_host_boundaries(kind):
    outputs = []
    for new in (False, True):
        holder = {}
        async def action(context, deps, runner):
            holder['context'] = context
            state = holder['runtime'].memory.task_state
            if kind.startswith('stale-'):
                state.fail_task('newer owner')
                if kind == 'stale-error':
                    raise StepRunError('late')
                if kind == 'stale-cancel':
                    raise asyncio.CancelledError()
                return StepRunOutput('late')
            if kind == 'resource-selected':
                from app.models.resources import ResourceType
                resources = holder['runtime'].memory.resources
                rid = resources.register('file.py', ResourceType.FILE)
                resources.select_context(rid, 'selected during execution')
                return await runner.run(context)
            if kind.startswith('timeout-'):
                if kind == 'timeout-read':
                    await deps.tool_registry.invoke(ToolCall('r', 'read_file', {}))
                if kind == 'timeout-write':
                    await deps.tool_registry.invoke(ToolCall('w', 'write_file', {}))
                if kind == 'timeout-overflow':
                    deps.context_manager.overflow_recovery_interrupted = True
                raise TimeoutError()
            if kind == 'failed-output':
                return StepRunOutput(' details ', observation=' evidence ', outcome=StepOutcome.FAILED, reason=' reason ')
            if kind == 'invalid-output':
                return StepRunOutput('answer', source_refs=('missing',))
            error = {'run-error': StepRunError, 'tool-limit': ToolRoundLimitError,
                     'business': StepBusinessError, 'format': StepFormatError,
                     'model': StepModelError, 'team-error': TeamError,
                     'blocked': StepBlockedError}[kind]
            raise error('host detail')
        mode = 'team' if kind == 'timeout-team' else 'loop'
        options = TeamOptions() if mode == 'team' else LoopOptions()
        runtime, model, _ = build(new, [ModelTurn('answer')], mode=mode, options=options, wrap=override(action))
        holder['runtime'] = runtime
        if kind.startswith('stale-'):
            from app.models.working_memory import StaleExecutionError
            events = []
            with pytest.raises(StaleExecutionError):
                await observed_run(runtime, events)
            outputs.append((runtime.memory.task_state.status, runtime.memory.task_state.scratchpad.failure_history[-1].message,
                            normalized(events, omit=EVENT_OMIT), normalized(runtime._step_log, omit=('step_id', 'seconds'))))
        else:
            outputs.append(await capture(runtime, model))
        # Compare the context separately, retaining every field except the deliberately removed tool pin.
        context = asdict(holder['context'])
        context['pinned_contexts'] = [x for x in context['pinned_contexts'] if x['source'] != 'available_tools']
        holder['normalized_context'] = normalized(context)
        if not new:
            legacy_context = holder['normalized_context']
        else:
            assert holder['normalized_context'] == legacy_context
    assert outputs[1] == outputs[0]
    if kind == 'resource-selected':
        assert outputs[0][0][0]['result'][2] == 'internal'
        assert outputs[0][0][0]['result'][1] == 'Execution failed: InvalidStateTransitionError'


@pytest.mark.parametrize('swallow', ['no', 'return', 'raise'])
async def test_cancellation_never_commits(swallow):
    outputs = []
    for new in (False, True):
        entered = asyncio.Event()
        async def action(context, deps, runner):
            await runner.run(context)
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                if swallow == 'return':
                    return StepRunOutput('late')
                if swallow == 'raise':
                    raise StepRunError('late failure')
                raise
        runtime, model, _ = build(new, [ModelTurn('answer')], wrap=override(action))
        events = []
        running = asyncio.create_task(observed_run(runtime, events))
        await asyncio.wait_for(entered.wait(), 2)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        task = runtime.memory.task_state
        assert not runtime._steps
        outputs.append((task.status, task.scratchpad.failure_history[-1].message,
                        normalized(events, omit=EVENT_OMIT),
                        normalized(runtime._step_log, omit=('seconds', 'step_id')),
                        [normalized(a.args[0]) for a in model.generate.await_args_list]))
    assert outputs[1] == outputs[0]


@pytest.mark.parametrize('late_failure', [False, True])
async def test_rebind_invalidates_late_old_binding(late_failure):
    from app.models.working_memory import StaleExecutionError
    outputs = []
    for new in (False, True):
        entered, release = asyncio.Event(), asyncio.Event()
        async def action(context, deps, runner):
            entered.set()
            await release.wait()
            if late_failure:
                raise StepRunError('old failure')
            return StepRunOutput('old answer')
        runtime, model, _ = build(new, [], wrap=override(action))
        # Direct use of an old binding can outlive a legal rebind of an idle runtime.
        old_binding = runtime._runner
        pending = asyncio.create_task(old_binding.run(None))
        await asyncio.wait_for(entered.wait(), 2)
        runtime.rebind(LoopPlanner(AsyncMock()), model, ToolRegistry(), context_manager=ContextManager())
        release.set()
        with pytest.raises(StaleExecutionError) as caught:
            await pending
        outputs.append((str(caught.value), runtime.memory.task_state.status))
    assert outputs[1] == outputs[0]


async def test_approval_wait_is_excluded_from_deadlines():
    from app.api.approvals import ApprovalGate
    from app.agent.hooks import HookSet, using_hooks
    outputs = []
    for new in (False, True):
        pending = SimpleNamespace(approvals={}, add=lambda e: None)
        gate = ApprovalGate(pending, frozenset({'read_file'}), 2)
        runtime, model, _ = build(new, [call('r'), ModelTurn('approved')])
        async def approve():
            async with asyncio.timeout(2):
                while not pending.approvals:
                    await asyncio.sleep(0)
                await asyncio.sleep(0.15)
                next(iter(pending.approvals.values())).set_result('allow')
        approving = asyncio.create_task(approve())
        # The run meter bounds the outer step deadline to 0.08s (the runner/service limits
        # themselves still follow RunAssembler). The 0.15s human wait exceeds that bound.
        with using_hooks(HookSet(gates=(gate,))), metering(RunMeter(timeout_seconds=0.08)):
            outputs.append(await capture(runtime, model))
        await approving
        assert outputs[-1][0][0]['result'][0] == 'completed'
    assert outputs[1] == outputs[0]


async def test_real_team_timeout():
    outputs = []
    for new in (False, True):
        runtime, model, _ = build(new, [], mode='team', options=TeamOptions(timeout_seconds=0.02))
        async def wait(*args):
            await asyncio.Future()
        model.generate.side_effect = wait
        outputs.append(await capture(runtime, model))
        assert outputs[-1][0][0]['result'][2] == 'timeout'
    assert outputs[1] == outputs[0]


async def test_context_fields_and_normalized_receipt():
    from app.models.resources import ResourceType
    contexts, outputs = [], []
    for new in (False, True):
        async def action(context, deps, runner):
            value = asdict(context)
            value['pinned_contexts'] = [x for x in value['pinned_contexts'] if x['source'] != 'available_tools']
            contexts.append(normalized(value))
            await runner.run(context)
            return StepRunOutput(' answer ', observation=' evidence ', source_refs=(f' {resource_id} ',),
                tool_calls=(ToolCall(' c ', ' read_file ', {'path': 'a.py'}),),
                tool_results=(ToolResult(' c ', ' read_file ', True, ' data ',
                                        (f' {resource_id} ',), ' note '),))
        runtime, model, _ = build(new, [ModelTurn('answer')], wrap=override(action))
        state = runtime.memory.task_state
        state.add_constraint('no writes')
        state.pin_content('earlier text', 'conversation_memory')
        resource_id = runtime.memory.resources.register('a.py', ResourceType.FILE)
        runtime.memory.resources.select_context(resource_id, 'resource excerpt')
        outputs.append(await capture(runtime, model))
    assert contexts[1] == contexts[0]
    assert outputs[1] == outputs[0]


async def test_exact_execution_limit_can_still_complete():
    old, om, _ = build(False, [ModelTurn('answer')], limits={'max_executions': 1})
    new, nm, _ = build(True, [ModelTurn('answer')], limits={'max_executions': 1})
    legacy = await capture(old, om)
    assert legacy[0][0]['result'] == ['completed', 'answer', None, 1, 0]
    assert await capture(new, nm) == legacy


async def test_second_execution_limit_check_is_limit_not_internal():
    from app.agent.hooks import HookSet, using_hooks
    outputs = []
    for new in (False, True):
        runtime, model, _ = build(new, [ModelTurn('unused')], limits={'max_executions': 1})
        def at_plan(event):
            if event['type'] == 'plan':
                runtime._execution_count = 1
        with using_hooks(HookSet((SimpleNamespace(observe=at_plan),))):
            outputs.append(await capture(runtime, model))
        assert outputs[-1][0][0]['result'] == ['failed', 'Agent execution limit reached', 'limit', 1, 0]
    assert outputs[1] == outputs[0]


async def test_budget_is_checked_after_successful_step_before_task_completion():
    from app.agent.run_meter import current_meter
    outputs = []
    for new in (False, True):
        async def action(context, deps, runner):
            output = await runner.run(context)
            current_meter().record(SimpleNamespace(prompt_tokens=2, completion_tokens=1))
            return output
        runtime, model, _ = build(new, [ModelTurn('answer')], wrap=override(action))
        with metering(RunMeter(max_total_tokens=3)):
            outputs.append(await capture(runtime, model))
        assert outputs[-1][0][0]['result'][2] == 'budget'
        assert len(outputs[-1][0][0]['receipts']) == 1
    assert outputs[1] == outputs[0]


@pytest.mark.parametrize('output', [
    None,
    StepRunOutput(''),
    StepRunOutput(1),
    StepRunOutput('answer', outcome='succeeded'),
    StepRunOutput('answer', reason='unexpected'),
    StepRunOutput('answer', outcome=StepOutcome.FAILED),
    StepRunOutput('answer', observation=' '),
    StepRunOutput('answer', source_refs=['missing']),
    StepRunOutput('answer', source_refs=(1,)),
    StepRunOutput('answer', tool_calls=[]),
    StepRunOutput('answer', tool_calls=(object(),)),
    StepRunOutput('answer', tool_calls=(ToolCall('', 'read_file', {}),)),
    StepRunOutput('answer', tool_calls=(ToolCall('c', '', {}),)),
    StepRunOutput('answer', tool_calls=(ToolCall('c', 'read_file', {1: 'x'}),)),
    StepRunOutput('answer', tool_results=[]),
    StepRunOutput('answer', tool_results=(object(),)),
    StepRunOutput('answer', tool_results=(ToolResult('c', 'read_file', 1, ''),)),
    StepRunOutput('answer', tool_results=(ToolResult('c', 'read_file', True, 1),)),
    StepRunOutput('answer', tool_results=(ToolResult('c', 'read_file', True, '', error=' '),)),
], ids=['wrong-output', 'empty-content', 'content-type', 'outcome-type', 'success-reason',
        'missing-failure-reason', 'empty-observation', 'refs-list', 'refs-type', 'calls-list',
        'call-type', 'empty-call-id', 'empty-tool-name', 'arguments-key', 'results-list',
        'result-type', 'success-type', 'result-content', 'empty-error'])
async def test_output_validation_matches_legacy(output):
    results = []
    for new in (False, True):
        async def action(context, deps, runner):
            await runner.run(context)
            return output
        runtime, model, _ = build(new, [ModelTurn('answer')], wrap=override(action))
        results.append(await capture(runtime, model))
        assert results[-1][0][0]['result'][2] == 'internal'
    assert results[1] == results[0]
