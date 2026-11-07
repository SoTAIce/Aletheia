"""Requests keep their start stable so the provider's prompt cache applies (D-057)."""

import json
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion

from app.agent.openai_compatible_tool_calling_model import OpenAICompatibleToolCallingModel
from app.agent.step_executor import StepExecutor
from app.agent.step_models import ToolDefinition, ToolsShownOnly, UserMessage
from app.agent.tool_calling_step_runner import CONTEXT_FIELD_ORDER, ToolCallingStepRunner
from app.config import config
from app.models.taskstate import TaskState
from app.models.working_memory import WorkingMemory
from app.services.rag_agent_service import RagAgentService
from app.tools.tool_registry import ToolRegistry

READ = ToolDefinition('read_file', 'Read a file', {'type': 'object', 'properties': {'path': {'type': 'string'}}})


def reply():
    return ChatCompletion(
        id='t', created=0, model='m', object='chat.completion',
        choices=[{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'ok'}}],
    ).model_dump()


async def bodies_for(calls, **options):
    bodies = []

    def respond(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=reply())

    async with AsyncOpenAI(api_key='k', base_url='https://provider.test/v1', max_retries=0,
                           http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))) as client:
        model = OpenAICompatibleToolCallingModel(client, 'm', **options)
        for tools in calls:
            await model.generate((UserMessage('hi'),), tools)
    return bodies


async def test_tools_shown_only_are_sent_unchanged_but_cannot_be_called():
    normal, wrap_up = await bodies_for([(READ,), ToolsShownOnly((READ,))])
    assert wrap_up['tools'] == normal['tools']                  # the request starts the same way
    assert wrap_up['tool_choice'] == 'none' and 'tool_choice' not in normal
    empty, = await bodies_for([ToolsShownOnly(())])
    assert 'tools' not in empty and 'tool_choice' not in empty


async def test_prompt_cache_key_is_sent_next_to_enable_thinking():
    body, = await bodies_for([()], prompt_cache_key='aletheia-abc', enable_thinking=False)
    assert body['prompt_cache_key'] == 'aletheia-abc' and body['enable_thinking'] is False
    plain, = await bodies_for([()])
    assert 'prompt_cache_key' not in plain


@pytest.mark.parametrize('key', ['', '  ', 'x' * 65, 7])
def test_a_bad_prompt_cache_key_is_refused(key):
    with pytest.raises(ValueError):
        OpenAICompatibleToolCallingModel(AsyncOpenAI(api_key='k', base_url='https://p.test/v1'), 'm',
                                         prompt_cache_key=key)


def test_the_cache_key_is_a_hash_per_conversation_and_only_for_the_separate_endpoint(monkeypatch):
    label = RagAgentService._archive_label('alice', 'session-1')
    monkeypatch.setattr(config, 'agent_model_base_url', 'https://relay.test/v1')
    monkeypatch.setattr(config, 'agent_model_prompt_cache_key', True)
    key = RagAgentService._prompt_cache_key(label)
    assert key.startswith('aletheia-') and len(key) <= 64
    assert 'alice' not in key and 'session' not in key
    assert key == RagAgentService._prompt_cache_key(label)
    assert key != RagAgentService._prompt_cache_key(RagAgentService._archive_label('alice', 'session-2'))
    monkeypatch.setattr(config, 'agent_model_prompt_cache_key', False)
    assert RagAgentService._prompt_cache_key(label) is None
    monkeypatch.setattr(config, 'agent_model_prompt_cache_key', True)
    monkeypatch.setattr(config, 'agent_model_base_url', '')
    assert RagAgentService._prompt_cache_key(label) is None            # DashScope gets none


async def test_the_service_passes_the_key_to_the_separate_endpoint(monkeypatch):
    import app.services.rag_agent_service as module
    seen = []

    def respond(request):
        seen.append(json.loads(request.content).get('prompt_cache_key'))
        return httpx.Response(200, json=reply())

    for name, value in (('agent_model_base_url', 'https://relay.test/v1'), ('agent_model_api_key', 'relay-key'),
                        ('agent_model', 'gpt-4o'), ('agent_model_prompt_cache_key', True)):
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(module, 'AsyncOpenAI', lambda **kw: AsyncOpenAI(
        **kw, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))))
    async with RagAgentService(mcp_servers=(), include_knowledge=False)._models('aletheia-k') as (_, model):
        await model.generate((UserMessage('hi'),), ())
    assert seen == ['aletheia-k']


def contexts(steps):
    wm = WorkingMemory(TaskState('user', '解释 runtime.py'))
    wm.task_state.pin_content('{"turns": ["earlier conversation"]}', 'conversation_history')
    wm.task_state.start_planning()
    wm.install_plan(steps)
    built = []
    for step in wm.task_state.plan:
        wm.begin_step(step.step_id)
        built.append(StepExecutor(wm, AsyncMock()).build_context(step.step_id))
        wm.task_state.complete_step(step.step_id, f'result of {step.description}')
    return built


def test_stable_context_fields_come_first_and_later_steps_share_them():
    runner = ToolCallingStepRunner(AsyncMock(), ToolRegistry())
    first, second = (runner._build_initial_messages(c) for c in contexts(['读文件', '解释']))
    payload = json.loads(first[1].content)
    assert list(payload) == list(CONTEXT_FIELD_ORDER)
    assert first[0] == second[0]                                          # same system message
    a, b = first[1].content, second[1].content
    shared = len(next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b))) * 'x')
    # Everything before the step's own fields is the same in both steps; they differ only there.
    assert '"pinned_contexts"' in a[:shared] and 'earlier conversation' in a[:shared]
    assert a.index('"intermediate_results"') < shared < len(a)
