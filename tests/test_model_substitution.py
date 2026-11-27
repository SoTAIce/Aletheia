"""D-107: accepted relay models, explicit mismatch notices, stop-on-substitution, and tool-args reminder."""

from types import SimpleNamespace

import httpx
import pytest
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion

from app.agent import provider_state as ps
from app.agent.agent_loop import AgentLoopRunner, LoopOptions
from app.agent.loop_run import LoopRun
from app.agent.openai_compatible_tool_calling_model import OpenAICompatibleToolCallingModel, profile_model
from app.agent.run_limits import RunLimits
from app.agent.run_meter import RunMeter, metering
from app.agent.runtime import FAILURE_MESSAGES
from app.agent.step_models import ModelTurn, SystemMessage, ToolCall, ToolDefinition, ToolOutput, UserMessage
from app.agent.tool_calling_model import ModelSubstitutedError
from app.config import Settings
from app.models.model_profile import ModelProfile, ModelProfileError, load_profiles
from app.models.run_state import RunMemory, RunState
from app.models.taskstate import TaskStatus
from app.tools.current_time_tool import CurrentTimeTool
from app.tools.tool_registry import ToolRegistry


def _settings(**kw):
    fields = {'dashscope_api_key': 'ds-key', 'dashscope_base_url': 'https://dashscope.test/v1',
              'agent_model_base_url': 'https://relay.test/v1', 'agent_model_api_key': 'relay-key',
              'agent_model': 'gpt-5.6-luna', 'rag_model': 'qwen-max', 'model_profiles_file': '', **kw}
    return Settings(_env_file=None, **fields)


def _profile(**kw):
    base = dict(name='main', base_url='https://relay.test/v1', api_key_env='AGENT_MODEL_API_KEY',
                model='gpt-5.6-luna', context_tokens=128000, max_output_tokens=4096, timeout_seconds=30)
    return ModelProfile(**{**base, **kw})


def _memory(goal: str) -> RunMemory:
    return RunMemory(RunState('test-user', goal))


# ---- 1. Profile & config validation ----

def test_profile_accepts_and_normalizes_substitution_fields():
    p = _profile(accepted_models=[' gpt-6.1-sol ', 'qwen-plus'], on_model_mismatch='stop')
    assert p.accepted_models == ('gpt-6.1-sol', 'qwen-plus')
    assert p.on_model_mismatch == 'stop'


@pytest.mark.parametrize('kw', [
    {'on_model_mismatch': 'ignore'},
    {'on_model_mismatch': 'STOP'},
    {'on_model_mismatch': 1},
    {'accepted_models': 'gpt-6.1-sol'},
    {'accepted_models': ('',)},
    {'accepted_models': ('   ',)},
    {'accepted_models': (123,)},
    {'accepted_models': tuple(f'm{i}' for i in range(21))},
    {'accepted_models': ('x' * 129,)},
])
def test_profile_rejects_invalid_substitution_fields(kw):
    with pytest.raises((TypeError, ValueError)):
        _profile(**kw)


def test_file_profiles_do_not_inherit_substitution_fields(tmp_path):
    default = _profile(name='default', accepted_models=('gpt-6.1-sol',), on_model_mismatch='stop')
    toml_path = tmp_path / 'models.toml'
    toml_path.write_text(
        '[profiles.other]\n'
        'base_url = "https://other.test/v1"\n'
        'api_key_env = "AGENT_MODEL_API_KEY"\n'
        'model = "other-model"\n'
        '\n'
        '[profiles.strict]\n'
        'base_url = "https://strict.test/v1"\n'
        'api_key_env = "AGENT_MODEL_API_KEY"\n'
        'model = "strict-model"\n'
        'accepted_models = [" alt-1 ", "alt-2"]\n'
        'on_model_mismatch = "stop"\n'
        '\n'
        '[roles]\n'
        'main = "other"\n'
        'route = "strict"\n',
        encoding='utf-8',
    )
    loaded = load_profiles(toml_path, default, _settings())
    assert loaded.role('main').accepted_models == ()
    assert loaded.role('main').on_model_mismatch == 'warn'
    assert loaded.role('route').accepted_models == ('alt-1', 'alt-2')
    assert loaded.role('route').on_model_mismatch == 'stop'


def test_file_profile_rejects_invalid_substitution_fields(tmp_path):
    default = _profile(name='default')
    bad = tmp_path / 'bad.toml'
    bad.write_text(
        '[profiles.main]\n'
        'base_url = "https://relay.test/v1"\n'
        'api_key_env = "AGENT_MODEL_API_KEY"\n'
        'model = "m"\n'
        'on_model_mismatch = "boom"\n'
        '[roles]\n'
        'main = "main"\n',
        encoding='utf-8',
    )
    with pytest.raises(ModelProfileError):
        load_profiles(bad, default, _settings())


def test_settings_build_default_profile_substitution_fields():
    s = _settings(agent_accepted_models=' gpt-6.1-sol , , alt-model ', agent_on_model_mismatch='stop')
    p = s.agent_model_profile
    assert p.accepted_models == ('gpt-6.1-sol', 'alt-model')
    assert p.on_model_mismatch == 'stop'
    with pytest.raises(ValueError):
        _settings(agent_on_model_mismatch='bad')
    with pytest.raises(ValueError):
        _settings(agent_accepted_models=','.join(f'm{i}' for i in range(21)))
    with pytest.raises(ValueError):
        _settings(agent_accepted_models='x' * 129)


# ---- 2. Comparison, summary, notices, and meter ----

@pytest.mark.parametrize('asked, got, accepted, expected', [
    ('gpt-5.6-luna', 'gpt-6.1-sol', (), True),
    ('gpt-5.6-luna', 'gpt-6.1-sol', ('gpt-6.1-sol',), False),
    ('gpt-5.6-luna', 'gpt-6.1-sol-2026-01', ('gpt-6.1-sol',), False),
    ('gpt-5.6-luna', 'relay/gpt-6.1-sol', ('gpt-6.1-sol',), False),
    ('gpt-5.6-luna', 'other-model', ('gpt-6.1-sol',), True),
    ('gpt-5.6-luna', 'gpt-5.6-luna-2026', (), False),
    ('gpt-5.6-luna', None, (), False),
    (None, 'gpt-6.1-sol', (), False),
    ('gpt-5.6-luna', 'gpt-6.1-sol', ('',), True),
])
def test_substituted(asked, got, accepted, expected):
    assert ps.substituted(asked, got, accepted) is expected


def test_summary_and_notices_for_accepted_missing_long_and_many_names():
    # Accepted response model: quiet.
    ok = ps.summary([2.0], ['gpt-5.6-luna'], ['gpt-6.1-sol'], accepted=[('gpt-6.1-sol',)])
    assert ok['model_mismatch'] is False
    assert ok['response_names'] == [] and ok['requested_names'] == []
    assert ok['anomaly'] is False and ok['notice'] is None

    # Unaccepted response model: notice names both.
    sub = ps.summary([2.0], ['gpt-5.6-luna'], ['gpt-6.1-sol'])
    assert sub['model_mismatch'] is True
    assert sub['requested_names'] == ['gpt-5.6-luna']
    assert sub['response_names'] == ['gpt-6.1-sol']
    assert sub['notice'] == '注意：请求的是 gpt-5.6-luna，服务商用 gpt-6.1-sol 回答，本次结果可能不可靠。'

    # Combined dropped + substitution keeps semicolon separator.
    both = ps.summary([0.5], ['gpt-5.6-luna'], ['gpt-6.1-sol'])
    assert both['notice'] == (
        '注意：服务商报告的提示令牌少于本机发出的内容，部分请求可能被丢弃；'
        '请求的是 gpt-5.6-luna，服务商用 gpt-6.1-sol 回答，本次结果可能不可靠。'
    )

    # Missing names fall back to the legacy sentence.
    fallback = ps.notice({'anomaly': True, 'model_mismatch': True})
    assert fallback == '注意：服务商回答所用的模型和请求的不同，本次结果可能不可靠。'

    # Long names are truncated to 60 characters.
    long_req = 'r' * 80
    long_resp = 's' * 80
    long_sum = ps.summary([1.0], [long_req], [long_resp])
    assert long_sum['requested_names'] == ['r' * 60]
    assert long_sum['response_names'] == ['s' * 60]
    assert f'请求的是 {"r" * 60}，服务商用 {"s" * 60} 回答' in long_sum['notice']

    # More than 3 distinct names: deduplicated in order, capped at 3.
    many = ps.summary(
        [1.0] * 5,
        ['a', 'a', 'b', 'c', 'd'],
        ['x1', 'x1', 'x2', 'x3', 'x4'],
    )
    assert many['requested_names'] == ['a', 'b', 'c']
    assert many['response_names'] == ['x1', 'x2', 'x3']
    assert '请求的是 a、b、c，服务商用 x1、x2、x3 回答' in many['notice']


def test_meter_tracks_accepted_models_per_profile():
    meter = RunMeter()
    p_accept = _profile(accepted_models=('gpt-6.1-sol',))
    p_empty = _profile(accepted_models=())
    meter.record(SimpleNamespace(prompt_tokens=100, completion_tokens=10), None, 'gpt-6.1-sol', 100,
                 'gpt-5.6-luna', profile=p_accept)
    snap = meter.snapshot()
    assert snap['provider']['model_mismatch'] is False
    assert snap['provider']['response_names'] == []

    meter.record(SimpleNamespace(prompt_tokens=100, completion_tokens=10), None, 'gpt-6.1-sol', 100,
                 'gpt-5.6-luna', profile=p_empty)
    snap2 = meter.snapshot()
    assert snap2['provider']['model_mismatch'] is True
    assert snap2['provider']['response_names'] == ['gpt-6.1-sol']


# ---- 3. Stop on unaccepted model ----

def _fake_client(payload_fn):
    return AsyncOpenAI(
        api_key='relay-key',
        base_url='https://relay.test/v1',
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(payload_fn)),
    )


async def test_stop_on_substitution_raises_without_retry_or_tool_execution():
    http_calls = 0
    tool_ran = False

    def respond(request):
        nonlocal http_calls
        http_calls += 1
        return httpx.Response(200, json=ChatCompletion(
            id='c1', created=0, model='gpt-6.1-sol', object='chat.completion',
            choices=[{
                'index': 0,
                'finish_reason': 'tool_calls',
                'message': {
                    'role': 'assistant',
                    'content': None,
                    'tool_calls': [{
                        'id': 'call_1',
                        'type': 'function',
                        'function': {'name': 'probe', 'arguments': '{"x": 1}'},
                    }],
                },
            }],
            usage={'prompt_tokens': 500, 'completion_tokens': 20, 'total_tokens': 520},
        ).model_dump())

    class ProbeTool:
        @property
        def definition(self):
            return ToolDefinition('probe', 'Probe tool.', {'type': 'object', 'properties': {'x': {'type': 'integer'}}})

        async def invoke(self, arguments):
            nonlocal tool_ran
            tool_ran = True
            return ToolOutput(True, 'ran')

    profile = _profile(on_model_mismatch='stop')
    async with _fake_client(respond) as client:
        model = profile_model(client, profile)
        model._max_retries = 2
        meter = RunMeter()
        with metering(meter):
            with pytest.raises(ModelSubstitutedError) as exc_info:
                await model.generate((UserMessage('hi'),), ())
        assert str(exc_info.value) == 'provider answered with another model'
        assert getattr(exc_info.value, 'retryable', False) is False
        assert http_calls == 1
        assert meter.model_calls == 1
        assert meter.prompt_tokens == 500

        # End-to-end through LoopRun + AgentLoopRunner: tool never runs, failure_kind is model_substituted.
        http_calls = 0
        registry = ToolRegistry()
        registry.register(ProbeTool())
        memory = _memory('test stop')
        loop_run = LoopRun(
            memory,
            model,
            registry,
            runner_factory=lambda deps: AgentLoopRunner(deps.model, deps.tool_registry, LoopOptions()),
            step_description='run',
        )
        result = await loop_run.run()
        assert result.status is TaskStatus.FAILED
        assert result.failure_kind == 'model_substituted'
        assert result.user_message == FAILURE_MESSAGES['model_substituted']
        assert result.usage['model_calls'] == 1
        assert result.usage['prompt_tokens'] == 500
        assert http_calls == 1
        assert tool_ran is False


async def test_stop_allows_accepted_model_and_missing_model_name():
    def respond_accepted(request):
        return httpx.Response(200, json=ChatCompletion(
            id='c1', created=0, model='gpt-6.1-sol', object='chat.completion',
            choices=[{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'done'}}],
            usage={'prompt_tokens': 100, 'completion_tokens': 5, 'total_tokens': 105},
        ).model_dump())

    profile = _profile(accepted_models=('gpt-6.1-sol',), on_model_mismatch='stop')
    async with _fake_client(respond_accepted) as client:
        model = profile_model(client, profile)
        memory = _memory('accepted')
        loop_run = LoopRun(
            memory, model, ToolRegistry(),
            runner_factory=lambda deps: AgentLoopRunner(deps.model, deps.tool_registry, LoopOptions()),
            step_description='run',
        )
        result = await loop_run.run()
        assert result.status is TaskStatus.COMPLETED
        assert result.content == 'done'
        assert result.usage['provider']['model_mismatch'] is False

    # Missing/empty response model name does not stop.
    class FakeRawClient:
        def __init__(self):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

        async def _create(self, **kwargs):
            return SimpleNamespace(
                model=None,
                choices=[SimpleNamespace(
                    finish_reason='stop',
                    message=SimpleNamespace(role='assistant', content='ok', refusal=None,
                                            function_call=None, tool_calls=None),
                )],
                usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2),
            )

    strict_profile = _profile(accepted_models=(), on_model_mismatch='stop')
    model_no_name = profile_model(FakeRawClient(), strict_profile)
    turn = await model_no_name.generate((UserMessage('hi'),), ())
    assert turn.content == 'ok'


# ---- 4. Byte-for-byte request invariance ----

async def test_substitution_fields_do_not_change_request_bytes():
    async def capture(profile):
        seen = []

        def respond(request):
            seen.append((str(request.url), request.headers['authorization'], request.content))
            return httpx.Response(200, json=ChatCompletion(
                id='t', created=0, model='gpt-5.6-luna', object='chat.completion',
                choices=[{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'ok'}}],
            ).model_dump())

        async with _fake_client(respond) as client:
            await profile_model(client, profile, 'cache-key').generate((UserMessage('hi'),), ())
        return seen

    plain = _profile(prompt_cache_key=True)
    configured = _profile(prompt_cache_key=True, accepted_models=('gpt-6.1-sol',), on_model_mismatch='stop')
    assert await capture(configured) == await capture(plain)


# ---- 5. Tool arguments as plain-text answer (3.4) ----

class _ScriptedModel:
    def __init__(self, turns):
        self._turns = list(turns)
        self.seen_messages = []

    async def generate(self, messages, tools):
        self.seen_messages.append(messages)
        return self._turns.pop(0)


async def test_tool_args_as_answer_reminds_once_then_calls_tool():
    registry = ToolRegistry()
    registry.register(CurrentTimeTool())
    model = _ScriptedModel([
        ModelTurn('{"timezone":"UTC"}'),
        ModelTurn(None, (ToolCall('c1', 'get_current_time', {'timezone': 'UTC'}),)),
        ModelTurn('The UTC time is now known.'),
    ])
    runner = AgentLoopRunner(model, registry, LoopOptions())
    memory = _memory('what time is it in UTC')
    loop_run = LoopRun(memory, model, registry, runner_factory=lambda deps: runner, step_description='time')
    result = await loop_run.run()
    assert result.status is TaskStatus.COMPLETED
    assert result.content == 'The UTC time is now known.'
    assert runner.format_repair_count == 0
    assert len(model.seen_messages) == 3
    reminder = model.seen_messages[1][-1]
    assert isinstance(reminder, SystemMessage)
    assert reminder.content == (
        'HOST: your reply looks like the arguments of tool get_current_time. '
        'Call the tool, or reply with the final answer.'
    )


async def test_tool_args_as_answer_twice_accepts_second():
    registry = ToolRegistry()
    registry.register(CurrentTimeTool())
    model = _ScriptedModel([
        ModelTurn('{"timezone":"UTC"}'),
        ModelTurn('{"timezone":"UTC"}'),
    ])
    runner = AgentLoopRunner(model, registry, LoopOptions())
    memory = _memory('return json')
    loop_run = LoopRun(memory, model, registry, runner_factory=lambda deps: runner, step_description='json')
    result = await loop_run.run()
    assert result.status is TaskStatus.COMPLETED
    assert result.content == '{"timezone":"UTC"}'
    assert runner.format_repair_count == 0
    assert len(model.seen_messages) == 2


async def test_tool_args_as_answer_does_not_trigger_on_extra_key_or_no_tools_or_control_only():
    # Extra key not in tool properties -> no reminder.
    registry = ToolRegistry()
    registry.register(CurrentTimeTool())
    model = _ScriptedModel([ModelTurn('{"timezone":"UTC","note":"x"}')])
    runner = AgentLoopRunner(model, registry, LoopOptions())
    res = await LoopRun(_memory('extra'), model, registry,
                        runner_factory=lambda deps: runner, step_description='extra').run()
    assert res.status is TaskStatus.COMPLETED
    assert res.content == '{"timezone":"UTC","note":"x"}'
    assert len(model.seen_messages) == 1

    # No workspace tools (only control tools todo / ask_user): neither control args nor other JSON triggers.
    empty_reg = ToolRegistry()
    model2 = _ScriptedModel([ModelTurn('{"question":"which one?"}')])
    runner2 = AgentLoopRunner(model2, empty_reg, LoopOptions(), allow_ask_user=True)
    res2 = await LoopRun(_memory('ctrl'), model2, empty_reg,
                         runner_factory=lambda deps: runner2, step_description='ctrl').run()
    assert res2.status is TaskStatus.COMPLETED
    assert res2.content == '{"question":"which one?"}'
    assert len(model2.seen_messages) == 1

    model3 = _ScriptedModel([ModelTurn('{"items":[]}')])
    runner3 = AgentLoopRunner(model3, empty_reg, LoopOptions(), allow_ask_user=False)
    res3 = await LoopRun(_memory('todo-only'), model3, empty_reg,
                         runner_factory=lambda deps: runner3, step_description='todo-only').run()
    assert res3.status is TaskStatus.COMPLETED
    assert res3.content == '{"items":[]}'
    assert len(model3.seen_messages) == 1


# ---- 6. --show-limits & bench ----

def test_run_limits_shows_substitution_settings():
    default_rows = dict((name, value) for _, name, value in RunLimits.current(settings=_settings()).rows())
    assert default_rows['认可的替换模型'] == '无'
    assert default_rows['换模型时'] == '提示'

    custom = _settings(agent_accepted_models='gpt-6.1-sol, qwen-plus', agent_on_model_mismatch='stop')
    custom_rows = dict((name, value) for _, name, value in RunLimits.current(settings=custom).rows())
    assert custom_rows['认可的替换模型'] == 'gpt-6.1-sol, qwen-plus'
    assert custom_rows['换模型时'] == '停止'


def test_bench_records_and_counts_response_names():
    from bench.run_bench import response_model_counts, usage_fields

    record = {'usage': {'model_calls': 1, 'provider': ps.summary([2.0], ['gpt-5.6-luna'], ['gpt-6.1-sol'])}}
    fields = usage_fields(record)
    assert fields['response_names'] == ['gpt-6.1-sol']

    quiet = {'usage': {'model_calls': 1, 'provider': ps.summary([2.0], ['gpt-5.6-luna'], ['gpt-6.1-sol'],
                                                                 accepted=[('gpt-6.1-sol',)])}}
    assert usage_fields(quiet)['response_names'] == []

    rows = [
        {'response_names': ['gpt-6.1-sol']},
        {'response_names': ['gpt-6.1-sol', 'alt-model']},
        {'response_names': []},
        {},
    ]
    assert response_model_counts(rows) == {'gpt-6.1-sol': 2, 'alt-model': 1}
