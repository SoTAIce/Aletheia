"""Assemble the long-term memory service from configuration and own its connections (D-042)."""

from contextlib import AsyncExitStack, asynccontextmanager

from openai import AsyncOpenAI
from pymilvus import MilvusClient

from app.config import config
from app.services.extrator import MemoryExtractor
from app.services.memory_resolver import MemoryResolver
from app.services.memory_retriever import MemoryRetriever
from app.services.memory_service import LongTermMemoryService
from app.services.memory_vector_index import MemoryVectorIndex
from app.services.postgres_memory_store import PostgresMemoryStore
from app.services.vector_embedding_service import vector_embedding_service


@asynccontextmanager
async def open_longterm_memory(dsn: str | None = None):
    """Yield a ready LongTermMemoryService, or None when no DSN is configured.

    The database pool, the Milvus client and the model client are opened here and
    closed on exit, in reverse order, even when initialization fails part way.
    """
    dsn = config.longterm_memory_dsn if dsn is None else dsn
    if not isinstance(dsn, str) or not dsn.strip():
        yield None
        return
    async with AsyncExitStack() as stack:
        store = PostgresMemoryStore(dsn)
        stack.push_async_callback(store.close)
        await store.initialize()
        milvus = MilvusClient(uri=f'http://{config.milvus_host}:{config.milvus_port}',
                              timeout=config.milvus_timeout / 1000)
        stack.callback(milvus.close)
        index = MemoryVectorIndex(milvus, vector_embedding_service, config.longterm_memory_collection)
        await index.initialize()
        client = await stack.enter_async_context(AsyncOpenAI(
            api_key=config.dashscope_api_key, base_url=config.dashscope_base_url,
            timeout=config.rag_model_timeout_seconds, max_retries=0))
        yield LongTermMemoryService(
            MemoryExtractor(client, config.rag_model), MemoryRetriever(store, index),
            MemoryResolver(client, config.rag_model), store, index)
