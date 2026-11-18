"""D-085: a read that raises goes back to the model, and each failure ends a task with its own kind.

No real model, network or MCP server: the tools and models here are fakes.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult, ImageContent, Tool, ToolAnnotations

from app.agent.agent_loop import MAX_TOOL_RAISES, AgentLoopRunner, LoopOptions, LOOP_STEP
from app.agent.delegation import MemberSpawner
from app.agent.loop_run import LoopRun
from app.agent.runtime import FAILURE_MESSAGES, _context_overflow
from app.agent.step_models import ModelTurn, ToolCall, ToolDefinition, ToolOutput
from app.agent.step_runner import StepModelError
from app.agent.tool_calling_model import ModelContextOverflowError, ModelRequestError
from app.models.run_state import RunMemory, RunState
from app.models.taskstate import TaskStatus
from app.services.request_fitter import ContextBudgetError
from app.tools.mcp_tool_adapter import MCPToolAdapter
from app.tools.tool_registry import ToolRegistry
from loop_fakes import started_context


def call(call_id, name='read_file', **arguments):
    return ModelTurn(tool_calls=(ToolCall(call_id, name, arguments),))


class Scripted:
    def __init__(self, *turns):
        self.turns, self.requests = list(turns), []

    async def generate(self, messages, tools):
        self.requests.append(list(messages))
        turn = self.turns.pop(0)
        if isinstance(turn, BaseException):
            raise turn
        return turn


def tool(name, *effects):
    return SimpleNamespace(definition=ToolDefinition(name, name, {'type': 'object'}),
                           invoke=AsyncMock(side_effect=list(effects)))


def run(model, *tools):
    registry = ToolRegistry()
    for item in tools:
        registry.register(item)
    factory = lambda deps: AgentLoopRunner(deps.model, deps.tool_registry, None, deps.context_manager)
    return LoopRun(RunMemory(RunState('user', 'goal')), model, registry, runner_factory=factory,
                   step_description=LOOP_STEP)


def test_the_failure_kinds():
    assert {'tool', 'context', 'limit', 'timeout', 'business'} <= set(FAILURE_MESSAGES)
    assert 'planning' not in FAILURE_MESSAGES
    assert not any('规划' in text for text in FAILURE_MESSAGES.values())


async def test_a_read_that_keeps_raising_ends_the_task_as_a_tool_failure():
    reader = tool('read_file', *[OSError('C:/private/path')] * MAX_TOOL_RAISES)
    model = Scripted(*[call(f'c{i}') for i in range(MAX_TOOL_RAISES)], ModelTurn('never'))
    result = await run(model, reader).run()
    assert result.status is TaskStatus.FAILED and result.failure_kind == 'tool'
    assert result.user_message == FAILURE_MESSAGES['tool']
    assert result.content == 'StepToolError: Tool read_file raised OSError' and 'private' not in result.content
    assert reader.invoke.await_count == MAX_TOOL_RAISES and result.execution_count == 1


async def test_one_raising_read_among_good_ones_completes():
    reader = tool('read_file', OSError('x'), ToolOutput(True, 'text'))
    model = Scripted(call('a'), call('b'), ModelTurn('answer'))
    result = await run(model, reader).run()
    assert result.status is TaskStatus.COMPLETED and result.content == 'answer'
    [receipt] = result.steps
    assert [r.success for r in receipt.tool_results] == [False, True]
    assert [c['success'] for c in result.step_log[0]['tools']] == [False, True]


async def test_a_raising_write_is_never_repeated():
    writer = tool('write_file', OSError('x'), ToolOutput(True, 'written'))
    model = Scripted(call('w', 'write_file'), ModelTurn('never'))
    result = await run(model, writer).run()
    assert result.failure_kind == 'side_effect' and writer.invoke.await_count == 1
    assert result.content.endswith('; it will not be repeated') and 'replanning' not in result.content


@pytest.mark.parametrize('errors', [[ContextBudgetError('host')],
                                    # the fitter retries an overflow once with less history (D-052)
                                    [ModelContextOverflowError('provider'), ModelContextOverflowError('again')]])
async def test_a_request_that_does_not_fit_is_a_context_failure(errors):
    result = await run(Scripted(*errors)).run()
    assert result.status is TaskStatus.FAILED and result.failure_kind == 'context'
    assert result.user_message == FAILURE_MESSAGES['context']


async def test_other_provider_errors_stay_model_failures():
    result = await run(Scripted(ModelRequestError('x'))).run()
    assert result.failure_kind == 'model'


def test_the_context_overflow_check_follows_the_cause_chain():
    wrapped = StepModelError('Model generation failed')
    wrapped.__cause__ = ContextBudgetError('host')
    assert _context_overflow(wrapped) and not _context_overflow(StepModelError('other'))
    loop = StepModelError('loop')
    loop.__cause__ = loop
    assert not _context_overflow(loop)


async def test_a_non_text_mcp_result_is_a_read_failure_the_model_sees():
    """MCPToolAdapter raises TypeError for image blocks (text-only V1); the loop carries on."""
    client = SimpleNamespace(call_tool=AsyncMock(return_value=CallToolResult(
        content=[ImageContent(type='image', data='AA==', mimeType='image/png')])))
    adapter = MCPToolAdapter(client, Tool(name='web_search', inputSchema={'type': 'object'},
                                          annotations=ToolAnnotations(readOnlyHint=True)))   # D-087
    model = Scripted(call('s', 'web_search'), ModelTurn('answer without the image'))
    result = await run(model, adapter).run()
    assert result.status is TaskStatus.COMPLETED
    reply = model.requests[1][-1].content
    assert 'web_search raised TypeError' in reply and 'text/JSON' not in reply


async def test_a_team_member_continues_after_a_raising_read():
    reader = tool('read_file', OSError('x'), ToolOutput(True, 'text'))
    registry = ToolRegistry()
    registry.register(reader)
    model = Scripted(call('a'), call('b'), ModelTurn('member report'))
    runner, seen = MemberSpawner(model, registry).build({'read_file'}, LoopOptions(), 'Read the file.')
    output = await runner.run(started_context())
    assert output.content == 'member report'
    assert [s.success for s in seen] == [True]        # only the call that returned is evidence
