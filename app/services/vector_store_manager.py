"""Lazy, shared LangChain Milvus vector store management."""

from threading import RLock
from typing import Any, Callable
from uuid import uuid4

from langchain_core.documents import Document
from langchain_milvus import Milvus
from loguru import logger

from app.config import config
from app.core.milvus_client import MilvusClientManager, milvus_manager
from app.services.vector_embedding_service import vector_embedding_service


class VectorStoreManager:
    def __init__(
        self,
        client_manager: MilvusClientManager = milvus_manager,
        embedding_service: Any = vector_embedding_service,
        vector_store_factory: Callable[..., Any] = Milvus,
    ) -> None:
        self.collection_name = getattr(
            client_manager, "collection_name", client_manager.COLLECTION_NAME
        )
        self._client_manager = client_manager
        self._embedding_service = embedding_service
        self._vector_store_factory = vector_store_factory
        self._vector_store: Any | None = None
        self._lock = RLock()

    def _initialize_vector_store(self) -> Any:
        with self._lock:
            if self._vector_store is not None:
                return self._vector_store
            self._client_manager.connect()
            self._vector_store = self._vector_store_factory(
                embedding_function=self._embedding_service,
                collection_name=self.collection_name,
                connection_args={
                    "host": config.milvus_host,
                    "port": str(config.milvus_port),
                },
                auto_id=False,
                drop_old=False,
                text_field="content",
                vector_field="vector",
                primary_field="id",
                metadata_field="metadata",
            )
            return self._vector_store

    def get_vector_store(self) -> Any:
        return self._initialize_vector_store()

    def add_document(self, documents: list[Document]) -> list[str]:
        if not documents:
            return []
        ids = [str(uuid4()) for _ in documents]
        vector_store = self.get_vector_store()
        result = vector_store.add_documents(documents, ids=ids)
        logger.info("Added {} chunks to vector store", len(documents))
        return list(result)

    @staticmethod
    def _escape_filter_string(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    def delete_by_source(self, file_path: str) -> int:
        self._client_manager.connect()
        collection = self._client_manager.get_collection()
        escaped_path = self._escape_filter_string(file_path)
        result = collection.delete(f'metadata["_source"] == "{escaped_path}"')
        return int(getattr(result, "delete_count", 0))

    def delete_by_id(self, vector_id: str) -> int:
        self._client_manager.connect()

        collection = self._client_manager.get_collection()

        escaped_id = self._escape_filter_string(vector_id)

        result = collection.delete(
            f'id == "{escaped_id}"'
        )

        return int(
            getattr(result, "delete_count", 0)
        )

    def similarity_search(self, query: str, k: int | None = None) -> list[Document]:
        if not query or not query.strip():
            return []
        vector_store = self.get_vector_store()
        return list(vector_store.similarity_search(query, k=k or config.rag_top_k))

    def search_with_scores(self, query: str, k: int | None = None) -> list[tuple[Document, float]]:
        """Return documents with Milvus L2 distances (smaller is better).

        The scored API is the canonical retrieval path for enhanced RAG. A
        backend without score support is an integration error, not an empty
        result, so callers cannot silently lose ranking information.
        """
        if not query or not query.strip():
            return []
        vector_store = self.get_vector_store()
        method = getattr(vector_store, "similarity_search_with_score", None)
        if not callable(method):
            raise RuntimeError("vector store does not support scored L2 retrieval")
        matches = method(query, k=k or config.rag_top_k)
        return [(document, float(distance)) for document, distance in matches]

    def list_documents(self, batch_size: int = 1000) -> list[Document]:
        """Load the current collection snapshot for an in-memory lexical index."""
        self._client_manager.connect()
        collection = self._client_manager.get_collection()
        collection.load()
        total = int(collection.num_entities)
        documents: list[Document] = []
        offset = 0
        while offset < total:
            rows = collection.query(
                expr='id != ""',
                output_fields=["id", "content", "metadata"],
                limit=min(max(1, int(batch_size)), total - offset),
                offset=offset,
            )
            if not rows:
                break
            for row in rows:
                metadata = dict(row.get("metadata") or {})
                metadata.setdefault("chunk_id", str(row.get("id", "")))
                documents.append(
                    Document(page_content=str(row.get("content", "")), metadata=metadata)
                )
            offset += len(rows)
        if len(documents) != total:
            raise RuntimeError(
                f"loaded {len(documents)} lexical documents from {total} collection entities"
            )
        return documents

    def close(self) -> None:
        with self._lock:
            self._vector_store = None
            self._client_manager.close()


vector_store_manager = VectorStoreManager()
