"""The web page approves side-effect tool calls while the run waits (D-061)."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from app.agent.hooks import extend_deadlines, pausable_deadline
from app.agent.run_meter import RunMeter, metering
from app.agent.step_models import ModelTurn, ToolCall
from app.services.rag_agent_service import RagAgentService
from tests.test_web_api import api, outcome

CLOCK = 'get_current_time'          # stands in for a side-effect tool: any listed tool is asked about


def call(n=1):
    return ModelTurn(tool_calls=(ToolCall(f'c{n}', CLOCK, {}),))


def service(tmp_path, turns, steps=('Work',), **kw):
    planner, model = AsyncMock(), AsyncMock()
    planner.generate.return_value = json.dumps({'steps': list(steps), 'reason': None})
    model.generate.side_effect = list(turns)
    return RagAgentService(model=model, planner_model=planner, mcp_servers=(), include_knowledge=False,
                           memory_directory=tmp_path, **kw)


async def pending(web, run_id, count=1):
    """Wait until the run has asked count questions and one is open; return its id."""
    run = web.app.state.runs.get(run_id)
    async with asyncio.timeout(5):
        while sum(e['type'] == 'approval_required' for e in run.events) < count or not run.approvals:
            await asyncio.sleep(0.01)
    return next(iter(run.approvals))


async def answer(web, run_id, approval_id, decision):
    return await web.client.post(f'/api/runs/{run_id}/approvals',
                                 json={'approval_id': approval_id, 'decision': decision})


def tools(events):
    step = next(e for e in events if e['type'] == 'step')
    return [(t['tool'], t['success']) for t in step['tools']]


def approvals_on(**kw):
    return {'approval_tools': frozenset({CLOCK}), **kw}


async def test_an_allowed_call_runs_after_the_user_answers(tmp_path):
    svc = service(tmp_path, [call(), outcome('it is noon')])
    async with api(svc, **approvals_on()) as web:
        await web.send('s', 'what time is it', 'r')
        approval_id = await pending(web, 'r')
        svc.model.generate.assert_awaited_once()                  # waiting before the tool, not re-asking
        assert (await answer(web, 'r', approval_id, 'allow')).status_code == 200
        events = await web.events('r')
    kinds = [e['type'] for e in events]
    assert kinds.index('approval_required') < kinds.index('approval_resolved') < kinds.index('step')
    asked = next(e for e in events if e['type'] == 'approval_required')
    assert asked['tool'] == CLOCK and asked['args_summary'] == {} and asked['timeout_seconds'] == 300
    assert next(e for e in events if e['type'] == 'approval_resolved')['decision'] == 'allow'
    assert tools(events) == [(CLOCK, True)] and events[-1]['content'] == 'it is noon'


async def test_a_denied_call_is_not_run_and_the_model_is_told(tmp_path):
    svc = service(tmp_path, [call(), outcome('could not check')])
    async with api(svc, **approvals_on()) as web:
        await web.send('s', 'what time is it', 'r')
        await answer(web, 'r', await pending(web, 'r'), 'deny')
        events = await web.events('r')
    told = json.loads(svc.model.generate.call_args_list[1].args[0][-1].content)
    assert told['error'] == 'not executed: the user did not allow this call'
    assert tools(events) == [(CLOCK, False)]


async def test_allow_all_covers_the_rest_of_the_run(tmp_path):
    svc = service(tmp_path, [call(1), call(2), outcome('done')])
    async with api(svc, **approvals_on()) as web:
        await web.send('s', 'twice', 'r')
        await answer(web, 'r', await pending(web, 'r'), 'allow_all')
        events = await web.events('r')
    assert sum(e['type'] == 'approval_required' for e in events) == 1
    assert tools(events) == [(CLOCK, True)] * 2


async def test_no_answer_in_time_is_a_denial(tmp_path):
    svc = service(tmp_path, [call(), outcome('gave up')])
    async with api(svc, **approvals_on(approval_wait_seconds=0.05)) as web:
        await web.send('s', 'what time is it', 'r')
        events = await web.events('r')
    assert next(e for e in events if e['type'] == 'approval_resolved')['decision'] == 'timeout'
    told = json.loads(svc.model.generate.call_args_list[1].args[0][-1].content)
    assert 'did not answer within 0.05 seconds' in told['error']


async def test_waiting_for_the_user_does_not_use_up_the_step_time(tmp_path):
    svc = service(tmp_path, [call(), outcome('in time')], step_timeout_seconds=0.3)
    async with api(svc, **approvals_on(approval_wait_seconds=5)) as web:
        await web.send('s', 'what time is it', 'r')
        approval_id = await pending(web, 'r')
        await asyncio.sleep(0.6)                                    # longer than the whole step allows
        await answer(web, 'r', approval_id, 'allow')
        events = await web.events('r')
    assert events[-1]['type'] == 'completed' and events[-1]['content'] == 'in time'


async def test_bad_or_late_answers_are_refused(tmp_path):
    svc = service(tmp_path, [call(), outcome('done')])
    async with api(svc, **approvals_on()) as web:
        await web.send('s', 'what time is it', 'r')
        approval_id = await pending(web, 'r')
        bad = await answer(web, 'r', approval_id, 'maybe')
        assert bad.status_code == 400 and bad.json()['code'] == 'invalid_input'
        unknown = await answer(web, 'r', 'not-an-id', 'allow')
        assert unknown.status_code == 409 and unknown.json()['code'] == 'approval_not_pending'
        assert (await answer(web, 'nope', approval_id, 'allow')).status_code == 404
        await answer(web, 'r', approval_id, 'allow')
        await web.events('r')
        late = await answer(web, 'r', approval_id, 'deny')
        assert late.status_code == 409


async def test_cancelling_a_waiting_run_ends_the_wait(tmp_path):
    svc = service(tmp_path, [call(), outcome('never')])
    async with api(svc, **approvals_on()) as web:
        await web.send('s', 'what time is it', 'r')
        await pending(web, 'r')
        state = (await web.client.post('/api/runs/r/cancel')).json()
        assert state['status'] == 'cancelled'
        assert web.app.state.runs.get('r').approvals == {}


async def test_without_approval_tools_nothing_is_asked(tmp_path):
    svc = service(tmp_path, [call(), outcome('noon')])
    async with api(svc) as web:
        await web.send('s', 'what time is it', 'r')
        events = await web.events('r')
        assert (await web.client.get('/api/health')).json()['approval_tools'] == []
    assert not any(e['type'].startswith('approval') for e in events)


async def test_deadlines_and_the_time_budget_move_together():
    meter = RunMeter(timeout_seconds=10)
    with metering(meter):
        async with asyncio.timeout(5) as deadline:
            with pausable_deadline(deadline):
                before, started = deadline.when(), meter.started
                extend_deadlines(30)
                assert deadline.when() == pytest.approx(before + 30) and meter.started == pytest.approx(started + 30)
                extend_deadlines(-25)
                assert deadline.when() == pytest.approx(before + 5)
        extend_deadlines(100)                       # nothing registered any more: only the meter moves
    assert meter.started == pytest.approx(started + 105)
