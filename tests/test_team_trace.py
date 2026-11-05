"""Deterministic tests for the safe team trace (team-diagnosis-001)."""

import json
import logging
from contextlib import asynccontextmanager

import pytest

from app.agent.team.coordinator import TeamCoordinator
from app.agent.team.models import TeamEnvironment, TeamError, TeamOptions, TeamStore
from app.agent.tool_calling_model import ModelToolArgumentsError
from app.tools.tool_registry import ToolRegistry
from bench import run_bench
from test_sandbox_step1 import _context
from test_sandbox_step3 import FakeTool, Scripted, act, call
from test_tool_argument_repair import Scripted as RaisingScripted

SECRET = 'SECRET-73f'


def _tools(*tools):
    @asynccontextmanager
    async def factory(memory, manager):
        registry = ToolRegistry()
        for tool in tools:
            registry.register(tool)
        yield registry
    return factory


def _trace(caplog):
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith('Team trace: ')]
    assert lines, 'no trace logged'
    text = lines[-1][len('Team trace: '):]
    assert SECRET not in text
    return json.loads(text)


async def test_trace_records_actions_outcomes_and_counters_without_model_text(caplog):
    caplog.set_level(logging.INFO, logger='app.agent.team.coordinator')
    write = FakeTool('write_file', 'written ' + SECRET)
    model = Scripted(
        act('l1', 'create_worker', 'coder', instruction='do ' + SECRET, tools='write_file'),
        act('l2', 'run_member', 'coder'),
        call('w1', 'write_file', path='a.py', content=SECRET),
        call('w2', 'submit_result', payload={'answer': SECRET}),
        act('l3', 'finish', 'coder', version=1),                       # rejected: no review yet
        act('l4', 'create_reviewer', 'checker', instruction=SECRET),
        act('l5', 'review', 'checker', 'coder', instruction=SECRET, version=1),
        call('r1', 'submit_result', payload={'candidate_version': 1, 'decision': 'approve', 'feedback': SECRET}),
        act('l6', 'finish', 'coder', version=1))
    ctx = _context()
    options = TeamOptions(allowed_tools=TeamOptions().allowed_tools | {'write_file'})
    state = TeamStore().get(ctx, options)
    await TeamCoordinator(state, model, TeamEnvironment('u', _tools(write), lambda m: {}), ctx).run()
    trace = _trace(caplog)
    assert trace['status'] == 'completed' and trace['failure'] is None
    assert [a['action'] for a in trace['actions']] == [
        'create_worker', 'run_member', 'finish', 'create_reviewer', 'review', 'finish']
    assert trace['actions'][2]['event'] == 'command_rejected'
    assert 'independent current approval' in trace['actions'][2]['reason']
    assert trace['actions'][4] == {'action': 'review', 'member': 'checker', 'target': 'coder',
                                   'event': 'review_completed', 'decision': 'approve'}
    coder = trace['sessions']['coder']
    assert coder['role'] == 'worker' and coder['tools'] == ['write_file']
    assert coder['tool_sequence'][0] == ['write_file', True]
    assert trace['sessions']['lead']['model_calls'] == 6


async def test_trace_pinpoints_session_that_exhausted_its_repairs(caplog):
    caplog.set_level(logging.INFO, logger='app.agent.team.coordinator')
    bad = ModelToolArgumentsError('invalid_json', tool_name='write_file', argument_chars=9000)
    model = RaisingScripted(
        act('l1', 'create_worker', 'coder', tools='write_file'),
        act('l2', 'run_member', 'coder'),
        bad, bad, bad,                                              # 2 repairs, then the member fails
        act('l3', 'fail', instruction='give up'))
    ctx = _context()
    options = TeamOptions(allowed_tools=TeamOptions().allowed_tools | {'write_file'})
    state = TeamStore().get(ctx, options)
    with pytest.raises(TeamError, match='give up'):
        await TeamCoordinator(state, model, TeamEnvironment('u', _tools(FakeTool('write_file', 'ok')),
                                                            lambda m: {}), ctx).run()
    trace = _trace(caplog)
    assert trace['status'] == 'failed' and trace['failure'] == 'TeamError'
    assert trace['actions'][1] == {'action': 'run_member', 'member': 'coder', 'target': '',
                                   'event': 'member_failed', 'failure': 'ModelToolArgumentsError'}
    assert trace['actions'][2] == {'action': 'fail', 'member': '', 'target': '', 'event': 'raised', 'type': 'TeamError'}
    assert trace['budget']['member_failures'] == 1 and trace['budget']['format_repairs'] == 2
    coder = trace['sessions']['coder']
    assert (coder['format_repairs'], coder['argument_repairs'], coder['max_format_repairs']) == (2, 2, 2)
    assert coder['event_counts']['tool_arguments_rejected'] == 3
    assert 'ModelToolArgumentsError' in coder['failure']
    assert coder['argument_rejections'] == [{'tool': 'write_file', 'reason': 'invalid_json', 'chars': 9000}] * 3


def test_failure_types_descend_nested_groups():
    from app.agent.team.trace import _failure_types
    nested = {'type': 'ExceptionGroup', 'exceptions': [
        {'type': 'ExceptionGroup', 'exceptions': [
            {'type': 'ModelToolArgumentsError', 'cause': {'type': 'JSONDecodeError'}}]}]}
    assert _failure_types(nested) == 'ExceptionGroup -> ModelToolArgumentsError -> JSONDecodeError'
    assert _failure_types(None) is None and _failure_types({'x': 1}) is None


def test_bench_row_carries_trace(monkeypatch):
    from types import SimpleNamespace
    trace = {'status': 'failed', 'sessions': {}}
    stderr = 'noise\nTeam trace: ' + json.dumps(trace) + '\n'
    monkeypatch.setattr(run_bench, 'launch_agent', lambda cmd, env, timeout: SimpleNamespace(
        stdout='TeamError: team execution failed: ExceptionGroup\n\nStatus: failed; executions: 1; replans: 0\n',
        stderr=stderr))
    task = next(t for t in run_bench.load_tasks(['all']) if t['id'] == 'parse_duration')
    row = run_bench.run_one(task, 'team', 1, 5)
    assert row['team_trace'] == trace and row['failure'].startswith('TeamError')


def test_failed_run_keeps_cli_failure_even_when_a_file_was_written(monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace

    def fake(cmd, env, timeout):
        root = Path(cmd[cmd.index('--filesystem-root') + 1])
        (root / 'parse_duration.py').write_text('def parse_duration(text):\n    return 0\n', encoding='utf-8')
        return SimpleNamespace(stdout='TeamError: team member limit reached\n\nStatus: failed; executions: 1; replans: 0\n',
                               stderr='')
    monkeypatch.setattr(run_bench, 'launch_agent', fake)
    task = next(t for t in run_bench.load_tasks(['all']) if t['id'] == 'parse_duration')
    row = run_bench.run_one(task, 'team', 1, 5, sandbox=True)
    assert row['deliverable_written'] is True
    assert row['failure'] == 'TeamError: team member limit reached'


def test_raised_outcome_keeps_only_fixed_host_texts():
    from app.agent.team.trace import raised_outcome
    gate = 'member tool permission: write and execution tools are granted to workers only'
    assert raised_outcome(TeamError(gate)) == {'event': 'raised', 'type': 'TeamError', 'reason': gate}
    assert raised_outcome(TeamError('team member failure limit reached: X'))['reason'] == \
        'team member failure limit reached'
    assert raised_outcome(TeamError(SECRET)) == {'event': 'raised', 'type': 'TeamError'}


async def test_trace_names_the_create_gate_that_ended_the_team(caplog):
    caplog.set_level(logging.INFO, logger='app.agent.team.coordinator')
    model = Scripted(act('l1', 'create_worker', 'w', instruction=SECRET, tools='run_tests'))
    ctx = _context()
    state = TeamStore().get(ctx, TeamOptions())                     # run_tests not on the allowlist
    with pytest.raises(TeamError):
        await TeamCoordinator(state, model, TeamEnvironment('u', _tools(), lambda m: {}), ctx).run()
    trace = _trace(caplog)
    assert trace['actions'][0]['reason'] == 'member tool permission exceeds host allowlist'


async def test_reviewer_tool_gate_is_a_recoverable_rejection(caplog):
    caplog.set_level(logging.INFO, logger='app.agent.team.coordinator')
    model = Scripted(act('l1', 'create_reviewer', 'checker', instruction=SECRET, tools='run_tests'),
                     act('l2', 'create_reviewer', 'checker', instruction=SECRET, tools='read_file'),
                     act('l3', 'fail', instruction=SECRET))
    ctx = _context()
    options = TeamOptions(allowed_tools=TeamOptions().allowed_tools | {'run_tests'})
    state = TeamStore().get(ctx, options)
    with pytest.raises(TeamError):
        await TeamCoordinator(state, model, TeamEnvironment('u', _tools(), lambda m: {}), ctx).run()
    trace = _trace(caplog)
    assert trace['actions'][0]['event'] == 'command_rejected'
    assert 'granted to workers only' in trace['actions'][0]['reason']
    assert trace['actions'][1]['event'] == 'member_created'
    assert trace['sessions']['checker']['tools'] == ['read_file']


def test_timeout_keeps_partial_trace_and_team_gets_planning_headroom(monkeypatch):
    import subprocess
    seen = {}
    trace = {'status': 'failed', 'sessions': {}}

    def fake(cmd, env, timeout):
        seen['timeout'] = timeout
        raise subprocess.TimeoutExpired(cmd, timeout, stderr=('Team trace: ' + json.dumps(trace) + '\n').encode())
    monkeypatch.setattr(run_bench, 'launch_agent', fake)
    task = next(t for t in run_bench.load_tasks(['all']) if t['id'] == 'parse_duration')
    row = run_bench.run_one(task, 'team', 1, 600, sandbox=True, team_timeout=400)
    assert seen['timeout'] == 700
    assert row['status'] == 'timeout' and row['team_trace'] == trace
    run_bench.run_one(task, 'single', 1, 600, sandbox=True, team_timeout=400)
    assert seen['timeout'] == 600


def test_sandbox_team_budgets_are_raised_only_with_sandbox():
    from app.agent.__main__ import team_options_for
    sandbox = team_options_for('team', 'w', True, 400)
    assert (sandbox.max_tool_calls, sandbox.max_model_calls, sandbox.max_estimated_tokens) == (96, 120, 1_500_000)
    plain = team_options_for('team', 'w')
    assert (plain.max_tool_calls, plain.max_model_calls) == (TeamOptions().max_tool_calls, TeamOptions().max_model_calls)


def test_timeout_still_grades_written_file_as_diagnostic_and_enables_traces(monkeypatch):
    import subprocess
    from pathlib import Path
    seen = {}

    def fake(cmd, env, timeout):
        seen['env'] = env.get('ALETHEIA_TEAM_TRACE')
        root = Path(cmd[cmd.index('--filesystem-root') + 1])
        (root / 'parse_duration.py').write_text('def parse_duration(text):\n    return 0\n', encoding='utf-8')
        ok = 'Team trace: ' + json.dumps({'status': 'completed'})
        raise subprocess.TimeoutExpired(cmd, timeout, stderr=ok + '\n' + ok.replace('completed', 'running') + '\n')
    monkeypatch.setattr(run_bench, 'launch_agent', fake)
    task = next(t for t in run_bench.load_tasks(['all']) if t['id'] == 'parse_duration')
    row = run_bench.run_one(task, 'team', 1, 5, sandbox=True)
    assert seen['env'] == '1' and row['passed'] is False and row['status'] == 'timeout'
    assert row['file_checks_total'] == 21 and row['file_checks_passed'] < 21
    assert [t['status'] for t in row['team_traces']] == ['completed', 'running']


async def test_successful_trace_is_loud_only_when_requested(caplog, monkeypatch):
    async def run_once():
        model = Scripted(
            act('l1', 'create_worker', 'coder'),
            act('l2', 'run_member', 'coder'),
            call('w1', 'submit_result', payload={'answer': 'done'}),
            act('l3', 'create_reviewer', 'checker'),
            act('l4', 'review', 'checker', 'coder', version=1),
            call('r1', 'submit_result', payload={'candidate_version': 1, 'decision': 'approve', 'feedback': 'ok'}),
            act('l5', 'finish', 'coder', version=1))
        ctx = _context()
        state = TeamStore().get(ctx, TeamOptions())
        await TeamCoordinator(state, model, TeamEnvironment('u', _tools(), lambda m: {}), ctx).run()
    caplog.set_level(logging.INFO, logger='app.agent.team.coordinator')
    monkeypatch.delenv('ALETHEIA_TEAM_TRACE', raising=False)
    await run_once()
    monkeypatch.setenv('ALETHEIA_TEAM_TRACE', '1')
    await run_once()
    levels = [r.levelname for r in caplog.records if r.getMessage().startswith('Team trace: ')]
    assert levels == ['INFO', 'WARNING']
