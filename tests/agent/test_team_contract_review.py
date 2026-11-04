import asyncio
from dataclasses import FrozenInstanceError
import json

import pytest

from app.agent.step_models import ModelTurn, ToolCall
from app.agent.team_poc.contracts import JsonContract, ContractError
from app.agent.team_poc.session import TeamSession, SessionError
from test_team_session_poc import ScriptedModel, ReadTool, factory, call


RESULT = JsonContract('host-result', (('nonce', str), ('amount', int), ('chosen_plan', str)))


def submit(identity, **payload):
    return call(identity, 'submit_result', payload=payload)


async def next_event(queue, identity, kind):
    async with asyncio.timeout(3):
        while True:
            event = await queue.get()
            if event.session_id == identity and event.kind == kind:
                return event


def test_contract_exact_keys_strict_types_and_immutable_fields():
    value = {'nonce': 'n', 'amount': 7, 'chosen_plan': 'beta'}
    assert json.loads(RESULT.serialize(value)) == value
    for invalid in [dict(value, extra=True), dict(value, amount=True), dict(value, amount='7'), {'amount': 7}]:
        with pytest.raises(ContractError):
            RESULT.validate(invalid)
    with pytest.raises(FrozenInstanceError):
        RESULT.fields = ()


async def test_contracted_session_repairs_text_without_reopening_business_tools():
    read = ReadTool()
    model = ScriptedModel(call('read'), ModelTurn('Lead asked for prose: amount is 7'),
        submit('fixed', nonce='n', amount=7, chosen_plan='beta'))
    session = TeamSession('member', model, factory(read), asyncio.Queue(), 'Read then report', output_contract=RESULT)
    result = await session.run()
    assert json.loads(result.content) == {'nonce': 'n', 'amount': 7, 'chosen_plan': 'beta'}
    assert len(read.calls) == 1 and session.format_repairs == 1
    assert session.request_tool_names[-1] == ('submit_result',)


async def test_bad_structured_submission_is_rejected_and_never_completed():
    model = ScriptedModel(submit('bad', nonce='n', amount=True, chosen_plan='beta'),
                          submit('bad-again', nonce='n', amount='7', chosen_plan='beta'))
    session = TeamSession('member', model, factory(), asyncio.Queue(), 'goal',
                          output_contract=RESULT, max_format_repairs=1)
    with pytest.raises(SessionError, match='format repair limit'):
        await session.run()
    assert not any(e.kind == 'completed' for e in session.events)


async def test_review_rework_keeps_history_and_resources_until_host_accepts():
    events, owners, read = asyncio.Queue(), [], ReadTool()
    model = ScriptedModel(call('read'), submit('draft1', nonce='n', amount=8, chosen_plan='beta'),
                          submit('draft2', nonce='n', amount=7, chosen_plan='beta'))
    session = TeamSession('worker', model, factory(read, owners), events, 'goal',
                          output_contract=RESULT, reviewable=True)
    task = asyncio.create_task(session.run())
    try:
        first = await next_event(events, 'worker', 'candidate_submitted')
        assert not task.done() and len(owners) == 1
        session.revise(first.version, 'Review rejected amount 8: correct to 7. Lead also asks for prose.')
        second = await next_event(events, 'worker', 'candidate_submitted')
        with pytest.raises(SessionError):
            session.accept(first.version)
        session.accept(second.version)
        result = await asyncio.wait_for(task, 3)
        assert json.loads(result.content)['amount'] == 7
        assert len(read.calls) == 1 and owners == [('open', task), ('close', task)]
    finally:
        task.cancel() if not task.done() else None
        await asyncio.gather(task, return_exceptions=True)


async def test_review_wait_cancellation_closes_resources():
    events, owners = asyncio.Queue(), []
    session = TeamSession('worker', ScriptedModel(submit('one', nonce='n', amount=7, chosen_plan='beta')),
                          factory(owners=owners), events, 'goal', output_contract=RESULT, reviewable=True)
    task = asyncio.create_task(session.run())
    await next_event(events, 'worker', 'candidate_submitted')
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert owners == [('open', task), ('close', task)]


async def test_revision_does_not_make_old_candidate_acceptable_as_new_version():
    events = asyncio.Queue()
    session = TeamSession('worker', ScriptedModel(submit('one', nonce='n', amount=7, chosen_plan='beta'),
        submit('two', nonce='n', amount=7, chosen_plan='beta')), factory(), events, 'goal',
        output_contract=RESULT, reviewable=True)
    task = asyncio.create_task(session.run())
    try:
        await next_event(events, 'worker', 'candidate_submitted')
        session.revise(1, 'revise before accepting')
        with pytest.raises(SessionError):
            session.accept(2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_repair_cannot_call_business_tool_or_change_host_contract():
    read = ReadTool()
    session = TeamSession('worker', ScriptedModel(ModelTurn('prose'), call('illegal-read')),
                          factory(read), asyncio.Queue(), 'goal', output_contract=RESULT)
    with pytest.raises(AttributeError):
        session.output_contract = JsonContract('other', (('anything', str),))
    with pytest.raises(SessionError, match='not permitted'):
        await session.run()
    assert not read.calls


def final_contract():
    def check(payload):
        if payload != {'nonce': 'n', 'amount': 7, 'chosen_plan': 'beta'}:
            raise ContractError('host facts do not match')
    return JsonContract('host-final', RESULT.fields, check)


def lead_decision(identity, action='revise'):
    return submit(identity, action=action, instruction='Continue; fix amount from evidence if needed. Report prose internally.',
                  review_focus='Check current draft against original facts; detailed prose feedback is welcome.')


async def test_same_team_handles_two_reports_review_rejection_rework_and_acceptance():
    from app.agent.team_poc.reviews import ReviewTrial, DECISION_PROTOCOL, REVIEW_PROTOCOL
    events, read, owners, lead_owners = asyncio.Queue(), ReadTool(), [], []
    worker = TeamSession('worker', ScriptedModel(call('read'),
        call('ask1', 'request_coordination', issue='need alternative'),
        call('ask2', 'request_coordination', issue='need confirmation'),
        submit('draft1', nonce='n', amount=8, chosen_plan='beta'),
        submit('draft2', nonce='n', amount=7, chosen_plan='beta')),
        factory(read, owners), events, 'original task', output_contract=RESULT, reviewable=True)
    lead = TeamSession('lead', ScriptedModel(lead_decision('l1'), lead_decision('l2'),
        lead_decision('l3'), lead_decision('l4', 'accept')), factory(owners=lead_owners), events, 'organize',
        output_contract=DECISION_PROTOCOL, reviewable=True)
    seen = []
    def reviewer(candidate, focus):
        seen.append(candidate.version)
        good = json.loads(candidate.content)['amount'] == 7
        return TeamSession('review-' + str(candidate.version), ScriptedModel(submit('r',
            candidate_version=candidate.version, decision='approve' if good else 'revise', feedback='amount check')),
            factory(), events, focus, output_contract=REVIEW_PROTOCOL)
    trial = ReviewTrial(worker, lead, reviewer, final_contract(), max_cycles=4)
    result = await asyncio.wait_for(trial.run(), 4)
    assert json.loads(result.content) == {'nonce': 'n', 'amount': 7, 'chosen_plan': 'beta'}
    assert worker.version == 4 and lead.version == 4 and seen == [3, 4]
    assert len(read.calls) == 1 and owners[0][1] is owners[1][1]
    assert trial.cycles == 4 and len(trial.reviews) == 2
    assert len(lead_owners) == 2 and lead_owners[0][1] is lead_owners[1][1]


async def test_stale_review_cannot_accept_current_candidate():
    from app.agent.team_poc.reviews import ReviewTrial, DECISION_PROTOCOL, REVIEW_PROTOCOL
    events = asyncio.Queue()
    worker = TeamSession('worker', ScriptedModel(submit('w', nonce='n', amount=7, chosen_plan='beta')),
                         factory(), events, 'task', output_contract=RESULT, reviewable=True)
    lead = TeamSession('lead', ScriptedModel(), factory(), events, 'lead',
                       output_contract=DECISION_PROTOCOL, reviewable=True)
    def reviewer(candidate, focus):
        return TeamSession('review', ScriptedModel(submit('r', candidate_version=0, decision='approve', feedback='old')),
                           factory(), events, focus, output_contract=REVIEW_PROTOCOL)
    with pytest.raises(SessionError, match='stale review'):
        await asyncio.wait_for(ReviewTrial(worker, lead, reviewer, final_contract()).run(), 3)
    assert worker.status == 'cancelled'


async def test_host_rejects_wrong_facts_even_if_reviewer_and_lead_approve():
    from app.agent.team_poc.reviews import ReviewTrial, DECISION_PROTOCOL, REVIEW_PROTOCOL
    events = asyncio.Queue()
    worker = TeamSession('worker', ScriptedModel(submit('w', nonce='n', amount=999, chosen_plan='beta')),
                         factory(), events, 'task', output_contract=RESULT, reviewable=True)
    lead = TeamSession('lead', ScriptedModel(lead_decision('l', 'accept')), factory(), events, 'lead',
                       output_contract=DECISION_PROTOCOL, reviewable=True)
    def reviewer(candidate, focus):
        return TeamSession('review', ScriptedModel(submit('r', candidate_version=candidate.version,
                           decision='approve', feedback='looks good')), factory(), events, focus,
                           output_contract=REVIEW_PROTOCOL)
    with pytest.raises(SessionError, match='acceptance gate'):
        await asyncio.wait_for(ReviewTrial(worker, lead, reviewer, final_contract()).run(), 3)
    assert worker.status == 'cancelled'


@pytest.mark.parametrize('cancel_phase', ['lead', 'review'])
async def test_trial_cancellation_joins_lead_reviewer_and_worker(cancel_phase):
    from app.agent.team_poc.reviews import ReviewTrial, DECISION_PROTOCOL, REVIEW_PROTOCOL
    events, entered = asyncio.Queue(), asyncio.Event()
    owners = []
    async def hold(messages):
        entered.set()
        await asyncio.Event().wait()
    response = call('ask', 'request_coordination', issue='help') if cancel_phase == 'lead' else submit(
        'w', nonce='n', amount=7, chosen_plan='beta')
    worker = TeamSession('worker', ScriptedModel(response), factory(owners=owners), events, 'task',
                         output_contract=RESULT, reviewable=True)
    lead = TeamSession('lead', ScriptedModel(hold), factory(owners=owners), events, 'lead',
                       output_contract=DECISION_PROTOCOL, reviewable=True)
    reviewers = []
    def reviewer(candidate, focus):
        r = TeamSession('review', ScriptedModel(hold), factory(owners=owners), events, focus,
                        output_contract=REVIEW_PROTOCOL)
        reviewers.append(r)
        return r
    task = asyncio.create_task(ReviewTrial(worker, lead, reviewer, final_contract()).run())
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert worker.status == 'cancelled'
    assert all(r.status == 'cancelled' for r in reviewers)
    assert sum(o[0] == 'open' for o in owners) == sum(o[0] == 'close' for o in owners)


async def test_worker_timeout_interrupts_reviewer_immediately():
    from app.agent.team_poc.reviews import ReviewTrial, DECISION_PROTOCOL, REVIEW_PROTOCOL
    events, reviewer_cancelled = asyncio.Queue(), asyncio.Event()
    worker = TeamSession('worker', ScriptedModel(submit('w', nonce='n', amount=7, chosen_plan='beta')),
                         factory(), events, 'task', output_contract=RESULT, reviewable=True, timeout_seconds=0.05)
    lead = TeamSession('lead', ScriptedModel(), factory(), events, 'lead',
                       output_contract=DECISION_PROTOCOL, reviewable=True)
    async def hold(messages):
        try:
            await asyncio.Event().wait()
        finally:
            reviewer_cancelled.set()
    def reviewer(candidate, focus):
        return TeamSession('review', ScriptedModel(hold), factory(), events, focus, output_contract=REVIEW_PROTOCOL)
    trial = asyncio.create_task(ReviewTrial(worker, lead, reviewer, final_contract()).run())
    try:
        await asyncio.wait_for(reviewer_cancelled.wait(), 0.5)
        with pytest.raises(TimeoutError):
            await trial
    finally:
        if not trial.done():
            trial.cancel()
        await asyncio.gather(trial, return_exceptions=True)


async def test_cycle_limit_and_reviewer_veto_fail_closed():
    from app.agent.team_poc.reviews import ReviewTrial, DECISION_PROTOCOL, REVIEW_PROTOCOL
    for mode in ('limit', 'veto'):
        events = asyncio.Queue()
        response = call('ask', 'request_coordination', issue='help') if mode == 'limit' else submit(
            'w', nonce='n', amount=7, chosen_plan='beta')
        worker = TeamSession('worker', ScriptedModel(response), factory(), events, 'task',
                             output_contract=RESULT, reviewable=True)
        lead = TeamSession('lead', ScriptedModel(lead_decision('l', 'revise' if mode == 'limit' else 'accept')),
                           factory(), events, 'lead', output_contract=DECISION_PROTOCOL, reviewable=True)
        def reviewer(candidate, focus):
            return TeamSession('review', ScriptedModel(submit('r', candidate_version=candidate.version,
                decision='revise', feedback='independent objection')), factory(), events, focus,
                output_contract=REVIEW_PROTOCOL)
        with pytest.raises(SessionError, match='cycle limit' if mode == 'limit' else 'acceptance gate'):
            await asyncio.wait_for(ReviewTrial(worker, lead, reviewer, final_contract(), max_cycles=1).run(), 2)
        assert worker.status == 'cancelled' and lead.status == 'cancelled'


async def test_format_repair_budget_is_cumulative_across_review_revisions():
    events = asyncio.Queue()
    worker = TeamSession('worker', ScriptedModel(ModelTurn('first prose'),
        submit('one', nonce='n', amount=7, chosen_plan='beta'), ModelTurn('second prose')),
        factory(), events, 'task', output_contract=RESULT, reviewable=True, max_format_repairs=1)
    task = asyncio.create_task(worker.run())
    await next_event(events, 'worker', 'candidate_submitted')
    worker.revise(1, 'change work, output must remain structured')
    with pytest.raises(SessionError, match='format repair limit'):
        await asyncio.wait_for(task, 2)


@pytest.mark.parametrize('cancel_review', [False, True])
async def test_contracted_review_wait_preserves_real_mcp_owner_and_completed_read(tmp_path, cancel_review):
    from contextlib import asynccontextmanager
    from app.models.taskstate import TaskState
    from app.models.working_memory import WorkingMemory
    from app.tools.tool_loader import open_tool_registry, local_mcp_servers
    (tmp_path / 'facts.txt').write_text('source amount 7', encoding='utf-8')
    owners, adapters, events = [], [], asyncio.Queue()
    @asynccontextmanager
    async def tools():
        owners.append(asyncio.current_task())
        try:
            async with open_tool_registry(WorkingMemory(TaskState('synthetic', 'contract-test')),
                    include_knowledge=False, mcp_servers=local_mcp_servers(filesystem_root=tmp_path)) as registry:
                adapters.append(registry.get('read_file'))
                yield registry
        finally:
            owners.append(asyncio.current_task())
    worker = TeamSession('worker', ScriptedModel(call('read', 'read_file', path='facts.txt'),
        submit('draft', nonce='n', amount=8, chosen_plan='beta'),
        submit('fixed', nonce='n', amount=7, chosen_plan='beta')), tools, events, 'task',
        output_contract=RESULT, reviewable=True)
    task = asyncio.create_task(worker.run())
    try:
        await next_event(events, 'worker', 'candidate_submitted')
        assert len(owners) == 1
        if cancel_review:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
        else:
            worker.revise(1, 'fix amount from retained evidence')
            await next_event(events, 'worker', 'candidate_submitted')
            worker.accept(2)
            assert json.loads((await asyncio.wait_for(task, 3)).content)['amount'] == 7
        assert owners == [task, task]
        assert sum(r.tool_name == 'read_file' for r in worker.tool_results) == 1
        with pytest.raises(RuntimeError, match='closed'):
            await adapters[0].invoke({'path': 'facts.txt'})
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
