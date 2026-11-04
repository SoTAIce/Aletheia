import asyncio
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock

import pytest

from app.models.conversation_memory import ConversationMemory, ConversationMessage, ConversationTurn, MessageRole
from app.services.conversation_archive import ConversationArchive


def received(identity='request-1', text='  original input\n'):
    from app.models.conversation_memory import TurnStatus
    stamp = datetime(2025, 1, 2, tzinfo=timezone.utc)
    return ConversationTurn([ConversationMessage(MessageRole.USER, text, stamp)], identity, stamp,
                            task_id='task-1', status=TurnStatus.RECEIVED)


async def test_received_and_completed_turn_restore_without_overwriting_original(tmp_path):
    from app.models.conversation_memory import TurnStatus
    archive = ConversationArchive(tmp_path, 's')
    memory = ConversationMemory('s', archive=archive)
    original = received()
    await memory.add(original)
    path = next(archive._directory.glob('*.json'))
    original_bytes = path.read_bytes()
    assert ConversationArchive(tmp_path, 's').read_turn(original.turn_id) == original
    done = await memory.complete_turn(original.turn_id, TurnStatus.COMPLETED, 'answer',
                                      result={'task_id': 'task-1', 'content': 'answer'})
    assert done.messages[0] == original.messages[0]
    assert done.messages[1].content == 'answer'
    assert done.task_id == 'task-1' and done.status is TurnStatus.COMPLETED
    assert done.finished_at >= original.created_at
    assert path.read_bytes() == original_bytes
    restarted = ConversationMemory('s', archive=ConversationArchive(tmp_path, 's'))
    await restarted.restore()
    assert restarted.retrieve_turns() == [done]
    assert await restarted.turn_result(original.turn_id) == {'task_id': 'task-1', 'content': 'answer'}
    # Re-publishing the same original after completion is still idempotent.
    archive.save_turn(original)
    assert len(archive.load_turns()) == 1


@pytest.mark.parametrize('status', ['completed', 'blocked', 'failed', 'cancelled'])
async def test_outcome_is_idempotent_and_cannot_be_replaced(tmp_path, status):
    from app.models.conversation_memory import TurnStatus
    memory = ConversationMemory('s', archive=ConversationArchive(tmp_path, 's'))
    original = received()
    await memory.add(original)
    first = await memory.complete_turn(original.turn_id, TurnStatus(status), 'reply')
    assert await memory.complete_turn(original.turn_id, TurnStatus(status), 'reply') == first
    with pytest.raises(ValueError, match='conflict'):
        await memory.complete_turn(original.turn_id, TurnStatus(status), 'changed')
    assert len(memory.archive.load_turns()) == 1


async def test_legacy_v1_records_can_mix_with_lifecycle_records(tmp_path):
    from app.models.conversation_memory import TurnStatus
    archive = ConversationArchive(tmp_path, 's')
    old = ConversationTurn([ConversationMessage(MessageRole.USER, 'legacy')])
    archive.save_turn(old)
    memory = ConversationMemory('s', archive=archive)
    await memory.add(received())
    await memory.complete_turn('request-1', TurnStatus.FAILED, 'failure')
    reopened = ConversationArchive(tmp_path, 's')
    assert reopened.load_turns()[0] == old
    assert reopened.load_turns()[1].status is TurnStatus.FAILED
    assert reopened.read_turn(old.turn_id).status is None


async def test_failed_outcome_publication_keeps_received_input_and_can_retry(tmp_path, monkeypatch):
    import os
    from app.models.conversation_memory import TurnStatus
    archive = ConversationArchive(tmp_path, 's')
    memory = ConversationMemory('s', archive=archive)
    await memory.add(received())
    publish = os.replace
    monkeypatch.setattr(os, 'replace', lambda *a: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError):
        await memory.complete_turn('request-1', TurnStatus.COMPLETED, 'done')
    assert archive.read_turn('request-1').status is TurnStatus.RECEIVED
    monkeypatch.setattr(os, 'replace', publish)
    await memory.complete_turn('request-1', TurnStatus.COMPLETED, 'done')
    assert archive.read_turn('request-1').status is TurnStatus.COMPLETED


async def test_foreign_or_malformed_outcome_is_rejected(tmp_path):
    import json
    from app.models.conversation_memory import TurnStatus
    archive = ConversationArchive(tmp_path, 's')
    memory = ConversationMemory('s', archive=archive)
    await memory.add(received())
    await memory.complete_turn('request-1', TurnStatus.COMPLETED, 'done')
    path = archive._outcome_path('request-1')
    data = json.loads(path.read_text(encoding='utf-8'))
    data['session_id'] = 'other'
    path.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(ValueError, match='outcome'):
        archive.load_turns(1)


async def test_memory_only_lifecycle_keeps_original_and_result():
    from app.models.conversation_memory import TurnStatus
    memory = ConversationMemory('s')
    await memory.add(received())
    done = await memory.complete_turn('request-1', TurnStatus.BLOCKED, 'which file?', result={'status': 'blocked'})
    assert done.messages[0].content == '  original input\n'
    assert await memory.turn_result('request-1') == {'status': 'blocked'}


def service(tmp_path, **kwargs):
    from app.agent.step_models import ModelTurn
    from app.services.rag_agent_service import RagAgentService
    model, planner = AsyncMock(), AsyncMock()
    model.generate.return_value = ModelTurn('{"status":"succeeded","content":"done","reason":null}')
    planner.generate.return_value = '{"steps":["Answer"],"reason":null}'
    return RagAgentService(model=model, planner_model=planner, mcp_servers=(),
                           include_knowledge=False, memory_directory=tmp_path, **kwargs)


async def test_service_persists_raw_input_before_model_and_replays_after_restart(tmp_path):
    from app.models.conversation_memory import TurnStatus
    from app.agent.step_models import ModelTurn
    s = service(tmp_path)
    async def generate(*args):
        turn = s._conversations['s'].archive.load_turns()[0]
        assert turn.status is TurnStatus.RECEIVED
        assert turn.messages[0].content == '  raw goal\n'
        return ModelTurn('{"status":"succeeded","content":"done","reason":null}')
    s.model.generate.side_effect = generate
    first = await s.query_result('  raw goal\n', 's', request_id='r')
    second = await s.query_result('  raw goal\n', 's', request_id='r')
    assert first == second and s.model.generate.await_count == 1
    reopened = service(tmp_path)
    assert await reopened.query_result('  raw goal\n', 's', request_id='r') == first
    reopened.model.generate.assert_not_awaited()
    with pytest.raises(ValueError, match='conflict'):
        await s.query_result('different', 's', request_id='r')
    assert len(s._conversations['s'].archive.load_turns()) == 1


async def test_service_blocked_task_resumes_in_new_turn_without_repeating_completed_step(tmp_path):
    from app.models.conversation_memory import TurnStatus
    from app.models.taskstate import TaskStatus
    from app.agent.step_models import ModelTurn, ToolCall
    s = service(tmp_path)
    s._planner_model.generate.side_effect = [
        '{"steps":["Collect evidence","Ask destination"],"reason":null}',
        '{"steps":["Finish with supplied destination"],"reason":null}',
    ]
    s.model.generate.side_effect = [
        ModelTurn(tool_calls=(ToolCall('clock', 'get_current_time', {'timezone': 'UTC'}),)),
        ModelTurn('{"status":"succeeded","content":"evidence saved","reason":null}'),
        ModelTurn('{"status":"blocked","content":"Which destination?","reason":"Need destination"}'),
        ModelTurn('{"status":"succeeded","content":"finished","reason":null}'),
    ]
    blocked = await s.query_result('Original goal', 's', request_id='first')
    assert blocked.status is TaskStatus.BLOCKED
    with pytest.raises(ValueError):
        await s.query_result('destination', 'other', resume_task_id=blocked.task_id)
    done = await s.query_result('  destination A\n', 's', request_id='second', resume_task_id=blocked.task_id)
    assert done.task_id == blocked.task_id and done.status is TaskStatus.COMPLETED
    assert done.execution_count == 3 and done.replan_count == 1
    assert s.model.generate.await_count == 4
    assert len(done.steps[0].tool_results) == 1 and done.steps[0].tool_results[0].call_id == 'clock'
    prompt = s._planner_model.generate.call_args.args[0]
    assert 'evidence saved' in prompt and 'destination A' in prompt
    turns = s._conversations['s'].archive.load_turns()
    assert [t.status for t in turns] == [TurnStatus.BLOCKED, TurnStatus.COMPLETED]
    assert turns[1].messages[0].content == '  destination A\n'
    assert turns[1].question_id
    assert await s.query_result('  destination A\n', 's', request_id='second', resume_task_id=blocked.task_id) == done
    assert s.model.generate.await_count == 4


async def test_cancel_and_setup_failure_keep_input(tmp_path, monkeypatch):
    from app.models.conversation_memory import TurnStatus
    from contextlib import asynccontextmanager
    import app.services.rag_agent_service as module
    s = service(tmp_path)
    entered = asyncio.Event()
    async def wait(*args):
        entered.set()
        await asyncio.Event().wait()
    s.model.generate.side_effect = wait
    task = asyncio.create_task(s.query_result('raw', 's', request_id='r'))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    turn = s._conversations['s'].archive.load_turns()[0]
    assert turn.status is TurnStatus.CANCELLED and turn.messages[0].content == 'raw'
    with pytest.raises(RuntimeError, match='cancelled'):
        await s.query_result('raw', 's', request_id='r')
    @asynccontextmanager
    async def broken(*a, **k):
        raise OSError('MCP unavailable')
        yield
    monkeypatch.setattr(module, 'open_tool_registry', broken)
    with pytest.raises(OSError):
        await s.query_result('setup raw', 's', request_id='setup')
    assert s._conversations['s'].archive.load_turns()[-1].status is TurnStatus.FAILED


async def test_outcome_write_retry_never_reexecutes_model(tmp_path, monkeypatch):
    s = service(tmp_path)
    # Pre-create session then inject only the outcome write failure.
    await s.query_result('warmup', 's')
    memory = s._conversations['s']
    finish = memory.complete_turn
    monkeypatch.setattr(memory, 'complete_turn', AsyncMock(side_effect=OSError('disk full')))
    with pytest.raises(OSError):
        await s.query_result('goal', 's', request_id='r')
    count = s.model.generate.await_count
    monkeypatch.setattr(memory, 'complete_turn', finish)
    result = await s.query_result('goal', 's', request_id='r')
    assert result.content == 'done' and s.model.generate.await_count == count
    assert len(memory.archive.load_turns()) == 2


async def test_standalone_runtime_records_input_before_execution_and_on_cancel():
    from app.agent.runtime import AgentRuntime
    from app.agent.planner import Planner
    from app.models.working_memory import WorkingMemory
    from app.models.taskstate import TaskState
    from app.models.conversation_memory import TurnStatus
    from app.tools.tool_registry import ToolRegistry
    conversation = ConversationMemory('s')
    model, planner = AsyncMock(), AsyncMock()
    entered = asyncio.Event()
    async def wait(*args):
        turn = conversation.retrieve_turns()[0]
        assert turn.messages[0].content == 'original goal'
        assert turn.status is TurnStatus.RECEIVED
        entered.set()
        await asyncio.Event().wait()
    planner.generate.side_effect = wait
    runtime = AgentRuntime(Planner(planner), model, WorkingMemory(TaskState('u', 'original goal')),
                           ToolRegistry(), conversation=conversation)
    task = asyncio.create_task(runtime.run())
    # Wait for either model entry or an early regression failure, without hanging.
    waiter = asyncio.create_task(entered.wait())
    done, _ = await asyncio.wait((task, waiter), timeout=3, return_when=asyncio.FIRST_COMPLETED)
    if task in done:
        task.result()
        pytest.fail('runtime did not reach the planner')
    assert waiter in done
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert conversation.retrieve_turns()[0].status is TurnStatus.CANCELLED


async def test_concurrent_request_id_and_user_forwarding(tmp_path):
    s = service(tmp_path)
    first, second = await asyncio.gather(*(
        s.query_result('same input', 's', user_id='u', request_id='r') for _ in range(2)))
    assert first == second and s.model.generate.await_count == 1
    assert await s.query('same input', 's', user_id='u', request_id='r') == first.content
    events = [e async for e in s.query_stream('same input', 's', user_id='u', request_id='r')]
    assert events[-1]['type'] == 'complete' and s.model.generate.await_count == 1
    other = await s.query_result('same input', 's', user_id='v', request_id='r')
    assert other.task_id != first.task_id


async def test_received_only_after_restart_cannot_reexecute(tmp_path):
    from app.models.conversation_memory import TurnStatus
    archive = ConversationArchive(tmp_path, 's')
    archive.save_turn(received('request:r', 'raw'))
    s = service(tmp_path)
    with pytest.raises(RuntimeError, match='received'):
        await s.query_result('raw', 's', request_id='r')
    s.model.generate.assert_not_awaited()
    s._planner_model.generate.assert_not_awaited()
    assert archive.read_turn('request:r').status is TurnStatus.RECEIVED


@pytest.mark.parametrize('phase', ['ingress', 'outcome', 'retry-outcome'])
async def test_cancel_during_disk_write_waits_for_publication(tmp_path, monkeypatch, phase):
    import threading
    from app.models.conversation_memory import TurnStatus
    s = service(tmp_path)
    if phase == 'retry-outcome':
        # Keep a completed result whose first publication failed.
        await s.query_result('warmup', 's')
        memory = s._conversations['s']
        finish = memory.complete_turn
        monkeypatch.setattr(memory, 'complete_turn', AsyncMock(side_effect=OSError('disk full')))
        with pytest.raises(OSError):
            await s.query_result('raw', 's', request_id='r')
        monkeypatch.setattr(memory, 'complete_turn', finish)
    entered, release = threading.Event(), threading.Event()
    method = 'save_turn' if phase == 'ingress' else 'complete_turn'
    original = getattr(ConversationArchive, method)
    def delayed(self, *args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(ConversationArchive, method, delayed)
    task = asyncio.create_task(s.query_result('raw', 's', request_id='r'))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(.02)
    assert not task.done()  # Still holds the session lock until the write settles.
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    turns = [t for t in ConversationArchive(tmp_path, 's').load_turns() if t.turn_id == 'request:r']
    assert len(turns) == 1
    assert turns[0].status is (TurnStatus.CANCELLED if phase == 'ingress' else TurnStatus.COMPLETED)
    if phase == 'ingress':
        s.model.generate.assert_not_awaited()


async def test_initial_write_failure_does_not_call_model(tmp_path, monkeypatch):
    monkeypatch.setattr(ConversationArchive, 'save_turn', lambda *a: (_ for _ in ()).throw(OSError('disk full')))
    s = service(tmp_path)
    with pytest.raises(OSError):
        await s.query_result('raw', 's', request_id='r')
    s.model.generate.assert_not_awaited()
    s._planner_model.generate.assert_not_awaited()


async def test_memory_only_lifecycle_receipts_survive_recent_cache_eviction():
    from app.models.conversation_memory import TurnStatus
    memory = ConversationMemory('s', max_turns=1)
    original = received()
    await memory.add(original)
    final = await memory.complete_turn(original.turn_id, TurnStatus.COMPLETED, 'done')
    final.messages[0].content = 'mutated caller data'
    await memory.add(received('another'))
    assert (await memory.read_turn(original.turn_id)).messages[0].content == original.messages[0].content
    await memory.add(original)
    assert len(memory) == 1


async def test_pending_standalone_outcome_does_not_hide_resolved_question(tmp_path, monkeypatch):
    from app.agent.runtime import AgentRuntime
    from app.agent.planner import Planner
    from app.agent.step_models import ModelTurn
    from app.models.working_memory import WorkingMemory
    from app.models.taskstate import TaskState, TaskStatus
    from app.tools.tool_registry import ToolRegistry
    conversation = ConversationMemory('s', archive=ConversationArchive(tmp_path, 's'))
    model, planner = AsyncMock(), AsyncMock()
    planner.generate.return_value = '{"steps":["Answer"],"reason":null}'
    model.generate.side_effect = [
        ModelTurn('{"status":"blocked","content":"Which destination?","reason":"Need destination"}'),
        ModelTurn('{"status":"succeeded","content":"done","reason":null}'),
    ]
    runtime = AgentRuntime(Planner(planner), model, WorkingMemory(TaskState('u', 'goal')),
                           ToolRegistry(), conversation=conversation)
    complete = conversation.complete_turn
    monkeypatch.setattr(conversation, 'complete_turn', AsyncMock(side_effect=OSError('disk full')))
    with pytest.raises(OSError):
        await runtime.run()
    runtime.memory.task_state.resolve_question(runtime.memory.task_state.scratchpad.open_questions[0].question_id, 'destination')
    monkeypatch.setattr(conversation, 'complete_turn', complete)
    assert (await runtime.run()).status is TaskStatus.BLOCKED  # Flush only; no replay.
    async def finish(*args):
        turns = conversation.archive.load_turns()
        assert len(turns) == 2 and turns[-1].messages[0].content == 'destination'
        return ModelTurn('{"status":"succeeded","content":"done","reason":null}')
    model.generate.side_effect = finish
    assert (await runtime.run()).status is TaskStatus.COMPLETED


async def test_blocked_runtime_survives_tool_context_cleanup_failure(tmp_path, monkeypatch):
    from app.agent.step_models import ModelTurn
    from contextlib import asynccontextmanager
    import app.services.rag_agent_service as module
    original = module.open_tool_registry
    @asynccontextmanager
    async def broken(*a, **kw):
        async with original(*a, **kw) as registry:
            yield registry
        raise OSError('cleanup failed')
    monkeypatch.setattr(module, 'open_tool_registry', broken)
    s = service(tmp_path)
    s.model.generate.return_value = ModelTurn('{"status":"blocked","content":"Where?","reason":"Need destination"}')
    with pytest.raises(OSError):
        await s.query_result('goal', 's', request_id='r')
    blocked = await s.query_result('goal', 's', request_id='r')
    monkeypatch.setattr(module, 'open_tool_registry', original)
    s.model.generate.return_value = ModelTurn('{"status":"succeeded","content":"done","reason":null}')
    assert (await s.query_result('destination', 's', resume_task_id=blocked.task_id)).content == 'done'


async def test_resume_mapping_and_execution_limit_are_not_reset(tmp_path):
    from app.agent.step_models import ModelTurn
    from app.models.taskstate import TaskStatus
    s = service(tmp_path, max_executions=1)
    s.model.generate.return_value = ModelTurn('{"status":"blocked","content":"Where?","reason":"Need destination"}')
    blocked = await s.query_result('goal', 's', request_id='r', user_id='alice')
    with pytest.raises(ValueError):
        await s.query_result('answer', 's', resume_task_id=blocked.task_id, user_id='bob')
    with pytest.raises(ValueError):
        await s.query_result('answer', 's', resume_task_id=blocked.task_id, question_id='unknown', user_id='alice')
    runtime = s._tasks[(s._session_key('alice', 's'), blocked.task_id)]
    runtime.memory.task_state.add_open_question('Second question', blocking=True)
    runtime.memory.task_state.pause_for_questions()
    with pytest.raises(ValueError, match='question_id'):
        await s.query_result('answer', 's', resume_task_id=blocked.task_id, user_id='alice')
    q1, q2 = runtime.memory.task_state.scratchpad.open_questions
    still_blocked = await s.query_result('first answer', 's', resume_task_id=blocked.task_id,
                                       question_id=q1.question_id, user_id='alice')
    assert still_blocked.status is TaskStatus.BLOCKED
    failed = await s.query_result('second answer', 's', resume_task_id=blocked.task_id,
                                 question_id=q2.question_id, user_id='alice')
    assert failed.status is TaskStatus.FAILED and failed.execution_count == 1
    assert s.model.generate.await_count == 1


async def test_business_failure_and_post_ingress_setup_error_are_archived(tmp_path, monkeypatch):
    from app.agent.step_models import ModelTurn
    from app.models.conversation_memory import TurnStatus
    import app.services.rag_agent_service as module
    s = service(tmp_path, max_replans=0)
    s.model.generate.return_value = ModelTurn('{"status":"failed","content":"cannot finish","reason":"No evidence"}')
    await s.query_result('raw', 's', request_id='failure')
    assert s._conversations['s'].archive.load_turns()[0].status is TurnStatus.FAILED
    monkeypatch.setattr(module, 'ContextManager', lambda **kw: (_ for _ in ()).throw(ValueError('invalid config')))
    with pytest.raises(ValueError):
        await s.query_result('raw2', 's', request_id='setup')
    assert s._conversations['s'].archive.load_turns()[-1].status is TurnStatus.FAILED


async def test_same_outcome_from_other_memory_instance_keeps_first_completion_time(tmp_path):
    from app.models.conversation_memory import TurnStatus
    archive = ConversationArchive(tmp_path, 's')
    memory = ConversationMemory('s', archive=archive)
    await memory.add(received())
    first = await memory.complete_turn('request-1', TurnStatus.COMPLETED, 'done', result={'ok': True})
    # Independent writers may observe RECEIVED simultaneously and assign
    # slightly different completion timestamps for the same logical result.
    later = first.finished_at + timedelta(seconds=1)
    proposal = replace(first, finished_at=later, messages=[first.messages[0],
        replace(first.messages[1], created_at=later)])
    other = ConversationArchive(tmp_path, 's')
    assert other.complete_turn(proposal, result={'ok': True}) == first
    assert other.read_turn('request-1') == first
