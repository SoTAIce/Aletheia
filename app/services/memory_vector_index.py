"""Rebuildable long-term memory index; PostgreSQL remains authoritative."""

import asyncio
from copy import deepcopy
import math
from numbers import Real
import re
import threading
from typing import Any

from langchain_core.embeddings import Embeddings
from pymilvus import DataType, MilvusClient

from app.config import config


class MemoryVectorIndex:
    """Use an injected client without owning its connection or collection lifetime.

    Worker-thread locking protects ownership checks within this process, even if
    an await is cancelled. Milvus has no conditional upsert: multi-process writers
    must serialize writes via the authoritative store. Keep the client alive.
    """

    COLLECTION_NAME = "agent_memories"
    _lock = threading.RLock()
    _TEXT_LIMITS = {"user_id": 256, "memory_id": 100, "content": 8000}

    def __init__(
        self,
        client: MilvusClient,
        embeddings: Embeddings,
        collection_name: str = COLLECTION_NAME,
    ) -> None:
        if not isinstance(collection_name, str) or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]{0,254}", collection_name
        ):
            raise ValueError("collection_name must be a valid Milvus collection name")
        if collection_name == "biz":
            raise ValueError("Memory collection must be separate from the RAG collection")
        self._client = client
        self._embeddings = embeddings
        self._collection_name = collection_name
        self._dim = config.embedding_dimensions
        self._timeout = config.milvus_timeout / 1000
        self._nlist = 128
        self._initialized = False

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        with self._lock:
            if self._initialized:
                return
            name = self._collection_name
            if not self._client.has_collection(collection_name=name, timeout=self._timeout):
                schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
                for field, length in (("id", 100), ("user_id", 256), ("content", 8000)):
                    schema.add_field(
                        field_name=field, datatype=DataType.VARCHAR,
                        is_primary=field == "id", max_length=length,
                    )
                schema.add_field(field_name="vector", datatype=DataType.FLOAT_VECTOR, dim=self._dim)
                self._client.create_collection(
                    collection_name=name, schema=schema, timeout=self._timeout,
                )

            info = self._client.describe_collection(collection_name=name, timeout=self._timeout)
            self._validate_schema(info)
            indexes = self._client.list_indexes(collection_name=name, timeout=self._timeout)
            vector_indexes = [
                index for index in (
                    self._client.describe_index(
                        collection_name=name, index_name=index_name, timeout=self._timeout,
                    ) for index_name in indexes
                ) if index["field_name"] == "vector"
            ]
            for index in vector_indexes:
                if index.get("index_type") != "IVF_FLAT" or index.get("metric_type") != "L2":
                    raise RuntimeError("Memory vector index must use IVF_FLAT and L2")
                self._nlist = int(index.get("nlist", 128))
                if not 1 <= self._nlist <= 65536:
                    raise RuntimeError("Invalid memory vector index nlist")
            if not vector_indexes:
                params = self._client.prepare_index_params()
                params.add_index(
                    field_name="vector", index_type="IVF_FLAT", metric_type="L2",
                    params={"nlist": 128},
                )
                self._client.create_index(
                    collection_name=name, index_params=params, timeout=self._timeout,
                )
            self._client.load_collection(collection_name=name, timeout=self._timeout)
            self._initialized = True

    def _validate_schema(self, info: dict[str, Any]) -> None:
        fields = {field["name"]: field for field in info["fields"]}
        if set(fields) != {"id", "user_id", "content", "vector"} or info.get("auto_id"):
            raise RuntimeError("Memory collection schema mismatch (fields or auto_id)")
        for name, field in fields.items():
            expected = DataType.FLOAT_VECTOR if name == "vector" else DataType.VARCHAR
            if field.get("type") != expected:
                raise RuntimeError(f"Memory collection field type mismatch: {name}")
            if bool(field.get("is_primary")) != (name == "id") or field.get("auto_id"):
                raise RuntimeError("Memory ID must be the only primary key with auto_id disabled")
            if field.get("nullable") or field.get("is_partition_key"):
                raise RuntimeError(f"Unsupported memory collection field options: {name}")
            params = field.get("params", {})
            if name == "vector":
                if int(params.get("dim", 0)) != self._dim:
                    raise RuntimeError("Memory vector dimension mismatch")
            else:
                required = self._TEXT_LIMITS["memory_id" if name == "id" else name]
                if int(params.get("max_length", 0)) < required:
                    raise RuntimeError(f"Memory collection field too short: {name}")

    @staticmethod
    def _validate_text(name: str, value: str, max_bytes: int) -> None:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a nonempty string")
        if len(value.encode("utf-8")) > max_bytes:
            raise ValueError(f"{name} exceeds maximum UTF-8 byte length ({max_bytes})")

    def _validate_vector(self, vector: list[float]) -> None:
        if not isinstance(vector, (list, tuple)) or len(vector) != self._dim:
            raise ValueError("Invalid embedding dimension")
        if any(isinstance(v, bool) or not isinstance(v, Real) or not math.isfinite(v) for v in vector):
            raise ValueError("Embedding must contain finite real numbers")

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise RuntimeError("Memory vector index is not initialized; call initialize() first")

    async def upsert(self, user_id: str, memory_id: str, content: str) -> None:
        for name, value in (("user_id", user_id), ("memory_id", memory_id), ("content", content)):
            self._validate_text(name, value, self._TEXT_LIMITS[name])
        await asyncio.to_thread(self._upsert_sync, user_id, memory_id, content)

    def _upsert_sync(self, user_id: str, memory_id: str, content: str) -> None:
        with self._lock:
            self._require_initialized()
            owners = self._client.query(
                collection_name=self._collection_name, filter="id == {mid}",
                filter_params={"mid": memory_id}, output_fields=["user_id"],
                consistency_level="Strong", timeout=self._timeout,
            )
            if any(row["user_id"] != user_id for row in owners):
                raise ValueError("memory_id belongs to another user")
            vectors = self._embeddings.embed_documents([content])
            if len(vectors) != 1:
                raise ValueError("Expected exactly one memory embedding")
            self._validate_vector(vectors[0])
            self._client.upsert(
                collection_name=self._collection_name,
                data=[{"id": memory_id, "user_id": user_id, "content": content, "vector": vectors[0]}],
                timeout=self._timeout,
            )

    async def search(
        self, user_id: str, query: str, limit: int = 5,
        *, search_params: dict[str, Any] | None = None,
    ) -> list[tuple[str, float]]:
        self._validate_text("user_id", user_id, 256)
        self._validate_text("query", query, 8000)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if search_params is not None and not isinstance(search_params, dict):
            raise ValueError("search_params must be a dict")
        params = deepcopy(search_params) if search_params is not None else {}
        if params.get("metric_type", "L2") != "L2":
            raise ValueError("search_params metric_type must be L2")
        params["metric_type"] = "L2"
        tuning = params.setdefault("params", {})
        if not isinstance(tuning, dict):
            raise ValueError("search_params params must be a dict")
        nprobe = tuning.setdefault("nprobe", min(16, self._nlist))
        if type(nprobe) is not int or not 1 <= nprobe <= 65536:
            raise ValueError("nprobe must be between 1 and 65536")
        return await asyncio.to_thread(self._search_sync, user_id, query, limit, params)

    def _search_sync(
        self, user_id: str, query: str, limit: int, search_params: dict[str, Any],
    ) -> list[tuple[str, float]]:
        with self._lock:
            self._require_initialized()
            if search_params["params"]["nprobe"] > self._nlist:
                raise ValueError("nprobe must not exceed the vector index nlist")
            vector = self._embeddings.embed_query(query)
            self._validate_vector(vector)
            results = self._client.search(
                collection_name=self._collection_name, data=[vector], anns_field="vector",
                filter="user_id == {uid}", filter_params={"uid": user_id}, limit=limit,
                search_params=search_params, consistency_level="Strong", timeout=self._timeout,
            )
            return [(str(hit["id"]), float(hit["distance"])) for hit in results[0]] if results else []

    async def delete(self, user_id: str, memory_id: str) -> None:
        self._validate_text("user_id", user_id, 256)
        self._validate_text("memory_id", memory_id, 100)
        await asyncio.to_thread(self._delete_sync, user_id, memory_id)

    def _delete_sync(self, user_id: str, memory_id: str) -> None:
        with self._lock:
            self._require_initialized()
            self._client.delete(
                collection_name=self._collection_name,
                filter="id == {mid} and user_id == {uid}",
                filter_params={"mid": memory_id, "uid": user_id}, timeout=self._timeout,
            )
