from collections.abc import Sequence

import pytest

from app.models.conversation_memory import (
    ConversationMemory,
    ConversationMessage,
    ConversationTurn,
    MessageRole,
)


class StubCompressor:
    def __init__(self, result: str = "compressed summary") -> None:
        self.result = result
        self.calls: list[tuple[str | None, tuple[ConversationTurn, ...]]] = []

    async def compress(
        self,
        previous_summary: str | None,
        turns: Sequence[ConversationTurn],
    ) -> str:
        self.calls.append((previous_summary, tuple(turns)))
        return self.result


class FailingCompressor:
    async def compress(
        self,
        previous_summary: str | None,
        turns: Sequence[ConversationTurn],
    ) -> str:
        raise RuntimeError("compressor unavailable")


def make_turn(content: str) -> ConversationTurn:
    return ConversationTurn(
        messages=[ConversationMessage(role=MessageRole.USER, content=content)]
    )


@pytest.mark.asyncio
async def test_add_evicts_complete_turn_to_buffer() -> None:
    memory = ConversationMemory(
        user_id="user-1",
        max_turns=4,
        compression_token_threshold=10_000,
    )
    turns = [make_turn(str(index)) for index in range(5)]

    await memory.add_turns(turns)

    assert memory.retrieve_turns() == turns[1:]
    assert memory.get_compression_buffer() == [turns[0]]
    assert memory.compression_buffer_tokens > 0


@pytest.mark.asyncio
async def test_add_compresses_buffer_after_threshold() -> None:
    compressor = StubCompressor()
    memory = ConversationMemory(
        user_id="user-1",
        compressor=compressor,
        max_turns=2,
        compression_token_threshold=1,
    )
    turns = [make_turn(str(index)) for index in range(3)]

    await memory.add_turns(turns)

    assert memory.summary == "compressed summary"
    assert memory.get_compression_buffer() == []
    assert memory.compression_buffer_tokens == 0
    assert len(compressor.calls) == 1
    assert compressor.calls[0][1] == (turns[0],)


@pytest.mark.asyncio
async def test_compression_failure_retains_buffer() -> None:
    memory = ConversationMemory(
        user_id="user-1",
        compressor=FailingCompressor(),
        max_turns=2,
        compression_token_threshold=1,
    )
    turns = [make_turn(str(index)) for index in range(3)]

    await memory.add_turns(turns)

    assert memory.summary is None
    assert memory.get_compression_buffer() == [turns[0]]
    assert memory.compression_buffer_tokens > 0
    assert memory.retrieve_turns() == turns[1:]


@pytest.mark.asyncio
async def test_force_compresses_buffer_below_threshold() -> None:
    compressor = StubCompressor()
    memory = ConversationMemory(
        user_id="user-1",
        compressor=compressor,
        max_turns=2,
        compression_token_threshold=10_000,
    )
    await memory.add_turns([make_turn("one"), make_turn("two"), make_turn("three")])

    compressed = await memory.force_compress()

    assert compressed is True
    assert memory.summary == "compressed summary"
    assert memory.get_compression_buffer() == []
