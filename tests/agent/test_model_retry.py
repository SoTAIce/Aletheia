"""Bounded retries of one model call when nothing from the failed response ran (D-039)."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import (APIConnectionError, APIResponseValidationError, APITimeoutError, BadRequestError,
                    InternalServerError, RateLimitError)
from openai.types.chat import ChatCompletion

import app.agent.openai_compatible_tool_calling_model as adapter
from app.agent.openai_compatible_tool_calling_model import OpenAICompatibleToolCallingModel
from app.agent.run_meter import RunMeter, metering
from app.agent.step_models import UserMessage
from app.agent.tool_calling_model import (ModelContextOverflowError, ModelOutputLimitError, ModelRequestError,
                                          ModelResponseError, ModelTimeoutError, ModelToolArgumentsError)

REQUEST = httpx.Request('POST', 'https://example.invalid/v1/chat/completions')


def completion(content='hi', finish='stop', refusal=None, tool_calls=None):
    message = {'role': 'assistant', 'content': content, 'refusal': refusal}
    if tool_calls is not None:
        message['tool_calls'] = tool_calls
    return ChatCompletion(id='r', object='chat.completion', created=0, model='m',
                          choices=[{'index': 0, 'finish_reason': finish, 'message': message}],
                          usage={'prompt_tokens': 4, 'completion_tokens': 2, 'total_tokens': 6})


def status(cls, code, body=None):
    return cls('SECRET provider text', response=httpx.Response(code, request=REQUEST), body=body)


TRANSIENT = {
    'validation': lambda: APIResponseValidationError(httpx.Response(200, request=REQUEST), {}),
    'connection': lambda: APIConnectionError(request=REQUEST),
    'timeout': lambda: APITimeoutError(request=REQUEST),
    'rate_limit': lambda: status(RateLimitError, 429),
    'server': lambda: status(InternalServerError, 503),
    'empty_reply': lambda: completion(content=None),
    'tool_finish_without_calls': lambda: completion(content='x', finish='tool_calls'),
}
FINAL = {
    'bad_request': (lambda: status(BadRequestError, 400), ModelRequestError),
    'context_overflow': (lambda: status(BadRequestError, 400, {'error': {'code': 'context_length_exceeded'}}),
                         ModelContextOverflowError),
    'truncated': (lambda: completion(finish='length'), ModelOutputLimitError),
    'filtered': (lambda: completion(finish='content_filter'), ModelResponseError),
    'refused': (lambda: completion(content=None, refusal='no'), ModelResponseError),
    'bad_tool_arguments': (lambda: completion(content=None, finish='tool_calls', tool_calls=[
        {'id': 'c', 'type': 'function', 'function': {'name': 'read', 'arguments': '{bad'}}]),
        ModelToolArgumentsError),
}


def model_with(outcomes, monkeypatch, **options):
    """A model whose provider returns or raises each outcome in turn; sleeps are recorded."""
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(adapter, 'asyncio', SimpleNamespace(sleep=sleep))

    async def create(**kwargs):
        outcome = outcomes.pop(0)()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    create_mock = AsyncMock(side_effect=create)
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create_mock)))
    return OpenAICompatibleToolCallingModel(client, 'm', **options), create_mock, sleeps


@pytest.mark.parametrize('kind', sorted(TRANSIENT))
async def test_transient_failures_are_asked_again(kind, monkeypatch):
    model, create, sleeps = model_with([TRANSIENT[kind], completion], monkeypatch)
    with metering(RunMeter()) as meter:
        turn = await model.generate([UserMessage('q')], ())
    assert turn.content == 'hi'
    assert create.await_count == 2 and sleeps == [0.5]
    assert meter.model_calls == 2   # every attempt is metered


@pytest.mark.parametrize('kind', sorted(FINAL))
async def test_deterministic_failures_are_not_retried(kind, monkeypatch):
    make, expected = FINAL[kind]
    model, create, sleeps = model_with([make, completion], monkeypatch)
    with pytest.raises(expected):
        await model.generate([UserMessage('q')], ())
    assert create.await_count == 1 and sleeps == []


async def test_gives_up_after_the_retry_limit_with_backoff(monkeypatch, caplog):
    model, create, sleeps = model_with([TRANSIENT['server']] * 4, monkeypatch)
    with caplog.at_level(logging.WARNING, logger=adapter.__name__):
        with pytest.raises(ModelRequestError) as caught:
            await model.generate([UserMessage('q')], ())
    assert create.await_count == 3 and sleeps == [0.5, 1.0]
    assert isinstance(caught.value.__cause__, InternalServerError)
    assert 'retry 2/2' in caplog.text and 'SECRET' not in caplog.text


async def test_retries_can_be_disabled(monkeypatch):
    model, create, _ = model_with([TRANSIENT['timeout'], completion], monkeypatch, max_retries=0)
    with pytest.raises(ModelTimeoutError):
        await model.generate([UserMessage('q')], ())
    assert create.await_count == 1


async def test_spent_budgets_stop_retrying(monkeypatch):
    model, create, sleeps = model_with([TRANSIENT['server'], completion], monkeypatch)
    meter = RunMeter(max_total_tokens=1)
    meter.record(SimpleNamespace(prompt_tokens=5, completion_tokens=0))
    with metering(meter), pytest.raises(ModelRequestError):
        await model.generate([UserMessage('q')], ())
    assert create.await_count == 1 and sleeps == []

    model, create, sleeps = model_with([TRANSIENT['server'], completion], monkeypatch)
    meter = RunMeter(timeout_seconds=0.2)   # less time left than the first backoff
    with metering(meter), pytest.raises(ModelRequestError):
        await model.generate([UserMessage('q')], ())
    assert create.await_count == 1 and sleeps == []


async def test_cancellation_and_bugs_are_never_retried(monkeypatch):
    for error in (asyncio.CancelledError, TypeError):
        model, create, sleeps = model_with([lambda e=error: e()], monkeypatch)
        with pytest.raises(error):
            await model.generate([UserMessage('q')], ())
        assert create.await_count == 1 and sleeps == []


@pytest.mark.parametrize('options', [{'max_retries': -1}, {'max_retries': True}, {'max_retries': 1.0},
                                     {'retry_delay_seconds': -1}, {'retry_delay_seconds': float('inf')},
                                     {'retry_delay_seconds': True}])
def test_invalid_retry_options(options):
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock())))
    with pytest.raises(ValueError):
        OpenAICompatibleToolCallingModel(client, 'm', **options)
