"""Built-in hooks: the audit log and the conversation token cap (D-060)."""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.builtin_hooks import AuditHook, ConversationTokenCap
from app.agent.hooks import PolicyDeniedError
from app.agent.run_meter import RunMeter, metering, record_model_call
from app.agent.runtime import FAILURE_MESSAGES, _policy_denial
from app.agent.step_models import ModelTurn, ToolCall
from app.config import config
from app.models.taskstate import TaskStatus
from app.services.conversation_archive import ConversationArchive
from app.services.rag_agent_service import RagAgentService


def outcome(text, status='succeeded', reason=None):
    return ModelTurn(json.dumps({'status': status, 'content': text, 'reason': reason}))


class Model:
    """A scripted model that reports provider usage the way the real client does."""

    def __init__(self, turns, tokens=300):
        self.turns, self.tokens, self.calls = list(turns), tokens, 0

    async def generate(self, messages, tools):
        self.calls += 1
        record_model_call(SimpleNamespace(prompt_tokens=self.tokens - 10, completion_tokens=10))
        return self.turns.pop(0)


def service(tmp_path, turns, plans=(('Answer',),), **kw):
    planner = AsyncMock()
    planner.generate.side_effect = [json.dumps({'steps': list(s), 'reason': None}) for s in plans]
    model = turns if isinstance(turns, Model) else Model(turns)
    return RagAgentService(model=model, planner_model=planner, mcp_servers=(), include_knowledge=False,
                           memory_directory=tmp_path, **kw), model


def audits(tmp_path):
    return sorted(tmp_path.rglob('audit/*.jsonl'))


def read(path):
    # Archive paths can pass the Windows path limit; read them the way the archive writes them.
    text = str(path.resolve())
    prefix = chr(92) * 2 + '?' + chr(92)
    if os.name == 'nt' and not text.startswith(prefix):
        text = prefix + text
    return [json.loads(line) for line in Path(text).read_text(encoding='utf-8').splitlines()]


async def test_each_run_leaves_one_audit_file_with_host_records_only(tmp_path):
    turns = [ModelTurn(tool_calls=(ToolCall('c1', 'get_current_time', {}),)), outcome('secret answer')]
    svc, _ = service(tmp_path, turns)
    async with svc:
        result = await svc.query_result('What time is it? private words', 's')
    assert result.status is TaskStatus.COMPLETED
    path, = audits(tmp_path)
    assert path.parent.parent.name.startswith('session-')            # next to the session's turns
    header, *events = read(path)
    assert header['type'] == 'audit' and header['mode'] == 'single' and header['read_only'] is False
    assert header['events'] == len(events) and header['dropped'] == 0
    assert [e['type'] for e in events] == ['task_start', 'plan', 'before_model', 'after_model', 'before_tool',
                                           'after_tool', 'before_model', 'after_model', 'step', 'task_end']
    assert events[3]['prompt_tokens'] == 290 and events[-1]['total_tokens'] == 600
    text = json.dumps(read(path), ensure_ascii=False)
    assert 'secret answer' not in text and 'private words' not in text


async def test_a_blocked_run_and_its_resume_get_one_file_each(tmp_path):
    svc, _ = service(tmp_path, [outcome('need it', 'blocked', '哪一句？'), outcome('Hello')],
                     plans=(('Ask',), ('Translate',)))
    async with svc:
        blocked = await svc.query_result('翻译', 's')
        await svc.query_result('你好', 's', resume_task_id=blocked.task_id)
    first, second = audits(tmp_path)
    assert read(first)[-1]['status'] == 'blocked' and read(second)[-1]['status'] == 'completed'


async def test_no_audit_when_turned_off_or_without_an_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'hooks_audit', False)
    svc, _ = service(tmp_path / 'off', [outcome('a')])
    async with svc:
        await svc.query_result('q', 's')
    assert audits(tmp_path) == []
    monkeypatch.setattr(config, 'hooks_audit', True)
    svc, _ = service('', [outcome('b')])
    async with svc:
        assert (await svc.query_result('q', 's')).status is TaskStatus.COMPLETED


async def test_the_cap_stops_a_conversation_and_survives_a_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'conversation_max_tokens', 300)
    svc, model = service(tmp_path, [outcome('first'), outcome('never')], plans=(('A',), ('B',)))
    async with svc:
        first = await svc.query_result('one', 's')
        second = await svc.query_result('two', 's')
    assert first.status is TaskStatus.COMPLETED and first.usage['total_tokens'] == 300
    assert second.status is TaskStatus.FAILED and second.failure_kind == 'policy'
    assert second.user_message == FAILURE_MESSAGES['policy'] and model.calls == 1
    assert 'token allowance (300 of 300 tokens)' in second.content
    # A new process reads what the archived runs used.
    svc2, model2 = service(tmp_path, [outcome('never')], plans=(('C',),))
    async with svc2:
        third = await svc2.query_result('three', 's')
    assert third.failure_kind == 'policy' and model2.calls == 0


async def test_the_cap_counts_per_conversation(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'conversation_max_tokens', 300)
    svc, _ = service(tmp_path, [outcome('a'), outcome('b')], plans=(('A',), ('B',)))
    async with svc:
        assert (await svc.query_result('one', 's1')).status is TaskStatus.COMPLETED
        assert (await svc.query_result('two', 's2')).status is TaskStatus.COMPLETED


async def test_the_cap_counts_the_current_run_too():
    cap = ConversationTokenCap(500, prior_tokens=200)
    with metering(RunMeter()) as meter:
        assert (await cap.check({'type': 'before_model'})).allow
        meter.record(SimpleNamespace(prompt_tokens=290, completion_tokens=10))
        decision = await cap.check({'type': 'before_model'})
    assert not decision.allow and '500 of 500' in decision.reason
    for bad in (0, -1, 1.5):
        with pytest.raises(ValueError):
            ConversationTokenCap(bad)


def test_a_denial_is_found_behind_wrapping_errors():
    denied = PolicyDeniedError('cap', 'over')
    try:
        try:
            raise denied
        except PolicyDeniedError as exc:
            raise RuntimeError('wrapped') from exc
    except RuntimeError as outer:
        assert _policy_denial(outer) is denied
    assert _policy_denial(RuntimeError('plain')) is None


def test_the_audit_file_name_is_checked_and_long_runs_are_bounded(tmp_path):
    archive = ConversationArchive(tmp_path, 'label')
    for bad in ('../x', 'a/b', '', 'x' * 121):
        with pytest.raises(ValueError):
            archive.save_audit(bad, [])
    hook = AuditHook(archive, max_events=3)
    for n in range(5):
        hook.observe({'type': 'after_tool', 'n': n})
    hook.observe({'type': 'task_end', 'task_id': '../../evil'})
    header, *events = read(hook.path)
    assert header['events'] == 3 and header['dropped'] == 3 and len(events) == 3
    assert hook.path.parent == archive._directory.parent / 'audit' and '..' not in hook.path.name
