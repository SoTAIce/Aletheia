"""Knowledge retrieval tool and its testable service function."""

from typing import Any, Literal, Sequence, TypedDict

from langchain_core.documents import Document
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.tools import tool
from langchain.tools import ToolRuntime
from langchain_qwq import ChatQwen
from loguru import logger

from app.config import config
from app.schemas.rag import Citation, QueryRewriteResult, RagTrace
from app.services.keyword_retriever import InMemoryBM25Retriever
from app.services.local_cross_encoder_reranker_provider import (
    LocalCrossEncoderRerankerProvider,
)
from app.services.rag_query_rewrite_service import QueryRewriteService
from app.services.rag_reranker_service import RerankerService
from app.services.rag_search_service import RagSearchService
from app.services.vector_store_manager import vector_store_manager


class KnowledgeArtifact(TypedDict):
    status: Literal["ok", "empty", "error"]
    documents: list[Document]
    error: str | None
    trace: RagTrace | None
    citations: list[Citation]


_query_rewrite_service: QueryRewriteService | None = None
_keyword_retriever: InMemoryBM25Retriever | None = None
_reranker_service: RerankerService | None = None


def get_query_rewrite_service() -> QueryRewriteService:
    global _query_rewrite_service
    if _query_rewrite_service is None:
        _query_rewrite_service = QueryRewriteService(
            ChatQwen(
                model=config.rag_query_rewrite_model,
                api_key=config.dashscope_api_key,
                base_url=config.dashscope_base_url,
                temperature=0,
            )
        )
    return _query_rewrite_service


def get_keyword_retriever() -> InMemoryBM25Retriever | None:
    """Build one process-local lexical index from the current Milvus snapshot."""
    global _keyword_retriever
    if config.rag_retrieval_mode == "dense":
        return None
    if _keyword_retriever is None:
        _keyword_retriever = InMemoryBM25Retriever(vector_store_manager.list_documents())
    return _keyword_retriever


def get_reranker_service() -> RerankerService:
    """Lazily load the local BGE model only when reranking is enabled."""
    global _reranker_service
    if _reranker_service is None:
        provider = None
        if config.rag_enable_rerank:
            provider = LocalCrossEncoderRerankerProvider(
                config.rag_rerank_model,
                config.rag_rerank_cache_dir,
                offline=True,
            )
        _reranker_service = RerankerService(
            provider=provider,
            enabled=config.rag_enable_rerank,
            candidate_limit=config.rag_rerank_top_k,
            model=config.rag_rerank_model,
            timeout_seconds=config.rag_rerank_timeout_seconds,
        )
    return _reranker_service


def _message_text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "".join(
            block if isinstance(block, str) else str(block.get("text", ""))
            for block in content
            if isinstance(block, (str, dict))
        ).strip()
    return ""


def extract_rewrite_history(
    messages: Sequence[BaseMessage], current_query: str, max_messages: int = 6
) -> list[BaseMessage]:
    """Keep natural-language turns, excluding tool protocol messages and current query."""
    history: list[BaseMessage] = []
    for message in messages:
        if isinstance(message, HumanMessage):
            if _message_text(message):
                history.append(message)
        elif isinstance(message, AIMessage) and not message.tool_calls:
            if _message_text(message):
                history.append(message)
    if history and isinstance(history[-1], HumanMessage):
        if _message_text(history[-1]) == current_query.strip():
            history.pop()
    return history[-max(0, max_messages) :]


def search_knowledge(query: str) -> tuple[str, KnowledgeArtifact]:
    """Retrieve knowledge without LangChain tool wrapping.

    The explicit artifact status lets internal callers distinguish an empty
    knowledge base from an infrastructure failure.
    """
    if not query or not query.strip():
        return "查询不能为空。", {
            "status": "error",
            "documents": [],
            "error": "empty query",
            "trace": None,
            "citations": [],
        }

    try:
        search_result = RagSearchService(vector_store_manager).dense_search_compat(query, k=config.rag_top_k)
        documents = search_result.documents
    except Exception as exc:
        logger.exception("Knowledge retrieval failed")
        return "知识库检索暂时不可用。", {
            "status": "error",
            "documents": [],
            "error": str(exc),
            "trace": None,
            "citations": [],
        }

    if not documents:
        return "没有找到相关信息。", {
            "status": "empty",
            "documents": [],
            "error": None,
            "trace": search_result.trace,
            "citations": search_result.built_context.citations if search_result.built_context else [],
        }

    return RagSearchService.build_context(search_result), {
        "status": "ok",
        "documents": documents,
        "error": None,
        "trace": search_result.trace,
        "citations": search_result.built_context.citations if search_result.built_context else [],
    }


async def asearch_knowledge(
    query: str,
    chat_history: Sequence[BaseMessage] | None = None,
    rewrite_service: QueryRewriteService | None = None,
) -> tuple[str, KnowledgeArtifact]:
    if not query or not query.strip():
        return search_knowledge(query)

    rewrite_result = QueryRewriteResult()
    try:
        service = rewrite_service or get_query_rewrite_service()
        rewrite_result = await service.rewrite(query, chat_history)
    except Exception as exc:
        rewrite_result = QueryRewriteResult(
            warning=f"query rewrite fallback: {type(exc).__name__}"
        )

    try:
        result = await RagSearchService(
            vector_store_manager,
            reranker_service=get_reranker_service(),
            keyword_retriever=get_keyword_retriever(),
            retrieval_mode=config.rag_retrieval_mode,
        ).asearch(
            query,
            k=config.rag_top_k,
            rewritten_queries=rewrite_result.rewritten_queries,
        )
        result.trace.rewritten_queries = list(rewrite_result.rewritten_queries)
        result.trace.rewrite_used = bool(rewrite_result.rewritten_queries)
        result.trace.rewrite_warning = rewrite_result.warning
        if rewrite_result.warning:
            result.trace.warnings.append(rewrite_result.warning)
    except Exception as exc:
        logger.exception("Knowledge retrieval failed")
        return "知识库检索暂时不可用。", {
            "status": "error",
            "documents": [],
            "error": str(exc),
            "trace": None,
            "citations": [],
        }

    built_context = result.built_context
    if built_context is None or not built_context.context:
        return "没有找到相关信息。", {
            "status": "empty",
            "documents": [],
            "error": None,
            "trace": result.trace,
            "citations": [],
        }
    return built_context.context, {
        "status": "ok",
        "documents": result.documents,
        "error": None,
        "trace": result.trace,
        "citations": built_context.citations,
    }


@tool(response_format="content_and_artifact")
async def retrieve_knowledge(
    query: str, runtime: ToolRuntime
) -> tuple[str, dict[str, Any]]:
    """从内部知识库检索与用户问题相关的参考资料。"""
    state = runtime.state if isinstance(runtime.state, dict) else {}
    messages = state.get("messages", [])
    history = extract_rewrite_history(messages, query)
    return await asearch_knowledge(query, history)
