import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import httpx
from openai import APIConnectionError, APIResponseValidationError, APITimeoutError
from openai.types.chat.chat_completion_message_function_tool_call import (
    ChatCompletionMessageFunctionToolCall,
)
from app.agent.tool_calling_model import ModelRequestError, ModelResponseError

from app.agent.openai_compatible_tool_calling_model import OpenAICompatibleToolCallingModel
from app.agent.step_models import AssistantMessage, SystemMessage, UserMessage, ToolMessage, ToolCall, ToolDefinition


def client():
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock())))


@pytest.mark.parametrize('kwargs,error', [
    ({'model': ''}, ValueError), ({'model': None}, TypeError),
    ({'temperature': True}, TypeError), ({'temperature': -1}, ValueError),
    ({'temperature': 3}, ValueError), ({'temperature': float('nan')}, ValueError),
    ({'max_output_tokens': True}, TypeError), ({'max_output_tokens': 1.5}, TypeError),
    ({'max_output_tokens': 0}, ValueError), ({'timeout_seconds': 0}, ValueError),
    ({'timeout_seconds': float('inf')}, ValueError), ({'timeout_seconds': '1'}, TypeError),
    ({'client': None}, TypeError),
])
def test_invalid_configuration(kwargs, error):
    with pytest.raises(error):
        OpenAICompatibleToolCallingModel(**({'client': client(), 'model': 'model'} | kwargs))


def test_payload_preserves_calls_and_raw_text():
    model = OpenAICompatibleToolCallingModel(client(), ' model ', temperature=0,
                                            max_output_tokens=1, timeout_seconds=0.5)
    assert model._model == 'model'
    call = ToolCall('call', 'read', {'text': '中文'})
    messages = [SystemMessage('system'), UserMessage(' input '),
                AssistantMessage(tool_calls=(call,)), ToolMessage('call', ''),
                AssistantMessage('done')]
    payload = model._build_message_payload(messages)
    assert len(payload) == 5
    assert payload[1]['content'] == ' input '
    assert payload[2]['content'] is None
    assert json.loads(payload[2]['tool_calls'][0]['function']['arguments']) == call.arguments
    assert payload[3] == {'role': 'tool', 'content': '', 'tool_call_id': 'call'}
    assert 'tool_calls' not in payload[4]


@pytest.mark.parametrize('messages', [
    [], 'text', [None], [UserMessage(None)], [UserMessage(' ')],
    [AssistantMessage()], [AssistantMessage(content=1)],
    [AssistantMessage(tool_calls=[ToolCall('c', 'tool', {})])],
    [AssistantMessage(tool_calls=(ToolCall('c', 'tool', {'x': float('nan')}),))],
    [ToolMessage('', 'output')],
])
def test_invalid_messages(messages):
    model = OpenAICompatibleToolCallingModel(client(), 'model')
    with pytest.raises((TypeError, ValueError)):
        model._build_message_payload(messages)


@pytest.mark.asyncio
async def test_generate_minimal_request():
    fake = client()
    fake.chat.completions.create.return_value = response('hello')
    model = OpenAICompatibleToolCallingModel(fake, 'model')
    turn = await model.generate([UserMessage('hello')], ())
    assert turn.content == 'hello' and turn.tool_calls == ()
    fake.chat.completions.create.assert_awaited_once_with(
        model='model', messages=[{'role': 'user', 'content': 'hello'}],
    )


@pytest.mark.asyncio
async def test_generate_passes_tools_and_configured_options():
    fake = client()
    fake.chat.completions.create.return_value = response(None, 'tool_calls', [{
        'id': 'call', 'type': 'function', 'function': {'name': 'read', 'arguments': '{}'},
    }])
    model = OpenAICompatibleToolCallingModel(fake, 'model', temperature=0,
                                            max_output_tokens=100, timeout_seconds=5)
    tools = (ToolDefinition('read', 'Read', {'type': 'object'}),)
    turn = await model.generate([UserMessage('hello')], tools)
    assert turn.tool_calls == (ToolCall('call', 'read', {}),)
    fake.chat.completions.create.assert_awaited_once_with(
        model='model', messages=[{'role': 'user', 'content': 'hello'}],
        tools=model._build_tool_payload(tools), temperature=0, max_tokens=100, timeout=5,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['connection', 'timeout', 'validation', 'bug', 'cancel'])
async def test_generate_classifies_errors_and_preserves_cause(kind):
    request = httpx.Request('POST', 'https://example.invalid/v1/chat/completions')
    errors = {
        'connection': (APIConnectionError(request=request), ModelRequestError),
        'timeout': (APITimeoutError(request=request), ModelRequestError),
        'validation': (APIResponseValidationError(httpx.Response(200, request=request), {}), ModelResponseError),
        'bug': (TypeError('wrong call'), TypeError),
        'cancel': (asyncio.CancelledError(), asyncio.CancelledError),
    }
    original, expected = errors[kind]
    fake = client()
    fake.chat.completions.create.side_effect = original
    with pytest.raises(expected) as caught:
        await OpenAICompatibleToolCallingModel(fake, 'model').generate([UserMessage('hello')], ())
    if kind in ('bug', 'cancel'):
        assert caught.value is original
    else:
        assert caught.value.__cause__ is original


@pytest.mark.asyncio
async def test_generate_keeps_response_errors_distinct():
    fake = client()
    fake.chat.completions.create.return_value = response('partial', 'length')
    with pytest.raises(ModelResponseError, match='truncated'):
        await OpenAICompatibleToolCallingModel(fake, 'model').generate([UserMessage('hello')], ())


@pytest.mark.asyncio
@pytest.mark.parametrize('messages,tools', [([], ()), ([UserMessage('hello')], (None,))])
async def test_generate_validates_before_request(messages, tools):
    fake = client()
    with pytest.raises((TypeError, ValueError)):
        await OpenAICompatibleToolCallingModel(fake, 'model').generate(messages, tools)
    fake.chat.completions.create.assert_not_awaited()


def test_tool_payload_and_schema_isolation():
    model = OpenAICompatibleToolCallingModel(client(), 'model')
    schema = {'type': 'object', 'properties': {'text': {'type': 'string'}}}
    tool = ToolDefinition('read', ' Read text ', schema)
    other = ToolDefinition('clock', 'Current time', {'type': 'object'})
    payload = model._build_tool_payload((tool, other))
    assert payload[0] == {'type': 'function', 'function': {
        'name': 'read', 'description': 'Read text', 'parameters': schema,
    }}
    assert payload[1]['function']['name'] == 'clock'
    payload[0]['function']['parameters']['properties'].clear()
    assert 'text' in schema['properties']
    assert tool.description == ' Read text '
    assert model._build_tool_payload(()) == []


@pytest.mark.parametrize('tools,error', [
    (None, TypeError), ([], TypeError), ((None,), TypeError),
    ((ToolDefinition(None, 'desc', {}),), TypeError),
    ((ToolDefinition('read', None, {}),), TypeError),
    ((ToolDefinition('', 'desc', {}),), ValueError),
    ((ToolDefinition(' read ', 'desc', {}),), ValueError),
    ((ToolDefinition('read', ' ', {}),), ValueError),
    ((ToolDefinition('read', 'desc', []),), TypeError),
    ((ToolDefinition('read', 'desc', {}),), ValueError),
    ((ToolDefinition('read', 'desc', {'type': 'array'}),), ValueError),
    ((ToolDefinition('read', 'desc', {'type': 'object', 'default': float('nan')}),), ValueError),
    ((ToolDefinition('read', 'desc', {'type': 'object', 'default': object()}),), ValueError),
    ((ToolDefinition('read', 'a', {'type': 'object'}),
      ToolDefinition('read', 'b', {'type': 'object'})), ValueError),
])
def test_tool_payload_rejects_invalid_definitions(tools, error):
    model = OpenAICompatibleToolCallingModel(client(), 'model')
    with pytest.raises(error):
        model._build_tool_payload(tools)


def test_parse_sdk_tool_calls():
    model = OpenAICompatibleToolCallingModel(client(), 'model')
    raw = [ChatCompletionMessageFunctionToolCall(
        id=f'call-{index}', type='function',
        function={'name': 'read', 'arguments': '{"path": "中文", "count": 2}'},
    ) for index in range(2)]
    parsed = model._parse_tool_calls(raw)
    assert parsed == tuple(ToolCall(f'call-{index}', 'read', {'path': '中文', 'count': 2})
                           for index in range(2))
    assert model._parse_tool_calls(None) == model._parse_tool_calls([]) == ()
    assert isinstance(raw[0].function.arguments, str)
    with pytest.raises(ModelResponseError, match='duplicate'):
        model._parse_tool_calls([raw[0], raw[0]])


@pytest.mark.parametrize('raw', [
    'invalid', {}, [None],
    [SimpleNamespace(type='custom')],
    [SimpleNamespace(type='function', id=' ', function=None)],
    [SimpleNamespace(type='function', id='c', function=None)],
    [SimpleNamespace(type='function', id='c', function=SimpleNamespace(name='', arguments='{}'))],
])
def test_parse_tool_calls_rejects_bad_shape(raw):
    with pytest.raises(ModelResponseError):
        OpenAICompatibleToolCallingModel(client(), 'model')._parse_tool_calls(raw)


@pytest.mark.parametrize('arguments', ['not json', '[]', 'null', '42', None, {}])
def test_parse_tool_calls_rejects_invalid_arguments(arguments):
    raw = SimpleNamespace(type='function', id='call',
                          function=SimpleNamespace(name='read', arguments=arguments))
    with pytest.raises(ModelResponseError):
        OpenAICompatibleToolCallingModel(client(), 'model')._parse_tool_calls([raw])


def response(content='answer', finish_reason='stop', tool_calls=None, **message_fields):
    from openai.types.chat import ChatCompletion
    return ChatCompletion(
        id='response', object='chat.completion', created=0, model='model',
        choices=[{'index': 0, 'finish_reason': finish_reason, 'message': {
            'role': 'assistant', 'content': content, 'tool_calls': tool_calls,
            **message_fields,
        }}],
    )


def test_parse_response_text_preserved():
    model = OpenAICompatibleToolCallingModel(client(), 'model')
    turn = model._parse_response(response(' answer\n'))
    assert turn.content == ' answer\n' and turn.tool_calls == ()


@pytest.mark.parametrize('content', [None, '', '  ', 'Looking up information'])
@pytest.mark.parametrize('reason', ['stop', 'tool_calls'])
def test_parse_response_accepts_tool_only_and_mixed_turns(content, reason):
    model = OpenAICompatibleToolCallingModel(client(), 'model')
    raw = response(content, reason, [{
        'id': 'call', 'type': 'function',
        'function': {'name': 'read', 'arguments': '{}'},
    }])
    turn = model._parse_response(raw)
    assert turn.content == (content if content and content.strip() else None)
    assert turn.tool_calls == (ToolCall('call', 'read', {}),)


@pytest.mark.parametrize('raw', [
    None, SimpleNamespace(choices=[]), SimpleNamespace(choices=[None]),
    SimpleNamespace(choices=[None, None]), SimpleNamespace(choices=()),
    response('partial', 'length'), response('blocked', 'content_filter'),
    response(None), response(' '), response('answer', 'tool_calls'),
    response('answer', refusal='refused'), response('answer', 'function_call'),
    response(function_call={'name': 'read', 'arguments': '{}'}),
    SimpleNamespace(choices=[SimpleNamespace(finish_reason=None)]),
    SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=None)]),
    SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(
        role='assistant', content=123))]),
])
def test_parse_response_rejects_unusable_provider_responses(raw):
    with pytest.raises(ModelResponseError):
        OpenAICompatibleToolCallingModel(client(), 'model')._parse_response(raw)


@pytest.mark.parametrize('arguments', [
    '{"x": NaN}', '{"x": Infinity}', '{"x": -Infinity}',
    '{"x": [1e999]}', '{"x": 1, "x": 2}', '{"nested": {"x": 1, "x": 2}}',
])
def test_arguments_reject_non_finite_values_and_duplicate_keys(arguments):
    with pytest.raises(ModelResponseError):
        OpenAICompatibleToolCallingModel(client(), 'model')._parse_arguments(arguments)
