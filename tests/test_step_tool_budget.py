"""A step whose tool budget is spent gets one chance to report from what it read (D-053)."""

import inspect
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.planner import Planner
from app.agent.step_executor import StepExecutor
from app.agent.step_models import ModelTurn, StepOutcome, SystemMessage, ToolCall, ToolDefinition, ToolOutput, ToolsShownOnly
from app.agent.step_runner import ToolRoundLimitError
from app.agent.tool_calling_step_runner import ToolCallingStepRunner
from app.models.taskstate import TaskState
from app.models.working_memory import WorkingMemory
from app.tools.tool_registry import ToolRegistry


def final(content, status='succeeded', reason=None):
    return ModelTurn(json.dumps({'status': status, 'content': content, 'reason': reason}))


def reads(*ids):
    return ModelTurn(tool_calls=tuple(ToolCall(i, 'read', {'part': i}) for i in ids))


async def run_step(turns, **limits):
    model = AsyncMock()
    model.generate.side_effect = turns
    read = SimpleNamespace(definition=ToolDefinition('read', 'read', {'type': 'object'}),
                           invoke=AsyncMock(side_effect=lambda args: ToolOutput(True, f'text of {args["part"]}')))
    registry = ToolRegistry()
    registry.register(read)
    runner = ToolCallingStepRunner(model, registry, require_step_outcome=True, **limits)
    wm = WorkingMemory(TaskState('user', 'goal'))
    wm.task_state.start_planning()
    wm.install_plan(['explain the module'])
    step_id = wm.task_state.plan[0].step_id
    wm.begin_step(step_id)
    context = StepExecutor(wm, AsyncMock()).build_context(step_id)
    return runner, model, read, context


async def test_a_spent_round_budget_asks_once_for_the_outcome_and_keeps_the_reads():
    runner, model, read, context = await run_step(
        [reads('a'), reads('b'), reads('c'), final('explained from a and b')], max_tool_rounds=2)
    output = await runner.run(context)
    assert output.outcome is StepOutcome.SUCCEEDED and output.content == 'explained from a and b'
    assert [c.call_id for c in output.tool_calls] == ['a', 'b']        # c was refused, not run
    assert read.invoke.await_count == 2
    last = model.generate.await_args_list[-1]
    # The wrap-up turn still lists the tools (same request start, D-057) but they cannot be called.
    assert isinstance(last.args[1], ToolsShownOnly) and len(last.args[1]) == len(model.generate.await_args_list[0].args[1])
    messages = last.args[0]
    assert isinstance(messages[-1], SystemMessage) and 'No more tools in this step' in messages[-1].content
    refused = json.loads(messages[-2].content)
    assert refused['success'] is False and 'not executed' in refused['error']


async def test_a_batch_over_the_call_budget_is_refused_and_the_step_can_still_report():
    runner, model, read, context = await run_step(
        [reads('a', 'b'), reads('c', 'd', 'e'), final('partial', 'failed', 'needs c-e')],
        max_tool_rounds=4, max_total_tool_calls=3)
    output = await runner.run(context)
    assert output.outcome is StepOutcome.FAILED and output.reason == 'needs c-e'
    assert read.invoke.await_count == 2


async def test_insisting_after_the_wrap_up_still_fails_the_step():
    runner, model, read, context = await run_step(
        [reads('a'), reads('b'), reads('c')], max_tool_rounds=1)
    with pytest.raises(ToolRoundLimitError):
        await runner.run(context)
    assert read.invoke.await_count == 1


def test_the_planner_prefers_one_step_for_questions():
    source = inspect.getsource(Planner._build_prompt)
    assert '优先只安排一步“读取所需资料并直接回答”' in source
