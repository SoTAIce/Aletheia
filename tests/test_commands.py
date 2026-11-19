"""Personal commands (docs/personal_commands_plan.md): the files, the service, the command line
and the web API.

Offline only: temporary directories and a scripted model.
"""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from app.agent import persona_commands
from app.agent.__main__ import interactive, personal_command
from app.agent.step_models import ModelTurn, ToolCall
from app.api.app import create_app
from app.services import personal_commands as module
from app.services.personal_commands import (
    PRESET_NAMES, Command, CommandError, check_name, expand, parse, preset, render, split_line,
)
from app.services.personas import DEFAULT_ID, PersonaError, PersonaStore
from app.services.rag_agent_service import RagAgentService

TOKEN = 'test-token'
AUTH = {'Authorization': f'Bearer {TOKEN}'}
LEAD = 'The user ran the personal command /'
PERSONA_LEAD = 'Instructions the user wrote for the persona'


def store_at(tmp_path):
    return PersonaStore(tmp_path / 'personas')


def command_dir(store, persona_id, user=None):
    return store.commands.directory(user, persona_id)


# ---- the files ---------------------------------------------------------------------------------

def test_the_six_presets_parse_as_the_plan_says():
    expected = {'explain': ('required', 'single', False), 'quiz': ('required', 'single', False),
                'note': ('optional', 'single', True), 'review': ('required', 'single', False),
                'test': ('required', 'single', True), 'fix': ('required', 'single', True)}
    assert set(PRESET_NAMES) == set(expected)
    for name, (args, mode, write) in expected.items():
        command = preset(name)
        assert (command.args, command.mode, command.write) == (args, mode, write)
        assert module.PLACEHOLDER in command.template and command.description
        # The repository file is exactly what render writes, so a reset copy is byte for byte the same.
        assert render(command) == (module.PRESET_DIR / f'{name}.md').read_text(encoding='utf-8')


def test_a_persona_gets_the_presets_on_first_use_and_keeps_its_own(tmp_path):
    store = store_at(tmp_path)
    other = store.create(None, '学后端')
    assert store.commands.count(None, DEFAULT_ID) == 0            # nothing is copied by counting
    names = [c['name'] for c in store.commands.list_commands(None, DEFAULT_ID)]
    assert names == sorted(PRESET_NAMES)
    store.commands.update(None, DEFAULT_ID, 'explain', template='Explain {{args}} with diagrams.')
    assert store.commands.get(None, DEFAULT_ID, 'explain').template == 'Explain {{args}} with diagrams.'
    # Another persona starts from the presets and is not touched by the first one's edit.
    assert store.commands.get(None, other.persona_id, 'explain').template == preset('explain').template
    store.commands.delete(None, other.persona_id, 'quiz')
    assert not store.commands.has(None, other.persona_id, 'quiz')     # a deleted preset stays deleted
    assert store.commands.has(None, DEFAULT_ID, 'quiz')
    store.commands.reset(None, DEFAULT_ID, 'explain')
    assert store.commands.get(None, DEFAULT_ID, 'explain') == preset('explain')
    store.commands.reset(None, other.persona_id, 'quiz')
    assert store.commands.has(None, other.persona_id, 'quiz')


def test_deleting_a_persona_deletes_its_commands(tmp_path):
    store = store_at(tmp_path)
    persona = store.create(None, '学 agent')
    store.commands.create(None, persona.persona_id, 'mine')
    directory = command_dir(store, persona.persona_id)
    assert store.commands.count(None, persona.persona_id) == len(PRESET_NAMES) + 1
    store.delete(None, persona.persona_id)
    assert not directory.exists()
    with pytest.raises(PersonaError):
        store.commands.list_commands(None, persona.persona_id)          # the persona is gone


def test_create_update_and_the_limits(tmp_path, monkeypatch):
    store = store_at(tmp_path)
    made = store.commands.create(None, DEFAULT_ID, 'plan')
    assert made.description and module.PLACEHOLDER in made.template     # the skeleton is valid
    with pytest.raises(CommandError) as duplicate:
        store.commands.create(None, DEFAULT_ID, 'plan')
    assert duplicate.value.code == 'duplicate'
    changed = store.commands.update(None, DEFAULT_ID, 'plan', {'mode': 'team', 'write': False, 'usage': '/plan 目标'},
                                    template='Plan {{args}}.')
    assert (changed.mode, changed.write, changed.usage_text) == ('team', False, '/plan 目标')
    cleared = store.commands.update(None, DEFAULT_ID, 'plan', {'mode': None, 'write': None, 'usage': None})
    assert (cleared.mode, cleared.write, cleared.usage) == (None, None, None)
    for fields in ({'args': 'some'}, {'mode': 'auto'}, {'write': 'yes'}, {'description': None},
                   {'description': 'two\nlines'}, {'colour': 'red'}):
        with pytest.raises(CommandError):
            store.commands.update(None, DEFAULT_ID, 'plan', fields)
    with pytest.raises(CommandError) as long:
        store.commands.update(None, DEFAULT_ID, 'plan', template='x' * 4001)
    assert long.value.code == 'too_long'
    with pytest.raises(CommandError):
        store.commands.update(None, DEFAULT_ID, 'plan', template='   ')
    monkeypatch.setattr(module, 'MAX_COMMANDS', len(PRESET_NAMES) + 1)
    with pytest.raises(CommandError) as many:
        store.commands.create(None, DEFAULT_ID, 'another')
    assert many.value.code == 'too_many'
    with pytest.raises(CommandError) as not_preset:
        store.commands.reset(None, DEFAULT_ID, 'plan')
    assert not_preset.value.code == 'not_preset'


@pytest.mark.parametrize('name', ['', 'Review', '1abc', 'a_b', 'a' * 21, 'persona', 'btw', 'commands',
                                  'help', 'new', 'dir', 'remember', '../x', 'a b'])
def test_bad_or_taken_names_are_refused(name):
    with pytest.raises(CommandError):
        check_name(name)


def test_the_file_format_and_its_problems(tmp_path):
    store = store_at(tmp_path)
    path = store.commands.file_path(None, DEFAULT_ID, 'review')
    store.commands.list_commands(None, DEFAULT_ID)
    path.write_text('+++\ndescription = "Mine"\nargs = "none"\n+++\nHand edited.\n', encoding='utf-8')
    assert store.commands.get(None, DEFAULT_ID, 'review') == Command('review', 'Mine', 'Hand edited.', args='none')
    for text, code in (('no header', 'bad_file'), ('+++\ndescription = "x"\n', 'bad_file'),
                       ('+++\ndescription = \n+++\nx', 'bad_file'), ('+++\nargs = "none"\n+++\nx', 'bad_file'),
                       ('+++\ndescription = "x"\ncolour = "red"\n+++\nx', 'bad_file'),
                       ('+++\ndescription = "x"\nwrite = "no"\n+++\nx', 'invalid_field')):
        path.write_text(text, encoding='utf-8')
        with pytest.raises(CommandError) as problem:
            store.commands.get(None, DEFAULT_ID, 'review')
        assert problem.value.code == code
    listed = {c['name']: c for c in store.commands.list_commands(None, DEFAULT_ID)}
    assert listed['review']['problem'] == 'invalid_field' and 'description' not in listed['review']
    assert parse('x', '﻿+++\r\ndescription = "d"\r\n+++\r\nbody\r\n').template == 'body'


def test_expanding_the_template():
    command = Command('review', 'd', 'Review {{args}} now; {{args}} again.', args='required', usage='/review 路径')
    assert expand(command, ' app/api ') == 'Review app/api now; app/api again.'
    with pytest.raises(CommandError) as missing:
        expand(command, '')
    assert missing.value.code == 'missing_args' and '/review 路径' in missing.value.message
    optional = Command('note', 'd', 'Notes on 「{{args}}」.', args='optional')
    assert expand(optional, '') == 'Notes on 「（未指定）」.'
    trailing = Command('ask', 'd', 'Answer from the knowledge base only.', args='optional')
    assert expand(trailing, 'what is RRF') == 'Answer from the knowledge base only.\nwhat is RRF'
    assert expand(trailing, '') == 'Answer from the knowledge base only.'
    none = Command('status', 'd', 'Say where we are.', args='none')
    with pytest.raises(CommandError) as extra:
        expand(none, 'x')
    assert extra.value.code == 'unexpected_args'
    assert split_line('/review  app/api\nmore') == ('review', 'app/api\nmore')
    assert split_line('/note') == ('note', '') and split_line('review x') is None
    assert split_line('/Review x') is None and split_line('/') is None


def test_the_commands_directory_lives_under_the_personas_directory(tmp_path):
    store = store_at(tmp_path)
    directory = command_dir(store, DEFAULT_ID, 'someone@example.com')
    assert (tmp_path / 'personas') in directory.parents
    assert 'someone' not in str(directory)


# ---- the service ------------------------------------------------------------------------------

def requests_text(model):
    return [repr(call) for call in model.generate.call_args_list]


def service_with(tmp_path, turns, **options):
    model = AsyncMock()
    model.generate.side_effect = list(turns)
    service = RagAgentService(model=model, mcp_servers=(), include_knowledge=False,
                              personas_dir=str(tmp_path / 'personas'), **options)
    return service, model


async def test_a_command_becomes_a_constraint_after_the_persona_and_the_line_is_kept(tmp_path):
    service, model = service_with(tmp_path, [ModelTurn('done')], memory_directory=tmp_path / 'archive')
    persona = service.personas.create(None, '学 agent', 'Persona rule.')
    service.personas.bind(None, 's', persona.persona_id)
    service.personas.commands.update(None, persona.persona_id, 'review', template='Review {{args}} carefully.')
    events = []
    async with service:
        result = await service.query_result('/review app/api', 's', on_event=events.append, command='review')
        turns = await service.session_turns('s')
    assert result.content == 'done'
    (sent,) = requests_text(model)
    assert LEAD + 'review.' in sent and 'Review app/api carefully.' in sent
    assert sent.index(PERSONA_LEAD) < sent.index(LEAD)
    (note,) = [e for e in events if e['type'] == 'command']
    assert note == {'type': 'command', 'name': 'review', 'chars': len('Review {{args}} carefully.'),
                    'fixed': ['mode', 'write'], 'mode': 'single', 'write': False}
    (route,) = [e for e in events if e['type'] == 'route']
    assert route['read_only'] is True and route['options']['write'] is False
    assert turns[-1]['user'] == '/review app/api'
    # The archive and audit keep the user's line, never the template.
    for path in (tmp_path / 'archive').rglob('*'):
        if path.is_file():
            assert 'carefully.' not in path.read_text(encoding='utf-8', errors='replace')


async def test_the_command_mode_wins_over_the_request_and_the_persona(tmp_path):
    ask = ModelTurn(tool_calls=(ToolCall('ask', 'ask_user', {'question': 'q'}),))
    service, model = service_with(tmp_path, [ask, ask], execution_mode='auto')
    service.personas.update(None, DEFAULT_ID, options={'mode': 'auto', 'write': False})
    events = []
    async with service:
        fixed = await service.query_result('/quiz RAG', 's', execution_mode='auto', on_event=events.append,
                                           command='quiz')
        plain = await service.query_result('quiz me', 't')
    assert service.task_options('s', fixed.task_id)['mode'] == 'single'
    assert service.task_options('t', plain.task_id)['mode'] == 'auto'
    persona_note = [e for e in events if e['type'] == 'persona'][0]
    assert 'mode' not in persona_note['used'] and 'write' not in persona_note['used']


async def test_a_command_that_writes_does_not_start_when_writing_is_off(tmp_path):
    service, model = service_with(tmp_path, [ModelTurn('done')], memory_directory=tmp_path / 'archive')
    async with service:
        with pytest.raises(CommandError) as refused:
            await service.query_result('/fix the bug', 's', allow_writes=False, command='fix')
        assert refused.value.code == 'needs_write'
        service.personas.update(None, DEFAULT_ID, options={'write': False})
        with pytest.raises(CommandError):
            await service.query_result('/fix the bug', 's', command='fix')      # the persona's read-only too
        service.personas.update(None, DEFAULT_ID, options={'write': None})
        result = await service.query_result('/fix the bug', 's', command='fix')
    assert result.content == 'done' and model.generate.call_count == 1


async def test_bad_command_requests_stop_before_the_model(tmp_path):
    service, model = service_with(tmp_path, [ModelTurn('done')], memory_directory=tmp_path / 'archive')
    async with service:
        for line, name, code in (('/review', 'review', 'missing_args'), ('/nothing x', 'nothing', 'not_found'),
                                 ('/review x', 'explain', 'mismatch')):
            with pytest.raises(CommandError) as problem:
                await service.query_result(line, 's', command=name)
            assert problem.value.code == code
        service.personas.commands.update(None, DEFAULT_ID, 'review', {'mode': 'team'})
        with pytest.raises(CommandError) as team:
            await service.query_result('/review x', 's', command='review')
        assert team.value.code == 'mode_unavailable'
    assert model.generate.call_count == 0


async def test_a_resumed_command_task_keeps_its_constraint(tmp_path):
    ask = ModelTurn(tool_calls=(ToolCall('ask', 'ask_user', {'question': 'answer these'}),))
    service, model = service_with(tmp_path, [ask, ModelTurn('graded')])
    async with service:
        blocked = await service.query_result('/quiz RAG', 's', command='quiz')
        service.personas.commands.update(None, DEFAULT_ID, 'quiz', template='Changed {{args}}.')
        events = []
        await service.query_result('my answers', 's', resume_task_id=blocked.task_id, on_event=events.append)
        with pytest.raises(ValueError):
            await service.query_result('/quiz x', 's', resume_task_id=blocked.task_id, command='quiz')
    last = requests_text(model)[-1]
    assert last.count(LEAD) == 1 and '围绕「RAG」' in last and 'Changed' not in last
    assert not [e for e in events if e['type'] == 'command']


async def test_commands_need_personas(tmp_path):
    model = AsyncMock()
    service = RagAgentService(model=model, mcp_servers=(), include_knowledge=False)
    async with service:
        with pytest.raises(ValueError):
            await service.query_result('/review x', 's', command='review')


# ---- the command line -------------------------------------------------------------------------

def run_commands(store, line, answers=()):
    lines = []
    replies = list(answers)

    async def read_line(prompt):
        lines.append(prompt)
        if not replies:
            raise EOFError
        return replies.pop(0)
    asyncio.run(persona_commands.commands_command(line, store, None, 's', lines.append, read_line))
    return lines


def test_cli_commands_command(tmp_path):
    store = store_at(tmp_path)
    listing = run_commands(store, '/commands')
    assert listing[0] == '「默认」的命令：'
    assert '  /review  只读审查代码，按位置和严重程度列出问题（只读）' in listing
    assert '  /fix  找到原因再修，改完说明改了什么（会改文件）' in listing
    shown = run_commands(store, '/commands show review')
    assert shown[0].startswith('命令 /review（「默认」），文件：') and shown[0].endswith('review.md')
    assert shown[1] == '只读审查代码，按位置和严重程度列出问题。用法 /review 路径，必须给参数，固定 single，只读。'
    assert shown[3] == '  1  审查 {{args}}。'
    assert run_commands(store, '/commands new plan')[0].startswith('已给「默认」新建命令 /plan。')
    assert run_commands(store, '/commands new review') == ['已经有 /review 这个命令了。']
    assert run_commands(store, '/commands delete review', ['n'])[-1] == '已取消，命令没有删除。'
    assert run_commands(store, '/commands delete review', ['y'])[-1] == '已删除 /review。'
    assert run_commands(store, '/commands delete review') == ['「默认」里没有 /review 这个命令。']
    assert run_commands(store, '/commands reset review') == ['已把「默认」的 /review 恢复成预置的样子。']
    assert run_commands(store, '/commands reset plan')[0].startswith('/plan 不是预置命令')
    assert run_commands(store, '/commands nonsense')[0].startswith('用法：')
    assert run_commands(None, '/commands') == [persona_commands.COMMANDS_OFF]


def test_cli_persona_delete_names_its_commands(tmp_path):
    store = store_at(tmp_path)
    persona = store.create(None, '学 agent')
    store.commands.list_commands(None, persona.persona_id)
    lines = []
    asyncio.run(persona_commands._delete(store, None, '学 agent', lines.append, None, None))
    assert lines == [f'已删除人设「学 agent」和它的 {len(PRESET_NAMES)} 个命令，用它的会话回到“默认”。']


def test_the_cli_notice_and_the_line_check(tmp_path):
    notice = persona_commands.command_notice
    assert notice({'name': 'review', 'mode': 'single', 'write': False}) == '命令 /review：只读，single 循环。'
    assert notice({'name': 'mine', 'mode': None, 'write': None}) == '命令 /mine。'
    store = store_at(tmp_path)
    assert personal_command(store, None, 's', '/review app') == 'review'
    assert personal_command(store, None, 's', '/nothing app') is None
    assert personal_command(store, None, 's', 'review app') is None
    assert personal_command(None, None, 's', '/review app') is None


class Args:
    session_id = 's'
    goal = None
    user_id = None


async def test_the_interactive_loop_runs_a_command_and_refuses_one_while_waiting(tmp_path):
    ask = ModelTurn(tool_calls=(ToolCall('ask', 'ask_user', {'question': 'answer these'}),))
    service, model = service_with(tmp_path, [ask, ModelTurn('graded')])
    lines = iter(['/quiz RAG', '/review x', 'my answers', '/unknown', '/exit'])
    out = []

    async def read_line(prompt):
        return next(lines)
    async with service:
        assert await interactive(service, Args(), read_line=read_line, write=out.append) == 0
    assert '命令 /quiz：只读，single 循环。' in out
    assert persona_commands.WAITING_HINT in out
    assert any(line.startswith('未知命令：/unknown。') for line in out)
    assert 'graded' in out
    assert model.generate.call_count == 2


# ---- the web API --------------------------------------------------------------------------------

class api:
    def __init__(self, service):
        self.service = service
        self.app = create_app(service, user_id='local', access_token=TOKEN, write_enabled=True)

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


async def test_web_command_endpoints(tmp_path):
    service, _ = service_with(tmp_path, [])
    async with api(service) as web:
        base = '/api/personas/default/commands'
        listing = (await web.client.get(base)).json()['commands']
        assert [c['name'] for c in listing] == sorted(PRESET_NAMES)
        assert 'template' not in listing[0] and listing[0]['preset'] is True
        detail = (await web.client.get(base + '/review')).json()
        assert detail['template'] == preset('review').template and detail['own_usage'] == '/review 路径'
        created = await web.client.post(base, json={'name': 'plan', 'description': '只出方案',
                                                    'write': False, 'template': 'Plan {{args}}.'})
        assert created.status_code == 201 and created.json()['write'] is False
        changed = await web.client.put(base + '/plan', json={'write': None, 'mode': 'single'})
        assert changed.json()['write'] is None and changed.json()['mode'] == 'single'
        assert changed.json()['template'] == 'Plan {{args}}.'            # left out: kept
        renamed = await web.client.put(base + '/plan', json={'name': 'other'})
        assert renamed.status_code == 400
        assert (await web.client.delete(base + '/plan')).json() == {'deleted': 'plan'}
        await web.client.put(base + '/review', json={'template': 'Mine {{args}}.'})
        reset = await web.client.post(base + '/review/reset')
        assert reset.json()['template'] == preset('review').template
        persona = (await web.client.post('/api/personas', json={'name': '学 agent'})).json()
        assert (await web.client.get(f"/api/personas/{persona['persona_id']}")).json()['commands'] == 0
        await web.client.get(f"/api/personas/{persona['persona_id']}/commands")
        assert (await web.client.get(f"/api/personas/{persona['persona_id']}")).json()['commands'] == len(PRESET_NAMES)


async def test_web_command_errors(tmp_path):
    service, _ = service_with(tmp_path, [])
    async with api(service) as web:
        base = '/api/personas/default/commands'
        bad = await web.client.post(base, json={'name': 'persona', 'description': 'x'})
        assert bad.status_code == 400 and bad.json()['code'] == 'command_invalid_name'
        missing = await web.client.get(base + '/nothing')
        assert missing.status_code == 404 and missing.json()['code'] == 'command_not_found'
        no_persona = await web.client.get('/api/personas/p-00000000/commands')
        assert no_persona.status_code == 404
        too_long = await web.client.put(base + '/review', json={'template': 'x' * 4001})
        assert too_long.json()['code'] == 'command_too_long'
        bad_mode = await web.client.put(base + '/review', json={'mode': 'auto'})
        assert bad_mode.status_code == 400 and bad_mode.json()['code'] == 'invalid_input'
        not_preset = await web.client.post(base + '/review/reset'.replace('review', 'nothing'))
        assert not_preset.status_code == 400 and not_preset.json()['code'] == 'command_not_preset'
        no_token = await web.client.get(base, headers={'Authorization': ''})
        assert no_token.status_code == 401


async def test_web_messages_run_a_command_and_refuse_a_mismatch(tmp_path):
    service, model = service_with(tmp_path, [ModelTurn('done')])
    async with api(service) as web:
        sent = await web.client.post('/api/sessions/s/messages', json={
            'content': '/review app/api', 'request_id': 'r1', 'command': 'review', 'options': {'write': True}})
        assert sent.status_code == 202
        run = web.app.state.runs.get(sent.json()['run_id'])
        await run.task
        assert [e for e in run.events if e['type'] == 'command'][0]['name'] == 'review'
        assert run.options['write'] is False                    # the command forced read-only
        again = await web.client.post('/api/sessions/s/messages', json={
            'content': '/review app/api', 'request_id': 'r1', 'command': 'review', 'options': {'write': True}})
        assert again.status_code == 200                         # the same request, not a new run
        mismatch = await web.client.post('/api/sessions/s/messages', json={
            'content': 'review app/api', 'request_id': 'r2', 'command': 'review'})
        assert mismatch.status_code == 400
        refused = await web.client.post('/api/sessions/s/messages', json={
            'content': '/fix it', 'request_id': 'r3', 'command': 'fix', 'options': {'write': False}})
        run = web.app.state.runs.get(refused.json()['run_id'])
        await run.task
        assert run.events[-1]['code'] == 'command_needs_write'
    (text,) = requests_text(model)
    assert '审查 app/api。' in text
