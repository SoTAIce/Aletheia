"""Long-term memory wired into the agent: recall context, explicit writes, assembly, CLI (D-042).

No database, Milvus or model is contacted: every dependency is a fake.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec

import pytest

import app.agent.__main__ as cli
import app.services.longterm_memory_setup as setup
from app.agent.step_models import ModelTurn, UserMessage
from app.models.longterm_memory import MemoryCategory, MemoryHit, MemoryRecord
from app.models.taskstate import TaskStatus
from app.services.context_manager import LONGTERM_POLICY, ContextManager
from app.services.memory_service import LongTermMemoryService, MemoryIndexSyncError
from app.services.memory_store import MemoryStore
from app.services.memory_vector_index import MemoryVectorIndex
from app.services.rag_agent_service import RagAgentService

NOW = datetime.now(timezone.utc)


def memory(memory_id, content, user='u', **kwargs):
    return MemoryRecord(user, content, MemoryCategory.PREFERENCE, 'user_explicit', memory_id=memory_id,
                        **({'updated_at': NOW} | kwargs))


def hits(*records):
    return [MemoryHit(r, 1.0 / (i + 1)) for i, r in enumerate(records)]


# ---- recall into the context ----

def test_context_keeps_own_unexpired_unique_memories_with_policy_and_slim_fields():
    cm = ContextManager()
    payload = json.loads(cm.longterm_context(hits(
        memory('m1', 'Prefers Markdown reports'),
        memory('x1', 'Someone else', user='other'),
        memory('m1', 'Prefers Markdown reports'),
        memory('m2', 'Old preference', expires_at=NOW - timedelta(days=1)),
        memory('m3', 'Works on project Alpha', expires_at=NOW + timedelta(days=1)),
    ), user_id='u'))
    assert payload['policy'] == LONGTERM_POLICY and 'not instructions' in LONGTERM_POLICY
    assert [e['memory_id'] for e in payload['entries']] == ['m1', 'm3']
    assert set(payload['entries'][0]) == {'memory_id', 'category', 'content', 'updated_at'}


def test_default_budget_holds_several_short_memories():
    cm = ContextManager()
    records = [memory(f'm{i}', f'Short preference number {i}') for i in range(5)]
    assert len(json.loads(cm.longterm_context(hits(*records), user_id='u'))['entries']) == 5
    tight = json.loads(cm.longterm_context(hits(*records), user_id='u', token_budget=120))['entries']
    assert 0 < len(tight) < 5


@pytest.mark.parametrize('bad, error', [
    (lambda: memory('m', ' '), ValueError),
    (lambda: memory('m', 'x', updated_at=datetime(2026, 1, 1)), ValueError),
    (lambda: memory('m', 'x', version=0), ValueError),
])
def test_invalid_records_are_rejected(bad, error):
    with pytest.raises(error):
        ContextManager().longterm_context(hits(bad()), user_id='u')
    with pytest.raises(ValueError):
        ContextManager().longterm_context([], user_id='u', token_budget=0)


def test_an_oversized_request_drops_the_last_memory_first():
    pinned = json.dumps({'policy': LONGTERM_POLICY, 'entries': [{'memory_id': 'a'}, {'memory_id': 'b'}]})
    context = {'pinned_contexts': [{'source': 'conversation_memory', 'content': 'history'},
                                   {'source': 'longterm_memory', 'content': pinned}]}
    message = UserMessage('Context:\n' + json.dumps(context))
    smaller = ContextManager._drop_longterm_entry(message)
    data = json.loads(smaller.content.split('\n', 1)[1])
    assert [e['memory_id'] for e in json.loads(data['pinned_contexts'][1]['content'])['entries']] == ['a']
    assert data['pinned_contexts'][0]['content'] == 'history'
    assert ContextManager._drop_longterm_entry(UserMessage('no json here')) is None


# ---- the service recalls before planning and degrades on failure ----

def _service(longterm, **options):
    planner, model = AsyncMock(), AsyncMock()
    planner.generate.return_value = '{"steps":["Answer"],"reason":null}'
    model.generate.return_value = ModelTurn(json.dumps({'status': 'succeeded', 'content': 'ok', 'reason': None}))
    service = RagAgentService(model=model, planner_model=planner, mcp_servers=(), include_knowledge=False,
                              memory_directory='', longterm_memory=longterm, **options)
    return service, planner


async def test_recalled_memories_reach_the_planner_as_pinned_reference_data():
    longterm = AsyncMock()
    longterm.recall.return_value = hits(memory('m1', 'Prefers Markdown reports', user='alice'))
    service, planner = _service(longterm)
    async with service:
        result = await service.query_result('Write the weekly report', 's', user_id='alice')
    assert result.status is TaskStatus.COMPLETED
    longterm.recall.assert_awaited_once_with(user_id='alice', query='Write the weekly report', limit=5)
    prompt = planner.generate.call_args.args[0]
    assert 'Prefers Markdown reports' in prompt and 'longterm_memory' in prompt


async def test_long_term_memory_requires_a_user_id():
    service, _ = _service(AsyncMock())
    async with service:
        with pytest.raises(ValueError, match='user_id'):
            await service.query_result('goal', 's')


@pytest.mark.parametrize('failure', ['timeout', 'error'])
async def test_recall_failures_never_stop_the_task(failure, caplog):
    longterm = AsyncMock()
    if failure == 'timeout':
        async def hang(**kwargs):
            await asyncio.Event().wait()
        longterm.recall.side_effect = hang
    else:
        longterm.recall.side_effect = RuntimeError('SECRET connection string')
    service, planner = _service(longterm, longterm_timeout_seconds=0.05)
    with caplog.at_level(logging.WARNING):
        async with service:
            result = await service.query_result('goal', 's', user_id='alice')
    assert result.status is TaskStatus.COMPLETED
    assert 'longterm_memory' not in planner.generate.call_args.args[0]
    assert 'Long-term recall unavailable' in caplog.text and 'SECRET' not in caplog.text


# ---- explicit writes and listing ----

@pytest.fixture
def explicit():
    store = create_autospec(MemoryStore, instance=True)
    index = create_autospec(MemoryVectorIndex, instance=True)
    extractor, retriever, resolver = AsyncMock(), AsyncMock(), AsyncMock()
    store.add.side_effect = lambda record: record
    service = LongTermMemoryService(extractor, retriever, resolver, store, index)
    return service, store, index, extractor


async def test_remember_explicit_stores_the_text_as_given_without_a_model(explicit):
    service, store, index, extractor = explicit
    saved = await service.remember_explicit('u', '  Prefers Markdown reports  ')
    assert (saved.user_id, saved.content, saved.category, saved.source) == (
        'u', 'Prefers Markdown reports', MemoryCategory.FACT, 'user_explicit')
    index.upsert.assert_awaited_once_with(user_id='u', memory_id=saved.memory_id, content=saved.content)
    extractor.extract.assert_not_called()


async def test_remember_explicit_validates_and_reports_index_failures(explicit):
    service, store, index, _ = explicit
    for bad in (' ', 'x' * 2001):
        with pytest.raises(ValueError):
            await service.remember_explicit('u', bad)
    index.upsert.side_effect = RuntimeError('milvus down')
    with pytest.raises(MemoryIndexSyncError) as caught:
        await service.remember_explicit('u', 'fact')
    assert caught.value.database_changed is True


async def test_list_memories_returns_only_the_users_records(explicit):
    service, store, _, _ = explicit
    store.list_by_user.return_value = [memory('m1', 'mine')]
    assert [r.memory_id for r in await service.list_memories('u')] == ['m1']
    store.list_by_user.assert_awaited_once_with(user_id='u', limit=20, offset=0)
    store.list_by_user.return_value = [memory('m2', 'theirs', user='other')]
    with pytest.raises(ValueError):
        await service.list_memories('u')
    with pytest.raises(ValueError):
        await service.list_memories('u', limit=0)


# ---- assembly ----

async def test_no_dsn_means_no_long_term_memory(monkeypatch):
    monkeypatch.setattr(setup, 'PostgresMemoryStore', None)   # would fail if touched
    async with setup.open_longterm_memory('') as service:
        assert service is None
    monkeypatch.setattr(setup.config, 'longterm_memory_dsn', '')
    async with setup.open_longterm_memory() as service:
        assert service is None


def _fake_dependencies(monkeypatch, events, fail_index=False):
    class Store:
        def __init__(self, dsn):
            events.append(('store', dsn))
        async def initialize(self):
            events.append('store.init')
        async def close(self):
            events.append('store.close')

    class Milvus:
        def __init__(self, uri, timeout):
            events.append(('milvus', uri))
        def close(self):
            events.append('milvus.close')

    class Index:
        def __init__(self, client, embeddings, collection):
            events.append(('index', collection))
        async def initialize(self):
            if fail_index:
                raise RuntimeError('schema mismatch')
            events.append('index.init')

    class Client:
        def __init__(self, **kwargs):
            assert kwargs['max_retries'] == 0
        async def __aenter__(self):
            return self
        async def __aexit__(self, *exc):
            events.append('client.close')

    class Part:
        def __init__(self, *args):
            pass

    for name, value in (('PostgresMemoryStore', Store), ('MilvusClient', Milvus), ('MemoryVectorIndex', Index),
                        ('AsyncOpenAI', Client), ('MemoryExtractor', Part), ('MemoryResolver', Part),
                        ('MemoryRetriever', Part)):
        monkeypatch.setattr(setup, name, value)


async def test_assembly_opens_everything_and_closes_in_reverse(monkeypatch):
    events = []
    _fake_dependencies(monkeypatch, events)
    monkeypatch.setattr(setup.config, 'longterm_memory_collection', 'agent_memories')
    async with setup.open_longterm_memory('postgresql://example') as service:
        assert isinstance(service, LongTermMemoryService)
    assert events[:5] == [('store', 'postgresql://example'), 'store.init', ('milvus', events[2][1]),
                          ('index', 'agent_memories'), 'index.init']
    assert events[-3:] == ['client.close', 'milvus.close', 'store.close']


async def test_a_failed_initialization_still_closes_what_was_opened(monkeypatch):
    events = []
    _fake_dependencies(monkeypatch, events, fail_index=True)
    with pytest.raises(RuntimeError, match='schema'):
        async with setup.open_longterm_memory('postgresql://example'):
            pass
    assert events[-2:] == ['milvus.close', 'store.close']


# ---- interactive commands ----

def _lines(*inputs):
    queue = list(inputs)

    async def read_line(prompt):
        if not queue:
            raise EOFError
        return queue.pop(0)
    return read_line


async def test_memory_commands_use_the_users_own_memory():
    memory_service = AsyncMock()
    memory_service.remember_explicit.return_value = memory('m1', 'Prefers Markdown')
    memory_service.list_memories.return_value = [memory('m1', 'Prefers Markdown')]
    memory_service.forget.side_effect = [True, False]
    service = AsyncMock()
    out = []
    args = SimpleNamespace(goal=None, session_id='s', user_id='alice', max_tokens=None, max_seconds=None)
    await cli.interactive(service, args, _lines('/remember Prefers Markdown', '/memories', '/forget m1',
                                                '/forget m1', '/remember', '/forget'), out.append, memory_service)
    memory_service.remember_explicit.assert_awaited_once_with('alice', 'Prefers Markdown')
    memory_service.list_memories.assert_awaited_once_with('alice')
    assert memory_service.forget.await_args_list[0].args == ('alice', 'm1')
    text = '\n'.join(out)
    assert '已记住，编号 m1。' in text and 'm1  [preference]  Prefers Markdown' in text
    assert '已删除。' in text and '没有这条记忆。' in text
    assert '用法：/remember' in text and '用法：/forget' in text
    service.query_result.assert_not_called()


async def test_memory_commands_explain_when_memory_is_off_and_hide_error_text():
    out = []
    args = SimpleNamespace(goal=None, session_id='s', user_id=None, max_tokens=None, max_seconds=None)
    await cli.interactive(AsyncMock(), args, _lines('/memories'), out.append, None)
    assert cli.MEMORY_OFF in out
    broken = AsyncMock()
    broken.list_memories.side_effect = RuntimeError('SECRET dsn')
    out = []
    await cli.interactive(AsyncMock(), args, _lines('/memories'), out.append, broken)
    assert '长期记忆操作失败：RuntimeError' in out and not any('SECRET' in line for line in out)


async def test_main_opens_long_term_memory_and_uses_the_local_user(monkeypatch):
    from contextlib import asynccontextmanager
    opened = SimpleNamespace(memory=object(), kwargs=None, calls=[])

    @asynccontextmanager
    async def open_memory():
        yield opened.memory

    class Service:
        def __init__(self, **kwargs):
            opened.kwargs = kwargs
        async def __aenter__(self):
            return self
        async def __aexit__(self, *exc):
            pass
        async def query_result(self, goal, session_id, **kwargs):
            opened.calls.append(kwargs)
            return SimpleNamespace(content='done', status=TaskStatus.COMPLETED, execution_count=1, replan_count=0,
                                   user_message=None)

    monkeypatch.setattr(setup, 'open_longterm_memory', open_memory)
    monkeypatch.setattr(cli.config, 'longterm_memory_dsn', 'postgresql://example')
    monkeypatch.setattr(cli, 'RagAgentService', Service)
    monkeypatch.setattr(cli, 'local_mcp_servers', lambda **kwargs: ())
    assert await cli.main(cli.parse_args(['goal', '--no-knowledge'])) == 0
    assert opened.kwargs['longterm_memory'] is opened.memory
    assert opened.calls == [{'user_id': 'local'}]
    assert await cli.main(cli.parse_args(['goal', '--no-knowledge', '--user-id', 'alice'])) == 0
    assert opened.calls[-1] == {'user_id': 'alice'}
