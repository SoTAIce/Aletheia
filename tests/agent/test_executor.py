from dataclasses import FrozenInstanceError, replace
from unittest.mock import AsyncMock

import pytest

from app.agent.executor import Executor, StalePlanError
from app.agent.planner import Planner
from app.models.taskstate import InvalidStateTransitionError, StepStatus, TaskState, TaskStatus
from app.models.working_memory import WorkingMemory


async def proposed_plan(wm, steps='["inspect", "deliver"]'):
    model = AsyncMock()
    model.generate.return_value = '{"steps": ' + steps + ', "reason": null}'
    planner = Planner(model)
    context = planner.build_context(wm)
    return await (planner.replan(context) if context.plan_revision else planner.plan(context))


def memory():
    wm = WorkingMemory(TaskState('user', 'goal'))
    wm.task_state.start_planning()
    return wm


async def test_planner_to_executor_installs_once_without_executing():
    wm = memory()
    executor = Executor(wm)
    proposal = await proposed_plan(wm)
    receipt = executor.accept_plan(proposal)
    assert proposal.task_id == receipt.task_id == wm.task_state.task_id
    assert receipt.state_version == proposal.base_state_version + 1
    assert receipt.plan_revision == 1
    assert receipt.next_step == receipt.steps[0]
    assert all(step.status == StepStatus.PENDING for step in receipt.steps)
    assert wm.task_state.status == TaskStatus.READY
    assert wm.task_state.active_step_id is None
    before = wm.snapshot()
    with pytest.raises(StalePlanError):
        executor.accept_plan(proposal)
    assert wm.snapshot() == before
    with pytest.raises(FrozenInstanceError):
        receipt.plan_revision = 2


@pytest.mark.parametrize('change', ['task', 'goal', 'constraint'])
async def test_stale_proposal_is_rejected_without_mutation(change):
    wm = memory()
    proposal = await proposed_plan(wm)
    if change == 'task':
        wm = memory()  # Same version and goal, different identity.
    elif change == 'goal':
        wm.update_goal('new goal')
    else:
        wm.task_state.add_constraint('new constraint')
    before = wm.snapshot()
    with pytest.raises(StalePlanError):
        Executor(wm).accept_plan(proposal)
    assert wm.snapshot() == before


@pytest.mark.parametrize('change', ['failure', 'goal', 'ready'])
async def test_replanning_preserves_evidence_and_replaces_steps(change):
    wm = memory()
    executor = Executor(wm)
    old = executor.accept_plan(await proposed_plan(wm))
    if change == 'failure':
        wm.task_state.start_step(old.next_step.step_id)
        wm.record_intermediate_result('partial evidence')
        wm.task_state.add_failure('read failed')
    elif change == 'goal':
        wm.update_goal('new goal')
    history = wm.task_state.scratchpad
    receipt = executor.accept_plan(await proposed_plan(wm, '["retry"]'))
    assert receipt.plan_revision == 2
    assert receipt.goal_revision == wm.task_state.goal_revision
    assert receipt.next_step.description == 'retry'
    assert receipt.next_step.step_id != old.next_step.step_id
    assert wm.task_state.scratchpad == history
    assert old.next_step.description == 'inspect'


async def test_receipt_surfaces_current_unresolved_blockers():
    wm = memory()
    wm.task_state.add_open_question('old blocker', True)
    wm.update_goal('new goal')
    resolved = wm.task_state.add_open_question('resolved', True)
    wm.task_state.resolve_question(resolved, 'yes')
    wm.task_state.add_open_question('optional', False)
    blocker = wm.task_state.add_open_question('need input', True)
    receipt = Executor(wm).accept_plan(await proposed_plan(wm))
    assert [q.question_id for q in receipt.blocking_questions] == [blocker]
    assert wm.task_state.active_step_id is None


@pytest.mark.parametrize('status', ['created', 'active', 'completed', 'failed'])
async def test_invalid_lifecycle_does_not_change_state(status):
    wm = memory()
    executor = Executor(wm)
    if status == 'created':
        wm = WorkingMemory(TaskState('user', 'goal'))
        executor = Executor(wm)
    else:
        receipt = executor.accept_plan(await proposed_plan(wm, '["inspect"]'))
        wm.task_state.start_step(receipt.next_step.step_id)
        if status == 'completed':
            wm.task_state.complete_step(receipt.next_step.step_id)
            wm.task_state.complete_task()
        elif status == 'failed':
            wm.task_state.fail_task('stop')
    proposal = await proposed_plan(wm)
    before = wm.snapshot()
    with pytest.raises(InvalidStateTransitionError):
        executor.accept_plan(proposal)
    assert wm.snapshot() == before


@pytest.mark.parametrize('changes,error', [
    ({'steps': ()}, ValueError),
    ({'steps': (' ',)}, ValueError),
    ({'steps': ('a', ' a ')}, ValueError),
    ({'steps': ('a', None)}, ValueError),
    ({'steps': ['a']}, TypeError),
    ({'goal_revision': True}, TypeError),
    ({'base_state_version': True}, TypeError),
])
async def test_malformed_proposal_does_not_change_state(changes, error):
    wm = memory()
    proposal = replace(await proposed_plan(wm), **changes)
    before = wm.snapshot()
    with pytest.raises(error):
        Executor(wm).accept_plan(proposal)
    assert wm.snapshot() == before


def test_invalid_input_types():
    with pytest.raises(TypeError, match='WorkingMemory'):
        Executor(None)
    with pytest.raises(TypeError, match='PlanProposal'):
        Executor(memory()).accept_plan(None)
