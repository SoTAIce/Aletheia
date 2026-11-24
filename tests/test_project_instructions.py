"""Project instructions (D-096, docs/project_instructions_plan.md): discovery, trust,
service wiring, delegation, privacy, and CLI interaction.

Offline only: temporary directories and scripted models.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from loop_fakes import loop_reply

from app.agent import __main__ as cli
from app.agent.builtin_hooks import TraceFileHook
from app.agent.delegation import DelegationOptions, _member_context
from app.agent.hooks import HookSet
from app.agent.step_models import ModelTurn, StepExecutionContext, ToolCall
from app.services.project_instructions import (
    InstructionsChanged,
    InstructionsTrust,
    MAX_INSTRUCTION_BYTES,
    MAX_INSTRUCTION_CHARS,
    PROJECT_INSTRUCTIONS_PREAMBLE,
    TRUNCATED_NOTE,
    find_instructions,
    instructions_notice,
)
from app.services.rag_agent_service import RagAgentService
from app.services.workspace import WorkspaceCheck, WorkspacePolicy


def _requests_text(model: AsyncMock) -> list[str]:
    texts = []
    for call in model.generate.call_args_list:
        messages = call.args[0] if call.args else call.kwargs['messages']
        texts.append('\n'.join(
            f"{getattr(m, 'role', '')}:{getattr(m, 'content', str(m))}" for m in messages
        ))
    return texts


def _trust(tmp_path: Path) -> InstructionsTrust:
    return InstructionsTrust(tmp_path / 'trust' / 'instructions_confirmed.json')


# ---- 1. File discovery and safety checks --------------------------------------------------

def test_file_discovery_priority_and_root_only(tmp_path):
    ws = tmp_path / 'repo'
    ws.mkdir()
    trust = _trust(tmp_path)

    # No file -> None
    assert find_instructions(ws, (), trust) is None

    # Subdirectory file is ignored
    sub = ws / 'sub'
    sub.mkdir()
    (sub / 'AGENTS.md').write_text('sub rule', encoding='utf-8')
    assert find_instructions(ws, (), trust) is None

    # Only AGENTS.md
    (ws / 'AGENTS.md').write_text('root agents rule', encoding='utf-8')
    found = find_instructions(ws, (), trust)
    assert found is not None and found.name == 'AGENTS.md' and found.status == 'unconfirmed'
    assert found.text == 'root agents rule' and found.chars == len('root agents rule') and not found.truncated

    # Both .aletheia/instructions.md and AGENTS.md -> .aletheia/instructions.md wins
    (ws / '.aletheia').mkdir()
    (ws / '.aletheia' / 'instructions.md').write_text('aletheia rule', encoding='utf-8')
    found = find_instructions(ws, (), trust)
    assert found is not None and found.name == '.aletheia/instructions.md'
    assert found.text == 'aletheia rule'

    # Only .aletheia/instructions.md
    (ws / 'AGENTS.md').unlink()
    found = find_instructions(ws, (), trust)
    assert found is not None and found.name == '.aletheia/instructions.md'


def test_unreadable_not_file_not_utf8_too_large_and_closed(tmp_path):
    trust = _trust(tmp_path)

    # Directory named AGENTS.md -> not_file
    ws_dir = tmp_path / 'ws_dir'
    (ws_dir / 'AGENTS.md').mkdir(parents=True)
    res = find_instructions(ws_dir, (), trust)
    assert res is not None and res.status == 'unreadable' and res.reason == 'not_file'

    # Invalid UTF-8 -> not_utf8
    ws_bin = tmp_path / 'ws_bin'
    ws_bin.mkdir()
    (ws_bin / 'AGENTS.md').write_bytes(b'\xff\xfe\x00invalid')
    res = find_instructions(ws_bin, (), trust)
    assert res is not None and res.status == 'unreadable' and res.reason == 'not_utf8'

    # Oversized file -> too_large (does not read content)
    ws_big = tmp_path / 'ws_big'
    ws_big.mkdir()
    (ws_big / 'AGENTS.md').write_bytes(b'a' * (MAX_INSTRUCTION_BYTES + 1))
    res = find_instructions(ws_big, (), trust)
    assert res is not None and res.status == 'unreadable' and res.reason == 'too_large'

    # Inside closed folder -> closed
    ws_closed = tmp_path / 'ws_closed'
    (ws_closed / '.aletheia').mkdir(parents=True)
    (ws_closed / '.aletheia' / 'instructions.md').write_text('secret', encoding='utf-8')
    res = find_instructions(ws_closed, (ws_closed / '.aletheia',), trust)
    assert res is not None and res.status == 'unreadable' and res.reason == 'closed'


def test_symlink_is_refused(tmp_path):
    ws = tmp_path / 'ws'
    ws.mkdir()
    outside = tmp_path / 'outside.md'
    outside.write_text('outside rule', encoding='utf-8')
    link = ws / 'AGENTS.md'
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip('cannot create symlink here')
    res = find_instructions(ws, (), _trust(tmp_path))
    assert res is not None and res.status == 'unreadable' and res.reason == 'link'


@pytest.mark.skipif(os.name != 'nt', reason='directory junction test is Windows-only')
def test_windows_junction_is_refused(tmp_path):
    ws = tmp_path / 'ws'
    ws.mkdir()
    target = tmp_path / 'real_aletheia'
    target.mkdir()
    (target / 'instructions.md').write_text('junction rule', encoding='utf-8')
    junction = ws / '.aletheia'
    done = subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(target)], capture_output=True)
    if done.returncode != 0:
        pytest.skip('cannot create directory junction here')
    res = find_instructions(ws, (), _trust(tmp_path))
    assert res is not None and res.status == 'unreadable' and res.reason == 'link'


def test_truncation_boundary_at_8000_chars(tmp_path):
    ws = tmp_path / 'ws'
    ws.mkdir()
    trust = _trust(tmp_path)

    exact = 'x' * MAX_INSTRUCTION_CHARS
    (ws / 'AGENTS.md').write_text(exact, encoding='utf-8')
    found = find_instructions(ws, (), trust)
    assert found is not None and found.chars == MAX_INSTRUCTION_CHARS and found.truncated is False
    assert len(found.text) == MAX_INSTRUCTION_CHARS
    assert TRUNCATED_NOTE not in found.constraint_text()
    assert found.event()['truncated'] is False

    over = 'y' * (MAX_INSTRUCTION_CHARS + 1)
    (ws / 'AGENTS.md').write_text(over, encoding='utf-8')
    found2 = find_instructions(ws, (), trust)
    assert found2 is not None and found2.chars == MAX_INSTRUCTION_CHARS + 1 and found2.truncated is True
    assert len(found2.text) == MAX_INSTRUCTION_CHARS
    assert TRUNCATED_NOTE in found2.constraint_text()
    assert found2.event()['truncated'] is True


# ---- 2. Trust store -----------------------------------------------------------------------

def test_trust_confirm_change_directory_isolation_and_corrupted_file(tmp_path, caplog):
    ws1 = tmp_path / 'ws1'
    ws2 = tmp_path / 'ws2'
    ws1.mkdir()
    ws2.mkdir()
    content = 'Use pytest.'
    (ws1 / 'AGENTS.md').write_text(content, encoding='utf-8')
    (ws2 / 'AGENTS.md').write_text(content, encoding='utf-8')
    trust = _trust(tmp_path)

    first = find_instructions(ws1, (), trust)
    assert first.status == 'unconfirmed'
    trust.confirm(ws1, first.name, first.digest)
    assert find_instructions(ws1, (), trust).status == 'loaded'

    # Different directory with identical content must still be unconfirmed
    assert find_instructions(ws2, (), trust).status == 'unconfirmed'

    # Changing one character flips status to changed
    (ws1 / 'AGENTS.md').write_text(content + '!', encoding='utf-8')
    assert find_instructions(ws1, (), trust).status == 'changed'

    # Corrupted trust file is treated as empty without raising
    trust.path.write_text('{not valid json', encoding='utf-8')
    with caplog.at_level('WARNING'):
        res = find_instructions(ws1, (), trust)
    assert res.status == 'unconfirmed'
    assert any('JSONDecodeError' in rec.getMessage() for rec in caplog.records)


# ---- 3. Service wiring, constraint order, resume, delegation, privacy ---------------------

async def test_unconfirmed_loaded_changed_and_constraint_order(tmp_path):
    ws = tmp_path / 'repo'
    ws.mkdir()
    (ws / 'AGENTS.md').write_text('PROJECT_RULE_ALPHA', encoding='utf-8')
    trust = _trust(tmp_path)

    model = AsyncMock()
    model.generate.side_effect = [ModelTurn('ok1'), ModelTurn('ok2'), ModelTurn('ok3')]
    service = RagAgentService(
        model=model,
        mcp_servers=(),
        include_knowledge=False,
        memory_directory='',
        default_workspace=str(ws),
        personas_dir=str(tmp_path / 'personas'),
        instructions_trust=trust,
    )
    persona = service.personas.create(None, 'Dev', 'PERSONA_RULE_BETA')
    service.personas.bind(None, 's1', persona.persona_id)

    async with service:
        # 1. Unconfirmed: task runs normally, no constraint added, event says unconfirmed
        events1 = []
        r1 = await service.query_result('hello', 's1', on_event=events1.append)
        assert r1.content == 'ok1'
        (ev1,) = [e for e in events1 if e['type'] == 'instructions']
        assert ev1 == {'type': 'instructions', 'file': 'AGENTS.md', 'chars': len('PROJECT_RULE_ALPHA'),
                       'truncated': False, 'status': 'unconfirmed', 'reason': None}
        assert 'PROJECT_RULE_ALPHA' not in _requests_text(model)[0]

        # 2. Confirm with wrong digest -> InstructionsChanged, trust file untouched
        with pytest.raises(InstructionsChanged):
            service.confirm_instructions(None, 'deadbeef')
        assert not trust.path.exists()

        # 3. Confirm with real digest -> loaded, constraint appears AFTER persona instructions
        info = service.instructions_status()
        confirmed = service.confirm_instructions(None, info.digest)
        assert confirmed.status == 'loaded'

        events2 = []
        r2 = await service.query_result('hello again', 's1', on_event=events2.append)
        assert r2.content == 'ok2'
        (ev2,) = [e for e in events2 if e['type'] == 'instructions']
        assert ev2['status'] == 'loaded'
        req2 = _requests_text(model)[1]
        assert PROJECT_INSTRUCTIONS_PREAMBLE in req2 and 'PROJECT_RULE_ALPHA' in req2
        assert req2.index('PERSONA_RULE_BETA') < req2.index('PROJECT_RULE_ALPHA')

        # 4. Modify file -> status becomes changed, constraint not added
        (ws / 'AGENTS.md').write_text('PROJECT_RULE_MODIFIED', encoding='utf-8')
        events3 = []
        r3 = await service.query_result('third', 's1', on_event=events3.append)
        assert r3.content == 'ok3'
        (ev3,) = [e for e in events3 if e['type'] == 'instructions']
        assert ev3['status'] == 'changed'
        assert 'PROJECT_RULE_MODIFIED' not in _requests_text(model)[2]


async def test_resumed_task_does_not_reread_or_duplicate_instructions(tmp_path):
    ws = tmp_path / 'repo'
    ws.mkdir()
    (ws / 'AGENTS.md').write_text('RESUME_RULE_ONE', encoding='utf-8')
    trust = _trust(tmp_path)
    digest = hashlib.sha256(b'RESUME_RULE_ONE').hexdigest()
    trust.confirm(ws, 'AGENTS.md', digest)

    ask = ModelTurn(tool_calls=(ToolCall('ask1', 'ask_user', {'question': 'which?'}),))
    model = AsyncMock()
    model.generate.side_effect = [ask, ModelTurn('final')]
    service = RagAgentService(
        model=model, mcp_servers=(), include_knowledge=False, memory_directory='',
        default_workspace=str(ws), instructions_trust=trust,
    )
    async with service:
        blocked = await service.query_result('start', 's1')
        (ws / 'AGENTS.md').write_text('RESUME_RULE_CHANGED', encoding='utf-8')
        events = []
        done = await service.query_result('answer', 's1', resume_task_id=blocked.task_id, on_event=events.append)
    assert done.content == 'final'
    assert not [e for e in events if e['type'] == 'instructions']
    last_req = _requests_text(model)[-1]
    assert last_req.count(PROJECT_INSTRUCTIONS_PREAMBLE) == 1
    assert 'RESUME_RULE_ONE' in last_req and 'RESUME_RULE_CHANGED' not in last_req


async def test_delegated_members_receive_project_instructions(tmp_path):
    ws = tmp_path / 'repo'
    ws.mkdir()
    marker = 'MEMBER_INSTRUCTIONS_MARKER_991'
    (ws / 'AGENTS.md').write_text(marker, encoding='utf-8')
    trust = _trust(tmp_path)
    trust.confirm(ws, 'AGENTS.md', hashlib.sha256(marker.encode('utf-8')).hexdigest())

    # 1. Direct _member_context check
    from dataclasses import fields
    names = {f.name for f in fields(StepExecutionContext)}
    ctx = StepExecutionContext(**{n: None for n in names} | {'constraints': (marker,), 'active_goal': 'g'})
    assert marker in _member_context(ctx, 'subtask').constraints

    # 2. Live fake-model delegation in team mode: lead delegates to a member, member sees marker
    model = AsyncMock()
    model.generate.side_effect = [
        ModelTurn(tool_calls=(ToolCall('d1', 'delegate', {'task': 'inspect'}),)),
        loop_reply('member done'),
        loop_reply('lead done'),
    ]
    service = RagAgentService(
        model=model, mcp_servers=(), include_knowledge=False, memory_directory='',
        default_workspace=str(ws), execution_mode='team',
        team_options=DelegationOptions(allowed_tools=frozenset(), require_passing_run=False),
        instructions_trust=trust,
    )
    async with service:
        result = await service.query_result('team goal', 's1')
    assert result.content == 'lead done'
    reqs = _requests_text(model)
    assert len(reqs) >= 3
    assert marker in reqs[0]    # lead
    assert marker in reqs[1]    # delegated member


async def test_instructions_body_never_leaks(tmp_path, monkeypatch):
    from app.config import config
    monkeypatch.setattr(config, 'hooks_audit', True)

    ws = tmp_path / 'repo'
    ws.mkdir()
    secret_body = 'UNIQUE_INSTRUCTIONS_BODY_7741_DO_NOT_LEAK'
    (ws / 'AGENTS.md').write_text(secret_body, encoding='utf-8')
    trust = _trust(tmp_path)
    trust.confirm(ws, 'AGENTS.md', hashlib.sha256(secret_body.encode('utf-8')).hexdigest())

    trace_dir = tmp_path / 't'
    archive_dir = tmp_path / 'a'
    hook_events: list[dict] = []
    progress_events: list[dict] = []

    class _Recorder:
        def observe(self, event):
            hook_events.append(dict(event))

    hooks = HookSet(observers=(_Recorder(), TraceFileHook(trace_dir)))
    model = AsyncMock()
    model.generate.side_effect = [ModelTurn('ok')]
    service = RagAgentService(
        model=model, mcp_servers=(), include_knowledge=False,
        memory_directory=archive_dir, hooks=hooks,
        default_workspace=str(ws), instructions_trust=trust,
    )
    async with service:
        res = await service.query_result('check', 's1', on_event=progress_events.append)
    assert res.content == 'ok'

    # Present in model request
    assert secret_body in _requests_text(model)[0]

    # Absent from progress events (and path also absent)
    ws_str = str(ws.resolve())
    progress_json = json.dumps(progress_events, ensure_ascii=False)
    assert secret_body not in progress_json
    assert ws_str not in progress_json
    (inst_ev,) = [e for e in progress_events if e['type'] == 'instructions']
    assert 'text' not in inst_ev and inst_ev['file'] == 'AGENTS.md'

    # Absent from hook events
    assert secret_body not in json.dumps(hook_events, ensure_ascii=False, default=str)

    # Absent from trace files
    trace_files = list(trace_dir.rglob('*'))
    assert any(p.is_file() for p in trace_files)
    for p in trace_files:
        if p.is_file():
            assert secret_body not in p.read_text(encoding='utf-8', errors='replace')

    # Absent from archive & audit files (use extended path on Windows because archive paths can exceed 260 chars)
    archive_root = Path('\\\\?\\' + str(archive_dir.resolve())) if os.name == 'nt' else archive_dir
    archive_files = [p for p in archive_root.rglob('*') if p.is_file()]
    assert archive_files
    for p in archive_files:
        assert secret_body not in p.read_text(encoding='utf-8', errors='replace')


async def test_no_instructions_file_leaves_request_and_events_identical(tmp_path):
    ws = tmp_path / 'empty_repo'
    ws.mkdir()

    async def run_with(trust):
        model = AsyncMock()
        model.generate.side_effect = [ModelTurn('same')]
        events = []
        service = RagAgentService(
            model=model, mcp_servers=(), include_knowledge=False, memory_directory='',
            default_workspace=str(ws), instructions_trust=trust,
        )
        async with service:
            await service.query_result('hi', 's1', on_event=events.append)
        return _requests_text(model)[0], [e['type'] for e in events]

    import re
    req_off, ev_off = await run_with(None)
    req_on, ev_on = await run_with(_trust(tmp_path))
    def norm(text):
        text = re.sub(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', '<UUID>', text)
        return re.sub(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+\+00:00', '<TS>', text)
    assert norm(req_on) == norm(req_off)
    assert ev_on == ev_off
    assert 'instructions' not in ev_on


# ---- 4. CLI flags, notices and interactive prompt -----------------------------------------

def test_cli_parse_args_and_notices():
    args = cli.parse_args(['goal', '--trust-instructions'])
    assert args.trust_instructions is True and args.no_instructions is False
    args2 = cli.parse_args(['goal', '--no-instructions'])
    assert args2.no_instructions is True and args2.trust_instructions is False
    with pytest.raises(SystemExit):
        cli.parse_args(['goal', '--trust-instructions', '--no-instructions'])

    lines: list[str] = []
    emit = cli._notice_to(lines.append)
    emit({'type': 'instructions', 'file': 'AGENTS.md', 'chars': 120, 'truncated': False, 'status': 'loaded', 'reason': None})
    emit({'type': 'instructions', 'file': 'AGENTS.md', 'chars': 9000, 'truncated': True, 'status': 'loaded', 'reason': None})
    emit({'type': 'instructions', 'file': 'AGENTS.md', 'chars': 50, 'truncated': False, 'status': 'unconfirmed', 'reason': None})
    emit({'type': 'instructions', 'file': 'AGENTS.md', 'chars': 50, 'truncated': False, 'status': 'changed', 'reason': None})
    emit({'type': 'instructions', 'file': 'AGENTS.md', 'chars': 0, 'truncated': False, 'status': 'unreadable', 'reason': 'link'})
    assert '本次任务带了项目说明 AGENTS.md（120 字）' == lines[0]
    assert '超过 8000 字的部分没带上' in lines[1]
    assert '--trust-instructions' in lines[2]
    assert '改过了' in lines[3]
    assert '符号链接' in lines[4]
    assert instructions_notice({'type': 'instructions', 'file': 'AGENTS.md', 'status': 'unknown'}) == ''


async def test_interactive_prompt_non_tty_decline_and_view_then_trust(tmp_path):
    ws1 = tmp_path / 'ws1'
    ws2 = tmp_path / 'ws2'
    ws1.mkdir()
    ws2.mkdir()
    (ws1 / 'AGENTS.md').write_text('WS1_BODY', encoding='utf-8')
    (ws2 / 'AGENTS.md').write_text('WS2_BODY', encoding='utf-8')
    trust = _trust(tmp_path)

    model = AsyncMock()
    model.generate.side_effect = [ModelTurn('ok')]
    policy = WorkspacePolicy()
    service = RagAgentService(
        model=model, mcp_servers=(), include_knowledge=False, memory_directory='',
        default_workspace=str(ws1), workspace_servers=lambda root, writable: (),
        workspace_policy=policy, instructions_trust=trust,
    )

    # 1. Non-TTY: never prompts
    asked = []
    async def no_read(prompt):
        asked.append(prompt)
        return '/exit'
    out: list[str] = []
    await cli.prompt_instructions(service, None, declined=set(), read_line=no_read, write=out.append, interactive=False)
    assert asked == [] and out == []

    # 2. Decline ws1 -> recorded in declined set, second prompt skipped
    answers = iter(['n'])
    prompts = []
    async def read_decline(prompt):
        prompts.append(prompt)
        return next(answers)
    declined: set = set()
    await cli.prompt_instructions(service, None, declined=declined, read_line=read_decline, write=out.append, interactive=True)
    assert len(prompts) == 1 and service.instructions_status().status == 'unconfirmed'
    await cli.prompt_instructions(service, None, declined=declined, read_line=read_decline, write=out.append, interactive=True)
    assert len(prompts) == 1   # not asked again in this run

    # 3. /dir to ws2 -> 'v' prints body, then 'y' trusts ws2
    dir_answers = iter(['v', 'y'])
    dir_out: list[str] = []
    async def read_dir(prompt):
        return next(dir_answers)
    state: dict = {}
    monkey_check = WorkspaceCheck(str(ws2.resolve()), False)
    service.check_workspace = lambda raw: monkey_check
    await cli.dir_command(f'/dir {ws2}', state, service, read_dir, write=dir_out.append, declined=declined, is_tty=True)
    assert any('WS2_BODY' in line for line in dir_out)
    assert service.instructions_status(str(ws2)).status == 'loaded'


async def test_cli_main_trust_and_no_instructions_flags(tmp_path, monkeypatch):
    ws = tmp_path / 'ws'
    ws.mkdir()
    (ws / 'AGENTS.md').write_text('CLI_FLAG_RULE', encoding='utf-8')
    trust_path = tmp_path / 'trust.json'

    captured: list[dict] = []

    class _FakeService:
        def __init__(self, **kwargs):
            self._trust = kwargs.get('instructions_trust')
            self._ws = kwargs.get('default_workspace')
            captured.append({'trust': self._trust, 'ws': self._ws})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        def instructions_status(self, workspace=None):
            if self._trust is None:
                return None
            return find_instructions(workspace or self._ws, (), self._trust)

        def confirm_instructions(self, workspace, digest):
            found = self.instructions_status(workspace)
            self._trust.confirm(workspace or self._ws, found.name, found.digest)
            return found

        async def query_result(self, goal, session_id, **kwargs):
            from app.agent.runtime import AgentRunResult
            from app.models.taskstate import TaskStatus
            return AgentRunResult(task_id='t1', status=TaskStatus.COMPLETED, content='ok',
                                  steps=(), execution_count=1, replan_count=0)

    monkeypatch.setattr(cli, 'RagAgentService', _FakeService)
    monkeypatch.setattr('app.services.project_instructions.DEFAULT_TRUST_PATH', trust_path)
    monkeypatch.setenv('SystemDrive', 'Q:')

    # --no-instructions passes instructions_trust=None
    args_no = cli.parse_args(['hi', '--filesystem-root', str(ws), '--no-instructions', '--no-knowledge'])
    assert await cli.main(args_no) == 0
    assert captured[-1]['trust'] is None
    assert not trust_path.exists()

    # --trust-instructions confirms the startup workspace's file
    args_trust = cli.parse_args(['hi', '--filesystem-root', str(ws), '--trust-instructions', '--no-knowledge'])
    assert await cli.main(args_trust) == 0
    assert captured[-1]['trust'] is not None
    assert InstructionsTrust(trust_path).status_of(
        ws, 'AGENTS.md', hashlib.sha256(b'CLI_FLAG_RULE').hexdigest()) == 'loaded'
