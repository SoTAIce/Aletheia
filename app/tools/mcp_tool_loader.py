from app.tools.mcp_client import MCPClient
from app.tools.mcp_tool_adapter import MCPToolAdapter
from app.tools.tool_registry import ToolRegistry


async def register_mcp_tools(
    client: MCPClient,
    registry: ToolRegistry,
) -> None:
    """The caller must keep client connected until all tool executions finish."""
    for tool in await client.list_tools():
        registry.register(MCPToolAdapter(client, tool))
