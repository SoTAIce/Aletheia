from app.tools.mcp_client import MCPClient
from app.tools.mcp_tool_adapter import MCPToolAdapter, UserMCPToolAdapter
from app.tools.tool_registry import ToolRegistry


async def register_mcp_tools(
    client: MCPClient,
    registry: ToolRegistry,
) -> None:
    """The caller must keep client connected until all tool executions finish."""
    for tool in await client.list_tools():
        registry.register(MCPToolAdapter(client, tool))


async def register_user_mcp_tools(
    client: MCPClient,
    registry: ToolRegistry,
    server,
) -> int:
    """Register tools from one user MCP server with prefixing, configured effects and cap (D-095)."""
    from app.tools.user_mcp import select_user_mcp_tools
    selected = select_user_mcp_tools(server, await client.list_tools())
    for tool in selected.accepted:
        registry.register(UserMCPToolAdapter(
            client,
            tool,
            server_name=server.name,
            effect=server.effect_for(tool.name),
            timeout_seconds=server.timeout_seconds,
        ))
    return len(selected.accepted)
