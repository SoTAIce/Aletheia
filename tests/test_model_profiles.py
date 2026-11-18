"""Stage 4d-2: the model profile file and roles (docs/stage4d_plan.md section 10). No model is called."""

import asyncio
from types import SimpleNamespace

import pytest

from app.agent.agent_loop import LOOP_STEP, LoopOptions
from app.agent.delegation import (DELEGATE, DelegatingLoopRunner, ExploreLoopRunner, MemberSpawner)
from app.agent.run_meter import RunMeter, metering, record_model_call
from app.agent.step_models import ModelTurn, ToolCall, ToolDefinition, ToolOutput
from app.agent.step_runner import RoleModel
from app.config import Settings, config
from app.models.model_profile import ModelProfile, ModelProfileError, ModelProfiles, ROLES
from app.services.context_manager import ContextManager
from app.tools.tool_registry import ToolRegistry

MAIN = '''
[profiles.main]
base_url = "https://relay.test/v1"
api_key_env = "AGENT_MODEL_API_KEY"
model = "main-model"
context_tokens = 128000
prompt_cache_key = true
'''
CHEAP = '''
[profiles.cheap]
base_url = "https://cheap.test/v1"
api_key_env = "CHEAP_MODEL_API_KEY"
model = "cheap-model"
context_tokens = 16000
input_price_per_million = 1.0
output_price_per_million = 4.0
'''
ROLES_CHEAP = '''
[roles]
main = "main"
route = "cheap"
member = "cheap"
explorer = "cheap"
'''


def _settings(path, **kw):
    values = dict(_env_file=None, dashscope_api_key='ds-key', agent_model_base_url='https://relay.test/v1',
                  agent_model_api_key='main-key', agent_model='gpt-main', context_output_tokens=2048,
                  rag_model_timeout_seconds=90.0, model_input_price_per_million_tokens=2.0,
                  model_output_price_per_million_tokens=8.0)
    values['model_profiles_file'] = str(path)
    values.update(kw)
    return Settings(**values)


@pytest.fixture
def cheap_key(monkeypatch):
    monkeypatch.setenv('CHEAP_MODEL_API_KEY', 'cheap-key')


def _write(tmp_path, text):
    path = tmp_path / 'models.toml'
    path.write_text(text, encoding='utf-8')
    return path


# --- the file ----------------------------------------------------------------------------------

def test_without_a_file_every_role_uses_the_default_profile(tmp_path):
    settings = _settings(tmp_path / 'models.toml', model_profiles_file='')
    profiles = settings.model_profiles()
    assert profiles.roles == dict.fromkeys(ROLES, 'default')
    assert profiles.role('main') == settings.agent_model_profile and len(profiles.used()) == 1
    # The default name may be absent: the settings without the file, as before 4d-2.
    absent = Settings(_env_file=None, dashscope_api_key='k')
    assert 'model_profiles_file' not in absent.model_fields_set
    assert absent.model_profiles().roles['member'] == 'default'


def test_a_path_set_explicitly_must_exist(tmp_path):
    with pytest.raises(ModelProfileError, match='找不到模型档案文件'):
        _settings(tmp_path / 'missing.toml').model_profiles()


def test_roles_left_out_use_main_and_unwritten_fields_follow_the_rules(tmp_path, cheap_key):
    path = _write(tmp_path, MAIN + CHEAP + '[roles]\nmain = "main"\nmember = "cheap"\n')
    settings = _settings(path)
    profiles = settings.model_profiles()
    assert profiles.roles == {'main': 'main', 'route': 'main', 'member': 'cheap', 'explorer': 'main'}
    cheap, default = profiles.profiles['cheap'], settings.agent_model_profile
    # Inherited from the .env settings:
    assert (cheap.max_output_tokens, cheap.timeout_seconds, cheap.temperature, cheap.json_mode) == (
        default.max_output_tokens, default.timeout_seconds, default.temperature, default.json_mode)
    # Written in the file:
    assert (cheap.context_tokens, cheap.model, cheap.input_price_per_million) == (16000, 'cheap-model', 1.0)
    # Never inherited: capabilities and prices belong to one model and provider.
    main = profiles.profiles['main']
    assert main.enable_thinking is None and main.prompt_cache_key is True
    assert (main.input_price_per_million, main.output_price_per_million) == (None, None)
    assert cheap.prompt_cache_key is False
    assert [p.name for p in profiles.used()] == ['main', 'cheap']


@pytest.mark.parametrize('text, message', [
    (MAIN + '[roles]\nmain = "main"\n[extra]\nx = 1\n', '不认识的段'),
    ('[roles]\nmain = "main"\n', '至少要有一个'),
    (MAIN, '缺少 [roles]'),
    (MAIN + 'colour = "blue"\n[roles]\nmain = "main"\n', '不认识的字段'),
    ('[profiles.main]\nbase_url = "https://x.test"\nmodel = "m"\n[roles]\nmain = "main"\n', '必须写明：api_key_env'),
    (MAIN + '[roles]\nmain = "main"\nboss = "main"\n', '不认识的角色'),
    (MAIN + '[roles]\nroute = "main"\n', '必须写 main'),
    (MAIN + '[roles]\nmain = "main"\nmember = "nowhere"\n', '指向不存在的档案'),
    (MAIN.replace('128000', '-5') + '[roles]\nmain = "main"\n', '取值不对'),
    (MAIN.replace('128000', '"big"') + '[roles]\nmain = "main"\n', '取值不对'),
    ('[profiles.main\n', '不是合法的 TOML'),
])
def test_a_bad_file_is_refused_with_a_reason(tmp_path, text, message):
    with pytest.raises(ModelProfileError, match=message.replace('[', r'\[')):
        _settings(_write(tmp_path, text)).model_profiles()


def test_an_empty_key_is_refused_without_showing_any_key(tmp_path, monkeypatch):
    monkeypatch.delenv('CHEAP_MODEL_API_KEY', raising=False)
    path = _write(tmp_path, MAIN + CHEAP + ROLES_CHEAP)
    with pytest.raises(ModelProfileError) as caught:
        _settings(path).model_profiles()
    assert 'CHEAP_MODEL_API_KEY' in str(caught.value) and 'main-key' not in str(caught.value)
    # A profile no role uses needs no key.
    unused = _write(tmp_path, MAIN + CHEAP + '[roles]\nmain = "main"\n')
    assert _settings(unused).model_profiles().roles['member'] == 'main'


def test_profiles_show_names_and_numbers_but_never_keys(tmp_path, cheap_key):
    profiles = _settings(_write(tmp_path, MAIN + CHEAP + ROLES_CHEAP)).model_profiles()
    shown = repr(profiles) + repr(profiles.describe())
    assert 'main-key' not in shown and 'cheap-key' not in shown
    assert ('member', 'cheap', 'cheap-model', 16000) in profiles.describe()


def test_the_example_file_parses(cheap_key):
    import pathlib
    example = pathlib.Path(__file__).resolve().parents[1] / 'models.example.toml'
    profiles = _settings(example).model_profiles()
    assert profiles.roles == {'main': 'main', 'route': 'cheap', 'member': 'cheap', 'explorer': 'cheap'}


def test_tests_never_read_a_personal_profile_file():
    assert config.model_profiles_file == ''      # tests/conftest.py


# --- building the models -----------------------------------------------------------------------

class _Client:
    opened = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        _Client.opened.append(self)

    async def create(self, **kwargs):
        raise AssertionError('no request in this test')

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True


def _service():
    from app.services.rag_agent_service import RagAgentService
    return RagAgentService(mcp_servers=(), include_knowledge=False)


async def test_each_used_profile_gets_one_model_and_same_endpoints_share_a_client(tmp_path, monkeypatch, cheap_key):
    import app.services.rag_agent_service as module
    monkeypatch.setattr(module, 'AsyncOpenAI', _Client)
    _Client.opened = []
    second = CHEAP.replace('[profiles.cheap]', '[profiles.cheap2]').replace('cheap-model', 'cheap-model-2')
    path = _write(tmp_path, MAIN + CHEAP + second
                  + '[roles]\nmain = "main"\nroute = "cheap"\nmember = "cheap2"\nexplorer = "cheap"\n')
    profiles = _settings(path).model_profiles()
    async with _service()._base_models('aletheia-k', profiles) as models:
        assert [models.main._model, models.route._model, models.member._model] == [
            'main-model', 'cheap-model', 'cheap-model-2']
        assert models.explorer is models.route
        assert [models.main.profile.name, models.member.profile.name] == ['main', 'cheap2']
        # The cache key goes only to the profile that supports it.
        assert models.main._prompt_cache_key == 'aletheia-k' and models.route._prompt_cache_key is None
        assert len(_Client.opened) == 2                        # cheap and cheap2 share one client
        assert models.member._client is models.route._client
    assert all(client.closed for client in _Client.opened)


async def test_without_a_file_there_is_one_client_and_one_model_for_every_role(monkeypatch):
    import app.services.rag_agent_service as module
    monkeypatch.setattr(module, 'AsyncOpenAI', _Client)
    monkeypatch.setattr(config, 'dashscope_api_key', 'k')
    _Client.opened = []
    async with _service()._base_models() as models:
        assert models.main is models.route is models.member is models.explorer
        assert models.main.profile.name == 'default'
    assert len(_Client.opened) == 1


async def test_hooks_wrap_each_distinct_model_once():
    from app.agent.hooks import EMPTY, HookedModel
    service = _service()
    service.model = SimpleNamespace(generate=None)
    async with service._role_models(hooks=EMPTY) as models:
        assert isinstance(models.main, HookedModel)
        assert models.main is models.route is models.member is models.explorer and models.profiles is None


def test_a_member_role_on_another_profile_gets_a_context_sized_to_it(tmp_path, cheap_key, monkeypatch):
    from app.services.rag_agent_service import RagAgentService, RoleModels
    profiles = _settings(_write(tmp_path, MAIN + CHEAP + ROLES_CHEAP)).model_profiles()
    main, cheap = object(), object()
    models = RoleModels(main, cheap, cheap, main, profiles)
    conversation = SimpleNamespace(archive=None)
    role = RagAgentService._member_role(models, 'member', conversation)
    assert role.model is cheap and role.context_manager.window_tokens == 16000
    assert role.context_manager.output_tokens == profiles.profiles['cheap'].max_output_tokens
    assert RagAgentService._member_role(models, 'explorer', conversation) is None    # on main
    same = RoleModels(main, main, main, main, ModelProfiles.single(profiles.profiles['main']))
    assert RagAgentService._member_role(same, 'member', conversation) is None


# --- the roles at work -------------------------------------------------------------------------

class Scripted:
    def __init__(self, *items):
        self.items, self.requests = list(items), []

    async def generate(self, messages, tools):
        self.requests.append(tuple(messages))
        return self.items.pop(0)


def _call(identity, name, **arguments):
    return ModelTurn(tool_calls=(ToolCall(identity, name, arguments),))


def test_the_spawner_uses_the_role_model_and_its_context_or_the_callers():
    lead, member, lead_cm, member_cm = object(), object(), ContextManager(), ContextManager(window_tokens=16000)
    registry = ToolRegistry()
    spawner = MemberSpawner(lead, registry, lead_cm, RoleModel(member, member_cm))
    assert spawner.model is member and spawner.context_manager is member_cm
    spawner = MemberSpawner(lead, registry, lead_cm, RoleModel(member))
    assert spawner.model is member and spawner.context_manager is lead_cm
    spawner = MemberSpawner(lead, registry, lead_cm)
    assert spawner.model is lead and spawner.context_manager is lead_cm


async def test_team_members_run_on_the_member_model_and_the_lead_on_main():
    from test_delegation import SANDBOX, _registry, delegate
    from test_sandbox_step1 import _context
    lead = Scripted(delegate('d'), ModelTurn('requested answer'))
    member = Scripted(ModelTurn('member notes'))
    runner = DelegatingLoopRunner(lead, _registry(), SANDBOX, member=RoleModel(member))
    output = await runner.run(_context())
    assert output.content == 'requested answer'
    assert len(member.requests) == 1 and 'Assignment:' in member.requests[0][0].content
    assert all('Assignment:' not in request[0].content for request in lead.requests)


async def test_explore_members_run_on_the_explorer_model():
    from loop_fakes import started_context

    async def read(arguments):
        return ToolOutput(True, 'x = 1')
    registry = ToolRegistry()
    registry.register(SimpleNamespace(definition=ToolDefinition('read_file', 'read', {'type': 'object'}), invoke=read))
    main = Scripted(_call('a', 'read_file', path='a.py'),
                    _call('d', DELEGATE, task='Find where x is set.', thoroughness='quick'),
                    ModelTurn('final answer'))
    explorer = Scripted(ModelTurn('x is set in a.py'))
    runner = ExploreLoopRunner(main, registry, member=RoleModel(explorer))
    output = await runner.run(started_context(goal='explain', user='user'))
    assert output.content == 'final answer' and len(main.requests) == 3
    assert len(explorer.requests) == 1 and 'read-only research member' in explorer.requests[0][0].content


async def test_a_resumed_run_assembles_its_runner_with_the_new_bindings_models():
    from app.agent.loop_run import LoopRun
    from app.models.run_state import RunMemory, RunState
    seen = []

    def factory(deps):
        seen.append(deps.member)

        async def run(context):
            raise AssertionError('not run in this test')
        return SimpleNamespace(run=run)
    first, second = RoleModel(object()), RoleModel(object())
    run = LoopRun(RunMemory(RunState('user', 'goal')), object(), ToolRegistry(), runner_factory=factory,
                  step_description=LOOP_STEP, member=first)
    run.rebind(object(), ToolRegistry(), context_manager=ContextManager(), member=second)
    run.rebind(object(), ToolRegistry(), context_manager=ContextManager())
    assert seen == [first, second, None]


async def test_the_router_uses_the_route_model(monkeypatch):
    import app.agent.route_selection as routes
    from test_run_assembler import _Run, _assembler
    used = []

    async def choose(question, team_options, model):
        used.append(model)
        return routes.ModeChoice('single', 'model', 'picked')
    monkeypatch.setattr(routes, 'choose_mode', choose)
    assembler, *_ = _assembler()
    main, router = object(), object()
    member = RoleModel(object())
    runtime, _ = await assembler.run(
        key='k', task=SimpleNamespace(task_id='t1'), question='q', runtime=None,
        options={'mode': 'auto', 'runner': 'loop', 'write': True, 'memory': False}, model=main,
        memory=object(), registry=ToolRegistry(), context_manager=None, audit=None, on_event=None,
        loop_run_factory=_Run, route_model=router, member=member)
    assert used == [router]
    assert runtime.kwargs['member'] is member and 'explorer' not in runtime.kwargs


# --- metering, trace and bench -----------------------------------------------------------------

def _profile(name, input_price=None, output_price=None):
    return ModelProfile(name=name, base_url='https://x.test', api_key_env='X_KEY', model=name,
                        context_tokens=8000, max_output_tokens=500, timeout_seconds=30,
                        input_price_per_million=input_price, output_price_per_million=output_price)


def _usage(prompt, completion, cached=None):
    details = None if cached is None else SimpleNamespace(cached_tokens=cached)
    return SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion, prompt_tokens_details=details)


def test_the_meter_books_each_profile_and_sums_their_costs():
    main, cheap = _profile('main', 2.0, 8.0), _profile('cheap', 1.0, 4.0)
    meter = RunMeter()
    meter.record(_usage(1_000_000, 100_000, 400_000), profile=main)
    meter.record(_usage(500_000, 50_000), profile=cheap)
    meter.record(None, profile=cheap)
    usage = meter.snapshot()
    assert usage['by_profile'] == {
        'main': {'model_calls': 1, 'prompt_tokens': 1_000_000, 'completion_tokens': 100_000,
                 'cached_prompt_tokens': 400_000, 'estimated_cost': 2.8},
        'cheap': {'model_calls': 2, 'prompt_tokens': 500_000, 'completion_tokens': 50_000,
                  'cached_prompt_tokens': 0, 'estimated_cost': 0.7}}
    assert usage['estimated_cost'] == 3.5 and usage['model_calls'] == 3
    assert [c.get('profile') for c in usage['calls']] == ['main', 'cheap', 'cheap']
    meter.record(_usage(10, 10), profile=_profile('free'))
    assert meter.snapshot()['estimated_cost'] is None          # one used profile has no price


def test_calls_without_a_profile_are_booked_as_before(monkeypatch):
    monkeypatch.setattr(config, 'model_input_price_per_million_tokens', 2.0)
    monkeypatch.setattr(config, 'model_output_price_per_million_tokens', 8.0)
    meter = RunMeter()
    meter.record(_usage(1_000_000, 500_000))
    usage = meter.snapshot()
    assert 'by_profile' not in usage and 'profile' not in usage['calls'][0]
    assert usage['estimated_cost'] == 6.0


def test_only_the_default_profile_gives_the_old_cost(monkeypatch):
    from app.agent.run_meter import estimated_cost
    monkeypatch.setattr(config, 'model_input_price_per_million_tokens', 0.3)
    monkeypatch.setattr(config, 'model_output_price_per_million_tokens', 1.7)
    meter = RunMeter()
    for prompt, completion in ((12_345, 678), (23_456, 789), (34_567, 0)):
        meter.record(_usage(prompt, completion), profile=config.agent_model_profile)
    assert meter.snapshot()['estimated_cost'] == estimated_cost(meter.prompt_tokens, meter.completion_tokens)


async def test_the_trace_names_the_profile_of_a_model_call():
    from app.agent.hooks import HookSet, HookedModel, using_hooks
    from app.agent.step_models import UserMessage
    from app.agent.trace_view import _line
    events = []

    class Fake:
        async def generate(self, messages, tools):
            record_model_call(_usage(100, 5), None, 'm', 100, 'm', profile=_profile('cheap'))
            return ModelTurn('ok')

    class Plain:
        async def generate(self, messages, tools):
            record_model_call(_usage(100, 5), None, 'm', 100, 'm')
            return ModelTurn('ok')

    with metering(RunMeter()), using_hooks(HookSet((SimpleNamespace(observe=events.append),), ())):
        await HookedModel(Fake()).generate((UserMessage('x'),), ())
        await HookedModel(Plain()).generate((UserMessage('x'),), ())
    first, second = [e for e in events if e['type'] == 'after_model']
    assert first['profile'] == 'cheap' and 'profile' not in second
    span = {'kind': 'model', 'label': '1', 'status': 'ok', 'seconds': 1.0, 'attrs': {'profile': 'cheap'}}
    assert '档案 cheap' in _line(span)


def test_bench_rows_keep_the_profile_of_each_call():
    from bench.run_bench import usage_fields
    calls = [{'prompt': 10, 'completion': 1, 'profile': 'main'}, {'prompt': 5, 'completion': 1, 'profile': 'cheap'}]
    two = {'main': {'model_calls': 1}, 'cheap': {'model_calls': 1}}
    fields = usage_fields({'usage': {'calls': calls, 'by_profile': two}})
    assert [c['profile'] for c in fields['calls']] == ['main', 'cheap'] and fields['by_profile'] == two
    one = usage_fields({'usage': {'calls': calls[:1], 'by_profile': {'main': {'model_calls': 1}}}})
    assert 'by_profile' not in one


# --- start-up ----------------------------------------------------------------------------------

def test_the_command_line_refuses_a_bad_file_and_shows_the_roles(tmp_path, monkeypatch, capsys):
    from app.agent import __main__ as cli
    import app.services.rag_agent_service as service
    monkeypatch.setattr(service, 'RagAgentService', None)      # building a service would fail loudly
    args = cli.parse_args(['--show-limits'])
    assert asyncio.run(cli.main(args)) == 0
    out = capsys.readouterr().out
    assert '模型档案（角色 / 档案 / 模型 / 窗口）' in out and 'member    default' in out
    monkeypatch.setattr(config, 'model_profiles_file', str(tmp_path / 'missing.toml'))
    assert cli.profile_problem().startswith('找不到模型档案文件')
    assert asyncio.run(cli.main(args)) == 0
    assert 'ModelProfileError: 找不到模型档案文件' in capsys.readouterr().out
    run = cli.parse_args(['goal', '--filesystem-root', str(tmp_path), '--no-knowledge',
                          '--confirm-system-drive'])     # tmp_path is on the system drive on Windows (D-074)
    assert asyncio.run(cli.main(run)) == 2
    assert 'ModelProfileError' in capsys.readouterr().out


async def test_the_assembled_runners_hand_the_member_roles_on():
    from app.agent.delegation import DelegationOptions
    from app.agent.step_runner import RunnerDependencies
    from test_run_assembler import _assembler, _run
    member, explorer = RoleModel(object()), RoleModel(object(), ContextManager(window_tokens=16000))
    main = SimpleNamespace(generate=lambda *a: None)
    deps = RunnerDependencies(main, ToolRegistry(), ContextManager(), member, explorer)
    assembler, *_ = _assembler(team=DelegationOptions())
    runtime, _ = await _run(assembler, mode='team')
    assert runtime.kwargs['runner_factory'](deps).member is member
    assembler, *_ = _assembler(delegate=True)
    runtime, _ = await _run(assembler)
    spawner = runtime.kwargs['runner_factory'](deps).explore.spawner
    assert spawner.model is explorer.model and spawner.context_manager is explorer.context_manager
