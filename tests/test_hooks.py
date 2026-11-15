"""Hooks around tasks, model calls, tool calls and steps (D-058, D-059)."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from app.agent.hooks import (
    ALLOW, Decision, HookSet, HookedModel, HookedTool, HookedToolRegistry, PolicyDeniedError,
    summarize_arguments, using_hooks,
)
from app.agent.step_models import ModelTurn, ToolCall, ToolDefinition, ToolOutput, ToolsShownOnly, UserMessage
from app.models.taskstate import TaskStatus
from app.services.rag_agent_service import RagAgentService
from app.tools.tool_registry import ToolRegistry


class Recorder:
    name = 'recorder'

    def __init__(self):
        self.events = []

    def observe(self, event):
        self.events.append(event)

    def types(self):
        return [e['type'] for e in self.events if not e['type'].startswith('scope_')]


def untraced(event):
    """An event without its span ids (D-073), which tests/test_trace.py checks."""
    return {k: v for k, v in event.items() if k not in ('span_id', 'parent_id')}


class DenyTool:
    name, events = 'deny-tool', frozenset({'before_tool'})

    async def check(self, event):
        return Decision.deny('not on this page') if event['tool'] == 'get_current_time' else ALLOW


class DenyModel:
    name, events = 'cap', frozenset({'before_model'})

    async def check(self, event):
        return Decision.deny('cost cap reached')


def outcome(text):
    return ModelTurn(text)


def service(tmp_path, turns, hooks, steps=('Answer',)):
    model = AsyncMock()
    model.generate.side_effect = turns
    return RagAgentService(model=model, mcp_servers=(), include_knowledge=False,
                           memory_directory=tmp_path, hooks=hooks), model


TIME_CALL = ModelTurn(tool_calls=(ToolCall('c1', 'get_current_time', {}),))


async def test_events_come_in_order_and_carry_no_model_or_tool_text(tmp_path):
    recorder = Recorder()
    svc, model = service(tmp_path, [TIME_CALL, outcome('secret answer')], HookSet([recorder]))
    async with svc:
        result = await svc.query_result('What time is it?', 's')
    assert result.status is TaskStatus.COMPLETED
    assert recorder.types() == ['task_start', 'plan', 'before_model', 'after_model', 'before_tool',
                                'after_tool', 'before_model', 'after_model', 'step', 'task_end']
    by_type = {e['type']: e for e in recorder.events}
    assert untraced(by_type['before_tool']) == {'type': 'before_tool', 'tool': 'get_current_time', 'call_id': 'c1',
                                      'side_effect': False, 'args_summary': {}}
    assert by_type['after_tool']['success'] is True and by_type['after_tool']['chars'] > 0
    calls = [e for e in recorder.events if not e['type'].startswith('scope_')]
    first_model = calls[2]
    assert first_model['purpose'] == 'step' and first_model['callable_tools'] > 0
    assert calls[3]['tool_calls'] == 1
    assert by_type['task_end']['status'] == 'completed' and by_type['task_end']['task_id'] == result.task_id
    text = json.dumps(recorder.events, ensure_ascii=False)
    assert 'secret answer' not in text and 'What time' not in text


async def test_a_failing_observer_never_changes_the_result(tmp_path):
    class Broken:
        name = 'broken'

        def observe(self, event):
            raise RuntimeError('gone')
    svc, _ = service(tmp_path, [TIME_CALL, outcome('42')], HookSet([Broken()]))
    async with svc:
        result = await svc.query_result('What?', 's')
    assert result.status is TaskStatus.COMPLETED and result.content == '42'


async def test_a_denied_tool_is_not_run_and_the_model_is_told(tmp_path):
    recorder = Recorder()
    svc, model = service(tmp_path, [TIME_CALL, outcome('could not read the clock')],
                         HookSet([recorder], [DenyTool()]))
    async with svc:
        result = await svc.query_result('What time is it?', 's')
    assert result.status is TaskStatus.COMPLETED
    told = json.loads(model.generate.call_args_list[1].args[0][-1].content)
    assert told['success'] is False and told['error'] == 'not executed: not on this page'
    after = [e for e in recorder.events if e['type'] == 'after_tool']
    assert [untraced(e) for e in after] == [{'type': 'after_tool', 'tool': 'get_current_time', 'call_id': 'c1', 'side_effect': False,
                      'success': False, 'chars': 0, 'seconds': 0.0, 'denied_by': 'deny-tool'}]
    assert result.step_log[0]['tools'][0]['success'] is False


async def test_a_denied_model_call_reaches_no_provider_and_fails_the_run(tmp_path):
    recorder = Recorder()
    svc, model = service(tmp_path, [outcome('never')], HookSet([recorder], [DenyModel()]))
    async with svc:
        result = await svc.query_result('Hi', 's')
    assert result.status is TaskStatus.FAILED
    model.generate.assert_not_awaited()
    denied = [e for e in recorder.events if e['type'] == 'after_model']
    assert denied and denied[0]['denied_by'] == 'cap' and denied[0]['success'] is False


@pytest.mark.parametrize('behaviour', ['raise', 'hang', 'nonsense'])
async def test_a_gate_that_fails_counts_as_a_denial(behaviour):
    class Bad:
        name, events = 'bad', frozenset({'before_tool'})

        async def check(self, event):
            if behaviour == 'raise':
                raise RuntimeError('broken')
            if behaviour == 'hang':
                await asyncio.sleep(10)
            return 'yes'
    hooks = HookSet(gates=[Bad()], gate_timeout=0.01)
    decision, by = await hooks.gate({'type': 'before_tool', 'tool': 'x'})
    assert decision.allow is False and by == 'bad'


def test_gates_run_only_before_model_and_tool_calls():
    class Late:
        name, events = 'late', frozenset({'after_tool'})

        async def check(self, event):
            return ALLOW
    with pytest.raises(ValueError):
        HookSet(gates=[Late()])
    with pytest.raises(TypeError):
        HookSet(observers=[object()])


class Echo:
    definition = ToolDefinition('write_file', 'Write', {'type': 'object'})

    def __init__(self):
        self.calls = 0

    async def invoke(self, arguments):
        self.calls += 1
        return ToolOutput(True, 'written')


async def test_a_tool_copied_into_another_registry_still_passes_the_hooks():
    # Team members build their own registries from tool objects taken out of the task's registry.
    task_registry, echo, recorder = HookedToolRegistry(), Echo(), Recorder()
    task_registry.register(echo)
    member = ToolRegistry()
    member.register(task_registry.get('write_file'))
    assert isinstance(member.get('write_file'), HookedTool)
    with using_hooks(HookSet([recorder])):
        await member.invoke(ToolCall('m1', 'write_file', {'path': 'out/a.txt', 'content': 'x' * 50}))
    assert recorder.types() == ['before_tool', 'after_tool']
    assert recorder.events[0]['call_id'] == 'm1' and recorder.events[0]['side_effect'] is True
    assert recorder.events[0]['args_summary'] == {'path': 'out/a.txt', 'content': {'chars': 50}}
    # Registering an already wrapped tool does not wrap it twice.
    again = HookedToolRegistry()
    again.register(task_registry.get('write_file'))
    assert not isinstance(again.get('write_file')._tool, HookedTool)


async def test_an_explicit_empty_hookset_passes_calls_straight_through():
    from app.agent.hooks import EMPTY, current_hooks, installed_hooks
    echo = Echo()
    inner = AsyncMock()
    inner.generate.return_value = ModelTurn('ok')
    with using_hooks(EMPTY):
        assert installed_hooks() is EMPTY and current_hooks() is EMPTY
        assert (await HookedTool(echo).invoke({})).content == 'written' and echo.calls == 1
        messages, tools = (UserMessage('hi'),), ()
        assert (await HookedModel(inner).generate(messages, tools)).content == 'ok'
    inner.generate.assert_awaited_once_with(messages, tools)
    assert installed_hooks() is None and current_hooks() is EMPTY


async def test_model_calls_are_labelled_step_and_wrap_up():
    inner, recorder = AsyncMock(), Recorder()
    inner.generate.return_value = ModelTurn('ok')
    model = HookedModel(inner)
    read = ToolDefinition('read_file', 'Read', {'type': 'object'})
    with using_hooks(HookSet([recorder])):
        await model.generate((UserMessage('hi'),), (read,))
        await model.generate((UserMessage('hi'),), ToolsShownOnly((read,)))
    before = [e for e in recorder.events if e['type'] == 'before_model']
    assert [(e['purpose'], e['callable_tools']) for e in before] == [('step', 1), ('wrap_up', 0)]
    with using_hooks(HookSet(gates=[DenyModel()])):
        with pytest.raises(PolicyDeniedError):
            await model.generate((UserMessage('hi'),), ())


def test_argument_summaries_keep_locations_and_bounds_only():
    summary = summarize_arguments({'path': 'a.py', 'start_line': 3, 'pattern': 'password=', 'query': 'q',
                                   'content': 'body', 'paths': ['a', 'b'], 'glob': 'x' * 300})
    assert summary == {'path': 'a.py', 'start_line': 3, 'pattern': {'chars': 9}, 'query': {'chars': 1},
                       'content': {'chars': 4}, 'paths': {'items': 2}, 'glob': {'chars': 300}}


def test_the_service_refuses_something_that_is_not_a_hook_set(tmp_path):
    with pytest.raises(TypeError):
        RagAgentService(model=AsyncMock(), mcp_servers=(), include_knowledge=False,
                        memory_directory=tmp_path, hooks=[Recorder()])


@pytest.mark.parametrize('failure', ['error', 'timeout'])
async def test_before_finish_is_a_gate_and_fails_closed(failure, caplog):
    from types import SimpleNamespace
    from app.agent.hooks import BEFORE_FINISH, EVENT_TYPES, GATE_EVENTS
    assert BEFORE_FINISH in EVENT_TYPES & GATE_EVENTS
    async def check(event):
        if failure == 'timeout':
            await asyncio.sleep(10)
        raise ValueError('private provider text')
    hooks = HookSet(gates=(SimpleNamespace(name='finish', events={BEFORE_FINISH}, check=check),),
                    gate_timeout=0.001)
    decision, name = await hooks.gate({'type': BEFORE_FINISH})
    assert not decision.allow and name == 'finish'
    assert 'private provider text' not in caplog.text


@pytest.mark.parametrize('required,review,writes,passed,approved,allowed', [
    (True, False, 0, False, False, True),
    (True, False, 1, False, False, False),
    (True, False, 1, True, False, True),
    (False, False, 1, False, False, True),
    (False, True, 0, False, False, False),
    (True, True, 1, True, False, False),
    (True, True, 1, True, True, True),
])
async def test_passing_run_gate_uses_only_this_runs_host_evidence(required, review, writes, passed, approved, allowed):
    from app.agent.builtin_hooks import PassingRunGate
    gate = PassingRunGate(require_passing_run=required, review=review)
    decision = await gate.check({'writes': writes, 'passing_run_after_last_write': passed,
                                 'review_after_last_write': approved})
    assert decision.allow is allowed


@pytest.mark.parametrize('shown_only', [False, True])
async def test_uninstalled_hooks_refuse_model_without_logging_messages(caplog, shown_only):
    from app.agent.hooks import EMPTY, current_hooks, installed_hooks, model_purpose
    inner = AsyncMock()
    assert installed_hooks() is None and current_hooks() is EMPTY
    tools = ToolsShownOnly(()) if shown_only else ()
    purpose = 'wrap_up' if shown_only else 'route'
    with caplog.at_level('WARNING', logger='app.agent.hooks'), model_purpose('route'):
        with pytest.raises(PolicyDeniedError) as caught:
            await HookedModel(inner).generate((UserMessage('PRIVATE-MESSAGE'),), tools)
    inner.generate.assert_not_awaited()
    assert caught.value.hook == 'hooks_not_installed'
    reason = caught.value.reason
    for fragment in (purpose, 'no hooks are installed for this run', 'approval gates',
                     'conversation token cap', 'audit log', 'RagAgentService', 'using_hooks(...)', 'EMPTY'):
        assert fragment in reason
    assert len(caplog.records) == 1 and reason in caplog.text
    assert 'PRIVATE-MESSAGE' not in caplog.text


async def test_uninstalled_hooks_refuse_tool_without_logging_arguments(caplog):
    from types import SimpleNamespace
    inner = SimpleNamespace(definition=ToolDefinition('write_file', 'write', {}), invoke=AsyncMock())
    with caplog.at_level('WARNING', logger='app.agent.hooks'):
        output = await HookedTool(inner).invoke({'path': 'PRIVATE-PATH', 'content': 'PRIVATE-CONTENT'})
    inner.invoke.assert_not_awaited()
    assert output.success is False and output.content == ''
    assert output.error.startswith('not executed: ')
    for fragment in ('write_file', 'no hooks are installed for this run', 'approval gates',
                     'conversation token cap', 'audit log', 'RagAgentService', 'using_hooks(...)', 'EMPTY'):
        assert fragment in output.error
    assert len(caplog.records) == 1 and output.error.removeprefix('not executed: ') in caplog.text
    assert 'PRIVATE-PATH' not in caplog.text and 'PRIVATE-CONTENT' not in caplog.text


@pytest.mark.parametrize('with_callback', [False, True])
async def test_loop_run_without_hooks_fails_with_detailed_hooks_reason(with_callback):
    from app.agent.agent_loop import AgentLoopRunner, LOOP_STEP
    from app.agent.loop_run import LoopRun
    from app.agent.runtime import FAILURE_MESSAGES
    from app.models.run_state import RunMemory, RunState
    inner = AsyncMock()
    runtime = LoopRun(RunMemory(RunState('user', 'answer')), HookedModel(inner), ToolRegistry(),
                      runner_factory=lambda deps: AgentLoopRunner(deps.model, deps.tool_registry),
                      step_description=LOOP_STEP)
    events = []
    result = await runtime.run(on_event=events.append if with_callback else None)
    assert [e['type'] for e in events] == (['plan', 'step'] if with_callback else [])
    assert result.status is TaskStatus.FAILED and result.failure_kind == 'hooks'
    for fragment in ('step', 'no hooks are installed for this run', 'RagAgentService', 'using_hooks(...)', 'EMPTY'):
        assert fragment in result.content
    assert result.user_message == FAILURE_MESSAGES['hooks']
    inner.generate.assert_not_awaited()


async def test_service_without_hook_configuration_installs_hooks(tmp_path, monkeypatch):
    from app.config import config
    from app.agent.hooks import EMPTY, installed_hooks
    monkeypatch.setattr(config, 'hooks_audit', False)
    monkeypatch.setattr(config, 'conversation_max_tokens', None)
    seen = []
    async def generate(messages, tools):
        seen.append(installed_hooks())
        return TIME_CALL if len(seen) == 1 else outcome('done')
    model = AsyncMock()
    model.generate.side_effect = generate
    async with RagAgentService(model=model, mcp_servers=(), include_knowledge=False,
                               memory_directory=tmp_path) as svc:
        result = await svc.query_result('What time is it?', 's')
    assert result.status is TaskStatus.COMPLETED and result.content == 'done'
    assert seen == [EMPTY, EMPTY]
    assert result.step_log[0]['tools'][0]['success'] is True
    assert installed_hooks() is None
