"""Web backend: runs, progress events, resume, cancel, limits, errors and memory (D-046)."""

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from app.agent.step_models import ModelTurn
from app.api.__main__ import is_loopback, startup_problem
from app.api.app import create_app
from app.api.runs import ApiError
from app.models.longterm_memory import MemoryCategory, MemoryRecord
from app.services.rag_agent_service import RagAgentService

TOKEN = 'test-token'
AUTH = {'Authorization': f'Bearer {TOKEN}'}


def outcome(text, status='succeeded', reason=None):
    return ModelTurn(json.dumps({'status': status, 'content': text, 'reason': reason}))


def service_for(tmp_path, steps=(), turns=()):
    planner, model = AsyncMock(), AsyncMock()
    planner.generate.side_effect = [json.dumps({'steps': s, 'reason': None}) for s in steps]
    model.generate.side_effect = list(turns)
    return RagAgentService(model=model, planner_model=planner, mcp_servers=(), include_knowledge=False,
                           memory_directory=tmp_path)


class Gate:
    """A model turn that waits until released, so a run stays in progress."""

    def __init__(self, result):
        self.entered, self.release, self.result = asyncio.Event(), asyncio.Event(), result

    async def call(self, *args, **kwargs):
        self.entered.set()
        await self.release.wait()
        return self.result


class api:
    def __init__(self, service, **options):
        self.service = service
        self.app = create_app(service, user_id='local', access_token=options.pop('token', TOKEN), **options)

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

    async def send(self, session, content, request_id, **fields):
        return await self.client.post(f'/api/sessions/{session}/messages',
                                      json={'content': content, 'request_id': request_id, **fields})

    async def events(self, run_id, after=None):
        headers = {'Last-Event-ID': str(after)} if after is not None else {}
        response = await self.client.get(f'/api/runs/{run_id}/events', headers=headers)
        assert response.headers['content-type'].startswith('text/event-stream')
        return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]

    async def finished(self, run_id):
        run = self.app.state.runs.get(run_id)
        await asyncio.wait_for(asyncio.shield(run.task), 5)
        return run


async def test_a_message_runs_in_the_background_and_reports_every_stage(tmp_path):
    async with api(service_for(tmp_path, [['Answer']], [outcome('Paris')])) as web:
        response = await web.send('s1', 'Capital of France?', 'r1')
        assert response.status_code == 202
        assert response.json() == {'run_id': 'r1', 'events': '/api/runs/r1/events', 'created': True}
        events = await web.events('r1')
        assert [e['type'] for e in events] == ['accepted', 'route', 'plan', 'step', 'completed']
        assert [e['id'] for e in events] == [1, 2, 3, 4, 5]
        assert events[-1]['content'] == 'Paris' and events[-1]['usage'] is not None
        state = (await web.client.get('/api/runs/r1')).json()
        assert state['status'] == 'completed' and state['result'] == events[-1]
        sessions = (await web.client.get('/api/sessions')).json()['sessions']
        assert [s['session_id'] for s in sessions] == ['s1']
        turns = (await web.client.get('/api/sessions/s1/turns')).json()['turns']
        assert [(t['user'], t['reply'], t['status']) for t in turns] == [('Capital of France?', 'Paris', 'completed')]


async def test_reconnecting_with_last_event_id_sends_only_later_events(tmp_path):
    async with api(service_for(tmp_path, [['Answer']], [outcome('ok')])) as web:
        await web.send('s', 'q', 'r')
        await web.finished('r')
        assert [e['id'] for e in await web.events('r', after=3)] == [4, 5]
        assert await web.events('r', after=5) == []


async def test_a_repeated_request_id_returns_the_same_run_and_a_changed_one_conflicts(tmp_path):
    async with api(service_for(tmp_path, [['Answer']], [outcome('ok')])) as web:
        await web.send('s', 'q', 'r')
        again = await web.send('s', 'q', 'r')
        assert again.status_code == 200 and again.json()['created'] is False
        conflict = await web.send('s', 'other text', 'r')
        assert conflict.status_code == 409 and conflict.json()['code'] == 'request_conflict'
        await web.finished('r')
        assert len(web.service._planner_model.generate.call_args_list) == 1


async def test_a_blocked_question_is_answered_by_the_next_message(tmp_path):
    turns = [outcome('need it', 'blocked', '要翻译哪一句？'), outcome('Hello, world.')]
    async with api(service_for(tmp_path, [['Ask'], ['Translate']], turns)) as web:
        await web.send('s', '翻译一句话', 'r1')
        blocked = (await web.events('r1'))[-1]
        assert blocked['type'] == 'blocked' and blocked['question'] == '要翻译哪一句？'
        [turn] = (await web.client.get('/api/sessions/s/turns')).json()['turns']
        assert turn['resumable'] is True and turn['task_id'] == blocked['task_id']
        response = await web.send('s', '你好，世界。', 'r2', resume_task_id=blocked['task_id'])
        assert response.status_code == 202
        done = (await web.events('r2'))[-1]
        assert done['type'] == 'completed' and done['content'] == 'Hello, world.'
        stale = await web.send('s', 'again', 'r3', resume_task_id=blocked['task_id'])
        assert stale.status_code == 409 and stale.json()['code'] == 'task_not_resumable'


async def test_one_run_per_session_and_a_global_limit(tmp_path):
    gate = Gate(outcome('done'))
    service = service_for(tmp_path, [['Work']])
    service.model.generate.side_effect = gate.call
    async with api(service, max_concurrent_runs=1) as web:
        assert (await web.send('a', 'first', 'r1')).status_code == 202
        await gate.entered.wait()
        busy = await web.send('a', 'second', 'r2')
        assert busy.status_code == 409 and busy.json()['code'] == 'session_busy'
        full = await web.send('b', 'third', 'r3')
        assert full.status_code == 429 and full.json()['code'] == 'too_many_runs'
        assert (await web.client.get('/api/health')).json()['active_runs'] == 1
        gate.release.set()
        await web.finished('r1')
        assert (await web.client.get('/api/health')).json()['active_runs'] == 0


async def test_cancel_stops_a_running_task_and_frees_the_session(tmp_path):
    gate = Gate(outcome('never'))
    service = service_for(tmp_path, [['Work'], ['Again']])
    service.model.generate.side_effect = gate.call
    async with api(service) as web:
        await web.send('s', 'long job', 'r1')
        await gate.entered.wait()
        state = (await web.client.post('/api/runs/r1/cancel')).json()
        assert state['status'] == 'cancelled' and state['result']['type'] == 'cancelled'
        assert '副作用' in state['result']['message']
        assert (await web.client.post('/api/runs/r1/cancel')).json()['status'] == 'cancelled'
        [turn] = (await web.client.get('/api/sessions/s/turns')).json()['turns']
        assert turn['status'] == 'cancelled'
        service.model.generate.side_effect = [outcome('fine')]
        assert (await web.send('s', 'next', 'r2')).status_code == 202
        assert (await web.events('r2'))[-1]['content'] == 'fine'


async def test_failures_carry_a_code_and_a_chinese_message_but_no_exception_text(tmp_path):
    service = service_for(tmp_path, [['Work']])
    service.model.generate.side_effect = RuntimeError('provider said: secret-key-123')
    async with api(service) as web:
        await web.send('s', 'q', 'r')
        events = await web.events('r')
        assert events[-1]['type'] == 'failed' and events[-1]['message']
        assert 'secret-key-123' not in json.dumps(events, ensure_ascii=False)


@pytest.mark.parametrize('body', [
    {'content': '   ', 'request_id': 'r'},
    {'content': 'x' * 20001, 'request_id': 'r'},
    {'content': 'q', 'request_id': 'bad id!'},
    {'content': 'q', 'request_id': 'r', 'question_id': 'q1'},
    {'content': 'q', 'request_id': 'r', 'user_id': 'someone-else'},
    {'content': 'q'},
])
async def test_invalid_messages_are_refused_before_any_work(tmp_path, body):
    service = service_for(tmp_path)
    async with api(service) as web:
        response = await web.client.post('/api/sessions/s/messages', json=body)
        assert response.status_code == 400 and response.json()['code'] == 'invalid_input'
    assert service.model.generate.call_count == 0 and list(tmp_path.iterdir()) == []


async def test_the_token_is_required_everywhere(tmp_path):
    async with api(service_for(tmp_path)) as web:
        for headers in ({'Authorization': ''}, {'Authorization': 'Bearer wrong'}, {'Authorization': TOKEN}):
            response = await web.client.get('/api/health', headers=headers)
            assert response.status_code == 401 and response.json()['code'] == 'unauthorized'
        response = await web.client.post('/api/sessions/s/messages', json={'content': 'q', 'request_id': 'r'},
                                         headers={'Authorization': 'Bearer wrong'})
        assert response.status_code == 401
        health = (await web.client.get('/api/health')).json()
        assert health == {'status': 'ok', 'longterm_memory': False, 'write_enabled': False, 'active_runs': 0}


async def test_unknown_runs_and_paths_have_codes(tmp_path):
    async with api(service_for(tmp_path)) as web:
        response = await web.client.get('/api/runs/nope')
        assert response.status_code == 404 and response.json()['code'] == 'run_not_found'
        response = await web.client.get('/api/nothing-here')
        assert response.status_code == 404 and response.json()['code'] == 'not_found'
        response = await web.client.get('/api/sessions?limit=0')
        assert response.status_code == 400 and response.json()['code'] == 'invalid_input'


async def test_finished_runs_expire_after_the_retention_period(tmp_path):
    now = [100.0]
    async with api(service_for(tmp_path, [['Answer']], [outcome('ok')]), event_retention_seconds=60,
                   clock=lambda: now[0]) as web:
        await web.send('s', 'q', 'r')
        await web.finished('r')
        now[0] += 59
        assert (await web.client.get('/api/runs/r')).status_code == 200
        now[0] += 2
        assert (await web.client.get('/api/runs/r')).json()['code'] == 'run_not_found'


async def test_shutdown_cancels_runs_that_outlive_the_timeout(tmp_path):
    gate = Gate(outcome('never'))
    service = service_for(tmp_path, [['Work']])
    service.model.generate.side_effect = gate.call
    async with api(service, shutdown_timeout_seconds=0.05) as web:
        await web.send('s', 'long job', 'r')
        await gate.entered.wait()
        run = web.app.state.runs.get('r')
    assert run.status == 'cancelled'
    with pytest.raises(ApiError) as refused:
        web.app.state.runs.start('s', 'late', 'r2')
    assert refused.value.code == 'shutting_down'
    [turn] = await service.session_turns('s', user_id='local')
    assert turn['status'] == 'cancelled'


class FakeMemory:
    def __init__(self):
        self.records = {}

    async def list_memories(self, user_id, limit=20, offset=0):
        return [r for r in self.records.values() if r.user_id == user_id][offset:offset + limit]

    async def remember_explicit(self, user_id, content):
        record = MemoryRecord(user_id=user_id, content=content.strip(), category=MemoryCategory.FACT,
                              source='user_explicit')
        self.records[record.memory_id] = record
        return record

    async def forget(self, user_id, memory_id):
        record = self.records.get(memory_id)
        if record is None or record.user_id != user_id:
            return False
        del self.records[memory_id]
        return True


async def test_memory_endpoints_use_the_server_user(tmp_path):
    memory = FakeMemory()
    async with api(service_for(tmp_path), memory=memory) as web:
        saved = (await web.client.post('/api/memories', json={'content': ' 喜欢简短的回答 '})).json()
        assert saved['content'] == '喜欢简短的回答' and saved['category'] == 'fact'
        assert memory.records[saved['memory_id']].user_id == 'local'
        listed = (await web.client.get('/api/memories')).json()['memories']
        assert [m['memory_id'] for m in listed] == [saved['memory_id']]
        assert (await web.client.delete(f"/api/memories/{saved['memory_id']}")).status_code == 200
        missing = await web.client.delete(f"/api/memories/{saved['memory_id']}")
        assert missing.status_code == 404 and missing.json()['code'] == 'memory_not_found'
        empty = await web.client.post('/api/memories', json={'content': '  '})
        assert empty.status_code == 400


async def test_memory_endpoints_say_when_memory_is_off(tmp_path):
    async with api(service_for(tmp_path)) as web:
        for response in (await web.client.get('/api/memories'),
                         await web.client.post('/api/memories', json={'content': 'x'}),
                         await web.client.delete('/api/memories/abc')):
            assert response.status_code == 404 and response.json()['code'] == 'memory_disabled'


def test_startup_requires_a_token_unless_waived_on_loopback():
    assert startup_problem('0.0.0.0', 'secret', False) is None
    assert 'WEB_ACCESS_TOKEN' in startup_problem('127.0.0.1', '', False)
    assert startup_problem('127.0.0.1', '', True) is None
    assert startup_problem('::1', '', True) is None
    assert 'loopback' in startup_problem('0.0.0.0', '', True)
    assert 'secret' not in (startup_problem('0.0.0.0', '', True) or '')
    assert is_loopback('localhost') and not is_loopback('example.com')


async def test_without_a_token_the_api_is_open_only_when_built_so(tmp_path):
    async with api(service_for(tmp_path), token='') as web:
        response = await web.client.get('/api/health', headers={'Authorization': ''})
        assert response.status_code == 200


async def test_the_page_and_its_files_are_served_with_strict_headers(tmp_path):
    async with api(service_for(tmp_path)) as web:
        page = await web.client.get('/', headers={'Authorization': ''})
        assert page.status_code == 200 and page.headers['content-type'].startswith('text/html')
        assert '/static/app.js' in page.text and '/static/app.css' in page.text
        policy = page.headers['content-security-policy']
        assert "script-src 'self'" in policy and "connect-src 'self'" in policy and "frame-ancestors 'none'" in policy
        assert page.headers['x-content-type-options'] == 'nosniff'
        for name, kind in (('app.js', 'text/javascript'), ('app.css', 'text/css')):
            response = await web.client.get(f'/static/{name}', headers={'Authorization': ''})
            assert response.status_code == 200 and response.headers['content-type'].startswith(kind)
        for name in ('index.html', '..%2Fapp.py', 'other.js'):
            response = await web.client.get(f'/static/{name}')
            assert response.status_code == 404 and response.json()['code'] == 'not_found'
        health = await web.client.get('/api/health')
        assert health.headers['cache-control'] == 'no-store' and 'content-security-policy' in health.headers


def test_the_page_writes_text_only_and_loads_nothing_from_elsewhere():
    from app.api.app import STATIC
    script = (STATIC / 'app.js').read_text(encoding='utf-8')
    assert 'innerHTML' not in script and 'outerHTML' not in script and 'eval(' not in script
    assert 'localStorage' not in script            # the token lives only in this tab (D-047)
    for name in ('index.html', 'app.js', 'app.css'):
        text = (STATIC / name).read_text(encoding='utf-8')
        assert 'http://' not in text and 'https://' not in text, name
