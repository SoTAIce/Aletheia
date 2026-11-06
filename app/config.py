"""Type-safe application configuration loaded from environment variables."""

from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    """Application settings isolated from generic host environment names."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "SuperBizAgent"
    app_version: str = "1.0.0"
    app_debug: bool = False
    host: str = "0.0.0.0"
    port: int = 9900

    dashscope_api_key: str = ""
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    dashscope_model: str = "qwen-max"
    dashscope_embedding_model: str = "text-embedding-v4"
    embedding_dimensions: int = Field(default=1024, gt=0)
    embedding_batch_size: int = Field(default=10, gt=0)
    embedding_timeout_seconds: float = Field(default=30.0, gt=0)
    embedding_max_retries: int = Field(default=3, ge=0)

    milvus_host: str = "localhost"
    milvus_port: int = 19530
    milvus_timeout: int = 10000
    milvus_allow_drop_on_dimension_mismatch: bool = False

    rag_top_k: int = Field(default=3, gt=0)
    rag_retrieval_mode: Literal["dense", "bm25", "hybrid"] = "hybrid"
    rag_bm25_top_k: int = Field(default=20, gt=0)
    rag_hybrid_candidate_k: int = Field(default=20, gt=0)
    rag_rrf_k: int = Field(default=60, gt=0)
    rag_model: str = "qwen-max"
    rag_json_mode: bool = True
    rag_enable_thinking: bool | None = None
    rag_model_timeout_seconds: float = Field(default=30.0, gt=0, allow_inf_nan=False)
    agent_step_timeout_seconds: float = Field(default=60.0, gt=0, allow_inf_nan=False)
    agent_planning_timeout_seconds: float = Field(default=60.0, gt=0, allow_inf_nan=False)
    # Optional prices for the usage report; unset means no cost estimate (D-034).
    model_input_price_per_million_tokens: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    model_output_price_per_million_tokens: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    rag_query_rewrite_model: str = "qwen3.8-flash"
    rag_enable_rerank: bool = True
    rag_rerank_top_k: int = Field(default=12, ge=2, le=15)
    rag_rerank_model: str = "BAAI/bge-reranker-v2-m3"
    rag_rerank_cache_dir: str = "data/models/huggingface"
    rag_rerank_timeout_seconds: float = Field(default=10.0, gt=0)
    rag_context_token_budget: int = Field(default=1800, gt=0)
    # Archived by default; an explicit empty value keeps the in-memory mode.
    # Relative paths are anchored at the project root, not the working directory.
    conversation_storage_dir: str = Field(default="data/conversations", validate_default=True)
    # Long-term memory (D-042): empty DSN keeps it off. PostgreSQL holds the records,
    # Milvus the vectors in a collection separate from the knowledge base.
    longterm_memory_dsn: str = ""
    longterm_memory_collection: str = "agent_memories"
    longterm_context_tokens: int = Field(default=600, gt=0)
    longterm_recall_limit: int = Field(default=5, ge=1, le=100)
    context_window_tokens: int = Field(default=32768, gt=0)
    # Per-call output cap; 2048 cut off whole modules and pages in the bench (D-036).
    context_output_tokens: int = Field(default=8192, gt=0)
    context_safety_tokens: int = Field(default=1024, ge=0)

    # Web backend (D-046): local only by default; a token is required unless explicitly waived.
    web_host: str = "127.0.0.1"
    web_port: int = Field(default=8000, gt=0, lt=65536)
    web_access_token: str = ""
    web_allow_no_token: bool = False
    web_user_id: str = Field(default="local", min_length=1, max_length=256)
    web_max_concurrent_runs: int = Field(default=2, ge=1, le=32)
    web_event_retention_seconds: float = Field(default=1800.0, gt=0, allow_inf_nan=False)
    web_shutdown_timeout_seconds: float = Field(default=30.0, gt=0, allow_inf_nan=False)

    chunk_max_size: int = Field(default=800, gt=0)
    chunk_overlap: int = Field(default=100, ge=0)
    rag_supported_extensions: tuple[str, ...] = (".md", ".txt", ".py", ".json")

    mcp_cls_transport: str = "streamable-http"
    mcp_cls_url: str = "http://localhost:8003/mcp"
    mcp_monitor_transport: str = "streamable-http"
    mcp_monitor_url: str = "http://localhost:8004/mcp"
    mcp_filesystem_root: str = ""
    mcp_filesystem_write_root: str = ""
    tavily_api_key: str = ""

    prometheus_base_url: str = "http://127.0.0.1:9090"
    prometheus_request_timeout: float = 10.0

    @field_validator("conversation_storage_dir", mode="after")
    @classmethod
    def anchor_conversation_storage_dir(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        path = Path(value).expanduser()
        return str(path if path.is_absolute() else PROJECT_ROOT / path)

    @model_validator(mode="after")
    def validate_chunk_config(self) -> "Settings":
        if self.chunk_overlap >= self.chunk_max_size:
            raise ValueError("chunk_overlap must be smaller than chunk_max_size")
        return self

    @property
    def debug(self) -> bool:
        """Backward-compatible alias for existing ``config.debug`` callers."""
        return self.app_debug

    @property
    def mcp_servers(self) -> dict[str, dict[str, Any]]:
        return {
            "cls": {
                "transport": self.mcp_cls_transport,
                "url": self.mcp_cls_url,
            },
            "monitor": {
                "transport": self.mcp_monitor_transport,
                "url": self.mcp_monitor_url,
            },
        }


config = Settings()
