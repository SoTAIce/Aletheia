"""Compose long-term memory operations; PostgreSQL is the source of truth."""

from dataclasses import replace

from app.services.extrator import MemoryExtractor
from app.services.memory_retriever import MemoryRetriever
from app.services.memory_resolver import MemoryResolver
from app.services.memory_store import MemoryStore
from app.services.memory_vector_index import MemoryVectorIndex
from app.models.longterm_memory import MemoryAction, MemoryDecision, MemoryRecord, MemoryHit


class MemoryIndexSyncError(RuntimeError):
    """The database operation completed, but the index operation failed.

    Retry/rebuild the index from current authoritative data, not remember().
    database_changed=False means forget() found no database row; cleanup still failed.
    The original index exception is preserved as __cause__.
    """

    def __init__(self, user_id: str, memory_id: str, operation: str,
                 *, database_changed: bool) -> None:
        self.user_id = user_id
        self.memory_id = memory_id
        self.operation = operation
        self.database_changed = database_changed
        super().__init__(f"Database operation completed (changed={database_changed}); "
                         f"memory index {operation} failed for {memory_id}")


class LongTermMemoryService:
    """Sequential, fail-fast orchestration with borrowed, initialized dependencies.

    A batch is not atomic: earlier candidates may be saved before a later failure.
    No automatic retry, cross-database rollback or cross-process serialization.
    Cancellation propagates and may leave a committed database write without an
    index update; do not blindly replay remember() after an uncertain outcome.
    """

    def __init__(self, extrator: MemoryExtractor, retriever: MemoryRetriever,
                 resolver: MemoryResolver, store: MemoryStore,
                 vector_index: MemoryVectorIndex) -> None:
        # Keep the existing keyword spelling for compatibility.
        self._extrator = extrator
        self._retriever = retriever
        self._resolver = resolver
        self._store = store
        self._vector_index = vector_index

    @staticmethod
    def _validate_text(name: str, value: str, max_bytes: int | None = None) -> None:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
        if max_bytes is not None and len(value.encode("utf-8")) > max_bytes:
            raise ValueError(f"{name} exceeds maximum UTF-8 byte length ({max_bytes})")

    @staticmethod
    def _validate_record(record: MemoryRecord, user_id: str, memory_id: str) -> None:
        if not isinstance(record, MemoryRecord):
            raise TypeError("Store must return a MemoryRecord")
        if record.user_id != user_id or record.memory_id != memory_id:
            raise ValueError("Memory record user or ID mismatch")

    async def _index_record(self, record: MemoryRecord) -> None:
        try:
            await self._vector_index.upsert(
                user_id=record.user_id, memory_id=record.memory_id, content=record.content,
            )
        except Exception as exc:
            raise MemoryIndexSyncError(record.user_id, record.memory_id, "upsert",
                                       database_changed=True) from exc

    async def remember(self, user_id: str, text: str, source: str,
                       source_id: str | None = None) -> list[MemoryDecision]:
        """Return every decision in candidate order; conflicts never overwrite data."""
        self._validate_text("user_id", user_id, 256)
        self._validate_text("text", text)
        self._validate_text("source", source)
        if source_id is not None and not isinstance(source_id, str):
            raise TypeError("source_id must be a string or None")

        candidates = await self._extrator.extract(text=text, source=source, source_id=source_id)
        decisions: list[MemoryDecision] = []
        for candidate in candidates:
            related = await self._retriever.retrieve(user_id=user_id, query=candidate.content)
            history: dict[str, MemoryRecord] = {}
            for hit in related:
                self._validate_record(hit.record, user_id, hit.record.memory_id)
                if hit.record.memory_id in history:
                    raise ValueError("Duplicate history memory ID")
                history[hit.record.memory_id] = hit.record
            decision = await self._resolver.resolve(candidate, related)
            if not isinstance(decision, MemoryDecision) or not isinstance(decision.action, MemoryAction):
                raise ValueError("Resolver returned an invalid memory decision")
            target_id = decision.target_memory_id
            if decision.action is MemoryAction.ADD:
                if target_id is not None:
                    raise ValueError("ADD must not specify target_memory_id")
                proposed = MemoryRecord(user_id=user_id, content=candidate.content,
                                        category=candidate.category, source=source, source_id=source_id)
                saved = await self._store.add(proposed)
                self._validate_record(saved, user_id, proposed.memory_id)
                await self._index_record(saved)
            else:
                if not isinstance(target_id, str) or target_id not in history:
                    raise ValueError("Decision target_memory_id must belong to retrieved history")
                if decision.action is MemoryAction.UPDATE:
                    current = await self._store.get(user_id, target_id)
                    if current is None:
                        raise ValueError("Target memory not found")
                    self._validate_record(current, user_id, target_id)
                    # Do not apply a model decision to data changed while it was reasoning.
                    if current.version != history[target_id].version:
                        raise ValueError("Target memory version changed since retrieval")
                    proposed = replace(current, content=candidate.content, category=candidate.category,
                                       source=source, source_id=source_id)
                    updated = await self._store.update(proposed)
                    self._validate_record(updated, user_id, target_id)
                    await self._index_record(updated)
                # SKIP and CONFLICT are observable decisions with no database writes.
            decisions.append(decision)
        return decisions

    async def recall(self, user_id: str, query: str, limit: int = 5) -> list[MemoryHit]:
        self._validate_text("user_id", user_id, 256)
        self._validate_text("query", query, 8000)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        return await self._retriever.retrieve(user_id=user_id, query=query, limit=limit)

    async def forget(self, user_id: str, memory_id: str) -> bool:
        """Delete authoritative data first, then clean even an orphaned index entry."""
        self._validate_text("user_id", user_id, 256)
        self._validate_text("memory_id", memory_id, 100)
        deleted = await self._store.delete(user_id=user_id, memory_id=memory_id)
        try:
            await self._vector_index.delete(user_id=user_id, memory_id=memory_id)
        except Exception as exc:
            raise MemoryIndexSyncError(user_id, memory_id, "delete",
                                       database_changed=deleted) from exc
        return deleted
