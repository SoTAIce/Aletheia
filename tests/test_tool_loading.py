"""Offline tests for on-demand deferred user MCP tool loading (D-099)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from mcp.types import CallToolResult, TextContent, Tool as MCPTool

from app.agent.agent_loop import ASK_USER, TODO, AgentLoopRunner, LoopOptions, LoopResumeStore
from app.agent.hooks import Decision, HookSet, HookedToolRegistry, using_hooks
from app.agent.permission_rules import PermissionRule, PermissionRules, RuleGate
from app.agent.route_selection import task_registry
from app.agent.run_limits import RunLimits
from app.agent.step_models import ModelTurn, SystemMessage, ToolCall, ToolDefinition, ToolOutput
from app.agent.tool_loading import (
    CALL_TOOL,
    DEFER_THRESHOLD,
    FIND_TOOLS,
    MAX_LOADED_TOOLS,
    MAX_VOLATILE_CHARS,
    LoadedTools,
    ToolLoading,
    is_user_mcp_tool,
)
from app.agent.trace_view import build_trace, render_text
from app.models.run_state import RunMemory, RunState
from app.services.context_manager import ContextManager
from app.services.run_assembler import RunAssembler
from app.tools.mcp_tool_adapter import UserMCPToolAdapter
from app.tools.tool_registry import ToolRegistry
from loop_fakes import started_context


def _call(call_id: str, tool_name: str, /, **arguments) -> ModelTurn:
    return ModelTurn(tool_calls=(ToolCall(call_id, tool_name, arguments),))


def _text(content: str) -> ModelTurn:
    return ModelTurn(content)


def _builtin_tool(name: str, log: list, *, effect: str = 'read'):
    async def invoke(arguments):
        log.append((name, arguments))
        return ToolOutput(True, f'{name}:{json.dumps(arguments, sort_keys=True)}')
    return SimpleNamespace(
        definition=ToolDefinition(name, f'Built-in {name}', {'type': 'object'}, effect=effect),
        invoke=invoke,
    )


def _user_mcp_tool(
    server: str,
    name: str,
    description: str,
    log: list,
    *,
    effect: str = 'read',
    schema: dict | None = None,
) -> UserMCPToolAdapter:
    client = AsyncMock()

    async def call_tool(remote_name, arguments, timeout_seconds=None):
        log.append((f'{server}__{remote_name}', arguments))
        return CallToolResult(
            content=[TextContent(type='text', text=f'ok:{server}__{remote_name}:{json.dumps(arguments, sort_keys=True)}')],
            isError=False,
        )

    client.call_tool.side_effect = call_tool
    mcp_tool = MCPTool(
        name=name,
        description=description,
        inputSchema=schema or {
            'type': 'object',
            'properties': {'id': {'type': 'string'}, 'count': {'type': 'integer'}},
            'required': ['id'],
            'additionalProperties': False,
        },
    )
    return UserMCPToolAdapter(client, mcp_tool, server_name=server, effect=effect, timeout_seconds=30.0)


def _registry_with_user_tools(count: int, log: list, *, hooked: bool = True, server: str = 'notes'):
    registry = HookedToolRegistry() if hooked else ToolRegistry()
    registry.register(_builtin_tool('read_file', log))
    for i in range(count):
        registry.register(_user_mcp_tool(
            server,
            f'tool_{i:02d}',
            f'Handle item {i} in {server}',
            log,
            effect='read' if i % 2 == 0 else 'external',
        ))
    return registry


def test_is_user_mcp_tool_detects_hooked_and_plain_adapters():
    log = []
    reg = HookedToolRegistry()
    reg.register(_builtin_tool('read_file', log))
    reg.register(_user_mcp_tool('notes', 'search', 'Search notes', log, effect='read'))
    assert not is_user_mcp_tool(reg.get('read_file'))
    assert is_user_mcp_tool(reg.get('notes__search'))


async def test_below_or_equal_threshold_keeps_requests_byte_for_byte_identical():
    log = []
    reg = _registry_with_user_tools(DEFER_THRESHOLD, log, hooked=False)
    assert ToolLoading.for_registry(reg) is None

    turns = [_call('c1', 'notes__tool_00', id='1'), _text('done')]
    model_a, model_b = AsyncMock(generate=AsyncMock(side_effect=list(turns))), AsyncMock(
        generate=AsyncMock(side_effect=list(turns))
    )
    ctx = started_context(goal='check notes', user='u')
    await AgentLoopRunner(model_a, reg, tool_loading=None).run(ctx)
    await AgentLoopRunner(model_b, reg, tool_loading=ToolLoading.for_registry(reg)).run(ctx)

    assert len(model_a.generate.await_args_list) == len(model_b.generate.await_args_list) == 2
    for req_a, req_b in zip(model_a.generate.await_args_list, model_b.generate.await_args_list):
        assert req_a.args == req_b.args


async def test_stable_prefix_and_volatile_zone_across_whole_task():
    log = []
    reg = HookedToolRegistry()
    reg.register(_builtin_tool('read_file', log))
    for i in range(6):
        reg.register(_user_mcp_tool('notes', f'note_{i}', f'Manage note {i}', log, effect='read'))
    for i in range(6):
        reg.register(_user_mcp_tool('calendar', f'event_{i}', f'Schedule calendar event {i}', log, effect='external'))

    recorded: list[tuple[tuple, tuple]] = []
    turns = [
        _call('f1', FIND_TOOLS, query='note_2'),
        _call('c1', CALL_TOOL, name='notes__note_2', arguments={'id': 'n42'}),
        _call('f2', FIND_TOOLS, query='event_1', server='calendar'),
        _call('c2', CALL_TOOL, name='calendar__event_1', arguments={'id': 'e7'}),
        _text('all done'),
    ]

    async def fake_generate(messages, tools):
        recorded.append((tuple(messages), tuple(tools)))
        return turns.pop(0)

    model = SimpleNamespace(generate=AsyncMock(side_effect=fake_generate))
    loading = ToolLoading.for_registry(reg)
    assert loading is not None
    events = []
    with using_hooks(HookSet((SimpleNamespace(observe=events.append),))):
        output = await AgentLoopRunner(model, reg, tool_loading=loading).run(started_context(goal='work', user='u'))

    assert output.content == 'all done'
    assert len(recorded) == 5

    # Tool list and first two messages (system + context) stay byte-for-byte identical on every round.
    first_messages, first_tools = recorded[0]
    first_tool_names = [t.name for t in first_tools]
    assert 'read_file' in first_tool_names
    assert FIND_TOOLS in first_tool_names and CALL_TOOL in first_tool_names
    assert not any(name.startswith(('notes__', 'calendar__')) for name in first_tool_names)
    assert '12 more tools are available from these servers: calendar (6), notes (6).' in first_messages[0].content

    for messages, tools in recorded[1:]:
        assert tools == first_tools
        assert messages[0] == first_messages[0]
        assert messages[1] == first_messages[1]

    # Round 0: no volatile message yet.
    assert not any(isinstance(m, SystemMessage) and 'Loaded tools' in m.content for m in recorded[0][0])

    # Round 1: volatile message at the very end with notes__note_2; not stored in history.
    assert isinstance(recorded[1][0][-1], SystemMessage)
    assert recorded[1][0][-1].content.startswith('HOST: Loaded tools (call them with call_tool):')
    assert 'Tool: notes__note_2' in recorded[1][0][-1].content
    assert sum(1 for m in recorded[1][0] if isinstance(m, SystemMessage) and 'Loaded tools' in m.content) == 1

    # Round 2 (same loaded set): volatile message is byte-for-byte identical to Round 1.
    assert recorded[2][0][-1] == recorded[1][0][-1]

    # Round 3 (after second find_tools): still only ONE volatile message at the end, containing both in load order.
    volatile_msgs = [m for m in recorded[3][0] if isinstance(m, SystemMessage) and 'Loaded tools' in m.content]
    assert len(volatile_msgs) == 1
    assert recorded[3][0][-1] == volatile_msgs[0]
    pos_note = volatile_msgs[0].content.index('Tool: notes__note_2')
    pos_event = volatile_msgs[0].content.index('Tool: calendar__event_1')
    assert pos_note < pos_event

    # Real tool names recorded in calls, results and hook events.
    assert [c.tool_name for c in output.tool_calls] == ['notes__note_2', 'calendar__event_1']
    assert [r.tool_name for r in output.tool_results] == ['notes__note_2', 'calendar__event_1']
    tool_events = [e['tool'] for e in events if e['type'] == 'before_tool']
    assert tool_events == ['notes__note_2', 'calendar__event_1']

    # tool_search events never contain query text.
    search_events = [e for e in events if e['type'] == 'tool_search']
    assert len(search_events) == 2
    for ev in search_events:
        assert 'query' not in ev
        assert 'note_2' not in json.dumps(ev)
        assert 'event_1' not in json.dumps(ev)


async def test_chinese_bigrams_server_filter_and_no_match():
    log = []
    reg = ToolRegistry()
    reg.register(_user_mcp_tool('finance', 'report', '生成季度财务报表并导出', log, effect='read'))
    reg.register(_user_mcp_tool('finance', 'invoice', '查询发票状态', log, effect='read'))
    reg.register(_user_mcp_tool('hr', 'summary', '生成员工出勤表', log, effect='read'))
    for i in range(9):
        reg.register(_user_mcp_tool('misc', f'item_{i}', f'其他工具 {i}', log, effect='read'))

    loading = ToolLoading.for_registry(reg)
    assert loading is not None

    # Chinese bigram match ranks 财务报表 first.
    res = await loading.find_tools({'query': '财务报表'})
    payload = json.loads(res.content)
    assert payload['loaded'][0]['name'] == 'finance__report'

    # Server filter restricts to hr only.
    res_hr = await loading.find_tools({'query': '生成', 'server': 'hr'})
    payload_hr = json.loads(res_hr.content)
    assert [item['name'] for item in payload_hr['loaded']] == ['hr__summary']

    # No match returns empty loaded and suggests rewording or server parameter.
    res_none = await loading.find_tools({'query': 'quantum_flux_capacitor'})
    payload_none = json.loads(res_none.content)
    assert payload_none['loaded'] == []
    assert payload_none['skipped_by_limit'] == 0
    assert 'server' in payload_none['note']


async def test_read_only_task_hides_external_user_tools():
    log = []
    reg = HookedToolRegistry()
    for i in range(12):
        reg.register(_user_mcp_tool('notes', f'read_{i}', f'Read note {i}', log, effect='read'))
    for i in range(5):
        reg.register(_user_mcp_tool('notes', f'delete_{i}', f'Delete note {i}', log, effect='external'))

    ro_reg = task_registry(reg, read_only=True)
    loading = ToolLoading.for_registry(ro_reg, read_only=True)
    assert loading is not None
    assert not any('delete_' in name for name in loading.deferred_names)
    res = await loading.find_tools({'query': 'delete'})
    payload = json.loads(res.content)
    assert payload['loaded'] == []


async def test_max_loaded_tools_and_max_volatile_chars_limits():
    log = []
    reg = ToolRegistry()
    for i in range(15):
        reg.register(_user_mcp_tool('svc', f'alpha_{i:02d}', f'Alpha tool number {i}', log, effect='read'))

    loading = ToolLoading.for_registry(reg)
    assert loading is not None
    # Load in batches to hit MAX_LOADED_TOOLS (10).
    for q in ('alpha_00 alpha_01 alpha_02 alpha_03 alpha_04',
              'alpha_05 alpha_06 alpha_07 alpha_08 alpha_09',
              'alpha_10 alpha_11 alpha_12'):
        res = await loading.find_tools({'query': q})
    payload = json.loads(res.content)
    assert len(loading.loaded_tools) == MAX_LOADED_TOOLS
    assert payload['skipped_by_limit'] == 3
    assert 'limit' in payload['note'].lower()

    # Re-searching already loaded tools does not skip or double-count.
    events = []
    with using_hooks(HookSet((SimpleNamespace(observe=events.append),))):
        again = await loading.find_tools({'query': 'alpha_00'})
    again_payload = json.loads(again.content)
    assert again_payload['skipped_by_limit'] == 0
    assert any(item['name'] == 'svc__alpha_00' for item in again_payload['loaded'])
    assert events[-1]['loaded'] == 0 and events[-1]['skipped'] == 0

    # Volatile char cap: big tool skipped, smaller tool still loads deterministically.
    reg2 = ToolRegistry()
    big_schema = {
        'type': 'object',
        'properties': {f'field_{k}': {'type': 'string', 'description': 'x' * 300} for k in range(30)},
    }
    reg2.register(_user_mcp_tool('big', 'huge_tool', 'widget giant schema', log, effect='read', schema=big_schema))
    for i in range(11):
        reg2.register(_user_mcp_tool('big', f'small_{i}', 'widget small schema', log, effect='read'))
    loading2 = ToolLoading.for_registry(reg2)
    assert loading2 is not None
    res2 = await loading2.find_tools({'query': 'widget'})
    payload2 = json.loads(res2.content)
    assert payload2['skipped_by_limit'] >= 1
    assert 'big__huge_tool' not in loading2.loaded_tools
    assert len(loading2.volatile_message() or '') <= MAX_VOLATILE_CHARS


async def test_call_tool_rejects_unloaded_and_invalid_schema_and_continues():
    log = []
    reg = _registry_with_user_tools(12, log, hooked=False)
    loading = ToolLoading.for_registry(reg)
    assert loading is not None

    turns = [
        _call('u1', CALL_TOOL, name='notes__tool_00', arguments={'id': '1'}),  # not loaded yet
        _call('f1', FIND_TOOLS, query='tool_00'),
        _call('b1', CALL_TOOL, name='notes__tool_00', arguments={'count': 'wrong'}),  # missing id + wrong type
        _call('b2', CALL_TOOL, name='notes__tool_00', arguments='not-an-object'),
        _call('ok', CALL_TOOL, name='notes__tool_00', arguments={'id': '42', 'count': 3}),
        _text('recovered'),
    ]
    model = AsyncMock(generate=AsyncMock(side_effect=turns))
    output = await AgentLoopRunner(model, reg, tool_loading=loading).run(started_context(goal='test', user='u'))

    assert output.content == 'recovered'
    # Only the valid call reached the underlying adapter.
    assert log == [('notes__tool_00', {'id': '42', 'count': 3})]
    # Check error messages returned to model.
    msgs_after_u1 = model.generate.await_args_list[1].args[0]
    err_u1 = json.loads(msgs_after_u1[-1].content)
    assert err_u1['success'] is False and 'not been loaded' in err_u1['error']

    msgs_after_b1 = model.generate.await_args_list[3].args[0]
    err_b1 = json.loads(msgs_after_b1[-2].content)  # -1 is volatile SystemMessage
    assert err_b1['success'] is False and 'invalid arguments' in err_b1['error']

    msgs_after_b2 = model.generate.await_args_list[4].args[0]
    err_b2 = json.loads(msgs_after_b2[-2].content)
    assert err_b2['success'] is False and 'JSON object' in err_b2['error']


async def test_call_tool_goes_through_permission_rule_gate_with_real_name():
    log = []
    reg = _registry_with_user_tools(12, log, hooked=True)
    loading = ToolLoading.for_registry(reg)
    assert loading is not None

    asked: list[tuple[str, dict]] = []

    class FakeAsker:
        async def ask(self, event, *, rule=None):
            asked.append((event['tool'], event))
            return Decision.deny('user denied', rule=rule)

    rules = PermissionRules((PermissionRule(1, 'ask', tools=('notes__tool_01',)),))
    gate = RuleGate(rules, asker=FakeAsker())
    turns = [
        _call('f1', FIND_TOOLS, query='tool_01'),
        _call('c1', CALL_TOOL, name='notes__tool_01', arguments={'id': '9'}),
        _text('denied gracefully'),
    ]
    model = AsyncMock(generate=AsyncMock(side_effect=turns))
    with using_hooks(HookSet((), (gate,))):
        output = await AgentLoopRunner(model, reg, tool_loading=loading).run(started_context(goal='g', user='u'))

    assert output.content == 'denied gracefully'
    assert len(asked) == 1 and asked[0][0] == 'notes__tool_01'
    assert log == []


async def test_loaded_tools_survive_blocked_task_resume():
    from dataclasses import replace
    from datetime import UTC, datetime
    from app.models.taskstate import OpenQuestion
    log = []
    reg = _registry_with_user_tools(12, log, hooked=False)
    memory = RunMemory(RunState('u', 'ask then call'))
    store = LoopResumeStore()

    loading1 = ToolLoading.for_registry(reg, memory.loaded_tools)
    turns1 = [
        _call('f1', FIND_TOOLS, query='tool_00'),
        _call('q1', ASK_USER, question='Which id?'),
    ]
    model1 = AsyncMock(generate=AsyncMock(side_effect=turns1))
    ctx1 = replace(started_context(goal='ask then call', user='u'), task_id=memory.task_state.task_id)
    out1 = await AgentLoopRunner(model1, reg, resume_store=store, tool_loading=loading1).run(ctx1)
    assert out1.outcome.value == 'blocked'
    assert 'notes__tool_00' in memory.loaded_tools

    # Simulate rebind with a fresh ToolLoading backed by the same memory.loaded_tools.
    loading2 = ToolLoading.for_registry(reg, memory.loaded_tools)
    turns2 = [
        _call('c1', CALL_TOOL, name='notes__tool_00', arguments={'id': 'from-user'}),
        _text('finished'),
    ]
    model2 = AsyncMock(generate=AsyncMock(side_effect=turns2))
    now = datetime.now(UTC)
    resolved_q = OpenQuestion('q1', 'Which id?', 's1', 1, False, now, answer='from-user', resolved_at=now)
    ctx2 = replace(ctx1, resolved_questions=(resolved_q,))
    out2 = await AgentLoopRunner(model2, reg, resume_store=store, tool_loading=loading2).run(ctx2)
    assert out2.content == 'finished'
    assert log == [('notes__tool_00', {'id': 'from-user'})]
    # Volatile message was present immediately on the first resumed request.
    assert 'Tool: notes__tool_00' in model2.generate.await_args_list[0].args[0][-1].content


async def test_request_folding_preserves_volatile_system_message():
    log = []
    reg = _registry_with_user_tools(12, log, hooked=False)
    loading = ToolLoading.for_registry(reg)
    assert loading is not None
    await loading.find_tools({'query': 'tool_00'})
    volatile = loading.volatile_message()
    assert volatile is not None

    manager = ContextManager(window_tokens=4000, output_tokens=500, safety_tokens=200)
    messages = [
        SystemMessage('base system'),
        SimpleNamespace(role='user', content='{"active_goal": "g"}'),
    ]
    # Use real UserMessage/AssistantMessage/ToolMessage via ContextManager.fitter.
    from app.agent.step_models import AssistantMessage, ToolMessage, UserMessage
    long_body = 'a' * 3000
    messages = (
        SystemMessage('base system'),
        UserMessage('{"active_goal": "g"}'),
        AssistantMessage(None, (ToolCall('t1', 'read_file', {'path': 'a.py'}),)),
        ToolMessage('t1', json.dumps({'success': True, 'content': long_body, 'error': None, 'source_refs': []})),
        AssistantMessage(None, (ToolCall('t2', 'read_file', {'path': 'b.py'}),)),
        ToolMessage('t2', json.dumps({'success': True, 'content': long_body, 'error': None, 'source_refs': []})),
        SystemMessage(volatile),
    )
    compacted = manager.fitter.compact(messages, ())
    prepared = manager.fitter.prepare(compacted, ())
    assert prepared[-1] == SystemMessage(volatile)


def test_trace_view_and_run_limits_include_tool_loading():
    records = [
        {'type': 'audit', 'started_at': '2026-11-24T00:00:00Z', 'mode': 'single', 'read_only': False},
        {'type': 'task_start', 't': 0.0, 'span_id': 's-1', 'task_id': 'task-1'},
        {'type': 'tool_search', 't': 0.1, 'parent_id': 's-1', 'found': 3, 'loaded': 2, 'skipped': 1},
        {'type': 'task_end', 't': 0.2, 'status': 'completed', 'task_id': 'task-1'},
    ]
    trace = build_trace(records)
    rendered = render_text(trace)
    assert '查找工具（匹配 3，加载 2，超限跳过 1）' in rendered

    rows = RunLimits.current().rows()
    loading_rows = [row for row in rows if row[0] == '工具加载']
    assert len(loading_rows) == 3
    joined = ' '.join(value for _, _, value in loading_rows)
    assert str(DEFER_THRESHOLD) in joined and str(MAX_LOADED_TOOLS) in joined and f'{MAX_VOLATILE_CHARS:,}' in joined


async def test_run_assembler_wires_tool_loading_for_single_loop_only():
    log = []
    reg = _registry_with_user_tools(12, log, hooked=True)
    memory = RunMemory(RunState('u', 'find a note'))
    model = AsyncMock(generate=AsyncMock(return_value=_text('done')))
    assembler = RunAssembler(
        team_options=None,
        loop_options=LoopOptions(),
        delegate=False,
        limits={},
        routes={},
        task_options={},
        default_workspace='.',
        emit=lambda cb, ev: None,
    )
    with using_hooks(HookSet()):
        _, result = await assembler.run(
            key=('u', 's'),
            task=memory.task_state,
            question='find a note',
            options={'mode': 'single', 'write': True, 'ask_user': True},
            runtime=None,
            model=model,
            memory=memory,
            registry=reg,
            context_manager=None,
            audit=None,
            on_event=None,
        )
    assert result.content == 'done'
    sent_tools = [t.name for t in model.generate.await_args_list[0].args[1]]
    assert FIND_TOOLS in sent_tools and CALL_TOOL in sent_tools
    assert not any(name.startswith('notes__') for name in sent_tools)
