from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING
from uuid import uuid4

from loguru import logger

from .base_memory import BaseMemory

if TYPE_CHECKING:
    from app.services.conversation_archive import ConversationArchive
    from app.services.memory_compression.base import ConversationCompressor


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


@dataclass
class ConversationMessage:
    role: MessageRole
    content: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class ConversationTurn:
    messages: list[ConversationMessage]
    turn_id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.messages:
            raise ValueError("conversation turn must not be empty")
        if not all(isinstance(message, ConversationMessage) for message in self.messages):
            raise TypeError("conversation turn contains an invalid message type")


@dataclass(frozen=True)
class SummaryEntry:
    memory_id: str
    title: str
    description: str
    storage_ref: str
    source_turn_ids: tuple[str, ...]
    session_id: str


class ConversationMemory(BaseMemory):
    def __init__(
        self,
        user_id: str,
        compressor: ConversationCompressor | None = None,
        max_turns: int = 20,
        compression_token_threshold: int = 6000,
        *,
        archive: ConversationArchive | None = None,
        summary_batch_size: int = 8,
        summary_token_budget: int = 6000,
    ) -> None:
        if max_turns <= 0:
            raise ValueError("max_turns must be greater than zero")
        if compression_token_threshold <= 0:
            raise ValueError("compression_token_threshold must be greater than zero")

        if summary_batch_size <= 0 or summary_token_budget <= 0:
            raise ValueError("summary batch size and token budget must be positive")
        self._summary_batch_size = summary_batch_size
        self._summary_token_budget = summary_token_budget
        self._summary_index: tuple[SummaryEntry, ...] = ()
        self._revision = 0
        super().__init__(user_id)
        self._compressor = compressor
        self._archive = archive
        self._max_turns = max_turns
        self._compression_token_threshold = compression_token_threshold

        self._turns: deque[ConversationTurn] = deque()
        self._compression_buffer: deque[ConversationTurn] = deque()
        self._compression_buffer_tokens = 0
        self._summary: str | None = None
        self._lock = asyncio.Lock()

    def __len__(self) -> int:
        """Return the number of complete active conversation turns."""
        return len(self._turns)

    @property
    def archive(self) -> ConversationArchive | None:
        return self._archive

    async def restore(self) -> None:
        """Rebuild the recent cache from durable originals, without buffering raw history."""
        if self._archive is None:
            return
        async with self._lock:
            revision = self._revision
            turns = await asyncio.to_thread(self._archive.load_turns, self._max_turns)
            entries = await asyncio.to_thread(self._archive.list_summaries)
            if revision != self._revision:
                return
            self._summary_index = tuple(entries)
            self._turns = deque(turns)
            self._compression_buffer.clear()
            self._compression_buffer_tokens = 0
            await self._refresh_pending_locked()

    @property
    def is_full(self) -> bool:
        return len(self._turns) >= self._max_turns

    @property
    def remaining_capacity(self) -> int:
        return max(0, self._max_turns - len(self._turns))

    @property
    def summary(self) -> str | None:
        return self._summary

    @property
    def summary_index(self) -> tuple[SummaryEntry, ...]:
        return self._summary_index

    def find_summaries(self, query: str, limit: int = 3) -> list[SummaryEntry]:
        import re
        if not isinstance(query, str) or type(limit) is not int or limit < 0:
            raise ValueError("invalid summary query or limit")
        terms = set(re.findall(r"\w+", query.casefold()))
        scored = [(sum(term in (entry.title + " " + entry.description).casefold()
                       for term in terms), index, entry)
                  for index, entry in enumerate(self._summary_index)]
        return [entry for score, _, entry in sorted(scored, key=lambda item: (item[0], item[1]), reverse=True)
                if score > 0][:limit]

    async def load_summary(self, memory_id: str) -> str:
        if self._archive is None or not any(
            entry.memory_id == memory_id and entry.session_id == self._archive.session_id
            for entry in self._summary_index
        ):
            raise KeyError(memory_id)
        revision = self._revision
        body = await asyncio.to_thread(self._archive.read_summary, memory_id)
        if revision != self._revision:
            raise KeyError(memory_id)
        return body

    async def find_turns(self, query: str, limit: int = 3) -> list[dict]:
        """Find bounded raw-source metadata, including turns too large to summarize."""
        if self._archive is None:
            return []
        revision = self._revision
        entries = await asyncio.to_thread(self._archive.find_turns, query, limit)
        return entries if revision == self._revision else []

    @property
    def compression_buffer_tokens(self) -> int:
        return self._compression_buffer_tokens

    @property
    def should_compress(self) -> bool:
        return (
            bool(self._compression_buffer)
            and self._compression_buffer_tokens >= self._compression_token_threshold
        )

    async def add(self, turn: ConversationTurn) -> None:
        """Add a complete turn and compress evicted turns when needed."""
        if not isinstance(turn, ConversationTurn):
            raise TypeError("turn must be a ConversationTurn")

        async with self._lock:
            if self._archive is not None:
                # No cache mutation before both durable publication and reload succeed.
                # Cancellation may leave a completed disk write; a retry/restart
                # reconstructs insertion order without adding the same ID twice.
                revision = self._revision
                await asyncio.to_thread(self._archive.save_turn, turn)
                turns = await asyncio.to_thread(self._archive.load_turns, self._max_turns)
                if revision != self._revision:
                    return
                self._turns = deque(turns)
                await self._refresh_pending_locked()
                if self.should_compress and self._compressor is not None:
                    await self._compress_archive_locked()
                return

            for existing in (*self._turns, *self._compression_buffer):
                if existing.turn_id == turn.turn_id:
                    if existing != turn:
                        raise ValueError(f"conversation turn ID conflict: {turn.turn_id}")
                    return
            if len(self._turns) >= self._max_turns:
                evict_count = max(1, self._max_turns // 4)
                self._move_oldest_turns_to_buffer(evict_count)

            self._turns.append(turn)

            if self.should_compress and self._compressor is not None:
                await self._compress_buffer_locked()

    async def add_turns(self, turns: list[ConversationTurn]) -> None:
        """Add multiple turns through the normal eviction and compression path."""
        if not all(isinstance(turn, ConversationTurn) for turn in turns):
            raise TypeError("turns contains an invalid type")

        for turn in turns:
            await self.add(turn)

    def pop(self) -> ConversationTurn | None:
        """Remove and return the most recent active turn."""
        if not self._turns:
            return None
        return self._turns.pop()

    def remove_last(self, count: int = 1) -> None:
        """Remove the most recent active turns."""
        if count < 0:
            raise ValueError("count must not be negative")
        for _ in range(min(count, len(self._turns))):
            self._turns.pop()

    def remove_oldest_turn(self) -> ConversationTurn | None:
        """Remove and return the oldest active turn."""
        if not self._turns:
            return None
        return self._turns.popleft()

    def retrieve(self, k: int = 20) -> list[ConversationTurn]:
        """Return the k most recent active turns."""
        if k < 0:
            raise ValueError("k must not be negative")
        if k == 0:
            return []
        return list(self._turns)[-k:]

    def retrieve_turns(self, k: int = 20) -> list[ConversationTurn]:
        """Explicit alias for retrieving complete active turns."""
        return self.retrieve(k)

    def retrieve_messages(self, k: int = 20) -> list[ConversationMessage]:
        """Flatten the k most recent turns into chronological messages."""
        return [
            message
            for turn in self.retrieve(k)
            for message in turn.messages
        ]

    def get_compression_buffer(self) -> list[ConversationTurn]:
        """Return a snapshot of turns waiting for compression."""
        return list(self._compression_buffer)

    async def force_compress(self) -> bool:
        """Compress a non-empty buffer even when it is below the threshold."""
        async with self._lock:
            if self._archive is not None:
                await self._refresh_pending_locked()
                return await self._compress_archive_locked()
            if self._compressor is None or not self._compression_buffer:
                return False
            return await self._compress_buffer_locked()

    def clear(self) -> None:
        """Clear active turns, pending compression data, and the summary."""
        self._revision += 1
        self._summary_index = ()
        self._turns.clear()
        self._compression_buffer.clear()
        self._compression_buffer_tokens = 0
        self._summary = None

    async def _refresh_pending_locked(self) -> None:
        if self._archive is None or self._compressor is None:
            return
        covered = {turn_id for entry in self._summary_index for turn_id in entry.source_turn_ids}
        revision = self._revision
        turns, tokens = await asyncio.to_thread(
            self._archive.pending_summary_turns, covered, self._max_turns,
            self._summary_batch_size, self._summary_token_budget,
        )
        if revision != self._revision:
            return
        self._compression_buffer = deque(turns)
        self._compression_buffer_tokens = tokens

    async def _compress_archive_locked(self) -> bool:
        if self._compressor is None or not self._compression_buffer or self._archive is None:
            return False
        revision = self._revision
        snapshot = tuple(self._compression_buffer)
        try:
            body = await self._compressor.compress(previous_summary=None, turns=snapshot)
            if not isinstance(body, str) or not body.strip():
                raise ValueError("compressor returned an empty summary")
            if revision != self._revision:
                return False
            entry = await asyncio.to_thread(
                self._archive.save_summary, [turn.turn_id for turn in snapshot], body.strip(),
                body.strip().splitlines()[0][:120], body.strip()[:400], committed=False,
            )
            if revision != self._revision:
                return False
            # No await between the revision check and commit. Only the tiny
            # commit marker is synced here; body/meta staging ran on a worker.
            # This briefly blocks the event loop to make clear/cancel atomic
            # with summary visibility in this single-process implementation.
            self._archive.commit_summary(entry)
        except Exception:
            logger.exception("Stage summary failed; archived originals were retained")
            return False
        self._summary_index = (*self._summary_index, entry)
        self._summary = body.strip()
        covered = {turn.turn_id for turn in snapshot}
        self._compression_buffer = deque(turn for turn in self._compression_buffer if turn.turn_id not in covered)
        self._compression_buffer_tokens = sum(self._estimate_turn_tokens(turn) for turn in self._compression_buffer)
        return True

    def _move_oldest_turns_to_buffer(self, count: int) -> None:
        actual_count = min(count, len(self._turns))
        for _ in range(actual_count):
            old_turn = self._turns.popleft()
            self._compression_buffer.append(old_turn)
            self._compression_buffer_tokens += self._estimate_turn_tokens(old_turn)

    async def _compress_buffer_locked(self) -> bool:
        """Compress the current buffer while the caller holds ``self._lock``."""
        if self._compressor is None or not self._compression_buffer:
            return False

        revision = self._revision
        snapshot = tuple(self._compression_buffer)
        try:
            new_summary = await self._compressor.compress(
                previous_summary=self._summary,
                turns=snapshot,
            )
        except Exception:
            logger.exception(
                "Conversation memory compression failed; pending turns were retained"
            )
            return False

        if revision != self._revision:
            return False
        self._summary = new_summary
        self._compression_buffer.clear()
        self._compression_buffer_tokens = 0
        return True

    @classmethod
    def _estimate_turn_tokens(cls, turn: ConversationTurn) -> int:
        role_overhead = 4 * len(turn.messages)
        content_tokens = sum(
            cls._estimate_text_tokens(message.content) for message in turn.messages
        )
        return max(1, role_overhead + content_tokens)

    @staticmethod
    def _estimate_text_tokens(text: str) -> int:
        """Conservatively estimate tokens for mixed Chinese and Latin text."""
        cjk_count = sum("\u4e00" <= char <= "\u9fff" for char in text)
        other_count = len(text) - cjk_count
        return cjk_count + (other_count + 3) // 4
