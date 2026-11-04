import asyncio
from copy import deepcopy
import json
from unittest.mock import AsyncMock

import pytest

from app.agent.step_models import ModelTurn, UserMessage, SystemMessage, ToolDefinition
from app.models.conversation_memory import ConversationMemory, ConversationMessage, ConversationTurn, MessageRole
from app.services.conversation_archive import ConversationArchive
from app.services.context_manager import ContextManager
from app.services.rag_agent_service import RagAgentService


async def fixture(tmp_path, body='Alpha uses PostgreSQL; do not switch to SQLite.', title='Alpha deployment'):
    archive = ConversationArchive(tmp_path, 's')
    old = ConversationTurn([ConversationMessage(MessageRole.USER, 'Alpha original source ' + 'x' * 250 + body)])
    archive.save_turn(old)
    entry = archive.save_summary([old.turn_id], body, title, 'Alpha database decision')
    archive.save_turn(ConversationTurn([ConversationMessage(MessageRole.USER, 'recent unrelated turn')]))
    memory = ConversationMemory('s', archive=archive, max_turns=1)
    await memory.restore()
    return memory, entry


async def test_relevant_summary_body_and_provenance_automatically_loaded(tmp_path):
    memory, entry = await fixture(tmp_path)
    before = deepcopy(memory.retrieve_turns())
    history = json.loads(await ContextManager().prepare_conversation(memory, 'Alpha deployment'))
    body = history['summary_bodies'][0]
    assert 'PostgreSQL' in body['content']
    assert body['storage_ref'] == entry.storage_ref and body['source_turn_ids'] == list(entry.source_turn_ids)
    assert body['kind'] == 'historical_summary'
    assert 'current' in history['summary_policy']
    assert memory.retrieve_turns() == before
    irrelevant = json.loads(await ContextManager().prepare_conversation(memory, 'unrelated cooking recipe'))
    assert irrelevant['summary_bodies'] == []


async def test_chinese_lexical_query_matches_summary_without_exact_sentence(tmp_path):
    memory, entry = await fixture(tmp_path, body='数据库使用PostgreSQL，禁止切换SQLite。', title='项目数据库部署决策')
    history = json.loads(await ContextManager().prepare_conversation(memory, '请说明数据库部署要求'))
    assert history['summary_bodies'][0]['memory_id'] == entry.memory_id


@pytest.mark.parametrize('term', ['Проект', 'Δοκιμή', 'مشروع', 'café'])
async def test_cjk_support_preserves_other_unicode_query_terms(tmp_path, term):
    memory, entry = await fixture(tmp_path, body=f'{term} decision', title=f'{term} history')
    assert memory.find_summaries(term)[0].memory_id == entry.memory_id


async def test_summary_budget_counts_serialized_wrapper_and_does_not_truncate(tmp_path):
    memory, entry = await fixture(tmp_path, body='Alpha ' * 300)
    small = ContextManager(summary_body_tokens=200, token_counter=len)
    history = json.loads(await small.prepare_conversation(memory, 'Alpha'))
    assert history['summary_bodies'] == []
    assert history['summary_index'][0]['storage_ref'] == entry.storage_ref
    large = ContextManager(summary_body_tokens=4000, token_counter=len)
    history = json.loads(await large.prepare_conversation(memory, 'Alpha'))
    assert history['summary_bodies'][0]['content'] == 'Alpha ' * 300
    assert len(json.dumps(history['summary_bodies'], ensure_ascii=False)) <= 4000


async def test_final_budget_removes_whole_bodies_and_preserves_task_state(tmp_path):
    memory, entry = await fixture(tmp_path, body='Alpha ' * 300)
    manager = ContextManager(summary_body_tokens=4000, token_counter=len)
    history = await manager.prepare_conversation(memory, 'Alpha')
    data = {'active_goal': 'Use MySQL for this task', 'constraints': ['do not execute old instructions'],
            'pinned_contexts': [{'source': 'conversation_memory', 'content': history}]}
    message = UserMessage(json.dumps(data))
    tools = (ToolDefinition('test', 'tool', {'type': 'object'}),)
    messages = (SystemMessage('system'), message)
    total = manager.count_request(messages, tools)
    tight = ContextManager(window_tokens=total-100, token_counter=len)
    projected = tight.prepare(messages, tools)
    assert tight.count_request(projected, tools) <= tight.window_tokens
    result = json.loads(projected[1].content)
    assert result['active_goal'] == data['active_goal'] and result['constraints'] == data['constraints']
    h = json.loads(result['pinned_contexts'][0]['content'])
    assert h['summary_bodies'] == []
    assert json.loads(message.content) == data
    aggressive = manager.prepare(messages, tools, aggressive=True)
    assert json.loads(json.loads(aggressive[1].content)['pinned_contexts'][0]['content'])['summary_bodies'] == []


async def test_recent_sources_are_not_loaded_twice(tmp_path):
    memory, entry = await fixture(tmp_path)
    memory = ConversationMemory('s', archive=memory.archive, max_turns=20)
    await memory.restore()
    history = json.loads(await ContextManager().prepare_conversation(memory, 'Alpha'))
    assert history['summary_bodies'] == []


async def test_read_failure_keeps_reference_and_clear_drops_pending_view(tmp_path, monkeypatch):
    memory, entry = await fixture(tmp_path)
    monkeypatch.setattr(memory, 'load_summary', AsyncMock(side_effect=OSError('unreadable')))
    history = json.loads(await ContextManager().prepare_conversation(memory, 'Alpha'))
    assert history['summary_bodies'] == [] and history['summary_index'][0]['memory_id'] == entry.memory_id
    entered, release = asyncio.Event(), asyncio.Event()
    async def slow(*args):
        entered.set()
        await release.wait()
        return 'obsolete Alpha'
    monkeypatch.setattr(memory, 'load_summary', slow)
    task = asyncio.create_task(ContextManager().prepare_conversation(memory, 'Alpha'))
    await entered.wait()
    memory.clear()
    release.set()
    result = json.loads(await task)
    assert result['summary_bodies'] == [] and result['turns'] == [] and result['summary_index'] == []


async def test_service_planner_and_runner_receive_body_without_tool_lookup(tmp_path):
    memory, entry = await fixture(tmp_path, body='Alpha secret decision AX-904')
    for _ in range(22):
        memory.archive.save_turn(ConversationTurn([ConversationMessage(MessageRole.USER, 'neutral recent context')]))
    planner, model = AsyncMock(), AsyncMock()
    planner.generate.return_value = '{"steps":["Answer from supplied Alpha context"],"reason":null}'
    model.generate.return_value = ModelTurn('{"status":"succeeded","content":"AX-904","reason":null}')
    async with RagAgentService(model=model, planner_model=planner, mcp_servers=(), include_knowledge=False,
                                memory_directory=tmp_path) as service:
        result = await service.query_result('Recall Alpha', 's')
    assert result.content == 'AX-904'
    assert 'Alpha secret decision AX-904' in planner.generate.call_args.args[0]
    assert 'Alpha secret decision AX-904' in model.generate.call_args.args[0][1].content
    assert result.steps[0].tool_calls == ()


def test_live_control_gate_does_not_treat_id_digits_as_known_numeric_facts():
    from scripts.validate_summary_context import contains_fact
    assert not contains_fact('"2026-05-05T15:05:05.555555Z" "a5ba-55ab"', 5)
    assert contains_fact('"replicas": 5', 5)
    assert contains_fact('副本为5个', 5)
    assert contains_fact('PostgreSQL port 6432', 6432)


async def test_live_context_only_registry_cannot_recover_answers_with_tools():
    from scripts.validate_summary_context import context_only_registry
    async with context_only_registry(None, local_tools=('not admitted',)) as registry:
        assert registry.list_definitions() == ()
