"""Minimal tool interface, independent of runners and model providers."""

from typing import Any, Protocol

from app.agent.step_models import ToolDefinition, ToolOutput


class Tool(Protocol):
    @property
    def definition(self) -> ToolDefinition:
        ...

    async def invoke(self, arguments: dict[str, Any]) -> ToolOutput:
        ...
