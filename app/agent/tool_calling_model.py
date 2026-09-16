from collections.abc import Sequence
from typing import Protocol

from app.agent.step_models import (
    ModelTurn,
    ModelMessage,
    ToolDefinition,
)


class ToolCallingModel(Protocol):
    """One model turn using application-owned messages, never provider payloads.

    Implementations must not mutate messages, tool arguments, or schemas.
    Convert to provider messages internally, preserving tool call IDs exactly.
    Validate inputs and response data at the adapter boundary: dataclass type
    annotations alone do not perform runtime validation. A successful response
    must contain usable text or tool calls; incomplete or malformed provider
    responses raise ModelResponseError. Cancellation propagates unchanged.
    The runner owns history order, tool replies, and the multi-turn loop.
    """
    async def generate(
        self,
        messages: Sequence[ModelMessage],
        tools: tuple[ToolDefinition, ...],
    ) -> ModelTurn:
        ...

class ToolCallingModelError(RuntimeError):
    """Base error for ToolCallingModel failures."""
    pass


class ModelRequestError(ToolCallingModelError):
    """The provider request failed."""
    pass


class ModelResponseError(ToolCallingModelError):
    """The provider returned an unusable response."""
    pass
