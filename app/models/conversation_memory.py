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


class ConversationMemory(BaseMemory):
    def __init__(
        self,
        user_id: str,
        compressor: ConversationCompressor | None = None,
        max_turns: int = 20,
        compression_token_threshold: int = 6000,
    ) -> None:
        if max_turns <= 0:
            raise ValueError("max_turns must be greater than zero")
        if compression_token_threshold <= 0:
            raise ValueError("compression_token_threshold must be greater than zero")

        super().__init__(user_id)
        self._compressor = compressor
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
    def is_full(self) -> bool:
        return len(self._turns) >= self._max_turns

    @property
    def remaining_capacity(self) -> int:
        return max(0, self._max_turns - len(self._turns))

    @property
    def summary(self) -> str | None:
        return self._summary

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
            if self._compressor is None or not self._compression_buffer:
                return False
            return await self._compress_buffer_locked()

    def clear(self) -> None:
        """Clear active turns, pending compression data, and the summary."""
        self._turns.clear()
        self._compression_buffer.clear()
        self._compression_buffer_tokens = 0
        self._summary = None

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
