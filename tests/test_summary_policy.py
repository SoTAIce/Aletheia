from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models.conversation_memory import ConversationMemory, ConversationMessage, ConversationTurn, MessageRole, TurnStatus
from app.services.conversation_archive import ConversationArchive
from app.services.memory_compression.llm_compressor import LLMCompressor


async def closed(memory, identity, task, status=TurnStatus.COMPLETED, text='goal'):
    now = datetime.now(timezone.utc)
    turn = ConversationTurn([ConversationMessage(MessageRole.USER, text, now)], identity, now,
                            task_id=task, status=TurnStatus.RECEIVED)
    await memory.add(turn)
    if status is not TurnStatus.RECEIVED:
        await memory.complete_turn(identity, status, 'outcome')
    return turn


async def test_selector_excludes_unfinished_and_does_not_mix_tasks(tmp_path):
    archive = ConversationArchive(tmp_path, 's')
    memory = ConversationMemory('s', archive=archive)
    await closed(memory, 'pending', 'a', TurnStatus.RECEIVED)
    await closed(memory, 'a1', 'a', TurnStatus.BLOCKED)
    await closed(memory, 'b1', 'b', TurnStatus.FAILED)
    await closed(memory, 'a2', 'a', TurnStatus.CANCELLED)
    await closed(memory, 'live', 'c')
    batch, _ = archive.pending_summary_turns(set(), 1, 8, 6000)
    assert [t.turn_id for t in batch] == ['a1', 'a2']
    batch, _ = archive.pending_summary_turns({'a1', 'a2'}, 1, 8, 6000)
    assert [t.turn_id for t in batch] == ['b1']


async def test_summary_records_task_provenance_and_legacy_is_readable(tmp_path):
    archive = ConversationArchive(tmp_path, 's')
    memory = ConversationMemory('s', archive=archive)
    await closed(memory, 'old', 'task-a', TurnStatus.BLOCKED, 'Use PostgreSQL; need database name')
    await closed(memory, 'live', 'task-b')
    compressor = SimpleNamespace(compress=AsyncMock(return_value='generated body'))
    memory = ConversationMemory('s', compressor=compressor, archive=archive, max_turns=1)
    await memory.restore()
    assert await memory.force_compress()
    entry = archive.list_summaries()[0]
    assert entry.task_id == 'task-a' and entry.source_turn_ids == ('old',)
    assert 'PostgreSQL' in entry.title and 'blocked' in entry.description
    assert await memory.load_summary(entry.memory_id) == 'generated body'
    # Old strict four-field artifact metadata still loads with unknown task.
    legacy = ConversationTurn([ConversationMessage(MessageRole.USER, 'old legacy')])
    archive.save_turn(legacy)
    old = archive.save_summary([legacy.turn_id], 'legacy body', 'legacy', 'legacy')
    assert old.task_id is None
    assert ConversationArchive(tmp_path, 's').list_summaries() == [entry, old]


async def test_compressor_prompt_has_lifecycle_and_source_metadata():
    from app.services.memory_compression.llm_compressor import SUMMARY_HEADINGS
    create = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=
        SimpleNamespace(role='assistant', content='\n'.join(SUMMARY_HEADINGS), refusal=None, tool_calls=None))]))
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    compressor = LLMCompressor(client, 'unchanged-model')
    turn = ConversationTurn([ConversationMessage(MessageRole.USER, 'Do not deploy')],
                            task_id='task-a', status=TurnStatus.FAILED)
    await compressor.compress(None, [turn])
    prompt = create.call_args.kwargs['messages'][1]['content']
    assert 'task-a' in prompt and 'failed' in prompt and turn.turn_id in prompt
    assert 'Do not deploy' in prompt and 'not proof' in create.call_args.kwargs['messages'][0]['content']
    for heading in SUMMARY_HEADINGS:
        assert heading in prompt


async def test_compressor_rejects_input_over_budget_without_model_call():
    create = AsyncMock()
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    compressor = LLMCompressor(client, 'unchanged-model', max_input_tokens=200)
    with pytest.raises(ValueError, match='budget'):
        await compressor.compress(None, [ConversationTurn([ConversationMessage(MessageRole.USER, 'x' * 1000)])])
    create.assert_not_awaited()
