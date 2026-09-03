"""Dense, BM25, and Hybrid retrieval orchestration for the knowledge tool."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Iterable, Literal

from langchain_core.documents import Document

from app.config import config
from app.schemas.rag import RagSearchResult, RagTrace, RetrievedDocument
from app.services.keyword_retriever import KeywordRetriever, KeywordSearchResult
from app.services.rag_context_builder import ContextBuilder
from app.services.rag_reranker_service import RerankerService
from app.services.reciprocal_rank_fusion import reciprocal_rank_fuse


RetrievalMode = Literal["dense", "bm25", "hybrid"]


class RagSearchService:
    """Run bounded retrieval while keeping each ranking signal independent."""

    def __init__(
        self,
        store_manager: Any,
        reranker_service: RerankerService | None = None,
        context_builder: ContextBuilder | None = None,
        keyword_retriever: KeywordRetriever | None = None,
        retrieval_mode: RetrievalMode = config.rag_retrieval_mode,
        rrf_k: int = config.rag_rrf_k,
        hybrid_candidate_k: int = config.rag_hybrid_candidate_k,
    ) -> None:
        if retrieval_mode not in {"dense", "bm25", "hybrid"}:
            raise ValueError(f"unsupported retrieval mode: {retrieval_mode}")
        self.store_manager = store_manager
        self.reranker_service = reranker_service
        self.context_builder = context_builder or ContextBuilder(config.rag_context_token_budget)
        self.keyword_retriever = keyword_retriever
        self.retrieval_mode: RetrievalMode = retrieval_mode
        self.rrf_k = max(1, int(rrf_k))
        self.hybrid_candidate_k = max(1, int(hybrid_candidate_k))

    @staticmethod
    def build_queries(
        original_query: str, rewritten_queries: Iterable[str] | None = None
    ) -> list[str]:
        original = original_query.strip()
        if not original:
            return []
        queries = [original]
        for query in rewritten_queries or ():
            normalized = str(query).strip()
            if normalized and normalized not in queries:
                queries.append(normalized)
            if len(queries) >= 3:
                break
        return queries

    @staticmethod
    def _key(document: Document) -> tuple[str, str]:
        metadata = document.metadata or {}
        doc_id = metadata.get("doc_id")
        if doc_id is not None:
            return "doc_id", str(doc_id)
        chunk_id = metadata.get("chunk_id")
        if chunk_id is not None:
            return "chunk_id", str(chunk_id)
        source = metadata.get("_source") or metadata.get("source") or metadata.get("_file_name") or ""
        chunk_index = metadata.get("chunk_index")
        if chunk_index is not None:
            return "source_chunk_index", f"{source}:{chunk_index}"
        return "source_content", f"{source}:{document.page_content.strip()}"

    @classmethod
    def _trace_item(cls, item: RetrievedDocument, rank: int) -> dict[str, Any]:
        metadata = item.document.metadata or {}
        return {
            "rank": rank,
            "key": ":".join(cls._key(item.document)),
            "source": str(
                metadata.get("doc_id")
                or metadata.get("_file_name")
                or metadata.get("_source")
                or ""
            ),
            "distance": item.distance,
            "bm25_score": item.bm25_score,
            "dense_rank": item.dense_rank,
            "bm25_rank": item.bm25_rank,
            "rrf_score": item.rrf_score,
            "rerank_score": item.rerank_score,
            "matched_retrievers": list(item.matched_retrievers),
        }

    def _dense_search(
        self,
        original_query: str,
        k: int | None = None,
        rewritten_queries: Iterable[str] | None = None,
        retrieval_k: int = 8,
        *,
        max_per_query_k: int = 8,
        candidate_limit: int = 20,
    ) -> RagSearchResult:
        queries = self.build_queries(original_query, rewritten_queries)
        trace = RagTrace(
            original_query=original_query.strip(),
            queries=queries,
            query_count=len(queries),
            retrieval_mode="dense",
        )
        if not queries:
            return RagSearchResult(queries=[], trace=trace)

        candidates: list[tuple[Document, float, str]] = []
        per_query_k = min(max(1, int(max_per_query_k)), max(6, int(retrieval_k)))
        for query in queries:
            matches = self.store_manager.search_with_scores(query, k=per_query_k)
            candidates.extend((doc, float(distance), query) for doc, distance in matches)
        candidates = sorted(candidates, key=lambda item: item[1])[: max(1, candidate_limit)]

        deduped: dict[tuple[str, str], RetrievedDocument] = {}
        hits: dict[tuple[str, str], set[str]] = {}
        for document, distance, query in candidates:
            key = self._key(document)
            hits.setdefault(key, set()).add(query)
            candidate = RetrievedDocument(document=document, distance=distance, query=query)
            current = deduped.get(key)
            if current is None or (
                current.distance is not None and candidate.distance is not None
                and candidate.distance < current.distance
            ):
                deduped[key] = candidate

        ranked = sorted(
            (
                replace(
                    item,
                    query_hits=tuple(sorted(hits[key])),
                    matched_retrievers=("dense",),
                )
                for key, item in deduped.items()
            ),
            key=lambda item: item.distance if item.distance is not None else float("inf"),
        )
        ranked = [replace(item, dense_rank=rank) for rank, item in enumerate(ranked, 1)]
        trace.candidate_count = len(candidates)
        trace.result_count = len(ranked)
        trace.metric = "L2"
        trace.query_hits = {
            f"{kind}:{value}": sorted(values) for (kind, value), values in hits.items()
        }
        trace.dense_ranking = [
            self._trace_item(item, rank) for rank, item in enumerate(ranked, 1)
        ]
        return RagSearchResult(
            documents=[item.document for item in ranked],
            ranked=ranked,
            queries=queries,
            trace=trace,
        )

    def _bm25_search(
        self,
        original_query: str,
        rewritten_queries: Iterable[str] | None = None,
        retrieval_k: int = config.rag_bm25_top_k,
        *,
        candidate_limit: int = 20,
    ) -> RagSearchResult:
        if self.keyword_retriever is None:
            raise RuntimeError("BM25 retrieval mode requires a KeywordRetriever")
        queries = self.build_queries(original_query, rewritten_queries)
        trace = RagTrace(
            original_query=original_query.strip(),
            queries=queries,
            query_count=len(queries),
            metric="BM25",
            retrieval_mode="bm25",
        )
        if not queries:
            return RagSearchResult(queries=[], trace=trace)

        best: dict[tuple[str, str], tuple[KeywordSearchResult, str]] = {}
        hits: dict[tuple[str, str], set[str]] = {}
        for query in queries:
            for result in self.keyword_retriever.search(query, k=max(1, int(retrieval_k))):
                key = self._key(result.document)
                hits.setdefault(key, set()).add(query)
                current = best.get(key)
                if current is None or (result.rank, -result.score) < (
                    current[0].rank,
                    -current[0].score,
                ):
                    best[key] = (result, query)

        ordered = sorted(
            best.items(),
            key=lambda item: (item[1][0].rank, -item[1][0].score, str(item[0])),
        )[: max(1, int(candidate_limit))]
        ranked = [
            RetrievedDocument(
                document=result.document,
                distance=None,
                query=query,
                query_hits=tuple(sorted(hits[key])),
                bm25_score=result.score,
                bm25_rank=rank,
                matched_retrievers=("bm25",),
            )
            for rank, (key, (result, query)) in enumerate(ordered, 1)
        ]
        trace.candidate_count = len(best)
        trace.result_count = len(ranked)
        trace.bm25_ranking = [
            self._trace_item(item, rank) for rank, item in enumerate(ranked, 1)
        ]
        return RagSearchResult(
            documents=[item.document for item in ranked],
            ranked=ranked,
            queries=queries,
            trace=trace,
        )

    def _retrieve_candidates(
        self,
        original_query: str,
        rewritten_queries: Iterable[str] | None,
        retrieval_k: int,
        mode: RetrievalMode,
    ) -> RagSearchResult:
        if mode == "dense":
            return self._dense_search(original_query, None, rewritten_queries, retrieval_k)
        if mode == "bm25":
            return self._bm25_search(
                original_query,
                rewritten_queries,
                retrieval_k=max(retrieval_k, config.rag_bm25_top_k),
                candidate_limit=self.hybrid_candidate_k,
            )

        dense = self._dense_search(
            original_query,
            None,
            rewritten_queries,
            retrieval_k=max(retrieval_k, self.hybrid_candidate_k),
            max_per_query_k=self.hybrid_candidate_k,
            candidate_limit=self.hybrid_candidate_k,
        )
        keyword = self._bm25_search(
            original_query,
            rewritten_queries,
            retrieval_k=max(retrieval_k, config.rag_bm25_top_k),
            candidate_limit=self.hybrid_candidate_k,
        )
        keyword_results = [
            KeywordSearchResult(
                document=item.document,
                score=float(item.bm25_score),
                rank=int(item.bm25_rank),
            )
            for item in keyword.ranked
            if item.bm25_score is not None and item.bm25_rank is not None
        ]
        fused = reciprocal_rank_fuse(
            dense.ranked,
            keyword_results,
            key=self._key,
            rrf_k=self.rrf_k,
            limit=self.hybrid_candidate_k,
        )
        trace = RagTrace(
            original_query=original_query.strip(),
            queries=dense.queries,
            query_count=len(dense.queries),
            candidate_count=len(fused),
            result_count=len(fused),
            metric="RRF",
            retrieval_mode="hybrid",
            query_hits=dense.trace.query_hits,
            dense_ranking=dense.trace.dense_ranking,
            bm25_ranking=keyword.trace.bm25_ranking,
            rrf_ranking=[
                self._trace_item(item, rank) for rank, item in enumerate(fused, 1)
            ],
        )
        return RagSearchResult(
            documents=[item.document for item in fused],
            ranked=fused,
            queries=dense.queries,
            trace=trace,
        )

    def dense_search_compat(
        self,
        original_query: str,
        k: int | None = None,
        rewritten_queries: Iterable[str] | None = None,
        retrieval_k: int = 8,
    ) -> RagSearchResult:
        """Synchronous dense-only compatibility path for legacy callers/tests."""
        dense = self._dense_search(original_query, k, rewritten_queries, retrieval_k)
        final = dense.ranked[: max(1, int(k or 3))]
        dense.ranked = final
        dense.documents = [item.document for item in final]
        dense.trace.result_count = len(final)
        dense.trace.final_ranking = [
            self._trace_item(item, rank) for rank, item in enumerate(final, 1)
        ]
        return dense

    def search(self, *args: Any, **kwargs: Any) -> RagSearchResult:
        """Deprecated dense-only compatibility alias; complete RAG uses asearch."""
        return self.dense_search_compat(*args, **kwargs)

    async def asearch(
        self,
        original_query: str,
        k: int | None = None,
        rewritten_queries: Iterable[str] | None = None,
        retrieval_k: int = 8,
        retrieval_mode: RetrievalMode | None = None,
    ) -> RagSearchResult:
        """Retrieve candidates, optionally rerank, then apply final top-k."""
        mode = retrieval_mode or self.retrieval_mode
        result = self._retrieve_candidates(original_query, rewritten_queries, retrieval_k, mode)
        if self.reranker_service is None:
            reranked_candidates = result.ranked
        else:
            reranked = await self.reranker_service.rerank(original_query, result.ranked)
            reranked_candidates = reranked.candidates
            result.trace.rerank_used = reranked.trace.rerank_used
            result.trace.rerank_candidate_count = reranked.trace.rerank_candidate_count
            result.trace.rerank_warning = reranked.trace.rerank_warning
            result.trace.rerank_time_ms = reranked.trace.rerank_time_ms
            if reranked.trace.rerank_warning:
                result.trace.warnings.append(reranked.trace.rerank_warning)
        final = reranked_candidates[: max(1, int(k or 3))]
        result.ranked = final
        result.documents = [item.document for item in final]
        result.trace.result_count = len(final)
        result.trace.final_ranking = [
            self._trace_item(item, rank) for rank, item in enumerate(final, 1)
        ]
        result.built_context = self.context_builder.build(final, final_top_k=k)
        result.documents = list(result.built_context.selected_documents)
        result.trace.selected_evidence_count = len(result.built_context.selected_documents)
        result.trace.context_token_count = result.built_context.token_count
        result.trace.skipped_context_count = result.built_context.skipped_documents
        return result

    @staticmethod
    def build_context(result: RagSearchResult) -> str:
        parts: list[str] = []
        for index, item in enumerate(result.ranked, 1):
            metadata = item.document.metadata or {}
            source = metadata.get("_file_name") or metadata.get("_source") or "未知来源"
            headings = [str(metadata[key]) for key in ("h1", "h2", "h3") if metadata.get(key)]
            lines = [f"[参考资料{index}]", f"来源: {source}"]
            if headings:
                lines.insert(1, f"标题: {' > '.join(headings)}")
            if item.distance is not None:
                lines.append(f"L2距离: {item.distance:g}")
            if item.bm25_score is not None:
                lines.append(f"BM25分数: {item.bm25_score:g}")
            if item.rrf_score is not None:
                lines.append(f"RRF分数: {item.rrf_score:g}")
            if item.rerank_score is not None:
                lines.append(f"精排分数: {item.rerank_score:g}")
            lines.append(f"内容:\n{item.document.page_content}")
            parts.append("\n".join(lines))
        return "\n\n".join(parts)
