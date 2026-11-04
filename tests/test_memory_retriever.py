import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import create_autospec

import pytest

from app.models.longterm_memory import MemoryCategory, MemoryRecord
from app.services.memory_retriever import MemoryRetriever
from app.services.memory_store import MemoryStore
from app.services.memory_vector_index import MemoryVectorIndex


def record(memory_id, user_id="user", **kwargs):
    return MemoryRecord(
        memory_id=memory_id, user_id=user_id, content="memory " + memory_id,
        category=MemoryCategory.FACT, source="conversation", **kwargs,
    )


@pytest.fixture
def setup_retriever():
    store = create_autospec(MemoryStore, instance=True)
    index = create_autospec(MemoryVectorIndex, instance=True)
    store.search.return_value = []
    index.search.return_value = []
    store.get.return_value = None
    return MemoryRetriever(store, index), store, index


@pytest.mark.parametrize("value", [None, True, 0, -1, 1.5, "60"])
def test_rrf_k_validation(setup_retriever, value):
    _, store, index = setup_retriever
    with pytest.raises(ValueError, match="rrf_k"):
        MemoryRetriever(store, index, rrf_k=value)


@pytest.mark.parametrize("kwargs", [
    {"user_id": None}, {"user_id": 1}, {"user_id": " "}, {"user_id": "中" * 86},
    {"query": None}, {"query": 2}, {"query": "\n"}, {"query": "中" * 2667},
    {"limit": True}, {"limit": 0}, {"limit": -1}, {"limit": 101}, {"limit": 1.0},
])
async def test_invalid_input_fails_before_io(setup_retriever, kwargs):
    retriever, store, index = setup_retriever
    with pytest.raises(ValueError):
        await retriever.retrieve(**({"user_id": "user", "query": "query"} | kwargs))
    store.search.assert_not_awaited()
    index.search.assert_not_awaited()
    store.get.assert_not_awaited()


@pytest.mark.parametrize("limit,candidates", [(1, 10), (5, 15), (34, 100), (100, 100)])
async def test_candidate_budget_and_empty_results(setup_retriever, limit, candidates):
    retriever, store, index = setup_retriever
    assert await retriever.retrieve("user", "query", limit=limit) == []
    store.search.assert_awaited_once_with("user", "query", limit=candidates)
    index.search.assert_awaited_once_with("user", "query", limit=candidates)
    store.get.assert_not_awaited()


async def test_rrf_uses_rank_and_authoritative_records(setup_retriever):
    retriever, store, index = setup_retriever
    first, shared, last = record("first"), record("shared"), record("last")
    store.search.return_value = [first, shared]
    index.search.return_value = [("shared", 100.0), ("last", 999.0)]
    current = {r.memory_id: r for r in (first, shared, last)}
    current["shared"] = replace(shared, content="fresh content", version=2)
    store.get.side_effect = lambda user_id, memory_id: current.get(memory_id)
    hits = await retriever.retrieve("user", "query")
    assert [h.record.memory_id for h in hits] == ["shared", "first", "last"]
    assert [h.score for h in hits] == pytest.approx([1 / 62 + 1 / 61, 1 / 61, 1 / 62])
    assert hits[0].record is current["shared"]
    assert hits[0].record.content == "fresh content"


async def test_duplicate_candidates_do_not_cast_extra_votes(setup_retriever):
    retriever, store, index = setup_retriever
    store.search.return_value = [record("a"), record("a"), record("b")]
    index.search.return_value = [("a", 0.0), ("a", 0.1), ("c", 1.0)]
    store.get.side_effect = lambda user_id, memory_id: record(memory_id)
    hits = await retriever.retrieve("user", "query")
    assert [h.record.memory_id for h in hits] == ["a", "b", "c"]
    assert [h.score for h in hits] == pytest.approx([2 / 61, 1 / 63, 1 / 63])
    assert store.get.await_count == 3


@pytest.mark.parametrize("source", ["keyword", "vector"])
async def test_either_source_can_return_empty(setup_retriever, source):
    retriever, store, index = setup_retriever
    if source == "keyword":
        store.search.return_value = [record("a")]
    else:
        index.search.return_value = [("a", 0.0)]
    store.get.return_value = record("a")
    hits = await retriever.retrieve("user", "query")
    assert len(hits) == 1
    assert hits[0].score == pytest.approx(1 / 61)


async def test_ties_are_deterministic_and_limit_stops_reads(setup_retriever):
    retriever, store, index = setup_retriever
    store.search.return_value = [record("z")]
    index.search.return_value = [("a", 0.0)]
    store.get.side_effect = lambda user_id, memory_id: record(memory_id)
    hits = await retriever.retrieve("user", "query", limit=1)
    assert [h.record.memory_id for h in hits] == ["a"]
    store.get.assert_awaited_once_with("user", "a")


async def test_missing_expired_and_wrong_owner_candidates_are_skipped(setup_retriever):
    retriever, store, index = setup_retriever
    now = datetime.now(timezone.utc)
    index.search.return_value = [(mid, rank) for rank, mid in enumerate(
        ["deleted", "expired", "foreign", "wrong-id", "valid", "valid2"]
    )]
    records = {
        "expired": record("expired", expires_at=now - timedelta(seconds=1)),
        "foreign": record("foreign", user_id="other"),
        "wrong-id": record("different-id"),
        "valid": record("valid"),
        "valid2": record("valid2", expires_at=(now + timedelta(days=1)).astimezone(
            timezone(timedelta(hours=8)))),
    }
    store.get.side_effect = lambda uid, mid: records.get(mid)
    hits = await retriever.retrieve("user", "query", limit=2)
    assert [h.record.memory_id for h in hits] == ["valid", "valid2"]
    assert all(args.args[0] == "user" for args in store.get.await_args_list)
    index.delete.assert_not_called()


async def test_wrong_owner_keyword_does_not_boost_vector_candidate(setup_retriever):
    retriever, store, index = setup_retriever
    store.search.return_value = [record("a", user_id="other")]
    index.search.return_value = [("a", 0.0)]
    store.get.return_value = record("a")
    hits = await retriever.retrieve("user", "query")
    assert hits[0].score == pytest.approx(1 / 61)


@pytest.mark.parametrize("expiry", [datetime(2026, 1, 1), "tomorrow"])
async def test_invalid_authoritative_expiry_has_clear_error(setup_retriever, expiry):
    retriever, store, index = setup_retriever
    index.search.return_value = [("a", 0.0)]
    store.get.return_value = record("a", expires_at=expiry)
    with pytest.raises(ValueError, match="timezone-aware"):
        await retriever.retrieve("user", "query")


async def test_expiry_is_checked_after_each_authoritative_read(setup_retriever, monkeypatch):
    retriever, store, index = setup_retriever
    deadline = datetime(2026, 1, 1, tzinfo=timezone.utc)
    index.search.return_value = [("a", 0.0), ("b", 1.0)]
    store.get.side_effect = lambda uid, mid: record(mid, expires_at=deadline)
    clock = create_autospec(datetime)
    clock.now.side_effect = [deadline - timedelta(seconds=1), deadline]
    # Keep isinstance(..., datetime) valid while providing a deterministic clock.
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.now(tz)
    import app.services.memory_retriever as module
    monkeypatch.setattr(module, "datetime", Clock)
    # Use instances of the patched datetime type for validation.
    expiry = Clock(2026, 1, 1, tzinfo=timezone.utc)
    store.get.side_effect = lambda uid, mid: record(mid, expires_at=expiry)
    hits = await retriever.retrieve("user", "query")
    assert [h.record.memory_id for h in hits] == ["a"]


@pytest.mark.parametrize("failing_source", ["keyword", "vector"])
async def test_search_failure_cancels_and_awaits_other_source(setup_retriever, failing_source):
    retriever, store, index = setup_retriever
    started, cancelled = asyncio.Event(), asyncio.Event()
    failure = RuntimeError("search unavailable")

    async def pending(*args, **kwargs):
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    async def fail(*args, **kwargs):
        await started.wait()
        raise failure

    store.search.side_effect = fail if failing_source == "keyword" else pending
    index.search.side_effect = pending if failing_source == "keyword" else fail
    with pytest.raises(RuntimeError) as caught:
        await asyncio.wait_for(retriever.retrieve("user", "query"), timeout=2)
    assert caught.value is failure
    assert cancelled.is_set()
    store.get.assert_not_awaited()


async def test_caller_cancellation_cleans_up_both_search_tasks(setup_retriever):
    retriever, store, index = setup_retriever
    started = [asyncio.Event(), asyncio.Event()]
    finished = [asyncio.Event(), asyncio.Event()]

    async def pending(i):
        started[i].set()
        try:
            await asyncio.Future()
        finally:
            finished[i].set()

    async def keyword(*args, **kwargs):
        return await pending(0)
    async def vector(*args, **kwargs):
        return await pending(1)
    store.search.side_effect = keyword
    index.search.side_effect = vector
    task = asyncio.create_task(retriever.retrieve("user", "query"))
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started)), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert all(event.is_set() for event in finished)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_authoritative_read_failure_does_not_return_partial_results(setup_retriever):
    retriever, store, index = setup_retriever
    index.search.return_value = [("a", 0), ("b", 1)]
    failure = RuntimeError("database unavailable")
    store.get.side_effect = [record("a"), failure]
    with pytest.raises(RuntimeError) as caught:
        await retriever.retrieve("user", "query")
    assert caught.value is failure


async def test_custom_rrf_constant(setup_retriever):
    _, store, index = setup_retriever
    store.search.return_value = [record("a")]
    index.search.return_value = [("a", 0)]
    store.get.return_value = record("a")
    hits = await MemoryRetriever(store, index, rrf_k=1).retrieve("user", "query")
    assert hits[0].score == 1.0
