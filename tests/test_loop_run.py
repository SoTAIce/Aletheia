"""LoopRun's public boundary and serialization contract."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.agent_loop import AgentLoopRunner, LoopOptions, LOOP_STEP
from app.agent.loop_run import LoopRun
from app.agent.step_models import ModelTurn, StepRunOutput
from app.agent.step_runner import StepRunError
from app.models.run_state import RunMemory, RunState
from app.models.taskstate import TaskStatus
from app.tools.tool_registry import ToolRegistry
from app.services.context_manager import ContextManager


def make(**kwargs):
    model = SimpleNamespace(generate=AsyncMock(return_value=ModelTurn('answer')))
    registry = ToolRegistry()
    factory = lambda d: AgentLoopRunner(d.model, d.tool_registry, LoopOptions(), d.context_manager)
    options = dict(runner_factory=factory, step_description=LOOP_STEP)
    options.update(kwargs)
    return LoopRun(RunMemory(RunState('user', 'goal')), model, registry, **options), model


@pytest.mark.parametrize('name,values', [
    ('max_executions', [False, True, 0, -1, 1.5, None, '2']),
    ('step_timeout_seconds', [False, True, 0, -1, float('inf'), float('nan'), None, '2']),
    ('max_tool_rounds', [False, True, -1, 1.5, None, '2']),
    ('max_total_tool_calls', [False, True, -1, 1.5, None, '2']),
])
def test_validation_matches_runtime(name, values):
    # AgentRuntime's errors for the same values, recorded before stage 4c deleted it.
    import json
    from pathlib import Path
    recorded = json.loads((Path(__file__).parent / 'fixtures' / 'loop_run_legacy_validation.json')
                          .read_text(encoding='utf-8'))
    for value in values:
        kind, message = recorded[f'{name}={value!r}']
        with pytest.raises({'ValueError': ValueError, 'TypeError': TypeError}[kind]) as new:
            make(**{name: value})
        assert type(new.value).__name__ == kind and str(new.value) == message


@pytest.mark.parametrize('kwargs', [
    {'max_executions': 1},
    {'step_timeout_seconds': 0.01},
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
            runtime.rebind(model, ToolRegistry(), context_manager=ContextManager())
    runtime.memory.task_state.start('active')
    with pytest.raises(RuntimeError, match='cannot rebind a running task'):
        runtime.rebind(model, ToolRegistry(), context_manager=ContextManager())


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


