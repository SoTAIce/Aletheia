"""Deterministic tests for sandbox step 1: restricted write (sandbox-step1-001).

Covers the team write toggle, the worker-only tool gate and file-based benchmark
delivery. No real model, MCP, Docker or network.
"""

import json
from types import SimpleNamespace

import pytest

from app.agent.__main__ import team_options_for
from app.agent.step_models import ModelTurn, ToolCall
from app.agent.team.coordinator import TeamCoordinator
from app.agent.team.models import TeamEnvironment, TeamError, TeamOptions, TeamStore, WORKER_ONLY_TOOLS
from app.models.taskstate import TaskState
from app.models.working_memory import WorkingMemory
from app.agent.step_executor import StepExecutor
from bench import run_bench
from test_bench_graders import FIXED_INVENTORY


def test_team_write_toggle_only_with_write_root():
    assert team_options_for('single', 'D:/w') is None
    assert team_options_for('team', None) is None
    options = team_options_for('team', 'D:/w')
    assert options.allowed_tools == TeamOptions().allowed_tools | {'write_file'}
    assert 'write_file' not in TeamOptions().allowed_tools
    assert WORKER_ONLY_TOOLS == {'write_file', 'run_tests', 'run_python'}


class Lead:
    def __init__(self, *turns):
        self.turns, self.requests = list(turns), []

    async def generate(self, messages, tools):
        self.requests.append(tuple(messages))
        return self.turns.pop(0)


def _command(action, member, tools):
    payload = {'action': action, 'member_id': member, 'target_id': '', 'instruction': 'do it',
               'tools': tools, 'expected_version': 0}
    return ModelTurn(tool_calls=(ToolCall('c1', 'submit_result', {'payload': payload}),))


def _context():
    from unittest.mock import AsyncMock
    wm = WorkingMemory(TaskState('u', 'goal'))
    wm.task_state.start_planning()
    wm.install_plan(['step'])
    step_id = wm.task_state.plan[0].step_id
    wm.begin_step(step_id)
    return StepExecutor(wm, AsyncMock()).build_context(step_id)


def _text(messages):
    return '\n'.join(m.content or '' for m in messages)


async def _run(options, *turns):
    ctx = _context()
    state = TeamStore().get(ctx, options)
    lead = Lead(*turns)
    coordinator = TeamCoordinator(state, lead, TeamEnvironment('u', None, None), ctx)
    with pytest.raises(TeamError, match='permission') as caught:
        await coordinator.run()
    return state, lead, caught.value


async def test_reviewer_cannot_receive_write_tool():
    options = TeamOptions(allowed_tools=TeamOptions().allowed_tools | {'write_file'})
    state, lead, error = await _run(options, _command('create_reviewer', 'checker', 'read_file,write_file'))
    assert 'workers only' in str(error) and state.members == {}
    assert 'Workers only (never reviewers): ["write_file"]' in _text(lead.requests[0])


async def test_worker_can_receive_write_tool_and_default_prompt_is_unchanged():
    options = TeamOptions(allowed_tools=TeamOptions().allowed_tools | {'write_file'})
    ctx = _context()
    state = TeamStore().get(ctx, options)
    lead = Lead(_command('create_worker', 'coder', 'read_file,write_file'))
    coordinator = TeamCoordinator(state, lead, TeamEnvironment('u', None, None), ctx)
    with pytest.raises(Exception):
        await coordinator.run()  # the scripted Lead runs out of turns after creating the worker
    assert state.members['coder'].tools == {'read_file', 'write_file'}
    _, default_lead, _ = await _run(TeamOptions(), _command('create_worker', 'coder', 'write_file'))
    assert 'Workers only' not in _text(default_lead.requests[0])


def _fake_agent(writes=None, stdout='已写入 inventory.py\n\nStatus: completed; executions: 2; replans: 0\n'):
    calls = []

    def fake_run(cmd, env, timeout):
        calls.append(cmd)
        if writes is not None and '--write-root' in cmd:
            root = cmd[cmd.index('--write-root') + 1]
            name, content = writes
            with open(f'{root}/{name}', 'w', encoding='utf-8') as handle:
                handle.write(content)
        return SimpleNamespace(stdout=stdout, stderr='')
    return fake_run, calls


def _task(task_id):
    return next(t for t in run_bench.load_tasks(['all']) if t['id'] == task_id)


def test_write_mode_grades_the_written_file(monkeypatch):
    fake, calls = _fake_agent(('inventory.py', FIXED_INVENTORY))
    monkeypatch.setattr(run_bench, 'launch_agent', fake)
    row = run_bench.run_one(_task('fix_inventory'), 'single', 1, 5, write=True)
    assert row['passed'] is True and row['deliverable_written'] is True and row['delivery'] == 'file'
    cmd = calls[0]
    assert cmd[cmd.index('--write-root') + 1] == cmd[cmd.index('--filesystem-root') + 1]
    assert 'write_file' in cmd[cmd.index('app.agent') + 1]


def test_write_mode_unchanged_or_missing_file_fails(monkeypatch):
    fake, _ = _fake_agent()
    monkeypatch.setattr(run_bench, 'launch_agent', fake)
    row = run_bench.run_one(_task('fix_inventory'), 'single', 1, 5, write=True)
    assert row['passed'] is False and row['deliverable_written'] is False   # shipped buggy file untouched
    row = run_bench.run_one(_task('parse_duration'), 'single', 1, 5, write=True)
    assert row == {**row, 'passed': False, 'failure': 'deliverable_missing'}


def test_answer_mode_has_no_write_root(monkeypatch):
    fake, calls = _fake_agent(stdout='x = 1\n\nStatus: completed; executions: 1; replans: 0\n')
    monkeypatch.setattr(run_bench, 'launch_agent', fake)
    row = run_bench.run_one(_task('parse_duration'), 'single', 1, 5)
    assert '--write-root' not in calls[0] and 'delivery' not in row and row['passed'] is False
