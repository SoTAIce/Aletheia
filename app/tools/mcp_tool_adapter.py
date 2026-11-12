"""Adapt MCP text/JSON tools to Aletheia's existing Tool protocol."""

from copy import deepcopy
import json
from typing import Any

from mcp.types import TextContent, Tool

from app.agent.step_models import ToolDefinition, ToolOutput
from app.tools.mcp_client import MCPClient


def _same_as_text(structured: Any, text: str) -> bool:
    """True when structuredContent only repeats the text blocks (D-052).

    FastMCP returns a str result both as text and as {"result": text}, and a dict
    result as its JSON text plus the dict; appending both doubled every read_file.
    """
    if not text:
        return False
    if structured == {"result": text}:
        return True
    try:
        parsed = json.loads(text)
    except ValueError:
        return False
    return parsed == structured or (isinstance(structured, dict) and structured.keys() == {"result"}
                                    and parsed == structured["result"])


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
        if result.structuredContent is not None and not _same_as_text(result.structuredContent, content):
            structured = json.dumps(result.structuredContent, ensure_ascii=False)
            content = f"{content}\n{structured}" if content else structured
        return ToolOutput(
            success=not result.isError,
            content=content,
            # MCP resource URIs are not RunMemory resource IDs.
            source_refs=(),
            error=(content.strip() or "MCP tool execution failed")
            if result.isError else None,
        )
