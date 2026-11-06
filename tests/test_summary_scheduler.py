import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models.conversation_memory import ConversationMemory, ConversationMessage, ConversationTurn, MessageRole
from app.services.conversation_archive import ConversationArchive


async def memory_at(root, name, compressor, *, threshold=1):
    memory = ConversationMemory(name, compressor, archive=ConversationArchive(root, name), max_turns=1,
                                compression_token_threshold=threshold, background_compression=True)
    for text in ['old source', 'recent source']:
        await memory.add(ConversationTurn([ConversationMessage(MessageRole.USER, text)]))
    return memory


async def until(predicate, timeout=3):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.005)


async def test_scheduler_coalesces_and_bounds_global_concurrency_and_queue(tmp_path):
    from app.services.summary_scheduler import SummaryScheduler
    active = peak = calls = 0
    release = asyncio.Event()
    async def compress(**kwargs):
        nonlocal active, peak, calls
        active += 1
        calls += 1
        peak = max(peak, active)
        try:
            await release.wait()
            return 'summary'
        finally:
            active -= 1
    memories = {i: await memory_at(tmp_path, str(i), SimpleNamespace(compress=compress)) for i in range(5)}
    scheduler = SummaryScheduler(memories, max_concurrency=2, max_pending=2, scan_interval_seconds=.02)
    try:
        for key in memories:
            scheduler.notify(key)
        assert scheduler.pending_count <= 2
        await until(lambda: active == 2)
        for _ in range(30):
            scheduler.notify(0)
        assert peak == 2
        release.set()
        await until(lambda: all(m.summary_index for m in memories.values()))
        assert calls == 5
    finally:
        release.set()
        await scheduler.aclose()


async def test_small_backlog_waits_for_age_and_restart_uses_sources(tmp_path):
    from app.services.summary_scheduler import SummaryScheduler
    compressor = SimpleNamespace(compress=AsyncMock(return_value='summary'))
    original = await memory_at(tmp_path, 's', compressor, threshold=10000)
    restored = ConversationMemory('s', compressor, archive=ConversationArchive(tmp_path, 's'), max_turns=1,
                                  compression_token_threshold=10000, background_compression=True)
    await restored.restore()
    scheduler = SummaryScheduler({'s': restored}, max_wait_seconds=.15, scan_interval_seconds=.01)
    try:
        scheduler.notify('s')
        await asyncio.sleep(.04)
        compressor.compress.assert_not_awaited()
        await until(lambda: len(restored.summary_index) == 1)
        assert original.archive.load_turns() == restored.archive.load_turns()
    finally:
        await scheduler.aclose()


async def test_failure_attempts_are_finite_and_shutdown_cancels_generation(tmp_path):
    from app.services.summary_scheduler import SummaryScheduler
    compressor = SimpleNamespace(compress=AsyncMock(side_effect=RuntimeError('model unavailable')))
    memory = await memory_at(tmp_path, 'failure', compressor)
    scheduler = SummaryScheduler({'s': memory}, retry_delay_seconds=.01, scan_interval_seconds=.01)
    scheduler.notify('s')
    await until(lambda: compressor.compress.await_count == 2)
    await asyncio.sleep(.08)
    assert compressor.compress.await_count == 2
    await scheduler.aclose()
    assert memory.archive.list_summaries() == []
    entered = asyncio.Event()
    async def wait(**kwargs):
        entered.set()
        await asyncio.Event().wait()
    memory._compressor = SimpleNamespace(compress=wait)
    second = SummaryScheduler({'s': memory}, scan_interval_seconds=.01)
    second.notify('s')
    await entered.wait()
    await asyncio.wait_for(second.aclose(timeout_seconds=.03), 1)
    assert memory.archive.list_summaries() == []
    assert not second.notify('s')


async def test_service_completion_does_not_wait_for_summary_and_closes_scheduler(tmp_path):
    from app.agent.step_models import ModelTurn
    from app.services.rag_agent_service import RagAgentService
    entered, release = asyncio.Event(), asyncio.Event()
    async def compress(**kwargs):
        entered.set()
        await release.wait()
        return 'summary'
    archive = ConversationArchive(tmp_path, 's')
    for i in range(26):
        archive.save_turn(ConversationTurn([ConversationMessage(MessageRole.USER, f'old source {i}' * 500)]))
    model, planner = AsyncMock(), AsyncMock()
    model.generate.return_value = ModelTurn('{"status":"succeeded","content":"done","reason":null}')
    planner.generate.return_value = '{"steps":["Answer"],"reason":null}'
    service = RagAgentService(model=model, planner_model=planner, mcp_servers=(), include_knowledge=False,
                              memory_directory=tmp_path, conversation_compressor=SimpleNamespace(compress=compress))
    try:
        result = await asyncio.wait_for(service.query_result('new goal', 's', request_id='r'), 3)
        assert result.content == 'done'
        await asyncio.wait_for(entered.wait(), 3)
        assert await service.query('new goal', 's', request_id='r') == 'done'
        assert await asyncio.wait_for(service.query('another input', 's', request_id='second'), 3) == 'done'
        assert model.generate.await_count == 2
    finally:
        release.set()
        await service.aclose()


async def test_compressor_client_and_cli_use_explicit_lifetimes(monkeypatch, tmp_path):
    from contextlib import asynccontextmanager
    import app.core.dependencies as dependencies
    import app.agent.__main__ as cli
    from app.models.taskstate import TaskStatus
    closed = []
    class Client:
        def __init__(self, **kwargs):
            assert kwargs['max_retries'] == 0
        async def __aenter__(self):
            return self
        async def __aexit__(self, *exc):
            closed.append('client')
    monkeypatch.setattr(dependencies, 'AsyncOpenAI', Client)
    async with dependencies.open_conversation_compressor() as compressor:
        assert compressor._model == dependencies.conversation_compressor._model
    assert closed == ['client']
    class Service:
        def __init__(self, **kwargs):
            assert kwargs['conversation_compressor'] is not None
        async def __aenter__(self):
            return self
        async def __aexit__(self, *exc):
            closed.append('service')
        async def query_result(self, *args, **kwargs):   # the CLI also passes user_id (D-042)
            return SimpleNamespace(content='done', status=TaskStatus.COMPLETED, execution_count=1, replan_count=0)
    monkeypatch.setattr(cli, 'RagAgentService', Service)
    monkeypatch.setattr(cli.config, 'conversation_storage_dir', str(tmp_path))
    args = SimpleNamespace(compress_history=True, filesystem_root=None, write_root=None,
                           no_knowledge=True, max_executions=1, max_replans=0, goal='goal', session_id='s')
    assert await cli.main(args) == 0
    assert closed[-2:] == ['service', 'client']


async def test_clear_does_not_get_undone_by_periodic_scan(tmp_path):
    from app.services.summary_scheduler import SummaryScheduler
    entered, release = asyncio.Event(), asyncio.Event()
    async def compress(**kwargs):
        entered.set()
        await release.wait()
        return 'obsolete'
    memory = await memory_at(tmp_path, 's', SimpleNamespace(compress=compress))
    scheduler = SummaryScheduler({'s': memory}, scan_interval_seconds=.01)
    try:
        scheduler.notify('s')
        await entered.wait()
        memory.clear()
        release.set()
        await asyncio.sleep(.08)
        assert memory.summary_index == ()
        assert memory.archive.list_summaries() == []
    finally:
        release.set()
        await scheduler.aclose()


async def test_queue_saturation_does_not_restart_small_backlog_wait_time():
    from app.services.summary_scheduler import SummaryScheduler
    class Memory:
        background_summary_enabled = True
        summary_work_key = (0, ('source',))
        compression_buffer_tokens = 1
        forced = False
        async def compress_pending(self, *, force, raise_errors):
            await asyncio.sleep(.02)
            self.forced |= force
            return False
        def get_compression_buffer(self):
            return ['source']
    memories = {i: Memory() for i in range(2)}
    scheduler = SummaryScheduler(memories, max_concurrency=1, max_pending=1,
                                  max_wait_seconds=.04, scan_interval_seconds=.005)
    try:
        scheduler.notify(0)
        await until(lambda: all(m.forced for m in memories.values()), timeout=.6)
    finally:
        await scheduler.aclose()


async def test_scheduler_notification_failure_cannot_change_business_result(tmp_path, monkeypatch):
    from app.agent.step_models import ModelTurn
    from app.services.rag_agent_service import RagAgentService
    model, planner = AsyncMock(), AsyncMock()
    model.generate.return_value = ModelTurn('{"status":"succeeded","content":"done","reason":null}')
    planner.generate.return_value = '{"steps":["Answer"],"reason":null}'
    async with RagAgentService(model=model, planner_model=planner, mcp_servers=(),
                                include_knowledge=False, memory_directory=tmp_path) as service:
        def fail(*args):
            raise RuntimeError('scheduler unavailable')
        monkeypatch.setattr(service._summary_scheduler, 'notify', fail)
        result = await service.query_result('raw goal', 's', request_id='r')
        assert result.content == 'done'
        assert await service.query_result('raw goal', 's', request_id='r') == result
        model.generate.assert_awaited_once()


async def test_saturated_queue_keeps_retry_not_before_time():
    import time
    from app.services.summary_scheduler import SummaryScheduler
    class Memory:
        background_summary_enabled = True
        summary_work_key = (0, ('source',))
        compression_buffer_tokens = 1
        def __init__(self):
            self.calls = []
        async def compress_pending(self, **kwargs):
            self.calls.append(time.monotonic())
            await asyncio.sleep(.02)
            raise RuntimeError('model unavailable')
        def get_compression_buffer(self):
            return ['source']
    memories = {i: Memory() for i in range(2)}
    scheduler = SummaryScheduler(memories, max_concurrency=1, max_pending=1,
                                  retry_delay_seconds=.15, scan_interval_seconds=.005)
    try:
        scheduler.notify(0)
        await until(lambda: all(len(m.calls) == 2 for m in memories.values()))
        assert all(m.calls[1] - m.calls[0] >= .15 for m in memories.values())
    finally:
        await scheduler.aclose()
