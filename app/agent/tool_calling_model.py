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


class ModelOutputLimitError(ModelResponseError):
    """An incomplete provider response; partial text/calls must never execute."""

    def __init__(self, *, prompt_tokens=None, completion_tokens=None, output_token_limit=None):
        super().__init__('model response was truncated (finish_reason=length)')
        self.finish_reason = 'length'
        # Keep only nonnegative counts, never provider payloads or partial text.
        for name, value in (('prompt_tokens', prompt_tokens), ('completion_tokens', completion_tokens),
                            ('output_token_limit', output_token_limit)):
            setattr(self, name, value if type(value) is int and value >= 0 else None)


class ModelTimeoutError(ModelRequestError):
    """The provider request exceeded its deadline."""


class ModelContextOverflowError(ModelRequestError):
    """Provider explicitly rejected the request's context length."""
