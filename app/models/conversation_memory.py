from __future__ import annotations

import asyncio
from copy import deepcopy
from collections import deque
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING
from uuid import uuid4

from loguru import logger

from .base_memory import BaseMemory

if TYPE_CHECKING:
    from app.services.conversation_archive import ConversationArchive
    from app.services.memory_compression.base import ConversationCompressor


async def finish_conversation_write(operation):
    """Settle critical conversation I/O before propagating caller cancellation."""
    job = asyncio.create_task(operation)
    cancelled = False
    while True:
        try:
            value = await asyncio.shield(job)
            break
        except asyncio.CancelledError:
            if job.cancelled():
                raise
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return value


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class TurnStatus(str, Enum):
    RECEIVED = "received"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


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
    task_id: str | None = None
    status: TurnStatus | None = None
    finished_at: datetime | None = None
    question_id: str | None = None

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
    task_id: str | None = None


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
        background_compression: bool = False,
    ) -> None:
        if max_turns <= 0:
            raise ValueError("max_turns must be greater than zero")
        if compression_token_threshold <= 0:
            raise ValueError("compression_token_threshold must be greater than zero")

        if summary_batch_size <= 0 or summary_token_budget <= 0:
            raise ValueError("summary batch size and token budget must be positive")
        self._summary_batch_size = summary_batch_size
        self._summary_token_budget = summary_token_budget
        self._background_compression = background_compression
        self._summary_suspended = False
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
        self._compression_lock = asyncio.Lock()
        self._turn_results: dict[str, dict | None] = {}
        # Memory-only request receipts outlive the bounded prompt cache. They
        # are process-local, and are deliberately discarded by clear().
        self._lifecycle_turns: dict[str, tuple[ConversationTurn, ConversationTurn]] = {}

    async def read_turn(self, turn_id: str) -> ConversationTurn:
        if self._archive is not None:
            return await asyncio.to_thread(self._archive.read_turn, turn_id)
        if turn_id in self._lifecycle_turns:
            return deepcopy(self._lifecycle_turns[turn_id][1])
        for turn in (*self._turns, *self._compression_buffer):
            if turn.turn_id == turn_id:
                return deepcopy(turn)
        raise KeyError(turn_id)

    async def turn_result(self, turn_id: str) -> dict | None:
        if self._archive is not None:
            return await asyncio.to_thread(self._archive.read_turn_result, turn_id)
        return deepcopy(self._turn_results.get(turn_id))

    async def complete_turn(self, turn_id: str, status: TurnStatus, content: str,
                            *, result: dict | None = None) -> ConversationTurn:
        """Finish a received interaction once, without replacing its original input."""
        if not isinstance(status, TurnStatus) or status is TurnStatus.RECEIVED:
            raise ValueError('Invalid completed turn status')
        if not isinstance(content, str):
            raise TypeError('reply content must be a string')
        if result is not None and not isinstance(result, dict):
            raise TypeError('result must be a dictionary or None')
        async with self._lock:
            revision = self._revision
            original = await self.read_turn(turn_id)
            if original.status is not TurnStatus.RECEIVED:
                if (original.status is not status or original.messages[-1].content != content
                        or await self.turn_result(turn_id) != result):
                    raise ValueError('turn outcome conflict')
                return original
            now = datetime.now(UTC)
            completed = replace(original, messages=[*original.messages,
                ConversationMessage(MessageRole.ASSISTANT, content, now)], status=status, finished_at=now)
            if self._archive is not None:
                completed = await asyncio.to_thread(self._archive.complete_turn, completed, result=result)
                turns = await asyncio.to_thread(self._archive.load_turns, self._max_turns)
                if revision != self._revision:
                    return completed
                self._turns = deque(turns)
            elif revision == self._revision:
                self._turns = deque(completed if item.turn_id == turn_id else item for item in self._turns)
                self._compression_buffer = deque(completed if item.turn_id == turn_id else item
                                                  for item in self._compression_buffer)
                self._turn_results[turn_id] = deepcopy(result)
                self._lifecycle_turns[turn_id] = (self._lifecycle_turns[turn_id][0], deepcopy(completed))
                self._compression_buffer_tokens = sum(self._estimate_turn_tokens(t) for t in self._compression_buffer)
            return deepcopy(completed)

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
            self._summary_suspended = False
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

    @property
    def revision(self) -> int:
        """View generation; consumers discard work invalidated by clear()."""
        return self._revision

    def find_summaries(self, query: str, limit: int = 3) -> list[SummaryEntry]:
        import re
        if not isinstance(query, str) or type(limit) is not int or limit < 0:
            raise ValueError("invalid summary query or limit")
        text = query.casefold()
        # Preserve prior Unicode word matching; only CJK sentence spans need
        # additional segmentation rather than exact whole-sentence overlap.
        non_cjk = re.sub(r'[\u3400-\u9fff]+', ' ', text)
        terms = set(re.findall(r'\w+', non_cjk))
        for segment in re.findall(r'[\u3400-\u9fff]+', text):
            terms.update(segment[i:i+2] for i in range(len(segment)-1))
            if len(segment) == 1:
                terms.add(segment)
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
                turns = await asyncio.to_thread(
                    self._archive._save_and_load_recent, turn, self._max_turns,
                )
                if revision != self._revision:
                    return
                self._turns = deque(turns)
                self._summary_suspended = False
                await self._refresh_pending_locked()
                if self.should_compress and self._compressor is not None and not self._background_compression:
                    await self._compress_archive_locked()
                return

            if turn.turn_id in self._lifecycle_turns:
                if self._lifecycle_turns[turn.turn_id][0] != turn:
                    raise ValueError(f'conversation turn ID conflict: {turn.turn_id}')
                return
            for existing in (*self._turns, *self._compression_buffer):
                if existing.turn_id == turn.turn_id:
                    if existing != turn:
                        raise ValueError(f"conversation turn ID conflict: {turn.turn_id}")
                    return
            if len(self._turns) >= self._max_turns:
                evict_count = max(1, self._max_turns // 4)
                self._move_oldest_turns_to_buffer(evict_count)

            if turn.status is not None:
                self._lifecycle_turns[turn.turn_id] = (deepcopy(turn), deepcopy(turn))
                turn = deepcopy(turn)
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
        if self._archive is not None:
            return await self.compress_pending(force=True)
        async with self._lock:
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
        self._summary_suspended = True
        self._turn_results.clear()
        self._lifecycle_turns.clear()

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
        # Compatibility for callers that still request inline automatic work.
        return await self._compress_archive(force=True, lock_held=True)

    @property
    def summary_work_key(self) -> tuple:
        """Identity of the current derived batch, for finite scheduler retries."""
        return self._revision, tuple(t.turn_id for t in self._compression_buffer)

    @property
    def background_summary_enabled(self) -> bool:
        return (not self._summary_suspended and self._background_compression
                and self._archive is not None and self._compressor is not None)

    async def compress_pending(self, *, force: bool = False, raise_errors: bool = False) -> bool:
        """Generate at most one archive batch without holding the memory lock."""
        return await self._compress_archive(force=force, raise_errors=raise_errors)

    async def _compress_archive(self, *, force: bool, lock_held: bool = False, raise_errors: bool = False) -> bool:
        if self._archive is None or self._compressor is None or self._compression_lock.locked():
            return False
        async with self._compression_lock:
            async with nullcontext() if lock_held else self._lock:
                revision = self._revision
                entries = await asyncio.to_thread(self._archive.list_summaries)
                if revision != self._revision:
                    return False
                self._summary_index = tuple(entries)
                await self._refresh_pending_locked()
                if revision != self._revision or not self._compression_buffer or (not force and not self.should_compress):
                    return False
                snapshot = deepcopy(tuple(self._compression_buffer))
            try:
                body = await self._compressor.compress(previous_summary=None, turns=snapshot)
                if not isinstance(body, str) or not body.strip():
                    raise ValueError('compressor returned an empty summary')
                if revision != self._revision:
                    return False
                entry = await asyncio.to_thread(
                    self._archive.save_summary, [turn.turn_id for turn in snapshot], body.strip(),
                    ' '.join(snapshot[0].messages[0].content.split())[:120] or 'Conversation stage',
                    f"task={snapshot[0].task_id or 'unknown'}; turns={len(snapshot)}; outcomes="
                    + ','.join(sorted({t.status.value if t.status else 'legacy' for t in snapshot}))
                    + '; ' + body.strip()[:240], committed=False, expected_turns=snapshot,
                )
                async with nullcontext() if lock_held else self._lock:
                    if revision != self._revision:
                        return False
                    # Keep the small marker atomic with clear/cancel in this
                    # event loop. Body I/O/model generation ran outside the lock.
                    self._archive.commit_summary(entry, expected_turns=snapshot)
                    self._summary_index = (*self._summary_index, entry)
                    self._summary = body.strip()
                    covered = set(entry.source_turn_ids)
                    self._compression_buffer = deque(t for t in self._compression_buffer if t.turn_id not in covered)
                    self._compression_buffer_tokens = sum(self._estimate_turn_tokens(t) for t in self._compression_buffer)
                return True
            except Exception:
                logger.exception('Stage summary failed; archived originals were retained')
                if raise_errors:
                    raise
                return False

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
