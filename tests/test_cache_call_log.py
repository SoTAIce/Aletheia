"""Per-call cache figures and the endpoint cache probe (D-056)."""

import json

import httpx
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion
from openai.types.completion_usage import CompletionUsage, PromptTokensDetails

from app.agent import cache_probe
from app.agent.openai_compatible_tool_calling_model import OpenAICompatibleToolCallingModel
from app.agent.run_meter import MAX_CALL_LOG, RunMeter, call_text, metering
from app.agent.step_models import AssistantMessage, SystemMessage, UserMessage


def usage(prompt, completion, cached=None):
    details = None if cached is None else PromptTokensDetails(cached_tokens=cached)
    return CompletionUsage(prompt_tokens=prompt, completion_tokens=completion,
                           total_tokens=prompt + completion, prompt_tokens_details=details)


def completion(cached=None):
    return ChatCompletion(
        id='t', created=0, model='m', object='chat.completion',
        choices=[{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'ok'}}],
        usage=usage(4096, 20, cached),
    ).model_dump()


def client_for(handler):
    return AsyncOpenAI(api_key='k', base_url='https://provider.test/v1', max_retries=0,
                       http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_each_call_is_logged_in_order_and_reaches_the_snapshot():
    meter = RunMeter()
    meter.record(usage(10000, 200, cached=8000), 0.97)
    meter.record(usage(12000, 300))
    meter.record(usage(100, 1, cached=900))          # capped at the prompt
    meter.record(None, 2.0)                           # no usage; an impossible share is dropped
    calls = meter.snapshot()['calls']
    assert calls == [
        {'prompt': 10000, 'cached': 8000, 'completion': 200, 'shared_prefix': 0.97},
        {'prompt': 12000, 'cached': None, 'completion': 300, 'shared_prefix': None},
        {'prompt': 100, 'cached': 100, 'completion': 1, 'shared_prefix': None},
        {'prompt': None, 'cached': None, 'completion': None, 'shared_prefix': None},
    ]
    assert meter.cached_prompt_tokens == 8100 and meter.calls_with_cache_report == 2
    assert call_text(calls[0]) == 'prompt 10000, cached 8000, previous request kept 97%'
    assert call_text(calls[1]) == 'prompt 12000, cached not reported, previous request kept unknown'
    json.dumps(meter.snapshot())                      # stored with the conversation turn


def test_the_log_is_bounded():
    meter = RunMeter()
    for _ in range(MAX_CALL_LOG + 5):
        meter.record(usage(10, 1))
    assert len(meter.snapshot()['calls']) == MAX_CALL_LOG and meter.model_calls == MAX_CALL_LOG + 5


async def test_the_model_measures_how_much_of_each_request_starts_like_the_previous_one():
    async with client_for(lambda request: httpx.Response(200, json=completion(cached=3072))) as client:
        model = OpenAICompatibleToolCallingModel(client, 'm')
        system, question = SystemMessage('rules ' * 200), UserMessage('question')
        with metering(RunMeter()) as meter:
            await model.generate((system, question), ())
            await model.generate((system, question, AssistantMessage('answer ' * 2000), UserMessage('more')), ())
            await model.generate((SystemMessage('other ' * 200), question), ())
    first, appended, changed = [entry['shared_prefix'] for entry in meter.call_log]
    assert first is None                     # nothing sent before
    assert appended == 1.0                   # only appended to, however much: all of it is kept
    assert changed < 0.01                    # a different system prompt keeps almost nothing


def test_the_probe_request_is_fixed_and_long_enough_to_be_cached():
    first, second = cache_probe.probe_messages(), cache_probe.probe_messages()
    assert first == second
    assert len(first[0]['content']) > 4 * 2048          # well over 1,024 tokens


async def test_the_probe_sends_identical_requests_and_reports_numbers_only():
    bodies, replies = [], iter([completion(), completion(cached=3072), completion(cached=0)])

    def respond(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=next(replies))

    async with client_for(respond) as client:
        entries = await cache_probe.run_probe(client, 'm', 3, cache_key='aletheia-probe')
    assert len({json.dumps(body, sort_keys=True) for body in bodies}) == 1
    assert bodies[0]['prompt_cache_key'] == 'aletheia-probe' and bodies[0]['max_tokens'] == 16
    assert [(e['prompt'], e['cached']) for e in entries] == [(4096, None), (4096, 3072), (4096, 0)]
    assert cache_probe.verdict(entries) == '时而命中、时而不命中：这个服务的缓存不稳定'
    assert cache_probe.entry_line(2, entries[1]).startswith('第 2 次：输入 4096，命中缓存 3072')


async def test_a_failed_probe_call_shows_only_the_error_type():
    async with client_for(lambda request: httpx.Response(400, json={'error': {'message': 'secret detail'}})) as client:
        entries = await cache_probe.run_probe(client, 'm', 2)
    assert entries == [{'error': 'BadRequestError 400'}] * 2
    assert 'secret' not in cache_probe.entry_line(1, entries[0])
    assert cache_probe.verdict(entries) == '有调用失败，无法判断'


def test_verdicts():
    hit, miss, silent = {'prompt': 9, 'cached': 9}, {'prompt': 9, 'cached': 0}, {'prompt': 9, 'cached': None}
    assert cache_probe.verdict([miss, hit, hit]) == '每次都命中缓存：这个服务的缓存是稳定的'
    assert cache_probe.verdict([miss, miss, miss]) == '后续调用都没有命中缓存：这个服务很可能没有保留缓存'
    assert cache_probe.verdict([silent, silent]) == '服务商没有报告缓存命中，无法判断'


def test_the_number_of_calls_is_bounded():
    import argparse
    import pytest
    for value in ('1', '6'):
        with pytest.raises(argparse.ArgumentTypeError):
            cache_probe._calls(value)
    assert cache_probe._calls('3') == 3
