from types import SimpleNamespace

import pytest

from app.models.conversation_memory import ConversationMessage, ConversationTurn, MessageRole
from app.services.memory_compression.llm_compressor import LLMCompressor


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['length', 'refusal', 'tools', 'role', 'multiple', 'nontext', 'function'])
async def test_rejects_incomplete_or_non_summary_completions(change):
    message = SimpleNamespace(content='summary', role='assistant', refusal=None, tool_calls=None)
    choice = SimpleNamespace(message=message, finish_reason='stop')
    choices = [choice]
    if change == 'length':
        choice.finish_reason = 'length'
    elif change == 'refusal':
        message.refusal = 'cannot summarize'
    elif change == 'tools':
        message.tool_calls = [object()]
    elif change == 'role':
        message.role = 'tool'
    elif change == 'multiple':
        choices.append(choice)
    elif change == 'function':
        message.function_call = {'name': 'search'}
    else:
        message.content = ['not text']

    async def create(**kwargs):
        return SimpleNamespace(choices=choices)

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    compressor = LLMCompressor(client, 'offline-test')
    with pytest.raises(RuntimeError):
        await compressor.compress(None, [ConversationTurn([ConversationMessage(MessageRole.USER, 'raw')])])
