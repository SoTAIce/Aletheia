"""Stable data contracts for the Phase 1 dense RAG pipeline."""

from dataclasses import dataclass, field
from pydantic import BaseModel, Field

from langchain_core.documents import Document


class QueryRewriteResult(BaseModel):
    rewritten_queries: list[str] = Field(default_factory=list)
    reason: str = ""
    warning: str = ""


@dataclass(frozen=True)
class RetrievedDocument:
    document: Document
    distance: float | None  # Milvus L2 distance; smaller is better; None if not retrieved by Dense.
    query: str = ""
    query_hits: tuple[str, ...] = ()
    bm25_score: float | None = None  # BM25 relevance; larger is better.
    dense_rank: int | None = None
    bm25_rank: int | None = None
    rrf_score: float | None = None  # Rank-only fusion score; larger is better.
    matched_retrievers: tuple[str, ...] = ()
    rerank_score: float | None = None  # Reranker relevance; larger is better.


@dataclass
class RerankTrace:
    rerank_used: bool = False
    rerank_candidate_count: int = 0
    rerank_warning: str = ""
    rerank_time_ms: float = 0.0


@dataclass
class RerankResult:
    candidates: list[RetrievedDocument] = field(default_factory=list)
    trace: RerankTrace = field(default_factory=RerankTrace)


@dataclass(frozen=True)
class Citation:
    ref_id: int
    source: str
    file_name: str
    heading_path: str
    start_line: int | None
    end_line: int | None
    distance: float | None
    bm25_score: float | None = None
    rrf_score: float | None = None
    rerank_score: float | None = None


@dataclass
class BuiltContext:
    context: str = ""
    citations: list[Citation] = field(default_factory=list)
    token_count: int = 0
    selected_documents: list[Document] = field(default_factory=list)
    skipped_documents: int = 0


@dataclass
class RagTrace:
    original_query: str = ""
    queries: list[str] = field(default_factory=list)
    query_count: int = 0
    candidate_count: int = 0
    result_count: int = 0
    metric: str = "L2"
    retrieval_mode: str = "dense"
    timings_ms: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    query_hits: dict[str, list[str]] = field(default_factory=dict)
    rewritten_queries: list[str] = field(default_factory=list)
    rewrite_used: bool = False
    rewrite_warning: str = ""
    rerank_used: bool = False
    rerank_candidate_count: int = 0
    rerank_warning: str = ""
    rerank_time_ms: float = 0.0
    selected_evidence_count: int = 0
    context_token_count: int = 0
    skipped_context_count: int = 0
    dense_ranking: list[dict] = field(default_factory=list)
    bm25_ranking: list[dict] = field(default_factory=list)
    rrf_ranking: list[dict] = field(default_factory=list)
    final_ranking: list[dict] = field(default_factory=list)


@dataclass
class RagSearchResult:
    # Compatibility projection for callers that only need documents.
    documents: list[Document] = field(default_factory=list)
    ranked: list[RetrievedDocument] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    trace: RagTrace = field(default_factory=RagTrace)
    built_context: BuiltContext | None = None
