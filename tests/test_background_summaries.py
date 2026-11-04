import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models.conversation_memory import ConversationMemory, ConversationMessage, ConversationTurn, MessageRole
from app.services.conversation_archive import ConversationArchive


async def prepared(tmp_path, compressor, **kwargs):
    archive = ConversationArchive(tmp_path, 's')
    memory = ConversationMemory('s', compressor=compressor, archive=archive, max_turns=1,
                                compression_token_threshold=1, background_compression=True, **kwargs)
    await memory.add(ConversationTurn([ConversationMessage(MessageRole.USER, 'old raw')]))
    await memory.add(ConversationTurn([ConversationMessage(MessageRole.USER, 'recent raw')]))
    return memory


async def test_slow_compression_does_not_hold_memory_lock_and_coalesces(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    async def compress(**kwargs):
        entered.set()
        await release.wait()
        return 'summary'
    compressor = SimpleNamespace(compress=AsyncMock(side_effect=compress))
    memory = await prepared(tmp_path, compressor)
    compressor.compress.assert_not_awaited()
    task = asyncio.create_task(memory.force_compress())
    await asyncio.wait_for(entered.wait(), 2)
    newest = ConversationTurn([ConversationMessage(MessageRole.USER, 'new input')])
    await asyncio.wait_for(memory.add(newest), 2)
    assert await memory.force_compress() is False  # One in-flight snapshot.
    release.set()
    assert await task
    assert memory.retrieve_turns() == [newest]
    compressor.compress.assert_awaited_once()


async def test_clear_discards_result_and_generation_failure_keeps_sources(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    async def compress(**kwargs):
        entered.set()
        await release.wait()
        return 'obsolete'
    memory = await prepared(tmp_path, SimpleNamespace(compress=compress))
    task = asyncio.create_task(memory.force_compress())
    await entered.wait()
    memory.clear()
    release.set()
    assert not await task and memory.archive.list_summaries() == []
    assert len(memory.archive.load_turns()) == 2


async def test_two_memory_instances_do_not_publish_overlapping_sources(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    async def slow(**kwargs):
        entered.set()
        await release.wait()
        return 'late'
    first = await prepared(tmp_path, SimpleNamespace(compress=slow))
    second = ConversationMemory('s', compressor=SimpleNamespace(compress=AsyncMock(return_value='first')),
                                archive=ConversationArchive(tmp_path, 's'), max_turns=1)
    await second.restore()
    task = asyncio.create_task(first.force_compress())
    await entered.wait()
    assert await second.force_compress()
    release.set()
    assert not await task
    assert len(first.archive.list_summaries()) == 1


async def test_force_ignores_threshold_but_automatic_attempt_does_not(tmp_path):
    compressor = SimpleNamespace(compress=AsyncMock(return_value='summary'))
    memory = await prepared(tmp_path, compressor)
    memory._compression_token_threshold = 10000
    assert not await memory.compress_pending()
    compressor.compress.assert_not_awaited()
    assert await memory.force_compress()
