import re
from collections.abc import Sequence
from typing import Protocol

from app.agent.step_models import (
    ModelTurn,
    ModelMessage,
    ToolDefinition,
)


_TOOL_NAME = re.compile(r'[A-Za-z0-9_.-]{1,64}')


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


class ModelToolArgumentsError(ModelResponseError):
    """Tool call arguments could not be decoded; no call from that turn may execute.

    Only safe metadata is kept (never the raw arguments), so callers can feed back a
    bounded repair instruction instead of failing the whole step.
    """

    _MESSAGES = {
        'not_string': 'tool call arguments must be a JSON string',
        'invalid_json': 'tool call arguments are not valid JSON',
        'not_object': 'tool call arguments must decode to a JSON object',
    }

    def __init__(self, reason='invalid_json', *, tool_name=None, argument_chars=None):
        reason = reason if reason in self._MESSAGES else 'invalid_json'
        super().__init__(self._MESSAGES[reason])
        self.reason = reason
        # The name is model-supplied; keep it only when it looks like a tool name.
        self.tool_name = (tool_name if isinstance(tool_name, str)
                          and _TOOL_NAME.fullmatch(tool_name) else None)
        self.argument_chars = argument_chars if type(argument_chars) is int and argument_chars >= 0 else None


def tool_argument_repair_instruction(error: ModelToolArgumentsError) -> str:
    """Host-authored repair text; contains no model output."""
    target = f'the call to {error.tool_name}' if error.tool_name else 'the previous tool call'
    return ('HOST PROTOCOL REJECTION: the arguments of ' + target + ' were rejected (' + error.reason +
            '), so nothing from that response was executed. Re-issue the call with arguments as one '
            'valid JSON object. Inside string values escape double quotes, backslashes and newlines '
            '(\\" \\\\ \\n). Keep long string values compact and do not add text outside the JSON.')


class ModelTimeoutError(ModelRequestError):
    """The provider request exceeded its deadline."""


class ModelContextOverflowError(ModelRequestError):
    """Provider explicitly rejected the request's context length."""
