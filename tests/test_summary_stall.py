"""Offline tests for summary content-error recovery, scheduler cooldown, and sanitized logs."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest
from loguru import logger as loguru_logger

from app.agent.run_limits import RunLimits
from app.models.conversation_memory import (
    ConversationMemory,
    ConversationMessage,
    ConversationTurn,
    MessageRole,
)
from app.services.conversation_archive import ConversationArchive
from app.services.memory_compression.llm_compressor import (
    LLMCompressor,
    SummaryBatchTooLargeError,
    SummaryContentError,
)
from app.services.summary_scheduler import SummaryScheduler

VALID_SUMMARY = (
    "## Goals and constraints\nNone\n"
    "## Confirmed information\nNone\n"
    "## Completed work\nNone\n"
    "## Failures and cancellations\nNone\n"
    "## Open questions\nNone\n"
    "## Sources\nNone"
)


def _fake_client(
    *,
    finish_reason: str = "stop",
    content: str | None = VALID_SUMMARY,
    role: str = "assistant",
    refusal: str | None = None,
    tool_calls: list | None = None,
    choices: list | None = None,
):
    async def create(**kwargs):
        if choices is not None:
            return SimpleNamespace(choices=choices, usage=None)
        msg = SimpleNamespace(
            role=role,
            content=content,
            refusal=refusal,
            tool_calls=tool_calls,
            function_call=None,
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(finish_reason=finish_reason, message=msg)],
            usage=None,
        )

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _turn(user_text: str, assistant_text: str = "", *, turn_id: str | None = None) -> ConversationTurn:
    messages = [ConversationMessage(MessageRole.USER, user_text)]
    if assistant_text:
        messages.append(ConversationMessage(MessageRole.ASSISTANT, assistant_text))
    kwargs = {"turn_id": turn_id} if turn_id is not None else {}
    return ConversationTurn(messages, **kwargs)


def _seed_archive(tmp_path, session_id: str, count: int = 10) -> tuple[ConversationArchive, list[str]]:
    archive = ConversationArchive(tmp_path, session_id)
    turn_ids: list[str] = []
    for i in range(count):
        tid = f"turn-{i:02d}"
        archive.save_turn(_turn(f"user message {i}", f"reply {i}", turn_id=tid))
        turn_ids.append(tid)
    return archive, turn_ids


async def _until(predicate, timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


# ---- SummaryContentError in LLMCompressor ----


@pytest.mark.parametrize(
    ("client_kwargs", "expected_message"),
    [
        ({"choices": []}, "compression model must return exactly one choice"),
        (
            {
                "choices": [
                    SimpleNamespace(finish_reason="stop", message=SimpleNamespace(role="assistant", content=VALID_SUMMARY)),
                    SimpleNamespace(finish_reason="stop", message=SimpleNamespace(role="assistant", content=VALID_SUMMARY)),
                ]
            },
            "compression model must return exactly one choice",
        ),
        ({"finish_reason": "content_filter"}, "compression model returned an incomplete or refused summary"),
        ({"refusal": "cannot comply"}, "compression model returned an incomplete or refused summary"),
        ({"content": "   "}, "compression model returned an empty summary"),
        ({"content": "## Goals and constraints\nOnly one heading"}, "compression model omitted or duplicated required summary sections"),
        (
            {"content": VALID_SUMMARY + "\n## Sources\nDuplicate"},
            "compression model omitted or duplicated required summary sections",
        ),
    ],
)
async def test_llm_compressor_invalid_outputs_raise_summary_content_error(client_kwargs, expected_message):
    turns = [_turn("hello", "world")]
    comp = LLMCompressor(_fake_client(**client_kwargs), "m")

    with pytest.raises(SummaryContentError, match=expected_message) as caught:
        await comp.compress(None, turns)
    assert isinstance(caught.value, RuntimeError)
    assert not isinstance(caught.value, SummaryBatchTooLargeError)


# ---- Content error recovery in ConversationMemory ----


async def test_single_turn_content_error_skips_on_second_failure_and_summarizes_next(tmp_path):
    archive, turn_ids = _seed_archive(tmp_path, "content-skip", count=8)
    bad_id = turn_ids[0]
    secret_detail = "PRIVATE_CONTENT_ERR_DETAIL"
    seen_batches: list[list[str]] = []

    class FakeCompressor:
        async def compress(self, previous_summary, turns):
            ids = [t.turn_id for t in turns]
            seen_batches.append(ids)
            if bad_id in ids:
                raise SummaryContentError(secret_detail)
            return VALID_SUMMARY

    memory = ConversationMemory(
        "content-skip",
        FakeCompressor(),
        archive=archive,
        max_turns=4,
        summary_batch_size=1,
        summary_token_budget=6000,
        background_compression=True,
    )
    await memory.restore()

    warnings: list[str] = []
    sink_id = loguru_logger.add(
        warnings.append,
        level="WARNING",
        filter=lambda record: record["level"].name == "WARNING",
    )
    try:
        # First failure records the batch without skipping.
        assert await memory.compress_pending(force=True) is False
        assert memory._content_failed_batch == (bad_id,)
        assert memory._skipped_turn_ids == set()

        # Second failure on the same single-turn batch skips it.
        assert await memory.compress_pending(force=True) is False
        assert memory._content_failed_batch is None
        assert bad_id in memory._skipped_turn_ids

        # Next pending turn is summarized normally.
        assert await memory.compress_pending(force=True) is True
    finally:
        loguru_logger.remove(sink_id)

    assert seen_batches == [[bad_id], [bad_id], [turn_ids[1]]]
    assert len(memory.summary_index) == 1
    assert memory.summary_index[0].source_turn_ids == (turn_ids[1],)

    skip_logs = [msg for msg in warnings if "Summary skipped one turn after repeated invalid output" in msg]
    assert len(skip_logs) == 1
    assert "SummaryContentError" in skip_logs[0]
    assert secret_detail not in skip_logs[0]
    assert bad_id not in skip_logs[0]


async def test_multi_turn_content_error_halves_on_second_failure_and_resets_state(tmp_path):
    archive, turn_ids = _seed_archive(tmp_path, "content-halve", count=10)
    seen_batches: list[list[str]] = []

    class FakeCompressor:
        async def compress(self, previous_summary, turns):
            ids = [t.turn_id for t in turns]
            seen_batches.append(ids)
            if len(turns) > 2:
                raise SummaryContentError("missing headings")
            return VALID_SUMMARY

    memory = ConversationMemory(
        "content-halve",
        FakeCompressor(),
        archive=archive,
        max_turns=4,
        summary_batch_size=4,
        summary_token_budget=6000,
        background_compression=True,
    )
    await memory.restore()

    # First failure keeps batch_limit=4.
    assert await memory.compress_pending(force=True) is False
    assert memory._batch_limit == 4
    assert memory._content_failed_batch == tuple(turn_ids[:4])

    # Second failure halves batch_limit to 2 and clears _content_failed_batch.
    assert await memory.compress_pending(force=True) is False
    assert memory._batch_limit == 2
    assert memory._content_failed_batch is None

    # Next call summarizes the halved 2-turn batch and resets batch_limit to 4.
    assert await memory.compress_pending(force=True) is True
    assert memory._batch_limit == 4
    assert memory._content_failed_batch is None
    assert seen_batches == [turn_ids[:4], turn_ids[:4], turn_ids[:2]]

    # clear() and restore() also reset _content_failed_batch.
    memory._content_failed_batch = ("turn-00",)
    memory.clear()
    assert memory._content_failed_batch is None
    memory._content_failed_batch = ("turn-00",)
    await memory.restore()
    assert memory._content_failed_batch is None


async def test_content_error_once_then_valid_commits_without_skipping(tmp_path):
    archive, turn_ids = _seed_archive(tmp_path, "content-once", count=8)
    calls = 0

    class FakeCompressor:
        async def compress(self, previous_summary, turns):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise SummaryContentError("missing headings once")
            return VALID_SUMMARY

    memory = ConversationMemory(
        "content-once",
        FakeCompressor(),
        archive=archive,
        max_turns=4,
        summary_batch_size=4,
        summary_token_budget=6000,
        background_compression=True,
    )
    await memory.restore()

    assert await memory.compress_pending(force=True) is False
    assert memory._content_failed_batch == tuple(turn_ids[:4])
    assert await memory.compress_pending(force=True) is True

    assert calls == 2
    assert memory._content_failed_batch is None
    assert memory._skipped_turn_ids == set()
    assert memory._batch_limit == 4
    assert len(memory.summary_index) == 1
    assert memory.summary_index[0].source_turn_ids == tuple(turn_ids[:4])


async def test_empty_summary_from_custom_compressor_raises_summary_content_error(tmp_path):
    archive, turn_ids = _seed_archive(tmp_path, "empty-custom", count=6)

    class EmptyOnceCompressor:
        async def compress(self, previous_summary, turns):
            if turns[0].turn_id == turn_ids[0]:
                return "   "
            return VALID_SUMMARY

    memory = ConversationMemory(
        "empty-custom",
        EmptyOnceCompressor(),
        archive=archive,
        max_turns=4,
        summary_batch_size=1,
        summary_token_budget=6000,
        background_compression=True,
    )
    await memory.restore()

    with pytest.raises(SummaryContentError, match="compressor returned an empty summary"):
        await memory.compress_pending(force=True, raise_errors=True)
    with pytest.raises(SummaryContentError, match="compressor returned an empty summary"):
        await memory.compress_pending(force=True, raise_errors=True)

    assert turn_ids[0] in memory._skipped_turn_ids
    assert await memory.compress_pending(force=True) is True
    assert memory.summary_index[0].source_turn_ids == (turn_ids[1],)


async def test_scheduler_recovers_from_repeated_content_error_without_cooldown(tmp_path):
    archive, turn_ids = _seed_archive(tmp_path, "sched-content", count=8)
    bad_id = turn_ids[0]
    seen_batches: list[list[str]] = []

    class FakeCompressor:
        async def compress(self, previous_summary, turns):
            ids = [t.turn_id for t in turns]
            seen_batches.append(ids)
            if bad_id in ids:
                raise SummaryContentError("omitted headings")
            return VALID_SUMMARY

    memory = ConversationMemory(
        "sched-content",
        FakeCompressor(),
        archive=archive,
        max_turns=4,
        compression_token_threshold=1,
        summary_batch_size=2,
        summary_token_budget=6000,
        background_compression=True,
    )
    await memory.restore()

    scheduler = SummaryScheduler(
        {"sched-content": memory},
        retry_delay_seconds=0.01,
        scan_interval_seconds=0.01,
        cooldown_seconds=60.0,
        max_attempts=2,
    )
    try:
        scheduler.notify("sched-content")
        await _until(lambda: len(memory.summary_index) >= 1)
    finally:
        await scheduler.aclose()

    assert bad_id in memory._skipped_turn_ids
    assert seen_batches[:4] == [turn_ids[:2], turn_ids[:2], [bad_id], [bad_id]]
    assert memory.summary_index[0].source_turn_ids == (turn_ids[1],)


# ---- SummaryScheduler cooldown ----


@pytest.mark.parametrize("bad_cooldown", [0, -1, float("inf"), float("nan"), True])
def test_scheduler_rejects_invalid_cooldown_seconds(bad_cooldown):
    with pytest.raises(ValueError, match="cooldown_seconds"):
        SummaryScheduler({}, cooldown_seconds=bad_cooldown)


async def test_scheduler_pauses_during_cooldown_and_retries_after_expiry(tmp_path, caplog):
    archive, _ = _seed_archive(tmp_path, "cooldown", count=6)
    calls = 0
    should_fail = True
    secret_detail = "PRIVATE_CONN_ERR_SECRET"

    class FlakyCompressor:
        async def compress(self, previous_summary, turns):
            nonlocal calls
            calls += 1
            if should_fail:
                raise ConnectionError(secret_detail)
            return VALID_SUMMARY

    memory = ConversationMemory(
        "cooldown",
        FlakyCompressor(),
        archive=archive,
        max_turns=4,
        compression_token_threshold=1,
        summary_batch_size=2,
        summary_token_budget=6000,
        background_compression=True,
    )
    await memory.restore()

    scheduler = SummaryScheduler(
        {"cooldown": memory},
        retry_delay_seconds=0.03,
        cooldown_seconds=0.30,
        scan_interval_seconds=0.04,
        max_attempts=2,
    )
    try:
        with caplog.at_level(logging.WARNING, logger="app.services.summary_scheduler"):
            scheduler.notify("cooldown")
            await _until(lambda: calls == 2)

            # Still in cooldown: periodic scans must not trigger new calls.
            await asyncio.sleep(0.15)
            assert calls == 2
            assert memory.summary_index == ()

            # Service recovers; once cooldown expires, scheduler retries and commits.
            should_fail = False
            await _until(lambda: len(memory.summary_index) == 1, timeout=2.0)
    finally:
        await scheduler.aclose()

    assert calls == 3
    pause_logs = [rec.getMessage() for rec in caplog.records if "Summary generation paused" in rec.getMessage()]
    assert len(pause_logs) == 1
    assert "0.3" in pause_logs[0]
    assert "2 attempts" in pause_logs[0]
    assert "ConnectionError" in pause_logs[0]
    assert secret_detail not in pause_logs[0]


# ---- Sanitized failure logs (D-033) ----


async def test_failure_logs_contain_only_type_name_no_detail_or_traceback(tmp_path):
    secret_detail = "SECRET_PROVIDER_400_TOKEN_COUNT_99999"
    secret_summary = "PRIVATE_SUMMARY_TEXT_SHOULD_NOT_LEAK"

    class FailingArchiveCompressor:
        async def compress(self, previous_summary, turns):
            raise RuntimeError(f"{secret_detail} {secret_summary}")

    archive, _ = _seed_archive(tmp_path, "log-sanitize", count=6)
    archive_mem = ConversationMemory(
        "log-sanitize",
        FailingArchiveCompressor(),
        archive=archive,
        max_turns=4,
        summary_batch_size=2,
        summary_token_budget=6000,
        background_compression=True,
    )
    await archive_mem.restore()

    buffer_mem = ConversationMemory(
        "log-buffer",
        FailingArchiveCompressor(),
        max_turns=1,
        compression_token_threshold=1,
    )
    await buffer_mem.add(_turn("first user turn", "first reply"))

    captured: list[str] = []
    sink_id = loguru_logger.add(captured.append, level="WARNING")
    try:
        assert await archive_mem.compress_pending(force=True) is False
        await buffer_mem.add(_turn("second user turn", "second reply"))
    finally:
        loguru_logger.remove(sink_id)

    stage_logs = [m for m in captured if "Stage summary failed" in m]
    buffer_logs = [m for m in captured if "Conversation memory compression failed" in m]
    assert len(stage_logs) == 1
    assert len(buffer_logs) == 1

    for msg in (*stage_logs, *buffer_logs):
        assert "RuntimeError" in msg
        assert secret_detail not in msg
        assert secret_summary not in msg
        assert "Traceback" not in msg


# ---- --show-limits ----


def test_show_limits_includes_summary_cooldown_row():
    rows = RunLimits.current().rows()
    idx = next(
        i for i, (layer, name, _) in enumerate(rows)
        if (layer, name) == ("后台摘要", "单次摘要输出上限")
    )
    assert rows[idx + 1] == ("后台摘要", "摘要失败后的冷却", "600 秒")
