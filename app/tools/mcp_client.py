"""Minimal stdio connection owned by an async context, not by the registry."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, Tool

from app.tools.sandbox.executor import SANDBOX_TOOLS, host_call_timeout_seconds

DEFAULT_READ_TIMEOUT = timedelta(seconds=30)
# A sandbox run may legitimately take its full limit plus container removal; with the
# default 30 s the transport timed out first and the model never saw "timed out".
SANDBOX_READ_TIMEOUT = timedelta(seconds=host_call_timeout_seconds())


class MCPClient:
    """Use connect() in the same task for the entire tool execution lifetime.

    Protocol/transport errors and cancellation propagate to the caller.
    This client does not retry calls that may have side effects.
    """

    def __init__(self, session: ClientSession) -> None:
        self._session = session
        self._closed = False

    @classmethod
    @asynccontextmanager
    async def connect(
        cls, parameters: StdioServerParameters
    ) -> AsyncIterator["MCPClient"]:
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(
                read, write, read_timeout_seconds=DEFAULT_READ_TIMEOUT
            ) as session:
                await session.initialize()
                client = cls(session)
                try:
                    yield client
                finally:
                    client._closed = True

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("MCP connection is closed")

    async def list_tools(self) -> tuple[Tool, ...]:
        self._require_open()
        tools: list[Tool] = []
        cursor = None
        seen_cursors: set[str] = set()
        while True:
            response = await self._session.list_tools(cursor=cursor)
            tools.extend(response.tools)
            cursor = response.nextCursor
            if not cursor:
                return tuple(tools)
            if cursor in seen_cursors:
                raise RuntimeError("MCP server repeated a tool-list cursor")
            seen_cursors.add(cursor)

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> CallToolResult:
        self._require_open()
        timeout = SANDBOX_READ_TIMEOUT if name in SANDBOX_TOOLS else None   # None: session default
        return await self._session.call_tool(name, arguments=arguments, read_timeout_seconds=timeout)
