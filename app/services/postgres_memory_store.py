"""PostgreSQL storage for adding and reading long-term memory records."""

import asyncio
from datetime import datetime

import asyncpg

from app.models.longterm_memory import MemoryCategory, MemoryRecord


class PostgresMemoryStore:
    def __init__(self, dsn: str) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("dsn must be a non-empty string")

        self._dsn = dsn.strip()
        self._pool: asyncpg.Pool | None = None
        self._lifecycle_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Create one pool and publish it only after schema setup succeeds."""
        async with self._lifecycle_lock:
            if self._pool is not None:
                return

            pool = await asyncpg.create_pool(
                dsn=self._dsn,
                min_size=1,
                max_size=5,
            )
            try:
                async with pool.acquire() as conn:
                    async with conn.transaction():
                        await conn.execute(
                            """
                            CREATE TABLE IF NOT EXISTS memories (
                                memory_id TEXT PRIMARY KEY,
                                user_id TEXT NOT NULL,
                                content TEXT NOT NULL,
                                category TEXT NOT NULL,
                                source TEXT NOT NULL,
                                source_id TEXT,
                                version INTEGER NOT NULL DEFAULT 1,
                                created_at TIMESTAMPTZ NOT NULL,
                                updated_at TIMESTAMPTZ NOT NULL,
                                expires_at TIMESTAMPTZ
                            )
                            """
                        )
                        await conn.execute(
                            """
                            CREATE INDEX IF NOT EXISTS idx_memories_user
                            ON memories(user_id)
                            """
                        )
            except BaseException:
                # Termination is synchronous, so cancellation also releases the
                # failed pool without a second await during error cleanup.
                pool.terminate()
                raise

            self._pool = pool

    async def close(self) -> None:
        """Close once; initialization and closure cannot race."""
        async with self._lifecycle_lock:
            if self._pool is not None:
                pool = self._pool
                self._pool = None
                await pool.close()

    async def add(self, record: MemoryRecord) -> MemoryRecord:
        """新增一条长期记忆，不修改传入对象或覆盖已存在的 ID。"""
        if not isinstance(record, MemoryRecord):
            raise TypeError("record must be a MemoryRecord")
        for name in ("memory_id", "user_id", "content", "source"):
            value = getattr(record, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(record.category, MemoryCategory):
            raise TypeError("category must be a MemoryCategory")
        if record.source_id is not None and not isinstance(record.source_id, str):
            raise TypeError("source_id must be a string or None")
        if type(record.version) is not int or record.version != 1:
            raise ValueError("New memory version must be 1")
        for name in ("created_at", "updated_at", "expires_at"):
            value = getattr(record, name)
            if name == "expires_at" and value is None:
                continue
            if not isinstance(value, datetime):
                raise TypeError(f"{name} must be a datetime")
            if value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")

        # Use the same pool reference throughout this operation.
        pool = self._pool
        if pool is None:
            raise RuntimeError("Database is not initialized")

        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO memories (
                        memory_id, user_id, content,
                        category, source, source_id,
                        version, created_at, updated_at, expires_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    """,
                    record.memory_id,
                    record.user_id,
                    record.content,
                    record.category.value,
                    record.source,
                    record.source_id,
                    record.version,
                    record.created_at,
                    record.updated_at,
                    record.expires_at,
                )
        except asyncpg.UniqueViolationError as exc:
            raise ValueError("Memory ID already exists") from exc

        return record

    async def get(self, user_id: str, memory_id: str) -> MemoryRecord | None:
        """按用户和记忆 ID 查询，避免跨用户读取。"""
        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("user_id must be a non-empty string")

        if not isinstance(memory_id, str) or not memory_id.strip():
            raise ValueError("memory_id must be a non-empty string")
        pool = self._pool
        if pool is None:
            raise RuntimeError("Database is not initialized")

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT memory_id, user_id, content, category, source, source_id,
                       version, created_at, updated_at, expires_at
                FROM memories
                WHERE user_id = $1 AND memory_id = $2
                """,
                user_id,
                memory_id,
            )

        if row is None:
            return None
        return self._row_to_record(row)

    @staticmethod
    def _row_to_record(row: asyncpg.Record) -> MemoryRecord:
        """将 PostgreSQL 查询结果转换为 MemoryRecord。"""

        return MemoryRecord(
            memory_id=row["memory_id"],
            user_id=row["user_id"],
            content=row["content"],
            category=MemoryCategory(row["category"]),
            source=row["source"],
            source_id=row["source_id"],
            version=row["version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            expires_at=row["expires_at"],
        )

    async def list_by_user(
        self,
        user_id: str,
        category: MemoryCategory | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[MemoryRecord]:
        
        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("user_id must not be empty")

        if category is not None and not isinstance(category, MemoryCategory):
            raise TypeError("category must be MemoryCategory or None")

        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")

        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a non-negative integer")

        # 2. 检查连接池
        if self._pool is None:
            raise RuntimeError("Database is not initialized")

        async with self._pool.acquire() as conn:

            if category is None:
                rows = await conn.fetch(
                    """
                    SELECT *
                    FROM memories
                    WHERE user_id = $1
                    ORDER BY created_at DESC, memory_id DESC
                    LIMIT $2 OFFSET $3
                    """,
                    user_id,
                    limit,
                    offset,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT *
                    FROM memories
                    WHERE user_id = $1
                    AND category = $2
                    ORDER BY created_at DESC, memory_id DESC
                    LIMIT $3 OFFSET $4
                    """,
                    user_id,
                    category.value,
                    limit,
                    offset,
                )

            return [
                self._row_to_record(row)
                for row in rows
            ]

    async def update(
            self,
            record: MemoryRecord,
    ) -> MemoryRecord:
        if not isinstance(record, MemoryRecord):
            raise TypeError("record must be MemoryRecord")

        if not all((
            record.memory_id.strip(),
            record.user_id.strip(),
            record.content.strip(),
            record.source.strip(),
        )):
            raise ValueError("Required fields must not be empty")

        if not isinstance(record.category, MemoryCategory):
            raise TypeError("Invalid memory category")

        if type(record.version) is not int or record.version < 1:
            raise ValueError("Invalid memory version")

        if self._pool is None:
            raise RuntimeError("Database is not initialized")
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                 """
                UPDATE memories
                SET
                    content = $1,
                    category = $2,
                    source = $3,
                    source_id = $4,
                    expires_at = $5,
                    version = version + 1,
                    updated_at = NOW()
                WHERE memory_id = $6
                AND user_id = $7
                AND version = $8
                RETURNING *
                """,
                record.content,
                record.category.value,
                record.source,
                record.source_id,
                record.expires_at,
                record.memory_id,
                record.user_id,
                record.version,
            )

        if row is None:
            raise ValueError(
                 "Memory not found or version conflict"
            )

        return self._row_to_record(row)


    async def delete(
        self,
        user_id: str,
        memory_id: str,
    ) -> bool:
        """删除指定用户的一条长期记忆。"""

        # 1. 参数校验
        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("user_id must not be empty")

        if not isinstance(memory_id, str) or not memory_id.strip():
            raise ValueError("memory_id must not be empty")

        # 2. 检查连接池
        if self._pool is None:
            raise RuntimeError("Database is not initialized")

        # 3. 执行删除
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                DELETE FROM memories
                WHERE user_id = $1
                AND memory_id = $2
                RETURNING memory_id
                """,
                user_id,
                memory_id,
            )

        # 4. 返回删除结果
        return row is not None


    
    async def search(
        self,
        user_id: str,
        query: str,
        limit: int = 5,
    ) -> list[MemoryRecord]:
        """根据关键词检索指定用户的有效长期记忆。"""

        # 1. 参数校验
        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("user_id must not be empty")

        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must not be empty")

        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")

        # 2. 检查连接池
        if self._pool is None:
            raise RuntimeError("Database is not initialized")

        # 3. 查询数据库
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT *
                FROM memories
                WHERE user_id = $1
                AND STRPOS(LOWER(content), LOWER($2)) > 0
                AND (expires_at IS NULL OR expires_at > NOW())
                ORDER BY updated_at DESC, memory_id DESC
                LIMIT $3
                """,
                user_id,
                query.strip(),
                limit,
            )

        # 4. 转换为 MemoryRecord
        return [
            self._row_to_record(row)
            for row in rows
        ]
