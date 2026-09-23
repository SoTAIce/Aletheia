"""Run from the project root: python -m app.agent.mcp_demo.registry_client."""

import asyncio
from pathlib import Path
import sys

from mcp import StdioServerParameters

from app.agent.step_models import ToolCall
from app.tools.mcp_client import MCPClient
from app.tools.mcp_tool_adapter import MCPToolAdapter
from app.tools.tool_registry import ToolRegistry


async def main() -> None:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(Path(__file__).resolve().with_name("time_server.py"))],
    )
    registry = ToolRegistry()
    async with MCPClient.connect(parameters) as client:
        for tool in await client.list_tools():
            registry.register(MCPToolAdapter(client, tool))
        for index, timezone in enumerate(("Asia/Shanghai", "Invalid/Timezone")):
            result = await registry.invoke(ToolCall(
                call_id=f"time-{index}",
                tool_name="get_current_time",
                arguments={"timezone": timezone},
            ))
            print(result)
    print("MCP connection closed.")


if __name__ == "__main__":
    asyncio.run(main())
