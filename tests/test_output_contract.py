"""Structured output contract (D-093, docs/output_contract_plan.md). Offline tests with scripted models."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.agent_loop import ASK_USER, AgentLoopRunner, LoopOptions, REMIND_TO_RUN
from app.agent.delegation import ExploreLoopRunner, DelegationOptions
from app.agent.hooks import HookSet, using_hooks
from app.agent.loop_run import LoopRun
from app.agent.output_contract import MAX_ERROR_CHARS, MAX_ERRORS, MAX_SCHEMA_CHARS, OutputContract
from app.agent.run_meter import RunMeter, metering
from app.agent.runtime import FAILURE_MESSAGES
from app.agent.step_models import (
    ModelTurn, StepOutcome, SystemMessage, ToolCall, ToolDefinition, ToolOutput, ToolsShownOnly,
)
from app.agent.step_runner import StepContractError, StepModelError
from app.agent.tool_calling_model import ModelToolArgumentsError, ToolCallingModelError
from app.agent.trace_view import build_trace, render_text
from app.models.run_state import RunMemory, RunState
from app.models.taskstate import TaskStatus
from app.services.rag_agent_service import RagAgentService
from app.tools.tool_registry import ToolRegistry
from bench.run_bench import usage_fields
from loop_fakes import started_context

CONTRACT_SCHEMA = {
    'type': 'object',
    'properties': {
        'party': {'type': 'string'},
        'items': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {'price': {'type': 'number'}},
                'required': ['price'],
            },
        },
    },
    'required': ['party', 'items'],
    'additionalProperties': False,
}


class ScriptedModel:
    """Returns scripted ModelTurns while recording every (messages, tools) request."""

    def __init__(self, *turns: ModelTurn) -> None:
        self._turns = list(turns)
        self.requests: list[tuple[tuple, object]] = []

    async def generate(self, messages, tools=()):
        self.requests.append((tuple(messages), tools))
        return self._turns.pop(0)


def _registry(*, writable: bool = False) -> tuple[ToolRegistry, list[dict]]:
    calls = []
    registry = ToolRegistry()

    def make(name: str, effect: str):
        async def invoke(args):
            calls.append({'tool': name, 'args': dict(args)})
            return ToolOutput(True, f'ok:{name}')
        return SimpleNamespace(definition=ToolDefinition(name, name, {'type': 'object'}, effect=effect), invoke=invoke)

    registry.register(make('read_file', 'read'))
    if writable:
        registry.register(make('write_file', 'write'))
        registry.register(make('run_tests', 'run'))
    return registry, calls


# ---- OutputContract validation & checking ----

def test_contract_accepts_valid_json_and_unwraps_fence():
    contract = OutputContract(CONTRACT_SCHEMA)
    raw = '```json\n{"party": "Acme", "items": [{"price": 12.5}]}\n```'
    checked = contract.check(raw)
    assert checked.ok is True
    assert checked.value == {'party': 'Acme', 'items': [{'price': 12.5}]}
    assert checked.text == '{"party": "Acme", "items": [{"price": 12.5}]}'
    assert checked.errors == ()


@pytest.mark.parametrize('bad', [
    'not json at all',
    '{"party": "A", "party": "B", "items": []}',
    '{"party": "A", "items": [{"price": NaN}]}',
    '{"party": "A", "items": [{"price": Infinity}]}',
])
def test_contract_rejects_invalid_duplicate_and_non_finite_json(bad):
    contract = OutputContract(CONTRACT_SCHEMA)
    checked = contract.check(bad)
    assert checked.ok is False and len(checked.errors) == 1
    assert checked.errors[0].startswith('/:')


def test_contract_reports_json_pointer_capped_at_five_and_200_chars():
    schema = {
        'type': 'object',
        'properties': {f'k{i}': {'type': 'integer'} for i in range(8)},
        'required': [f'k{i}' for i in range(8)],
    }
    contract = OutputContract(schema)
    checked = contract.check(json.dumps({f'k{i}': 'x' * 300 for i in range(8)}))
    assert checked.ok is False
    assert len(checked.errors) == MAX_ERRORS
    assert all(len(e) <= MAX_ERROR_CHARS for e in checked.errors)

    nested = OutputContract(CONTRACT_SCHEMA).check('{"party": "A", "items": [{"price": "bad"}]}')
    assert nested.ok is False
    assert any(e.startswith('/items/0/price:') for e in nested.errors)


def test_contract_rejects_bad_oversized_and_remote_ref_schemas_without_leaking_text():
    secret = 'SECRET_SCHEMA_TOKEN'
    bad_schemas = [
        ['not', 'a', 'dict'],
        {'type': 'object', 'description': secret + 'x' * MAX_SCHEMA_CHARS},
        {'type': 'not_a_real_type', 'description': secret},
        {'$ref': f'https://example.com/{secret}.json'},
        {'properties': {'a': {'$ref': f'http://example.com/{secret}.json'}}},
        {'properties': {'a': {'$ref': f'#/$defs/{secret}_missing'}}},
    ]
    for bad in bad_schemas:
        with pytest.raises(ValueError) as exc_info:
            OutputContract(bad)
        assert secret not in str(exc_info.value)

    # Local $defs $ref resolves offline and validates normally.
    local_ref = {
        '$defs': {'pos': {'type': 'integer', 'minimum': 1}},
        'type': 'object',
        'properties': {'count': {'$ref': '#/$defs/pos'}},
        'required': ['count'],
    }
    contract = OutputContract(local_ref)
    assert contract.check('{"count": 3}').ok is True
    assert contract.check('{"count": 0}').ok is False


# ---- AgentLoopRunner & LoopRun ----

async def test_loop_delivers_valid_fenced_answer_and_records_zero_repairs():
    model = ScriptedModel(ModelTurn('```json\n{"party": "Acme", "items": [{"price": 10}]}\n```'))
    registry, _ = _registry()
    runner = AgentLoopRunner(model, registry, output_contract=OutputContract(CONTRACT_SCHEMA))
    with metering(RunMeter()) as meter:
        out = await runner.run(started_context(goal='extract'))
    assert out.outcome is StepOutcome.SUCCEEDED
    assert out.content == '{"party": "Acme", "items": [{"price": 10}]}'
    assert out.output == {'party': 'Acme', 'items': [{'price': 10}]}
    assert meter.snapshot()['contract'] == {'repairs': 0}


async def test_loop_repairs_once_without_running_tools_or_mutating_prior_messages():
    bad = ModelTurn('{"party": "Acme", "items": [{"price": "ten"}]}')
    good = ModelTurn('{"party": "Acme", "items": [{"price": 10}]}')
    model = ScriptedModel(bad, good)
    registry, calls = _registry()
    runner = AgentLoopRunner(model, registry, output_contract=OutputContract(CONTRACT_SCHEMA))
    with metering(RunMeter()) as meter:
        out = await runner.run(started_context(goal='extract'))
    assert out.outcome is StepOutcome.SUCCEEDED and out.output == {'party': 'Acme', 'items': [{'price': 10}]}
    assert calls == []
    assert len(model.requests) == 2
    first_msgs, first_tools = model.requests[0]
    second_msgs, second_tools = model.requests[1]
    assert second_msgs[:len(first_msgs)] == first_msgs
    assert isinstance(second_tools, ToolsShownOnly)
    assert isinstance(second_msgs[-1], SystemMessage)
    assert second_msgs[-1].content.startswith('HOST:')
    assert '/items/0/price' in second_msgs[-1].content
    assert 'do not call any tools' in second_msgs[-1].content
    assert meter.snapshot()['contract'] == {'repairs': 1}


async def test_loop_refuses_tool_calls_during_repair_and_fails_immediately():
    model = ScriptedModel(
        ModelTurn('{"party": "Acme"}'),
        ModelTurn(tool_calls=(ToolCall('c1', 'read_file', {'path': 'a.txt'}),)),
    )
    registry, calls = _registry()
    runner = AgentLoopRunner(model, registry, output_contract=OutputContract(CONTRACT_SCHEMA))
    with pytest.raises(StepContractError):
        await runner.run(started_context(goal='extract'))
    assert calls == [] and len(model.requests) == 2


@pytest.mark.parametrize('error, expected', [
    (ModelToolArgumentsError('invalid_json', tool_name='read_file', argument_chars=20), StepContractError),
    (ToolCallingModelError('provider down'), StepModelError),
])
async def test_a_model_error_in_the_repair_turn_keeps_its_kind_unless_it_is_a_tool_call(error, expected):
    class Failing(ScriptedModel):
        async def generate(self, messages, tools=()):
            if self.requests:
                self.requests.append((tuple(messages), tools))
                raise error
            return await super().generate(messages, tools)

    model = Failing(ModelTurn('{"party": "Acme"}'))
    registry, _ = _registry()
    runner = AgentLoopRunner(model, registry, output_contract=OutputContract(CONTRACT_SCHEMA))
    with pytest.raises((StepModelError, StepContractError)) as caught:
        await runner.run(started_context(goal='extract'))
    assert type(caught.value) is expected


async def test_loop_run_maps_second_failure_to_contract_kind_and_chinese_message():
    model = ScriptedModel(ModelTurn('{"party": "Acme"}'), ModelTurn('still not json'))
    registry, _ = _registry()
    memory = RunMemory(RunState('u1', 'extract'))
    contract = OutputContract(CONTRACT_SCHEMA)
    runtime = LoopRun(
        memory, model, registry,
        runner_factory=lambda d: AgentLoopRunner(d.model, d.tool_registry, output_contract=contract),
        step_description='step',
    )
    result = await runtime.run()
    assert result.status is TaskStatus.FAILED
    assert result.failure_kind == 'contract'
    assert result.user_message == FAILURE_MESSAGES['contract']
    assert result.output is None
    assert result.usage['contract'] == {'repairs': 1}


async def test_loop_can_repair_even_after_tool_budget_is_spent():
    model = ScriptedModel(
        ModelTurn(tool_calls=(ToolCall('c1', 'read_file', {'path': 'a.txt'}),)),
        ModelTurn('{"party": "Acme"}'),
        ModelTurn('{"party": "Acme", "items": [{"price": 5}]}'),
    )
    registry, calls = _registry()
    options = LoopOptions(max_tool_rounds=1)
    runner = AgentLoopRunner(model, registry, options=options, output_contract=OutputContract(CONTRACT_SCHEMA))
    out = await runner.run(started_context(goal='extract'))
    assert len(calls) == 1
    assert out.outcome is StepOutcome.SUCCEEDED and out.output == {'party': 'Acme', 'items': [{'price': 5}]}
    assert isinstance(model.requests[-1][1], ToolsShownOnly)


async def test_remind_to_run_fires_before_output_contract_check():
    model = ScriptedModel(
        ModelTurn(tool_calls=(ToolCall('w1', 'write_file', {'path': 'a.py'}),)),
        ModelTurn('{"party": "Acme"}'),                          # triggers REMIND_TO_RUN first
        ModelTurn('{"party": "Acme"}'),                          # now hits contract repair
        ModelTurn('{"party": "Acme", "items": [{"price": 1}]}'),  # repaired and accepted
    )
    registry, _ = _registry(writable=True)
    options = LoopOptions(remind_to_run=True)
    runner = AgentLoopRunner(model, registry, options=options, output_contract=OutputContract(CONTRACT_SCHEMA))
    out = await runner.run(started_context(goal='update and report'))
    assert out.outcome is StepOutcome.SUCCEEDED and runner.unverified is True
    assert model.requests[2][0][-1].content == REMIND_TO_RUN
    assert model.requests[3][0][-1].content.startswith('HOST: your previous answer did not conform')


async def test_explore_loop_runner_enforces_contract_on_final_answer():
    model = ScriptedModel(
        ModelTurn('not json'),
        ModelTurn('{"party": "Acme", "items": []}'),
    )
    registry, _ = _registry()
    runner = ExploreLoopRunner(model, registry, output_contract=OutputContract(CONTRACT_SCHEMA))
    out = await runner.run(started_context(goal='extract'))
    assert out.outcome is StepOutcome.SUCCEEDED and out.output == {'party': 'Acme', 'items': []}


# ---- RagAgentService: modes, resume, privacy & trace ----

async def test_service_auto_mode_skips_router_and_preserves_write_setting(tmp_path):
    main_model = ScriptedModel(
        ModelTurn('{"party": "Acme", "items": []}'),
        ModelTurn('{"party": "Beta", "items": []}'),
    )
    team_opts = DelegationOptions(allowed_tools=frozenset({'read_file', 'write_file', 'run_tests'}),
                                  require_passing_run=True)
    events = []
    async with RagAgentService(
        model=main_model, mcp_servers=(), include_knowledge=False,
        memory_directory=tmp_path, execution_mode='auto', team_options=team_opts,
    ) as service:
        res_default = await service.query_result(
            'extract', 's1', output_schema=CONTRACT_SCHEMA, on_event=events.append,
        )
        res_ro = await service.query_result(
            'extract', 's2', output_schema=CONTRACT_SCHEMA, allow_writes=False, on_event=events.append,
        )
    assert res_default.status is TaskStatus.COMPLETED and res_default.output == {'party': 'Acme', 'items': []}
    assert res_ro.status is TaskStatus.COMPLETED and res_ro.output == {'party': 'Beta', 'items': []}
    # Exactly 2 model calls total (1 per task) -> the route model was never called.
    assert len(main_model.requests) == 2
    route_events = [e for e in events if e.get('type') == 'route']
    assert [(e['mode'], e['read_only']) for e in route_events] == [('single', False), ('single', True)]
    # Schema constraint was injected into the task context.
    sent_text = json.dumps([m.content for m in main_model.requests[0][0]], ensure_ascii=False)
    assert 'HOST OUTPUT CONTRACT' in sent_text


async def test_service_rejects_team_mode_and_bad_schema_before_model_call(tmp_path):
    model = AsyncMock()
    team_opts = DelegationOptions(allowed_tools=frozenset({'read_file', 'write_file', 'run_tests'}),
                                  require_passing_run=True)
    async with RagAgentService(
        model=model, mcp_servers=(), include_knowledge=False,
        memory_directory=tmp_path, execution_mode='auto', team_options=team_opts,
    ) as service:
        with pytest.raises(ValueError, match='team'):
            await service.query_result('extract', 's1', execution_mode='team', output_schema=CONTRACT_SCHEMA)
        with pytest.raises(ValueError):
            await service.query_result('extract', 's1', output_schema={'$ref': 'https://example.com/a.json'})
    assert model.generate.await_count == 0


async def test_service_resume_keeps_contract_and_rejects_new_schema(tmp_path):
    model = ScriptedModel(
        ModelTurn(tool_calls=(ToolCall('q1', ASK_USER, {'question': 'Which currency?'}),)),
        ModelTurn('not json'),
        ModelTurn('{"party": "Acme", "items": [{"price": 42}]}'),
    )
    async with RagAgentService(
        model=model, mcp_servers=(), include_knowledge=False, memory_directory=tmp_path,
    ) as service:
        blocked = await service.query_result('extract', 's1', output_schema=CONTRACT_SCHEMA)
        assert blocked.status is TaskStatus.BLOCKED and blocked.output is None
        with pytest.raises(ValueError, match='resume'):
            await service.query_result('USD', 's1', resume_task_id=blocked.task_id, output_schema=CONTRACT_SCHEMA)
        done = await service.query_result('USD', 's1', resume_task_id=blocked.task_id)
        assert done.status is TaskStatus.COMPLETED
        assert done.output == {'party': 'Acme', 'items': [{'price': 42}]}
        assert done.usage['contract'] == {'repairs': 1}


async def test_observer_events_and_trace_never_leak_secret_text():
    secret = 'TOP_SECRET_VALUE_999'
    schema = {
        'type': 'object',
        'description': secret,
        'properties': {'answer': {'type': 'string', 'const': secret}},
        'required': ['answer'],
    }
    model = ScriptedModel(
        ModelTurn(json.dumps({'answer': f'wrong {secret}'})),
        ModelTurn(json.dumps({'answer': secret})),
    )
    registry, _ = _registry()
    events = []
    observer = SimpleNamespace(observe=events.append)
    runner = AgentLoopRunner(model, registry, output_contract=OutputContract(schema))
    with using_hooks(HookSet((observer,))):
        await runner.run(started_context(goal='extract'))
    contract_events = [e for e in events if e['type'] == 'output_contract']
    assert contract_events == [
        {'type': 'output_contract', 'result': 'repair', 'errors': 1, 'parent_id': None},
        {'type': 'output_contract', 'result': 'pass', 'errors': 0, 'parent_id': None},
    ]
    assert secret not in json.dumps(events)

    header = {'type': 'audit', 'version': 2, 'trace': True, 'events': 0, 'dropped': 0}
    records = [
        header,
        {'t': 0.0, 'type': 'task_start', 'task_id': 'abcdef12', 'span_id': 'T', 'parent_id': None},
        {'t': 0.1, 'type': 'output_contract', 'result': 'repair', 'errors': 2, 'parent_id': 'T'},
        {'t': 0.2, 'type': 'output_contract', 'result': 'pass', 'errors': 0, 'parent_id': 'T'},
        {'t': 0.3, 'type': 'output_contract', 'result': 'unknown_kind', 'errors': 1, 'parent_id': 'T'},
    ]
    rendered = render_text(build_trace(records))
    assert '输出格式不合格，要求修改一次（2 处错误）' in rendered
    assert '输出格式符合要求' in rendered
    assert 'unknown_kind' not in rendered


def test_bench_usage_fields_includes_contract_only_when_present():
    assert usage_fields({'usage': {'contract': {'repairs': 1}}})['contract'] == {'repairs': 1}
    assert 'contract' not in usage_fields({'usage': {'model_calls': 1}})
