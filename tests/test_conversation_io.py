"""Deterministic correctness and body-I/O gates for the conversation harness."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import Mock

import pytest

from app.models.conversation_memory import ConversationMemory, ConversationMessage, ConversationTurn, MessageRole
from app.services.conversation_archive import ConversationArchive


def turn(index):
    return ConversationTurn([ConversationMessage(MessageRole.USER, f'content-{index}')], turn_id=f'id-{index}')


def seed(archive, count=40):
    turns = [turn(i) for i in range(count)]
    for item in turns:
        archive.save_turn(item)
    return turns


def test_warm_save_does_not_decode_unchanged_history(tmp_path, monkeypatch):
    archive = ConversationArchive(tmp_path, 's')
    seed(archive)
    reader = Mock(wraps=archive._read)
    monkeypatch.setattr(archive, '_read', reader)
    archive.save_turn(turn(40))
    assert reader.call_count == 0


def test_warm_duplicate_reads_only_target_and_rejects_changed_content(tmp_path, monkeypatch):
    archive = ConversationArchive(tmp_path, 's')
    originals = seed(archive)
    # Duplicates validate the immutable input, not its materialized outcome.
    reader = Mock(wraps=archive._read_original)
    monkeypatch.setattr(archive, '_read_original', reader)
    archive.save_turn(originals[20])
    assert reader.call_count == 1
    with pytest.raises(ValueError, match='conflict'):
        archive.save_turn(replace(originals[20], messages=turn(100).messages))


def test_changed_cold_record_still_rejected_before_append(tmp_path):
    archive = ConversationArchive(tmp_path, 's')
    seed(archive, 3)
    path = sorted(archive._directory.glob('*.json'))[0]
    path.write_text('{"broken":true}', encoding='utf-8')
    with pytest.raises(ValueError, match='archive'):
        archive.save_turn(turn(9))
    assert len(list(archive._directory.glob('*.json'))) == 3


def test_multiple_instances_keep_insertion_order_and_detect_new_records(tmp_path):
    first, second = ConversationArchive(tmp_path, 's'), ConversationArchive(tmp_path, 's')
    originals = [turn(i) for i in range(12)]
    with ThreadPoolExecutor() as pool:
        futures = [pool.submit((first if i % 2 else second).save_turn, item)
                   for i, item in enumerate(originals)]
        for future in futures:
            future.result()
    loaded = first.load_turns()
    assert {item.turn_id for item in loaded} == {item.turn_id for item in originals}
    assert second.load_turns() == loaded
    first.save_turn(originals[0])
    assert ConversationArchive(tmp_path, 's').load_turns() == loaded


def test_failed_publish_retry_does_not_poison_metadata(tmp_path, monkeypatch):
    import os
    archive = ConversationArchive(tmp_path, 's')
    originals = seed(archive, 2)
    pending = turn(2)
    publish = os.replace
    monkeypatch.setattr(os, 'replace', Mock(side_effect=OSError('disk full')))
    with pytest.raises(OSError):
        archive.save_turn(pending)
    monkeypatch.setattr(os, 'replace', publish)
    archive.save_turn(pending)
    archive.save_turn(pending)
    assert archive.load_turns() == [*originals, pending]


def test_replaced_cold_record_is_revalidated_even_when_recent_tail_unchanged(tmp_path):
    import json
    archive = ConversationArchive(tmp_path, 's')
    seed(archive, 3)
    path = sorted(archive._directory.glob('*.json'))[0]
    data = json.loads(path.read_text(encoding='utf-8'))
    data['session_id'] = 'foreign'
    replacement = path.with_suffix('.tmp')
    replacement.write_text(json.dumps(data), encoding='utf-8')
    replacement.replace(path)
    with pytest.raises(ValueError, match='archive'):
        archive.save_turn(turn(9))


def test_returned_turn_mutation_does_not_mutate_durable_original(tmp_path):
    archive = ConversationArchive(tmp_path, 's')
    original = turn(0)
    archive.save_turn(original)
    loaded = archive.load_turns(1)
    loaded[0].messages[0].content = 'changed in caller'
    archive.save_turn(original)
    assert archive.read_turn(original.turn_id) == original


def test_warm_bounded_load_reads_only_recent_bodies_but_audits_changed_cold(tmp_path, monkeypatch):
    archive = ConversationArchive(tmp_path, 's')
    originals = seed(archive)
    reader = Mock(wraps=archive._read)
    monkeypatch.setattr(archive, '_read', reader)
    assert archive.load_turns(5) == originals[-5:]
    assert reader.call_count == 5
    reader.reset_mock()
    assert archive.load_turns(0) == []
    assert reader.call_count == 0
    path = sorted(archive._directory.glob('*.json'))[0]
    path.write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError, match='archive'):
        archive.load_turns(5)


def test_pending_selection_uses_metadata_and_reads_only_chosen_batch(tmp_path, monkeypatch):
    archive = ConversationArchive(tmp_path, 's')
    originals = seed(archive)
    reader = Mock(wraps=archive._read)
    monkeypatch.setattr(archive, '_read', reader)
    batch, tokens = archive.pending_summary_turns(set(), 5, 8, 6000)
    assert batch == originals[:8]
    assert tokens == sum(ConversationMemory._estimate_turn_tokens(item) for item in originals[:-5])
    assert reader.call_count == 8


async def test_add_runs_public_save_and_recent_reload_in_one_worker(tmp_path, monkeypatch):
    import asyncio
    archive = ConversationArchive(tmp_path, 's')
    seed(archive)
    memory = ConversationMemory('u', archive=archive, max_turns=5)
    await memory.restore()
    worker = Mock(wraps=asyncio.to_thread)
    reader = Mock(wraps=archive._read)
    monkeypatch.setattr(asyncio, 'to_thread', worker)
    monkeypatch.setattr(archive, '_read', reader)
    new = turn(41)
    await memory.add(new)
    assert worker.call_count == 1
    assert reader.call_count <= 5
    assert memory.retrieve_turns()[-1] == new


async def test_clear_during_add_worker_never_refills_cache(tmp_path, monkeypatch):
    import asyncio
    import threading
    archive = ConversationArchive(tmp_path, 's')
    memory = ConversationMemory('u', archive=archive)
    started, release = threading.Event(), threading.Event()
    save = archive.save_turn
    new = turn(1)
    def blocked(item):
        started.set()
        assert release.wait(5)
        save(item)
    monkeypatch.setattr(archive, 'save_turn', blocked)
    task = asyncio.create_task(memory.add(new))
    assert await asyncio.to_thread(started.wait, 5)
    memory.clear()
    release.set()
    await task
    assert memory.retrieve_turns() == []
    assert archive.load_turns() == [new]


async def test_recent_reload_failure_retains_cache_and_retry_recovers(tmp_path, monkeypatch):
    archive = ConversationArchive(tmp_path, 's')
    memory = ConversationMemory('u', archive=archive, max_turns=1)
    original, new = turn(1), turn(2)
    await memory.add(original)
    load = archive.load_turns
    monkeypatch.setattr(archive, 'load_turns', Mock(side_effect=OSError('read failed')))
    with pytest.raises(OSError):
        await memory.add(new)
    assert memory.retrieve_turns() == [original]
    monkeypatch.setattr(archive, 'load_turns', load)
    await memory.add(new)
    assert memory.retrieve_turns() == [new]
    assert archive.load_turns() == [original, new]


def test_cold_recent_load_does_not_decode_the_tail_twice(tmp_path, monkeypatch):
    archive = ConversationArchive(tmp_path, 's')
    originals = seed(archive)
    reopened = ConversationArchive(tmp_path, 's')
    reader = Mock(wraps=reopened._read)
    monkeypatch.setattr(reopened, '_read', reader)
    assert reopened.load_turns(5) == originals[-5:]
    assert reader.call_count == len(originals)


def test_publication_metadata_uses_durable_snapshot_not_mutated_caller(tmp_path, monkeypatch):
    import copy
    import os
    archive = ConversationArchive(tmp_path, 's')
    incoming = turn(1)
    original = copy.deepcopy(incoming)
    publish = os.replace
    def mutate_after_publish(*args):
        publish(*args)
        incoming.turn_id = 'caller-mutated-id'
        incoming.messages[0].content = 'x' * 10000
    monkeypatch.setattr(os, 'replace', mutate_after_publish)
    archive.save_turn(incoming)
    monkeypatch.setattr(os, 'replace', publish)
    archive.save_turn(original)
    archive.save_turn(turn(2))
    assert archive.load_turns() == [original, archive.read_turn('id-2')]
    _, pending = archive.pending_summary_turns(set(), 1, 8, 6000)
    assert pending == ConversationMemory._estimate_turn_tokens(original)
