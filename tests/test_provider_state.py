"""provider-state-001: what the provider did to a request, from its usage report (docs/provider_state_plan.md)."""

import json
from types import SimpleNamespace

import httpx
import pytest
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion

from app.agent import provider_state as ps
from app.agent.openai_compatible_tool_calling_model import OpenAICompatibleToolCallingModel
from app.agent.run_meter import RunMeter, metering
from app.agent.step_models import ToolDefinition, UserMessage
from app.services.rag_context_builder import ContextBuilder


@pytest.mark.parametrize('value, state', [
    (None, 'unknown'), (0.0, 'dropped'), (0.79, 'dropped'), (0.8, 'plain'), (1.49, 'plain'),
    (1.5, 'padded'), (3.99, 'padded'), (4.0, 'heavy'), (7.2, 'heavy')])
def test_bands(value, state):
    assert ps.band(value) == state


def test_ratio_needs_both_numbers():
    assert ps.ratio(20160, 2827) == 7.13
    assert ps.ratio(None, 2827) is None and ps.ratio(100, None) is None and ps.ratio(100, 0) is None
    assert ps.ratio(True, 10) is None and ps.ratio(-1, 10) is None


@pytest.mark.parametrize('asked, got, different', [
    ('gpt-5.6-luna', 'gpt-6.1-sol', True), ('gpt-4o', 'gpt-4o-2024-08-06', False), ('qwen-max', 'qwen-max', False),
    ('GPT-4o', 'gpt-4o', False), ('deepseek-chat', 'deepseek/deepseek-chat', False), ('m', None, False),
    # Documented limit: gpt-4o-mini starts with "gpt-4o-", so it reads as a version of gpt-4o.
    (None, 'm', False), ('gpt-4o', 'gpt-4o-mini', False)])
def test_model_names(asked, got, different):
    # A dated or provider-prefixed name of the same model is not a mismatch.
    assert ps.different_model(asked, got) is different


def test_summary_uses_the_first_call_and_flags_drops_and_mismatches():
    s = ps.summary([7.13, 7.3], ['gpt-5.6-luna'] * 2, ['gpt-5.6-luna'] * 2)
    assert (s['state'], s['states'], s['anomaly'], s['notice']) == ('heavy', ['heavy'], False, None)
    s = ps.summary([2.36, 1.3], ['m', 'm'], ['m', 'm'])
    assert (s['state'], s['states'], s['anomaly']) == ('padded', ['padded', 'plain'], False)
    s = ps.summary([0.55], ['m'], ['m'])
    assert s['anomaly'] and '少于本机发出的内容' in s['notice'] and '模型' not in s['notice']
    s = ps.summary([7.1], ['gpt-5.6-luna'], ['gpt-6.1-sol'])
    assert s['model_mismatch'] and '模型和请求的不同' in s['notice']
    empty = ps.summary([], [], [])
    assert (empty['state'], empty['anomaly'], empty['notice']) == ('unknown', False, None)


def test_the_meter_reports_the_provider_state_beside_the_old_fields():
    meter = RunMeter()
    meter.record(SimpleNamespace(prompt_tokens=20160, completion_tokens=50), None, 'gpt-6.1-sol', 2827, 'gpt-5.6-luna')
    meter.record(None, 1.0, None, 3000, 'gpt-5.6-luna')                  # failed call: no usage
    snap = meter.snapshot()
    assert snap['calls'][0] == {'prompt': 20160, 'cached': None, 'completion': 50, 'shared_prefix': None}
    assert snap['provider']['ratios'] == [7.13, None]
    assert snap['provider']['states'] == ['heavy', 'unknown'] and snap['provider']['model_mismatch'] is True
    assert RunMeter().snapshot()['provider']['state'] == 'unknown'
    # Old callers that pass no estimate still work.
    meter = RunMeter()
    meter.record(SimpleNamespace(prompt_tokens=10, completion_tokens=1))
    assert meter.snapshot()['provider']['state'] == 'unknown'


async def test_the_adapter_records_its_own_estimate_and_the_requested_model():
    def respond(request):
        return httpx.Response(200, json=ChatCompletion(
            id='t', created=0, model='gpt-6.1-sol', object='chat.completion',
            choices=[{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'ok'}}],
            usage={'prompt_tokens': 900, 'completion_tokens': 1, 'total_tokens': 901},
        ).model_dump())

    client = AsyncOpenAI(api_key='k', base_url='https://relay.test/v1', max_retries=0,
                         http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    model = OpenAICompatibleToolCallingModel(client, 'gpt-5.6-luna')
    tools = (ToolDefinition('read_file', 'Read one file.', {'type': 'object'}),)
    meter = RunMeter()
    with metering(meter):
        await model.generate((UserMessage('hi ' * 200),), tools)
    request = {'tools': OpenAICompatibleToolCallingModel._build_tool_payload(tools),
               'messages': OpenAICompatibleToolCallingModel._build_message_payload((UserMessage('hi ' * 200),))}
    expected = ContextBuilder.estimate_tokens(json.dumps([request['tools'], request['messages']],
                                                         ensure_ascii=False, default=str))
    assert meter.host_estimates == [expected] and meter.requested_models == ['gpt-5.6-luna']
    assert meter.snapshot()['provider']['model_mismatch'] is True


async def test_model_events_and_the_trace_view_show_the_state():
    from app.agent.hooks import HookSet, HookedModel, using_hooks
    from app.agent.trace_view import _line

    events = []

    class Recorder:
        def observe(self, event):
            events.append(event)

    class Fake:
        async def generate(self, messages, tools):
            from app.agent.run_meter import record_model_call
            record_model_call(SimpleNamespace(prompt_tokens=1500, completion_tokens=5), None, 'm', 2500, 'm')
            from app.agent.step_models import ModelTurn
            return ModelTurn('ok')

    with metering(RunMeter()), using_hooks(HookSet((Recorder(),), ())):
        await HookedModel(Fake()).generate((UserMessage('x'),), ())
    [after] = [e for e in events if e['type'] == 'after_model']
    assert (after['host_estimate'], after['provider_ratio'], after['provider_state']) == (2500, 0.6, 'dropped')
    span = {'kind': 'model', 'label': '1', 'status': 'ok', 'seconds': 1.0,
            'attrs': {k: after[k] for k in ('prompt_tokens', 'host_estimate', 'provider_ratio', 'provider_state')}}
    assert '本机估算 2,500，报告/估算 0.6（内容可能被丢弃）' in _line(span)


def test_the_command_line_warns_before_the_status_line_only_on_an_anomaly():
    from app.agent.__main__ import report
    from app.models.taskstate import TaskStatus

    def lines(provider):
        out = []
        result = SimpleNamespace(content='answer', user_message=None, status=TaskStatus.COMPLETED,
                                 execution_count=1, replan_count=0, usage={'provider': provider})
        report(result, out.append)
        return out
    plain = lines(ps.summary([7.1], ['m'], ['m']))
    assert not any('注意' in line for line in plain) and plain[-1].startswith('\nStatus:')
    warned = lines(ps.summary([0.5], ['m'], ['m']))
    assert '注意' in warned[-2] and warned[-1].startswith('\nStatus:')


def test_the_bench_records_and_groups_by_state():
    from bench.run_bench import state_summary, usage_fields
    record = {'usage': {'model_calls': 1, 'provider': ps.summary([2.36], ['a'], ['b'])}}
    fields = usage_fields(record)
    assert (fields['provider_state'], fields['provider_states'], fields['provider_ratios'],
            fields['model_mismatch']) == ('padded', ['padded'], [2.36], True)
    assert usage_fields({'usage': {'provider': {'state': 'odd', 'ratios': ['x']}}})['provider_state'] == 'unknown'
    rows = [{'task': 't1', 'mode': 'single', 'passed': True, 'provider_state': 'heavy'},
            {'task': 't2', 'mode': 'single', 'passed': False, 'provider_state': 'padded'},
            {'task': 't3', 'mode': 'single', 'passed': True}]
    table = state_summary(rows)
    assert table['heavy/single']['passed'] == 1 and table['padded/single']['passed'] == 0
    assert table['unknown/single']['tasks'] == ['t3']
