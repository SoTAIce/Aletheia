"""One registry for RAG, local tools and MCP sessions owned by the caller."""

from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
import sys

from mcp import StdioServerParameters

from app.models.conversation_memory import ConversationMemory
from app.models.working_memory import WorkingMemory
from app.tools.current_time_tool import CurrentTimeTool
from app.tools.knowledge_registry_tool import KnowledgeRegistryTool
from app.tools.mcp_client import MCPClient
from app.tools.mcp_tool_loader import register_mcp_tools
from app.tools.tool import Tool
from app.tools.tool_registry import ToolRegistry


def local_mcp_servers(*, filesystem_root: str | Path | None = None,
                      write_root: str | Path | None = None,
                      tavily_api_key: str = "") -> tuple[StdioServerParameters, ...]:
    """Configure existing stdio servers; omit unconfigured optional services."""
    project_root = str(Path(__file__).resolve().parents[2])
    servers = []
    if write_root and not filesystem_root:
        raise ValueError("filesystem_root is required when write_root is configured")
    if filesystem_root:
        args = ["-m", "app.tools.filesystem.server", "--root", str(Path(filesystem_root).resolve())]
        if write_root:
            args += ["--write-root", str(Path(write_root).resolve())]
        servers.append(StdioServerParameters(command=sys.executable, args=args, cwd=project_root))
    if tavily_api_key.strip():
        servers.append(StdioServerParameters(
            command=sys.executable, args=["-m", "app.tools.web_search.server"],
            cwd=project_root, env={"TAVILY_API_KEY": tavily_api_key},
        ))
    return tuple(servers)


@asynccontextmanager
async def open_tool_registry(
    memory: WorkingMemory, *,
    mcp_servers: Sequence[StdioServerParameters] = (),
    local_tools: Sequence[Tool] = (),
    conversation: ConversationMemory | None = None,
    knowledge_search=None,
    include_knowledge: bool = True,
) -> AsyncIterator[ToolRegistry]:
    """Run the entire agent inside this context and in the same asyncio task.

    AsyncExitStack closes every connected server on success, failure or cancellation.
    Duplicate tool names are rejected by the existing registry, never overwritten.
    """
    registry = ToolRegistry()
    registry.register(CurrentTimeTool())
    if include_knowledge:
        registry.register(KnowledgeRegistryTool(memory, conversation, knowledge_search))
    for tool in local_tools:
        registry.register(tool)
    async with AsyncExitStack() as stack:
        for parameters in mcp_servers:
            client = await stack.enter_async_context(MCPClient.connect(parameters))
            await register_mcp_tools(client, registry)
        yield registry
