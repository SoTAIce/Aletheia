"""Fuse keyword and vector candidates, then read authoritative memory records."""

import asyncio
from datetime import datetime, timezone

from app.models.longterm_memory import ALL_PERSONAS, MemoryHit, check_scope, in_scope
from app.services.memory_store import MemoryStore
from app.services.memory_vector_index import MemoryVectorIndex


class MemoryRetriever:
    """Rank by reciprocal rank fusion, never by mixing L2 and keyword scores.

    Both search sources must return results in relevance order. Each source votes
    at most once per memory ID. Scores are ranking weights, not confidence values.
    Dependencies are borrowed; their initialization and lifetime belong to callers.
    """

    def __init__(
        self,
        store: MemoryStore,
        vector_index: MemoryVectorIndex,
        rrf_k: int = 60,
    ) -> None:
        if type(rrf_k) is not int or rrf_k <= 0:
            raise ValueError("rrf_k must be a positive integer")
        self._store = store
        self._vector_index = vector_index
        self._rrf_k = rrf_k

    async def retrieve(
        self,
        user_id: str,
        query: str,
        limit: int = 5,
        persona_scope=ALL_PERSONAS,
    ) -> list[MemoryHit]:
        """persona_scope (docs/persona_memory_plan.md): the keyword side filters in SQL; the vector
        index knows no persona, so its candidates are filtered on the authoritative record, with a
        larger candidate pool so other personas' memories do not crowd the visible ones out."""
        check_scope(persona_scope)
        for name, value, max_bytes in (("user_id", user_id, 256), ("query", query, 8000)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a nonempty string")
            if len(value.encode("utf-8")) > max_bytes:
                raise ValueError(f"{name} exceeds maximum UTF-8 byte length ({max_bytes})")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")

        scoped = persona_scope is not ALL_PERSONAS
        candidate_k = min(100, max(limit * 6, 20) if scoped else max(limit * 3, 10))
        keyword = (self._store.search(user_id, query, limit=candidate_k, persona_scope=persona_scope)
                   if scoped else self._store.search(user_id, query, limit=candidate_k))
        searches = [
            asyncio.create_task(keyword),
            asyncio.create_task(self._vector_index.search(user_id, query, limit=candidate_k)),
        ]
        try:
            keyword_results, vector_results = await asyncio.gather(*searches)
        except BaseException:
            # gather does not cancel siblings when one raises. Clean up both
            # coroutines; a to_thread call already in progress may still finish.
            for task in searches:
                task.cancel()
            await asyncio.gather(*searches, return_exceptions=True)
            raise

        scores: dict[str, float] = {}
        keyword_ids = [record.memory_id for record in keyword_results if record.user_id == user_id]
        vector_ids = [memory_id for memory_id, _distance in vector_results]
        for source_ids in (keyword_ids, vector_ids):
            seen: set[str] = set()
            for rank, memory_id in enumerate(source_ids, start=1):
                if memory_id in seen:
                    continue
                seen.add(memory_id)
                scores[memory_id] = scores.get(memory_id, 0.0) + 1 / (self._rrf_k + rank)

        ranked_ids = sorted(scores, key=lambda memory_id: (-scores[memory_id], memory_id))
        hits: list[MemoryHit] = []
        for memory_id in ranked_ids:
            # Never return indexed content or a possibly stale keyword snapshot.
            record = await self._store.get(user_id, memory_id)
            if record is None or record.user_id != user_id or record.memory_id != memory_id:
                continue
            if not in_scope(getattr(record, 'persona_id', None), persona_scope):
                continue
            if record.expires_at is not None:
                if not isinstance(record.expires_at, datetime) or record.expires_at.utcoffset() is None:
                    raise ValueError("Memory expires_at must be a timezone-aware datetime")
                if record.expires_at <= datetime.now(timezone.utc):
                    continue
            hits.append(MemoryHit(record=record, score=scores[memory_id]))
            if len(hits) >= limit:
                break
        return hits
