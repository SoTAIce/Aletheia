"""DashScope embedding adapter for LangChain."""

from typing import Any

from langchain_core.embeddings import Embeddings
from loguru import logger
from openai import OpenAI

from app.config import config


class DashScopeEmbeddings(Embeddings):
    """OpenAI-compatible DashScope embeddings with strict output validation."""

    _PLACEHOLDER_KEYS = {
        "your_api_key_here",
        "your-api-key-here",
        "your_api_key",
        "your-api-key",
    }

    def __init__(
        self,
        api_key: str,
        model: str = "text-embedding-v4",
        dimensions: int = 1024,
        batch_size: int = 10,
        timeout: float = 30.0,
        max_retries: int = 3,
        client: Any | None = None,
    ) -> None:
        if dimensions <= 0:
            raise ValueError("dimensions must be greater than zero")
        if batch_size <= 0:
            raise ValueError("batch_size must be greater than zero")

        self.api_key = api_key.strip()
        self.model = model
        self.dimensions = dimensions
        self.batch_size = batch_size
        self.timeout = timeout
        self.max_retries = max_retries
        self._client = client

    @property
    def client(self) -> Any:
        """Create the external client only when an embedding is requested."""
        if self._client is None:
            normalized_key = self.api_key.lower()
            if not normalized_key or normalized_key in self._PLACEHOLDER_KEYS:
                raise ValueError("DASHSCOPE_API_KEY is not configured")
            self._client = OpenAI(
                api_key=self.api_key,
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                timeout=self.timeout,
                max_retries=self.max_retries,
            )
            logger.info(
                "DashScope embeddings initialized: model={}, dimensions={}, key={}",
                self.model,
                self.dimensions,
                self._mask_api_key(self.api_key),
            )
        return self._client

    @staticmethod
    def _mask_api_key(api_key: str) -> str:
        if len(api_key) > 12:
            return f"{api_key[:8]}...{api_key[-4:]}"
        return "***"

    def _extract_embeddings(self, response: Any, expected_count: int) -> list[list[float]]:
        embeddings = [list(item.embedding) for item in response.data]
        if len(embeddings) != expected_count:
            raise ValueError(
                f"embedding response count mismatch: expected {expected_count}, got {len(embeddings)}"
            )
        for index, embedding in enumerate(embeddings):
            if len(embedding) != self.dimensions:
                raise ValueError(
                    f"embedding dimension mismatch at index {index}: "
                    f"expected {self.dimensions}, got {len(embedding)}"
                )
        return embeddings

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        client = self.client
        all_embeddings: list[list[float]] = []
        try:
            for start in range(0, len(texts), self.batch_size):
                batch = texts[start : start + self.batch_size]
                response = client.embeddings.create(
                    model=self.model,
                    input=batch,
                    dimensions=self.dimensions,
                    encoding_format="float",
                )
                all_embeddings.extend(self._extract_embeddings(response, len(batch)))
        except ValueError:
            raise
        except Exception as exc:
            logger.exception("Batch embedding request failed")
            raise RuntimeError(f"batch embedding request failed: {exc}") from exc

        return all_embeddings

    def embed_query(self, text: str) -> list[float]:
        if not text or not text.strip():
            raise ValueError("query text must not be empty")

        client = self.client
        try:
            response = client.embeddings.create(
                model=self.model,
                input=text,
                dimensions=self.dimensions,
                encoding_format="float",
            )
            return self._extract_embeddings(response, 1)[0]
        except ValueError:
            raise
        except Exception as exc:
            logger.exception("Query embedding request failed")
            raise RuntimeError(f"query embedding request failed: {exc}") from exc


vector_embedding_service = DashScopeEmbeddings(
    api_key=config.dashscope_api_key,
    model=config.dashscope_embedding_model,
    dimensions=config.embedding_dimensions,
    batch_size=config.embedding_batch_size,
    timeout=config.embedding_timeout_seconds,
    max_retries=config.embedding_max_retries,
)
