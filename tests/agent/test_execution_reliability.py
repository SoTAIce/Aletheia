"""Execution reliability (DECISIONS D-031..D-033): envelope repair, unavailable tools,
uncertain side effects, failure kinds, clarification budget and the sandbox step deadline."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.planner import Planner
from app.agent.runtime import AgentRuntime, FAILURE_MESSAGES
from app.agent.step_executor import StepExecutor
from app.agent.step_models import ModelTurn, StepOutcome, ToolCall, ToolDefinition, ToolOutput
from app.agent.step_runner import StepFormatError, StepRunError
from app.agent.tool_calling_step_runner import ToolCallingStepRunner
from app.models.taskstate import TaskState, TaskStatus
from app.models.working_memory import WorkingMemory
from app.tools.tool_registry import ToolRegistry


def final(content="Done", status="succeeded", reason=None):
    return ModelTurn(json.dumps({"status": status, "content": content, "reason": reason}))


def plan(*steps):
    return json.dumps({"steps": list(steps), "reason": None})


def tool(name, *effects):
    return SimpleNamespace(definition=ToolDefinition(name, name, {"type": "object"}),
                           invoke=AsyncMock(side_effect=list(effects)))


def registry(*tools):
    reg = ToolRegistry()
    for item in tools:
        reg.register(item)
    return reg


def runtime_for(turns, plans, reg=None, **limits):
    memory = WorkingMemory(TaskState("user", "Complete the requested goal"))
    model, planner_model = AsyncMock(), AsyncMock()
    model.generate.side_effect = turns
    planner_model.generate.side_effect = plans
    runtime = AgentRuntime(Planner(planner_model), model, memory, reg or ToolRegistry(), **limits)
    return runtime, memory, model


def call(name, call_id="c1", **arguments):
    return ModelTurn(tool_calls=(ToolCall(call_id, name, arguments),))


# ---- D-031: the step-outcome envelope ----

async def run_step(turns, reg=None, **options):
    model = AsyncMock()
    model.generate.side_effect = turns
    runner = ToolCallingStepRunner(model, reg or ToolRegistry(), require_step_outcome=True, **options)
    wm = WorkingMemory(TaskState("user", "goal"))
    wm.task_state.start_planning()
    wm.install_plan(["step"])
    step_id = wm.task_state.plan[0].step_id
    wm.begin_step(step_id)
    context = StepExecutor(wm, AsyncMock()).build_context(step_id)
    return await runner.run(context), runner, model


@pytest.mark.parametrize('reply', [
    '{"total": 42}',                                             # the answer object itself
    '[1, 2, 3]',
    '{"status": "succeeded", "content": "x"}',                   # missing reason
    '{"status": "succeeded", "content": "x", "reason": null, "extra": 1}',
    '{"status": "succeeded", "content": 7, "reason": null}',      # content not text
])
async def test_wrong_shape_is_repaired_once_by_the_model(reply):
    output, runner, model = await run_step([ModelTurn(reply), final('{"total": 42}')])
    assert output.outcome is StepOutcome.SUCCEEDED and output.content == '{"total": 42}'
    assert runner.format_repair_count == 1
    repair = model.generate.call_args_list[1].args[0][-1].content
    assert 'full answer, even when it is JSON' in repair


async def test_a_structured_content_is_serialized_without_a_repair():
    output, runner, _ = await run_step([ModelTurn('{"status": "succeeded", "content": {"total": 42}, "reason": null}')])
    assert json.loads(output.content) == {"total": 42} and runner.format_repair_count == 0


@pytest.mark.parametrize('reply', [
    '{"status": "maybe", "content": "x", "reason": null}',
    '{"status": "succeeded", "content": "x", "reason": "unsure"}',
    '{"status": "failed", "content": "x", "reason": null}',
    '{"status": "failed", "status": "succeeded", "content": "x", "reason": null}',
])
async def test_status_conflicts_are_still_never_repaired(reply):
    with pytest.raises(StepFormatError):
        await run_step([ModelTurn(reply), final()])


async def test_shape_repair_shares_the_existing_budget():
    with pytest.raises(StepFormatError, match='not the step outcome'):
        await run_step([ModelTurn('{"a": 1}')] * 3, max_format_repairs=2)


# ---- D-032: unavailable tools ----

async def test_an_unavailable_tool_is_answered_in_the_same_round():
    reader = tool("read_file", ToolOutput(True, "text"))
    output, _, model = await run_step(
        [call("write_file", path="a"), call("read_file", "c2"), final("read it")],
        registry(reader), unavailable_tool_feedback=True)
    assert output.outcome is StepOutcome.SUCCEEDED and [c.tool_name for c in output.tool_calls] == ["read_file"]
    reply = json.loads(model.generate.call_args_list[1].args[0][-1].content)
    assert reply["success"] is False and "write_file is not available" in reply["error"]
    assert "read_file" in reply["error"]


async def test_insisting_on_an_unavailable_tool_still_fails_the_step():
    with pytest.raises(StepRunError, match='unavailable tool'):
        await run_step([call("write_file", "c1"), call("write_file", "c2"), call("write_file", "c3")],
                       registry(tool("read_file")), unavailable_tool_feedback=True)


async def test_without_feedback_the_old_behaviour_stays():
    with pytest.raises(StepRunError, match='unavailable tool'):
        await run_step([call("write_file")], registry(tool("read_file")))


# ---- D-032: uncertain side effects are not replayed ----

async def test_a_raised_write_fails_the_task_instead_of_replanning():
    writer = tool("write_file", ConnectionError("C:\\Users\\me\\secret path"))
    runtime, memory, _ = runtime_for([call("write_file", path="a")], [plan("Write"), plan("Retry")], registry(writer))
    result = await runtime.run()
    assert result.status is TaskStatus.FAILED and result.failure_kind == 'side_effect'
    assert result.replan_count == 0 and writer.invoke.await_count == 1
    assert 'secret path' not in result.content and 'ConnectionError' in result.content
    assert result.user_message == FAILURE_MESSAGES['side_effect']


async def test_a_raised_read_is_still_replanned():
    reader = tool("read_file", ConnectionError("x"), ToolOutput(True, "ok"))
    runtime, _, _ = runtime_for([call("read_file"), call("read_file", "c2"), final("ok")],
                                [plan("Read"), plan("Read again")], registry(reader))
    result = await runtime.run()
    assert result.status is TaskStatus.COMPLETED and result.replan_count == 1


async def test_a_timeout_after_a_write_is_not_replayed():
    async def slow(arguments):
        await asyncio.sleep(5)
    writer = SimpleNamespace(definition=ToolDefinition("run_tests", "run", {"type": "object"}), invoke=slow)
    runtime, _, _ = runtime_for([call("run_tests")], [plan("Run"), plan("Again")], registry(writer),
                                step_timeout_seconds=0.05)
    result = await runtime.run()
    assert result.status is TaskStatus.FAILED and result.failure_kind == 'side_effect' and result.replan_count == 0


async def test_a_timeout_with_only_reads_is_replanned():
    async def slow(arguments):
        await asyncio.sleep(5)
    reader = SimpleNamespace(definition=ToolDefinition("read_file", "read", {"type": "object"}), invoke=slow)
    runtime, memory, _ = runtime_for([call("read_file"), final("answered")], [plan("Read"), plan("Answer")],
                                     registry(reader), step_timeout_seconds=0.05)
    result = await runtime.run()
    assert result.status is TaskStatus.COMPLETED and result.replan_count == 1
    assert 'only read-only tools' in memory.task_state.scratchpad.failure_history[0].message


# ---- D-033: failure kinds, root cause, clarification budget ----

async def test_the_replan_limit_keeps_the_last_failure_and_its_kind():
    runtime, _, _ = runtime_for([final("no", "failed", "file missing")] * 3,
                                [plan("Try")] * 3, max_replans=1)
    result = await runtime.run()
    assert result.status is TaskStatus.FAILED and result.failure_kind == 'limit'
    assert 'replanning limit reached; last failure:' in result.content and 'file missing' in result.content
    assert result.user_message.startswith('已达到')


async def test_format_and_model_failures_have_their_own_kind():
    runtime, _, _ = runtime_for([ModelTurn('{"status": "maybe", "content": "x", "reason": null}')], [plan("Do")])
    assert (await runtime.run()).failure_kind == 'format'


async def test_completed_results_have_no_failure_kind():
    runtime, _, _ = runtime_for([final("ok")], [plan("Do")])
    result = await runtime.run()
    assert result.status is TaskStatus.COMPLETED and result.failure_kind is None and result.user_message is None


async def test_every_failure_kind_has_a_chinese_message():
    assert all(text and not text.isascii() for text in FAILURE_MESSAGES.values())


async def test_answering_questions_does_not_spend_the_replan_budget():
    blocked = final("need input", "blocked", "which file?")
    runtime, memory, _ = runtime_for([blocked, blocked, final("done")],
                                     [plan("Ask"), plan("Ask again"), plan("Finish")], max_replans=0)
    for answer in ("a.txt", "b.txt"):
        result = await runtime.run()
        assert result.status is TaskStatus.BLOCKED
        question = next(q for q in memory.task_state.scratchpad.open_questions if q.resolved_at is None)
        memory.task_state.resolve_question(question.question_id, answer)
    result = await runtime.run()
    assert result.status is TaskStatus.COMPLETED and result.replan_count == 0


# ---- D-033: the sandbox step deadline ----

def test_the_step_deadline_outlasts_a_sandbox_call():
    from app.agent.__main__ import step_timeout_for
    from app.tools.sandbox.executor import host_call_timeout_seconds
    assert step_timeout_for(False, None) is None
    assert step_timeout_for(True, None) >= host_call_timeout_seconds() + 30
    assert step_timeout_for(True, SimpleNamespace(timeout_seconds=400)) == 405
