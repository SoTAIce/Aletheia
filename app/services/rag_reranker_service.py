"""Bounded reranking with deterministic L2 fallback."""

import asyncio
import math
from dataclasses import replace
from time import perf_counter
from typing import Protocol, Sequence

from loguru import logger

from app.config import config
from app.schemas.rag import RerankResult, RerankTrace, RetrievedDocument


class RerankerProvider(Protocol):
    """Provider contract: return one relevance score per document, larger is better."""

    async def score(
        self, question: str, documents: Sequence[str], model: str
    ) -> Sequence[float]: ...


class RerankerService:
    """Rerank a bounded dense candidate pool without changing L2 distances."""

    def __init__(
        self,
        provider: RerankerProvider | None,
        *,
        enabled: bool = config.rag_enable_rerank,
        candidate_limit: int = config.rag_rerank_top_k,
        max_candidate_limit: int = 15,
        model: str = config.rag_rerank_model,
        timeout_seconds: float = config.rag_rerank_timeout_seconds,
    ) -> None:
        self.provider = provider
        self.enabled = enabled
        maximum = max(10, int(max_candidate_limit))
        self.candidate_limit = min(maximum, max(10, int(candidate_limit)))
        self.model = model
        self.timeout_seconds = max(0.01, float(timeout_seconds))

    @staticmethod
    def _dense_fallback(
        candidates: Sequence[RetrievedDocument],
        warning: str,
        elapsed_ms: float,
        attempted_count: int = 0,
    ) -> RerankResult:
        if any(item.rrf_score is not None for item in candidates):
            ordered = sorted(
                candidates,
                key=lambda item: -(item.rrf_score if item.rrf_score is not None else -1.0),
            )
        elif any(item.bm25_rank is not None for item in candidates) and all(
            item.distance is None for item in candidates
        ):
            ordered = sorted(candidates, key=lambda item: item.bm25_rank or 10**9)
        else:
            ordered = sorted(
                candidates,
                key=lambda item: item.distance if item.distance is not None else float("inf"),
            )
        return RerankResult(
            candidates=ordered,
            trace=RerankTrace(
                rerank_used=False,
                rerank_candidate_count=attempted_count,
                rerank_warning=warning,
                rerank_time_ms=elapsed_ms,
            ),
        )

    async def rerank(
        self, question: str, candidates: Sequence[RetrievedDocument]
    ) -> RerankResult:
        started = perf_counter()
        original = list(candidates)
        if len(original) <= 1:
            return self._dense_fallback(original, "", 0.0)
        if not self.enabled:
            return self._dense_fallback(original, "rerank_disabled", 0.0)

        pool = original[: self.candidate_limit]
        tail = original[self.candidate_limit :]
        try:
            if self.provider is None:
                raise RuntimeError("reranker provider is unavailable")
            scores = await asyncio.wait_for(
                self.provider.score(
                    question,
                    [candidate.document.page_content for candidate in pool],
                    self.model,
                ),
                timeout=self.timeout_seconds,
            )
            if not isinstance(scores, Sequence) or isinstance(scores, (str, bytes)):
                raise TypeError("reranker scores must be a sequence")
            if len(scores) != len(pool):
                raise ValueError("reranker score count mismatch")

            scored: list[RetrievedDocument] = []
            for candidate, raw_score in zip(pool, scores, strict=True):
                if isinstance(raw_score, bool):
                    raise TypeError("reranker score must be numeric")
                score = float(raw_score)
                if not math.isfinite(score):
                    raise ValueError("reranker score must be finite")
                scored.append(replace(candidate, rerank_score=score))

            elapsed_ms = (perf_counter() - started) * 1000
            return RerankResult(
                candidates=sorted(
                    scored, key=lambda item: item.rerank_score, reverse=True
                )
                + tail,
                trace=RerankTrace(
                    rerank_used=True,
                    rerank_candidate_count=len(pool),
                    rerank_time_ms=elapsed_ms,
                ),
            )
        except Exception as exc:
            logger.warning("Rerank failed; using L2 fallback: {}", exc)
            elapsed_ms = (perf_counter() - started) * 1000
            return self._dense_fallback(
                original, "rerank_failed", elapsed_ms, attempted_count=len(pool)
            )
