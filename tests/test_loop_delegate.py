"""On-demand read-only delegation inside the single loop (docs/loop_delegate_plan.md, loop-delegate-001)."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.agent_loop import ASK_USER, TODO, AgentLoopRunner, LoopOptions, LoopPlanner
from app.agent.delegation import (DELEGATE, EXPLORE_INSTRUCTIONS, ExploreDelegation, ExploreLoopRunner,
                                  ExploreOptions)
from app.agent.hooks import HookSet, using_hooks
from app.agent.run_meter import RunMeter, metering, record_model_call
from app.agent.step_executor import StepExecutor
from app.agent.step_models import ModelTurn, ToolCall, ToolDefinition, ToolOutput
from app.models.taskstate import TaskState
from app.models.working_memory import WorkingMemory
from app.tools.tool_registry import ToolRegistry


def call(call_id, name, **arguments):
    return ModelTurn(tool_calls=(ToolCall(call_id, name, arguments),))


def text(content):
    return ModelTurn(content)


def _tool(name, log):
    async def invoke(arguments):
        log.append((name, arguments))
        return ToolOutput(True, f'{name} {arguments}')
    return SimpleNamespace(definition=ToolDefinition(name, name, {'type': 'object'}), invoke=invoke)


def setup(turns, *, loop_options=None, options=None, tally=None):
    model = AsyncMock()
    model.generate.side_effect = turns
    log = []
    registry = ToolRegistry()
    for name in ('read_file', 'search_files', 'write_file'):
        registry.register(_tool(name, log))
    runner = ExploreLoopRunner(model, registry, loop_options, options=options, tally=tally)
    wm = WorkingMemory(TaskState('user', 'explain the whole project'))
    wm.task_state.pin_content('earlier turns', 'conversation_memory')
    wm.task_state.start_planning()
    wm.install_plan([LoopPlanner.STEP])
    step_id = wm.task_state.plan[0].step_id
    wm.begin_step(step_id)
    context = StepExecutor(wm, AsyncMock()).build_context(step_id)
    return runner, model, log, context


def _tools(model, index):
    return [t.name for t in model.generate.await_args_list[index].args[1]]


def _messages(model, index):
    return model.generate.await_args_list[index].args[0]


async def test_delegate_is_offered_only_after_the_first_round():
    runner, model, log, context = setup([call('a', 'read_file', path='a.py'), text('done')])
    output = await runner.run(context)
    assert output.content == 'done'
    assert DELEGATE not in _tools(model, 0)
    assert _tools(model, 1)[-1] == DELEGATE
    assert EXPLORE_INSTRUCTIONS in _messages(model, 0)[0].content


async def test_delegate_disappears_when_most_rounds_are_used():
    turns = [call(f'r{i}', 'read_file', path=f'{i}.py') for i in range(3)] + [text('done')]
    runner, model, log, context = setup(turns, loop_options=LoopOptions(max_tool_rounds=4, max_tool_calls=10))
    await runner.run(context)
    offered = [DELEGATE in _tools(model, i) for i in range(4)]
    # Rounds used before each request: 0, 1, 2, 3; offered while 1 <= rounds < 0.75 * 4.
    assert offered == [False, True, True, False]


async def test_a_delegate_call_before_it_is_offered_is_unavailable():
    runner, model, log, context = setup([call('d', DELEGATE, task='look around'), text('done')])
    await runner.run(context)
    reply = json.loads(_messages(model, 1)[-1].content)
    assert reply['success'] is False and 'not available' in reply['error']
    assert model.generate.await_count == 2      # no member was started


async def test_a_member_gets_a_fresh_read_only_context_and_reports_to_the_loop():
    events = []
    turns = [call('a', 'read_file', path='README.md'),
             call('d', DELEGATE, task='List the modules that read settings, at most 10 lines.',
                  thoroughness='quick'),
             # The member:
             call('m1', 'read_file', path='shop/db.py'),
             text('shop/db.py reads db.host'),
             # The main loop again:
             text('final answer')]
    runner, model, log, context = setup(turns)
    with using_hooks(HookSet((SimpleNamespace(observe=events.append),))):
        output = await runner.run(context)
    assert output.content == 'final answer'
    member = _messages(model, 2)
    assert 'read-only research member' in member[0].content
    assert 'List the modules that read settings' in member[0].content
    assert 'at most 4 tool rounds' in member[0].content          # quick
    assert 'call ask_user' not in member[0].content and 'exactly what information is missing' in member[0].content
    data = json.loads(member[1].content)
    assert data['active_goal'] == 'List the modules that read settings, at most 10 lines.'
    assert 'pinned_contexts' not in data and 'intermediate_results' not in data
    assert _tools(model, 2) == ['read_file', 'search_files', TODO]   # no write, ask_user or delegate
    report = json.loads(_messages(model, 4)[-1].content)
    assert report == {'member': 'explore-1', 'status': 'succeeded', 'result': 'shop/db.py reads db.host',
                      'files_read': ['shop/db.py'], 'tool_calls': 1, 'delegations_remaining': 1}
    delegate_events = [e for e in events if e['type'] == DELEGATE]
    assert [e['outcome'] for e in delegate_events] == ['started', 'succeeded']
    assert delegate_events[0]['thoroughness'] == 'quick'
    assert any(e['type'] == 'scope_start' and e.get('kind') == 'member' for e in events)
    # The member's tool calls are not the loop's own results.
    assert [c.call_id for c in output.tool_calls] == ['a']


@pytest.mark.parametrize('arguments, reason', [
    ({'task': 'x', 'thoroughness': 'deep'}, 'thoroughness'),
    ({'task': '   '}, 'task'),
    ({'task': 'x' * 4001}, 'task_length'),
])
async def test_bad_requests_are_refused_without_a_member(arguments, reason):
    events = []
    model = AsyncMock()
    model.generate.side_effect = AssertionError('no member may start')
    registry = ToolRegistry()
    registry.register(_tool('read_file', []))
    explore = ExploreDelegation(model, registry)
    with using_hooks(HookSet((SimpleNamespace(observe=events.append),))):
        reply = await explore.delegate(SimpleNamespace(), arguments)
    assert reply.content.startswith('HOST: ') and reply.answer is None
    assert explore.delegations == 0 and events[-1]['outcome'] == 'refused' and events[-1]['reason'] == reason


async def test_the_limit_holds_across_bindings_of_one_task():
    tally = {'delegations': 2}
    turns = [call('a', 'read_file', path='a.py'), call('d', DELEGATE, task='find it'), text('done')]
    runner, model, log, context = setup(turns, tally=tally)
    await runner.run(context)
    reply = _messages(model, 2)[-1].content
    assert reply.startswith('HOST: all 2 delegations') and model.generate.await_count == 3
    assert tally == {'delegations': 2}


async def test_a_failed_member_reports_only_the_exception_type():
    turns = [call('a', 'read_file', path='a.py'), call('d', DELEGATE, task='find it'),
             text(''), text(''), text(''),           # the member never answers
             text('done')]
    runner, model, log, context = setup(turns)
    output = await runner.run(context)
    report = json.loads(_messages(model, 5)[-1].content)
    assert output.content == 'done'
    assert report['status'] == 'error' and report['failure'] == 'StepFormatError'
    assert 'result' not in report and report['delegations_remaining'] == 1
    assert runner.format_repair_count == 2       # the member's empty-reply nudges


async def test_member_calls_are_marked_in_the_meter_and_counted_by_the_bench():
    from bench.run_bench import usage_fields
    turns = [call('a', 'read_file', path='a.py'), call('d', DELEGATE, task='find it', thoroughness='medium'),
             text('member conclusion'), text('done')]
    runner, model, log, context = setup([])

    async def generate(messages, tools):
        record_model_call(SimpleNamespace(prompt_tokens=10, completion_tokens=1))
        return turns.pop(0)
    model.generate.side_effect = generate
    with metering(RunMeter()) as meter:
        await runner.run(context)
    assert [e.get('member') for e in meter.call_log] == [None, None, 'explore-1', None]
    assert 'at most 8 tool rounds' in _messages(model, 2)[0].content    # medium
    fields = usage_fields({'usage': meter.snapshot()})
    assert (fields['delegations'], fields['delegate_requests'], fields['delegate_calls']) == (1, 1, 1)
    assert fields['calls'][2]['member'] == 'explore-1'
    plain = usage_fields({'usage': {'calls': [{'prompt': 1, 'completion': 1, 'requested': ['read_file']}]}})
    assert 'delegations' not in plain


async def test_a_member_that_runs_too_long_is_stopped():
    import asyncio
    turns = [call('a', 'read_file', path='a.py'), call('d', DELEGATE, task='find it')]
    runner, model, log, context = setup([], options=ExploreOptions(member_timeout_seconds=0.05))

    async def generate(messages, tools):
        if turns:
            return turns.pop(0)
        if 'research member' in messages[0].content:
            await asyncio.sleep(5)
        return text('done')
    model.generate.side_effect = generate
    output = await runner.run(context)
    report = json.loads(_messages(model, 3)[-1].content)
    assert output.content == 'done' and report['status'] == 'error' and report['failure'] == 'TimeoutError'


async def test_without_delegate_the_loop_request_is_unchanged():
    """The plain loop's tools and messages do not depend on this feature."""
    turns = [call('a', 'read_file', path='a.py'), text('done')]
    runner, model, log, context = setup(list(turns))
    plain_model = AsyncMock()
    plain_model.generate.side_effect = list(turns)
    plain = AgentLoopRunner(plain_model, runner.loop._registry)
    await plain.run(context)
    first = plain_model.generate.await_args_list[0]
    assert [t.name for t in first.args[1]] == ['read_file', 'search_files', 'write_file', TODO, ASK_USER]
    assert DELEGATE not in first.args[0][0].content and EXPLORE_INSTRUCTIONS not in first.args[0][0].content


class _Planner:
    async def generate(self, prompt):
        raise AssertionError('the loop has no planning model call')


async def test_the_service_offers_delegate_only_when_enabled():
    from app.services.rag_agent_service import RagAgentService
    for enabled in (False, True):
        model = AsyncMock()
        model.generate.side_effect = [call('c', 'get_current_time', timezone='UTC'), text('noon')]
        async with RagAgentService(model=model, planner_model=_Planner(), mcp_servers=(), include_knowledge=False,
                                   memory_directory='', single_runner='loop', delegate=enabled) as service:
            result = await service.query_result('What time is it?', 's1')
        assert result.content == 'noon'
        second = [t.name for t in model.generate.await_args_list[1].args[1]]
        assert (DELEGATE in second) is enabled
        assert DELEGATE not in [t.name for t in model.generate.await_args_list[0].args[1]]
    with pytest.raises(TypeError):
        RagAgentService(model=AsyncMock(), mcp_servers=(), delegate='yes')


def test_the_entry_points_and_the_bench_pass_the_flag(monkeypatch):
    from app.agent.__main__ import parse_args
    from app.api.__main__ import build_parser
    from bench import run_bench
    assert parse_args(['goal', '--delegate']).delegate is True and parse_args(['goal']).delegate is False
    assert build_parser().parse_args(['--delegate']).delegate is True
    task = next(t for t in run_bench.load_tasks(['all']) if t['id'] == 'billing_qa')
    seen = []

    def launch(cmd, env, timeout):
        seen.append(cmd)
        return SimpleNamespace(stdout='{}\n\nStatus: completed; executions: 1; replans: 0\n', stderr='')
    monkeypatch.setattr(run_bench, 'launch_agent', launch)
    row = run_bench.run_one(task, 'single', 1, 5, delegate=True)
    assert '--delegate' in seen[-1] and row['delegate'] is True
    row = run_bench.run_one(task, 'single', 1, 5)
    assert '--delegate' not in seen[-1] and 'delegate' not in row
    run_bench.run_one(task, 'team', 1, 5, delegate=True)
    assert '--delegate' not in seen[-1]


def test_explore_options_are_checked():
    for bad in ({'max_delegations': 0}, {'member_timeout_seconds': 0}, {'max_round_share': 1.5},
                {'max_task_chars': 1.0}):
        with pytest.raises(ValueError):
            ExploreOptions(**bad)
    with pytest.raises(TypeError):
        ExploreDelegation(AsyncMock(), ToolRegistry(), options={'max_delegations': 1})
