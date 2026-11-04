import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion
import pytest

from app.models.longterm_memory import (
    MemoryAction, MemoryCandidate, MemoryCategory, MemoryDecision, MemoryHit, MemoryRecord,
)
from app.services.memory_resolver import MemoryResolver


def candidate():
    return MemoryCandidate('用户现在偏好中文', MemoryCategory.PREFERENCE, 'conversation', 'turn-1')


def hit(memory_id='old-1', user_id='user'):
    return MemoryHit(MemoryRecord(
        user_id=user_id, memory_id=memory_id, content='用户偏好英文',
        category=MemoryCategory.PREFERENCE, source='conversation',
    ), score=0.01)


def response(content, finish_reason='stop'):
    return ChatCompletion(
        id='test', created=0, model='test', object='chat.completion',
        choices=[{'index': 0, 'finish_reason': finish_reason,
                  'message': {'role': 'assistant', 'content': content}}],
    )


def setup_resolver(payload=None):
    if payload is None:
        payload = {'action': 'update', 'target_memory_id': 'old-1', 'reason': '明确更新语言偏好'}
    create = AsyncMock(return_value=response(json.dumps(payload, ensure_ascii=False)))
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return MemoryResolver(client, ' model '), create


async def test_real_sdk_accepts_request_and_uses_json_mode_without_network():
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=response(json.dumps({
            'action': 'update', 'target_memory_id': 'old-1', 'reason': '明确更新',
        })).model_dump())
    async with AsyncOpenAI(
        api_key='test', base_url='https://example.test/v1', max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    ) as client:
        result = await MemoryResolver(client, ' model ').resolve(candidate(), [hit()])
    assert result == MemoryDecision(MemoryAction.UPDATE, 'old-1', '明确更新')
    assert len(requests) == 1
    assert requests[0]['model'] == 'model'
    assert requests[0]['response_format'] == {'type': 'json_object'}
    payload = json.loads(requests[0]['messages'][1]['content'])
    assert payload['new_memory']['category'] == 'preference'
    assert payload['existing_memories'][0]['memory_id'] == 'old-1'


async def test_empty_history_adds_without_model_and_explains_reason():
    resolver, create = setup_resolver()
    decision = await resolver.resolve(candidate(), [])
    assert decision.action is MemoryAction.ADD
    assert decision.target_memory_id is None
    assert decision.reason.strip()
    create.assert_not_awaited()


@pytest.mark.parametrize('action,target', [('add', None), ('skip', 'old-1'),
                                         ('update', 'old-1'), ('conflict', 'old-1')])
async def test_supported_actions_return_typed_decisions(action, target):
    resolver, create = setup_resolver({'action': action, 'target_memory_id': target, 'reason': ' 原因\n'})
    assert await resolver.resolve(candidate(), [hit()]) == MemoryDecision(MemoryAction(action), target, '原因')
    create.assert_awaited_once()


@pytest.mark.parametrize('client', [None, object(), SimpleNamespace(chat=None)])
def test_invalid_client_rejected_at_construction(client):
    with pytest.raises(TypeError, match='client'):
        MemoryResolver(client, 'model')


@pytest.mark.parametrize('model', [None, 1, '', '  '])
def test_invalid_model_rejected(model):
    resolver, _ = setup_resolver()
    with pytest.raises(ValueError, match='model'):
        MemoryResolver(resolver._client, model)


@pytest.mark.parametrize('item,error', [
    (None, TypeError), (replace(candidate(), content=None), ValueError),
    (replace(candidate(), content=' '), ValueError),
    (replace(candidate(), category='preference'), TypeError),
])
async def test_invalid_candidate_fails_before_request(item, error):
    resolver, create = setup_resolver()
    with pytest.raises(error):
        await resolver.resolve(item, [])
    create.assert_not_awaited()


@pytest.mark.parametrize('history,error', [
    (None, TypeError), ((hit(),), TypeError), ([None], TypeError),
    ([MemoryHit(None, 0)], TypeError),
    ([replace(hit(), record=replace(hit().record, memory_id=' '))], ValueError),
    ([replace(hit(), record=replace(hit().record, content=None))], ValueError),
    ([replace(hit(), record=replace(hit().record, category='fact'))], TypeError),
    ([replace(hit(), record=replace(hit().record, user_id=' '))], ValueError),
    ([hit(), hit('old-2', 'other-user')], ValueError),
    ([hit(), hit()], ValueError),
])
async def test_invalid_history_fails_before_model(history, error):
    resolver, create = setup_resolver()
    with pytest.raises(error):
        await resolver.resolve(candidate(), history)
    create.assert_not_awaited()


@pytest.mark.parametrize('content', [
    'not json', '[]', 'null', '{}',
    '{"action":"add","action":"update","target_memory_id":"old-1","reason":"r"}',
    '{"action":"add","target_memory_id":null,"reason":"r","score":NaN}',
    '{"action":"add","target_memory_id":null,"reason":"r","score":1e999}',
    '{"action":"add","reason":"r"}',
    '{"action":"add","target_memory_id":null,"reason":"r","extra":1}',
])
async def test_malformed_ambiguous_and_incomplete_json_is_rejected(content):
    resolver, create = setup_resolver()
    create.return_value = response(content)
    with pytest.raises(ValueError):
        await resolver.resolve(candidate(), [hit()])
    create.assert_awaited_once()


@pytest.mark.parametrize('changes', [
    {'action': 'delete'}, {'action': None}, {'action': []},
    {'reason': ''}, {'reason': None}, {'reason': []},
    {'target_memory_id': 'unknown'}, {'target_memory_id': []},
    {'target_memory_id': None}, {'action': 'add', 'target_memory_id': 'old-1'},
])
async def test_invalid_action_target_or_reason_is_rejected(changes):
    resolver, create = setup_resolver({
        'action': 'update', 'target_memory_id': 'old-1', 'reason': 'reason', **changes,
    })
    with pytest.raises(ValueError):
        await resolver.resolve(candidate(), [hit()])
    create.assert_awaited_once()


@pytest.mark.parametrize('finish_reason', ['length', 'content_filter', 'tool_calls', 'function_call'])
async def test_abnormal_completion_is_not_accepted_as_a_decision(finish_reason):
    resolver, create = setup_resolver()
    create.return_value = response('{"action":"add","target_memory_id":null,"reason":"r"}', finish_reason)
    with pytest.raises(ValueError, match='complete'):
        await resolver.resolve(candidate(), [hit()])


@pytest.mark.parametrize('changes', [
    {'content': None}, {'content': ' '}, {'content': []}, {'role': 'user'},
    {'refusal': 'refused'}, {'tool_calls': [object()]}, {'function_call': object()},
])
async def test_invalid_refused_or_tool_response_is_rejected(changes):
    resolver, create = setup_resolver()
    message = SimpleNamespace(role='assistant', content='{"action":"add","target_memory_id":null,"reason":"r"}', **{})
    for key, value in changes.items():
        setattr(message, key, value)
    create.return_value = SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=message)])
    with pytest.raises(ValueError):
        await resolver.resolve(candidate(), [hit()])


@pytest.mark.parametrize('choices', [None, [], [None], [object(), object()]])
async def test_invalid_choice_structure_has_clear_error(choices):
    resolver, create = setup_resolver()
    create.return_value = SimpleNamespace(choices=choices)
    with pytest.raises(ValueError):
        await resolver.resolve(candidate(), [hit()])


@pytest.mark.parametrize('failure', [RuntimeError('provider unavailable'), asyncio.CancelledError()])
async def test_provider_errors_and_cancellation_propagate(failure):
    resolver, create = setup_resolver()
    create.side_effect = failure
    with pytest.raises(type(failure)) as caught:
        await resolver.resolve(candidate(), [hit()])
    assert caught.value is failure
    create.assert_awaited_once()
