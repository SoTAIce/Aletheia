"""Deterministic tests for team traces in the bench and failure type chains (team-diagnosis-001).

The V1 coordinator trace tests were removed with V1 (DECISIONS D-025); Team V2 traces are covered
in test_team_v2.py.
"""

import json

from bench import run_bench


def test_failure_types_descend_nested_groups():
    from app.agent.team.diagnostics import failure_types
    nested = {'type': 'ExceptionGroup', 'exceptions': [
        {'type': 'ExceptionGroup', 'exceptions': [
            {'type': 'ModelToolArgumentsError', 'cause': {'type': 'JSONDecodeError'}}]}]}
    assert failure_types(nested) == 'ExceptionGroup -> ModelToolArgumentsError -> JSONDecodeError'


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
