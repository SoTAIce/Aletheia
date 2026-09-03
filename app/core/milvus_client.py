"""Milvus connection and collection lifecycle management."""

from loguru import logger
from pymilvus import (
    Collection,
    CollectionSchema,
    DataType,
    FieldSchema,
    MilvusClient,
    connections,
    utility,
)

from app.config import config


def _patch_pymilvus_milvus_client_orm_alias() -> None:
    """Keep LangChain Milvus and ORM operations on the same connection alias."""
    if getattr(_patch_pymilvus_milvus_client_orm_alias, "_done", False):
        return
    try:
        from pymilvus.milvus_client.milvus_client import MilvusClient as ClientClass
    except ImportError:
        return

    original_init = ClientClass.__init__

    def wrapped_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        original_init(self, *args, **kwargs)
        self._using = "default"

    ClientClass.__init__ = wrapped_init  # type: ignore[method-assign]
    setattr(_patch_pymilvus_milvus_client_orm_alias, "_done", True)


class MilvusClientManager:
    COLLECTION_NAME = "biz"
    ID_MAX_LENGTH = 100
    CONTENT_MAX_LENGTH = 8000
    DEFAULT_SHARD_NUMBER = 2

    def __init__(self, collection_name: str | None = None) -> None:
        self.collection_name = collection_name or self.COLLECTION_NAME
        self._client: MilvusClient | None = None
        self._collection: Collection | None = None

    @property
    def vector_dim(self) -> int:
        return config.embedding_dimensions

    def connect(self) -> MilvusClient:
        if self._collection is not None and self._client is not None:
            return self._client

        try:
            _patch_pymilvus_milvus_client_orm_alias()
            connections.connect(
                alias="default",
                host=config.milvus_host,
                port=str(config.milvus_port),
                timeout=config.milvus_timeout / 1000,
            )
            self._client = MilvusClient(
                uri=f"http://{config.milvus_host}:{config.milvus_port}"
            )

            if not utility.has_collection(self.collection_name):
                self._create_collection()
            else:
                self._collection = Collection(self.collection_name)
                existing_dimension = self._get_existing_vector_dimension()
                if existing_dimension is not None:
                    self._handle_dimension_mismatch(existing_dimension)

            self._load_collection()
            return self._client
        except Exception as exc:
            self.close()
            if isinstance(exc, RuntimeError):
                raise
            raise RuntimeError(f"Milvus initialization failed: {exc}") from exc

    def _get_existing_vector_dimension(self) -> int | None:
        if self._collection is None:
            return None
        for field in self._collection.schema.fields:
            if field.name == "vector" and "dim" in getattr(field, "params", {}):
                return int(field.params["dim"])
        return None

    def _handle_dimension_mismatch(self, existing_dimension: int) -> None:
        if existing_dimension == self.vector_dim:
            return
        message = (
            f"Milvus vector dimension mismatch: collection={existing_dimension}, "
            f"configured={self.vector_dim}"
        )
        if not config.milvus_allow_drop_on_dimension_mismatch:
            raise RuntimeError(
                message
                + "; automatic collection deletion is disabled. Reindex explicitly or set "
                "MILVUS_ALLOW_DROP_ON_DIMENSION_MISMATCH=true."
            )

        logger.warning("{}; recreating collection by explicit configuration", message)
        if self._collection is not None:
            self._collection.release()
            self._collection = None
        utility.drop_collection(self.collection_name)
        self._create_collection()

    def _create_collection(self) -> None:
        fields = [
            FieldSchema(
                name="id",
                dtype=DataType.VARCHAR,
                max_length=self.ID_MAX_LENGTH,
                is_primary=True,
            ),
            FieldSchema(
                name="vector",
                dtype=DataType.FLOAT_VECTOR,
                dim=self.vector_dim,
            ),
            FieldSchema(
                name="content",
                dtype=DataType.VARCHAR,
                max_length=self.CONTENT_MAX_LENGTH,
            ),
            FieldSchema(name="metadata", dtype=DataType.JSON),
        ]
        schema = CollectionSchema(
            fields=fields,
            description="Business knowledge collection",
            enable_dynamic_field=False,
        )
        self._collection = Collection(
            name=self.collection_name,
            schema=schema,
            num_shards=self.DEFAULT_SHARD_NUMBER,
        )
        self._collection.create_index(
            field_name="vector",
            index_params={
                "metric_type": "L2",
                "index_type": "IVF_FLAT",
                "params": {"nlist": 128},
            },
        )

    def _load_collection(self) -> None:
        if self._collection is None:
            self._collection = Collection(self.collection_name)
        load_state = utility.load_state(self.collection_name)
        if getattr(load_state, "name", str(load_state)) != "Loaded":
            self._collection.load()

    def get_collection(self) -> Collection:
        if self._collection is None:
            raise RuntimeError("Milvus collection is not initialized; call connect() first")
        return self._collection

    def health_check(self) -> bool:
        if self._client is None:
            return False
        try:
            connections.list_connections()
            return True
        except Exception:
            logger.exception("Milvus health check failed")
            return False

    def close(self) -> None:
        if self._collection is not None:
            try:
                self._collection.release()
            except Exception:
                logger.exception("Failed to release Milvus collection")
            finally:
                self._collection = None
        try:
            if connections.has_connection("default"):
                connections.disconnect("default")
        except Exception:
            logger.exception("Failed to disconnect Milvus")
        self._client = None

    def __enter__(self) -> "MilvusClientManager":
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        self.close()


milvus_manager = MilvusClientManager()
