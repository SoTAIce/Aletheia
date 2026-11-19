"""Persona memories (docs/persona_memory_plan.md): ownership, scoped recall, deleting with a persona.

Offline only: stand-in connections, stores and indexes; no database, Milvus or model.
"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, create_autospec

import httpx
import pytest

from app.agent import __main__ as cli
from app.agent import persona_commands
from app.agent.step_models import ModelTurn
from app.api.app import create_app
from app.models.longterm_memory import (
    ALL_PERSONAS, MemoryCategory, MemoryHit, MemoryRecord, check_scope, in_scope,
)
from app.services.memory_retriever import MemoryRetriever
from app.services.memory_service import LongTermMemoryService, MemoryIndexSyncError
from app.services.memory_store import MemoryStore
from app.services.memory_vector_index import MemoryVectorIndex
from app.services.personas import DEFAULT_ID, PersonaStore
from app.services.postgres_memory_store import PostgresMemoryStore
from app.services.rag_agent_service import RagAgentService

TOKEN = 'test-token'
AUTH = {'Authorization': f'Bearer {TOKEN}'}


def memory(memory_id, persona_id=None, user_id='local', content=None):
    return MemoryRecord(memory_id=memory_id, user_id=user_id, content=content or f'memory {memory_id}',
                        category=MemoryCategory.FACT, source='user_explicit', persona_id=persona_id)


# ---- scopes ---------------------------------------------------------------------------------------

def test_scopes():
    assert repr(ALL_PERSONAS) == 'ALL_PERSONAS' and type(ALL_PERSONAS)() is ALL_PERSONAS
    for scope in (ALL_PERSONAS, None, 'p-1'):
        check_scope(scope)
    for bad in ('', ' ', 3, 'x' * 101):
        with pytest.raises(ValueError):
            check_scope(bad)
    assert in_scope(None, None) and in_scope(None, 'p-1') and in_scope('p-2', ALL_PERSONAS)
    assert in_scope('p-1', 'p-1') and not in_scope('p-2', 'p-1') and not in_scope('p-1', None)


# ---- PostgreSQL ------------------------------------------------------------------------------------

def store_with(conn):
    @asynccontextmanager
    async def acquire():
        yield conn

    store = PostgresMemoryStore('postgresql://localhost/test')
    store._pool = SimpleNamespace(acquire=Mock(side_effect=acquire))
    return store


def flat(sql):
    return ' '.join(sql.split())


async def test_add_writes_the_persona_and_rows_without_it_are_shared():
    conn = SimpleNamespace(execute=AsyncMock())
    item = memory('m1', persona_id='p-1')
    await store_with(conn).add(item)
    sql, *values = conn.execute.call_args.args
    assert 'persona_id' in sql and values[-1] == 'p-1'
    row = {'memory_id': 'm1', 'user_id': 'u', 'content': 'c', 'category': 'fact', 'source': 's',
           'source_id': None, 'version': 1, 'created_at': item.created_at, 'updated_at': item.updated_at,
           'expires_at': None}
    assert PostgresMemoryStore._row_to_record(row).persona_id is None              # a row from before
    assert PostgresMemoryStore._row_to_record(row | {'persona_id': 'p-1'}).persona_id == 'p-1'
    with pytest.raises(ValueError):
        await store_with(conn).add(memory('m2', persona_id=' '))


@pytest.mark.parametrize('scope, condition, extra', [
    (ALL_PERSONAS, None, []),
    (None, 'AND persona_id IS NULL', []),
    ('p-1', 'AND (persona_id IS NULL OR persona_id = $4)', ['p-1']),
])
async def test_keyword_search_filters_by_scope(scope, condition, extra):
    conn = SimpleNamespace(fetch=AsyncMock(return_value=[]))
    store = store_with(conn)
    if scope is ALL_PERSONAS:
        await store.search('u', 'q', limit=7)
    else:
        await store.search('u', 'q', limit=7, persona_scope=scope)
    sql, *values = conn.fetch.call_args.args
    assert values == ['u', 'q', 7, *extra]
    if condition is None:
        assert 'persona_id' not in sql
    else:
        assert condition in flat(sql)


async def test_count_and_delete_by_persona():
    conn = SimpleNamespace(fetchval=AsyncMock(return_value=3),
                           fetch=AsyncMock(return_value=[{'memory_id': 'a'}, {'memory_id': 'b'}]))
    store = store_with(conn)
    assert await store.count_by_persona('u', 'p-1') == 3
    sql, *values = conn.fetchval.call_args.args
    assert flat(sql) == 'SELECT COUNT(*) FROM memories WHERE user_id = $1 AND persona_id = $2'
    assert values == ['u', 'p-1']
    assert await store.delete_by_persona('u', 'p-1') == ['a', 'b']
    sql, *values = conn.fetch.call_args.args
    assert flat(sql) == 'DELETE FROM memories WHERE user_id = $1 AND persona_id = $2 RETURNING memory_id'
    for call in (store.count_by_persona, store.delete_by_persona):
        with pytest.raises(ValueError):
            await call('u', '')


def test_update_never_changes_the_persona():
    import inspect
    source = inspect.getsource(PostgresMemoryStore.update)
    assert 'persona_id' not in source


# ---- recall -----------------------------------------------------------------------------------------

@pytest.fixture
def retriever():
    store = create_autospec(MemoryStore, instance=True)
    index = create_autospec(MemoryVectorIndex, instance=True)
    records = {r.memory_id: r for r in (memory('shared'), memory('mine', 'p-1'), memory('theirs', 'p-2'))}
    store.search.return_value = []
    index.search.return_value = [(memory_id, 0.1) for memory_id in records]
    store.get.side_effect = lambda user_id, memory_id: records.get(memory_id)
    return MemoryRetriever(store, index), store, index


@pytest.mark.parametrize('scope, expected', [
    ('p-1', {'shared', 'mine'}),
    (None, {'shared'}),
])
async def test_scoped_recall_skips_other_personas(retriever, scope, expected):
    subject, store, index = retriever
    hits = await subject.retrieve('local', 'q', limit=5, persona_scope=scope)
    assert {hit.record.memory_id for hit in hits} == expected
    assert index.search.call_args.kwargs['limit'] == 30           # max(5 * 6, 20)
    assert store.search.call_args.kwargs == {'limit': 30, 'persona_scope': scope}


async def test_unscoped_recall_is_unchanged(retriever):
    subject, store, index = retriever
    hits = await subject.retrieve('local', 'q', limit=5)
    assert {hit.record.memory_id for hit in hits} == {'shared', 'mine', 'theirs'}
    assert store.search.call_args.kwargs == {'limit': 15}


# ---- the memory service -----------------------------------------------------------------------------

def memory_service():
    store = create_autospec(MemoryStore, instance=True)
    index = create_autospec(MemoryVectorIndex, instance=True)
    retriever = AsyncMock()
    store.add.side_effect = lambda record: record
    return LongTermMemoryService(AsyncMock(), retriever, AsyncMock(), store, index), store, index, retriever


async def test_remember_explicit_and_recall_carry_the_persona():
    service, store, _, retriever = memory_service()
    saved = await service.remember_explicit('u', 'likes FastAPI', persona_id='p-1')
    assert saved.persona_id == 'p-1' and (await service.remember_explicit('u', 'x')).persona_id is None
    retriever.retrieve.return_value = []
    await service.recall('u', 'q', limit=3, persona_scope='p-1')
    assert retriever.retrieve.call_args.kwargs == {'user_id': 'u', 'query': 'q', 'limit': 3,
                                                   'persona_scope': 'p-1'}
    await service.recall('u', 'q', limit=3)
    assert retriever.retrieve.call_args.kwargs == {'user_id': 'u', 'query': 'q', 'limit': 3}
    with pytest.raises(ValueError):
        await service.remember_explicit('u', 'x', persona_id='')


async def test_forget_persona_deletes_rows_then_vectors():
    service, store, index, _ = memory_service()
    store.delete_by_persona.return_value = ['a', 'b']
    store.count_by_persona.return_value = 2
    assert await service.count_persona('u', 'p-1') == 2
    assert await service.forget_persona('u', 'p-1') == 2
    assert [c.kwargs for c in index.delete.call_args_list] == [{'user_id': 'u', 'memory_id': 'a'},
                                                              {'user_id': 'u', 'memory_id': 'b'}]
    index.delete.side_effect = RuntimeError('milvus down')
    with pytest.raises(MemoryIndexSyncError) as error:
        await service.forget_persona('u', 'p-1')
    assert error.value.database_changed is True
    for call in (service.forget_persona, service.count_persona):
        with pytest.raises(ValueError):
            await call('u', None)


# ---- the service facade -------------------------------------------------------------------------------

class Recorder:
    def __init__(self):
        self.calls = []

    async def recall(self, **kwargs):
        self.calls.append(kwargs)
        return []


def facade(tmp_path, personas=True):
    model = AsyncMock()
    model.generate.side_effect = [ModelTurn('a'), ModelTurn('b')]
    recorder = Recorder()
    service = RagAgentService(model=model, mcp_servers=(), include_knowledge=False, longterm_memory=recorder,
                              personas_dir=str(tmp_path / 'personas') if personas else '')
    return service, recorder


async def test_recall_scope_follows_the_session_persona(tmp_path):
    service, recorder = facade(tmp_path)
    persona = service.personas.create('u', 'Backend')
    service.personas.bind('u', 'other', persona.persona_id)
    service.personas.bind('u', 'plain', DEFAULT_ID)
    async with service:
        await service.query_result('goal', 'plain', user_id='u')
        await service.query_result('goal', 'other', user_id='u')
    assert [c['persona_scope'] for c in recorder.calls] == [None, persona.persona_id]


async def test_recall_without_personas_is_unscoped(tmp_path):
    service, recorder = facade(tmp_path, personas=False)
    async with service:
        await service.query_result('goal', 's', user_id='u')
    assert 'persona_scope' not in recorder.calls[0]


# ---- the command line -----------------------------------------------------------------------------------

def fake_memory(count=0):
    service = AsyncMock()
    service.remember_explicit.side_effect = lambda user_id, content, **kw: memory(
        'm1', kw.get('persona_id'), user_id, content)
    service.count_persona.return_value = count
    service.forget_persona.return_value = count
    return service


async def test_remember_goes_to_the_session_persona_unless_shared(tmp_path):
    store = PersonaStore(tmp_path / 'personas')
    persona = store.create('local', '学 agent')
    store.bind('local', 's', persona.persona_id)
    service, out = fake_memory(), []
    await cli.memory_command('/remember 注意力机制没懂', service, 'local', out.append, store=store, session_id='s')
    await cli.memory_command('/remember --shared 我叫小明', service, 'local', out.append, store=store, session_id='s')
    assert service.remember_explicit.await_args_list[0].kwargs == {'persona_id': persona.persona_id}
    assert service.remember_explicit.await_args_list[1].kwargs == {}
    assert out == ['已记住（属于「学 agent」），编号 m1。', '已记住（公共的），编号 m1。']
    store.bind('local', 's', DEFAULT_ID)
    await cli.memory_command('/remember 默认下记的', service, 'local', out.append, store=store, session_id='s')
    assert service.remember_explicit.await_args_list[2].kwargs == {} and out[-1] == '已记住（公共的），编号 m1。'


async def test_memories_name_their_owner(tmp_path):
    store = PersonaStore(tmp_path / 'personas')
    persona = store.create('local', '学 agent')
    service, out = fake_memory(), []
    service.list_memories.return_value = [memory('a'), memory('b', persona.persona_id), memory('c', 'p-gone')]
    await cli.memory_command('/memories', service, 'local', out.append, store=store, session_id='s')
    assert out == ['a  [fact]  [公共]  memory a', 'b  [fact]  [学 agent]  memory b',
                   'c  [fact]  [已删除的人设]  memory c']


def answers(*lines):
    queue = list(lines)

    async def read_line(prompt):
        if not queue:
            raise EOFError
        return queue.pop(0)
    return read_line


async def run_delete(store, memory_service, *typed):
    out = []
    await persona_commands.persona_command('/persona delete 学 agent', SimpleNamespace(personas=store),
                                           'local', 's', out.append, memory=memory_service,
                                           read_line=answers(*typed))
    return out


async def test_deleting_a_persona_deletes_its_memories_after_confirming(tmp_path):
    store = PersonaStore(tmp_path / 'personas')
    persona = store.create('local', '学 agent')
    service = fake_memory(count=3)
    out = await run_delete(store, service, 'n')
    assert out[-1] == '已取消，人设和记忆都没有删除。'
    assert store.find('local', '学 agent') == persona and service.forget_persona.await_count == 0
    out = await run_delete(store, service, 'y')
    assert out == ['人设「学 agent」有 3 条记忆，会一起删除，删了找不回来。',
                   '已删除人设「学 agent」和它的 3 条记忆，用它的会话回到“默认”。']
    service.forget_persona.assert_awaited_once_with('local', persona.persona_id)
    assert [p['persona_id'] for p in store.list_personas('local')] == [DEFAULT_ID]


async def test_a_persona_without_memories_is_deleted_without_asking(tmp_path):
    store = PersonaStore(tmp_path / 'personas')
    store.create('local', '学 agent')
    service = fake_memory(count=0)
    assert await run_delete(store, service) == ['已删除人设「学 agent」，用它的会话回到“默认”。']
    assert service.forget_persona.await_count == 0


async def test_a_failed_memory_delete_keeps_the_persona(tmp_path):
    store = PersonaStore(tmp_path / 'personas')
    persona = store.create('local', '学 agent')
    service = fake_memory(count=2)
    service.forget_persona.side_effect = MemoryIndexSyncError('local', 'a', 'delete', database_changed=True)
    out = await run_delete(store, service, 'y')
    assert out[-1] == '删除记忆失败：MemoryIndexSyncError，人设没有删除。'
    assert store.find('local', '学 agent') == persona


async def test_the_default_persona_is_never_deleted_even_with_memory(tmp_path):
    store = PersonaStore(tmp_path / 'personas')
    out = []
    await persona_commands.persona_command('/persona delete 默认', SimpleNamespace(personas=store), 'local', 's',
                                           out.append, memory=fake_memory(count=5))
    assert out == ['“默认”人设不能删除。']


# ---- the web API ----------------------------------------------------------------------------------------

class api:
    def __init__(self, service, memory):
        self.service = service
        self.app = create_app(service, user_id='local', access_token=TOKEN, memory=memory)

    async def __aenter__(self):
        await self.service.__aenter__()
        self._lifespan = self.app.router.lifespan_context(self.app)
        await self._lifespan.__aenter__()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://test',
                                        headers=AUTH)
        return self

    async def __aexit__(self, *exc):
        await self.client.aclose()
        await self._lifespan.__aexit__(*exc)
        await self.service.__aexit__(*exc)


def web_service(tmp_path):
    return RagAgentService(model=AsyncMock(), mcp_servers=(), include_knowledge=False,
                           personas_dir=str(tmp_path / 'personas'))


async def test_web_memories_belong_to_the_session_persona(tmp_path):
    service, memories = web_service(tmp_path), fake_memory(count=2)
    persona = service.personas.create('local', '学 agent')
    service.personas.bind('local', 's', persona.persona_id)
    service.personas.bind('local', 'plain', DEFAULT_ID)
    async with api(service, memories) as web:
        own = (await web.client.post('/api/memories', json={'content': 'x', 'session_id': 's'})).json()
        shared = (await web.client.post('/api/memories', json={'content': 'x', 'session_id': 's',
                                                               'shared': True})).json()
        plain = (await web.client.post('/api/memories', json={'content': 'x', 'session_id': 'plain'})).json()
        bare = (await web.client.post('/api/memories', json={'content': 'x'})).json()
        assert own['persona_id'] == persona.persona_id and own['persona_name'] == '学 agent'
        assert shared['persona_id'] is None and plain['persona_id'] is None and bare['persona_id'] is None
        memories.list_memories.return_value = [memory('a'), memory('b', persona.persona_id), memory('c', 'p-gone')]
        listed = (await web.client.get('/api/memories')).json()['memories']
        assert [(m['persona_id'], m['persona_name']) for m in listed] == [
            (None, None), (persona.persona_id, '学 agent'), ('p-gone', None)]
        detail = (await web.client.get(f'/api/personas/{persona.persona_id}')).json()
        assert detail['memories'] == 2
        assert (await web.client.get('/api/personas/default')).json()['memories'] is None


async def test_web_delete_takes_the_memories_and_keeps_the_persona_on_failure(tmp_path):
    service, memories = web_service(tmp_path), fake_memory(count=2)
    first = service.personas.create('local', 'one')
    second = service.personas.create('local', 'two')
    async with api(service, memories) as web:
        response = await web.client.delete(f'/api/personas/{first.persona_id}')
        assert response.json() == {'deleted': first.persona_id, 'memories_deleted': 2}
        memories.forget_persona.side_effect = RuntimeError('SECRET')
        failed = await web.client.delete(f'/api/personas/{second.persona_id}')
        assert failed.status_code == 500 and failed.json()['code'] == 'persona_memories_failed'
        assert 'SECRET' not in failed.text
        assert service.personas.get('local', second.persona_id).name == 'two'
        missing = await web.client.delete('/api/personas/p-00000000')
        assert missing.status_code == 404
        default = await web.client.delete('/api/personas/default')
        assert default.status_code == 400 and default.json()['code'] == 'persona_default_fixed'
    assert memories.forget_persona.await_count == 2


async def test_web_memories_without_personas_keep_the_old_view(tmp_path):
    service = RagAgentService(model=AsyncMock(), mcp_servers=(), include_knowledge=False)
    memories = fake_memory()
    async with api(service, memories) as web:
        saved = (await web.client.post('/api/memories', json={'content': 'x', 'session_id': 's'})).json()
    assert 'persona_id' not in saved and memories.remember_explicit.await_args.kwargs == {}
