"""Personas (docs/persona_plan.md): the store, the service, the command line and the web API.

Offline only: temporary directories and a scripted model.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from app.agent import persona_commands
from app.agent.__main__ import build_parser
from app.agent.step_models import ModelTurn, ToolCall
from app.api.app import create_app
from app.services import personas as module
from app.services.personas import (
    DEFAULT_ID, PERSONA_PREAMBLE, Persona, PersonaError, PersonaStore, parse, render,
)
from app.services.rag_agent_service import RagAgentService
from app.services.workspace import WorkspaceCheck

TOKEN = 'test-token'
AUTH = {'Authorization': f'Bearer {TOKEN}'}
LEAD = 'Instructions the user wrote for the persona'


class Policy:
    """Stands in for WorkspacePolicy.check: one directory on the system drive, others fine."""

    def __init__(self, system=()):
        self.system = {str(Path(p)) for p in system}

    def check(self, raw):
        path = str(Path(raw))
        if 'missing' in path:
            return WorkspaceCheck(None, False, 'not_found', '目录不存在。')
        return WorkspaceCheck(path, path in self.system)


def store_at(tmp_path, policy=None):
    return PersonaStore(tmp_path / 'personas', workspace_policy=policy)


def files(directory: Path):
    return sorted(p for p in directory.rglob('*') if p.is_file())


# ---- the store ------------------------------------------------------------------------------

def test_the_default_persona_exists_before_any_file(tmp_path):
    store = store_at(tmp_path)
    assert store.get(None, DEFAULT_ID) == Persona(DEFAULT_ID, '默认')
    assert store.list_personas(None) == [{'persona_id': 'default', 'name': '默认', 'chars': 0, 'options': {}}]
    assert store.session_persona_id(None, 's') == DEFAULT_ID
    assert not (tmp_path / 'personas').exists()


def test_create_find_rename_and_delete(tmp_path):
    store = store_at(tmp_path)
    persona = store.create('alice', '学 Agent', 'Explain trade-offs first.', {'mode': 'single'})
    assert persona.persona_id.startswith('p-') and persona.options == {'mode': 'single'}
    assert store.find('alice', '  学 agent ') == persona          # case and spaces ignored
    assert store.find('alice', persona.persona_id) == persona
    renamed = store.update('alice', persona.persona_id, name='学后端')
    assert renamed.name == '学后端' and renamed.instructions == 'Explain trade-offs first.'
    with pytest.raises(PersonaError) as error:
        store.find('alice', '学 Agent')
    assert error.value.code == 'not_found'
    store.delete('alice', persona.persona_id)
    assert [p['persona_id'] for p in store.list_personas('alice')] == [DEFAULT_ID]


def test_users_are_separate_and_paths_carry_no_names(tmp_path):
    store = store_at(tmp_path)
    persona = store.create('alice@example.com', '学 agent')
    store.btw('alice@example.com', persona.persona_id, 'hello')
    assert store.list_personas('bob') == store.list_personas(None)[:1]
    for path in files(tmp_path / 'personas'):
        relative = str(path.relative_to(tmp_path))
        assert 'alice' not in relative and '学' not in relative
    (user_dir,) = (tmp_path / 'personas').iterdir()
    assert len(user_dir.name) == 16 and all(c in '0123456789abcdef' for c in user_dir.name)


@pytest.mark.parametrize('name', ['', ' ', 'x' * 41, 'new', 'Show', '/btw', '--all', 'a\nb', '默认'])
def test_bad_names_are_refused(tmp_path, name):
    with pytest.raises(PersonaError):
        store_at(tmp_path).create(None, name)


def test_duplicates_and_the_persona_limit(tmp_path, monkeypatch):
    store = store_at(tmp_path)
    store.create(None, 'Backend')
    with pytest.raises(PersonaError) as error:
        store.create(None, 'backend')
    assert error.value.code == 'duplicate'
    monkeypatch.setattr(module, 'MAX_PERSONAS', 3)
    store.create(None, 'Two')
    with pytest.raises(PersonaError) as error:
        store.create(None, 'Three')
    assert error.value.code == 'too_many'


def test_the_default_persona_cannot_be_renamed_or_deleted(tmp_path):
    store = store_at(tmp_path)
    for call in (lambda: store.update(None, DEFAULT_ID, name='x'), lambda: store.delete(None, DEFAULT_ID)):
        with pytest.raises(PersonaError) as error:
            call()
        assert error.value.code == 'default_fixed'
    updated = store.update(None, DEFAULT_ID, instructions='Be brief.', options={'write': False})
    assert store.get(None, DEFAULT_ID) == updated and updated.name == '默认'


def test_options_are_checked_and_none_removes_one(tmp_path):
    store = store_at(tmp_path)
    persona = store.create(None, 'p', options={'mode': 'team', 'use_memory': True})
    for options in ({'mode': 'plan'}, {'write': 'yes'}, {'colour': 'red'}, {'colour': None},
                    {'workspace': 'relative/dir'}):
        with pytest.raises(PersonaError):
            store.update(None, persona.persona_id, options=options)
    assert store.update(None, persona.persona_id, options={'mode': None}).options == {'use_memory': True}


def test_instructions_over_the_limit_are_refused(tmp_path):
    store = store_at(tmp_path)
    with pytest.raises(PersonaError) as error:
        store.update(None, DEFAULT_ID, instructions='x' * 4001)
    assert error.value.code == 'too_long'
    assert store.update(None, DEFAULT_ID, instructions='x' * 4000).chars == 4000


def test_the_file_is_toml_then_the_instructions(tmp_path):
    store = store_at(tmp_path)
    persona = store.create(None, 'Say "hi"', 'Line one.\nLine two.', {'mode': 'single', 'write': False})
    text = store.file_path(None, persona.persona_id).read_text(encoding='utf-8')
    assert text == ('+++\nname = "Say \\"hi\\""\nmode = "single"\nwrite = false\n+++\n'
                    'Line one.\nLine two.\n')
    assert parse(persona.persona_id, render(persona)) == persona


def test_a_hand_edited_file_is_read_at_once(tmp_path):
    store = store_at(tmp_path)
    persona = store.create(None, 'p')
    path = store.file_path(None, persona.persona_id)
    path.write_bytes('﻿+++\r\nname = "p"\r\nuse_memory = false\r\n+++\r\nNew rules.\r\n'.encode('utf-8'))
    assert store.get(None, persona.persona_id) == Persona(persona.persona_id, 'p', 'New rules.',
                                                          {'use_memory': False})


@pytest.mark.parametrize('text, code', [
    ('name = "p"\n', 'bad_file'),                       # no header fence
    ('+++\nname = "p"\n', 'bad_file'),                  # no closing fence
    ('+++\nname = p\n+++\n', 'bad_file'),               # not TOML
    ('+++\nname = "p"\ncolour = "red"\n+++\n', 'bad_file'),
    ('+++\nname = "p"\nmode = "plan"\n+++\n', 'invalid_option'),
    ('+++\nname = "p"\n+++\n' + 'x' * 4001, 'too_long'),
])
def test_a_broken_file_names_its_problem_and_is_listed(tmp_path, text, code):
    store = store_at(tmp_path)
    persona = store.create(None, 'p')
    store.file_path(None, persona.persona_id).write_text(text, encoding='utf-8')
    with pytest.raises(PersonaError) as error:
        store.get(None, persona.persona_id)
    assert error.value.code == code and 'colour = "red"' not in error.value.message
    listed = store.list_personas(None)[1]
    assert listed['problem'] == code and listed['name'] is None


def test_btw_appends_and_undo_removes_only_its_own_line(tmp_path, monkeypatch):
    store = store_at(tmp_path)
    assert store.btw(None, DEFAULT_ID, '  use   FastAPI ').instructions == 'use FastAPI'
    assert store.btw(None, DEFAULT_ID, 'be brief').instructions == 'use FastAPI\nbe brief'
    assert store.undo_btw(None, DEFAULT_ID).instructions == 'use FastAPI'
    with pytest.raises(PersonaError) as error:
        store.undo_btw(None, DEFAULT_ID)                # one level only
    assert error.value.code == 'nothing_to_undo'
    store.btw(None, DEFAULT_ID, 'third')
    store.update(None, DEFAULT_ID, instructions='rewritten')
    with pytest.raises(PersonaError):
        store.undo_btw(None, DEFAULT_ID)                # an edit forgets the last /btw
    store.btw(None, DEFAULT_ID, 'fourth')
    path = store.file_path(None, DEFAULT_ID)
    path.write_text(path.read_text(encoding='utf-8') + 'by hand\n', encoding='utf-8')
    with pytest.raises(PersonaError) as error:
        store.undo_btw(None, DEFAULT_ID)
    assert error.value.code == 'changed'
    monkeypatch.setattr(module, 'TEXT_LIMIT', 30)
    with pytest.raises(PersonaError) as error:
        store.btw(None, DEFAULT_ID, 'x' * 20)
    assert error.value.code == 'too_long'


def test_sessions_are_bound_and_new_ones_take_the_last_used(tmp_path, monkeypatch):
    store = store_at(tmp_path)
    persona = store.create(None, 'p')
    assert not store.is_bound(None, 's1')
    store.bind(None, 's1', persona.persona_id)
    assert store.session_persona_id(None, 's1') == persona.persona_id
    assert store.session_persona_id(None, 'brand-new') == persona.persona_id   # the last one used
    store.bind(None, 's2', DEFAULT_ID)
    assert store.session_persona_id(None, 's1') == persona.persona_id
    restarted = store_at(tmp_path)
    assert restarted.session_persona_id(None, 's1') == persona.persona_id
    store.delete(None, persona.persona_id)
    assert store.session_persona_id(None, 's1') == DEFAULT_ID and not store.is_bound(None, 's1')
    monkeypatch.setattr(module, 'MAX_SESSIONS', 2)
    for name in ('a', 'b', 'c'):
        store.bind(None, name, DEFAULT_ID)
    assert not store.is_bound(None, 'a') and store.is_bound(None, 'c')


def test_a_persona_deleted_by_hand_falls_back_to_the_default(tmp_path):
    store = store_at(tmp_path)
    persona = store.create(None, 'p')
    store.bind(None, 's', persona.persona_id)
    store.file_path(None, persona.persona_id).unlink()
    assert store.session_persona_id(None, 's') == DEFAULT_ID


def test_a_workspace_needs_the_policy_and_never_the_system_drive(tmp_path):
    with pytest.raises(PersonaError) as error:
        store_at(tmp_path).update(None, DEFAULT_ID, options={'workspace': str(tmp_path)})
    assert error.value.code == 'workspace_unavailable'
    store = store_at(tmp_path, Policy(system=[tmp_path / 'c']))
    assert store.update(None, DEFAULT_ID, options={'workspace': str(tmp_path / 'd')}).options == {
        'workspace': str(tmp_path / 'd')}
    for path, code in ((tmp_path / 'c', 'workspace_system_drive'), (tmp_path / 'missing', 'workspace_invalid')):
        with pytest.raises(PersonaError) as error:
            store.update(None, DEFAULT_ID, options={'workspace': str(path)})
        assert error.value.code == code


def test_writes_leave_no_temporary_files(tmp_path):
    store = store_at(tmp_path)
    persona = store.create(None, 'p')
    store.btw(None, persona.persona_id, 'x')
    store.bind(None, 's', persona.persona_id)
    assert {p.name for p in files(tmp_path / 'personas')} == {f'{persona.persona_id}.md', 'state.json'}


# ---- the service ------------------------------------------------------------------------------

def requests_text(model):
    return [repr(call) for call in model.generate.call_args_list]


def service_with(tmp_path, turns, **options):
    model = AsyncMock()
    model.generate.side_effect = list(turns)
    service = RagAgentService(model=model, mcp_servers=(), include_knowledge=False,
                              personas_dir=str(tmp_path / 'personas'), **options)
    return service, model


async def test_a_new_task_takes_the_persona_instructions_as_a_constraint(tmp_path):
    service, model = service_with(tmp_path, [ModelTurn('done')], memory_directory=tmp_path / 'archive')
    persona = service.personas.create(None, 'Backend', 'Answer with code first.')
    service.personas.bind(None, 's', persona.persona_id)
    events = []
    async with service:
        result = await service.query_result('goal', 's', on_event=events.append)
    assert result.content == 'done'
    (sent,) = requests_text(model)
    assert LEAD in sent and 'Answer with code first.' in sent
    (note,) = [e for e in events if e['type'] == 'persona']
    assert note == {'type': 'persona', 'persona_id': persona.persona_id, 'name': 'Backend', 'chars': 23,
                    'used': [], 'skipped': []}
    # The archive, audit and run records keep numbers, never the instructions.
    for path in files(tmp_path / 'archive'):
        assert 'Answer with code first.' not in path.read_text(encoding='utf-8', errors='replace')


async def test_an_empty_persona_adds_no_constraint(tmp_path):
    service, model = service_with(tmp_path, [ModelTurn('done')])
    async with service:
        await service.query_result('goal', 's')
    (sent,) = requests_text(model)
    assert LEAD not in sent
    assert service.personas.is_bound(None, 's')        # the first task fixes the session's persona


async def test_personas_off_changes_nothing(tmp_path):
    model = AsyncMock()
    model.generate.side_effect = [ModelTurn('done')]
    events = []
    service = RagAgentService(model=model, mcp_servers=(), include_knowledge=False)
    assert service.personas is None
    async with service:
        await service.query_result('goal', 's', on_event=events.append)
    assert not [e for e in events if e['type'] == 'persona']
    with pytest.raises(ValueError):
        await service.query_result('goal', 's', persona='default')


async def test_a_resumed_task_keeps_its_constraint_and_persona(tmp_path):
    ask = ModelTurn(tool_calls=(ToolCall('ask', 'ask_user', {'question': 'which?'}),))
    service, model = service_with(tmp_path, [ask, ModelTurn('done')])
    first = service.personas.create(None, 'First', 'Rule one.')
    second = service.personas.create(None, 'Second', 'Rule two.')
    service.personas.bind(None, 's', first.persona_id)
    async with service:
        blocked = await service.query_result('goal', 's')
        service.personas.bind(None, 's', second.persona_id)
        events = []
        await service.query_result('this one', 's', resume_task_id=blocked.task_id, on_event=events.append)
    last = requests_text(model)[-1]
    assert last.count(LEAD) == 1 and 'Rule one.' in last and 'Rule two.' not in last
    assert not [e for e in events if e['type'] == 'persona']


async def test_the_request_persona_is_for_this_request_only(tmp_path):
    service, model = service_with(tmp_path, [ModelTurn('a'), ModelTurn('b')])
    other = service.personas.create(None, 'Other', 'Other rules.')
    service.personas.bind(None, 's', DEFAULT_ID)
    async with service:
        await service.query_result('goal', 's', persona=other.persona_id)
        await service.query_result('goal', 's')
    first, second = requests_text(model)
    assert 'Other rules.' in first and 'Other rules.' not in second
    assert service.personas.session_persona_id(None, 's') == DEFAULT_ID


async def test_defaults_fill_only_open_choices_and_unusable_ones_are_skipped(tmp_path):
    ask = ModelTurn(tool_calls=(ToolCall('ask', 'ask_user', {'question': 'q'}),))
    service, model = service_with(tmp_path, [ask, ask])
    service.personas.update(None, DEFAULT_ID, options={'mode': 'team', 'write': False, 'use_memory': True})
    events = []
    async with service:
        first = await service.query_result('goal', 's', on_event=events.append)
        second = await service.query_result('goal', 't', allow_writes=True, on_event=events.append)
    options = service.task_options('s', first.task_id)
    assert options['mode'] == 'single' and options['write'] is False and options['memory'] is False
    assert service.task_options('t', second.task_id)['write'] is True        # the request's own choice wins
    notes = [e for e in events if e['type'] == 'persona']
    assert notes[0]['used'] == ['write'] and notes[0]['skipped'] == ['mode', 'use_memory']
    assert notes[1]['used'] == [] and notes[1]['skipped'] == ['mode', 'use_memory']


async def test_a_broken_persona_file_stops_the_task_before_anything_runs(tmp_path):
    service, model = service_with(tmp_path, [ModelTurn('done')], memory_directory=tmp_path / 'archive')
    service.personas.update(None, DEFAULT_ID, instructions='x')
    service.personas.file_path(None, DEFAULT_ID).write_text('broken', encoding='utf-8')
    async with service:
        with pytest.raises(PersonaError):
            await service.query_result('goal', 's')
    assert model.generate.call_count == 0
    assert not (tmp_path / 'archive').exists() or not [p for p in files(tmp_path / 'archive')
                                                       if p.suffix == '.json' and 'turn' in p.name]


def test_team_members_keep_the_constraints():
    from dataclasses import fields
    from app.agent.delegation import _member_context
    from app.agent.step_models import StepExecutionContext
    names = {f.name for f in fields(StepExecutionContext)}
    assert 'constraints' in names
    context = StepExecutionContext(**{name: None for name in names} | {'constraints': ('rule',), 'active_goal': 'g'})
    assert _member_context(context, 'assignment').constraints == ('rule',)


def test_the_bench_turns_personas_off():
    source = Path('bench/run_bench.py').read_text(encoding='utf-8')
    assert "PERSONAS_DIR=''" in source


def test_the_personas_directory_is_closed_to_the_agent(tmp_path):
    from types import SimpleNamespace
    from app.services.workspace import WorkspacePolicy
    settings = SimpleNamespace(conversation_storage_dir='', personas_dir=str(tmp_path / 'p'),
                               workspace_deny_dirs='', workspace_allowed_roots='')
    assert (tmp_path / 'p').resolve() in WorkspacePolicy.from_config(settings).closed_folders()


# ---- the command line -------------------------------------------------------------------------

class FakeService:
    def __init__(self, store):
        self.personas = store


def run_command(store, line, session='s', waiting=None):
    lines = []
    persona_commands.persona_command(line, FakeService(store), None, session, lines.append, waiting)
    return lines


def test_cli_commands(tmp_path):
    store = store_at(tmp_path)
    assert run_command(store, '/persona') == ['人设（* 是这个会话用的）：', '* 默认  指令 0 字']
    assert run_command(store, '/persona new 学 agent')[0].startswith('已新建人设「学 agent」')
    created = store.find(None, '学 agent')
    assert store.session_persona_id(None, 's') == created.persona_id
    assert run_command(store, '/btw 先讲概念') == ['已加到「学 agent」的指令里，共 4 字，下个任务起生效。']
    assert run_command(store, '/persona set mode team')[0] == '已更新「学 agent」的默认设置：执行模式 team。'
    assert run_command(store, '/persona set write 关')[0] == '已更新「学 agent」的默认设置：执行模式 team，写文件 关。'
    assert run_command(store, '/persona set mode -')[0] == '已更新「学 agent」的默认设置：写文件 关。'
    assert run_command(store, '/persona set colour red') == [
        '不认识的设置：colour。可以设置 mode、write、use_memory、workspace。']
    shown = run_command(store, '/persona show')
    assert shown[1] == '默认设置：写文件 关' and shown[2] == '指令（4 字）：' and shown[3] == '  1  先讲概念'
    assert run_command(store, '/btw undo') == ['已撤掉上一次 /btw，「学 agent」的指令现在 0 字。']
    assert run_command(store, '/persona 默认', waiting='t')[0].startswith('已切换到「默认」，下个任务起生效。（等待回答的任务仍用原来的人设）')
    assert run_command(store, '/persona 默认') == ['这个会话已经在用「默认」。']
    assert run_command(store, '/persona nobody') == ['没有叫「nobody」的人设。用 /persona 查看现有的人设。']
    assert run_command(store, '/persona rename x') == ['“默认”人设不能改名。']
    assert run_command(store, '/persona delete 学 agent') == ['已删除人设「学 agent」，用它的会话回到“默认”。']


def test_cli_without_personas():
    lines = []
    persona_commands.persona_command('/btw x', FakeService(None), None, 's', lines.append)
    assert lines == [persona_commands.PERSONAS_OFF]


def test_the_cli_notice_and_the_persona_flag():
    notice = persona_commands.persona_notice
    assert notice({'persona_id': 'default', 'name': '默认', 'chars': 0, 'used': [], 'skipped': []}) is None
    assert notice({'persona_id': 'p-1', 'name': '学后端', 'chars': 12, 'used': ['mode'], 'skipped': ['workspace']}) == (
        '本次任务用的人设：学后端（指令 12 字）。用了它的默认设置：执行模式。这些默认设置这次用不了，已跳过：工作目录。')
    assert build_parser().parse_args(['goal', '--persona', '学后端']).persona == '学后端'


# ---- the web API --------------------------------------------------------------------------------

class api:
    def __init__(self, service):
        self.service = service
        self.app = create_app(service, user_id='local', access_token=TOKEN)

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


async def test_web_persona_endpoints(tmp_path):
    service, _ = service_with(tmp_path, [])
    async with api(service) as web:
        assert (await web.client.get('/api/health')).json()['personas'] is True
        created = await web.client.post('/api/personas', json={'name': '学 agent', 'instructions': 'Rules.',
                                                               'options': {'mode': 'single', 'write': None}})
        assert created.status_code == 201
        persona_id = created.json()['persona_id']
        assert created.json()['options'] == {'mode': 'single'}
        listing = (await web.client.get('/api/personas', params={'session_id': 's'})).json()
        assert [p['name'] for p in listing['personas']] == ['默认', '学 agent'] and listing['current'] == 'default'
        assert 'instructions' not in listing['personas'][1]
        bound = await web.client.put('/api/sessions/s/persona', json={'persona_id': persona_id})
        assert bound.json()['name'] == '学 agent'
        assert (await web.client.get('/api/sessions/s/persona')).json()['persona_id'] == persona_id
        after = await web.client.post(f'/api/personas/{persona_id}/btw', json={'text': 'More.'})
        assert after.json()['instructions'] == 'Rules.\nMore.'
        undone = await web.client.post(f'/api/personas/{persona_id}/btw', json={'undo': True})
        assert undone.json()['instructions'] == 'Rules.'
        changed = await web.client.put(f'/api/personas/{persona_id}', json={'options': {'mode': None}})
        assert changed.json()['options'] == {}
        detail = (await web.client.get(f'/api/personas/{persona_id}')).json()
        assert detail['instructions'] == 'Rules.'
        assert (await web.client.delete(f'/api/personas/{persona_id}')).json() == {'deleted': persona_id}


async def test_web_persona_errors(tmp_path):
    service, _ = service_with(tmp_path, [])
    async with api(service) as web:
        refused = await web.client.post('/api/personas', json={'name': 'new'})
        assert refused.status_code == 400 and refused.json() == {
            'code': 'persona_invalid_name', 'message': '这个名字和命令冲突，请换一个。'}
        missing = await web.client.get('/api/personas/p-00000000')
        assert missing.status_code == 404 and missing.json()['code'] == 'persona_not_found'
        bad_id = await web.client.get('/api/personas/..%2Fstate')
        assert bad_id.status_code in (400, 404)
        too_big = await web.client.put('/api/personas/default', json={'instructions': 'x' * 20001})
        assert too_big.status_code == 400 and too_big.json()['code'] == 'invalid_input'
        too_long = await web.client.put('/api/personas/default', json={'instructions': 'x' * 4001})
        assert too_long.json()['code'] == 'persona_too_long'
        extra = await web.client.post('/api/personas', json={'name': 'a', 'colour': 'red'})
        assert extra.status_code == 400
        no_token = await web.client.get('/api/personas', headers={'Authorization': ''})
        assert no_token.status_code == 401


async def test_web_without_personas(tmp_path):
    model = AsyncMock()
    service = RagAgentService(model=model, mcp_servers=(), include_knowledge=False)
    async with api(service) as web:
        assert 'personas' not in (await web.client.get('/api/health')).json()
        response = await web.client.get('/api/personas')
        assert response.status_code == 404 and response.json()['code'] == 'personas_disabled'


async def test_web_messages_take_the_persona_defaults_for_omitted_choices(tmp_path):
    ask = ModelTurn(tool_calls=(ToolCall('ask', 'ask_user', {'question': 'q'}),))
    service, model = service_with(tmp_path, [ask, ask])
    service.personas.update('local', DEFAULT_ID, instructions='Persona rule.', options={'write': False})
    async with api(service) as web:
        first = (await web.client.post('/api/sessions/s/messages',
                                       json={'content': 'goal', 'request_id': 'r1'})).json()
        run = web.app.state.runs.get(first['run_id'])
        await run.task
        notes = [e for e in run.events if e['type'] == 'persona']
        assert notes[0]['used'] == ['write'] and notes[0]['chars'] == len('Persona rule.')
        second = (await web.client.post('/api/sessions/s/messages',
                                        json={'content': 'goal', 'request_id': 'r2',
                                              'options': {'write': False}})).json()
        run = web.app.state.runs.get(second['run_id'])
        await run.task
        assert [e for e in run.events if e['type'] == 'persona'][0]['used'] == []
    assert all('Persona rule.' in text for text in requests_text(model))
