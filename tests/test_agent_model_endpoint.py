"""The agent's chat model can use its own OpenAI-compatible endpoint (D-050)."""

import json

import httpx
import pytest
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion
from pydantic import ValidationError

from app.agent.step_models import UserMessage
from app.config import Settings, config
from app.services.rag_agent_service import RagAgentService


def _settings(**kw):
    # Explicit values so the developer's own environment (.env loaded into os.environ) cannot leak in.
    fields = {'dashscope_api_key': 'ds-key', 'dashscope_base_url': 'https://dashscope.test/v1',
              'agent_model_base_url': '', 'agent_model_api_key': '', 'agent_model': '', **kw}
    return Settings(_env_file=None, **fields)


def test_without_a_separate_endpoint_the_agent_uses_dashscope_and_rag_model():
    s = _settings(rag_model='qwen-max')
    assert s.agent_model_endpoint == (s.dashscope_base_url, 'ds-key', 'qwen-max')
    assert _settings(rag_model='qwen-max', agent_model='qwen-plus').agent_model_endpoint[2] == 'qwen-plus'


def test_a_separate_endpoint_uses_its_own_key_and_model():
    s = _settings(rag_model='qwen-max', agent_model_base_url=' https://relay.test/v1 ',
                  agent_model_api_key=' relay-key ', agent_model=' gpt-4o ')
    assert s.agent_model_endpoint == ('https://relay.test/v1', 'relay-key', 'gpt-4o')
    assert s.dashscope_api_key == 'ds-key' and s.rag_model == 'qwen-max'     # embeddings and memory unchanged
    assert _settings(rag_model='qwen-max', agent_model_base_url='https://relay.test/v1',
                     agent_model_api_key='k').agent_model_endpoint[2] == 'qwen-max'


@pytest.mark.parametrize('kw', [{'agent_model_base_url': 'https://relay.test/v1'},
                                {'agent_model_base_url': 'https://relay.test/v1', 'agent_model_api_key': '  '},
                                {'agent_model_api_key': 'relay-key'}])
def test_endpoint_and_key_must_be_set_together(kw):
    # Never fall back to the DashScope key for a third-party host.
    with pytest.raises(ValidationError, match='must be set together'):
        _settings(**kw)


async def test_the_service_sends_agent_requests_to_the_separate_endpoint(monkeypatch):
    import app.services.rag_agent_service as module
    seen = []

    def respond(request):
        body = json.loads(request.content)
        assert 'enable_thinking' not in body                 # DashScope-only parameter is not sent
        seen.append((str(request.url), request.headers['authorization'], body['model']))
        return httpx.Response(200, json=ChatCompletion(
            id='t', created=0, model='t', object='chat.completion',
            choices=[{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'ok'}}],
        ).model_dump())

    for name, value in (('dashscope_api_key', 'ds-key'), ('rag_model', 'qwen-max'), ('rag_enable_thinking', False),
                        ('agent_model_base_url', 'https://relay.test/v1'),
                        ('agent_model_api_key', 'relay-key'), ('agent_model', 'gpt-4o')):
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(module, 'AsyncOpenAI', lambda **kw: AsyncOpenAI(
        **kw, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))))
    async with RagAgentService(mcp_servers=(), include_knowledge=False)._models() as model:
        assert (await model.generate((UserMessage('hi'),), ())).content == 'ok'
    assert seen == [('https://relay.test/v1/chat/completions', 'Bearer relay-key', 'gpt-4o')]
