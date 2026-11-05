"""Deterministic tests for Team V2, the delegating team (team-v2-001, DECISIONS D-019)."""

import json
import logging
import re

import pytest

from app.agent.step_models import ModelTurn, StepOutcome, ToolMessage
from app.agent.team.models import TeamError
from app.agent.team_v2 import DelegatingTeamRunner, DelegationOptions, READ_TOOLS
from app.agent.tool_calling_model import ModelToolArgumentsError
from app.agent.tool_calling_step_runner import ToolCallingStepRunner
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


async def test_worker_error_without_tool_results_keeps_the_gate():
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
    # D-023: the host observed that worker-2 wrote nothing, so worker-1's passing run still stands.
    assert second == {'member': 'worker-2', 'status': 'error',
                      'failure': 'StepModelError -> ModelToolArgumentsError',
                      'passing_run_after_last_write': True, 'delegations_remaining': 4}


class _Raising(FakeTool):
    async def invoke(self, arguments):
        self.calls.append(arguments)
        raise ConnectionError('disconnected')


async def test_invalid_final_report_keeps_the_host_observed_delivery():
    # Live V2: StepFormatError -> ValueError after a passing run discarded the delivery (D-023).
    model = Scripted(
        delegate('l1'),
        call('w1', 'write_file', path='parse_duration.py', content='code'),
        call('w2', 'run_tests', path='test_parse_duration.py'),
        ModelTurn(content='{"status": "succeeded", "content": "done"}'),   # valid JSON, missing reason
        final('implemented; the host saw the passing run'))
    output = await DelegatingTeamRunner(model, _registry(*_sandbox_tools()), SANDBOX).run(_context())
    assert output.outcome is StepOutcome.SUCCEEDED
    assert output.content == 'implemented; the host saw the passing run' and not model.items
    report = _reports(model)[0]
    assert report['status'] == 'error' and report['failure'] == 'StepFormatError -> ValueError'
    assert report['files_written'] == ['parse_duration.py']
    assert report['last_run'] == {'tool': 'run_tests', 'summary': 'exit code 0; 1.0s'}
    assert report['passing_run_after_last_write'] is True and 'host kept' in report['note']


async def test_invalid_final_report_after_a_late_write_does_not_pass():
    model = Scripted(
        delegate('l1'),
        call('w1', 'run_tests', path='t.py'),
        call('w2', 'write_file', path='p.py', content='late'),
        ModelTurn(content='{"status": "succeeded", "content": "done"}'),
        final('done'), ModelTurn(content='FAILED: unverified'))
    with pytest.raises(TeamError, match='lead reported failure'):
        await DelegatingTeamRunner(model, _registry(*_sandbox_tools()), SANDBOX).run(_context())
    assert _reports(model)[0]['passing_run_after_last_write'] is False


async def test_interrupted_write_stays_unknown_and_resets_the_gate():
    tools = (FakeTool('read_file', 'x'), _Raising('write_file', ''), Seq('run_tests', 'exit code 0; 1.0s'))
    model = Scripted(
        delegate('l1', tools=['read_file', 'run_tests']),
        call('w1', 'run_tests', path='t.py'), final('ran', status='failed', reason='nothing written'),
        delegate('l2'),
        call('v1', 'write_file', path='p.py', content='x'),
        ModelTurn(content='FAILED: write failed'))
    with pytest.raises(TeamError):
        await DelegatingTeamRunner(model, _registry(*tools), SANDBOX).run(_context())
    first, second = _reports(model)
    assert first['passing_run_after_last_write'] is True
    assert second['status'] == 'error' and second['passing_run_after_last_write'] is False
    assert 'files_written' not in second


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
    assert type(team_options_for('team', 'w', True, 400)).__name__ == 'TeamOptions'   # programmatic V1 API kept
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


def _worker_writes(request):
    from app.agent.step_models import AssistantMessage
    return {c.arguments['path']: c.arguments['content'] for m in request if isinstance(m, AssistantMessage)
            for c in m.tool_calls if c.tool_name == 'write_file'}


def _four_writes():
    return [call(f'w{i}', 'write_file', path=f'{n}.py', content=n * 3000) for i, n in enumerate('abcd', 1)]


async def test_plain_worker_overflows_on_whole_file_writes():
    # Reproduces the live failure: prepare() never shrinks write_file arguments.
    from app.agent.step_runner import StepModelError
    from app.services.context_manager import ContextManager
    model = Scripted(*_four_writes(), call('w5', 'run_tests', path='t.py'), final('done'))
    runner = ToolCallingStepRunner(model, _registry(*_sandbox_tools()), 12, 24, require_step_outcome=True,
                                   context_manager=ContextManager(window_tokens=7000))
    with pytest.raises(StepModelError, match='ContextBudgetError'):
        await runner.run(_context())


async def test_worker_view_keeps_only_the_latest_file_body_when_near_the_window():
    from app.services.context_manager import ContextManager
    model = Scripted(delegate('l1'), *_four_writes(), call('w5', 'run_tests', path='t.py'), final('done'))
    runner = DelegatingTeamRunner(model, _registry(*_sandbox_tools()), SANDBOX, ContextManager(window_tokens=7000))
    output = await runner.run(_context())
    assert output.outcome is StepOutcome.SUCCEEDED and not model.items
    view = _worker_writes(model.requests[5])                  # worker request after the 4th write
    assert view['d.py'] == 'd' * 3000
    for name in 'abc':
        assert view[f'{name}.py'] == '[host: 3000 chars omitted; read_file this path for its current content]'
    record = {c.arguments['path']: c.arguments['content'] for c in output.tool_calls if c.tool_name == 'write_file'}
    assert record == {f'{n}.py': n * 3000 for n in 'abcd'}   # evidence keeps the full arguments


async def test_worker_view_drops_superseded_bodies_first(caplog, monkeypatch):
    from app.services.context_manager import ContextManager
    monkeypatch.setenv('ALETHEIA_TEAM_TRACE', '1')
    model = Scripted(delegate('l1'),
                     call('w1', 'write_file', path='a.py', content='a' * 3000),
                     call('w2', 'write_file', path='a.py', content='b' * 3000),
                     call('w3', 'run_tests', path='t.py'), final('done'))
    runner = DelegatingTeamRunner(model, _registry(*_sandbox_tools()), SANDBOX, ContextManager(window_tokens=6500))
    with caplog.at_level(logging.INFO):
        await runner.run(_context())
    from app.agent.step_models import AssistantMessage
    bodies = [c.arguments['content'] for m in model.requests[3] if isinstance(m, AssistantMessage)
              for c in m.tool_calls if c.tool_name == 'write_file']
    assert bodies == ['[host: 3000 chars omitted; a later write_file replaced this file]', 'b' * 3000]
    line = next(r.getMessage() for r in caplog.records if 'Team trace' in r.getMessage())
    entry = json.loads(line.split('Team trace: ', 1)[1])['delegations'][0]
    assert entry['compacted'] >= 1 and entry['strong_compacted'] == 0


async def test_strong_tier_also_drops_old_code_arguments_and_long_outputs():
    # Live run: strong compaction of write bodies alone still overflowed (run_python code, full outputs).
    from app.agent.step_models import AssistantMessage
    from app.services.context_manager import ContextManager
    big_out = 'exit code 0; 1.0s\n' + 'o' * 3000
    tools = (FakeTool('read_file', 'x'), FakeTool('write_file', 'ok'), FakeTool('run_tests', big_out),
             FakeTool('run_python', big_out))
    model = Scripted(delegate('l1'),
                     call('w1', 'run_python', code='p' * 3000),
                     call('w2', 'run_python', code='q' * 3000),
                     call('w3', 'run_python', code='r' * 3000),
                     call('w4', 'write_file', path='a.py', content='a' * 3000),
                     call('w5', 'run_tests', path='t.py'), final('done'))
    runner = DelegatingTeamRunner(model, _registry(*tools), SANDBOX, ContextManager(window_tokens=7000))
    output = await runner.run(_context())
    assert output.outcome is StepOutcome.SUCCEEDED and not model.items
    request = model.requests[5]                                  # after the write, before run_tests
    codes = [c.arguments.get('code') for m in request if isinstance(m, AssistantMessage)
             for c in m.tool_calls if c.tool_name == 'run_python']
    assert codes == ['[host: 3000 chars omitted from an earlier call]'] * 3
    assert _worker_writes(request) == {'a.py': 'a' * 3000}      # the latest turn stays whole
    assert [c.arguments['code'] for c in output.tool_calls if c.tool_name == 'run_python'] == [
        'p' * 3000, 'q' * 3000, 'r' * 3000]                      # evidence keeps full arguments


ACC = DelegationOptions(allowed_tools=SANDBOX.allowed_tools, require_passing_run=True, acceptance_tests=True)
PASS, FAIL = 'exit code 0; 1.0s\n3 passed', 'exit code 1; 1.0s\nFAILED test_acceptance_p.py::test_rejects_1d'


def _lead_requests(model):
    return [r for r in model.requests if 'Team Lead' in (getattr(r[0], 'content', '') or '')]


def _lead_replies(model):
    return [json.loads(m.content) for m in _lead_requests(model)[-1]
            if isinstance(m, ToolMessage) and m.content.startswith('{')]


def _acc_tools(*runs):
    return (FakeTool('read_file', 'x'), FakeTool('write_file', 'File written successfully.'), Seq('run_tests', *runs))


def _worker_round(tag):
    return [call(f'{tag}1', 'write_file', path='p.py', content='code'), call(f'{tag}2', 'run_tests'), final('works')]


def _verifier_round(tag):
    return [call(f'{tag}1', 'write_file', path='test_acceptance_p.py', content='tests'),
            call(f'{tag}2', 'run_tests', path='test_acceptance_p.py'), final('tests written')]


async def test_host_runs_independent_acceptance_tests_before_completing():
    tools = _acc_tools(PASS, PASS, PASS)                  # worker, verifier, host
    model = Scripted(delegate('l1'), *_worker_round('w'), *_verifier_round('v'))
    output = await DelegatingTeamRunner(model, _registry(*tools), ACC).run(_context())
    assert output.outcome is StepOutcome.SUCCEEDED and output.content == 'works' and not model.items
    host = [(c.call_id, c.arguments) for c in output.tool_calls if c.call_id.startswith('host/')]
    assert host == [('host/acceptance-1', {'path': 'test_acceptance_p.py'})]
    assert tools[2].calls[-1] == {'path': 'test_acceptance_p.py'}           # the host's own run
    assert 'independent acceptance tester' in _texts(model.requests[4])     # verifier gets a fresh brief
    assert 'Team Lead' not in _texts(model.requests[4])


async def test_failed_acceptance_goes_back_to_the_lead_and_the_suite_is_reused():
    tools = _acc_tools(PASS, FAIL, FAIL, PASS, PASS)    # worker, verifier, host, worker, host
    model = Scripted(delegate('l1'), *_worker_round('w'), *_verifier_round('v'),
                     delegate('l2', task='fix p.py so the acceptance tests pass'), *_worker_round('x'))
    output = await DelegatingTeamRunner(model, _registry(*tools), ACC).run(_context())
    assert output.outcome is StepOutcome.SUCCEEDED and not model.items
    first = _lead_replies(model)[0]
    assert first['acceptance']['passed'] is False and first['acceptance']['verifiers'][0]['member'] == 'verifier-2'
    assert 'test_rejects_1d' in first['acceptance']['runs'][0]['output']
    assert 'make them pass' in _texts(model.requests[8]) and 'test_acceptance_p.py' in _texts(model.requests[8])
    assert [c.call_id for c in output.tool_calls if c.call_id.startswith('host/')] == [
        'host/acceptance-1', 'host/acceptance-2']                            # no second verifier


async def test_lead_answer_is_not_accepted_while_acceptance_fails():
    tools = _acc_tools(PASS, FAIL, FAIL)
    done = ModelTurn(content='{"status": "succeeded", "content": "done", "reason": null}')
    model = Scripted(delegate('l1'), *_worker_round('w'), *_verifier_round('v'), done, done)
    with pytest.raises(TeamError, match='acceptance gate not met'):
        await DelegatingTeamRunner(model, _registry(*tools), ACC).run(_context())


async def test_write_gate_keeps_workers_and_verifiers_on_their_own_files():
    from app.agent.team_v2 import _WriteGate
    write = FakeTool('write_file', 'ok')
    worker, verifier = _WriteGate(write, False), _WriteGate(write, True)
    assert (await worker.invoke({'path': 'sub/test_acceptance_p.py'})).success is False
    assert (await worker.invoke({'path': 'p.py'})).success is True
    assert (await verifier.invoke({'path': 'p.py'})).success is False
    assert (await verifier.invoke({'path': 'test_acceptance_p.py'})).success is True
    assert write.calls == [{'path': 'p.py'}, {'path': 'test_acceptance_p.py'}]
    # In a delegation the rejection reaches the worker as a failed tool result, never a write.
    tools = _acc_tools(PASS)
    model = Scripted(delegate('l1'), call('w1', 'write_file', path='test_acceptance_p.py', content='weaker'),
                     final('tried', status='failed', reason='blocked by host'),
                     ModelTurn(content='{"status": "failed", "content": "x", "reason": "stop"}'))
    with pytest.raises(TeamError):
        await DelegatingTeamRunner(model, _registry(*tools), ACC).run(_context())
    assert tools[1].calls == []


async def test_verify_rewrites_the_suite_and_can_complete():
    tools = _acc_tools(PASS, FAIL, FAIL, PASS, PASS)    # worker, verifier, host, new verifier, host
    model = Scripted(call('l0', 'verify', focus='too early'),
                     delegate('l1'), *_worker_round('w'), *_verifier_round('v'),
                     call('l2', 'verify', focus='1d is allowed by the goal'), *_verifier_round('u'))
    output = await DelegatingTeamRunner(model, _registry(*tools), ACC).run(_context())
    assert output.outcome is StepOutcome.SUCCEEDED and not model.items
    early = next(m.content for m in _lead_requests(model)[-1] if isinstance(m, ToolMessage) and m.call_id == 'l0')
    assert early == 'HOST: no worker has succeeded yet; delegate the implementation first.'
    assert '1d is allowed by the goal' in _texts(model.requests[-3])          # the fresh verifier's brief


async def test_verifiers_have_their_own_budget_then_fall_back_to_the_worker_gate(caplog, monkeypatch):
    # Live run 1: a failed verifier used up the shared budget and a 21/21 delivery ended blocked.
    monkeypatch.setenv('ALETHEIA_TEAM_TRACE', '1')
    model = Scripted(delegate('l1'), *_worker_round('w'),
                     final('wrote nothing'),                                    # verifier 1: no tests
                     BAD, BAD, BAD)                                              # verifier 2: crashes
    options = DelegationOptions(allowed_tools=ACC.allowed_tools, require_passing_run=True,
                                acceptance_tests=True, max_delegations=1)
    with caplog.at_level(logging.INFO):
        output = await DelegatingTeamRunner(model, _registry(*_acc_tools(PASS)), options).run(_context())
    assert output.outcome is StepOutcome.SUCCEEDED and output.content == 'works' and not model.items
    line = next(r.getMessage() for r in caplog.records if 'Team trace' in r.getMessage())
    trace = json.loads(line.split('Team trace: ', 1)[1])
    assert trace['budget']['delegations'] == 1 and trace['budget']['verifications'] == 2
    assert trace['delegations'][-1] == {'member': 'host', 'role': 'acceptance', 'status': 'unavailable', 'suite': 0}


async def test_failed_verifier_files_come_from_host_observation():
    listing = FakeTool('list_directory', '{"entries": [{"name": "p.py"}, {"name": "test_acceptance_p.py"}]}')
    tools = (*_acc_tools(PASS, PASS), listing)          # worker run, host run
    model = Scripted(delegate('l1'), *_worker_round('w'),
                     call('v1', 'write_file', path='test_acceptance_p.py', content='tests'),
                     BAD, BAD, BAD)                      # the verifier crashes after writing its file
    output = await DelegatingTeamRunner(model, _registry(*tools), ACC).run(_context())
    assert output.outcome is StepOutcome.SUCCEEDED and not model.items
    assert [c.arguments for c in output.tool_calls if c.call_id.startswith('host/')] == [
        {'path': 'test_acceptance_p.py'}]
    assert listing.calls == []                           # D-023: observed, no workspace listing needed


async def test_ungranted_tool_is_named_in_the_report():
    model = Scripted(delegate('l1', tools=['read_file']), call('w1', 'write_file', path='p.py', content='x'),
                     ModelTurn(content='{"status": "failed", "content": "x", "reason": "stop"}'))
    with pytest.raises(TeamError):
        await DelegatingTeamRunner(model, _registry(*_sandbox_tools()), SANDBOX).run(_context())
    report = _reports(model)[0]
    assert report['status'] == 'error' and report['unavailable_tool'] == 'write_file'
    assert 'not granted' in report['note']


def test_command_line_and_bench_default_to_version_2(monkeypatch):
    # D-024: V2 is the default team for the command line and the bench; --team-version 1 keeps V1.
    from types import SimpleNamespace
    from app.agent.__main__ import build_parser
    from bench import run_bench
    assert build_parser().parse_args(['goal']).team_version == 2
    assert build_parser().parse_args(['goal', '--team-version', '1']).team_version == 1
    seen = []
    monkeypatch.setattr(run_bench, 'launch_agent', lambda cmd, env, timeout: seen.append(cmd) or SimpleNamespace(
        stdout='x\n\nStatus: failed; executions: 1; replans: 0\n', stderr=''))
    task = next(t for t in run_bench.load_tasks(['all']) if t['id'] == 'parse_duration')
    run_bench.run_one(task, 'team', 1, 5)
    run_bench.run_one(task, 'team', 1, 5, team_version=1)
    assert [cmd[cmd.index('--team-version') + 1] for cmd in seen] == ['2', '1']


def test_acceptance_tests_are_opt_in():
    # D-022: independent acceptance blocked correct deliveries on the current model; off by default.
    from types import SimpleNamespace
    from app.agent.__main__ import team_options_for
    from bench import run_bench
    assert team_options_for('team', 'w', True, 400, team_version=2).acceptance_tests is False
    assert team_options_for('team', 'w', True, 400, team_version=2, acceptance=True).acceptance_tests is True
    assert team_options_for('team', 'w', False, 400, team_version=2, acceptance=True).acceptance_tests is False
    seen = []
    monkeypatch_target = run_bench.launch_agent
    run_bench.launch_agent = lambda cmd, env, timeout: seen.append(cmd) or SimpleNamespace(
        stdout='x\n\nStatus: failed; executions: 1; replans: 0\n', stderr='')
    try:
        task = next(t for t in run_bench.load_tasks(['all']) if t['id'] == 'parse_duration')
        run_bench.run_one(task, 'team', 1, 5, team_version=2)
        run_bench.run_one(task, 'team', 1, 5, team_version=2, team_acceptance=True)
        run_bench.run_one(task, 'single', 1, 5, team_version=2, team_acceptance=True)
    finally:
        run_bench.launch_agent = monkeypatch_target
    assert ['--team-acceptance' in cmd for cmd in seen] == [False, True, False]
    runner = DelegatingTeamRunner(Scripted(), _registry(FakeTool('read_file', 'x')), ACC)
    assert runner.acceptance is False                                         # no run/write tools, no gate


def test_bench_rows_name_the_static_guard_reason(monkeypatch):
    # Live run 2026-11-05: a delivery was refused by the static guard and the row did not say why.
    from types import SimpleNamespace
    from bench import run_bench
    from bench.graders import static_guard
    assert {static_guard(c) for c in ('import os', 'open("x")', 'x.__class__', 'def (')} == run_bench._GUARD_REASONS
    answer = 'import os\n\ndef parse_duration(text):\n    return 0\n'
    monkeypatch.setattr(run_bench, 'launch_agent', lambda cmd, env, timeout: SimpleNamespace(
        stdout=answer + '\nStatus: completed; executions: 1; replans: 0\n', stderr=''))
    task = next(t for t in run_bench.load_tasks(['all']) if t['id'] == 'parse_duration')
    row = run_bench.run_one(task, 'single', 1, 5)
    assert row['passed'] is False and row['guard_reason'] == 'forbidden_import'
