"""Deterministic tests for team recovery (team-recovery-001).

1. Member-local protocol failures go back to the Lead as member_failed, bounded by max_member_failures.
2. Format repairs reset per member run / Lead command, bounded by the team total max_format_repairs.
"""

import json
from contextlib import asynccontextmanager

import pytest

from app.agent.step_models import ModelTurn
from app.agent.team.coordinator import TeamCoordinator
from app.agent.team.models import TeamEnvironment, TeamError, TeamOptions, TeamStore
from app.agent.tool_calling_model import ModelToolArgumentsError
from app.tools.tool_registry import ToolRegistry
from test_sandbox_step1 import _context
from test_sandbox_step3 import FakeTool, act, call
from test_tool_argument_repair import Scripted

BAD = ModelToolArgumentsError('invalid_json', tool_name='submit_result', argument_chars=12000)
PROSE = ModelTurn(content='not a command')


def _env(*tools):
    @asynccontextmanager
    async def factory(memory, manager):
        registry = ToolRegistry()
        for tool in tools:
            registry.register(tool)
        yield registry
    return TeamEnvironment('u', factory, lambda m: {})


def _options(**kw):
    return TeamOptions(allowed_tools=TeamOptions().allowed_tools | {'write_file'}, **kw)


def _lead_events(model):
    """HOST TEAM EVENT packets the Lead received, decoded."""
    out = []
    for request in model.requests:
        for message in request:
            text = getattr(message, 'content', None)
            if isinstance(text, str) and 'HOST TEAM EVENT: ' in text:
                packet = text.split('HOST TEAM EVENT: ', 1)[1]
                try:
                    out.append(json.loads(packet))
                except ValueError:
                    pass
    return out


async def test_member_protocol_failure_goes_back_to_lead_who_replaces_it():
    write = FakeTool('write_file', 'File written successfully.')
    model = Scripted(
        act('l1', 'create_worker', 'coder', tools='write_file'),
        act('l2', 'run_member', 'coder'),
        BAD, BAD, BAD,                                            # 2 repairs, then the member fails
        act('l3', 'create_worker', 'coder2', tools='write_file'),
        act('l4', 'run_member', 'coder2'),
        call('w1', 'write_file', path='a.py', content='x'),
        call('w2', 'submit_result', payload={'answer': 'done'}),
        act('l5', 'create_reviewer', 'checker'),
        act('l6', 'review', 'checker', 'coder2', version=1),
        call('r1', 'submit_result', payload={'candidate_version': 1, 'decision': 'approve', 'feedback': 'ok'}),
        act('l7', 'finish', 'coder2', version=1))
    ctx = _context()
    state = TeamStore().get(ctx, _options())
    output = await TeamCoordinator(state, model, _env(write), ctx).run()
    assert json.loads(output.content) == {'answer': 'done'}
    assert state.status == 'completed' and state.budget.member_failures == 1
    assert state.members['coder'].session.status == 'failed'
    assert 'ModelToolArgumentsError' in state.members['coder'].failure
    failed = [p for p in _lead_events(model) if p['event'].get('event') == 'member_failed']
    assert failed and failed[0]['event']['member_id'] == 'coder'
    assert failed[0]['event']['failures_remaining'] == 1
    assert 'ModelToolArgumentsError' in failed[0]['event']['failure']


async def test_failed_member_cannot_run_again_and_failure_limit_is_terminal():
    model = Scripted(
        act('l1', 'create_worker', 'coder', tools='write_file'),
        act('l2', 'run_member', 'coder'),
        BAD, BAD, BAD,
        act('l3', 'create_worker', 'coder2', tools='write_file'),
        act('l4', 'run_member', 'coder2'),
        BAD, BAD, BAD)
    ctx = _context()
    state = TeamStore().get(ctx, _options(max_member_failures=1))
    with pytest.raises(TeamError, match='member failure limit'):
        await TeamCoordinator(state, model, _env(FakeTool('write_file', 'ok')), ctx).run()
    assert state.status == 'failed' and state.budget.member_failures == 2


async def test_rerunning_a_failed_member_is_not_a_recovery_path():
    model = Scripted(
        act('l1', 'create_worker', 'coder', tools='write_file'),
        act('l2', 'run_member', 'coder'),
        BAD, BAD, BAD,
        act('l3', 'run_member', 'coder'))                          # failed sessions are single-use
    ctx = _context()
    state = TeamStore().get(ctx, _options())
    with pytest.raises(TeamError):
        await TeamCoordinator(state, model, _env(FakeTool('write_file', 'ok')), ctx).run()
    assert state.budget.member_failures == 1 and state.status == 'failed'


async def test_provider_errors_stay_terminal():
    model = Scripted(
        act('l1', 'create_worker', 'coder', tools='write_file'),
        act('l2', 'run_member', 'coder'),
        RuntimeError('PRIVATE provider body'))
    ctx = _context()
    state = TeamStore().get(ctx, _options())
    with pytest.raises(TeamError):
        await TeamCoordinator(state, model, _env(FakeTool('write_file', 'ok')), ctx).run()
    assert state.budget.member_failures == 0
    assert 'PRIVATE' not in json.dumps(state.failure)


async def test_lead_repairs_reset_per_command():
    # 2 rejected outputs on each of two commands: 4 repairs in total, never 3 in one run.
    model = Scripted(
        PROSE, PROSE, act('l1', 'create_worker', 'coder', tools='write_file'),
        PROSE, PROSE, act('l2', 'fail', instruction='stop'))
    ctx = _context()
    state = TeamStore().get(ctx, _options())
    with pytest.raises(TeamError, match='stop'):
        await TeamCoordinator(state, model, _env(FakeTool('write_file', 'ok')), ctx).run()
    assert state.history[-1]['action'] == 'fail'
    assert state.budget.format_repairs == 4


async def test_team_total_bounds_repairs_across_runs():
    model = Scripted(
        PROSE, PROSE, act('l1', 'create_worker', 'coder', tools='write_file'),
        PROSE, PROSE)                                              # total 3: the 4th rejection is fatal
    ctx = _context()
    state = TeamStore().get(ctx, _options(max_format_repairs=3))
    with pytest.raises(TeamError):
        await TeamCoordinator(state, model, _env(FakeTool('write_file', 'ok')), ctx).run()
    assert state.budget.format_repairs == 3 and state.lead.format_repairs == 2


def test_new_limits_are_validated():
    assert (TeamOptions().max_member_failures, TeamOptions().max_format_repairs) == (2, 8)
    for name in ('max_member_failures', 'max_format_repairs'):
        with pytest.raises(ValueError):
            TeamOptions(**{name: -1})
        with pytest.raises(ValueError):
            TeamOptions(**{name: True})


async def test_unknown_tool_is_a_bounded_repair_for_the_lead():
    import logging
    model = Scripted(
        call('u1', 'create_worker', payload={}),                  # action name used as a tool name
        call('u2', 'request_coordination', issue='?'),            # Lead has no coordination tool
        act('l1', 'fail', instruction='stop'))
    ctx = _context()
    state = TeamStore().get(ctx, _options())
    with pytest.raises(TeamError, match='stop'):
        await TeamCoordinator(state, model, _env(), ctx).run()
    lead = state.lead
    assert [e.kind for e in lead.events].count('unknown_tool_rejected') == 2
    assert lead.tool_calls == 1 and lead.format_repairs == 2      # only the fail command executed
    texts = [getattr(m, 'content', '') or '' for m in model.requests[1]]
    assert any('"create_worker" is not an available tool' in t and '["submit_result"]' in t for t in texts)
    assert 'request_coordination' not in lead.messages[0].content


async def test_unknown_tool_after_repairs_fails_member_and_returns_to_lead():
    model = Scripted(
        act('l1', 'create_worker', 'coder', tools='write_file'),
        act('l2', 'run_member', 'coder'),
        call('x1', 'shell', cmd='ls'), call('x2', 'shell', cmd='ls'), call('x3', 'shell', cmd='ls'),
        act('l3', 'fail', instruction='stop'))
    ctx = _context()
    state = TeamStore().get(ctx, _options())
    with pytest.raises(TeamError, match='stop'):
        await TeamCoordinator(state, model, _env(FakeTool('write_file', 'ok')), ctx).run()
    coder = state.members['coder']
    assert coder.session.status == 'failed' and coder.session.tool_calls == 0
    assert 'SessionError' in coder.failure and state.budget.member_failures == 1
    assert 'request_coordination' in coder.session.messages[0].content


def _requested_writes(request):
    from app.agent.step_models import AssistantMessage
    return [c.arguments['content'] for m in request if isinstance(m, AssistantMessage)
            for c in m.tool_calls if c.tool_name == 'write_file']


async def test_model_view_drops_superseded_file_bodies_without_touching_the_record():
    from app.services.context_manager import ContextManager
    from app.agent.step_models import AssistantMessage
    write = FakeTool('write_file', 'File written successfully.')
    old, new = 'a' * 3000, 'b' * 3000
    model = Scripted(
        act('l1', 'create_worker', 'coder', tools='write_file'),
        act('l2', 'run_member', 'coder'),
        call('w1', 'write_file', path='a.py', content=old),
        call('w2', 'write_file', path='a.py', content=new),
        call('w3', 'submit_result', payload={'answer': 'done'}),
        act('l3', 'fail', instruction='stop'))
    ctx = _context()
    state = TeamStore().get(ctx, _options())
    coordinator = TeamCoordinator(state, model, _env(write), ctx, ContextManager(window_tokens=6000))
    with pytest.raises(TeamError, match='stop'):
        await coordinator.run()
    submit_request = model.requests[4]                              # coder's third model call
    writes = _requested_writes(submit_request)
    assert writes[0].startswith('[host: 3000 chars omitted') and writes[1] == new
    record = [c.arguments['content'] for m in state.members['coder'].session.messages
              if isinstance(m, AssistantMessage) for c in m.tool_calls if c.tool_name == 'write_file']
    assert record == [old, new]


async def test_small_contexts_are_sent_unchanged():
    write = FakeTool('write_file', 'ok')
    model = Scripted(
        act('l1', 'create_worker', 'coder', tools='write_file'),
        act('l2', 'run_member', 'coder'),
        call('w1', 'write_file', path='a.py', content='x' * 500),
        call('w2', 'write_file', path='a.py', content='y' * 500),
        call('w3', 'submit_result', payload={'answer': 'done'}),
        act('l3', 'fail', instruction='stop'))
    ctx = _context()
    state = TeamStore().get(ctx, _options())
    with pytest.raises(TeamError, match='stop'):
        await TeamCoordinator(state, model, _env(write), ctx).run()
    assert _requested_writes(model.requests[4]) == ['x' * 500, 'y' * 500]


async def test_lead_sees_finish_ready_only_after_current_approval():
    write = FakeTool('write_file', 'ok')
    model = Scripted(
        act('l1', 'create_worker', 'coder', tools='write_file'),
        act('l2', 'run_member', 'coder'),
        call('w1', 'submit_result', payload={'answer': 'done'}),
        act('l3', 'create_reviewer', 'checker'),
        act('l4', 'review', 'checker', 'coder', version=1),
        call('r1', 'submit_result', payload={'candidate_version': 1, 'decision': 'approve', 'feedback': 'ok'}),
        act('l5', 'finish', 'coder', version=1))
    ctx = _context()
    state = TeamStore().get(ctx, _options())
    await TeamCoordinator(state, model, _env(write), ctx).run()
    ready = [{m['id']: m['finish_ready'] for m in p['members']} for p in _lead_events(model)]
    # Requests repeat earlier packets; the approval packet is the only one marking coder ready.
    assert {'coder': True, 'checker': False} in ready
    first_ready = ready.index({'coder': True, 'checker': False})
    assert all(r.get('coder') is not True for r in ready[:first_ready])
    assert [p['event'].get('event') for p in _lead_events(model)][first_ready] == 'review_completed'
    assert 'finish_ready true means' in model.requests[0][1].content
