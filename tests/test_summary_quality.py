"""Offline tests for background summary output limit, language hint, and batch halving."""

from __future__ import annotations

import asyncio
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
    CHINESE_BODY_NOTE,
    CONCISE_NOTE,
    DEFAULT_MAX_OUTPUT_TOKENS,
    SUMMARY_HEADINGS,
    SYSTEM_PROMPT,
    LLMCompressor,
    SummaryInputTooLargeError,
    SummaryTooLongError,
    _mostly_chinese,
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


def _fake_client(*, finish_reason: str = "stop", content: str = VALID_SUMMARY,
                 choices: list | None = None, captured: list[dict] | None = None):
    async def create(**kwargs):
        if captured is not None:
            captured.append(kwargs)
        if choices is not None:
            return SimpleNamespace(choices=choices, usage=None)
        msg = SimpleNamespace(
            role="assistant",
            content=content,
            refusal=None,
            tool_calls=None,
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


async def _until(predicate, timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


# ---- Output cap ----

async def test_default_and_explicit_max_output_tokens():
    assert DEFAULT_MAX_OUTPUT_TOKENS == 1536
    default_calls: list[dict] = []
    custom_calls: list[dict] = []
    turns = [_turn("Check the server logs.", "All clear.")]

    default_comp = LLMCompressor(_fake_client(captured=default_calls), "m")
    custom_comp = LLMCompressor(_fake_client(captured=custom_calls), "m", max_output_tokens=256)

    await default_comp.compress(None, turns)
    await custom_comp.compress(None, turns)

    assert default_calls[0]["max_tokens"] == 1536
    assert custom_calls[0]["max_tokens"] == 256


# ---- Language detection and prompt formatting ----

async def test_language_detection_and_prompt_formatting():
    chinese_turns = [_turn("请帮我总结一下今天的会议纪要。", "好的，已经整理完成。")]
    mixed_over_30 = [_turn("请检查 server 日志里的 timeout 问题", "ok 已经修复了三个 bug")]
    mixed_under_30 = [_turn("Please inspect the database migration script and fix 报错", "Done.")]
    english_turns = [_turn("Please summarize the deployment plan.", "Sure, here is the plan.")]
    digits_symbols = [_turn("123456 7890 !@#$%^&*()", "2026-11-25 10:00 -> 200")]

    assert _mostly_chinese(chinese_turns) is True
    assert _mostly_chinese(mixed_over_30) is True
    assert _mostly_chinese(mixed_under_30) is False
    assert _mostly_chinese(english_turns) is False
    assert _mostly_chinese(digits_symbols) is False

    comp = LLMCompressor(_fake_client(), "m")

    # Chinese and mixed >= 30% get the Simplified Chinese body instruction at the end.
    zh_prompt = comp._build_prompt(None, chinese_turns)
    mixed_prompt = comp._build_prompt(None, mixed_over_30)
    assert zh_prompt.endswith(CHINESE_BODY_NOTE)
    assert mixed_prompt.endswith(CHINESE_BODY_NOTE)

    # English batch prompt includes the concise note and omits the Chinese note,
    # even when previous_summary is in Chinese.
    expected_en_prompt = (
        "Previous summary:\n"
        "之前的中文摘要\n\n"
        "New conversation turns:\n"
        f"{comp._format_turns(english_turns)}\n\n"
        "Return only the summary with these exact headings:\n" + "\n".join(SUMMARY_HEADINGS)
        + CONCISE_NOTE
    )
    en_prompt = comp._build_prompt("之前的中文摘要", english_turns)
    assert en_prompt == expected_en_prompt
    assert CHINESE_BODY_NOTE not in en_prompt

    # Digits/symbols-only batch does not append the Chinese instruction.
    sym_prompt = comp._build_prompt(None, digits_symbols)
    assert CHINESE_BODY_NOTE not in sym_prompt

    # System prompt is sent unchanged and user prompt includes the Chinese note for Chinese batches.
    zh_calls: list[dict] = []
    await LLMCompressor(_fake_client(captured=zh_calls), "m").compress(None, chinese_turns)
    assert zh_calls[0]["messages"][0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert zh_calls[0]["messages"][1]["content"].endswith(CHINESE_BODY_NOTE)


async def test_input_budget_counts_chinese_instruction_line():
    turns = [_turn("这是一段中文对话内容。", "收到，马上处理。")]
    comp = LLMCompressor(_fake_client(), "m")
    prompt = comp._build_prompt(None, turns)
    needed = ConversationMemory._estimate_text_tokens(SYSTEM_PROMPT + prompt) + 16
    tight = LLMCompressor(_fake_client(), "m", max_input_tokens=needed - 1)
    with pytest.raises(SummaryInputTooLargeError, match="summary input exceeds token budget"):
        await tight.compress(None, turns)


# ---- Truncation error vs other failures ----

async def test_finish_reason_length_raises_summary_too_long_error():
    turns = [_turn("hello", "world")]

    comp_length = LLMCompressor(_fake_client(finish_reason="length", content="partial"), "m")
    with pytest.raises(SummaryTooLongError) as caught:
        await comp_length.compress(None, turns)
    assert isinstance(caught.value, RuntimeError)

    # Empty choices check runs first and stays plain RuntimeError.
    comp_empty = LLMCompressor(_fake_client(choices=[]), "m")
    with pytest.raises(RuntimeError) as caught_empty:
        await comp_empty.compress(None, turns)
    assert not isinstance(caught_empty.value, SummaryTooLongError)

    # Other incomplete finish reasons stay plain RuntimeError.
    comp_filter = LLMCompressor(_fake_client(finish_reason="content_filter"), "m")
    with pytest.raises(RuntimeError) as caught_filter:
        await comp_filter.compress(None, turns)
    assert not isinstance(caught_filter.value, SummaryTooLongError)

    # Missing headings stay plain RuntimeError.
    comp_bad_headings = LLMCompressor(_fake_client(content="## Goals and constraints\nOnly one"), "m")
    with pytest.raises(RuntimeError) as caught_headings:
        await comp_bad_headings.compress(None, turns)
    assert not isinstance(caught_headings.value, SummaryTooLongError)


# ---- Batch halving, reset on commit, single-turn skip, clear() ----

def _seed_legacy_archive(tmp_path, session_id: str, count: int = 30) -> tuple[ConversationArchive, list[str]]:
    archive = ConversationArchive(tmp_path, session_id)
    turn_ids: list[str] = []
    for i in range(count):
        tid = f"turn-{i:02d}"
        archive.save_turn(_turn(f"legacy user message {i}", f"legacy reply {i}", turn_id=tid))
        turn_ids.append(tid)
    return archive, turn_ids


async def test_truncation_halves_batch_and_resets_after_commit(tmp_path):
    archive, _ = _seed_legacy_archive(tmp_path, "halving", count=30)
    seen_sizes: list[int] = []

    class FakeCompressor:
        async def compress(self, previous_summary, turns):
            seen_sizes.append(len(turns))
            if len(turns) > 2:
                raise SummaryTooLongError("compression model hit the output limit")
            return VALID_SUMMARY

    memory = ConversationMemory(
        "halving",
        FakeCompressor(),
        archive=archive,
        max_turns=6,
        summary_batch_size=8,
        summary_token_budget=6000,
        background_compression=True,
    )
    await memory.restore()

    assert await memory.compress_pending(force=True) is False  # 8 -> fails, limit=4
    assert await memory.compress_pending(force=True) is False  # 4 -> fails, limit=2
    assert await memory.compress_pending(force=True) is True   # 2 -> commits, limit resets to 8
    assert await memory.compress_pending(force=True) is False  # next batch tries 8 again

    assert seen_sizes == [8, 4, 2, 8]
    assert len(memory.summary_index) == 1
    assert len(memory.summary_index[0].source_turn_ids) == 2


async def test_single_turn_truncation_skips_turn_and_logs_only_type_name(tmp_path):
    archive, turn_ids = _seed_legacy_archive(tmp_path, "skip-one", count=30)
    bad_id = turn_ids[0]
    secret_detail = "PRIVATE_PROVIDER_ERR_DETAIL_DO_NOT_LOG"
    seen_batches: list[list[str]] = []

    class FakeCompressor:
        async def compress(self, previous_summary, turns):
            ids = [t.turn_id for t in turns]
            seen_batches.append(ids)
            if bad_id in ids:
                raise SummaryTooLongError(secret_detail)
            return VALID_SUMMARY

    memory = ConversationMemory(
        "skip-one",
        FakeCompressor(),
        archive=archive,
        max_turns=6,
        summary_batch_size=4,
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
        assert await memory.compress_pending(force=True) is False  # 4 -> limit=2
        assert await memory.compress_pending(force=True) is False  # 2 -> limit=1
        assert await memory.compress_pending(force=True) is False  # 1 (bad_id) -> skipped
        assert await memory.compress_pending(force=True) is True   # next turn succeeds
    finally:
        loguru_logger.remove(sink_id)

    assert [len(b) for b in seen_batches] == [4, 2, 1, 1]
    assert seen_batches[2] == [bad_id]
    assert seen_batches[3] == [turn_ids[1]]
    assert len(memory.summary_index) == 1
    assert memory.summary_index[0].source_turn_ids == (turn_ids[1],)
    assert bad_id in memory._skipped_turn_ids

    skip_logs = [msg for msg in warnings if "Summary skipped one turn" in msg]
    assert len(skip_logs) == 1
    assert "SummaryTooLongError" in skip_logs[0]
    assert secret_detail not in skip_logs[0]
    assert bad_id not in skip_logs[0]

    # clear() resets both _skipped_turn_ids and _batch_limit.
    memory._batch_limit = 1
    memory.clear()
    assert memory._skipped_turn_ids == set()
    assert memory._batch_limit == 4


async def test_scheduler_commits_after_truncation_halving(tmp_path):
    archive, _ = _seed_legacy_archive(tmp_path, "sched-halve", count=14)
    seen_sizes: list[int] = []

    class FakeCompressor:
        async def compress(self, previous_summary, turns):
            seen_sizes.append(len(turns))
            if len(turns) > 2:
                raise SummaryTooLongError("compression model hit the output limit")
            return VALID_SUMMARY

    memory = ConversationMemory(
        "sched-halve",
        FakeCompressor(),
        archive=archive,
        max_turns=6,
        compression_token_threshold=1,
        summary_batch_size=8,
        summary_token_budget=6000,
        background_compression=True,
    )
    await memory.restore()
    scheduler = SummaryScheduler(
        {"sched-halve": memory},
        retry_delay_seconds=0.01,
        scan_interval_seconds=0.01,
        max_attempts=2,
    )
    try:
        scheduler.notify("sched-halve")
        await _until(lambda: len(memory.summary_index) >= 1)
    finally:
        await scheduler.aclose()

    assert seen_sizes[:3] == [8, 4, 2]
    assert len(memory.summary_index) >= 1


@pytest.mark.parametrize("error", [RuntimeError("x"), ConnectionError("network down")])
async def test_non_truncation_errors_keep_batch_limit_and_skip_nothing(tmp_path, error):
    archive, _ = _seed_legacy_archive(tmp_path, "other-err", count=14)
    seen_sizes: list[int] = []

    class FakeCompressor:
        async def compress(self, previous_summary, turns):
            seen_sizes.append(len(turns))
            raise error

    memory = ConversationMemory(
        "other-err",
        FakeCompressor(),
        archive=archive,
        max_turns=6,
        summary_batch_size=8,
        summary_token_budget=6000,
        background_compression=True,
    )
    await memory.restore()

    assert await memory.compress_pending(force=True) is False
    assert await memory.compress_pending(force=True) is False

    assert seen_sizes == [8, 8]
    assert memory._batch_limit == 8
    assert memory._skipped_turn_ids == set()


# ---- --show-limits ----

def test_show_limits_includes_summary_output_cap_row():
    rows = RunLimits.current().rows()
    idx = next(
        i for i, (layer, name, _) in enumerate(rows)
        if (layer, name) == ("后台摘要", "历史压缩模型（--compress-history）")
    )
    assert rows[idx + 1] == ("后台摘要", "摘要模型窗口（SUMMARY_MODEL_CONTEXT_TOKENS 或档案）", "4096 令牌")
    assert rows[idx + 2] == ("后台摘要", "单次摘要输出上限", "1024 令牌")
