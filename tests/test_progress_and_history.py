"""Progress callbacks and read-only session history for the web layer (D-045)."""

import json
from unittest.mock import AsyncMock

import pytest

from app.agent.step_models import ModelTurn
from app.models.taskstate import TaskStatus
from app.services.conversation_archive import ConversationArchive
from app.services.rag_agent_service import RagAgentService


def _outcome(text, status='succeeded', reason=None):
    return ModelTurn(json.dumps({'status': status, 'content': text, 'reason': reason}))


def _service(tmp_path, plans, turns, **kw):
    planner, model = AsyncMock(), AsyncMock()
    planner.generate.side_effect = [json.dumps({'steps': steps, 'reason': None}) for steps in plans]
    model.generate.side_effect = turns
    return RagAgentService(model=model, planner_model=planner, mcp_servers=(), include_knowledge=False,
                           memory_directory=tmp_path, **kw)


async def test_progress_reports_route_plan_and_each_step_without_model_text(tmp_path):
    events = []
    async with _service(tmp_path, [['Look it up', 'Answer']], [_outcome('secret draft'), _outcome('Paris')]) as service:
        result = await service.query_result('Capital of France?', 's', on_event=events.append)
    assert result.status is TaskStatus.COMPLETED and result.content == 'Paris'
    assert [e['type'] for e in events] == ['route', 'plan', 'step', 'step']
    assert events[0] == {'type': 'route', 'mode': 'single', 'read_only': False,
                         'options': {'mode': 'single', 'write': True, 'memory': False}}     # D-051
    assert events[1]['revision'] == 1 and events[1]['steps'] == ['Look it up', 'Answer']
    assert [e['step_id'] for e in events[2:]] == events[1]['step_ids']      # the page matches steps to the plan
    assert [(e['step'], e['outcome'], e['tools']) for e in events[2:]] == [(1, 'succeeded', []), (2, 'succeeded', [])]
    assert 'secret draft' not in json.dumps(events) and 'Paris' not in json.dumps(events)
    assert [{k: v for k, v in e.items() if k != 'type'} for e in events[2:]] == [dict(s) for s in result.step_log]


async def test_a_blocked_step_is_reported_and_the_resumed_run_reports_again(tmp_path):
    events = []
    async with _service(tmp_path, [['Ask'], ['Translate']],
                        [_outcome('need it', 'blocked', '哪一句？'), _outcome('Hello')]) as service:
        blocked = await service.query_result('翻译', 's', on_event=events.append)
        assert [e.get('outcome') for e in events if e['type'] == 'step'] == ['blocked']
        events.clear()
        done = await service.query_result('你好', 's', resume_task_id=blocked.task_id, on_event=events.append)
    assert done.content == 'Hello'
    assert [e['type'] for e in events] == ['route', 'plan', 'step']


async def test_a_failing_callback_never_changes_the_result(tmp_path):
    def broken(event):
        raise RuntimeError('display is gone')
    async with _service(tmp_path, [['Answer']], [_outcome('42')]) as service:
        result = await service.query_result('What?', 's', on_event=broken)
    assert result.status is TaskStatus.COMPLETED and result.content == '42'


async def test_sessions_and_turns_are_listed_per_user_newest_first(tmp_path):
    async with _service(tmp_path, [['A'], ['B'], ['C']],
                        [_outcome('one'), _outcome('two'), _outcome('three')]) as service:
        await service.query_result('first question', 'alpha', user_id='alice')
        await service.query_result('second question', 'beta', user_id='alice')
        await service.query_result('bob question', 'alpha', user_id='bob')
        sessions = await service.list_sessions(user_id='alice')
        assert [s['session_id'] for s in sessions] == ['beta', 'alpha']
        assert sessions[0]['last_message'] == 'second question'
        assert [s['session_id'] for s in await service.list_sessions(user_id='bob')] == ['alpha']
        assert await service.list_sessions(user_id='carol') == []
        turns = await service.session_turns('alpha', user_id='alice')
    assert [(t['user'], t['reply'], t['status'], t['resumable']) for t in turns] == [
        ('first question', 'one', 'completed', False)]
    assert set(turns[0]) == {'turn_id', 'task_id', 'status', 'question_id', 'resumable',
                             'created_at', 'finished_at', 'user', 'reply'}


async def test_a_blocked_turn_is_resumable_only_while_its_task_lives(tmp_path):
    async with _service(tmp_path, [['Ask']], [_outcome('need it', 'blocked', '哪一句？')]) as service:
        blocked = await service.query_result('翻译', 's', user_id='alice')
        [turn] = await service.session_turns('s', user_id='alice')
        assert (turn['status'], turn['task_id'], turn['reply'], turn['resumable']) == (
            'blocked', blocked.task_id, '哪一句？', True)
    async with _service(tmp_path, [], []) as restarted:
        [turn] = await restarted.session_turns('s', user_id='alice')
        assert turn['status'] == 'blocked' and turn['resumable'] is False


async def test_an_unknown_session_has_no_turns_and_creates_nothing(tmp_path):
    async with _service(tmp_path, [], []) as service:
        assert await service.session_turns('never', user_id='alice') == []
        assert await service.list_sessions(user_id='alice') == []
    assert list(tmp_path.iterdir()) == []


async def test_history_without_an_archive_comes_from_memory(tmp_path):
    async with _service('', [['A']], [_outcome('one')]) as service:
        await service.query_result('in memory', 's', user_id='alice')
        assert [s['session_id'] for s in await service.list_sessions(user_id='alice')] == ['s']
        assert [t['reply'] for t in await service.session_turns('s', user_id='alice')] == ['one']
        assert await service.list_sessions(user_id='bob') == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('limit', [0, 201, '5', True])
async def test_history_limits_are_checked(tmp_path, limit):
    async with _service(tmp_path, [], []) as service:
        with pytest.raises(ValueError):
            await service.list_sessions(limit=limit)
        with pytest.raises(ValueError):
            await service.session_turns('s', limit=limit)


async def test_archive_listing_skips_directories_whose_label_does_not_match(tmp_path):
    async with _service(tmp_path, [['A']], [_outcome('one')]) as service:
        await service.query_result('real', 'real-session')
    forged = tmp_path / 'session-forged' / 'turns'      # claims another session's label
    forged.mkdir(parents=True)
    (forged / 'x.json').write_text(json.dumps({'session_id': 'real-session'}), encoding='utf-8')
    (tmp_path / 'session-empty' / 'turns').mkdir(parents=True)
    (tmp_path / 'session-broken' / 'turns').mkdir(parents=True)
    (tmp_path / 'session-broken' / 'turns' / 'x.json').write_text('not json', encoding='utf-8')
    assert ConversationArchive.session_ids(tmp_path) == ['real-session']
