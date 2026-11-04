import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import create_autospec

import pytest

from app.models.longterm_memory import (
    MemoryAction, MemoryCandidate, MemoryCategory, MemoryDecision, MemoryHit, MemoryRecord,
)
from app.services.extrator import MemoryExtractor
from app.services.memory_resolver import MemoryResolver
from app.services.memory_retriever import MemoryRetriever
from app.services.memory_service import LongTermMemoryService
from app.services.memory_store import MemoryStore
from app.services.memory_vector_index import MemoryVectorIndex


@pytest.fixture
def components():
    extractor = create_autospec(MemoryExtractor, instance=True)
    retriever = create_autospec(MemoryRetriever, instance=True)
    resolver = create_autospec(MemoryResolver, instance=True)
    store = create_autospec(MemoryStore, instance=True)
    index = create_autospec(MemoryVectorIndex, instance=True)
    extractor.extract.return_value = [MemoryCandidate('new fact', MemoryCategory.FACT, 'conversation', 't1')]
    retriever.retrieve.return_value = []
    resolver.resolve.return_value = MemoryDecision(MemoryAction.ADD, reason='new')
    store.add.side_effect = lambda record: record
    service = LongTermMemoryService(extractor, retriever, resolver, store, index)
    return service, extractor, retriever, resolver, store, index


def record(**kwargs):
    return replace(MemoryRecord('u', 'old fact', MemoryCategory.FACT, 'document', memory_id='m1'), **kwargs)


async def remember(service, **kwargs):
    return await service.remember(**({'user_id': 'u', 'text': 'raw text',
                                     'source': 'conversation', 'source_id': 't1'} | kwargs))


async def test_add_returns_decision_and_indexes_the_saved_record(components):
    service, extractor, _, _, store, index = components
    result = await remember(service)
    assert result == [MemoryDecision(MemoryAction.ADD, reason='new')]
    extractor.extract.assert_awaited_once_with(text='raw text', source='conversation', source_id='t1')
    saved = store.add.call_args.args[0]
    assert saved.user_id == 'u' and saved.content == 'new fact'
    assert saved.source == 'conversation' and saved.source_id == 't1'
    index.upsert.assert_awaited_once_with(user_id='u', memory_id=saved.memory_id, content='new fact')


async def test_empty_extraction_returns_empty_without_retrieval(components):
    service, extractor, retriever, resolver, store, index = components
    extractor.extract.return_value = []
    assert await remember(service) == []
    retriever.retrieve.assert_not_awaited()
    resolver.resolve.assert_not_awaited()
    store.add.assert_not_awaited()
    index.upsert.assert_not_awaited()


@pytest.mark.parametrize('action', [MemoryAction.SKIP, MemoryAction.CONFLICT])
async def test_nonwriting_decisions_are_returned_without_changes(components, action):
    service, _, retriever, resolver, store, index = components
    retriever.retrieve.return_value = [MemoryHit(record(), .1)]
    decision = MemoryDecision(action, 'm1', 'existing')
    resolver.resolve.return_value = decision
    assert await remember(service) == [decision]
    store.add.assert_not_awaited()
    store.update.assert_not_awaited()
    index.upsert.assert_not_awaited()


async def test_update_preserves_identity_version_and_lifetime(components):
    service, _, retriever, resolver, store, index = components
    old = record(version=4, expires_at=datetime.now(timezone.utc) + timedelta(days=1))
    retriever.retrieve.return_value = [MemoryHit(old, .1)]
    store.get.return_value = old
    store.update.side_effect = lambda item: replace(item, version=5)
    decision = MemoryDecision(MemoryAction.UPDATE, 'm1', 'correction')
    resolver.resolve.return_value = decision
    assert await remember(service) == [decision]
    proposed = store.update.call_args.args[0]
    assert proposed.memory_id == old.memory_id and proposed.user_id == old.user_id
    assert proposed.version == 4 and proposed.created_at == old.created_at
    assert proposed.expires_at == old.expires_at and proposed.content == 'new fact'
    assert proposed.source_id == 't1'
    index.upsert.assert_awaited_once_with(user_id='u', memory_id='m1', content='new fact')


async def test_multiple_candidates_return_all_decisions_in_input_order(components):
    service, extractor, retriever, resolver, store, index = components
    first = extractor.extract.return_value[0]
    extractor.extract.return_value = [first, replace(first, content='other fact')]
    retriever.retrieve.return_value = [MemoryHit(record(), .1)]
    decisions = [MemoryDecision(MemoryAction.SKIP, 'm1', 'same'),
                 MemoryDecision(MemoryAction.ADD, reason='other')]
    resolver.resolve.side_effect = decisions
    assert await remember(service) == decisions
    assert retriever.retrieve.await_count == 2
    assert store.add.await_count == index.upsert.await_count == 1


@pytest.mark.parametrize('kwargs', [
    {'user_id': ''}, {'user_id': None}, {'user_id': 'x' * 257},
    {'text': ' '}, {'source': ''}, {'source': 3}, {'source_id': 42},
])
async def test_invalid_remember_input_rejected_before_extraction(components, kwargs):
    service, extractor, *_ = components
    with pytest.raises((ValueError, TypeError)):
        await remember(service, **kwargs)
    extractor.extract.assert_not_awaited()


@pytest.mark.parametrize('decision', [
    MemoryDecision(MemoryAction.ADD, 'm1'),
    MemoryDecision(MemoryAction.UPDATE),
    MemoryDecision(MemoryAction.UPDATE, 'unretrieved'),
    MemoryDecision(MemoryAction.SKIP, 'unretrieved'),
    MemoryDecision(MemoryAction.CONFLICT, 'unretrieved'),
    MemoryDecision('unexpected'),
])
async def test_invalid_decision_cannot_write(components, decision):
    service, _, retriever, resolver, store, index = components
    retriever.retrieve.return_value = [MemoryHit(record(), .1)]
    resolver.resolve.return_value = decision
    with pytest.raises((ValueError, TypeError)):
        await remember(service)
    store.add.assert_not_awaited()
    store.update.assert_not_awaited()
    index.upsert.assert_not_awaited()


async def test_foreign_history_rejected_before_resolver(components):
    service, _, retriever, resolver, store, _ = components
    retriever.retrieve.return_value = [MemoryHit(record(user_id='other'), .1)]
    with pytest.raises(ValueError, match='user'):
        await remember(service)
    resolver.resolve.assert_not_awaited()
    store.add.assert_not_awaited()


@pytest.mark.parametrize('current', [None, record(user_id='other'), record(memory_id='other'), record(version=2)])
async def test_update_rejects_missing_foreign_or_changed_target(components, current):
    service, _, retriever, resolver, store, index = components
    retriever.retrieve.return_value = [MemoryHit(record(), .1)]
    resolver.resolve.return_value = MemoryDecision(MemoryAction.UPDATE, 'm1')
    store.get.return_value = current
    with pytest.raises(ValueError):
        await remember(service)
    store.update.assert_not_awaited()
    index.upsert.assert_not_awaited()


async def test_store_failure_does_not_touch_index(components):
    service, _, _, _, store, index = components
    failure = RuntimeError('database unavailable')
    store.add.side_effect = failure
    with pytest.raises(RuntimeError) as caught:
        await remember(service)
    assert caught.value is failure
    index.upsert.assert_not_awaited()


async def test_index_failure_reports_committed_record_without_replaying_or_rollback(components):
    from app.services.memory_service import MemoryIndexSyncError
    service, extractor, _, _, store, index = components
    extractor.extract.return_value *= 2
    failure = RuntimeError('milvus unavailable')
    index.upsert.side_effect = failure
    with pytest.raises(MemoryIndexSyncError) as caught:
        await remember(service)
    error = caught.value
    assert error.__cause__ is failure
    assert error.user_id == 'u' and error.memory_id == store.add.call_args.args[0].memory_id
    assert error.operation == 'upsert' and error.database_changed is True
    assert store.add.await_count == index.upsert.await_count == 1
    store.delete.assert_not_awaited()


async def test_recall_delegates_with_limit(components):
    service, _, retriever, *_ = components
    hits = [MemoryHit(record(), .1)]
    retriever.retrieve.return_value = hits
    assert await service.recall('u', 'query', 3) == hits
    retriever.retrieve.assert_awaited_once_with(user_id='u', query='query', limit=3)


@pytest.mark.parametrize('deleted', [True, False])
async def test_forget_always_cleans_index_even_if_database_record_is_absent(components, deleted):
    service, _, _, _, store, index = components
    store.delete.return_value = deleted
    assert await service.forget('u', 'm1') is deleted
    store.delete.assert_awaited_once_with(user_id='u', memory_id='m1')
    index.delete.assert_awaited_once_with(user_id='u', memory_id='m1')


async def test_forget_database_failure_leaves_index_untouched(components):
    service, _, _, _, store, index = components
    store.delete.side_effect = RuntimeError('database failed')
    with pytest.raises(RuntimeError, match='database failed'):
        await service.forget('u', 'm1')
    index.delete.assert_not_awaited()


@pytest.mark.parametrize('deleted', [True, False])
async def test_forget_index_failure_exposes_database_result(components, deleted):
    from app.services.memory_service import MemoryIndexSyncError
    service, _, _, _, store, index = components
    store.delete.return_value = deleted
    index.delete.side_effect = RuntimeError('index failed')
    with pytest.raises(MemoryIndexSyncError) as caught:
        await service.forget('u', 'm1')
    assert caught.value.database_changed is deleted
    assert caught.value.operation == 'delete' and caught.value.memory_id == 'm1'


async def test_cancellation_is_not_swallowed_or_retried(components):
    service, _, _, _, store, index = components
    index.upsert.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await remember(service)
    store.add.assert_awaited_once()
    index.upsert.assert_awaited_once()


@pytest.mark.parametrize('kwargs', [
    {'user_id': ''}, {'user_id': 'x' * 257}, {'query': None},
    {'query': 'q' * 8001}, {'limit': True}, {'limit': 0}, {'limit': 101},
])
async def test_invalid_recall_does_not_query_dependencies(components, kwargs):
    service, _, retriever, *_ = components
    with pytest.raises(ValueError):
        await service.recall(**({'user_id': 'u', 'query': 'query'} | kwargs))
    retriever.retrieve.assert_not_awaited()


@pytest.mark.parametrize('kwargs', [
    {'user_id': None}, {'user_id': 'x' * 257},
    {'memory_id': ' '}, {'memory_id': 'm' * 101},
])
async def test_invalid_forget_cannot_delete(components, kwargs):
    service, _, _, _, store, index = components
    with pytest.raises(ValueError):
        await service.forget(**({'user_id': 'u', 'memory_id': 'm1'} | kwargs))
    store.delete.assert_not_awaited()
    index.delete.assert_not_awaited()


@pytest.mark.parametrize('stage', ['extract', 'retrieve', 'resolve'])
async def test_upstream_failure_propagates_without_writing(components, stage):
    service, extractor, retriever, resolver, store, index = components
    method = {'extract': extractor.extract, 'retrieve': retriever.retrieve,
              'resolve': resolver.resolve}[stage]
    failure = RuntimeError('upstream unavailable')
    method.side_effect = failure
    with pytest.raises(RuntimeError) as caught:
        await remember(service)
    assert caught.value is failure
    store.add.assert_not_awaited()
    store.update.assert_not_awaited()
    index.upsert.assert_not_awaited()


async def test_update_database_conflict_does_not_publish_vector(components):
    service, _, retriever, resolver, store, index = components
    retriever.retrieve.return_value = [MemoryHit(record(), .1)]
    resolver.resolve.return_value = MemoryDecision(MemoryAction.UPDATE, 'm1')
    store.get.return_value = record()
    store.update.side_effect = ValueError('version conflict')
    with pytest.raises(ValueError, match='version conflict'):
        await remember(service)
    index.upsert.assert_not_awaited()


async def test_update_index_failure_leaves_database_update_intact(components):
    from app.services.memory_service import MemoryIndexSyncError
    service, _, retriever, resolver, store, index = components
    old = record()
    retriever.retrieve.return_value = [MemoryHit(old, .1)]
    resolver.resolve.return_value = MemoryDecision(MemoryAction.UPDATE, 'm1')
    store.get.return_value = old
    store.update.side_effect = lambda item: replace(item, version=2)
    index.upsert.side_effect = RuntimeError('index unavailable')
    with pytest.raises(MemoryIndexSyncError) as caught:
        await remember(service)
    assert caught.value.database_changed is True and caught.value.memory_id == 'm1'
    store.update.assert_awaited_once()
    store.delete.assert_not_awaited()


async def test_persisted_identity_mismatch_is_not_indexed(components):
    service, _, _, _, store, index = components
    store.add.side_effect = lambda item: replace(item, user_id='other')
    with pytest.raises(ValueError, match='user or ID'):
        await remember(service)
    index.upsert.assert_not_awaited()
