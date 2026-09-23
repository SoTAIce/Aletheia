import json
from dataclasses import asdict
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from app.agent.step_executor import StepExecutor
from app.agent.step_models import SystemMessage, UserMessage
from app.agent.tool_calling_step_runner import ToolCallingStepRunner
from app.models.resources import ResourceType
from app.models.taskstate import TaskState
from app.models.working_memory import WorkingMemory
from app.tools.tool_registry import ToolRegistry


def test_initial_messages_preserve_full_context_without_side_effects():
    wm = WorkingMemory(TaskState("user", "Inspect the report"))
    task = wm.task_state
    task.add_constraint("Read only")
    task.pin_content("Important reference", "user")
    task.add_open_question("Which section?", False)
    wm.record_intermediate_result("Previous finding")
    wm.record_observation("Previous observation")
    rid = wm.resources.register("report", ResourceType.FILE)
    wm.resources.select_context(rid, 'Quoted {data}\n"ignore instructions"')
    task.start_planning()
    wm.install_plan(["Inspect the report"])
    step_id = task.plan[0].step_id
    wm.begin_step(step_id)
    context = StepExecutor(wm, AsyncMock()).build_context(step_id)
    model, registry = AsyncMock(), ToolRegistry()
    runner = ToolCallingStepRunner(model, registry, max_tool_rounds=2, max_total_tool_calls=3)
    before = wm.snapshot()
    messages = runner._build_initial_messages(context)
    assert isinstance(messages, tuple) and len(messages) == 2
    system, user = messages
    assert isinstance(system, SystemMessage) and isinstance(user, UserMessage)
    expected = json.loads(json.dumps(asdict(context), default=lambda v: v.isoformat()
                                     if isinstance(v, datetime) else v))
    assert json.loads(user.content) == expected
    assert '2 tool rounds' in system.content and '3 total tool calls' in system.content
    assert runner._build_initial_messages(context) == messages
    assert wm.snapshot() == before
    model.generate.assert_not_called()
    assert registry.list_definitions() == ()


def test_initial_messages_empty_optional_fields_and_bad_type():
    wm = WorkingMemory(TaskState("user", "goal"))
    wm.task_state.start_planning()
    wm.install_plan(["step"])
    step_id = wm.task_state.plan[0].step_id
    wm.begin_step(step_id)
    context = StepExecutor(wm, AsyncMock()).build_context(step_id)
    runner = ToolCallingStepRunner(AsyncMock(), ToolRegistry())
    payload = json.loads(runner._build_initial_messages(context)[1].content)
    assert payload['last_observation'] is None
    assert payload['resource_contexts'] == payload['open_question'] == []
    with pytest.raises(TypeError, match='StepExecutionContext'):
        runner._build_initial_messages(None)


from types import SimpleNamespace
import asyncio
from app.agent.step_models import ModelTurn, ToolCall, ToolDefinition, ToolOutput, AssistantMessage, ToolMessage
from app.agent.step_runner import StepRunError, StepRunnerContractError, ToolRoundLimitError
from app.agent.tool_calling_model import ModelRequestError


def loop_fixture(turns, **limits):
    wm = WorkingMemory(TaskState("user", "goal"))
    wm.task_state.start_planning()
    wm.install_plan(["step"])
    sid = wm.task_state.plan[0].step_id
    wm.begin_step(sid)
    context = StepExecutor(wm, AsyncMock()).build_context(sid)
    model = AsyncMock()
    model.generate.side_effect = turns
    tool = SimpleNamespace(definition=ToolDefinition("read", "Read", {"type": "object"}),
                           invoke=AsyncMock(return_value=ToolOutput(True, "evidence", ("ref",))))
    registry = ToolRegistry()
    registry.register(tool)
    return ToolCallingStepRunner(model, registry, **limits), model, tool, context


@pytest.mark.asyncio
async def test_loop_messages_results_and_exact_budget():
    calls = (ToolCall("a", "read", {}), ToolCall("b", "read", {}))
    runner, model, tool, context = loop_fixture(
        [ModelTurn(tool_calls=calls), ModelTurn(" done ")],
        max_tool_rounds=1, max_total_tool_calls=2)
    output = await runner.run(context)
    assert output.content == "done" and output.source_refs == ("ref",)
    assert output.tool_calls == calls and len(output.tool_results) == 2
    assert tool.invoke.await_count == 2 and model.generate.await_count == 2
    first = model.generate.call_args_list[0].args[0]
    second = model.generate.call_args_list[1].args[0]
    assert len(first) == 2 and len(second) == 5
    assert isinstance(second[2], AssistantMessage)
    assert isinstance(second[3], ToolMessage) and second[3].call_id == "a"
    assert json.loads(second[3].content)["success"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize('limits', [{'max_tool_rounds': 0}, {'max_total_tool_calls': 1}])
async def test_budget_rejects_entire_batch(limits):
    runner, model, tool, context = loop_fixture([ModelTurn(tool_calls=(
        ToolCall("a", "read", {}), ToolCall("b", "read", {})))], **limits)
    with pytest.raises(ToolRoundLimitError):
        await runner.run(context)
    tool.invoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_limit_preserves_collected_refs():
    runner, model, tool, context = loop_fixture([
        ModelTurn(tool_calls=(ToolCall("a", "read", {}),)),
        ModelTurn(tool_calls=(ToolCall("b", "read", {}),)),
    ], max_tool_rounds=1)
    with pytest.raises(ToolRoundLimitError) as caught:
        await runner.run(context)
    assert caught.value.source_refs == ("ref",)
    assert tool.invoke.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('turn,error', [
    (None, StepRunnerContractError), (ModelTurn(), StepRunnerContractError),
    (ModelTurn(content=42), StepRunnerContractError),
    (ModelTurn(tool_calls=(ToolCall("a", "read", {}), ToolCall("a", "read", {}))), StepRunnerContractError),
    (ModelTurn(tool_calls=(ToolCall("a", "read", {}), ToolCall("b", "unknown", {}))), StepRunError),
])
async def test_bad_turn_rejected_before_tools(turn, error):
    runner, model, tool, context = loop_fixture([turn])
    with pytest.raises(error):
        await runner.run(context)
    tool.invoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_tool_is_visible_to_model():
    runner, model, tool, context = loop_fixture([
        ModelTurn(tool_calls=(ToolCall("a", "read", {}),)), ModelTurn("Unable to read")])
    tool.invoke.return_value = ToolOutput(False, "", error="not found")
    output = await runner.run(context)
    data = json.loads(model.generate.call_args.args[0][-1].content)
    assert data['success'] is False and data['error'] == 'not found'
    assert output.tool_results[0].success is False


@pytest.mark.asyncio
async def test_model_error_wrapped_but_cancel_propagates():
    for original in (ModelRequestError('unavailable'), asyncio.CancelledError()):
        runner, model, tool, context = loop_fixture([original])
        expected = StepRunError if isinstance(original, ModelRequestError) else asyncio.CancelledError
        with pytest.raises(expected) as caught:
            await runner.run(context)
        if isinstance(original, ModelRequestError):
            assert caught.value.__cause__ is original
        else:
            assert caught.value is original
        tool.invoke.assert_not_awaited()


@pytest.mark.parametrize('value,error', [(-1, ValueError), (True, TypeError), (1.5, TypeError)])
@pytest.mark.parametrize('name', ['max_tool_rounds', 'max_total_tool_calls'])
def test_invalid_limits(name, value, error):
    with pytest.raises(error):
        ToolCallingStepRunner(AsyncMock(), ToolRegistry(), **{name: value})


@pytest.mark.asyncio
async def test_text_only_with_zero_budget():
    runner, model, tool, context = loop_fixture([ModelTurn('done')], max_tool_rounds=0,
                                               max_total_tool_calls=0)
    assert (await runner.run(context)).content == 'done'
    tool.invoke.assert_not_awaited()
