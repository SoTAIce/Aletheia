from openai import AsyncOpenAI

from app.models.conversation_memory import ConversationMemory
from app.services.memory_compression.llm_compressor import LLMCompressor


ollama_client = AsyncOpenAI(
    base_url="http://127.0.0.1:11434/v1",
    api_key="ollama",
    timeout=120.0,
    max_retries=2,
)

conversation_compressor = LLMCompressor(
    client=ollama_client,
    model="qwen3:4b-instruct",
)


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
