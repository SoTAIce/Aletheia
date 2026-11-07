"""Prompt-cache hits are recorded next to token usage (D-054)."""

from types import SimpleNamespace

import httpx
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion
from openai.types.completion_usage import CompletionUsage, PromptTokensDetails

from app.agent.__main__ import cache_line
from app.agent.openai_compatible_tool_calling_model import OpenAICompatibleToolCallingModel
from app.agent.run_meter import RunMeter, cache_text, metering
from app.agent.step_models import UserMessage


def usage(prompt, completion, cached=None):
    details = None if cached is None else PromptTokensDetails(cached_tokens=cached)
    return CompletionUsage(prompt_tokens=prompt, completion_tokens=completion,
                           total_tokens=prompt + completion, prompt_tokens_details=details)


def test_the_meter_adds_cached_tokens_and_counts_calls_that_report_them():
    meter = RunMeter()
    meter.record(usage(10000, 200, cached=8000))
    meter.record(usage(12000, 300))                                   # not reported
    meter.record(SimpleNamespace(prompt_tokens=5000, completion_tokens=100,
                                 prompt_tokens_details={'cached_tokens': 0}))   # reported as 0
    meter.record(SimpleNamespace(prompt_tokens=100, completion_tokens=1,
                                 prompt_tokens_details={'cached_tokens': 900}))  # capped at the prompt
    meter.record(None)
    snap = meter.snapshot()
    assert snap['cached_prompt_tokens'] == 8100 and snap['calls_with_cache_report'] == 3
    assert snap['prompt_tokens'] == 27100 and snap['model_calls'] == 5
    assert cache_text(snap) == '8100 (29% of prompt; reported by 3/5 calls)'
    assert cache_line(snap) == '其中输入命中缓存 8100（29%）'


def test_without_any_report_the_text_says_so_instead_of_zero():
    meter = RunMeter()
    meter.record(usage(1000, 10))
    snap = meter.snapshot()
    assert snap['cached_prompt_tokens'] == 0 and snap['calls_with_cache_report'] == 0
    assert cache_text(snap) == 'not reported'
    assert cache_line(snap) == '缓存命中：服务商未报告'


async def test_a_real_provider_response_reaches_the_meter():
    def respond(request):
        return httpx.Response(200, json=ChatCompletion(
            id='t', created=0, model='m', object='chat.completion',
            choices=[{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'ok'}}],
            usage=usage(4096, 20, cached=3072),
        ).model_dump())

    async with AsyncOpenAI(api_key='k', base_url='https://provider.test/v1', max_retries=0,
                           http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))) as client:
        model = OpenAICompatibleToolCallingModel(client, 'm')
        with metering(RunMeter()) as meter:
            await model.generate((UserMessage('hi'),), ())
    assert (meter.cached_prompt_tokens, meter.calls_with_cache_report) == (3072, 1)
