"""Bounded, process-local scheduling of reconstructible archive summaries."""
from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Mapping

from app.models.conversation_memory import ConversationMemory

logger = logging.getLogger(__name__)


class SummaryScheduler:
    """Queue session keys, never transcript bodies. Caller owns compressors."""

    def __init__(self, memories: Mapping[object, ConversationMemory], *, max_concurrency=2,
                 max_pending=64, timeout_seconds=30.0, max_wait_seconds=300.0,
                 scan_interval_seconds=30.0, retry_delay_seconds=1.0, max_attempts=2):
        for name, value in [('max_concurrency', max_concurrency), ('max_pending', max_pending),
                            ('max_attempts', max_attempts)]:
            if type(value) is not int or value < 1:
                raise ValueError(f'{name} must be a positive integer')
        for name, value in [('timeout_seconds', timeout_seconds), ('max_wait_seconds', max_wait_seconds),
                            ('scan_interval_seconds', scan_interval_seconds), ('retry_delay_seconds', retry_delay_seconds)]:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f'{name} must be finite and positive')
        self._memories = memories
        self._concurrency, self._capacity = max_concurrency, max_pending
        self._timeout, self._max_wait = timeout_seconds, max_wait_seconds
        self._interval, self._retry_delay, self._max_attempts = scan_interval_seconds, retry_delay_seconds, max_attempts
        self._pending: dict[object, tuple[float, float]] = {}
        self._active: dict[object, tuple[asyncio.Task, float]] = {}
        self._failures: dict[object, tuple[tuple, int, float]] = {}
        self._waiting_since: dict[object, tuple[int, float]] = {}
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._closing = False
        self._scan_cursor = 0

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def notify(self, key: object) -> bool:
        memory = self._memories.get(key)
        if self._closing or memory is None or not memory.background_summary_enabled:
            return False
        generation = memory.summary_work_key[0]
        previous = self._waiting_since.get(key)
        if previous is None or previous[0] != generation:
            self._waiting_since[key] = generation, time.monotonic()
        if key not in self._active and key not in self._pending:
            if len(self._pending) >= self._capacity:
                return False  # Originals remain pending; a rotating scan retries admission.
            now = time.monotonic()
            signature, _, not_before = self._failures.get(key, (None, 0, 0.0))
            due = max(now, not_before) if signature == memory.summary_work_key else now
            self._pending[key] = self._waiting_since[key][1], due
        self.start()
        self._wake.set()
        return True

    def start(self) -> None:
        if not self._closing and self._task is None:
            self._task = asyncio.create_task(self._run(), name='conversation-summary-scheduler')

    def _scan(self) -> None:
        keys = list(self._memories)
        if not keys:
            return
        # Rotate admission so small, below-threshold sessions cannot starve
        # sessions whose earlier notification arrived while the queue was full.
        for offset in range(len(keys)):
            index = (self._scan_cursor + offset) % len(keys)
            if len(self._pending) >= self._capacity:
                self._scan_cursor = index
                break
            self.notify(keys[index])
        else:
            self._scan_cursor = (self._scan_cursor + 1) % len(keys)

    async def _compress(self, key, since) -> float | None:
        memory = self._memories.get(key)
        if memory is None or not memory.background_summary_enabled:
            return None
        signature, attempts, _ = self._failures.get(key, (None, 0, 0.0))
        if signature == memory.summary_work_key and attempts >= self._max_attempts:
            return None
        try:
            async with asyncio.timeout(self._timeout):
                committed = await memory.compress_pending(
                    force=time.monotonic() - since >= self._max_wait, raise_errors=True)
        except Exception as exc:
            signature_now = memory.summary_work_key
            attempts = attempts + 1 if signature == signature_now else 1
            self._failures[key] = signature_now, attempts, time.monotonic() + self._retry_delay
            logger.warning('Summary generation deferred (%s), attempt %s/%s',
                           type(exc).__name__, attempts, self._max_attempts)
            return self._retry_delay if attempts < self._max_attempts else None
        if committed:
            self._failures.pop(key, None)
            return 0.0  # Examine the next batch; at most one batch per work item.
        if memory.get_compression_buffer():
            return self._interval  # Below threshold, or another caller is compressing.
        if memory.compression_buffer_tokens:
            logger.warning('Summary backlog cannot fit a batch; original turns remain readable')
        else:
            self._waiting_since.pop(key, None)
        return None

    async def _run(self):
        next_scan = time.monotonic() + self._interval
        try:
            while not self._closing or self._pending or self._active:
                now = time.monotonic()
                self._wake.clear()
                if not self._closing and now >= next_scan:
                    self._scan()
                    next_scan = now + self._interval
                for key, (task, since) in list(self._active.items()):
                    if task.done():
                        del self._active[key]
                        delay = task.result()
                        if delay is not None and not self._closing and len(self._pending) < self._capacity:
                            self._pending[key] = since, now + delay
                for key, (since, due) in list(self._pending.items()):
                    if len(self._active) >= self._concurrency:
                        break
                    if self._closing and due > now:
                        del self._pending[key]  # Shutdown must not bypass retry backoff.
                        continue
                    if due <= now:
                        del self._pending[key]
                        task = asyncio.create_task(self._compress(key, since), name='conversation-summary-batch')
                        task.add_done_callback(lambda _: self._wake.set())
                        self._active[key] = task, since
                if self._closing and not self._pending and not self._active:
                    break
                waits = [self._interval]
                if len(self._active) < self._concurrency:
                    waits.extend(max(.001, due - now) for _, due in self._pending.values())
                try:
                    await asyncio.wait_for(self._wake.wait(), min(waits))
                except TimeoutError:
                    pass
        finally:
            tasks = [task for task, _ in self._active.values()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._active.clear()
            self._pending.clear()

    async def aclose(self, *, timeout_seconds=5.0):
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError('timeout_seconds must be finite and positive')
        self._closing = True
        self._wake.set()
        if self._task is not None:
            try:
                async with asyncio.timeout(timeout_seconds):
                    await asyncio.shield(self._task)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
                if asyncio.current_task().cancelling():
                    raise
