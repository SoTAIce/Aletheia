from typing import Protocol

from app.models.longterm_memory import (
    MemoryRecord,
    MemoryCategory,
)

class MemoryStore(Protocol):

    async def add(
        self,
        record: MemoryRecord,
    ) -> MemoryRecord:
        """新增一条记忆。"""
        ...

    async def get(
        self,
        user_id: str,
        memory_id: str,
    ) -> MemoryRecord | None:
        """根据 ID 查询记忆。"""
        ...

    async def list_by_user(
        self,
        user_id: str,
        category: MemoryCategory | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[MemoryRecord]:
        """分页查询用户记忆。"""
        ...

    async def search(
        self,
        user_id: str,
        query: str,
        limit: int = 5,
        persona_scope=...,
    ) -> list[MemoryRecord]:
        """按关键词查询相关记忆；persona_scope 见 longterm_memory.ALL_PERSONAS。"""
        ...

    async def count_by_persona(
        self,
        user_id: str,
        persona_id: str,
    ) -> int:
        """某个人设名下有几条记忆。"""
        ...

    async def delete_by_persona(
        self,
        user_id: str,
        persona_id: str,
    ) -> list[str]:
        """删除某个人设名下的全部记忆，返回删掉的编号。"""
        ...

    async def update(
        self,
        record: MemoryRecord,
    ) -> MemoryRecord:
        """更新记忆并递增版本号。"""
        ...

    async def delete(
        self,
        user_id: str,
        memory_id: str,
    ) -> bool:
        """删除指定记忆。"""
        ...