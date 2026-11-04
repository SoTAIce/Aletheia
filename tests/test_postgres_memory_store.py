import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import asyncpg
import pytest

from app.models.longterm_memory import MemoryCategory, MemoryRecord
from app.services.postgres_memory_store import PostgresMemoryStore


def record():
    return MemoryRecord(
        user_id="user", content="  Original content\n", category=MemoryCategory.FACT,
        source="conversation", source_id="turn-1",
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )


def pool_fixture():
    events = []

    @asynccontextmanager
    async def transaction():
        events.append("begin")
        try:
            yield
        except BaseException:
            events.append("rollback")
            raise
        else:
            events.append("commit")

    conn = SimpleNamespace(
        execute=AsyncMock(), fetchrow=AsyncMock(), transaction=Mock(side_effect=transaction),
    )

    @asynccontextmanager
    async def acquire():
        yield conn

    pool = SimpleNamespace(
        acquire=Mock(side_effect=acquire), close=AsyncMock(), terminate=Mock(),
    )
    return pool, conn, events


@pytest.mark.parametrize("dsn", [None, 123, "", "   "])
def test_invalid_dsn_has_actionable_error(dsn):
    with pytest.raises(ValueError, match="dsn"):
        PostgresMemoryStore(dsn)


@pytest.mark.asyncio
async def test_lifecycle_is_idempotent_and_serializes_concurrent_initialization(monkeypatch):
    pool, conn, events = pool_fixture()

    async def create(**kwargs):
        await asyncio.sleep(0)
        return pool

    factory = AsyncMock(side_effect=create)
    monkeypatch.setattr(asyncpg, "create_pool", factory)
    store = PostgresMemoryStore(" postgresql://localhost/test ")
    await asyncio.gather(store.initialize(), store.initialize())
    factory.assert_awaited_once_with(dsn="postgresql://localhost/test", min_size=1, max_size=5)
    assert conn.execute.await_count == 2
    assert events == ["begin", "commit"]
    await asyncio.gather(store.close(), store.close())
    pool.close.assert_awaited_once()
    assert store._pool is None
    await store.initialize()
    assert factory.await_count == 2
    await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("index failed"), asyncio.CancelledError()])
async def test_schema_failure_rolls_back_and_terminates_pool_before_retry(monkeypatch, failure):
    pool, conn, events = pool_fixture()
    factory = AsyncMock(return_value=pool)
    monkeypatch.setattr(asyncpg, "create_pool", factory)
    conn.execute.side_effect = [None, failure]
    store = PostgresMemoryStore("postgresql://localhost/test")
    with pytest.raises(type(failure)) as caught:
        await store.initialize()
    assert caught.value is failure
    assert events == ["begin", "rollback"]
    pool.terminate.assert_called_once()
    assert store._pool is None
    conn.execute.side_effect = None
    await store.initialize()
    assert factory.await_count == 2
    await store.close()


@pytest.mark.asyncio
async def test_close_during_initialization_waits_for_the_published_pool(monkeypatch):
    pool, _, _ = pool_fixture()
    started, proceed = asyncio.Event(), asyncio.Event()

    async def create(**kwargs):
        started.set()
        await proceed.wait()
        return pool

    monkeypatch.setattr(asyncpg, "create_pool", create)
    store = PostgresMemoryStore("postgresql://localhost/test")
    initializing = asyncio.create_task(store.initialize())
    await started.wait()
    closing = asyncio.create_task(store.close())
    await asyncio.sleep(0)
    assert not closing.done()
    proceed.set()
    await asyncio.gather(initializing, closing)
    assert store._pool is None
    pool.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_add_preserves_record_and_binds_values_without_sql_interpolation():
    pool, conn, _ = pool_fixture()
    store = PostgresMemoryStore("postgresql://localhost/test")
    store._pool = pool
    item = replace(record(), content="Robert'); DROP TABLE memories; --\n")
    before = asdict(item)
    assert await store.add(item) is item
    sql, *values = conn.execute.call_args.args
    assert "$1" in sql and "$10" in sql and item.content not in sql
    assert values == [item.memory_id, item.user_id, item.content, item.category.value,
                      item.source, item.source_id, item.version, item.created_at,
                      item.updated_at, item.expires_at]
    assert asdict(item) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("changes,error", [
    ({"memory_id": " "}, ValueError), ({"user_id": None}, ValueError),
    ({"content": 42}, ValueError), ({"source": ""}, ValueError),
    ({"category": "fact"}, TypeError), ({"source_id": 1}, TypeError),
    ({"version": True}, ValueError), ({"version": 2}, ValueError),
    ({"created_at": None}, TypeError), ({"updated_at": "yesterday"}, TypeError),
    ({"expires_at": 1}, TypeError),
    ({"created_at": datetime(2026, 1, 1)}, ValueError),
    ({"updated_at": datetime(2026, 1, 1)}, ValueError),
    ({"expires_at": datetime(2026, 1, 1)}, ValueError),
])
async def test_invalid_records_are_rejected_before_database_access(changes, error):
    pool, conn, _ = pool_fixture()
    store = PostgresMemoryStore("postgresql://localhost/test")
    store._pool = pool
    with pytest.raises(error):
        await store.add(replace(record(), **changes))
    pool.acquire.assert_not_called()
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_initialization_and_invalid_record_type_are_explicit():
    store = PostgresMemoryStore("postgresql://localhost/test")
    with pytest.raises(TypeError, match="MemoryRecord"):
        await store.add({})
    with pytest.raises(RuntimeError, match="not initialized"):
        await store.add(record())
    with pytest.raises(RuntimeError, match="not initialized"):
        await store.get("user", "memory")
    await store.close()


@pytest.mark.asyncio
async def test_duplicate_id_is_translated_but_unrelated_database_errors_propagate():
    pool, conn, _ = pool_fixture()
    store = PostgresMemoryStore("postgresql://localhost/test")
    store._pool = pool
    duplicate = asyncpg.UniqueViolationError("duplicate")
    conn.execute.side_effect = duplicate
    with pytest.raises(ValueError, match="already exists") as caught:
        await store.add(record())
    assert caught.value.__cause__ is duplicate
    unavailable = asyncpg.PostgresConnectionError("disconnected")
    conn.execute.side_effect = unavailable
    with pytest.raises(asyncpg.PostgresConnectionError) as caught:
        await store.add(record())
    assert caught.value is unavailable


@pytest.mark.asyncio
async def test_get_scopes_query_to_user_and_converts_category_without_losing_fields():
    pool, conn, _ = pool_fixture()
    store = PostgresMemoryStore("postgresql://localhost/test")
    store._pool = pool
    item = record()
    conn.fetchrow.return_value = asdict(item) | {"category": item.category.value}
    actual = await store.get(item.user_id, item.memory_id)
    assert actual == item and actual.category is MemoryCategory.FACT
    sql, user_id, memory_id = conn.fetchrow.call_args.args
    assert "WHERE user_id = $1 AND memory_id = $2" in sql
    assert (user_id, memory_id) == (item.user_id, item.memory_id)
    conn.fetchrow.return_value = None
    assert await store.get("another-user", item.memory_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id,memory_id", [(None, "id"), (" ", "id"), ("user", 1), ("user", "")])
async def test_invalid_lookup_does_not_access_database(user_id, memory_id):
    pool, _, _ = pool_fixture()
    store = PostgresMemoryStore("postgresql://localhost/test")
    store._pool = pool
    with pytest.raises(ValueError):
        await store.get(user_id, memory_id)
    pool.acquire.assert_not_called()
