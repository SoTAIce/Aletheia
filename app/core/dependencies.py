import asyncio
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from openai import AsyncOpenAI

from app.config import config
from app.models.conversation_memory import ConversationMemory
from app.services.memory_compression.llm_compressor import LLMCompressor

_DEFAULT_SUMMARY_BASE_URL = "http://127.0.0.1:11434/v1"
_DEFAULT_SUMMARY_MODEL = "qwen3:4b-instruct"

ollama_client = AsyncOpenAI(
    base_url=config.summary_model_base_url.strip() or _DEFAULT_SUMMARY_BASE_URL,
    api_key="ollama",
    timeout=120.0,
    max_retries=2,
)

conversation_compressor = LLMCompressor(
    client=ollama_client,
    model=config.summary_model.strip() or _DEFAULT_SUMMARY_MODEL,
)


def _summary_host_port(raw_url: object) -> str:
    """Format only host:port from a base URL for user-facing error messages."""
    parsed = urlparse(str(raw_url or config.summary_model_base_url))
    host = parsed.hostname or "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is None:
        port = 443 if parsed.scheme == "https" else (80 if parsed.scheme == "http" else None)
    return f"{host}:{port}" if port is not None else host


@asynccontextmanager
async def open_conversation_compressor():
    """Owned local client for background work; scheduler controls retries."""
    base_url = config.summary_model_base_url.strip()
    model = config.summary_model.strip()
    async with AsyncOpenAI(
        base_url=base_url,
        api_key="ollama",
        timeout=120.0,
        max_retries=0,
    ) as client:
        yield LLMCompressor(client=client, model=model)


async def check_conversation_compressor(compressor, *, timeout_seconds: float = 3.0) -> str | None:
    """Probe the local summary model endpoint via /models; returns a Chinese reason or None."""
    client = compressor._client
    model = compressor._model
    host_port = _summary_host_port(getattr(client, "base_url", None))
    try:
        async with asyncio.timeout(timeout_seconds):
            response = await client.models.list()
    except Exception as exc:
        return f"历史压缩不可用：连不上本机摘要服务（{host_port}，{type(exc).__name__}），请先启动 Ollama"
    items = getattr(response, "data", response)
    model_ids = {getattr(item, "id", item) for item in (items or ())}
    if model not in model_ids:
        return f"历史压缩不可用：本机没有模型 {model}，请先 ollama pull {model}（请写完整模型名）"
    return None


def create_conversation_memory(
    user_id: str,
    max_turns: int = 20,
    compression_token_threshold: int = 6000,
) -> ConversationMemory:
    """Create conversation memory with the shared local compressor."""
    return ConversationMemory(
        user_id=user_id,
        compressor=conversation_compressor,
        max_turns=max_turns,
        compression_token_threshold=compression_token_threshold,
    )
