"""Tests for cross-session history search (D-109)."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from app.agent.__main__ import INTERACTIVE_HELP, interactive, search_command
from app.agent.run_limits import (
    RunLimits,
    SESSION_SEARCH_MAX_QUERY_CHARS,
    SESSION_SEARCH_TURN_CHARS,
)
from app.agent.step_models import ModelTurn
from app.api.app import STATIC, create_app
from app.models.conversation_memory import (
    ConversationMessage,
    ConversationTurn,
    MessageRole,
    TurnStatus,
    search_terms,
)
from app.services.conversation_archive import ConversationArchive
from app.services.personas import DEFAULT_ID, DEFAULT_NAME
from app.services.rag_agent_service import RagAgentService
from app.tools.conversation_search_tool import (
    ConversationSearchTools,
    NOT_FOUND_TEXT,
    PAST_CONVERSATION_POLICY,
)


def _write_turn(
    service: RagAgentService,
    *,
    user_id: str | None,
    session_id: str,
    turn_id: str,
    user_text: str,
    assistant_text: str | None = None,
    status: TurnStatus = TurnStatus.COMPLETED,
    created_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> ConversationTurn:
    stamp = created_at or datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
    end = finished_at or (stamp + timedelta(seconds=5))
    root = service._archive_root(user_id)
    label = service._archive_label(user_id, session_id)
    archive = ConversationArchive(root, label)
    received = ConversationTurn(
        messages=[ConversationMessage(MessageRole.USER, user_text, stamp)],
        turn_id=turn_id,
        created_at=stamp,
        task_id=f'task-{turn_id}',
        status=TurnStatus.RECEIVED,
    )
    archive.save_turn(received)
    if status is TurnStatus.RECEIVED or assistant_text is None:
        return received
    completed = ConversationTurn(
        messages=[
            ConversationMessage(MessageRole.USER, user_text, stamp),
            ConversationMessage(MessageRole.ASSISTANT, assistant_text, end),
        ],
        turn_id=turn_id,
        created_at=stamp,
        task_id=f'task-{turn_id}',
        status=status,
        finished_at=end,
    )
    archive.complete_turn(completed)
    return completed


def _make_service(tmp_path: Path, *, personas: bool = False, longterm_memory=None, model=None, archive: bool = True):
    return RagAgentService(
        model=model or AsyncMock(),
        mcp_servers=(),
        include_knowledge=False,
        memory_directory=str(tmp_path / 'conv') if archive else '',
        personas_dir=str(tmp_path / 'personas') if personas else '',
        longterm_memory=longterm_memory,
    )


async def test_user_isolation_and_forged_archive_directory(tmp_path):
    svc = _make_service(tmp_path)
    _write_turn(svc, user_id='alice', session_id='s-a', turn_id='t1',
                user_text='部署方案是蓝绿发布', assistant_text='好的，记住蓝绿发布方案。')
    _write_turn(svc, user_id='bob', session_id='s-b', turn_id='t2',
                user_text='部署方案是金丝雀发布', assistant_text='好的，记住金丝雀发布方案。')

    # Manually place a directory with a non-matching label under user-scoped.
    root = svc._archive_root('alice')
    ConversationArchive(root, 'raw-unscoped-label').save_turn(
        ConversationTurn([ConversationMessage(MessageRole.USER, '部署方案是秘密', datetime(2026, 11, 2, tzinfo=UTC))],
                         turn_id='t3', created_at=datetime(2026, 11, 2, tzinfo=UTC))
    )

    alice_hits = await svc.search_sessions('部署方案', user_id='alice')
    assert [r['session_id'] for r in alice_hits['results']] == ['s-a']
    assert alice_hits['truncated'] is False

    bob_hits = await svc.search_sessions('部署方案', user_id='bob')
    assert [r['session_id'] for r in bob_hits['results']] == ['s-b']

    # Alice cannot read Bob's turn even if she guesses session_id and turn_id.
    assert await svc.read_session_turn('s-b', 't2', user_id='alice') is None
    assert await svc.read_session_turn('s-a', 't1', user_id='alice') is not None


async def test_cjk_bigrams_english_scoring_ordering_statuses_and_snippet(tmp_path):
    svc = _make_service(tmp_path)
    t_old = datetime(2026, 11, 1, 8, 0, tzinfo=UTC)
    t_mid = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)
    t_new = datetime(2026, 11, 3, 8, 0, tzinfo=UTC)

    _write_turn(svc, user_id='u1', session_id='s1', turn_id='t-old',
                user_text='部署方案讨论', assistant_text='A' * 300 + ' Kubernetes rollout ' + 'B' * 300,
                status=TurnStatus.COMPLETED, created_at=t_old, finished_at=t_old + timedelta(seconds=10))
    _write_turn(svc, user_id='u1', session_id='s2', turn_id='t-new',
                user_text='部署方案讨论', assistant_text='暂时失败了',
                status=TurnStatus.FAILED, created_at=t_new, finished_at=t_new + timedelta(seconds=10))
    _write_turn(svc, user_id='u1', session_id='s3', turn_id='t-block',
                user_text='数据库迁移部署方案', assistant_text='请确认备份窗口',
                status=TurnStatus.BLOCKED, created_at=t_mid, finished_at=t_mid + timedelta(seconds=10))

    # Same score: newer turn comes first.
    res = await svc.search_sessions('部署方案', user_id='u1')
    assert [r['turn_id'] for r in res['results']] == ['t-new', 't-block', 't-old']
    assert [r['status'] for r in res['results']] == ['failed', 'blocked', 'completed']
    assert all(r['persona_name'] is None for r in res['results'])

    # Higher distinct-term score beats recency.
    res2 = await svc.search_sessions('数据库 迁移 部署方案', user_id='u1')
    assert res2['results'][0]['turn_id'] == 't-block'

    # Case-insensitive English match and snippet windowing with ellipses.
    res3 = await svc.search_sessions('kubernetes', user_id='u1')
    assert [r['turn_id'] for r in res3['results']] == ['t-old']
    snippet = res3['results'][0]['snippet']
    assert 'Kubernetes' in snippet
    assert snippet.startswith('…') and snippet.endswith('…')
    assert len(snippet) <= 242

    # Single CJK character tokenization works.
    assert '猫' in search_terms('猫')


async def test_scan_limits_truncation_validation_and_corrupt_archive_logging(tmp_path, monkeypatch, caplog):
    import app.services.rag_agent_service as mod

    svc = _make_service(tmp_path)
    for i in range(4):
        _write_turn(svc, user_id='u1', session_id=f's{i}', turn_id=f't{i}',
                    user_text=f'alpha topic {i}', assistant_text=f'reply {i}',
                    created_at=datetime(2026, 11, 1 + i, tzinfo=UTC))

    # Session scan cap -> truncated=True.
    monkeypatch.setattr(mod, 'SESSION_SEARCH_MAX_SESSIONS', 2)
    capped = await svc.search_sessions('alpha', user_id='u1')
    assert capped['truncated'] is True
    assert len(capped['results']) <= 2

    # Turn scan cap -> truncated=True.
    monkeypatch.setattr(mod, 'SESSION_SEARCH_MAX_SESSIONS', 500)
    monkeypatch.setattr(mod, 'SESSION_SEARCH_MAX_TURNS', 2)
    capped_turns = await svc.search_sessions('alpha', user_id='u1')
    assert capped_turns['truncated'] is True
    assert len(capped_turns['results']) <= 2

    # Query and limit validation.
    for bad_q in ('', '   ', 'x' * (SESSION_SEARCH_MAX_QUERY_CHARS + 1), 123):
        with pytest.raises(ValueError):
            await svc.search_sessions(bad_q, user_id='u1')
    for bad_limit in (0, 21, -1, True, '5'):
        with pytest.raises(ValueError):
            await svc.search_sessions('alpha', user_id='u1', limit=bad_limit)
    for bad_offset in (-1, True, '0'):
        with pytest.raises(ValueError):
            await svc.read_session_turn('s0', 't0', user_id='u1', offset=bad_offset)

    # Punctuation-only query yields no terms -> empty result, not an error.
    assert await svc.search_sessions('!!!', user_id='u1') == {'results': [], 'truncated': False}

    # No memory_directory -> empty search and None read.
    no_archive = _make_service(tmp_path, archive=False)
    assert await no_archive.search_sessions('alpha', user_id='u1') == {'results': [], 'truncated': False}
    assert await no_archive.read_session_turn('s0', 't0', user_id='u1') is None

    # Corrupt archive is skipped and warning logs type name only (no query or session content).
    monkeypatch.setattr(mod, 'SESSION_SEARCH_MAX_TURNS', 5000)
    root = svc._archive_root('u1')
    bad_label = svc._archive_label('u1', 's-corrupt')
    bad_archive = ConversationArchive(root, bad_label)
    _write_turn(svc, user_id='u1', session_id='s-corrupt', turn_id='t-bad-1',
                user_text='secret-content-xyz', assistant_text='secret-reply-xyz')
    _write_turn(svc, user_id='u1', session_id='s-corrupt', turn_id='t-bad-2',
                user_text='valid-second-turn', assistant_text='ok')
    first_turn_file = sorted(bad_archive._directory.glob('*.json'))[0]
    first_turn_file.write_text('{corrupt', encoding='utf-8')
    with caplog.at_level(logging.WARNING, logger='app.services.rag_agent_service'):
        res = await svc.search_sessions('alpha', user_id='u1')
    assert len(res['results']) == 4
    warn_text = '\n'.join(rec.getMessage() for rec in caplog.records)
    assert 'ValueError' in warn_text
    assert 'alpha' not in warn_text
    assert 'secret-content-xyz' not in warn_text


async def test_persona_isolation_tools_policy_pagination_and_uniform_not_found(tmp_path):
    svc = _make_service(tmp_path, personas=True)
    store = svc.personas
    coder = store.create('u1', '代码助手')

    # Unbound session s-unbound; bind s-coder last so state['last'] == coder.persona_id.
    _write_turn(svc, user_id='u1', session_id='s-unbound', turn_id='t-unb',
                user_text='讨论架构选型', assistant_text='未绑定会话的回答')
    _write_turn(svc, user_id='u1', session_id='s-default', turn_id='t-def',
                user_text='讨论架构选型', assistant_text='默认人设的回答')
    store.bind('u1', 's-default', DEFAULT_ID)
    _write_turn(svc, user_id='u1', session_id='s-current', turn_id='t-cur',
                user_text='讨论架构选型', assistant_text='当前会话的回答')
    store.bind('u1', 's-current', DEFAULT_ID)
    _write_turn(svc, user_id='u1', session_id='s-coder', turn_id='t-cod',
                user_text='讨论架构选型', assistant_text='代码助手的回答')
    store.bind('u1', 's-coder', coder.persona_id)

    # bound_persona_id does NOT fall back to 'last'.
    assert store.session_persona_id('u1', 's-unbound') == coder.persona_id
    assert store.bound_persona_id('u1', 's-unbound') is None
    assert store.bound_persona_id('u1', 's-coder') == coder.persona_id

    # Default persona tools see s-unbound and s-default, NOT s-coder and NOT s-current.
    default_search, default_read = ConversationSearchTools(
        svc, user_id='u1', persona_id=DEFAULT_ID, exclude_session='s-current', max_chars=16000,
    ).tools()
    assert default_search.definition.effect == 'read'
    assert default_read.definition.effect == 'read'

    out = await default_search.invoke({'query': '架构选型'})
    assert out.success is True
    payload = json.loads(out.content)
    assert list(payload.keys())[0] == 'policy'
    assert payload['policy'] == PAST_CONVERSATION_POLICY
    assert {r['session_id'] for r in payload['results']} == {'s-unbound', 's-default'}

    # Coder persona tools see ONLY s-coder (not s-unbound!).
    coder_search, coder_read = ConversationSearchTools(
        svc, user_id='u1', persona_id=coder.persona_id, exclude_session='s-other', max_chars=16000,
    ).tools()
    coder_out = await coder_search.invoke({'query': '架构选型'})
    assert coder_out.success is True
    assert [r['session_id'] for r in json.loads(coder_out.content)['results']] == ['s-coder']

    # Model cannot override user_id or persona_id via extra arguments.
    rejected = await default_search.invoke({'query': '架构选型', 'user_id': 'bob'})
    assert rejected.success is False
    rejected2 = await default_read.invoke({'session_id': 's-coder', 'turn_id': 't-cod', 'persona_id': coder.persona_id})
    assert rejected2.success is False

    # Uniform "没有这一轮。" for wrong persona, wrong user, nonexistent session, nonexistent turn.
    _write_turn(svc, user_id='bob', session_id='s-bob', turn_id='t-bob',
                user_text='hi', assistant_text='hello')
    missing_cases = [
        await default_read.invoke({'session_id': 's-coder', 'turn_id': 't-cod'}),
        await default_read.invoke({'session_id': 's-bob', 'turn_id': 't-bob'}),
        await default_read.invoke({'session_id': 'no-such-session', 'turn_id': 't1'}),
        await default_read.invoke({'session_id': 's-default', 'turn_id': 'no-such-turn'}),
    ]
    bodies = [case.content for case in missing_cases]
    assert all(case.success for case in missing_cases)
    assert len(set(bodies)) == 1
    parsed_missing = json.loads(bodies[0])
    assert list(parsed_missing.keys())[0] == 'policy'
    assert parsed_missing['policy'] == PAST_CONVERSATION_POLICY
    assert parsed_missing['message'] == NOT_FOUND_TEXT

    # Pagination via offset/next_offset reconstructs the entire long turn.
    long_reply = ''.join(f'段落{i:04d}-' + '内容' * 20 + '\n' for i in range(200))
    _write_turn(svc, user_id='u1', session_id='s-long', turn_id='t-long',
                user_text='请详细说明', assistant_text=long_reply)
    store.bind('u1', 's-long', DEFAULT_ID)
    expected_full = f'用户：请详细说明\n\n助手：{long_reply}'
    assert len(expected_full) > SESSION_SEARCH_TURN_CHARS

    pieces = []
    offset = 0
    while True:
        page = await default_read.invoke({'session_id': 's-long', 'turn_id': 't-long', 'offset': offset})
        assert page.success is True
        body = json.loads(page.content)
        assert list(body.keys())[0] == 'policy'
        assert body['offset'] == offset
        pieces.append(body['text'])
        if body['next_offset'] is None:
            break
        offset = body['next_offset']
    assert ''.join(pieces) == expected_full
    assert len(pieces) >= 2


async def test_tool_injection_requires_archive_and_memory_option(tmp_path):
    class FakeLongTermMemory:
        async def recall(self, **kwargs):
            return []

    seen_tools: list[set[str]] = []

    async def capture_tools(messages, tools, **kwargs):
        seen_tools.append({t.name for t in tools})
        return ModelTurn(content='done')

    # 1. Archive + longterm_memory (options['memory'] True) -> both tools injected;
    # 2. Archive + use_memory=False -> tools NOT injected.
    model1 = AsyncMock()
    model1.generate.side_effect = capture_tools
    svc_on = _make_service(tmp_path / 'on', longterm_memory=FakeLongTermMemory(), model=model1)
    async with svc_on:
        await svc_on.query_result('hello', 's1', user_id='u1')
        assert {'search_conversations', 'read_conversation_turn'} <= seen_tools[-1]
        await svc_on.query_result('hello again', 's1', user_id='u1', use_memory=False)
        assert 'search_conversations' not in seen_tools[-1]
        assert 'read_conversation_turn' not in seen_tools[-1]

    # 3. No archive + memory enabled -> tools NOT injected.
    model2 = AsyncMock()
    model2.generate.side_effect = capture_tools
    svc_no_arch = _make_service(tmp_path / 'no_arch', longterm_memory=FakeLongTermMemory(), model=model2, archive=False)
    async with svc_no_arch:
        await svc_no_arch.query_result('hello', 's1', user_id='u1')
    assert 'search_conversations' not in seen_tools[-1]
    assert 'read_conversation_turn' not in seen_tools[-1]


async def test_web_search_api_auth_fields_persona_names_and_errors(tmp_path):
    svc = _make_service(tmp_path, personas=True)
    writer = svc.personas.create('local', '写作助手')
    _write_turn(svc, user_id='local', session_id='s1', turn_id='t1',
                user_text='复盘季度报告', assistant_text='好的，这是季度报告要点。')
    svc.personas.bind('local', 's1', writer.persona_id)
    _write_turn(svc, user_id='local', session_id='s2', turn_id='t2',
                user_text='复盘季度报告模板', assistant_text='默认人设下的模板。')

    app = create_app(svc, user_id='local', access_token='tok-123')
    async with svc:
        lifespan = app.router.lifespan_context(app)
        async with lifespan:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url='http://test',
                headers={'Authorization': 'Bearer tok-123'},
            ) as client:
                # Auth required.
                unauth = await client.get('/api/search', params={'q': '季度报告'}, headers={'Authorization': ''})
                assert unauth.status_code == 401

                # Web search spans all personas and includes persona_name.
                resp = await client.get('/api/search', params={'q': '季度报告', 'limit': 10})
                assert resp.status_code == 200
                body = resp.json()
                assert body['truncated'] is False
                by_session = {r['session_id']: r for r in body['results']}
                assert set(by_session) == {'s1', 's2'}
                assert by_session['s1']['persona_name'] == '写作助手'
                assert by_session['s2']['persona_name'] == DEFAULT_NAME
                for item in body['results']:
                    assert set(item) == {'session_id', 'turn_id', 'created_at', 'status', 'persona_name', 'snippet'}

                # Invalid inputs return 400.
                for params in ({'q': ''}, {'q': '   '}, {'q': 'a' * 201}, {'q': 'ok', 'limit': 0}, {'q': 'ok', 'limit': 21}):
                    bad = await client.get('/api/search', params=params)
                    assert bad.status_code == 400 and bad.json()['code'] == 'invalid_input'


async def test_cli_search_command_and_help(tmp_path):
    assert '/search' in INTERACTIVE_HELP
    svc = _make_service(tmp_path)
    _write_turn(svc, user_id='u1', session_id='s-cli', turn_id='t-cli',
                user_text='第一行关键词\n第二行补充', assistant_text='好的收到')

    lines: list[str] = []
    await search_command('/search', svc, 'u1', write=lines.append)
    assert any('用法：/search' in line for line in lines)

    lines.clear()
    await search_command('/search 关键词', svc, 'u1', write=lines.append)
    assert len(lines) == 1
    assert '  s-cli  ' in lines[0]
    assert '\n' not in lines[0]
    assert '第一行关键词' in lines[0]

    lines.clear()
    await search_command('/search 不存在的词', svc, 'u1', write=lines.append)
    assert lines == ['没有找到。']

    # Interactive dispatch handles /search directly without calling model.
    inputs = iter(['/search 关键词', '/exit'])
    out: list[str] = []

    async def fake_read(_prompt):
        return next(inputs)

    args = type('Args', (), {'session_id': 's-now', 'user_id': 'u1', 'goal': None})()
    rc = await interactive(svc, args, read_line=fake_read, write=out.append, is_tty=False)
    assert rc == 0
    assert any('s-cli' in line for line in out)


def test_run_limits_includes_session_search_group():
    rows = RunLimits.current().rows()
    search_rows = [row for row in rows if row[0] == '历史搜索']
    assert len(search_rows) >= 3
    joined = '\n'.join(f'{a} {b} {c}' for a, b, c in search_rows)
    assert '200' in joined and '500' in joined and '5000' in joined and '6000' in joined


def test_frontend_static_safety_and_node_syntax():
    js = (STATIC / 'app.js').read_text(encoding='utf-8')
    html = (STATIC / 'index.html').read_text(encoding='utf-8')
    css = (STATIC / 'app.css').read_text(encoding='utf-8')

    for forbidden in ('localStorage', 'innerHTML', 'outerHTML', 'eval('):
        assert forbidden not in js
    for name, text in (('index.html', html), ('app.js', js), ('app.css', css)):
        cleaned = text.replace("'http://www.w3.org/2000/svg'", '')
        assert 'http://' not in cleaned and 'https://' not in cleaned, name

    assert html.count('id="session-search"') == 1
    assert 'placeholder="筛选会话，回车搜索全部历史"' in html
    assert 'maxlength="200"' in html
    # Typing still filters the loaded list (the box it replaced did); Enter runs the full search.
    assert "(s.last_message || '').toLowerCase().includes(term)" in js
    assert "addEventListener('input'" in js and "event.key === 'Enter'" in js
    assert '/api/search' in js
    assert 'turn-highlight' in js and '.turn-highlight' in css

    node = shutil.which('node')
    if node is None:
        pytest.skip('node is not installed')
    result = subprocess.run([node, '--check', str(STATIC / 'app.js')], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
