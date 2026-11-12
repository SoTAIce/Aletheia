"""Stage 4d-1: the agent model's settings as one profile; requests stay byte-for-byte the same."""

import itertools

import httpx
import pytest
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion

from app.agent.openai_compatible_tool_calling_model import OpenAICompatibleToolCallingModel, profile_model
from app.agent.step_models import UserMessage
from app.config import Settings
from app.models.model_profile import ModelProfile


def _settings(**kw):
    fields = {'dashscope_api_key': 'ds-key', 'dashscope_base_url': 'https://dashscope.test/v1',
              'agent_model_base_url': '', 'agent_model_api_key': '', 'agent_model': '',
              'rag_model': 'qwen-max', **kw}
    return Settings(_env_file=None, **fields)


def _old_model(client, s, cache_key):
    """The construction in rag_agent_service._base_models before 4d-1, kept verbatim as the reference."""
    if not (s.agent_model_base_url and s.agent_model_prompt_cache_key):
        cache_key = None
    model_name = s.agent_model or s.rag_model
    return OpenAICompatibleToolCallingModel(
        client, model_name, temperature=0, json_mode=s.rag_json_mode,
        enable_thinking=None if s.agent_model_base_url else s.rag_enable_thinking,
        max_output_tokens=s.context_output_tokens, prompt_cache_key=cache_key)


def _new_model(client, s, cache_key):
    profile = s.agent_model_profile
    return profile_model(client, profile, cache_key if profile.prompt_cache_key else None)


async def _request(build, s):
    seen = []

    def respond(request):
        seen.append((str(request.url), request.headers['authorization'], request.content))
        return httpx.Response(200, json=ChatCompletion(
            id='t', created=0, model='t', object='chat.completion',
            choices=[{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'ok'}}],
        ).model_dump())

    base_url, api_key, _ = s.agent_model_endpoint
    async with AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0,
                           http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))) as client:
        await build(client, s, 'aletheia-key').generate((UserMessage('hi'),), ())
    return seen


SEPARATE = {'agent_model_base_url': 'https://relay.test/v1', 'agent_model_api_key': 'relay-key',
            'agent_model': 'gpt-x'}


@pytest.mark.parametrize('separate, thinking, json_mode, cache_flag', list(itertools.product(
    (False, True), (None, False, True), (False, True), (False, True))))
async def test_the_profile_sends_the_same_request_bytes_as_before(separate, thinking, json_mode, cache_flag):
    s = _settings(**(SEPARATE if separate else {}), rag_enable_thinking=thinking, rag_json_mode=json_mode,
                  agent_model_prompt_cache_key=cache_flag, context_output_tokens=4096)
    assert await _request(_new_model, s) == await _request(_old_model, s)


def test_the_default_profile_follows_the_settings():
    dash = _settings(rag_enable_thinking=False, agent_model_context_tokens=None, context_window_tokens=32768,
                     model_input_price_per_million_tokens=2.0, model_output_price_per_million_tokens=8.0)
    p = dash.agent_model_profile
    assert (p.name, p.base_url, p.api_key_env, p.model) == ('default', 'https://dashscope.test/v1',
                                                           'DASHSCOPE_API_KEY', 'qwen-max')
    assert (p.context_tokens, p.enable_thinking, p.prompt_cache_key) == (32768, False, False)
    assert p.api_key(dash) == 'ds-key' and p.estimated_cost(1_000_000, 500_000) == 6.0
    relay = _settings(**SEPARATE, rag_enable_thinking=False, agent_model_context_tokens=128000)
    q = relay.agent_model_profile
    assert (q.base_url, q.api_key_env, q.model, q.context_tokens) == (
        'https://relay.test/v1', 'AGENT_MODEL_API_KEY', 'gpt-x', 128000)
    assert q.enable_thinking is None and q.prompt_cache_key is True and q.api_key(relay) == 'relay-key'
    assert q.estimated_cost(10, 10) is None
    assert relay.agent_model_endpoint == ('https://relay.test/v1', 'relay-key', 'gpt-x')
    assert relay.agent_model_window_tokens == 128000


def test_a_profile_never_holds_or_shows_the_key():
    s = _settings(**SEPARATE)
    shown = repr(s.agent_model_profile)
    assert 'relay-key' not in shown and 'ds-key' not in shown and 'AGENT_MODEL_API_KEY' in shown


def test_the_key_falls_back_to_the_environment(monkeypatch):
    p = ModelProfile('cheap', 'https://x.test/v1', 'CHEAP_MODEL_API_KEY', 'm', 1000, 100, 30)
    monkeypatch.setenv('CHEAP_MODEL_API_KEY', ' k ')
    assert p.api_key() == 'k' and p.api_key(_settings()) == 'k'
    monkeypatch.delenv('CHEAP_MODEL_API_KEY')
    assert p.api_key() == ''


@pytest.mark.parametrize('kw', [
    {'name': 'bad name'}, {'api_key_env': 'not-a-var'}, {'context_tokens': 0}, {'max_output_tokens': 1.5},
    {'timeout_seconds': float('inf')}, {'temperature': 3}, {'json_mode': 1}, {'enable_thinking': 'yes'},
    {'input_price_per_million': -1}, {'output_price_per_million': float('nan')}])
def test_invalid_profiles_are_refused(kw):
    base = dict(name='p', base_url='https://x.test/v1', api_key_env='K', model='m', context_tokens=1000,
                max_output_tokens=100, timeout_seconds=30)
    with pytest.raises((TypeError, ValueError)):
        ModelProfile(**{**base, **kw})


def test_requests_carry_the_cache_key_only_when_the_profile_supports_it():
    on = ModelProfile('a', 'u', 'K', 'm', 1000, 100, 30, prompt_cache_key=True)
    off = ModelProfile('b', 'u', 'K', 'm', 1000, 100, 30, prompt_cache_key=False)
    client = AsyncOpenAI(api_key='k', base_url='https://x.test/v1')
    assert profile_model(client, on, 'key')._prompt_cache_key == 'key'
    assert profile_model(client, off, 'key')._prompt_cache_key is None
