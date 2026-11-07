"""Per-message run options: mode, writing and long-term memory chosen for each request (D-051).

Offline only: scripted planner and model, a fake runtime where the runner itself is not under test.
"""

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from app.agent import route_selection
from app.agent.runtime import AgentRunResult
from app.agent.step_models import ModelTurn
from app.agent.team_v2 import READ_TOOLS, DelegationOptions, SingleStepPlanner
from app.api.app import create_app
from app.models.taskstate import TaskStatus
from app.services import rag_agent_service as module
from app.services.rag_agent_service import RagAgentService

SANDBOX = DelegationOptions(allowed_tools=READ_TOOLS | {'write_file', 'run_tests', 'run_python'},
                            require_passing_run=True, acceptance_tests=True, timeout_seconds=300)


def outcome(text, status='succeeded', reason=None):
    return ModelTurn(json.dumps({'status': status, 'content': text, 'reason': reason}))


def route_reply(route, writes=True):
    return ModelTurn(json.dumps({'route': route, 'writes': writes, 'reason': 'r'}))


class FakeRuntime:
    created = []

    def __init__(self, planner, model, memory, registry, **options):
        self.planner, self.memory, self.registry, self.options = planner, memory, registry, options
        FakeRuntime.created.append(self)

    async def run(self, **kwargs):
        return AgentRunResult(self.memory.task_state.task_id, TaskStatus.COMPLETED, 'done', (), 1, 0)


class FakeLongterm:
    def __init__(self):
        self.recalls = 0

    async def recall(self, *, user_id, query, limit):
        self.recalls += 1
        return []


@pytest.fixture
def fake_runtime(monkeypatch):
    FakeRuntime.created = []
    monkeypatch.setattr(module, 'AgentRuntime', FakeRuntime)
    return FakeRuntime.created


@pytest.fixture
def read_only_calls(monkeypatch):
    """Record whether each task got the D-027 read-only tool view."""
    calls, original = [], route_selection.task_registry

    def spy(registry, read_only):
        calls.append(read_only)
        return original(registry, read_only)
    monkeypatch.setattr(route_selection, 'task_registry', spy)
    return calls


def service(tmp_path=None, model=None, **kw):
    return RagAgentService(model=model or AsyncMock(), mcp_servers=(), include_knowledge=False,
                           memory_directory=tmp_path, **kw)


# ---- service ------------------------------------------------------------------------------

def test_the_service_offers_team_modes_only_when_the_team_was_prepared():
    assert service().run_options() == {'modes': ['single'], 'default_mode': 'single', 'memory_available': False}
    assert service(execution_mode='auto').run_options()['modes'] == ['single', 'auto']   # the default stays usable
    prepared = service(team_options=SANDBOX, longterm_memory=FakeLongterm())
    assert prepared.run_options() == {'modes': ['single', 'team', 'auto'], 'default_mode': 'single',
                                      'memory_available': True}


async def test_unavailable_choices_are_refused_before_anything_runs(fake_runtime):
    async with service() as plain:
        with pytest.raises(ValueError, match='execution mode is not available'):
            await plain.query_result('q', 's', execution_mode='team')
        with pytest.raises(ValueError, match='long-term memory is not configured'):
            await plain.query_result('q', 's', use_memory=True)
        with pytest.raises(ValueError, match='allow_writes'):
            await plain.query_result('q', 's', allow_writes='yes')
    assert fake_runtime == []


async def test_a_message_may_pick_the_team_in_a_single_default_service(fake_runtime):
    events = []
    async with service(team_options=SANDBOX) as svc:
        await svc.query_result('implement it', 's', execution_mode='team', on_event=events.append)
        await svc.query_result('what is it?', 's')
    team, single = fake_runtime
    assert isinstance(team.planner, SingleStepPlanner) and not isinstance(single.planner, SingleStepPlanner)
    assert team.options['runner_requirements'](None).capabilities == {'agent_team'}
    assert team.options['step_timeout_seconds'] >= SANDBOX.timeout_seconds + 5   # widened for the team only
    assert single.options['runner_registry'] is None and single.options['runner_requirements'] is None
    assert single.options['step_timeout_seconds'] < SANDBOX.timeout_seconds
    assert events[0] == {'type': 'route', 'mode': 'team', 'read_only': False,
                         'options': {'mode': 'team', 'write': True, 'memory': False}}


async def test_a_message_may_pick_single_in_a_team_default_service(fake_runtime):
    async with service(execution_mode='team', team_options=SANDBOX) as svc:
        await svc.query_result('what is it?', 's', execution_mode='single')
    [single] = fake_runtime
    assert not isinstance(single.planner, SingleStepPlanner)
    assert single.options['runner_registry'] is None and single.options['runner_requirements'] is None


async def test_writes_off_reuses_the_read_only_path(fake_runtime, read_only_calls):
    events = []
    async with service() as svc:
        await svc.query_result('look only', 's', allow_writes=False, on_event=events.append)
        await svc.query_result('may write', 's')
    assert read_only_calls == [True, False]
    assert events[0]['read_only'] is True and events[0]['options']['write'] is False


async def test_auto_with_writes_off_never_asks_the_model_and_runs_single(fake_runtime, read_only_calls):
    model = AsyncMock()
    async with service(model=model, execution_mode='auto', team_options=SANDBOX) as svc:
        await svc.query_result('implement it', 's', allow_writes=False)
    model.generate.assert_not_called()                     # the host rule 'read_only' leaves only single
    assert not isinstance(fake_runtime[0].planner, SingleStepPlanner) and read_only_calls == [True]


async def test_auto_with_writes_on_still_asks_the_model(fake_runtime):
    model = AsyncMock()
    model.generate.return_value = route_reply('team')
    async with service(model=model, execution_mode='auto', team_options=SANDBOX) as svc:
        await svc.query_result('implement it', 's')
    assert isinstance(fake_runtime[0].planner, SingleStepPlanner)


async def test_a_team_with_writes_off_gets_read_only_workers(fake_runtime, monkeypatch):
    seen = []
    from app.agent import team_v2
    original = team_v2.register_delegating_team_runner

    def spy(registry, *, options):
        seen.append(options)
        return original(registry, options=options)
    monkeypatch.setattr(team_v2, 'register_delegating_team_runner', spy)
    async with service(team_options=SANDBOX) as svc:
        await svc.query_result('review it', 's', execution_mode='team', allow_writes=False)
    [options] = seen
    assert options.allowed_tools == READ_TOOLS
    assert not options.require_passing_run and not options.acceptance_tests
    assert options.timeout_seconds == SANDBOX.timeout_seconds


async def test_memory_off_skips_recall(fake_runtime):
    memory = FakeLongterm()
    async with service(longterm_memory=memory) as svc:
        await svc.query_result('q', 's', user_id='u', use_memory=False)
        assert memory.recalls == 0
        await svc.query_result('q', 's', user_id='u')
        assert memory.recalls == 1


# ---- web API ------------------------------------------------------------------------------

AUTH = {'Authorization': 'Bearer t'}


class api:
    def __init__(self, svc, **options):
        self.service = svc
        self.app = create_app(svc, user_id='local', access_token='t', **options)

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

    async def send(self, content, request_id, session='s', **fields):
        return await self.client.post(f'/api/sessions/{session}/messages',
                                      json={'content': content, 'request_id': request_id, **fields})

    async def done(self, run_id):
        run = self.app.state.runs.get(run_id)
        await asyncio.wait_for(asyncio.shield(run.task), 5)
        return (await self.client.get(f'/api/runs/{run_id}')).json()

    async def events(self, run_id):
        response = await self.client.get(f'/api/runs/{run_id}/events')
        return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]


def scripted(tmp_path, steps=(), turns=(), **kw):
    planner, model = AsyncMock(), AsyncMock()
    planner.generate.side_effect = [json.dumps({'steps': s, 'reason': None}) for s in steps]
    model.generate.side_effect = list(turns)
    return RagAgentService(model=model, planner_model=planner, mcp_servers=(), include_knowledge=False,
                           memory_directory=tmp_path, **kw)


async def test_health_reports_what_each_message_may_choose(tmp_path):
    async with api(service(tmp_path)) as web:
        health = (await web.client.get('/api/health')).json()
    assert health['write_enabled'] is False and health['longterm_memory'] is False   # kept unchanged
    assert health['options'] == {'modes': ['single'], 'default_mode': 'single', 'write_available': False,
                                 'write_default': False, 'memory_available': False, 'memory_default': False}
    svc = service(tmp_path, execution_mode='auto', team_options=SANDBOX, longterm_memory=FakeLongterm())
    async with api(svc, write_enabled=True) as web:
        health = (await web.client.get('/api/health')).json()
    assert health['options'] == {'modes': ['single', 'team', 'auto'], 'default_mode': 'auto',
                                 'write_available': True, 'write_default': True,
                                 'memory_available': True, 'memory_default': True}


@pytest.mark.parametrize('options, code', [
    ({'write': True}, 'write_unavailable'),
    ({'memory': True}, 'memory_unavailable'),
    ({'mode': 'team'}, 'mode_unavailable'),
    ({'mode': 'auto'}, 'mode_unavailable'),
])
async def test_unavailable_choices_are_refused_with_a_code(tmp_path, fake_runtime, options, code):
    async with api(service(tmp_path)) as web:
        response = await web.send('q', 'r1', options=options)
        assert response.status_code == 400 and response.json()['code'] == code
        assert all('一' <= c <= '鿿' for c in response.json()['message'][:2])   # Chinese message
        assert (await web.client.get('/api/runs/r1')).status_code == 404
    assert fake_runtime == []


@pytest.mark.parametrize('options', [{'mode': 'parallel'}, {'write': 'yes'}, {'memory': 1}, {'sandbox': True},
                                     'team'])
async def test_malformed_options_are_invalid_input(tmp_path, options):
    async with api(service(tmp_path)) as web:
        response = await web.send('q', 'r1', options=options)
    assert response.status_code == 400 and response.json()['code'] == 'invalid_input'


async def test_route_event_and_run_state_carry_the_options_in_effect(tmp_path, fake_runtime):
    svc = service(tmp_path, team_options=SANDBOX, longterm_memory=FakeLongterm())
    async with api(svc, write_enabled=True) as web:
        assert (await web.send('q', 'r1', options={'mode': 'team', 'memory': False})).status_code == 202
        state = await web.done('r1')
        route = next(e for e in await web.events('r1') if e['type'] == 'route')
        defaults = await web.done((await web.send('q', 'r2')).json()['run_id'])
    expected = {'mode': 'team', 'write': True, 'memory': False}
    assert state['options'] == expected and route['options'] == expected and route['mode'] == 'team'
    assert defaults['options'] == {'mode': 'single', 'write': True, 'memory': True}


async def test_omitted_options_follow_the_startup_defaults(tmp_path, fake_runtime, read_only_calls):
    async with api(service(tmp_path)) as web:
        state = await web.done((await web.send('q', 'r1', options={})).json()['run_id'])
    # Without a write root the default is no writing: the read-only path, as before (D-027).
    assert state['options'] == {'mode': 'single', 'write': False, 'memory': False} and read_only_calls == [True]


async def test_a_repeated_request_id_with_other_options_conflicts(tmp_path, fake_runtime):
    async with api(service(tmp_path, team_options=SANDBOX), write_enabled=True) as web:
        assert (await web.send('q', 'r1', options={'write': False})).status_code == 202
        await web.done('r1')
        same = await web.send('q', 'r1', options={'write': False})
        assert same.status_code == 200 and same.json()['created'] is False
        for other in ({'write': True}, {'write': False, 'mode': 'team'}, None):
            fields = {} if other is None else {'options': other}
            response = await web.send('q', 'r1', **fields)
            assert response.status_code == 409 and response.json()['code'] == 'request_conflict'
    assert len(fake_runtime) == 1


async def test_a_resume_ignores_new_options_and_keeps_the_tasks_own(tmp_path, read_only_calls):
    turns = [outcome('need it', 'blocked', '要翻译哪一句？'), outcome('Hello, world.')]
    async with api(scripted(tmp_path, [['Ask'], ['Translate']], turns), write_enabled=True) as web:
        await web.send('翻译一句话', 'r1', options={'write': False})
        blocked = await web.done('r1')
        assert blocked['status'] == 'blocked'
        task_id = blocked['result']['task_id']
        # memory=true would be refused for a new task here; a resume ignores it entirely.
        response = await web.send('你好，世界。', 'r2', resume_task_id=task_id,
                                  options={'write': True, 'memory': True, 'mode': 'team'})
        assert response.status_code == 202
        assert (await web.client.get('/api/runs/r2')).json()['options'] == blocked['options']
        done = await web.done('r2')
        route = next(e for e in await web.events('r2') if e['type'] == 'route')
    original = {'mode': 'single', 'write': False, 'memory': False}
    assert blocked['options'] == original and done['options'] == original and route['options'] == original
    assert done['status'] == 'completed' and read_only_calls == [True, True]


# ---- launcher -----------------------------------------------------------------------------

@pytest.mark.parametrize('argv, write_root, sandbox, writes', [
    (['--execution-mode', 'single'], None, False, False),
    (['--execution-mode', 'auto', '--write-root', 'w'], 'w', False, True),
])
async def test_the_web_launcher_always_prepares_the_team_but_keeps_the_default_mode(
        monkeypatch, argv, write_root, sandbox, writes):
    import uvicorn
    from app.api import __main__ as launcher
    from app.api import app as app_module
    from app.config import config
    seen = {}

    class FakeService:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

    class FakeServer:
        def __init__(self, cfg):
            pass

        async def serve(self):
            return None

    monkeypatch.setattr(launcher, 'RagAgentService', FakeService)
    monkeypatch.setattr(launcher, 'local_mcp_servers', lambda **kw: ())
    monkeypatch.setattr(app_module, 'create_app', lambda service, **kw: seen.setdefault('app', kw))
    monkeypatch.setattr(uvicorn, 'Server', FakeServer)
    monkeypatch.setattr(uvicorn, 'Config', lambda *a, **kw: None)
    for name, value in (('web_access_token', 't'), ('longterm_memory_dsn', ''),
                        ('mcp_filesystem_write_root', ''), ('mcp_filesystem_root', '')):
        monkeypatch.setattr(config, name, value)
    assert await launcher.serve(launcher.build_parser().parse_args(argv)) == 0
    assert seen['execution_mode'] == argv[1]
    team = seen['team_options']
    assert isinstance(team, DelegationOptions)
    assert ('write_file' in team.allowed_tools) is writes and team.require_passing_run is sandbox
    assert seen['step_timeout_seconds'] is None            # the service widens team tasks only
    assert seen['app']['write_enabled'] is writes
    # The routing rules are unchanged: auto without the sandbox never offers the team (D-026).
    assert route_selection.allowed_modes(team)[0] == frozenset({'single'})
