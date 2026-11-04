import asyncio
import threading
from dataclasses import FrozenInstanceError

import pytest

from app.models.conversation_memory import ConversationMemory, ConversationMessage, ConversationTurn, MessageRole
from app.services.conversation_archive import ConversationArchive


def turn(text):
    return ConversationTurn([ConversationMessage(MessageRole.USER, text)])


class Compressor:
    def __init__(self, result='Project Alpha decision: use SQLite'):
        self.result = result
        self.calls = []

    async def compress(self, previous_summary, turns):
        self.calls.append((previous_summary, tuple(turns)))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_summary_publication_rebuilds_metadata_and_retains_original(tmp_path):
    archive = ConversationArchive(tmp_path, 'session/中文')
    original = turn('Alpha original')
    archive.save_turn(original)
    entry = archive.save_summary([original.turn_id], 'full body', 'Alpha', 'storage choice')
    assert entry.source_turn_ids == (original.turn_id,)
    assert entry.session_id == 'session/中文'
    assert archive.read_summary(entry.memory_id) == 'full body'
    assert ConversationArchive(tmp_path, 'session/中文').list_summaries() == [entry]
    assert archive.read_turn(original.turn_id) == original
    with pytest.raises(FrozenInstanceError):
        entry.title = 'changed'


def test_text_artifacts_are_bounded_and_session_bound(tmp_path):
    archive = ConversationArchive(tmp_path, 'one')
    ref = archive.save_text('abcdef', metadata={'tool': 'search'})
    assert archive.read_text(ref, offset=2, limit=3) == 'cde'
    for bad in ('../secret', ref.replace('ctx:', 'file:', 1)):
        with pytest.raises(ValueError):
            archive.read_text(bad)
    with pytest.raises(ValueError):
        ConversationArchive(tmp_path, 'two').read_text(ref)
    for kwargs in ({'offset': -1}, {'limit': 16001}, {'limit': True}):
        with pytest.raises(ValueError):
            archive.read_text(ref, **kwargs)


@pytest.mark.asyncio
async def test_bounded_raw_stage_summaries_reload_and_search(tmp_path):
    archive = ConversationArchive(tmp_path, 's')
    originals = [turn(f'raw {i}') for i in range(13)]
    for item in originals:
        archive.save_turn(item)
    compressor = Compressor()
    memory = ConversationMemory('u', compressor, max_turns=2, archive=archive)
    await memory.restore()
    assert await memory.force_compress()
    assert len(compressor.calls[0][1]) == 8
    assert compressor.calls[0][0] is None
    assert await memory.force_compress()
    assert compressor.calls[1] == (None, tuple(originals[8:11]))
    assert not await memory.force_compress()
    assert memory.retrieve_turns() == originals[-2:]
    restored = ConversationMemory('u', archive=archive, max_turns=2)
    await restored.restore()
    assert restored.summary_index == memory.summary_index
    assert isinstance(restored.summary_index, tuple)
    entry = restored.find_summaries('Alpha')[0]
    assert await restored.load_summary(entry.memory_id) == 'Project Alpha decision: use SQLite'
    assert archive.load_turns() == originals
    with pytest.raises(KeyError):
        await restored.load_summary('foreign')


@pytest.mark.asyncio
async def test_failure_retains_recoverable_bounded_sources(tmp_path):
    archive = ConversationArchive(tmp_path, 's')
    compressor = Compressor(RuntimeError('unavailable'))
    memory = ConversationMemory('u', compressor, max_turns=1, compression_token_threshold=1, archive=archive)
    originals = [turn('a' * 100) for _ in range(30)]
    await memory.add_turns(originals)
    assert len(memory.get_compression_buffer()) <= 8
    assert memory.summary_index == ()
    assert archive.load_turns() == originals


@pytest.mark.asyncio
async def test_automatic_summary_and_clear_during_compression(tmp_path):
    archive = ConversationArchive(tmp_path, 's')
    compressor = Compressor()
    memory = ConversationMemory('u', compressor, max_turns=1, compression_token_threshold=10, archive=archive)
    await memory.add_turns([turn('x' * 24), turn('current')])
    assert len(memory.summary_index) == 1
    started = asyncio.Event()
    finish = asyncio.Event()

    class Waiting:
        async def compress(self, previous_summary, turns):
            started.set()
            await finish.wait()
            return 'stale summary'

    memory._compressor = Waiting()
    await memory.add(turn('latest'))
    task = asyncio.create_task(memory.force_compress())
    await started.wait()
    memory.clear()
    finish.set()
    assert await task is False
    assert memory.summary_index == ()
    assert memory.retrieve_turns() == []
    assert len(archive.list_summaries()) == 1



@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [False, True])
async def test_clear_or_cancel_during_disk_publish_never_commits_summary(tmp_path, monkeypatch, cancel):
    archive = ConversationArchive(tmp_path, 's')
    originals = [turn('original'), turn('live')]
    for original in originals:
        archive.save_turn(original)
    memory = ConversationMemory('u', Compressor(), archive=archive, max_turns=1)
    await memory.restore()
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    save = archive.save_summary

    def delayed_save(*args, **kwargs):
        started.set()
        assert release.wait(5)
        try:
            return save(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(archive, 'save_summary', delayed_save)
    task = asyncio.create_task(memory.force_compress())
    assert await asyncio.to_thread(started.wait, 5)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        memory.clear()
    release.set()
    assert await asyncio.to_thread(finished.wait, 5)
    if not cancel:
        assert await task is False
    assert memory.summary_index == ()
    assert archive.list_summaries() == []
    restored = ConversationMemory('u', archive=archive, max_turns=1)
    await restored.restore()
    assert restored.summary_index == ()
    assert archive.load_turns() == originals


@pytest.mark.asyncio
async def test_archive_without_compressor_keeps_no_pending_raw_buffer(tmp_path, monkeypatch):
    archive = ConversationArchive(tmp_path, 's')
    memory = ConversationMemory('u', archive=archive, max_turns=1)

    def forbidden_scan(*args):
        pytest.fail('no compressor must not load old raw sources')

    monkeypatch.setattr(archive, 'pending_summary_turns', forbidden_scan)
    await memory.add_turns([turn('old'), turn('live')])
    await memory.restore()
    assert memory.get_compression_buffer() == []
    assert not await memory.force_compress()


@pytest.mark.asyncio
async def test_oversized_source_remains_discoverable_without_partial_summary(tmp_path):
    archive = ConversationArchive(tmp_path, 's')
    large = turn('rarekeyword ' + 'x' * 400)
    archive.save_turn(large)
    archive.save_turn(turn('live'))
    compressor = Compressor()
    memory = ConversationMemory('u', compressor, archive=archive, max_turns=1, summary_token_budget=10)
    await memory.restore()
    assert not await memory.force_compress()
    found = await memory.find_turns('rarekeyword', limit=1)
    assert len(found) == 1
    assert found[0]['turn_id'] == large.turn_id
    assert found[0]['storage_ref'] == 'turn:' + large.turn_id
    assert len(found[0]['description']) <= 200
    assert archive.read_turn(found[0]['turn_id']) == large
    assert memory.summary_index == ()
    assert compressor.calls == []


def test_staged_summary_requires_explicit_commit(tmp_path):
    archive = ConversationArchive(tmp_path, 's')
    original = turn('original')
    archive.save_turn(original)
    entry = archive.save_summary([original.turn_id], 'body', 'title', 'description', committed=False)
    assert archive.list_summaries() == []
    archive.commit_summary(entry)
    assert archive.list_summaries() == [entry]
    assert archive.read_summary(entry.memory_id) == 'body'

@pytest.mark.asyncio
async def test_clear_during_restore_cannot_refill_recent_cache(tmp_path, monkeypatch):
    archive = ConversationArchive(tmp_path, 's')
    archive.save_turn(turn('old'))
    memory = ConversationMemory('u', archive=archive)
    started = threading.Event()
    release = threading.Event()
    load = archive.load_turns

    def delayed_load(*args):
        started.set()
        assert release.wait(5)
        return load(*args)

    monkeypatch.setattr(archive, 'load_turns', delayed_load)
    task = asyncio.create_task(memory.restore())
    assert await asyncio.to_thread(started.wait, 5)
    memory.clear()
    release.set()
    await task
    assert memory.retrieve_turns() == []

@pytest.mark.asyncio
async def test_legacy_clear_during_compression_does_not_revive_summary():
    started, release = asyncio.Event(), asyncio.Event()

    class Waiting:
        async def compress(self, previous_summary, turns):
            started.set()
            await release.wait()
            return 'stale'

    memory = ConversationMemory('u', Waiting(), max_turns=1)
    await memory.add_turns([turn('old'), turn('live')])
    task = asyncio.create_task(memory.force_compress())
    await started.wait()
    memory.clear()
    release.set()
    assert await task is False
    assert memory.summary is None
    assert memory.get_compression_buffer() == []


@pytest.mark.asyncio
@pytest.mark.parametrize('result', ['', None, ['not text']])
async def test_invalid_summary_keeps_original_sources(tmp_path, result):
    archive = ConversationArchive(tmp_path, 's')
    originals = [turn('old'), turn('live')]
    for original in originals:
        archive.save_turn(original)
    memory = ConversationMemory('u', Compressor(result), archive=archive, max_turns=1)
    await memory.restore()
    assert not await memory.force_compress()
    assert memory.get_compression_buffer() == originals[:1]
    assert archive.list_summaries() == []
    assert archive.load_turns() == originals


def test_failed_summary_commit_never_exposes_staged_body(tmp_path, monkeypatch):
    import os
    archive = ConversationArchive(tmp_path, 's')
    original = turn('original')
    archive.save_turn(original)
    entry = archive.save_summary([original.turn_id], 'body', 'title', 'description', committed=False)

    def fail(*args):
        raise OSError('disk unavailable')

    monkeypatch.setattr(os, 'replace', fail)
    with pytest.raises(OSError):
        archive.commit_summary(entry)
    assert archive.list_summaries() == []
    assert archive.read_turn(original.turn_id) == original
