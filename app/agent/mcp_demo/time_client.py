import asyncio
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent

async def main() -> None:
    server_path = Path(__file__).resolve().with_name("time_server.py")

    server_params = StdioServerParameters(
        command = sys.executable,
        args = [str(server_path)]
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:

            await session.initialize()

            response = await session.list_tools()

            for tool in response.tools:
                print("工具名称:", tool.name)
                print("工具描述:", tool.description)
                print("参数结构:", tool.inputSchema)

            for timezone in ("Asia/Shanghai", "Invalid/Timezone"):
                result = await session.call_tool(
                    "get_current_time",
                    arguments={"timezone": timezone},
                )

                print(f"\n时区: {timezone}")
                print("是否失败:", result.isError)

                for block in result.content:
                    if isinstance(block, TextContent):
                        print("返回内容:", block.text)

if __name__ == "__main__":
    asyncio.run(main())