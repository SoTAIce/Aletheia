from datetime import datetime
from pathlib import Path
import sys
from unittest.mock import AsyncMock

from mcp import StdioServerParameters
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool
import pytest

from app.agent.step_models import ToolCall
from app.tools.mcp_client import MCPClient
from app.tools.mcp_tool_adapter import MCPToolAdapter
from app.tools.tool_registry import ToolRegistry


@pytest.mark.asyncio
async def test_real_stdio_registry_and_cleanup():
    server = Path(__file__).resolve().parents[2] / "app/agent/mcp_demo/time_server.py"
    registry = ToolRegistry()
    async with MCPClient.connect(StdioServerParameters(
        command=sys.executable, args=[str(server)]
    )) as client:
        for tool in await client.list_tools():
            registry.register(MCPToolAdapter(client, tool))
        result = await registry.invoke(ToolCall(
            "ok", "get_current_time", {"timezone": "Asia/Shanghai"}
        ))
        assert result.success and result.error is None
        assert datetime.fromisoformat(result.content.splitlines()[0]).utcoffset().total_seconds() == 28800
        failed = await registry.invoke(ToolCall(
            "bad", "get_current_time", {"timezone": "Invalid/Timezone"}
        ))
        assert not failed.success
        assert "Unknown timezone" in failed.error
        assert failed.call_id == "bad"
    with pytest.raises(RuntimeError, match="closed"):
        await registry.invoke(ToolCall("late", "get_current_time", {}))


@pytest.mark.asyncio
async def test_pagination():
    session = AsyncMock()
    session.list_tools.side_effect = [
        ListToolsResult(tools=[Tool(name="one", inputSchema={})], nextCursor="next"),
        ListToolsResult(tools=[Tool(name="two", inputSchema={})]),
    ]
    assert [tool.name for tool in await MCPClient(session).list_tools()] == ["one", "two"]
    assert session.list_tools.call_args_list[1].kwargs == {"cursor": "next"}


@pytest.mark.asyncio
async def test_adapter_conversion_and_transport_errors():
    client = AsyncMock()
    adapter = MCPToolAdapter(client, Tool(name="test", inputSchema={"type": "object"}))
    adapter.definition.input_schema.clear()
    assert adapter.definition.input_schema == {"type": "object"}
    assert adapter.definition.description
    client.call_tool.return_value = CallToolResult(content=[], isError=True)
    assert (await adapter.invoke({})).error == "MCP tool execution failed"
    client.call_tool.return_value = CallToolResult(
        content=[TextContent(type="text", text="hello")], structuredContent={"count": 1}
    )
    result = await adapter.invoke({})
    assert result.success and '"count": 1' in result.content
    assert result.source_refs == ()
    client.call_tool.side_effect = ConnectionError("disconnected")
    with pytest.raises(ConnectionError, match="disconnected"):
        await adapter.invoke({})
