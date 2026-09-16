from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.agent.step_models import ToolCall
from app.tools.current_time_tool import CurrentTimeTool
from app.tools.tool_registry import ToolRegistry


@pytest.mark.asyncio
@pytest.mark.parametrize('arguments,expected', [
    ({}, '2026-01-15T20:00:00+08:00 [Asia/Shanghai]'),
    ({'timezone': ' UTC '}, '2026-01-15T12:00:00+00:00 [UTC]'),
    ({'timezone': 'America/New_York'}, '2026-01-15T07:00:00-05:00 [America/New_York]'),
])
async def test_registered_time_tool(arguments, expected):
    registry = ToolRegistry()
    tool = CurrentTimeTool()
    registry.register(tool)
    assert registry.get('get_current_time') is tool
    assert registry.list_definitions() == (tool.definition,)
    instant = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
    with patch('app.tools.current_time_tool.datetime') as clock:
        clock.now.side_effect = lambda zone: instant.astimezone(zone)
        result = await registry.invoke(ToolCall('clock-1', 'get_current_time', arguments))
    assert result.success and result.error is None
    assert result.content == expected
    assert result.call_id == 'clock-1'
    assert result.tool_name == 'get_current_time'
    assert result.source_refs == ()


@pytest.mark.asyncio
@pytest.mark.parametrize('arguments', [
    {'timezone': ''}, {'timezone': ' '}, {'timezone': None}, {'timezone': 1},
    {'timezone': 'Not/A_Zone'}, {'timezone': '/etc/localtime'},
    {'timezone': '../UTC'}, {'extra': True},
])
async def test_invalid_arguments_return_failed_tool_result(arguments):
    registry = ToolRegistry()
    registry.register(CurrentTimeTool())
    result = await registry.invoke(ToolCall('bad-call', 'get_current_time', arguments))
    assert not result.success
    assert result.error and result.content == ''
    assert result.call_id == 'bad-call'


def test_definition_schema_is_not_shared_between_reads():
    tool = CurrentTimeTool()
    definition = tool.definition
    definition.input_schema['properties'].clear()
    assert 'timezone' in tool.definition.input_schema['properties']
