"""LoopRun's public boundary and serialization contract."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.agent_loop import AgentLoopRunner, LoopOptions, LoopPlanner
from app.agent.loop_run import LoopRun
from app.agent.runtime import AgentRuntime
from app.agent.step_models import ModelTurn, StepRunOutput
from app.agent.step_runner import StepRunError
from app.models.run_state import RunMemory, RunState
from app.models.taskstate import TaskState, TaskStatus
from app.models.working_memory import WorkingMemory
from app.tools.tool_registry import ToolRegistry
from app.services.context_manager import ContextManager


def make(**kwargs):
    model = SimpleNamespace(generate=AsyncMock(return_value=ModelTurn('answer')))
    registry = ToolRegistry()
    factory = lambda d: AgentLoopRunner(d.model, d.tool_registry, LoopOptions(), d.context_manager)
    options = dict(runner_factory=factory, step_description=LoopPlanner.STEP)
    options.update(kwargs)
    return LoopRun(RunMemory(RunState('user', 'goal')), model, registry, **options), model


@pytest.mark.parametrize('name,values', [
    ('max_executions', [False, True, 0, -1, 1.5, None, '2']),
    ('max_replans', [False, True, -1, 1.5, None, '2']),
    ('step_timeout_seconds', [False, True, 0, -1, float('inf'), float('nan'), None, '2']),
    ('planning_timeout_seconds', [False, True, 0, -1, float('inf'), float('nan'), None, '2']),
    ('max_tool_rounds', [False, True, -1, 1.5, None, '2']),
    ('max_total_tool_calls', [False, True, -1, 1.5, None, '2']),
])
def test_validation_matches_runtime(name, values):
    for value in values:
        registry, model = ToolRegistry(), AsyncMock()
        with pytest.raises((ValueError, TypeError)) as old:
            AgentRuntime(LoopPlanner(AsyncMock()), model, WorkingMemory(TaskState('u', 'g')),
                         registry, **{name: value})
        with pytest.raises(type(old.value)) as new:
            make(**{name: value})
        assert str(new.value) == str(old.value)


@pytest.mark.parametrize('kwargs', [
    {'max_executions': 1, 'max_replans': 0},
    {'step_timeout_seconds': 0.01, 'planning_timeout_seconds': 1},
    {'max_tool_rounds': 0, 'max_total_tool_calls': 0},
])
def test_valid_limits(kwargs):
    runtime, _ = make(**kwargs)
    assert isinstance(runtime.memory, RunMemory)


def test_conversation_is_not_accepted():
    with pytest.raises(TypeError, match='conversation'):
        make(conversation=None)


async def test_rebind_rejects_lock_or_running_state():
    runtime, model = make()
    async with runtime._lock:
        with pytest.raises(RuntimeError, match='cannot rebind a running task'):
            runtime.rebind(None, model, ToolRegistry(), context_manager=ContextManager())
    runtime.memory.task_state.start('active')
    with pytest.raises(RuntimeError, match='cannot rebind a running task'):
        runtime.rebind(None, model, ToolRegistry(), context_manager=ContextManager())


async def test_concurrent_runs_serialize_and_reuse_final_result():
    entered, release = asyncio.Event(), asyncio.Event()
    active = calls = 0
    async def run(context):
        nonlocal active, calls
        active += 1
        calls += 1
        assert active == 1
        entered.set()
        await release.wait()
        active -= 1
        return StepRunOutput('answer')
    runtime, _ = make(runner_factory=lambda d: SimpleNamespace(run=run, runner_id='agent_loop'))
    first = asyncio.create_task(runtime.run())
    await asyncio.wait_for(entered.wait(), 2)
    second = asyncio.create_task(runtime.run())
    await asyncio.sleep(0)
    assert not second.done()
    release.set()
    one, two = await asyncio.wait_for(asyncio.gather(first, second), 2)
    assert one is two
    assert calls == 1
    assert one.status is TaskStatus.COMPLETED


async def test_format_repairs_and_rebind_ignore_planner():
    runtime, model = make()
    model.generate.side_effect = [ModelTurn(''), ModelTurn('answer')]
    assert (await runtime.run()).content == 'answer'
    assert runtime.format_repair_count == 1
    previous = runtime._runner
    runtime.rebind(object(), model, ToolRegistry(), context_manager=ContextManager())
    assert runtime.format_repair_count == 0
    from app.models.working_memory import StaleExecutionError
    with pytest.raises(StaleExecutionError):
        await previous.run(None)


async def test_planning_compatibility_parameters_do_not_enable_replay():
    async def run(context):
        raise StepRunError('cannot finish')
    runtime, _ = make(max_replans=3, planning_timeout_seconds=0.001,
                      runner_factory=lambda d: SimpleNamespace(run=run, runner_id='agent_loop'))
    result = await runtime.run()
    assert result.execution_count == 1
    assert result.replan_count == 0
    assert result.failure_kind == 'limit'
    assert result.content == 'Agent replanning limit reached; last failure: cannot finish'
