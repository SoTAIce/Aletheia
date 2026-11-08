"""Stage 1 trial: one agent loop for the whole task, entered through a fixed one-step plan (loop-trial-001)."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.agent_loop import (ASK_USER, TODO, AgentLoopRunner, LoopOptions, LoopPlanner, _context_message)
from app.agent.hooks import HookSet, using_hooks
from app.agent.step_executor import StepExecutor
from app.agent.step_models import (ModelTurn, StepOutcome, SystemMessage, ToolCall, ToolDefinition, ToolOutput,
                                   ToolsShownOnly)
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
    assert runner.todo == items and events == [{'type': 'todo', 'items': items}]


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
    assert 'Paris' in resumed[1].content          # the answer is in resolved_questions
    assert second.execution_count == 2 and second.replan_count == 0


def test_the_service_checks_the_runner_choice():
    with pytest.raises(ValueError):
        RagAgentService(model=AsyncMock(), mcp_servers=(), single_runner='steps')
    with pytest.raises(TypeError):
        RagAgentService(model=AsyncMock(), mcp_servers=(), single_runner='loop', loop_options={'max_tool_rounds': 3})


def test_the_bench_passes_the_runner_to_single_runs_only(monkeypatch):
    from bench import run_bench
    task = next(t for t in run_bench.load_tasks(['all']) if t['id'] == 'billing_qa')
    seen = []

    def launch(cmd, env, timeout):
        seen.append(cmd)
        return SimpleNamespace(stdout='{}\n\nStatus: completed; executions: 1; replans: 0\n', stderr='')
    monkeypatch.setattr(run_bench, 'launch_agent', launch)
    row = run_bench.run_one(task, 'single', 1, 5, single_runner='loop')
    assert seen[-1][seen[-1].index('--single-runner') + 1] == 'loop' and row['runner'] == 'loop'
    run_bench.run_one(task, 'auto', 1, 5, single_runner='loop')
    assert '--single-runner' not in seen[-1]


def test_the_cli_accepts_the_runner_flag():
    from app.agent.__main__ import parse_args
    assert parse_args(['goal', '--single-runner', 'loop']).single_runner == 'loop'
    assert parse_args(['goal']).single_runner == 'plan'
