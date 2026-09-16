import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.step_models import ToolCall, ToolDefinition, ToolOutput, ToolResult
from app.tools.tool_registry import ToolRegistry


def make_tool(name='read'):
    return SimpleNamespace(
        definition=ToolDefinition(name, 'Read a file', {}),
        invoke=AsyncMock(return_value=ToolOutput(True, 'result')),
    )


def test_registration_lookup_and_definition_order():
    registry = ToolRegistry()
    assert registry.list_definitions() == ()
    first, second = make_tool(), make_tool('other')
    registry.register(first)
    registry.register(second)
    assert registry.get(' read ') is first
    assert registry.list_definitions() == (first.definition, second.definition)
    assert registry.definition() == registry.list_definitions()
    with pytest.raises(ValueError, match='already registered'):
        registry.register(make_tool())
    assert registry.get('read') is first
    with pytest.raises(KeyError, match='unknown tool'):
        registry.get('missing')


@pytest.mark.parametrize('name,error', [(None, TypeError), (1, TypeError),
                                       ('', ValueError), ('  ', ValueError)])
def test_lookup_rejects_invalid_names(name, error):
    with pytest.raises(error):
        ToolRegistry().get(name)


@pytest.mark.parametrize('changes,error', [
    ({'name': ''}, ValueError), ({'name': ' read '}, ValueError),
    ({'name': None}, ValueError), ({'description': ' '}, ValueError),
    ({'description': None}, ValueError), ({'input_schema': []}, TypeError),
])
def test_invalid_registration_does_not_change_registry(changes, error):
    registry = ToolRegistry()
    tool = make_tool()
    tool.definition = replace(tool.definition, **changes)
    with pytest.raises(error):
        registry.register(tool)
    assert registry.list_definitions() == ()


def test_registration_rejects_missing_contract_and_name_drift():
    registry = ToolRegistry()
    for tool in (None, object(), SimpleNamespace(definition='invalid'),
                 SimpleNamespace(definition=make_tool().definition, invoke=None)):
        with pytest.raises(TypeError):
            registry.register(tool)
    tool = make_tool()
    registry.register(tool)
    tool.definition = replace(tool.definition, name='changed')
    with pytest.raises(ValueError, match='definition changed'):
        registry.get('read')
    with pytest.raises(ValueError, match='definition changed'):
        registry.list_definitions()


@pytest.mark.asyncio
@pytest.mark.parametrize('success,error', [(True, None), (False, ' failed ')])
async def test_invoke_wraps_output_and_normalizes_identifiers(success, error):
    registry, tool = ToolRegistry(), make_tool()
    original = ToolOutput(success, ' raw\n', (' resource ',), error)
    tool.invoke.return_value = original
    registry.register(tool)
    call = ToolCall(' call ', ' read ', {'path': ' report '})
    result = await registry.invoke(call)
    assert result == ToolResult('call', 'read', success, ' raw\n', ('resource',),
                                None if success else 'failed')
    tool.invoke.assert_awaited_once_with({'path': ' report '})
    assert original.source_refs == (' resource ',)
    assert call.call_id == ' call '


@pytest.mark.asyncio
@pytest.mark.parametrize('call,error', [
    (None, TypeError), (ToolCall('', 'read', {}), ValueError),
    (ToolCall(1, 'read', {}), TypeError), (ToolCall('c', ' ', {}), ValueError),
    (ToolCall('c', 'missing', {}), KeyError), (ToolCall('c', 'read', []), TypeError),
    (ToolCall('c', 'read', {1: 'x'}), TypeError),
])
async def test_bad_calls_do_not_invoke_tool(call, error):
    registry, tool = ToolRegistry(), make_tool()
    registry.register(tool)
    with pytest.raises(error):
        await registry.invoke(call)
    tool.invoke.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('output,error', [
    (None, TypeError), (ToolOutput(1, 'x'), TypeError),
    (ToolOutput(True, None), TypeError),
    (ToolOutput(True, '', source_refs=[]), TypeError),
    (ToolOutput(True, '', source_refs=(1,)), TypeError),
    (ToolOutput(True, '', source_refs=(' ',)), ValueError),
    (ToolOutput(True, '', error='unexpected'), ValueError),
    (ToolOutput(False, '', error=None), ValueError),
    (ToolOutput(False, '', error=' '), ValueError),
    (ToolOutput(False, '', error=1), TypeError),
])
async def test_invalid_tool_output_is_rejected(output, error):
    registry, tool = ToolRegistry(), make_tool()
    tool.invoke.return_value = output
    registry.register(tool)
    with pytest.raises(error):
        await registry.invoke(ToolCall('c', 'read', {}))


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [RuntimeError('tool crashed'), asyncio.CancelledError()])
async def test_tool_exceptions_propagate_unchanged(error):
    registry, tool = ToolRegistry(), make_tool()
    registry.register(tool)
    tool.invoke.side_effect = error
    with pytest.raises(type(error)) as caught:
        await registry.invoke(ToolCall('c', 'read', {}))
    assert caught.value is error
