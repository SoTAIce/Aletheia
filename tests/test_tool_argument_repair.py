"""Deterministic tests for recoverable tool-argument errors (capability-phase1-001).

Undecodable tool-call arguments must surface as a typed, text-free error and be
repaired a bounded number of times by both the single runner and Team Sessions.
No real model, MCP, DB or network.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.openai_compatible_tool_calling_model import OpenAICompatibleToolCallingModel
from app.agent.step_executor import StepExecutor
from app.agent.step_models import ModelTurn, SystemMessage, ToolDefinition, ToolOutput, UserMessage
from app.agent.step_runner import StepModelError
from app.agent.team.diagnostics import failure_details
from app.agent.team.session import TeamSession
from app.agent.tool_calling_model import (
    ModelRequestError, ModelResponseError, ModelToolArgumentsError, tool_argument_repair_instruction,
)
from app.agent.tool_calling_step_runner import ToolCallingStepRunner
from app.models.taskstate import TaskState
from app.models.working_memory import WorkingMemory
from app.tools.tool_registry import ToolRegistry

SECRET = '<html>"unescaped SECRET-PAYLOAD'


def _response(name, arguments):
    call = SimpleNamespace(type='function', id='call-1',
                           function=SimpleNamespace(name=name, arguments=arguments))
    message = SimpleNamespace(role='assistant', content=None, refusal=None,
                              function_call=None, tool_calls=[call])
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason='tool_calls', message=message)])


def _adapter(response):
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=AsyncMock(return_value=response))))
    return OpenAICompatibleToolCallingModel(client, 'test-model')


@pytest.mark.parametrize('arguments, reason, message', [
    ('{"payload": ' + SECRET, 'invalid_json', 'not valid JSON'),
    ('[1, 2]', 'not_object', 'must decode to a JSON object'),
    (None, 'not_string', 'must be a JSON string'),
])
async def test_adapter_raises_typed_error_without_raw_arguments(arguments, reason, message):
    with pytest.raises(ModelToolArgumentsError, match=message) as caught:
        await _adapter(_response('submit_result', arguments)).generate((UserMessage('go'),), ())
    error = caught.value
    assert isinstance(error, ModelResponseError)
    assert (error.reason, error.tool_name) == (reason, 'submit_result')
    assert error.argument_chars == (None if arguments is None else len(arguments))
    detail = json.dumps(failure_details(error))
    assert 'SECRET' not in str(error) and 'SECRET' not in detail
    assert '"reason": "' + reason + '"' in detail


async def test_adapter_drops_suspicious_tool_name():
    with pytest.raises(ModelToolArgumentsError) as caught:
        await _adapter(_response('bad name; ' + SECRET, '{oops')).generate((UserMessage('go'),), ())
    assert caught.value.tool_name is None
    assert 'SECRET' not in tool_argument_repair_instruction(caught.value)


class Scripted:
    """Each item is a ModelTurn to return or an exception to raise."""

    def __init__(self, *items):
        self.items, self.requests = list(items), []

    async def generate(self, messages, tools):
        self.requests.append(tuple(messages))
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _bad_args():
    return ModelToolArgumentsError('invalid_json', tool_name='read', argument_chars=99)


class ReadTool:
    definition = ToolDefinition('read', 'Read synthetic evidence', {'type': 'object'})

    def __init__(self):
        self.calls = []

    async def invoke(self, args):
        self.calls.append(args)
        return ToolOutput(True, 'evidence')


def _step_context():
    wm = WorkingMemory(TaskState('user', 'goal'))
    wm.task_state.start_planning()
    wm.install_plan(['step'])
    step_id = wm.task_state.plan[0].step_id
    wm.begin_step(step_id)
    return StepExecutor(wm, AsyncMock()).build_context(step_id)


def _runner(model, tool=None, repairs=2):
    registry = ToolRegistry()
    if tool is not None:
        registry.register(tool)
    return ToolCallingStepRunner(model, registry, max_format_repairs=repairs)


async def test_single_runner_repairs_bad_arguments_then_succeeds():
    tool = ReadTool()
    model = Scripted(_bad_args(), ModelTurn('final answer'))
    runner = _runner(model, tool)
    output = await runner.run(_step_context())
    assert output.content == 'final answer'
    assert runner.argument_repair_count == 1 and tool.calls == []
    repair = model.requests[1][-1]
    assert isinstance(repair, SystemMessage) and 'call to read' in repair.content
    assert model.requests[1][:-1] == model.requests[0]


async def test_single_runner_repair_is_bounded_and_reports_type_chain():
    model = Scripted(_bad_args(), _bad_args(), _bad_args())
    runner = _runner(model, ReadTool(), repairs=2)
    with pytest.raises(StepModelError, match='ModelToolArgumentsError') as caught:
        await runner.run(_step_context())
    assert runner.argument_repair_count == 2 and len(model.requests) == 3
    assert isinstance(caught.value.__cause__, ModelToolArgumentsError)


async def test_single_runner_does_not_repair_other_model_errors():
    model = Scripted(ModelRequestError('provider said ' + SECRET))
    with pytest.raises(StepModelError) as caught:
        await _runner(model).run(_step_context())
    assert str(caught.value) == 'Model generation failed: ModelRequestError'
    assert len(model.requests) == 1


def _tools(tool):
    @asynccontextmanager
    async def open_tools():
        registry = ToolRegistry()
        registry.register(tool)
        yield registry
    return open_tools


async def test_team_session_repairs_bad_arguments_then_completes():
    tool, events = ReadTool(), asyncio.Queue()
    model = Scripted(_bad_args(), ModelTurn('done'))
    session = TeamSession('worker', model, _tools(tool), events, 'goal', max_format_repairs=2)
    result = await session.run()
    assert result.content == 'done' and tool.calls == []
    assert (session.argument_repairs, session.format_repairs) == (1, 1)
    kinds = [event.kind for event in session.events]
    assert 'tool_arguments_rejected' in kinds
    repair = model.requests[1][-1]
    assert isinstance(repair, UserMessage) and repair.content.startswith('HOST PROTOCOL REJECTION')


async def test_team_session_repair_is_bounded():
    model = Scripted(_bad_args(), _bad_args())
    session = TeamSession('worker', model, _tools(ReadTool()), asyncio.Queue(), 'goal',
                          max_format_repairs=1)
    with pytest.raises(ModelToolArgumentsError):
        await session.run()
    assert session.status == 'failed' and session.argument_repairs == 1
    assert session.failure['reason'] == 'invalid_json' and session.failure['tool_name'] == 'read'
