"""Web run metering: per-run RunMeter, route-call accounting, conversation cap and budgets (D-101)."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from loop_fakes import loop_reply

from app.agent.delegation import DelegationOptions
from app.agent.run_limits import RunLimits
from app.agent.run_meter import current_meter, record_model_call
from app.agent.step_models import ModelTurn
from app.api.app import create_app
from app.config import Settings, config
from app.services.rag_agent_service import RagAgentService

TOKEN = 'test-token'
AUTH = {'Authorization': f'Bearer {TOKEN}'}


class MeteredFakeModel:
    """Fake model that records token usage on the active RunMeter for each call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.meters = []

    async def generate(self, messages, tools, **kwargs):
        self.meters.append(current_meter())
        item = self._responses.pop(0)
        if callable(item):
            return await item(messages, tools)
        turn, prompt_tokens, completion_tokens = item
        record_model_call(SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens))
        return turn


class _WebClient:
    def __init__(self, service, **options):
        self.service = service
        self.app = create_app(service, user_id='local', access_token=TOKEN, **options)

    async def __aenter__(self):
        await self.service.__aenter__()
        self._lifespan = self.app.router.lifespan_context(self.app)
        await self._lifespan.__aenter__()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url='http://test', headers=AUTH,
        )
        return self

    async def __aexit__(self, *exc):
        await self.client.aclose()
        await self._lifespan.__aexit__(*exc)
        await self.service.__aexit__(*exc)

    async def send(self, session, content, request_id, **fields):
        return await self.client.post(
            f'/api/sessions/{session}/messages',
            json={'content': content, 'request_id': request_id, **fields},
        )

    async def events(self, run_id):
        response = await self.client.get(f'/api/runs/{run_id}/events')
        return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]


def _auto_service(tmp_path, model):
    team_options = DelegationOptions(
        allowed_tools=frozenset({'write_file', 'run_tests'}),
        require_passing_run=True,
    )
    return RagAgentService(
        model=model,
        mcp_servers=(),
        include_knowledge=False,
        memory_directory=tmp_path,
        execution_mode='auto',
        team_options=team_options,
    )


async def test_auto_mode_web_run_includes_route_call_in_usage(tmp_path):
    route_turn = ModelTurn(content='{"route": "single", "writes": false, "reason": "simple question"}')
    model = MeteredFakeModel([
        (route_turn, 30, 10),
        (loop_reply('Paris'), 100, 20),
    ])
    async with _WebClient(_auto_service(tmp_path, model), write_enabled=True) as web:
        resp = await web.send('s1', 'Capital of France?', 'r1')
        assert resp.status_code == 202
        events = await web.events('r1')
        done = events[-1]
        assert done['type'] == 'completed'
        usage = done['usage']
        assert usage['model_calls'] == 2
        assert usage['prompt_tokens'] == 130
        assert usage['completion_tokens'] == 30
        assert usage['total_tokens'] == 160
        assert usage['by_purpose']['route']['model_calls'] == 1
        assert usage['by_purpose']['route']['prompt_tokens'] == 30
        assert usage['by_purpose']['route']['completion_tokens'] == 10


async def test_auto_mode_route_call_counts_toward_conversation_token_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'conversation_max_tokens', 50)
    route_turn = ModelTurn(content='{"route": "single", "writes": false, "reason": "question"}')
    model = MeteredFakeModel([
        (route_turn, 40, 20),          # 60 tokens >= 50 cap
        (loop_reply('should not run'), 10, 5),
    ])
    async with _WebClient(_auto_service(tmp_path, model), write_enabled=True) as web:
        resp = await web.send('s1', 'Hello', 'r1')
        assert resp.status_code == 202
        events = await web.events('r1')
        failed = events[-1]
        assert failed['type'] == 'failed'
        assert failed['failure_kind'] == 'policy'
        assert failed['usage']['model_calls'] == 1
        assert failed['usage']['total_tokens'] == 60
        assert len(model.meters) == 1


async def test_web_run_max_tokens_fails_with_budget(tmp_path):
    route_turn = ModelTurn(content='{"route": "single", "writes": false, "reason": "question"}')
    model = MeteredFakeModel([
        (route_turn, 40, 20),          # 60 tokens >= 50 budget
        (loop_reply('never'), 10, 5),
    ])
    async with _WebClient(_auto_service(tmp_path, model), write_enabled=True, max_run_tokens=50) as web:
        resp = await web.send('s1', 'Hello', 'r1')
        assert resp.status_code == 202
        events = await web.events('r1')
        failed = events[-1]
        assert failed['type'] == 'failed'
        assert failed['failure_kind'] == 'budget'
        assert failed['usage']['total_tokens'] == 60


async def test_web_run_max_seconds_fails_with_budget(tmp_path):
    async def slow_call(messages, tools):
        await asyncio.sleep(0.2)
        return loop_reply('too late')

    model = MeteredFakeModel([slow_call])
    service = RagAgentService(
        model=model, mcp_servers=(), include_knowledge=False, memory_directory=tmp_path,
    )
    async with _WebClient(service, max_run_seconds=0.05) as web:
        resp = await web.send('s1', 'Slow task', 'r1')
        assert resp.status_code == 202
        events = await web.events('r1')
        failed = events[-1]
        assert failed['type'] == 'failed'
        assert failed['failure_kind'] == 'budget'


async def test_unset_web_run_limits_open_an_unlimited_meter(tmp_path):
    model = MeteredFakeModel([(loop_reply('ok'), 12, 8)])
    service = RagAgentService(
        model=model, mcp_servers=(), include_knowledge=False, memory_directory=tmp_path,
    )
    async with _WebClient(service) as web:
        resp = await web.send('s1', 'Ping', 'r1')
        assert resp.status_code == 202
        events = await web.events('r1')
        assert events[-1]['type'] == 'completed'
        assert len(model.meters) == 1
        meter = model.meters[0]
        assert meter is not None
        assert meter.max_total_tokens is None
        assert meter.timeout_seconds is None


def test_web_run_limit_settings_and_show_limits_rows():
    default_settings = Settings(
        _env_file=None, dashscope_api_key='k', agent_model_base_url='',
        agent_model_api_key='', agent_model='',
    )
    assert default_settings.web_run_max_tokens is None
    assert default_settings.web_run_max_seconds is None
    default_rows = dict((name, value) for _, name, value in RunLimits.current(settings=default_settings).rows())
    assert default_rows['每次运行的令牌上限（WEB_RUN_MAX_TOKENS）'] == '不限'
    assert default_rows['每次运行的时限（WEB_RUN_MAX_SECONDS）'] == '不限'

    configured = Settings(
        _env_file=None, dashscope_api_key='k', agent_model_base_url='',
        agent_model_api_key='', agent_model='',
        web_run_max_tokens=50000, web_run_max_seconds=120.0,
    )
    rows = dict((name, value) for _, name, value in RunLimits.current(settings=configured).rows())
    assert rows['每次运行的令牌上限（WEB_RUN_MAX_TOKENS）'] == '50000'
    assert rows['每次运行的时限（WEB_RUN_MAX_SECONDS）'] == '120 秒'

    with pytest.raises(ValidationError):
        Settings(_env_file=None, web_run_max_tokens=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, web_run_max_seconds=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, web_run_max_seconds=float('nan'))
