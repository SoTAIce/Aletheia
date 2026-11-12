"""Offline full Agent executions with real archives/tools and controlled providers."""
import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from loop_fakes import loop_reply

from app.agent.step_models import ModelTurn, ToolCall
from app.models.conversation_memory import ConversationMessage, ConversationTurn, MessageRole, TurnStatus
from app.models.taskstate import TaskStatus
from app.services.conversation_archive import ConversationArchive
from app.services.memory_compression.llm_compressor import LLMCompressor, SUMMARY_HEADINGS
from app.services.rag_agent_service import RagAgentService
from app.services.summary_scheduler import SummaryScheduler


def outcome(text, status='succeeded', reason=None):
    """Plan mode's step result as a loop reply (stage 4c)."""
    return loop_reply(text, status, reason)


async def wait_until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(.005)


@pytest.mark.parametrize('trial', range(2))
@pytest.mark.parametrize('mode', ['success', 'provider_error', 'truncated', 'timeout', 'shutdown_cancel'])
async def test_full_agent_lifecycle_with_background_summary_faults(tmp_path, mode, trial):
    archive = ConversationArchive(tmp_path, 's')
    originals = []
    for i in range(26):
        now = datetime.now(timezone.utc)
        turn = ConversationTurn([
            ConversationMessage(MessageRole.USER, 'Alpha requires PostgreSQL; do not switch to SQLite. ' + 'x' * 5500, now),
            ConversationMessage(MessageRole.ASSISTANT, 'PostgreSQL is retained.', now),
        ], f'history-{i}', now, task_id='history-task', status=TurnStatus.COMPLETED, finished_at=now)
        archive.save_turn(turn)
        originals.append(turn)
    before = {p.name: p.read_bytes() for p in archive._directory.glob('*.json')}
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    sources = []
    summary_body = '\n'.join(f'{heading}\nAlpha: keep PostgreSQL; do not switch to SQLite.' for heading in SUMMARY_HEADINGS)

    async def create(**kwargs):
        # Exercise the production LLMCompressor prompt/finish checks. Only its
        # provider transport is replaced; no network or real LLM is involved.
        raw = kwargs['messages'][1]['content'].split('New conversation turns:\n', 1)[1]
        batch, _ = json.JSONDecoder().raw_decode(raw)
        assert all(t['status'] != 'received' for t in batch)
        assert {t['task_id'] for t in batch} == {'history-task'}
        sources.append(tuple(t['turn_id'] for t in batch))
        entered.set()
        try:
            await release.wait()
            if mode == 'provider_error':
                raise RuntimeError('controlled provider outage')
            if mode in ('timeout', 'shutdown_cancel'):
                await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason='length' if mode == 'truncated' else 'stop',
            message=SimpleNamespace(role='assistant', content=summary_body, refusal=None, tool_calls=None))])

    create_mock = AsyncMock(side_effect=create)
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create_mock)))
    model = AsyncMock()
    model.generate.side_effect = [
        ModelTurn(tool_calls=(ToolCall('clock-once', 'get_current_time', {'timezone': 'UTC'}),)),
        outcome('', 'blocked', 'Which output format?'),
        outcome('Report finished with earlier clock evidence'),
    ]
    service = RagAgentService(model=model, mcp_servers=(), include_knowledge=False,
                              memory_directory=tmp_path, conversation_compressor=LLMCompressor(client, 'offline-provider'))
    # The scheduler's timeout also covers the lock wait and archive reads before the provider
    # call; .15s expired before the provider was reached on a loaded machine. The provider
    # never answers in timeout mode, so 1s still times out every attempt.
    service._summary_scheduler = SummaryScheduler(service._conversations, max_concurrency=1, max_pending=4,
        scan_interval_seconds=.01, max_wait_seconds=.02, retry_delay_seconds=.01,
        timeout_seconds=1.0 if mode == 'timeout' else 5)
    try:
        blocked = await asyncio.wait_for(service.query_result('  Get clock evidence and report\n', 's', request_id='first'), 5)
        assert blocked.status is TaskStatus.BLOCKED
        await asyncio.wait_for(entered.wait(), 5)
        # Resume and complete while summary generation is held by the provider.
        done = await asyncio.wait_for(service.query_result('  Markdown\n', 's', request_id='answer',
                                                          resume_task_id=blocked.task_id), 5)
        assert done.status is TaskStatus.COMPLETED and done.task_id == blocked.task_id
        assert done.execution_count == 2 and done.replan_count == 0   # answering a question replans without spending the budget (D-033)
        assert model.generate.await_count == 3
        # The clock ran once, before the question; the resumed loop reuses its reply instead of calling again.
        from app.agent.step_models import ToolMessage
        resumed = model.generate.call_args_list[-1].args[0]
        assert [m.call_id for m in resumed if isinstance(m, ToolMessage)] == ['clock-once', 'ask']
        assert [c.call_id for receipt in done.steps for c in receipt.tool_calls] == []
        assert not release.is_set()
        assert await service.query_result('  Markdown\n', 's', request_id='answer', resume_task_id=done.task_id) == done
        assert model.generate.await_count == 3
        memory = service._conversations['s']
        if mode == 'shutdown_cancel':
            await service.aclose(timeout_seconds=.03)
            assert cancelled.is_set()
        else:
            release.set()
            if mode == 'success':
                await wait_until(lambda: bool(memory.summary_index))
                ref = memory.summary_index[0].storage_ref
                model.generate.side_effect = [
                    ModelTurn(tool_calls=(ToolCall('read-summary', 'read_context_artifact', {'ref': ref, 'limit': 4000}),)),
                    outcome('Alpha keeps PostgreSQL; SQLite is forbidden'),
                ]
                recall = await service.query_result('Recall Alpha constraints', 's', request_id='recall')
                assert recall.status is TaskStatus.COMPLETED
                result = recall.steps[0].tool_results[0]
                assert result.success and result.tool_name == 'read_context_artifact'
                assert result.content == summary_body
                first_request = model.generate.call_args_list[-2].args[0]
                assert any(ref in (m.content or '') for m in first_request)   # the summary is offered up front
            else:
                await wait_until(lambda: create_mock.await_count == 2)
                await wait_until(lambda: not service._summary_scheduler._active)
                await asyncio.sleep(.04)
                assert create_mock.await_count == 2
            await service.aclose()
        summaries = archive.list_summaries()
        if mode != 'success':
            assert summaries == []
        covered = [identity for entry in summaries for identity in entry.source_turn_ids]
        assert len(covered) == len(set(covered))
        for name, raw in before.items():
            assert (archive._directory / name).read_bytes() == raw
        restored = ConversationArchive(tmp_path, 's')
        turns = {t.turn_id: t for t in restored.load_turns()}
        assert turns['request:first'].messages[0].content == '  Get clock evidence and report\n'
        assert turns['request:first'].status is TurnStatus.BLOCKED
        assert turns['request:answer'].messages[0].content == '  Markdown\n'
        assert turns['request:answer'].status is TurnStatus.COMPLETED
        assert all(turns[t.turn_id] == t for t in originals)
        # A fresh service replays the durable receipt without planner/tools.
        async with RagAgentService(model=AsyncMock(), mcp_servers=(),
                                    include_knowledge=False, memory_directory=tmp_path) as restarted:
            assert await restarted.query_result('  Markdown\n', 's', request_id='answer', resume_task_id=done.task_id) == done
            restarted.model.generate.assert_not_awaited()
    finally:
        release.set()
        await service.aclose()
