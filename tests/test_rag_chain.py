"""Offline tests for the RAG retrieval chain (plan section 3.5 / design review 5.1)."""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.documents import Document

from app.schemas.rag import QueryRewriteResult, RetrievedDocument
from app.services.keyword_retriever import InMemoryBM25Retriever, KeywordSearchResult
from app.services.rag_context_builder import ContextBuilder
from app.services.rag_query_rewrite_service import QueryRewriteService
from app.services.rag_reranker_service import RerankerService
from app.services.rag_search_service import RagSearchService
from app.services.reciprocal_rank_fusion import reciprocal_rank_fuse


def _doc(doc_id: str, text: str, **extra_meta) -> Document:
    meta = {"doc_id": doc_id, "_file_name": f"{doc_id}.md", "_source": f"docs/{doc_id}.md"}
    meta.update(extra_meta)
    return Document(page_content=text, metadata=meta)


class FakeStoreManager:
    """In-memory stand-in for VectorStoreManager (search_with_scores only)."""

    def __init__(self, responses: dict[str, list[tuple[Document, float]]]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, int]] = []

    def search_with_scores(self, query: str, k: int = 8) -> list[tuple[Document, float]]:
        self.calls.append((query, k))
        return list(self._responses.get(query, []))[:k]


class FakeRerankerProvider:
    def __init__(self, scores_by_doc: dict[str, float] | None = None, *, error: Exception | None = None) -> None:
        self._scores = scores_by_doc or {}
        self._error = error
        self.calls = 0

    async def score(self, question: str, documents, model: str):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return [self._scores.get(doc, 0.0) for doc in documents]


# ---- Dense, BM25, Hybrid paths ----

@pytest.mark.asyncio
async def test_dense_retrieval_deduplicates_keeps_best_distance_and_ranks():
    d1 = _doc("d1", "alpha beta")
    d2 = _doc("d2", "gamma delta")
    store = FakeStoreManager({
        "q": [(d1, 0.4), (d2, 0.2)],
        "q2": [(d1, 0.1)],  # better distance for d1
    })
    svc = RagSearchService(store, retrieval_mode="dense")
    res = await svc.asearch("q", k=2, rewritten_queries=["q2"])

    assert res.trace.retrieval_mode == "dense"
    assert [d.metadata["doc_id"] for d in res.documents] == ["d1", "d2"]
    assert res.ranked[0].distance == 0.1
    assert res.ranked[0].dense_rank == 1
    assert res.ranked[0].matched_retrievers == ("dense",)
    assert res.built_context is not None
    assert "[ref:1]" in res.built_context.context


@pytest.mark.asyncio
async def test_bm25_retrieval_matches_identifiers_and_sets_bm25_fields():
    d1 = _doc("d1", "ErrorCode_404 in paymentHandler module")
    d2 = _doc("d2", "Unrelated document about gardening")
    bm25 = InMemoryBM25Retriever([d1, d2])
    svc = RagSearchService(FakeStoreManager({}), keyword_retriever=bm25, retrieval_mode="bm25")
    res = await svc.asearch("paymentHandler", k=2)

    assert res.trace.retrieval_mode == "bm25"
    assert res.documents[0].metadata["doc_id"] == "d1"
    assert res.ranked[0].distance is None
    assert res.ranked[0].bm25_score is not None
    assert res.ranked[0].matched_retrievers == ("bm25",)


@pytest.mark.asyncio
async def test_hybrid_retrieval_fuses_dense_and_bm25_with_rrf():
    d1 = _doc("d1", "python asyncio timeout handling")
    d2 = _doc("d2", "milvus vector index configuration")
    d3 = _doc("d3", "asyncio event loop scheduling")
    store = FakeStoreManager({"asyncio": [(d2, 0.1), (d1, 0.2)]})
    bm25 = InMemoryBM25Retriever([d1, d2, d3])
    svc = RagSearchService(store, keyword_retriever=bm25, retrieval_mode="hybrid", rrf_k=60)
    res = await svc.asearch("asyncio", k=3)

    assert res.trace.retrieval_mode == "hybrid"
    assert res.trace.metric == "RRF"
    # d1 appears in both dense and bm25 -> highest RRF score.
    assert res.ranked[0].document.metadata["doc_id"] == "d1"
    assert res.ranked[0].matched_retrievers == ("dense", "bm25")
    assert res.ranked[0].rrf_score is not None


# ---- Reciprocal Rank Fusion ----

def test_rrf_score_deduplication_and_tie_breaking():
    a = _doc("a", "doc a")
    b = _doc("b", "doc b")
    c = _doc("c", "doc c")

    dense = [
        RetrievedDocument(document=a, distance=0.1, dense_rank=1),
        RetrievedDocument(document=b, distance=0.2, dense_rank=2),
    ]
    bm25 = [
        KeywordSearchResult(document=c, score=5.0, rank=1),
        KeywordSearchResult(document=b, score=3.0, rank=2),
    ]
    fused = reciprocal_rank_fuse(dense, bm25, key=RagSearchService._key, rrf_k=60, limit=10)

    # b is rank 2 in both -> 2 / 62; a is rank 1 dense -> 1 / 61; c is rank 1 bm25 -> 1 / 61.
    assert [item.document.metadata["doc_id"] for item in fused] == ["b", "a", "c"]
    assert fused[0].rrf_score == pytest.approx(1 / 62 + 1 / 62)
    assert fused[0].matched_retrievers == ("dense", "bm25")
    # Tie between a (dense_rank=1) and c (bm25_rank=1): dense_rank breaks the tie before bm25_rank.
    assert fused[1].rrf_score == pytest.approx(fused[2].rrf_score)
    assert fused[1].document.metadata["doc_id"] == "a"


# ---- Reranker ----

@pytest.mark.asyncio
async def test_reranker_reorders_by_descending_score():
    d1 = RetrievedDocument(document=_doc("d1", "low"), distance=0.1)
    d2 = RetrievedDocument(document=_doc("d2", "high"), distance=0.5)
    provider = FakeRerankerProvider({"low": 0.2, "high": 0.95})
    svc = RerankerService(provider=provider, enabled=True)
    res = await svc.rerank("q", [d1, d2])

    assert res.trace.rerank_used is True
    assert res.trace.rerank_warning == ""
    assert [c.document.metadata["doc_id"] for c in res.candidates] == ["d2", "d1"]
    assert res.candidates[0].rerank_score == 0.95


@pytest.mark.asyncio
async def test_reranker_disabled_skips_provider_and_uses_fallback():
    d1 = RetrievedDocument(document=_doc("d1", "far"), distance=0.9)
    d2 = RetrievedDocument(document=_doc("d2", "near"), distance=0.1)
    provider = FakeRerankerProvider({"far": 1.0, "near": 0.0})
    svc = RerankerService(provider=provider, enabled=False)
    res = await svc.rerank("q", [d1, d2])

    assert provider.calls == 0
    assert res.trace.rerank_used is False
    assert res.trace.rerank_warning == "rerank_disabled"
    assert [c.document.metadata["doc_id"] for c in res.candidates] == ["d2", "d1"]


@pytest.mark.asyncio
async def test_reranker_error_falls_back_to_rrf_order():
    d1 = RetrievedDocument(document=_doc("d1", "first"), distance=0.5, rrf_score=0.05)
    d2 = RetrievedDocument(document=_doc("d2", "second"), distance=0.1, rrf_score=0.02)
    provider = FakeRerankerProvider(error=RuntimeError("boom"))
    svc = RerankerService(provider=provider, enabled=True)
    res = await svc.rerank("q", [d2, d1])

    assert res.trace.rerank_used is False
    assert res.trace.rerank_warning == "rerank_failed"
    assert [c.document.metadata["doc_id"] for c in res.candidates] == ["d1", "d2"]


# ---- Query Rewrite ----

class FakeStructuredLlm:
    def __init__(self, result=None, *, error: Exception | None = None, delay: float = 0.0) -> None:
        self._result = result
        self._error = error
        self._delay = delay

    def with_structured_output(self, schema):
        return self

    async def ainvoke(self, prompt, config=None):
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        return self._result


@pytest.mark.asyncio
async def test_query_rewrite_deduplicates_excludes_original_and_caps_at_two():
    llm = FakeStructuredLlm(QueryRewriteResult(
        rewritten_queries=["orig", "first rewrite", "first rewrite", "second rewrite", "third rewrite"],
        reason="expanded",
    ))
    svc = QueryRewriteService(llm)
    res = await svc.rewrite("orig")
    assert res.rewritten_queries == ["first rewrite", "second rewrite"]
    assert res.warning == ""


@pytest.mark.asyncio
async def test_query_rewrite_failure_falls_back_to_original_with_type_only_warning():
    llm = FakeStructuredLlm(error=ConnectionError("secret-host:8443"))
    svc = QueryRewriteService(llm)
    res = await svc.rewrite("my question")
    assert res.rewritten_queries == []
    assert "ConnectionError" in res.warning
    assert "secret-host" not in res.warning


# ---- Context Builder ----

def test_context_builder_respects_budget_counts_skipped_and_invents_no_metadata():
    d1 = RetrievedDocument(document=Document(page_content="short", metadata={}), distance=0.1)
    d2 = RetrievedDocument(document=Document(page_content="A" * 500, metadata={"_file_name": "big.md"}), distance=0.2)
    d3 = RetrievedDocument(document=Document(page_content="tiny", metadata={"_file_name": "ok.md", "start_line": 3, "end_line": 8}), distance=0.3)

    builder = ContextBuilder(token_budget=60)
    built = builder.build([d1, d2, d3], final_top_k=3)

    assert len(built.selected_documents) == 2
    assert built.skipped_documents == 1
    assert [c.ref_id for c in built.citations] == [1, 2]
    assert "[ref:1]\n" in built.context and "[ref:2]\n" in built.context
    # No invented metadata for d1.
    assert built.citations[0].file_name == ""
    assert built.citations[0].source == ""
    assert built.citations[0].heading_path == ""
    assert built.citations[0].start_line is None
    assert built.citations[0].end_line is None
    assert built.citations[1].start_line == 3 and built.citations[1].end_line == 8
