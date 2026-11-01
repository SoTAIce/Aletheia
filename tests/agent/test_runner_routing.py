import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.step_executor import StepExecutor
from app.agent.step_models import StepRunOutput
from app.models.taskstate import TaskState
from app.models.working_memory import WorkingMemory, StaleExecutionError


def context():
    memory = WorkingMemory(TaskState('u', 'goal'))
    memory.task_state.start_planning()
    memory.install_plan(['Test step'])
    step_id = memory.task_state.plan[0].step_id
    memory.begin_step(step_id)
    return StepExecutor(memory, AsyncMock()).build_context(step_id)


def binding(identity, caps, output):
    from app.agent.runner_registry import BoundRunner, RunnerDefinition
    return BoundRunner(RunnerDefinition(identity, 'Test only', frozenset(caps)),
                       SimpleNamespace(run=AsyncMock(return_value=StepRunOutput(output))))


async def test_trusted_capabilities_are_checked_independently_of_policy_decision():
    from app.agent.runner_router import RunnerRequirements, RouteDecision
    from app.agent.routing_step_runner import RoutingStepRunner
    from app.agent.step_runner import StepRoutingError
    weak = binding('weak', {'text'}, 'must not run')
    strong = binding('strong', {'tool_calling'}, 'must not run')
    malicious = SimpleNamespace(select=lambda *args: RouteDecision('weak', 'I claim no capabilities are needed'))
    runner = RoutingStepRunner((weak, strong), routing_policy=malicious,
                              requirements_provider=lambda ctx: RunnerRequirements(frozenset({'tool_calling'})))
    with pytest.raises(StepRoutingError, match='capabilit'):
        await runner.run(context())
    weak.runner.run.assert_not_awaited()
    strong.runner.run.assert_not_awaited()


async def test_capability_policy_routes_test_doubles_and_records_identity():
    from app.agent.runner_router import RunnerRequirements
    from app.agent.routing_step_runner import RoutingStepRunner
    weak, strong = binding('weak', {'text'}, 'text'), binding('strong', {'tools'}, 'tool result')
    runner = RoutingStepRunner((weak, strong), requirements_provider=lambda ctx: RunnerRequirements(frozenset({'tools'})))
    ctx = context()
    assert (await runner.run(ctx)).content == 'tool result'
    assert runner.decisions[0].runner_id == 'strong'
    assert runner.decisions[0].task_id == ctx.task_id
    assert runner.decisions[0].required_capabilities == frozenset({'tools'})
    weak.runner.run.assert_not_awaited()
    strong.runner.run.assert_awaited_once_with(ctx)


@pytest.mark.parametrize('kind', ['unknown', 'ambiguous', 'bad_decision', 'bad_requirement', 'policy_raises', 'async_policy', 'async_provider'])
async def test_invalid_routes_fail_before_execution(kind):
    from app.agent.runner_router import RunnerRequirements, RouteDecision
    from app.agent.routing_step_runner import RoutingStepRunner
    from app.agent.step_runner import StepRoutingError
    a, b = binding('a', {'test'}, 'a'), binding('b', {'test'}, 'b')
    policy, provider = None, lambda ctx: RunnerRequirements(frozenset({'test'}))
    if kind == 'unknown':
        policy = SimpleNamespace(select=lambda *args: RouteDecision('absent', 'test'))
    elif kind == 'bad_decision':
        policy = SimpleNamespace(select=lambda *args: 'a')
    elif kind == 'bad_requirement':
        provider = lambda ctx: frozenset({'test'})
    elif kind == 'policy_raises':
        policy = SimpleNamespace(select=lambda *args: (_ for _ in ()).throw(ValueError('invalid routing configuration')))
    elif kind == 'async_policy':
        policy = SimpleNamespace(select=AsyncMock(return_value=RouteDecision('a', 'test')))
    elif kind == 'async_provider':
        provider = AsyncMock(return_value=RunnerRequirements(frozenset({'test'})))
    runner = RoutingStepRunner((a, b), routing_policy=policy, requirements_provider=provider)
    with pytest.raises(StepRoutingError):
        await runner.run(context())
    a.runner.run.assert_not_awaited()
    b.runner.run.assert_not_awaited()


async def test_selected_runner_failure_never_falls_back():
    from app.agent.runner_router import RouteDecision
    from app.agent.routing_step_runner import RoutingStepRunner
    from app.agent.step_runner import StepToolError
    a, b = binding('a', {'test'}, 'a'), binding('b', {'test'}, 'b')
    a.runner.run.side_effect = StepToolError('side effect might exist')
    runner = RoutingStepRunner((a, b), routing_policy=SimpleNamespace(select=lambda *args: RouteDecision('a', 'explicit')))
    with pytest.raises(StepToolError):
        await runner.run(context())
    b.runner.run.assert_not_awaited()


@pytest.mark.parametrize('invalidate', [False, True])
@pytest.mark.parametrize('late_failure', [False, True])
async def test_cancelled_or_invalidated_binding_discards_late_success(invalidate, late_failure):
    from app.agent.routing_step_runner import RoutingStepRunner
    entered, release = asyncio.Event(), asyncio.Event()
    async def late(ctx):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()  # Simulate an accidentally cancellation-suppressing Runner.
        if late_failure:
            raise ValueError('late invalid result')
        return StepRunOutput('late stale answer')
    selected = binding('tool_calling', {'tool_calling'}, '')
    selected.runner.run.side_effect = late
    runner = RoutingStepRunner((selected,))
    task = asyncio.create_task(runner.run(context()))
    await entered.wait()
    if invalidate:
        runner.invalidate()
    else:
        task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(StaleExecutionError if invalidate else asyncio.CancelledError):
        await task


async def test_explicit_preference_resolves_capability_ambiguity():
    from app.agent.runner_router import RunnerRequirements, CapabilityRoutingPolicy
    from app.agent.routing_step_runner import RoutingStepRunner
    a, b = binding('a', {'test'}, 'a'), binding('b', {'test'}, 'b')
    runner = RoutingStepRunner((a, b), routing_policy=CapabilityRoutingPolicy(preferred_runner_ids=('b',)),
                              requirements_provider=lambda ctx: RunnerRequirements(frozenset({'test'})))
    assert (await runner.run(context())).content == 'b'
    a.runner.run.assert_not_awaited()


async def test_default_dispatch_preserves_exact_legacy_output_and_model_messages():
    from app.agent.runner_registry import default_runner_registry, RunnerDependencies, RunnerLimits
    from app.agent.routing_step_runner import RoutingStepRunner
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
    routed = RoutingStepRunner(default_runner_registry().bind(RunnerDependencies(b, tools, ContextManager(), RunnerLimits(2, 3))))
    ctx = context()
    assert await direct.run(ctx) == await routed.run(ctx)
    assert a.generate.await_args_list == b.generate.await_args_list
