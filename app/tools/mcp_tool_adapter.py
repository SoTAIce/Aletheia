"""Adapt MCP text/JSON tools to Aletheia's existing Tool protocol."""

from copy import deepcopy
import json
from typing import Any

from mcp.types import TextContent, Tool

from app.agent.step_models import ToolDefinition, ToolOutput
from app.tools.mcp_client import MCPClient


class MCPToolAdapter:
    def __init__(self, client: MCPClient, tool: Tool) -> None:
        self._client = client
        self._definition = ToolDefinition(
            name=tool.name,
            description=tool.description or f"Invoke MCP tool {tool.name}.",
            input_schema=deepcopy(tool.inputSchema),
        )

    @property
    def definition(self) -> ToolDefinition:
        return deepcopy(self._definition)

    async def invoke(self, arguments: dict[str, Any]) -> ToolOutput:
        result = await self._client.call_tool(self._definition.name, arguments)
        # V1 is text-only: do not silently discard images or resource blocks.
        if any(not isinstance(block, TextContent) for block in result.content):
            raise TypeError("MCPToolAdapter currently supports text/JSON results only")
        content = "\n".join(block.text for block in result.content)
        if result.structuredContent is not None:
            structured = json.dumps(result.structuredContent, ensure_ascii=False)
            content = f"{content}\n{structured}" if content else structured
        return ToolOutput(
            success=not result.isError,
            content=content,
            # MCP resource URIs are not WorkingMemory resource IDs.
            source_refs=(),
            error=(content.strip() or "MCP tool execution failed")
            if result.isError else None,
        )
