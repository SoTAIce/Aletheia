"""Reciprocal Rank Fusion for Dense and BM25 result lists."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Hashable, Sequence

from langchain_core.documents import Document

from app.schemas.rag import RetrievedDocument
from app.services.keyword_retriever import KeywordSearchResult


@dataclass
class _FusionCandidate:
    document: Document
    distance: float | None = None
    bm25_score: float | None = None
    dense_rank: int | None = None
    bm25_rank: int | None = None
    query: str = ""
    query_hits: tuple[str, ...] = ()


def reciprocal_rank_fuse(
    dense: Sequence[RetrievedDocument],
    bm25: Sequence[KeywordSearchResult],
    *,
    key: Callable[[Document], Hashable],
    rrf_k: int = 60,
    limit: int = 20,
) -> list[RetrievedDocument]:
    """Fuse by rank only, retaining each retriever's raw score separately."""
    constant = max(1, int(rrf_k))
    fused: dict[Hashable, _FusionCandidate] = {}
    scores: dict[Hashable, float] = {}

    for rank, candidate in enumerate(dense, 1):
        candidate_key = key(candidate.document)
        fused[candidate_key] = _FusionCandidate(
            document=candidate.document,
            distance=candidate.distance,
            dense_rank=rank,
            query=candidate.query,
            query_hits=candidate.query_hits,
        )
        scores[candidate_key] = scores.get(candidate_key, 0.0) + 1.0 / (constant + rank)

    for rank, result in enumerate(bm25, 1):
        candidate_key = key(result.document)
        current = fused.get(candidate_key)
        if current is None:
            current = _FusionCandidate(document=result.document)
            fused[candidate_key] = current
        current.bm25_score = result.score
        current.bm25_rank = rank
        scores[candidate_key] = scores.get(candidate_key, 0.0) + 1.0 / (constant + rank)

    ranked_keys = sorted(
        fused,
        key=lambda candidate_key: (
            -scores[candidate_key],
            fused[candidate_key].dense_rank or 10**9,
            fused[candidate_key].bm25_rank or 10**9,
            str(candidate_key),
        ),
    )[: max(1, int(limit))]
    results: list[RetrievedDocument] = []
    for candidate_key in ranked_keys:
        candidate = fused[candidate_key]
        matched = tuple(
            name
            for name, rank in (("dense", candidate.dense_rank), ("bm25", candidate.bm25_rank))
            if rank is not None
        )
        results.append(
            RetrievedDocument(
                document=candidate.document,
                distance=candidate.distance,
                query=candidate.query,
                query_hits=candidate.query_hits,
                bm25_score=candidate.bm25_score,
                dense_rank=candidate.dense_rank,
                bm25_rank=candidate.bm25_rank,
                rrf_score=scores[candidate_key],
                matched_retrievers=matched,
            )
        )
    return results
