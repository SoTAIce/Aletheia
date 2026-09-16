from collections.abc import Sequence
from dataclasses import FrozenInstanceError, asdict
from typing import get_type_hints

import pytest

from app.agent.step_models import (
    AssistantMessage, ModelMessage, ModelTurn, SystemMessage,
    ToolCall, ToolMessage, UserMessage,
)
from app.agent.tool_calling_model import ToolCallingModel


@pytest.mark.parametrize('message,role', [
    (SystemMessage('instructions'), 'system'),
    (UserMessage('task'), 'user'),
    (AssistantMessage('answer'), 'assistant'),
    (ToolMessage('call-1', ''), 'tool'),
])
def test_message_roles_are_fixed_and_serializable(message, role):
    assert message.role == role
    assert asdict(message)['role'] == role
    with pytest.raises(FrozenInstanceError):
        message.role = 'other'
    with pytest.raises(FrozenInstanceError):
        message.content = 'changed'
    with pytest.raises(TypeError):
        type(message)(**{**asdict(message), 'role': 'other'})


@pytest.mark.parametrize('content', [None, 'Checking the time'])
def test_model_turn_converts_to_assistant_history_preserving_calls(content):
    call = ToolCall('call-1', 'get_current_time', {'timezone': 'UTC'})
    turn = ModelTurn(content=content, tool_calls=(call,))
    assistant = turn.to_assistant_message()
    reply = ToolMessage(call_id=call.call_id, content='2026-09-16T00:00:00+00:00')
    history: list[ModelMessage] = [SystemMessage('instructions'), UserMessage('time?'),
                                   assistant, reply]
    assert assistant.content == content
    assert assistant.tool_calls == (call,)
    assert history[-1].call_id == assistant.tool_calls[0].call_id
    assert turn.tool_calls == (call,)


def test_text_only_turn_and_protocol_input():
    assert ModelTurn('done').to_assistant_message() == AssistantMessage('done')
    hints = get_type_hints(ToolCallingModel.generate)
    assert hints['messages'] == Sequence[ModelMessage]
    assert hints['return'] is ModelTurn
