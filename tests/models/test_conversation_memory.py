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


@pytest.mark.asyncio
async def test_archive_restores_recent_turns_without_raw_eviction_buffer(tmp_path):
    from app.services.conversation_archive import ConversationArchive
    archive = ConversationArchive(tmp_path, "session")
    memory = ConversationMemory("u", max_turns=2, archive=archive)
    turns = [make_turn(str(i)) for i in range(12)]
    await memory.add_turns(turns)
    assert memory.retrieve() == turns[-2:]
    assert memory.get_compression_buffer() == []
    assert memory.compression_buffer_tokens == 0
    restored = ConversationMemory("u", max_turns=2, archive=ConversationArchive(tmp_path, "session"))
    await restored.restore()
    assert restored.retrieve() == turns[-2:]
    assert archive.load_turns() == turns
    await restored.add(turns[0])
    assert restored.retrieve() == turns[-2:]
    assert archive.load_turns() == turns


@pytest.mark.asyncio
async def test_failed_archive_save_does_not_evict_cache(tmp_path, monkeypatch):
    from app.services.conversation_archive import ConversationArchive
    archive = ConversationArchive(tmp_path, "session")
    memory = ConversationMemory("u", max_turns=1, archive=archive)
    original = make_turn("original")
    await memory.add(original)
    def fail(turn):
        raise OSError("disk full")
    monkeypatch.setattr(archive, "save_turn", fail)
    with pytest.raises(OSError):
        await memory.add(make_turn("new"))
    assert memory.retrieve() == [original]
    assert archive.load_turns() == [original]


@pytest.mark.asyncio
async def test_concurrent_duplicate_add_is_idempotent(tmp_path):
    import asyncio
    from dataclasses import replace
    from app.services.conversation_archive import ConversationArchive
    archive = ConversationArchive(tmp_path, "session")
    memory = ConversationMemory("u", max_turns=20, archive=archive)
    turns = [make_turn(str(i)) for i in range(10)]
    await asyncio.gather(*(memory.add(turn) for turn in turns for _ in range(3)))
    assert memory.retrieve() == turns
    assert archive.load_turns() == turns
    with pytest.raises(ValueError, match="conflict"):
        await memory.add(replace(turns[0], messages=[ConversationMessage(MessageRole.USER, "different")]))
    assert memory.retrieve() == turns


@pytest.mark.asyncio
async def test_cancelled_save_leaves_cache_and_retry_recovers_one_durable_turn(tmp_path, monkeypatch):
    import asyncio
    import threading
    from app.services.conversation_archive import ConversationArchive
    archive = ConversationArchive(tmp_path, "session")
    memory = ConversationMemory("u", max_turns=1, archive=archive)
    original, pending = make_turn("original"), make_turn("pending")
    await memory.add(original)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    save = archive.save_turn
    def blocked_save(turn):
        started.set()
        assert release.wait(5)
        try:
            save(turn)
        finally:
            finished.set()
    monkeypatch.setattr(archive, "save_turn", blocked_save)
    task = asyncio.create_task(memory.add(pending))
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
        assert memory.retrieve() == [original]
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)
    await memory.add(pending)
    assert memory.retrieve() == [pending]
    assert archive.load_turns() == [original, pending]


@pytest.mark.asyncio
async def test_legacy_memory_deduplicates_turn_id():
    memory = ConversationMemory("u")
    turn = make_turn("same")
    await memory.add(turn)
    await memory.add(turn)
    assert memory.retrieve() == [turn]
