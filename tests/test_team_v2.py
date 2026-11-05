"""Deterministic tests for Team V2, the delegating team (team-v2-001, DECISIONS D-019)."""

import json
import logging
import re

import pytest

from app.agent.step_models import ModelTurn, StepOutcome, ToolMessage
from app.agent.team.models import TeamError
from app.agent.team_v2 import DelegatingTeamRunner, DelegationOptions, READ_TOOLS
from app.agent.tool_calling_model import ModelToolArgumentsError
from app.tools.tool_registry import ToolRegistry
from test_sandbox_step1 import _context
from test_sandbox_step3 import FakeTool, call
from test_tool_argument_repair import Scripted

SANDBOX = DelegationOptions(allowed_tools=READ_TOOLS | {'write_file', 'run_tests', 'run_python'},
                            require_passing_run=True)
BAD = ModelToolArgumentsError('invalid_json', tool_name='write_file', argument_chars=9000)


def final(content='done', status='succeeded', reason=None):
    return ModelTurn(content=json.dumps({'status': status, 'content': content, 'reason': reason}))


def delegate(identity, task='write parse_duration.py with tests', **extra):
    return call(identity, 'delegate', task=task, **extra)


class Seq(FakeTool):
    """Fake tool answering from a list of contents in order."""

    def __init__(self, name, *contents):
        super().__init__(name, contents[0])
        self.contents = list(contents)

    async def invoke(self, arguments):
        self.content = self.contents.pop(0) if len(self.contents) > 1 else self.contents[0]
        return await super().invoke(arguments)


def _registry(*tools):
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


def _sandbox_tools(*runs):
    return (FakeTool('read_file', 'x = 1'), FakeTool('write_file', 'File written successfully.'),
            Seq('run_tests', *(runs or ('exit code 0; 1.0s\n3 passed',))))


def _texts(request):
    return '\n'.join(getattr(m, 'content', '') or '' for m in request)


def _reports(model):
    """Host delegation reports the Lead received, decoded (its last request has them all)."""
    out = []
    lead = [r for r in model.requests if 'Team Lead' in (getattr(r[0], 'content', '') or '')][-1]
    for message in lead:
        if isinstance(message, ToolMessage) and message.content.startswith('{'):
            out.append(json.loads(message.content))
    return out


async def test_host_completes_when_a_worker_passes_after_its_last_write():
    model = Scripted(
        delegate('l1'),
        call('w1', 'write_file', path='parse_duration.py', content='code'),
        call('w2', 'run_tests', path='test_parse_duration.py'),
        final('implemented and tested'))
    runner = DelegatingTeamRunner(model, _registry(*_sandbox_tools()), SANDBOX)
    output = await runner.run(_context())
    assert output.outcome is StepOutcome.SUCCEEDED and output.content == 'implemented and tested'
    assert not model.items                                   # no extra Lead call after the gate passed
    assert [c.call_id for c in output.tool_calls] == ['worker-1/w1', 'worker-1/w2']
    assert [r.call_id for r in output.tool_results] == ['worker-1/w1', 'worker-1/w2']
    worker_first = model.requests[1]
    assert 'worker delegated by the team lead' in _texts(worker_first)
    assert 'Team Lead' not in _texts(worker_first)            # fresh worker context, not the Lead's


async def test_every_delegation_starts_fresh_and_rework_follows_the_report():
    model = Scripted(
        delegate('l1'),
        call('w1', 'write_file', path='parse_duration.py', content='FIRST-ATTEMPT-BODY'),
        call('w2', 'run_tests', path='t.py'),
        final('wrote it'),
        delegate('l2', task='read parse_duration.py and fix the failing test'),
        call('v1', 'write_file', path='parse_duration.py', content='fixed'),
        call('v2', 'run_tests', path='t.py'),
        final('fixed'))
    tools = _sandbox_tools('exit code 1; 1.0s\n1 failed', 'exit code 0; 1.0s\n3 passed')
    output = await DelegatingTeamRunner(model, _registry(*tools), SANDBOX).run(_context())
    assert output.content == 'fixed'
    second_worker = model.requests[5]
    assert 'FIRST-ATTEMPT-BODY' not in _texts(second_worker)  # earlier worker history is not carried over
    report = json.loads(next(m.content for m in model.requests[4] if isinstance(m, ToolMessage)))
    assert report['passing_run_after_last_write'] is False and report['files_written'] == ['parse_duration.py']
    assert report['last_run'] == {'tool': 'run_tests', 'summary': 'exit code 1; 1.0s'}
    assert report['delegations_remaining'] == SANDBOX.max_delegations - 1


async def test_write_after_the_passing_run_does_not_complete():
    model = Scripted(
        delegate('l1'),
        call('w1', 'run_tests', path='t.py'),
        call('w2', 'write_file', path='parse_duration.py', content='late'),
        final('done'),
        ModelTurn(content='FAILED: could not verify'))
    with pytest.raises(TeamError, match='lead reported failure'):
        await DelegatingTeamRunner(model, _registry(*_sandbox_tools()), SANDBOX).run(_context())
    assert _reports(model)[0]['passing_run_after_last_write'] is False


async def test_lead_final_answer_without_the_gate_is_rejected_once_then_fails():
    model = Scripted(ModelTurn(content='All done!'), ModelTurn(content='Really done.'))
    with pytest.raises(TeamError, match='acceptance gate not met'):
        await DelegatingTeamRunner(model, _registry(*_sandbox_tools()), SANDBOX).run(_context())
    assert 'the task is not complete yet' in _texts(model.requests[1])


async def test_grants_stay_inside_the_allowlist_and_reviewers_stay_read_only():
    options = DelegationOptions(allowed_tools=SANDBOX.allowed_tools, require_passing_run=True, review=True)
    model = Scripted(
        delegate('l1', tools=['shell']),
        call('l2', 'review', focus='check', tools=['write_file']),
        ModelTurn(content='FAILED: stop'))
    with pytest.raises(TeamError):
        await DelegatingTeamRunner(model, _registry(*_sandbox_tools()), options).run(_context())
    replies = [m.content for m in model.requests[-1] if isinstance(m, ToolMessage)]
    assert replies[0].startswith('HOST: tools not permitted for a worker')
    assert replies[1].startswith('HOST: tools not permitted for a reviewer') and 'write_file' not in replies[1]
    assert len(model.requests) == 3                          # no worker or reviewer ever ran


async def test_tools_missing_from_the_parent_registry_are_never_granted():
    runner = DelegatingTeamRunner(Scripted(), _registry(FakeTool('read_file', 'x')), SANDBOX)
    assert runner.worker_tools == {'read_file'} and runner.review_tools == {'read_file'}


async def test_worker_failure_is_reported_and_its_unknown_writes_reset_the_gate():
    model = Scripted(
        delegate('l1'),
        call('w1', 'write_file', path='p.py', content='x'),
        call('w2', 'run_tests', path='t.py'),
        final('ok', status='failed', reason='not sure'),     # passes the gate but reports failure
        delegate('l2'),
        BAD, BAD, BAD,                                       # argument repairs exhausted: worker error
        ModelTurn(content='FAILED: giving up'))
    with pytest.raises(TeamError, match='lead reported failure'):
        await DelegatingTeamRunner(model, _registry(*_sandbox_tools()), SANDBOX).run(_context())
    first, second = _reports(model)
    assert first['status'] == 'failed' and first['passing_run_after_last_write'] is True
    assert second == {'member': 'worker-2', 'status': 'error',
                      'failure': 'StepModelError -> ModelToolArgumentsError',
                      'passing_run_after_last_write': False, 'delegations_remaining': 4}


async def test_unknown_lead_tool_and_empty_task_get_host_replies():
    model = Scripted(call('l1', 'write_file', path='a'), call('l2', 'delegate', task=' '),
                     ModelTurn(content='FAILED: stop'))
    with pytest.raises(TeamError):
        await DelegatingTeamRunner(model, _registry(*_sandbox_tools()), SANDBOX).run(_context())
    replies = [m.content for m in model.requests[-1] if isinstance(m, ToolMessage)]
    assert replies == ['HOST: "write_file" is not an available tool. Call only ["delegate"].',
                       'HOST: task must be a nonempty string.']


async def test_read_only_team_ends_with_the_lead_answer():
    model = Scripted(delegate('l1', task='read notes.txt and summarize'),
                     call('w1', 'read_file', path='notes.txt'), final('summary'),
                     ModelTurn(content='The notes say x = 1.'))
    output = await DelegatingTeamRunner(model, _registry(FakeTool('read_file', 'x = 1')),
                                        DelegationOptions()).run(_context())
    assert output.outcome is StepOutcome.SUCCEEDED and output.content == 'The notes say x = 1.'
    assert json.loads(output.observation) == {'team': 'v2', 'delegations': 1, 'members': ['worker-1']}


async def test_blocked_answer_becomes_a_blocked_outcome():
    model = Scripted(ModelTurn(content='BLOCKED: which file should I edit?'))
    output = await DelegatingTeamRunner(model, _registry(), DelegationOptions()).run(_context())
    assert output.outcome is StepOutcome.BLOCKED and output.reason == 'which file should I edit?'


async def test_review_needs_an_approval_after_the_last_write():
    options = DelegationOptions(allowed_tools=SANDBOX.allowed_tools, require_passing_run=True, review=True)
    model = Scripted(
        delegate('l1'),
        call('w1', 'write_file', path='p.py', content='x'), call('w2', 'run_tests', path='t.py'), final('v1'),
        call('l2', 'review', focus='edge cases'),
        call('r1', 'read_file', path='p.py'), final('missing 1 h', status='failed', reason='reject 1 h'),
        delegate('l3', task='fix: reject "1 h"'),
        call('v1', 'write_file', path='p.py', content='y'), call('v2', 'run_tests', path='t.py'), final('v2'),
        call('l4', 'review', focus='edge cases'),
        call('s1', 'read_file', path='p.py'), final('looks right'))
    output = await DelegatingTeamRunner(model, _registry(*_sandbox_tools()), options).run(_context())
    assert output.content == 'v2' and not model.items
    reviews = [r for r in _reports(model) if r['member'].startswith('reviewer')]
    assert [r['decision'] for r in reviews] == ['revise']     # the approving review completed the team
    assert 'Latest worker report:\\nv1' in _texts(model.requests[5])   # JSON-escaped in the context


async def test_reviewer_session_only_sees_read_tools():
    options = DelegationOptions(allowed_tools=SANDBOX.allowed_tools, review=True)
    seen = []

    class Spy(Scripted):
        async def generate(self, messages, tools):
            seen.append(sorted(t.name for t in tools))
            return await super().generate(messages, tools)
    model = Spy(call('l1', 'review', focus='check'), final('fine'), ModelTurn(content='Reviewed: fine.'))
    output = await DelegatingTeamRunner(model, _registry(*_sandbox_tools()), options).run(_context())
    assert output.outcome is StepOutcome.SUCCEEDED and output.content == 'Reviewed: fine.'
    assert seen[1] == ['read_file']


async def test_limits_end_the_team():
    options = DelegationOptions(max_delegations=1, max_lead_turns=3)
    model = Scripted(delegate('l1'), final('done'), delegate('l2'), delegate('l3'))
    with pytest.raises(TeamError, match='lead turn limit'):
        await DelegatingTeamRunner(model, _registry(), options).run(_context())
    replies = [m.content for m in model.requests[-1] if isinstance(m, ToolMessage)]
    assert replies[-1] == 'HOST: delegation limit reached; give your final answer now.'


async def test_lead_argument_errors_are_repaired_then_terminal():
    model = Scripted(BAD, BAD, BAD, BAD)
    with pytest.raises(Exception) as caught:
        await DelegatingTeamRunner(model, _registry(), DelegationOptions()).run(_context())
    assert type(caught.value).__name__ == 'StepModelError'


async def test_trace_has_host_values_only_and_bench_counts_parse(caplog, monkeypatch):
    monkeypatch.setenv('ALETHEIA_TEAM_TRACE', '1')
    model = Scripted(
        delegate('l1', task='PRIVATE-ASSIGNMENT'),
        call('w1', 'write_file', path='p.py', content='PRIVATE-CODE'),
        call('w2', 'run_tests', path='t.py'),
        final('PRIVATE-REPORT'))
    with caplog.at_level(logging.INFO):
        await DelegatingTeamRunner(model, _registry(*_sandbox_tools()), SANDBOX).run(_context())
    line = next(r.getMessage() for r in caplog.records if 'Team trace' in r.getMessage())
    assert 'PRIVATE' not in line
    trace = json.loads(line.split('Team trace: ', 1)[1])
    assert trace['status'] == 'succeeded' and trace['version'] == 2
    assert trace['delegations'][0]['tool_sequence'] == [['write_file', True], ['run_tests', True]]
    assert trace['delegations'][0]['gate'] is True
    counts = re.search(r'"model_calls": (\d+), "tool_calls": (\d+)', line)
    assert counts.groups() == ('4', '2')


async def test_failed_trace_is_loud_and_names_host_reason(caplog, monkeypatch):
    monkeypatch.delenv('ALETHEIA_TEAM_TRACE', raising=False)
    model = Scripted(ModelTurn(content='FAILED: PRIVATE reason'))
    with caplog.at_level(logging.WARNING), pytest.raises(TeamError):
        await DelegatingTeamRunner(model, _registry(), DelegationOptions()).run(_context())
    line = next(r.getMessage() for r in caplog.records if 'Team trace' in r.getMessage())
    assert 'PRIVATE' not in line and 'TeamError: team lead reported failure' in line


def test_options_are_validated():
    for bad in ({'max_delegations': 0}, {'review': 1}, {'timeout_seconds': float('inf')},
                {'allowed_tools': {'read_file'}}):
        with pytest.raises((ValueError, TypeError)):
            DelegationOptions(**bad)


def test_cli_and_bench_select_version_2(monkeypatch):
    from types import SimpleNamespace
    from app.agent.__main__ import team_options_for
    from bench import run_bench
    options = team_options_for('team', 'w', True, 400, team_version=2)
    assert isinstance(options, DelegationOptions) and options.require_passing_run
    assert {'write_file', 'run_tests', 'run_python'} <= options.allowed_tools and options.timeout_seconds == 400
    assert team_options_for('team', None, team_version=2).allowed_tools == READ_TOOLS
    assert type(team_options_for('team', 'w', True, 400)).__name__ == 'TeamOptions'   # V1 stays the default
    seen = {}

    def fake(cmd, env, timeout):
        seen['cmd'] = cmd
        return SimpleNamespace(stdout='x\n\nStatus: failed; executions: 1; replans: 0\n', stderr='')
    monkeypatch.setattr(run_bench, 'launch_agent', fake)
    task = next(t for t in run_bench.load_tasks(['all']) if t['id'] == 'parse_duration')
    run_bench.run_one(task, 'team', 1, 5, team_version=2)
    assert seen['cmd'][seen['cmd'].index('--team-version') + 1] == '2'
    run_bench.run_one(task, 'single', 1, 5, team_version=2)
    assert '--team-version' not in seen['cmd']


def test_service_plans_one_step_and_binds_the_v2_runner():
    from app.agent.runner_registry import RunnerDependencies, RunnerLimits
    from app.services.context_manager import ContextManager
    from app.services.rag_agent_service import RagAgentService
    service = RagAgentService(execution_mode='team', team_options=SANDBOX, mcp_servers=())
    assert service._team_v2 is True
    registry = service._team_registry(None)
    assert [d.runner_id for d in registry.definitions()] == ['tool_calling', 'agent_team']
    bound = registry.bind(RunnerDependencies(Scripted(), _registry(*_sandbox_tools()), ContextManager(),
                                             RunnerLimits()))
    assert isinstance(bound[1].runner, DelegatingTeamRunner)


async def test_single_step_planner_never_calls_the_planning_model():
    from app.agent.team_v2 import SingleStepPlanner
    from app.models.taskstate import TaskState
    from app.models.working_memory import WorkingMemory

    class NoModel:
        async def generate(self, prompt):
            raise AssertionError('planning model must not be called')
    memory = WorkingMemory(TaskState('u', 'a long goal ' * 100))
    memory.task_state.start_planning()
    planner = SingleStepPlanner(NoModel(), max_steps=1)
    proposal = await planner.plan(planner.build_context(memory))
    assert proposal.steps == (SingleStepPlanner.STEP,) and len(SingleStepPlanner.STEP) <= planner.max_step_length
    with pytest.raises(ValueError, match='use plan'):
        await planner.replan(planner.build_context(memory))


def test_bench_recognizes_planning_failures(monkeypatch):
    from types import SimpleNamespace
    from bench import run_bench
    monkeypatch.setattr(run_bench, 'launch_agent', lambda cmd, env, timeout: SimpleNamespace(
        stdout='Planning failed: ValueError: steps must contain at most 1 items\n\n'
               'Status: failed; executions: 0; replans: 0\n', stderr=''))
    task = next(t for t in run_bench.load_tasks(['all']) if t['id'] == 'parse_duration')
    row = run_bench.run_one(task, 'team', 1, 5)
    assert row['failure'] == 'Planning failed: ValueError: steps must contain at most 1 items'


async def test_runtime_end_to_end_with_single_step_plan_and_v2_runner():
    from app.agent.runner_registry import default_runner_registry
    from app.agent.runner_router import RunnerRequirements
    from app.agent.runtime import AgentRuntime
    from app.agent.team_v2 import SingleStepPlanner, register_delegating_team_runner
    from app.models.taskstate import TaskState, TaskStatus
    from app.models.working_memory import WorkingMemory

    class NoModel:
        async def generate(self, prompt):
            raise AssertionError('planning model must not be called')
    model = Scripted(
        delegate('l1'),
        call('w1', 'write_file', path='parse_duration.py', content='code'),
        call('w2', 'run_tests', path='t.py'),
        final('implemented and tested'))
    registry = default_runner_registry()
    register_delegating_team_runner(registry, options=SANDBOX)
    memory = WorkingMemory(TaskState('u', 'write parse_duration.py ' * 40))
    runtime = AgentRuntime(SingleStepPlanner(NoModel(), max_steps=1), model, memory,
                           _registry(*_sandbox_tools()), runner_registry=registry,
                           runner_requirements=lambda context: RunnerRequirements(frozenset({'agent_team'})))
    result = await runtime.run()
    assert result.status is TaskStatus.COMPLETED and result.execution_count == 1
    assert not model.items


async def test_every_lead_request_mentions_json_for_provider_json_mode():
    # With RAG_JSON_MODE the provider rejects requests that never mention "json" (live run 2, BadRequestError).
    model = Scripted(delegate('l1'), call('w1', 'read_file', path='a'), final('ok'),
                     ModelTurn(content='{"status": "succeeded", "content": "answer", "reason": null}'))
    output = await DelegatingTeamRunner(model, _registry(FakeTool('read_file', 'x')),
                                        DelegationOptions()).run(_context())
    assert output.content == 'answer'
    assert all('json' in _texts(request).lower() for request in model.requests)


@pytest.mark.parametrize('text, outcome, reason', [
    ('{"status": "blocked", "content": "need input", "reason": "which file?"}', StepOutcome.BLOCKED, 'which file?'),
    ('```json\n{"status": "succeeded", "content": "fine", "reason": null}\n```', StepOutcome.SUCCEEDED, None),
    ('plain answer', StepOutcome.SUCCEEDED, None),
])
async def test_lead_final_answers_follow_the_single_mode_outcome(text, outcome, reason):
    model = Scripted(ModelTurn(content=text))
    output = await DelegatingTeamRunner(model, _registry(), DelegationOptions()).run(_context())
    assert output.outcome is outcome and output.reason == reason


async def test_lead_json_failure_ends_the_team():
    model = Scripted(ModelTurn(content='{"status": "failed", "content": "x", "reason": "PRIVATE"}'))
    with pytest.raises(TeamError, match='lead reported failure'):
        await DelegatingTeamRunner(model, _registry(), DelegationOptions()).run(_context())
