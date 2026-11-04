import asyncio
from contextlib import asynccontextmanager

import pytest

from app.agent.step_models import ModelTurn, ToolCall, ToolDefinition, ToolOutput, ToolMessage
from app.agent.team_poc.session import TeamSession, SessionError
from app.tools.tool_registry import ToolRegistry


class ScriptedModel:
    def __init__(self, *turns):
        self.turns = iter(turns)
        self.requests = []

    async def generate(self, messages, tools):
        self.requests.append(tuple(messages))
        response = next(self.turns)
        return await response(messages) if callable(response) else response


class ReadTool:
    definition = ToolDefinition('read', 'Read synthetic evidence', {'type': 'object'})

    def __init__(self, action=None):
        self.calls = []
        self.action = action

    async def invoke(self, args):
        self.calls.append(args)
        if self.action:
            await self.action()
        return ToolOutput(True, 'original-evidence-73')


def factory(tool=None, owners=None):
    @asynccontextmanager
    async def open_tools():
        if owners is not None:
            owners.append(('open', asyncio.current_task()))
        registry = ToolRegistry()
        if tool:
            registry.register(tool)
        try:
            yield registry
        finally:
            if owners is not None:
                owners.append(('close', asyncio.current_task()))
    return open_tools


def call(identity, name='read', **args):
    return ModelTurn(tool_calls=(ToolCall(identity, name, args),))


async def waiting(events, session):
    async with asyncio.timeout(2):
        while True:
            event = await events.get()
            if event.session_id == session.session_id and event.kind == 'coordination_requested':
                return event


async def test_session_continues_same_history_without_repeating_completed_tool():
    events, owners, read = asyncio.Queue(), [], ReadTool()
    model = ScriptedModel(call('read-1'), call('ask-1', 'request_coordination', issue='Need output format'),
                          ModelTurn('evidence 73 in JSON format'))
    member = TeamSession('worker', model, factory(read, owners), events, 'Read evidence then ask format')
    task = asyncio.create_task(member.run())
    try:
        event = await waiting(events, member)
        assert not task.done() and member.status == 'waiting'
        assert event.version == 1
        member.revise(1, 'Use JSON, preserve original evidence')
        result = await asyncio.wait_for(task, 2)
        assert result.version == 2 and len(read.calls) == 1
        last = model.requests[-1]
        assert any(isinstance(m, ToolMessage) and 'original-evidence-73' in m.content for m in last)
        assert any('Use JSON' in (m.content or '') for m in last)
        assert member.model_calls == 3 and member.tool_calls == 2
        assert owners == [('open', task), ('close', task)]
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_revision_during_model_wait_discards_old_tool_plan():
    entered, release = asyncio.Event(), asyncio.Event()
    async def old_plan(messages):
        entered.set()
        await release.wait()
        return call('obsolete')
    read = ReadTool()
    member = TeamSession('worker', ScriptedModel(old_plan, ModelTurn('new work')),
                         factory(read), asyncio.Queue(), 'old work')
    task = asyncio.create_task(member.run())
    await entered.wait()
    member.revise(1, 'new work')
    release.set()
    result = await asyncio.wait_for(task, 2)
    assert result.version == 2 and not read.calls
    assert any(e.kind == 'stale_model_discarded' for e in member.events)


async def test_revision_during_tool_keeps_result_and_skips_remaining_batch():
    entered, release = asyncio.Event(), asyncio.Event()
    async def hold():
        entered.set()
        await release.wait()
    tool = ReadTool(hold)
    batch = ModelTurn(tool_calls=(ToolCall('first', 'read', {}), ToolCall('second', 'read', {})))
    model = ScriptedModel(batch, ModelTurn('revised result'))
    member = TeamSession('worker', model, factory(tool), asyncio.Queue(), 'old')
    task = asyncio.create_task(member.run())
    await entered.wait()
    member.revise(1, 'new')
    release.set()
    await asyncio.wait_for(task, 2)
    replies = [m for m in model.requests[-1] if isinstance(m, ToolMessage)]
    assert [m.call_id for m in replies] == ['first', 'second']
    assert 'original-evidence-73' in replies[0].content
    assert 'superseded' in replies[1].content
    assert len(tool.calls) == 1


@pytest.mark.parametrize('where', ['waiting', 'model', 'tool'])
async def test_cancel_at_any_wait_closes_owner_resources(where):
    entered, owners = asyncio.Event(), []
    async def hang(*args):
        entered.set()
        await asyncio.Event().wait()
    model = ScriptedModel(call('ask', 'request_coordination', issue='help')) if where == 'waiting' else (
        ScriptedModel(hang) if where == 'model' else ScriptedModel(call('read')))
    events = asyncio.Queue()
    member = TeamSession('worker', model, factory(ReadTool(hang), owners), events, 'goal')
    task = asyncio.create_task(member.run())
    if where == 'waiting':
        await waiting(events, member)
    else:
        await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert member.status == 'cancelled'
    assert owners == [('open', task), ('close', task)]
    assert not any(e.kind == 'completed' for e in member.events)


async def test_swallowed_cancellation_cannot_publish_success():
    entered = asyncio.Event()
    async def swallow(messages):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return ModelTurn('late success')
    member = TeamSession('worker', ScriptedModel(swallow), factory(), asyncio.Queue(), 'goal')
    task = asyncio.create_task(member.run())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert member.status == 'cancelled'


async def test_model_budget_survives_revision_and_waiting():
    events = asyncio.Queue()
    model = ScriptedModel(call('ask', 'request_coordination', issue='help'), call('read'))
    member = TeamSession('worker', model, factory(ReadTool()), events, 'goal', max_model_calls=1)
    task = asyncio.create_task(member.run())
    await waiting(events, member)
    member.revise(1, 'continue')
    with pytest.raises(SessionError, match='model.*limit'):
        await asyncio.wait_for(task, 2)
    assert member.model_calls == 1


async def test_duplicate_call_ids_and_mixed_report_batch_execute_no_new_tools():
    for turns in [
        (call('same'), call('same')),
        (ModelTurn(tool_calls=(ToolCall('ask', 'request_coordination', {'issue': 'help'}),
                               ToolCall('read', 'read', {}))),),
    ]:
        tool = ReadTool()
        member = TeamSession('worker', ScriptedModel(*turns), factory(tool), asyncio.Queue(), 'goal')
        with pytest.raises(SessionError):
            await member.run()
        assert len(tool.calls) == (1 if len(turns) == 2 else 0)


async def test_revision_validation_and_single_run():
    member = TeamSession('worker', ScriptedModel(ModelTurn('done')), factory(), asyncio.Queue(), 'goal')
    with pytest.raises(SessionError):
        member.revise(0, 'stale')
    await member.run()
    with pytest.raises(SessionError):
        member.revise(1, 'too late')
    with pytest.raises(SessionError):
        await member.run()


async def test_no_revision_admitted_while_completed_result_resources_are_closing():
    closing, release = asyncio.Event(), asyncio.Event()
    @asynccontextmanager
    async def tools():
        try:
            yield ToolRegistry()
        finally:
            closing.set()
            await release.wait()
    member = TeamSession('worker', ScriptedModel(ModelTurn('done')), tools, asyncio.Queue(), 'goal')
    task = asyncio.create_task(member.run())
    await closing.wait()
    try:
        with pytest.raises(SessionError):
            member.revise(1, 'cannot replace already finalized work')
    finally:
        release.set()
        await task


async def test_lead_changes_assignment_and_starts_helper_before_worker_finishes():
    from app.agent.team_poc.coordination import CoordinationTrial
    events, read = asyncio.Queue(), ReadTool()
    worker = TeamSession('worker', ScriptedModel(call('read'),
        call('ask', 'request_coordination', issue='Evidence contradicts original plan'), ModelTurn('revised evidence 73')),
        factory(read), events, 'Read then report conflict')
    lead = ScriptedModel(call('revise', 'revise_worker', expected_version=1, instruction='Use alternative plan'),
                         call('helper', 'start_helper', instruction='Independently check alternative'), ModelTurn('Assigned'))
    trial = CoordinationTrial(worker, lead,
        lambda goal: TeamSession('helper', ScriptedModel(ModelTurn('checked')), factory(), events, goal))
    outcome = await asyncio.wait_for(trial.run(), 3)
    assert outcome.worker.version == 2 and outcome.helper.content == 'checked'
    assert len(read.calls) == 1
    assert trial.worker_unfinished_at_report
    assert trial.helper_started_before_worker_completion
    assert any('Evidence contradicts' in (m.content or '') for m in lead.requests[0])
    assert any('Read then report conflict' in (m.content or '') for m in lead.requests[0])


async def test_lead_failure_cancels_waiting_worker_without_deadlock():
    from app.agent.team_poc.coordination import CoordinationTrial
    events, owners = asyncio.Queue(), []
    worker = TeamSession('worker', ScriptedModel(call('ask', 'request_coordination', issue='help')),
                         factory(owners=owners), events, 'goal')
    async def fail(messages):
        raise RuntimeError('Lead unavailable')
    trial = CoordinationTrial(worker, ScriptedModel(fail), lambda goal: None)
    with pytest.raises(RuntimeError, match='Lead unavailable'):
        await asyncio.wait_for(trial.run(), 2)
    assert worker.status == 'cancelled'
    assert owners[0][1] is owners[1][1]


async def test_cancelling_team_while_lead_waits_joins_worker():
    from app.agent.team_poc.coordination import CoordinationTrial
    entered = asyncio.Event()
    async def hold(messages):
        entered.set()
        await asyncio.Event().wait()
    events = asyncio.Queue()
    worker = TeamSession('worker', ScriptedModel(call('ask', 'request_coordination', issue='help')),
                         factory(), events, 'goal')
    trial = CoordinationTrial(worker, ScriptedModel(hold), lambda goal: None)
    task = asyncio.create_task(trial.run())
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert worker.status == 'cancelled' and trial.lead.status == 'cancelled'


async def test_worker_finishing_without_coordination_does_not_hang_trial():
    from app.agent.team_poc.coordination import CoordinationTrial
    worker = TeamSession('worker', ScriptedModel(ModelTurn('no report')), factory(), asyncio.Queue(), 'goal')
    with pytest.raises(SessionError, match='without requesting'):
        await asyncio.wait_for(CoordinationTrial(worker, ScriptedModel(), lambda goal: None).run(), 2)


async def test_second_report_fails_explicitly_instead_of_waiting_for_absent_lead():
    from app.agent.team_poc.coordination import CoordinationTrial
    events = asyncio.Queue()
    worker = TeamSession('worker', ScriptedModel(
        call('ask', 'request_coordination', issue='first'),
        call('ask-again', 'request_coordination', issue='new dependency')),
        factory(), events, 'goal')
    lead = ScriptedModel(call('revise', 'revise_worker', expected_version=1, instruction='continue'),
                         call('helper', 'start_helper', instruction='check'), ModelTurn('done'))
    trial = CoordinationTrial(worker, lead, lambda goal: TeamSession(
        'helper', ScriptedModel(ModelTurn('checked')), factory(), events, goal))
    with pytest.raises(SessionError, match='additional coordination'):
        await asyncio.wait_for(trial.run(), 2)
    assert worker.status == 'cancelled'


async def test_helper_must_publish_to_trial_queue():
    from app.agent.team_poc.coordination import CoordinationTrial
    worker = TeamSession('worker', ScriptedModel(call('ask', 'request_coordination', issue='help')),
                         factory(), asyncio.Queue(), 'goal')
    lead = ScriptedModel(call('helper', 'start_helper', instruction='check'))
    trial = CoordinationTrial(worker, lead, lambda goal: TeamSession(
        'helper', ScriptedModel(ModelTurn('done')), factory(), asyncio.Queue(), goal))
    with pytest.raises(SessionError, match='event queue'):
        await asyncio.wait_for(trial.run(), 2)


async def test_wait_deadline_closes_resources():
    owners = []
    member = TeamSession('worker', ScriptedModel(call('ask', 'request_coordination', issue='help')),
                         factory(owners=owners), asyncio.Queue(), 'goal', timeout_seconds=0.05)
    with pytest.raises(TimeoutError):
        await member.run()
    assert member.status == 'failed' and owners[0][1] is owners[1][1]


async def test_tool_and_revision_limits_are_not_reset():
    read = ReadTool()
    member = TeamSession('worker', ScriptedModel(call('one'), call('two')), factory(read),
                         asyncio.Queue(), 'goal', max_tool_calls=1, max_revisions=1)
    member.revise(1, 'revision consumes allowed change')
    with pytest.raises(SessionError, match='revision limit'):
        member.revise(2, 'too many')
    with pytest.raises(SessionError, match='tool call limit'):
        await member.run()
    assert len(read.calls) == 1


async def test_context_overflow_retries_only_model_after_completed_read():
    from app.agent.tool_calling_model import ModelContextOverflowError
    async def overflow(messages):
        raise ModelContextOverflowError('synthetic overflow')
    read = ReadTool()
    member = TeamSession('worker', ScriptedModel(call('read'), overflow, ModelTurn('done')),
                         factory(read), asyncio.Queue(), 'goal')
    assert (await member.run()).content == 'done'
    assert len(read.calls) == 1 and member.model_calls == 3


@pytest.mark.parametrize('cancel_wait', [False, True])
async def test_real_filesystem_mcp_owner_continuity_and_cancel(tmp_path, cancel_wait):
    from app.models.taskstate import TaskState
    from app.models.working_memory import WorkingMemory
    from app.tools.tool_loader import open_tool_registry, local_mcp_servers
    (tmp_path / 'facts.txt').write_text('actual-mcp-evidence-923', encoding='utf-8')
    owners, adapters = [], []
    @asynccontextmanager
    async def tools():
        owners.append(asyncio.current_task())
        try:
            async with open_tool_registry(WorkingMemory(TaskState('synthetic', 'test')),
                    include_knowledge=False, mcp_servers=local_mcp_servers(filesystem_root=tmp_path)) as registry:
                adapters.append(registry.get('read_file'))
                yield registry
        finally:
            owners.append(asyncio.current_task())
    model = ScriptedModel(call('read', 'read_file', path='facts.txt'),
                          call('ask', 'request_coordination', issue='need format'), ModelTurn('923'))
    events = asyncio.Queue()
    member = TeamSession('worker', model, tools, events, 'goal')
    task = asyncio.create_task(member.run())
    try:
        await asyncio.wait_for(waiting(events, member), 10)
        assert any('actual-mcp-evidence-923' in m.content for m in member.messages if isinstance(m, ToolMessage))
        if cancel_wait:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
        else:
            member.revise(1, 'continue with evidence')
            assert (await asyncio.wait_for(task, 5)).content == '923'
        assert owners == [task, task]
        assert sum(r.tool_name == 'read_file' for r in member.tool_results) == 1
        with pytest.raises(RuntimeError, match='closed'):
            await adapters[0].invoke({'path': 'facts.txt'})
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
