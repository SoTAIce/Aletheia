"""Stage 1 trial: one agent loop for the whole task, entered through a fixed one-step plan (loop-trial-001)."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.agent_loop import (ASK_USER, TODO, AgentLoopRunner, LoopOptions, LoopPlanner, _context_message)
from app.agent.hooks import HookSet, using_hooks
from app.agent.step_executor import StepExecutor
from app.agent.step_models import (ModelTurn, StepOutcome, SystemMessage, ToolCall, ToolDefinition, ToolMessage,
                                   ToolOutput, ToolsShownOnly)
from app.agent.step_runner import StepFormatError, StepRunError, StepToolError, ToolRoundLimitError
from app.models.taskstate import TaskState, TaskStatus
from app.models.working_memory import WorkingMemory
from app.services.rag_agent_service import RagAgentService
from app.tools.tool_registry import ToolRegistry


def call(call_id, name='read', **arguments):
    return ModelTurn(tool_calls=(ToolCall(call_id, name, arguments or {'part': call_id}),))


def text(content):
    return ModelTurn(content)


def setup(turns, *, options=None, invoke=None):
    model = AsyncMock()
    model.generate.side_effect = turns
    read = SimpleNamespace(definition=ToolDefinition('read', 'read', {'type': 'object'}),
                           invoke=AsyncMock(side_effect=invoke or (lambda args: ToolOutput(True, f'text of {args}'))))
    registry = ToolRegistry()
    registry.register(read)
    runner = AgentLoopRunner(model, registry, options)
    wm = WorkingMemory(TaskState('user', 'explain the module'))
    wm.task_state.pin_content('[{"name": "read"}]', 'available_tools')
    wm.task_state.pin_content('earlier turns', 'conversation_memory')
    wm.task_state.start_planning()
    wm.install_plan([LoopPlanner.STEP])
    step_id = wm.task_state.plan[0].step_id
    wm.begin_step(step_id)
    context = StepExecutor(wm, AsyncMock()).build_context(step_id)
    return runner, model, read, context


async def test_plain_text_after_tools_is_the_answer():
    runner, model, read, context = setup([call('a'), text('the module parses dates')])
    output = await runner.run(context)
    assert output.outcome is StepOutcome.SUCCEEDED and output.content == 'the module parses dates'
    assert [c.call_id for c in output.tool_calls] == ['a'] and read.invoke.await_count == 1
    tools = model.generate.await_args_list[0].args[1]
    assert [t.name for t in tools] == ['read', TODO, ASK_USER]


def test_the_context_skips_the_duplicated_tool_list_and_empty_fields():
    runner, model, read, context = setup([])
    data = json.loads(_context_message(context))
    assert [p['source'] for p in data['pinned_contexts']] == ['conversation_memory']
    assert data['active_goal'] == 'explain the module'
    assert 'current_step' not in data and 'intermediate_results' not in data


async def test_todo_is_kept_by_the_loop_and_reported_to_observers():
    events = []
    observer = SimpleNamespace(observe=events.append)
    items = [{'content': 'read the module', 'status': 'in_progress'}, {'content': 'answer', 'status': 'pending'}]
    runner, model, read, context = setup([call('t', TODO, items=items), text('done')])
    with using_hooks(HookSet((observer,))):
        output = await runner.run(context)
    assert output.content == 'done' and output.tool_calls == () and read.invoke.await_count == 0
    assert runner.todo == items and events == [{'type': 'todo', 'items': items, 'parent_id': None}]


async def test_a_bad_todo_is_answered_with_an_error():
    runner, model, read, context = setup([call('t', TODO, items=[{'content': 'x', 'status': 'later'}]), text('ok')])
    await runner.run(context)
    reply = json.loads(model.generate.await_args_list[1].args[0][-1].content)
    assert reply['success'] is False and 'status' in reply['error'] and runner.todo == []


async def test_ask_user_blocks_with_the_question():
    runner, model, read, context = setup([call('q', ASK_USER, question='Which file?')])
    output = await runner.run(context)
    assert output.outcome is StepOutcome.BLOCKED and output.reason == output.content == 'Which file?'


async def test_a_spent_budget_asks_once_for_the_answer_then_fails():
    options = LoopOptions(max_tool_rounds=1)
    runner, model, read, context = setup([call('a'), call('b'), text('from a')], options=options)
    output = await runner.run(context)
    assert output.content == 'from a' and read.invoke.await_count == 1
    last = model.generate.await_args_list[-1]
    assert isinstance(last.args[1], ToolsShownOnly)
    assert isinstance(last.args[0][-1], SystemMessage) and 'no more tools' in last.args[0][-1].content

    runner, model, read, context = setup([call('a'), call('b'), call('c')], options=options)
    with pytest.raises(ToolRoundLimitError):
        await runner.run(context)


async def test_an_unavailable_tool_is_answered_then_bounded():
    runner, model, read, context = setup([call('x', 'write_file'), text('answered')])
    output = await runner.run(context)
    assert output.content == 'answered'
    reply = json.loads(model.generate.await_args_list[1].args[0][-1].content)
    assert 'not available' in reply['error']
    runner, model, read, context = setup([call('x', 'nope'), call('y', 'nope'), call('z', 'nope')])
    with pytest.raises(StepRunError, match='unavailable tool: nope'):
        await runner.run(context)


async def test_a_raising_tool_is_a_step_tool_error():
    def boom(args):
        raise OSError('C:/secret/path')
    runner, model, read, context = setup([call('a')], invoke=boom)
    with pytest.raises(StepToolError) as info:
        await runner.run(context)
    assert 'secret' not in info.value.message and 'OSError' in info.value.message


async def test_empty_replies_are_nudged_then_fail():
    runner, model, read, context = setup([text(''), text('fine')])
    assert (await runner.run(context)).content == 'fine'
    runner, model, read, context = setup([text(''), text(' '), text('')])
    with pytest.raises(StepFormatError):
        await runner.run(context)


def test_the_prompt_keeps_todo_for_long_work_and_never_alone():
    from app.agent.agent_loop import CONTROL_DEFINITIONS, _system
    system = ' '.join(_system(LoopOptions()).split())
    assert 'more than three distinct parts' in system and 'never alone' in system
    assert 'just before the final answer' in system
    assert 'never alone' in CONTROL_DEFINITIONS[0].description


async def test_each_logged_call_records_the_tools_the_model_asked_for():
    from app.agent.run_meter import RunMeter, metering, record_model_call
    items = [{'content': 'a', 'status': 'pending'}]
    turns = [ModelTurn(tool_calls=(ToolCall('t', TODO, {'items': items}), ToolCall('a', 'read', {}))),
             call('x', 'made_up_tool'), text('done')]
    runner, model, read, context = setup([])

    async def generate(messages, tools):
        record_model_call(SimpleNamespace(prompt_tokens=10, completion_tokens=1))
        return turns.pop(0)
    model.generate.side_effect = generate
    with metering(RunMeter()) as meter:
        await runner.run(context)
    assert [entry['requested'] for entry in meter.call_log] == [[TODO, 'read'], ['unavailable'], []]
    assert meter.snapshot()['calls'][0]['requested'] == [TODO, 'read']


def test_the_bench_counts_todo_calls_per_task():
    from bench.run_bench import usage_fields
    record = {'usage': {'model_calls': 3, 'calls': [
        {'prompt': 20000, 'cached': None, 'completion': 5, 'requested': ['todo']},
        {'prompt': 21000, 'cached': None, 'completion': 5, 'requested': ['todo', 'read']},
        {'prompt': 22000, 'cached': None, 'completion': 9, 'requested': []}]}}
    fields = usage_fields(record)
    assert fields['todo_calls'] == 2 and fields['todo_only_calls'] == 1
    assert fields['calls'][2] == {'prompt': 22000, 'completion': 9, 'requested': []}
    plain = usage_fields({'usage': {'calls': [{'prompt': 1, 'completion': 1}]}})
    assert 'todo_calls' not in plain and plain['calls'] == [{'prompt': 1, 'completion': 1}]


def _answered(context, question, answer, *, goal_revision=None):
    from dataclasses import replace
    from datetime import datetime, UTC
    from app.models.taskstate import OpenQuestion
    now = datetime.now(UTC)
    asked = OpenQuestion('q1', question, None, context.goal_revision, True, now, answer, now)
    return replace(context, resolved_questions=(asked,),
                   goal_revision=context.goal_revision if goal_revision is None else goal_revision)


async def test_a_blocked_loop_continues_with_its_messages_after_the_answer():
    from app.agent.agent_loop import LoopResumeStore
    store = LoopResumeStore()
    runner, model, read, context = setup([call('a'), ModelTurn(tool_calls=(
        ToolCall('q', ASK_USER, {'question': 'Which part?'}), ToolCall('b', 'read', {'part': 'b'})))],
        options=LoopOptions(max_tool_rounds=3))
    runner._store = store
    first = await runner.run(context)
    assert first.outcome is StepOutcome.BLOCKED and read.invoke.await_count == 1

    second = AgentLoopRunner(model, runner._registry, LoopOptions(max_tool_rounds=3), resume_store=store)
    model.generate.side_effect = [text('the parser')]
    output = await second.run(_answered(context, 'Which part?', 'the parser'))
    assert output.content == 'the parser' and second.resumed and read.invoke.await_count == 1
    sent = model.generate.await_args_list[-1].args[0]
    replies = {m.call_id: json.loads(m.content) for m in sent if isinstance(m, ToolMessage)}
    assert replies['a']['success'] and replies['q'] == {'success': True, 'content': 'the parser',
                                                        'error': None, 'source_refs': []}
    assert replies['b']['success'] is False and 'asked first' in replies['b']['error']
    assert output.tool_calls == ()            # the earlier call was reported by the first attempt


async def test_the_budget_counts_the_work_before_the_question():
    from app.agent.agent_loop import LoopResumeStore
    store = LoopResumeStore()
    options = LoopOptions(max_tool_rounds=2)
    runner, model, read, context = setup([call('a'), call('q', ASK_USER, question='Which?')], options=options)
    runner._store = store
    await runner.run(context)
    second = AgentLoopRunner(model, runner._registry, options, resume_store=store)
    model.generate.side_effect = [call('c'), text('from a')]
    output = await second.run(_answered(context, 'Which?', 'x'))
    assert output.content == 'from a' and read.invoke.await_count == 1   # two rounds were already used
    assert isinstance(model.generate.await_args_list[-2].args[1], ToolsShownOnly)


async def test_the_loop_starts_again_when_it_cannot_continue():
    from app.agent.agent_loop import LoopResumeStore
    for answered in (lambda c: c,                                              # no answer yet
                     lambda c: _answered(c, 'Another question?', 'x'),         # a different question
                     lambda c: _answered(c, 'Which?', 'x', goal_revision=c.goal_revision + 1)):   # new goal
        store = LoopResumeStore()
        runner, model, read, context = setup([call('a'), call('q', ASK_USER, question='Which?')])
        runner._store = store
        await runner.run(context)
        second = AgentLoopRunner(model, runner._registry, resume_store=store)
        model.generate.side_effect = [text('fresh')]
        assert (await second.run(answered(context))).content == 'fresh' and not second.resumed
        assert len(model.generate.await_args_list[-1].args[0]) == 2     # system and context only
        assert store.take(context) is None                              # taking it cleared it


class _Planner:
    async def generate(self, prompt):
        raise AssertionError('the loop has no planning model call')


async def test_the_service_runs_single_mode_as_one_loop_and_resumes_after_ask_user():
    model = AsyncMock()
    model.generate.side_effect = [call('c1', 'get_current_time', timezone='UTC'),
                                  call('q', ASK_USER, question='Which city?'),
                                  text('It is noon in Paris.')]
    events = []
    async with RagAgentService(model=model, planner_model=_Planner(), mcp_servers=(), include_knowledge=False,
                               memory_directory='', single_runner='loop') as service:
        first = await service.query_result('What time is it?', 's1', on_event=events.append)
        assert first.status is TaskStatus.BLOCKED and first.content == 'Which city?'
        assert [e['type'] for e in events] == ['route', 'plan', 'step']
        assert events[1]['steps'] == [LoopPlanner.STEP]
        second = await service.query_result('Paris', 's1', resume_task_id=first.task_id)
    assert second.status is TaskStatus.COMPLETED and second.content == 'It is noon in Paris.'
    resumed = model.generate.await_args_list[2].args[0]
    # The loop continued: the earlier time call is still there and the answer is the ask_user result.
    assert [type(m).__name__ for m in resumed] == ['SystemMessage', 'UserMessage', 'AssistantMessage',
                                                  'ToolMessage', 'AssistantMessage', 'ToolMessage']
    assert resumed[-1].call_id == 'q' and json.loads(resumed[-1].content)['content'] == 'Paris'
    assert model.generate.await_count == 3        # nothing was asked of the model twice
    assert second.execution_count == 2 and second.replan_count == 0


def test_the_service_checks_the_runner_choice():
    with pytest.raises(ValueError):
        RagAgentService(model=AsyncMock(), mcp_servers=(), single_runner='steps')
    with pytest.raises(TypeError):
        RagAgentService(model=AsyncMock(), mcp_servers=(), single_runner='loop', loop_options={'max_tool_rounds': 3})


def test_the_bench_passes_the_runner_to_single_and_auto_runs(monkeypatch):
    from bench import run_bench
    task = next(t for t in run_bench.load_tasks(['all']) if t['id'] == 'billing_qa')
    seen = []

    def launch(cmd, env, timeout):
        seen.append(cmd)
        return SimpleNamespace(stdout='{}\n\nStatus: completed; executions: 1; replans: 0\n', stderr='')
    monkeypatch.setattr(run_bench, 'launch_agent', launch)
    row = run_bench.run_one(task, 'single', 1, 5, single_runner='loop')
    assert seen[-1][seen[-1].index('--single-runner') + 1] == 'loop' and row['runner'] == 'loop'
    row = run_bench.run_one(task, 'single', 1, 5)
    assert seen[-1][seen[-1].index('--single-runner') + 1] == 'loop' and row['runner'] == 'loop'
    row = run_bench.run_one(task, 'single', 1, 5, single_runner='plan')
    assert seen[-1][seen[-1].index('--single-runner') + 1] == 'plan' and row['runner'] == 'plan'
    run_bench.run_one(task, 'auto', 1, 5, single_runner='plan')
    assert seen[-1][seen[-1].index('--single-runner') + 1] == 'plan'
    run_bench.run_one(task, 'team', 1, 5)
    assert '--single-runner' not in seen[-1]


def test_the_cli_accepts_the_runner_flag():
    from app.agent.__main__ import parse_args
    assert parse_args(['goal', '--single-runner', 'plan']).single_runner == 'plan'
    assert parse_args(['goal']).single_runner == 'loop'


async def test_host_tool_can_finish_without_registry_or_another_model_call():
    from app.agent.agent_loop import HostTool, HostToolResult
    runner, model, read, context = setup([call('h', 'delegate')])
    host = HostTool(ToolDefinition('delegate', 'delegate', {'type': 'object'}),
                    AsyncMock(return_value=HostToolResult('report', answer='member answer')))
    runner = AgentLoopRunner(model, runner._registry, host_tools=(host,))
    output = await runner.run(context)
    assert output.content == 'member answer' and model.generate.await_count == 1
    assert read.invoke.await_count == 0 and output.tool_calls == ()
    assert 'delegate' in [d.name for d in model.generate.await_args.args[1]]
    assert 'delegate' not in [d.name for d in runner._registry.list_definitions()]


async def test_without_ask_user_the_prompt_says_to_name_what_is_missing():
    """Lead and members cannot ask the user: the prompt must not mention ask_user or a lead above them."""
    runner, model, read, context = setup([text('answer')])
    runner = AgentLoopRunner(model, runner._registry, allow_ask_user=False)
    await runner.run(context)
    messages, tools = model.generate.await_args.args
    system = messages[0].content
    assert 'call ask_user' not in system and 'what information is missing' in system
    assert ASK_USER not in [d.name for d in tools]


async def test_no_host_tools_preserves_messages_and_controls():
    runner, model, read, context = setup([call('a'), text('answer')])
    events = []
    with using_hooks(HookSet((SimpleNamespace(observe=events.append),))):
        output = await runner.run(context)
    assert output.content == 'answer' and output.tool_calls[0].call_id == 'a'
    assert [d.name for d in model.generate.await_args.args[1]] == ['read', TODO, ASK_USER]
    assert events == []


@pytest.mark.parametrize('twice', [False, True])
async def test_before_finish_denial_prompts_once_then_fails(twice):
    from app.agent.hooks import ALLOW, Decision
    from app.agent.team.models import TeamError
    check = AsyncMock(side_effect=[Decision.deny('run required'),
                                   Decision.deny('run required') if twice else ALLOW])
    gate = SimpleNamespace(name='finish', events=frozenset({'before_finish'}), check=check)
    runner, model, read, context = setup([text('private draft'), text('final')])
    with using_hooks(HookSet(gates=(gate,))):
        if twice:
            with pytest.raises(TeamError, match='team acceptance gate not met'):
                await runner.run(context)
        else:
            assert (await runner.run(context)).content == 'final'
    sent = model.generate.await_args_list[-1].args[0]
    assert 'HOST:' in sent[-1].content and 'run required' in sent[-1].content
    assert 'private draft' not in json.dumps(check.await_args.args[0])


async def test_host_calls_in_one_batch_obey_call_budget():
    from app.agent.agent_loop import HostTool, HostToolResult
    runner, model, read, context = setup([ModelTurn(tool_calls=(
        ToolCall('h1', 'delegate', {}), ToolCall('h2', 'delegate', {}))), text('answer')])
    invoke = AsyncMock(return_value=HostToolResult('report'))
    host = HostTool(ToolDefinition('delegate', 'delegate', {'type': 'object'}), invoke)
    runner = AgentLoopRunner(model, runner._registry, LoopOptions(max_tool_calls=1), host_tools=(host,))
    await runner.run(context)
    assert invoke.await_count == 1


async def test_host_finish_cannot_bypass_another_finish_gate():
    from app.agent.agent_loop import HostTool, HostToolResult
    from app.agent.hooks import Decision
    from app.agent.team.models import TeamError
    runner, model, read, context = setup([call('h', 'delegate'), text('again')])
    host = HostTool(ToolDefinition('delegate', 'delegate', {'type': 'object'}),
                    AsyncMock(return_value=HostToolResult('report', answer='member')))
    runner = AgentLoopRunner(model, runner._registry, host_tools=(host,))
    gate = SimpleNamespace(name='deny', events={'before_finish'},
                           check=AsyncMock(return_value=Decision.deny('host rule')))
    with using_hooks(HookSet(gates=(gate,))):
        with pytest.raises(TeamError, match='team acceptance gate not met'):
            await runner.run(context)
    assert model.generate.await_count == 2
