"""D-087: a tool declares what it does to the workspace; undeclared tools fall to the built-in table,
and anything else counts as a side effect. No model, network or Docker."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.types import Tool, ToolAnnotations

from app.agent.agent_loop import LOOP_STEP, AgentLoopRunner
from app.agent.loop_run import LoopRun
from app.agent.route_selection import READ_ONLY_HIDDEN, allowed_modes, read_only_registry
from app.agent.side_effects import SIDE_EFFECT_TOOLS, SideEffectWatch, is_side_effect
from app.agent.step_models import ModelTurn, ToolCall, ToolDefinition, ToolOutput
from app.agent.team.models import RUN_TOOLS, WORKER_ONLY_TOOLS
from app.agent.tool_effects import BUILTIN_EFFECTS, EFFECTS, effect_of, effect_of_name, has_side_effect
from app.models.run_state import RunMemory, RunState
from app.tools.mcp_tool_adapter import MCPToolAdapter
from app.tools.tool_registry import ToolRegistry


def fake(name, effect=None, *effects):
    return SimpleNamespace(definition=ToolDefinition(name, name, {'type': 'object'}, effect=effect),
                           invoke=AsyncMock(side_effect=list(effects) or [ToolOutput(True, 'ok')]))


def registry(*tools):
    reg = ToolRegistry()
    for tool in tools:
        reg.register(tool)
    return reg


# ---- the rule ----

@pytest.mark.parametrize('name, declared, effect', [
    ('read_file', None, 'read'), ('edit_file', None, 'write'), ('run_tests', None, 'run'),
    ('run_python_code', None, 'read'),            # the workspace is mounted read-only (D-028)
    ('delete_file', None, 'external'),            # unknown and undeclared: a side effect
    ('delete_file', 'write', 'write'),            # a declaration wins
    ('read_file', 'external', 'external'),
    ('read_file', 'bogus', 'read'),               # not an effect: ignored
    (None, None, 'external'),
])
def test_declared_first_then_the_table_then_external(name, declared, effect):
    assert effect_of_name(name, declared) == effect
    assert effect_of(SimpleNamespace(name=name, effect=declared)) == effect


def test_every_built_in_effect_is_known_and_only_reads_have_no_side_effect():
    assert set(BUILTIN_EFFECTS.values()) <= set(EFFECTS)
    assert [e for e in EFFECTS if not has_side_effect(e)] == ['read']


def test_the_older_name_sets_come_from_the_table():
    writes_and_runs = {n for n, e in BUILTIN_EFFECTS.items() if e in ('write', 'run')}
    assert WORKER_ONLY_TOOLS == SIDE_EFFECT_TOOLS == READ_ONLY_HIDDEN == writes_and_runs
    assert RUN_TOOLS == {'run_tests', 'run_python'}
    assert 'edit_file' in WORKER_ONLY_TOOLS


def test_the_effect_field_is_never_sent_to_the_provider():
    from app.agent.openai_compatible_tool_calling_model import OpenAICompatibleToolCallingModel
    declared = OpenAICompatibleToolCallingModel._build_tool_payload(
        (ToolDefinition('read', 'r', {'type': 'object'}, effect='read'),))
    plain = OpenAICompatibleToolCallingModel._build_tool_payload((ToolDefinition('read', 'r', {'type': 'object'}),))
    assert declared == plain and 'effect' not in str(declared)


# ---- MCP annotations ----

@pytest.mark.parametrize('annotations, effect', [
    (ToolAnnotations(readOnlyHint=True), 'read'),
    (ToolAnnotations(readOnlyHint=False, destructiveHint=True), None),
    (None, None),
])
def test_the_adapter_declares_read_only_from_the_annotation(annotations, effect):
    adapter = MCPToolAdapter(SimpleNamespace(), Tool(name='lookup', inputSchema={'type': 'object'},
                                                     annotations=annotations))
    assert adapter.definition.effect == effect
    assert effect_of(adapter.definition) == (effect or 'external')


async def test_the_sandbox_and_web_search_servers_annotate_their_tools():
    from app.tools.sandbox.server import create_server
    from app.tools.web_search.server import create_service
    listed = [*await create_server(None, sandbox=SimpleNamespace()).list_tools(),
              *await create_service('unused').list_tools()]
    assert {t.name for t in listed} == {'run_tests', 'run_python', 'run_python_code', 'search_web'}
    for tool in listed:
        definition = MCPToolAdapter(SimpleNamespace(), tool).definition
        assert effect_of(definition) == BUILTIN_EFFECTS[tool.name]
        assert (definition.effect == 'read') == (BUILTIN_EFFECTS[tool.name] == 'read')


# ---- an undeclared, unknown tool is a side effect everywhere ----

def test_the_watch_and_the_read_only_view_treat_it_as_a_side_effect():
    reg = registry(fake('delete_file'), fake('lookup', 'read'))
    assert is_side_effect(reg, 'delete_file') and not is_side_effect(reg, 'lookup')
    assert not is_side_effect(reg, 'not_registered')          # an unknown name runs nothing
    assert [d.name for d in read_only_registry(reg).list_definitions()] == ['lookup']


async def test_the_watch_marks_it_touched():
    watch = SideEffectWatch(registry(fake('delete_file')))
    await watch.invoke(ToolCall('c', 'delete_file', {}))
    assert watch.touched


async def test_a_raise_from_it_ends_the_task_and_is_not_repeated():
    tool = fake('delete_file', None, OSError('x'))
    model = AsyncMock()
    model.generate.side_effect = [ModelTurn(tool_calls=(ToolCall('c', 'delete_file', {}),)), ModelTurn('never')]
    run = LoopRun(RunMemory(RunState('user', 'goal')), model, registry(tool),
                  runner_factory=lambda deps: AgentLoopRunner(deps.model, deps.tool_registry),
                  step_description=LOOP_STEP)
    result = await run.run()
    assert result.failure_kind == 'side_effect' and tool.invoke.await_count == 1


def test_team_routing_needs_a_write_tool():
    with_edit = SimpleNamespace(allowed_tools=frozenset({'read_file', 'edit_file'}), require_passing_run=True)
    read_only = SimpleNamespace(allowed_tools=frozenset({'read_file'}), require_passing_run=True)
    assert allowed_modes(with_edit)[1] == 'code_delivery_with_sandbox'
    assert allowed_modes(read_only)[1] == 'read_only'


def test_writes_off_drop_every_tool_that_is_not_a_read():
    from app.services.run_assembler import _without_writes
    from app.agent.delegation import DelegationOptions
    options = DelegationOptions(allowed_tools=frozenset({'read_file', 'write_file', 'edit_file', 'run_tests',
                                                         'run_python_code'}), require_passing_run=True)
    kept = _without_writes(options)
    assert kept.allowed_tools == {'read_file', 'run_python_code'} and kept.require_passing_run is False


# ---- the web approval gate ----

class _Run:
    def __init__(self):
        self.approvals, self.events = {}, []

    def add(self, event):
        self.events.append(event)


async def test_the_approval_gate_asks_about_a_tool_of_unknown_effect():
    from app.api.approvals import ApprovalGate
    run = _Run()
    gate = ApprovalGate(run, frozenset({'write_file'}), 5)
    assert (await gate.check({'tool': 'lookup', 'effect': 'read'})).allow
    pending = asyncio.ensure_future(gate.check({'tool': 'delete_file', 'effect': 'external'}))
    async with asyncio.timeout(5):
        while not run.approvals:
            await asyncio.sleep(0.01)
    next(iter(run.approvals.values())).set_result('deny')
    assert not (await pending).allow
    assert [e['tool'] for e in run.events if e['type'] == 'approval_required'] == ['delete_file']
