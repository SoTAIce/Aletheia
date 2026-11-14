from dataclasses import asdict
import json
from unittest.mock import AsyncMock

import pytest

from app.agent.step_models import SystemMessage, UserMessage, ToolDefinition, ModelTurn
from app.agent.tool_calling_model import ModelRequestError
from app.models.conversation_memory import ConversationMemory, ConversationMessage, ConversationTurn, MessageRole
from app.services.context_manager import ContextBudgetError, ContextManager


def test_budget_counts_system_tools_output_and_safety():
    messages = (SystemMessage('system'), UserMessage('goal'))
    tool = ToolDefinition('lookup', 'description', {'type': 'object', 'properties': {}})
    manager = ContextManager(window_tokens=100000, output_tokens=20, safety_tokens=10, token_counter=len)
    base = manager.count_request(messages, ())
    total = manager.count_request(messages, (tool,))
    assert total > base > 30
    ContextManager(window_tokens=total, output_tokens=20, safety_tokens=10, token_counter=len).prepare(messages, (tool,))
    with pytest.raises(ContextBudgetError):
        ContextManager(window_tokens=total-1, output_tokens=20, safety_tokens=10, token_counter=len).prepare(messages, (tool,))


async def test_all_recent_turns_are_available_not_only_three():
    memory = ConversationMemory('session')
    for i in range(5):
        await memory.add(ConversationTurn([ConversationMessage(MessageRole.USER, f'turn-{i}')]))
    manager = ContextManager()
    context = json.loads(manager.conversation_context(memory))
    assert len(context['turns']) == 5
    assert 'turn-0' in json.dumps(context)


def test_projection_removes_complete_old_turns_without_touching_goal_or_constraints():
    history = {'summary': None, 'turns': [
        {'turn_id': 'old', 'messages': [{'role':'user','content':'x'*2000}]},
        {'turn_id': 'new', 'messages': [{'role':'user','content':'recent'}]},
    ]}
    data = {'active_goal':'do task', 'constraints':[{'content':'never overwrite'}],
            'pinned_contexts':[{'source':'conversation_memory', 'content':json.dumps(history)}]}
    original = json.dumps(data)
    manager = ContextManager(window_tokens=1000, output_tokens=20, safety_tokens=10, token_counter=len)
    prepared = manager.prepare((UserMessage(original),), ())
    projected = json.loads(prepared[0].content)
    assert projected['active_goal'] == 'do task'
    assert projected['constraints'] == [{'content':'never overwrite'}]
    assert json.loads(projected['pinned_contexts'][0]['content'])['turns'][0]['turn_id'] == 'new'
    assert len(json.loads(original)['pinned_contexts'][0]['content']) > 2000


def test_mandatory_context_is_not_silently_truncated():
    manager = ContextManager(window_tokens=250, output_tokens=20, safety_tokens=10, token_counter=len)
    with pytest.raises(ContextBudgetError):
        manager.prepare((UserMessage(json.dumps({'active_goal':'x'*300,'constraints':['important']})),), ())


async def test_manager_checks_each_request_before_calling_backend():
    backend = AsyncMock()
    backend.generate.return_value = ModelTurn('done')
    manager = ContextManager(window_tokens=300, output_tokens=20, safety_tokens=10, token_counter=len)
    assert (await manager.generate(backend, (UserMessage('goal'),), ())).content == 'done'
    with pytest.raises(ContextBudgetError):
        await manager.generate(backend, (UserMessage('x'*500),), ())
    assert backend.generate.await_count == 1


@pytest.mark.parametrize('kwargs', [{'window_tokens':True},{'output_tokens':0},{'safety_tokens':-1},
                                    {'window_tokens':100,'output_tokens':100}])
def test_invalid_budget_is_explicit(kwargs):
    with pytest.raises(ValueError):
        ContextManager(**kwargs)

async def test_conversation_projection_respects_configured_cache_size():
    memory = ConversationMemory('session', max_turns=25)
    for i in range(25):
        await memory.add(ConversationTurn([ConversationMessage(MessageRole.USER, str(i))]))
    assert len(json.loads(ContextManager().conversation_context(memory))['turns']) == 25
