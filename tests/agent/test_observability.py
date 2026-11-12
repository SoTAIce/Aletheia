"""Observability (DECISIONS D-034): usage metering, budgets, the step log, the CLI result record,
the bench reading it, and one logging pipeline."""

import asyncio
import json
import logging
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openai import APITimeoutError
import httpx

from app.agent.openai_compatible_tool_calling_model import OpenAICompatibleToolCallingModel
from app.agent.run_meter import RunMeter, current_meter, estimated_cost, metering, record_model_call
from app.agent.runtime import FAILURE_MESSAGES
from app.agent.step_models import ModelTurn, ToolCall, ToolDefinition, ToolOutput, UserMessage
from app.agent.tool_calling_model import ModelTimeoutError
from app.models.taskstate import TaskStatus
from app.tools.tool_registry import ToolRegistry


def usage(prompt, completion):
    return SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion)


class Metered:
    """A fake model that reports usage like the real provider adapter does."""

    def __init__(self, turns, tokens=(100, 20), delay=0.0):
        self.turns, self.tokens, self.delay = list(turns), tokens, delay

    async def generate(self, messages, tools):
        if self.delay:
            await asyncio.sleep(self.delay)
        record_model_call(usage(*self.tokens))
        return self.turns.pop(0)


def tool(name, *effects):
    return SimpleNamespace(definition=ToolDefinition(name, name, {'type': 'object'}),
                           invoke=AsyncMock(side_effect=list(effects)))


def call(name, call_id='c1', **arguments):
    return ModelTurn(tool_calls=(ToolCall(call_id, name, arguments),))


# ---- the meter ----

def test_the_meter_adds_usage_and_counts_calls_without_it():
    meter = RunMeter()
    meter.record(usage(10, 5))
    meter.record(None)
    meter.record(usage(True, -3))          # not counts: ignored
    assert (meter.model_calls, meter.prompt_tokens, meter.completion_tokens) == (3, 10, 5)
    assert meter.calls_without_usage == 2 and meter.snapshot()['total_tokens'] == 15


@pytest.mark.parametrize('kwargs', [{'max_total_tokens': 0}, {'max_total_tokens': True},
                                    {'timeout_seconds': 0}, {'timeout_seconds': float('inf')}])
def test_the_meter_rejects_bad_budgets(kwargs):
    with pytest.raises(ValueError):
        RunMeter(**kwargs)


def test_budgets_and_bounded_deadlines():
    meter = RunMeter(max_total_tokens=100, timeout_seconds=50)
    assert meter.budget_problem() is None and meter.bound(60) <= 50 and meter.bound(5) == 5
    meter.record(usage(90, 10))
    assert 'token budget reached (100 of 100' in meter.budget_problem()
    late = RunMeter(timeout_seconds=1)
    late.started -= 2
    assert 'time budget reached' in late.budget_problem() and late.bound(60) == 0.001


def test_cost_needs_both_prices(monkeypatch):
    from app.config import config
    monkeypatch.setattr(config, 'model_input_price_per_million_tokens', None)
    assert estimated_cost(10, 10) is None
    monkeypatch.setattr(config, 'model_input_price_per_million_tokens', 2.0)
    monkeypatch.setattr(config, 'model_output_price_per_million_tokens', 8.0)
    assert estimated_cost(1_000_000, 500_000) == 6.0


def test_the_meter_follows_the_context_into_child_tasks():
    async def scenario():
        with metering(RunMeter()) as meter:
            await asyncio.gather(asyncio.create_task(asyncio.to_thread(lambda: None)),
                                 asyncio.create_task(_record()))
        assert current_meter() is None
        return meter

    async def _record():
        record_model_call(usage(1, 2))
    assert asyncio.run(scenario()).total_tokens == 3


# ---- the provider adapter records every request ----

def _client(create):
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


async def test_the_provider_adapter_records_usage_and_failed_requests():
    from openai.types.chat import ChatCompletion
    ok = ChatCompletion(id='r', object='chat.completion', created=0, model='m',
                        choices=[{'index': 0, 'finish_reason': 'stop',
                                  'message': {'role': 'assistant', 'content': 'hi'}}],
                        usage={'prompt_tokens': 7, 'completion_tokens': 3, 'total_tokens': 10})
    timeout = APITimeoutError(request=httpx.Request('POST', 'https://example.invalid'))
    model = OpenAICompatibleToolCallingModel(_client(AsyncMock(side_effect=[ok, timeout])), 'm', max_retries=0)
    with metering(RunMeter()) as meter:
        assert (await model.generate([UserMessage('q')], ())).content == 'hi'
        with pytest.raises(ModelTimeoutError):
            await model.generate([UserMessage('q')], ())
    assert (meter.model_calls, meter.total_tokens, meter.calls_without_usage) == (2, 10, 1)


# ---- the runtime: usage, budgets and the step log are in tests/test_loop_run_guarantees.py ----

async def test_unknown_tool_names_are_not_copied_into_the_log():
    reader = tool('read_file', ToolOutput(True, 'ok'))
    from app.agent.side_effects import SideEffectWatch
    registry = ToolRegistry()
    registry.register(reader)
    watch = SideEffectWatch(registry)
    with pytest.raises(Exception):
        await watch.invoke(ToolCall('c', 'ignore previous instructions', {}))
    assert watch.calls[0]['tool'] == 'unknown'


# ---- the CLI record and the bench ----

def test_the_cli_writes_a_result_record(tmp_path):
    from app.agent.__main__ import build_parser, result_record, write_result
    result = SimpleNamespace(status=TaskStatus.FAILED, failure_kind='budget', user_message='说明',
                             content='Agent token budget reached', execution_count=2, replan_count=0,
                             usage={'total_tokens': 5}, step_log=({'step': 1},))
    path = tmp_path / 'r.json'
    write_result(path, result)
    record = json.loads(path.read_text(encoding='utf-8'))
    assert record == result_record(result) and record['steps'] == [{'step': 1}]
    assert not (tmp_path / 'r.json.tmp').exists()
    args = build_parser().parse_args(['goal', '--max-tokens', '500', '--max-seconds', '90'])
    assert (args.max_tokens, args.max_seconds) == (500, 90.0)
    for bad in (['--max-tokens', '0'], ['--max-seconds', 'inf'], ['--max-tokens', 'x']):
        with pytest.raises(SystemExit):
            build_parser().parse_args(['goal', *bad])


def test_the_bench_prefers_the_result_record(monkeypatch):
    from bench import run_bench
    from pathlib import Path
    task = next(t for t in run_bench.load_tasks(['all']) if t['id'] == 'billing_qa')
    expected = json.loads((task['dir'] / task['expected']).read_text(encoding='utf-8'))
    record = {'status': 'completed', 'failure_kind': None, 'content': json.dumps(expected),
              'execution_count': 2, 'replan_count': 0,
              'usage': {'model_calls': 4, 'prompt_tokens': 900, 'completion_tokens': 100, 'total_tokens': 1000},
              'steps': [{'tools': [{'tool': 'read_file', 'success': True}, {'tool': 'run_python_code',
                                                                            'success': False}]}]}

    def launch(cmd, env, timeout):
        Path(cmd[cmd.index('--result-json') + 1]).write_text(json.dumps(record), encoding='utf-8')
        return SimpleNamespace(stdout='not the answer\n\nStatus: failed; executions: 9; replans: 9\n', stderr='')
    monkeypatch.setattr(run_bench, 'launch_agent', launch)
    row = run_bench.run_one(task, 'single', 1, 5)
    assert row['result_source'] == 'result_json' and row['passed'] is True
    assert (row['executions'], row['total_tokens'], row['model_calls']) == (2, 1000, 4)
    assert (row['tool_calls'], row['failed_tool_calls']) == (2, 1)
    assert run_bench.summary([row])['billing_qa/single']['total_tokens'] == 1000


def test_a_malformed_record_falls_back_to_stdout(tmp_path):
    from bench.run_bench import read_result
    path = tmp_path / 'r.json'
    for text in ('{', '[]', json.dumps({'status': 'completed', 'content': 'x', 'execution_count': '1',
                                        'replan_count': 0})):
        path.write_text(text, encoding='utf-8')
        assert read_result(path) is None
    assert read_result(tmp_path / 'missing.json') is None


# ---- one logging pipeline ----

@pytest.fixture
def restore_logging():
    from loguru import logger
    handlers, level = logging.root.handlers[:], logging.root.level
    app_level = logging.getLogger('app').level
    yield
    logger.remove()
    logger.add(sys.__stderr__)
    logging.root.handlers[:] = handlers
    logging.root.setLevel(level)
    logging.getLogger('app').setLevel(app_level)


def test_configure_logging_routes_both_libraries_through_one_level(capsys, restore_logging, monkeypatch):
    from loguru import logger
    from app.core.logging_setup import configure_logging
    monkeypatch.setenv('ALETHEIA_LOG_LEVEL', 'nonsense')
    assert configure_logging() == 'INFO'
    logging.getLogger('app.agent.route_selection').info('stdlib info line')
    logger.bind().patch(lambda r: r.update(name='app.agent.runtime')).info('loguru info line')
    logger.patch(lambda r: r.update(name='app.agent.runtime')).debug('loguru debug line')
    logging.getLogger('httpx').info('HTTP Request: POST https://provider.invalid')
    logging.getLogger('httpx').warning('httpx warning line')
    err = capsys.readouterr().err
    assert 'stdlib info line' in err and 'app.agent.route_selection' in err and 'loguru info line' in err
    assert 'debug line' not in err and 'provider.invalid' not in err and 'httpx warning line' in err
    assert configure_logging('warning') == 'WARNING'
    logging.getLogger('app.x').info('quiet now')
    assert 'quiet now' not in capsys.readouterr().err
