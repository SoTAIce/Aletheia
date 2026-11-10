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
    # Optional separate endpoint for the agent's chat model only (D-050). Empty
    # means the DashScope endpoint and RAG_MODEL; embeddings, knowledge and
    # long-term memory always stay on DashScope.
    agent_model_base_url: str = ""
    agent_model_api_key: str = ""
    agent_model: str = ""
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
    # Context window of the agent's chat model when it differs from CONTEXT_WINDOW_TOKENS,
    # e.g. a large GPT model behind AGENT_MODEL_BASE_URL. Tool-result budgets scale with it (D-052).
    agent_model_context_tokens: int | None = Field(default=None, gt=0)
    # Send prompt_cache_key (one per conversation) to the separate endpoint so its requests
    # are routed together and hit the prompt cache more often (D-057); turn off if refused.
    agent_model_prompt_cache_key: bool = True

    # Hooks (D-060): one audit file per run in the conversation's archive directory, and an
    # optional token allowance per conversation (empty means no limit).
    hooks_audit: bool = True
    conversation_max_tokens: int | None = Field(default=None, gt=0)

    # Web backend (D-046): local only by default; a token is required unless explicitly waived.
    web_host: str = "127.0.0.1"
    web_port: int = Field(default=8000, gt=0, lt=65536)
    web_access_token: str = ""
    web_allow_no_token: bool = False
    # Tools the web page asks the user to approve before each call (D-061); empty asks for none.
    web_approval_tools: str = "write_file,run_python,run_tests"
    web_approval_timeout_seconds: float = Field(default=300.0, gt=0, le=3600, allow_inf_nan=False)

    @property
    def web_approval_tool_names(self) -> frozenset[str]:
        return frozenset(name.strip() for name in self.web_approval_tools.split(",") if name.strip())
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
    # Choosing the working directory (workspace-001, D-074): folders the agent may never read or
    # write, and optionally the only folders a working directory may be in (both ';'-separated).
    # The web page offers the choice on a loopback host, elsewhere only with allowed roots.
    workspace_choice: bool = True
    workspace_deny_dirs: str = ""
    workspace_allowed_roots: str = ""
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

    @model_validator(mode="after")
    def validate_agent_model_endpoint(self) -> "Settings":
        # A separate endpoint never falls back to the DashScope key, so that key
        # is not sent to a third-party host; a separate key alone is meaningless.
        self.agent_model_base_url = self.agent_model_base_url.strip()
        self.agent_model_api_key = self.agent_model_api_key.strip()
        self.agent_model = self.agent_model.strip()
        if bool(self.agent_model_base_url) != bool(self.agent_model_api_key):
            raise ValueError("AGENT_MODEL_BASE_URL and AGENT_MODEL_API_KEY must be set together")
        return self

    @property
    def agent_model_window_tokens(self) -> int:
        """Context window of the agent's chat model (D-052)."""
        return self.agent_model_context_tokens or self.context_window_tokens

    @property
    def agent_model_endpoint(self) -> tuple[str, str, str]:
        """(base_url, api_key, model) for the agent's chat model."""
        if self.agent_model_base_url:
            return self.agent_model_base_url, self.agent_model_api_key, self.agent_model or self.rag_model
        return self.dashscope_base_url, self.dashscope_api_key, self.agent_model or self.rag_model

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
