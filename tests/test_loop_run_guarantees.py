"""Host guarantees on the loop path that were only tested through the plan path (stage 4c section 13.5).

Each test here replaces one that ran on AgentRuntime, Planner and ToolCallingStepRunner; the original is
named in its docstring. No real model or network; one test starts the local filesystem MCP server.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.agent_loop import CONTEXT_FIELDS, AgentLoopRunner, LOOP_STEP, LoopResumeStore, _context_message
from app.agent.loop_run import LoopRun
from app.agent.run_meter import RunMeter, current_meter, metering, record_model_call
from app.agent.step_models import ModelTurn, SystemMessage, ToolCall, ToolDefinition, ToolOutput
from app.agent.step_runner import StepModelError
from app.agent.tool_calling_model import ModelContextOverflowError, ModelRequestError, ModelToolArgumentsError
from app.models.run_state import RunMemory, RunState
from app.models.taskstate import TaskStatus
from app.services.context_manager import ContextManager
from app.tools.tool_registry import ToolRegistry
from loop_fakes import started_context
from test_tool_argument_repair import ReadTool, Scripted, _bad_args


def loop_factory(deps):
    return AgentLoopRunner(deps.model, deps.tool_registry, None, deps.context_manager, LoopResumeStore())


def loop_run(model, *tools, goal='Complete the requested goal', **kwargs):
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    memory = RunMemory(RunState('user', goal))
    return LoopRun(memory, model, registry, runner_factory=loop_factory, step_description=LOOP_STEP, **kwargs)


def tool(name, *effects):
    return SimpleNamespace(definition=ToolDefinition(name, name, {'type': 'object'}),
                           invoke=AsyncMock(side_effect=list(effects)))


def call(name, call_id='c1', **arguments):
    return ModelTurn(tool_calls=(ToolCall(call_id, name, arguments),))


class Metered:
    """A fake model that reports usage like the provider adapter does."""

    def __init__(self, turns, tokens=(100, 20), delay=0.0):
        self.turns, self.tokens, self.delay = list(turns), tokens, delay

    async def generate(self, messages, tools):
        if self.delay:
            await asyncio.sleep(self.delay)
        record_model_call(SimpleNamespace(prompt_tokens=self.tokens[0], completion_tokens=self.tokens[1]))
        return self.turns.pop(0)


# ---- 1. tool-argument repair in the single loop (was tests/test_tool_argument_repair.py) ----

async def test_the_loop_repairs_bad_arguments_then_succeeds():
    tool_ = ReadTool()
    model = Scripted(_bad_args(), ModelTurn('final answer'))
    registry = ToolRegistry()
    registry.register(tool_)
    runner = AgentLoopRunner(model, registry)
    output = await runner.run(started_context())
    assert output.content == 'final answer'
    assert runner.format_repair_count == 1 and tool_.calls == []
    repair = model.requests[1][-1]
    assert isinstance(repair, SystemMessage) and 'call to read' in repair.content
    assert model.requests[1][:-1] == model.requests[0]


async def test_the_loop_repair_is_bounded_and_reports_the_type_chain():
    model = Scripted(_bad_args(), _bad_args(), _bad_args())
    registry = ToolRegistry()
    registry.register(ReadTool())
    runner = AgentLoopRunner(model, registry)
    with pytest.raises(StepModelError, match='ModelToolArgumentsError') as caught:
        await runner.run(started_context())
    assert runner.format_repair_count == 2 and len(model.requests) == 3
    assert isinstance(caught.value.__cause__, ModelToolArgumentsError)


async def test_the_loop_does_not_repair_other_model_errors():
    model = Scripted(ModelRequestError('provider said <html>"unescaped SECRET-PAYLOAD'))
    with pytest.raises(StepModelError) as caught:
        await AgentLoopRunner(model, ToolRegistry()).run(started_context())
    assert str(caught.value) == 'Model generation failed: ModelRequestError'
    assert len(model.requests) == 1


# ---- 2. the context budget (was tests/agent/test_context_integration.py) ----

async def test_required_context_over_the_budget_fails_before_the_model():
    manager = ContextManager(window_tokens=700, output_tokens=20, safety_tokens=10, token_counter=len)
    model = AsyncMock()
    model.generate.return_value = ModelTurn('done')
    runtime = loop_run(model, goal='goal', context_manager=manager)
    result = await runtime.run()
    assert result.status is TaskStatus.FAILED
    model.generate.assert_not_awaited()


async def test_an_overflow_retry_does_not_run_a_completed_tool_twice(tmp_path):
    from app.services.conversation_archive import ConversationArchive
    reader = tool('read', ToolOutput(True, 'raw evidence ' + 'x' * 20000))
    model = AsyncMock()
    model.generate.side_effect = [call('read', 'read-once'), ModelContextOverflowError('overflow'),
                                  ModelTurn('verified')]
    manager = ContextManager(archive=ConversationArchive(tmp_path, 'user'), tool_result_chars=200)
    result = await loop_run(model, reader, goal='read and answer', context_manager=manager).run()
    assert result.status is TaskStatus.COMPLETED and result.content == 'verified'
    assert result.replan_count == 0 and result.execution_count == 1
    reader.invoke.assert_awaited_once()
    assert len(result.steps[0].tool_results[0].content) > 20000
    retried = model.generate.call_args_list[2].args[0]
    assert 'storage_ref' in retried[-1].content and 'x' * 20000 not in retried[-1].content


# ---- 3. the run meter (was tests/agent/test_observability.py) ----

async def test_results_carry_usage_and_a_step_log_without_tool_text():
    reader = tool('read_file', ToolOutput(True, 'secret file text'))
    runtime = loop_run(Metered([call('read_file', path='private/notes.txt'), call('nonsense_tool', 'c2'),
                                ModelTurn('answer')]), reader)
    result = await runtime.run()
    assert result.status is TaskStatus.COMPLETED and result.content == 'answer'
    assert result.usage['model_calls'] == 3 and result.usage['total_tokens'] == 360
    [entry] = result.step_log
    assert entry['outcome'] == 'succeeded' and entry['runner'] == 'agent_loop' and entry['error'] is None
    assert [c['tool'] for c in entry['tools']] == ['read_file'] and entry['tools'][0]['success'] is True
    text = json.dumps(result.step_log)
    assert 'private' not in text and 'secret' not in text


async def test_a_failed_tool_is_logged_with_its_type_only():
    reader = tool('read_file', ConnectionError('C:\\host\\path'))
    result = await loop_run(Metered([call('read_file')]), reader).run()
    assert result.status is TaskStatus.FAILED and result.failure_kind == 'limit'
    [entry] = result.step_log
    assert entry['outcome'] == 'failed' and entry['tools'][0] == {
        'tool': 'read_file', 'success': False, 'error': 'ConnectionError', 'seconds': entry['tools'][0]['seconds']}
    assert 'host' not in json.dumps(result.step_log)


async def test_the_token_budget_is_checked_before_and_after_the_loop_step_not_inside_it():
    """The plan path stopped between steps (one model call each); a loop step makes all its calls,
    then the run fails on the budget and keeps the step's receipt. Recorded as found in stage 4c."""
    runtime = loop_run(Metered([call('read_file'), call('read_file', 'c2'), ModelTurn('answer')]),
                       tool('read_file', ToolOutput(True, 'a'), ToolOutput(True, 'b')))
    with metering(RunMeter(max_total_tokens=200)):
        result = await runtime.run()
    assert result.usage['model_calls'] == 3
    assert result.status is TaskStatus.FAILED and result.failure_kind == 'budget'
    assert 'token budget reached' in result.content and len(result.steps) == 1


async def test_the_time_budget_bounds_a_slow_step():
    runtime = loop_run(Metered([ModelTurn('late')], delay=5), step_timeout_seconds=30)
    with metering(RunMeter(timeout_seconds=0.2)):
        result = await asyncio.wait_for(runtime.run(), 3)
    assert result.status is TaskStatus.FAILED and result.failure_kind == 'budget'
    assert result.step_log[0]['outcome'] == 'timeout'


async def test_without_a_caller_meter_each_run_is_metered_and_a_finished_task_repeats_its_result():
    runtime = loop_run(Metered([ModelTurn('ok')]))
    result = await runtime.run()
    assert result.usage['model_calls'] == 1 and current_meter() is None
    assert await runtime.run() is result


# ---- 5. a stable request start for the provider's prompt cache (was tests/test_prompt_cache_layout.py) ----

def test_stable_context_fields_come_first_and_a_resumed_run_shares_them():
    memory = RunMemory(RunState('user', '解释 runtime.py'))
    first = started_context(memory, pins=(('{"turns": ["earlier conversation"]}', 'conversation_history'),))
    memory.task_state.block('Which file?', first.current_step.step_id)
    question = memory.task_state.scratchpad.open_questions[0]
    memory.task_state.resolve_question(question.question_id, 'runtime.py')
    second = started_context(memory)
    a, b = _context_message(first), _context_message(second)
    for message in (a, b):
        assert list(json.loads(message)) == [f for f in CONTEXT_FIELDS if f in json.loads(message)]
    assert list(json.loads(b))[:3] == ['task_id', 'active_goal', 'pinned_contexts']
    shared = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
    # The task, goal and pinned conversation stay in front; only the answer that came later differs.
    assert '"pinned_contexts"' in a[:shared] and 'earlier conversation' in a[:shared]
    assert '"resolved_questions"' in b[shared - 40:]


# ---- 6. cancellation with a real MCP session (was tests/agent/test_runtime.py) ----

async def test_cancellation_with_a_real_mcp_session_closes_the_tools(tmp_path):
    from app.tools.tool_loader import local_mcp_servers, open_tool_registry
    memory = RunMemory(RunState('user', 'goal'))
    model = AsyncMock()
    model.generate.side_effect = asyncio.CancelledError()
    async with open_tool_registry(memory, mcp_servers=local_mcp_servers(filesystem_root=tmp_path),
                                  include_knowledge=False) as registry:
        runtime = LoopRun(memory, model, registry, runner_factory=loop_factory, step_description=LOOP_STEP)
        with pytest.raises(asyncio.CancelledError):
            await runtime.run()
    assert memory.task_state.status is TaskStatus.FAILED
    with pytest.raises(RuntimeError, match='closed'):
        await registry.invoke(ToolCall('late', 'read_file', {'path': 'report.txt'}))
