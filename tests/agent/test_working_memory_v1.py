"""Boundary invariants for the serialized Working Memory V1 flow."""

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from app.agent.executor import Executor, StalePlanError
from app.agent.planner import Planner
from app.models.resources import ResourceType
from app.models.taskstate import InvalidStateTransitionError, StepStatus, TaskState, TaskStatus
from app.models.working_memory import WorkingMemory, StepExecutionResult, ResourceInUseError


def workspace():
    wm = WorkingMemory(TaskState('user', 'goal'))
    wm.task_state.start_planning()
    model = AsyncMock()
    model.generate.return_value = '{"steps": ["inspect", "deliver"], "reason": null}'
    return wm, Planner(model), Executor(wm)


async def proposal(wm, planner):
    context = planner.build_context(wm)
    return await (planner.replan(context) if context.plan_revision else planner.plan(context))


@pytest.mark.parametrize('change', ['constraint', 'pin', 'question', 'answer'])
async def test_planning_edits_require_replan_before_next_step(change):
    wm, planner, executor = workspace()
    question_id = wm.task_state.add_open_question('detail?', False)
    old = executor.accept_plan(await proposal(wm, planner))
    token = wm.begin_step(old.next_step.step_id)
    wm.commit_step_result(StepExecutionResult(token, 'useful evidence'))
    pending_proposal = await proposal(wm, planner)
    previous = wm.task_state.snapshot()
    if change == 'constraint':
        wm.task_state.add_constraint('new budget')
    elif change == 'pin':
        wm.task_state.pin_content('new requirement', 'user')
    elif change == 'question':
        wm.task_state.add_open_question('which source?', True)
    else:
        wm.task_state.resolve_question(question_id, 'answer')
    task = wm.task_state
    assert task.status == TaskStatus.PLANNING
    assert task.goal_revision == previous.goal_revision
    assert task.plan_revision == previous.plan_revision
    assert task.state_version == previous.state_version + 1
    assert task.plan == previous.plan  # Preserve history for the replanner.
    before = wm.snapshot()
    with pytest.raises(InvalidStateTransitionError):
        task.start_step(task.plan[1].step_id)
    with pytest.raises(InvalidStateTransitionError):
        wm.begin_step(task.plan[1].step_id)
    with pytest.raises(InvalidStateTransitionError):
        wm.complete_task()
    with pytest.raises(StalePlanError):
        executor.accept_plan(pending_proposal)
    assert wm.snapshot() == before
    receipt = executor.accept_plan(await proposal(wm, planner))
    assert receipt.plan_revision == old.plan_revision + 1
    assert task.scratchpad.intermediate_results[-1].content == 'useful evidence'
    assert receipt.next_step.status == StepStatus.PENDING
    assert task.active_step_id is None


@pytest.mark.parametrize('change', ['goal', 'constraint', 'pin', 'question', 'answer'])
async def test_requirement_edits_during_active_step_are_atomic(change):
    wm, planner, executor = workspace()
    question = wm.task_state.add_open_question('detail?', False)
    receipt = executor.accept_plan(await proposal(wm, planner))
    token = wm.begin_step(receipt.next_step.step_id)
    operations = {
        'goal': lambda: wm.update_goal('new goal'),
        'constraint': lambda: wm.task_state.add_constraint('budget'),
        'pin': lambda: wm.task_state.pin_content('requirement', 'user'),
        'question': lambda: wm.task_state.add_open_question('new?', True),
        'answer': lambda: wm.task_state.resolve_question(question, 'answer'),
    }
    before = wm.snapshot()
    with pytest.raises(InvalidStateTransitionError):
        operations[change]()
    assert wm.snapshot() == before
    wm.commit_step_result(StepExecutionResult(token, 'still valid'))


@pytest.mark.parametrize('change', ['select', 'clear', 'remove', 'change_back'])
async def test_resource_selection_rejects_inflight_proposal_and_installed_plan(change):
    wm, planner, executor = workspace()
    rid = wm.resources.register('file', ResourceType.FILE)
    wm.resources.select_context(rid, 'old context')
    receipt = executor.accept_plan(await proposal(wm, planner))
    pending = await proposal(wm, planner)
    task_before = wm.task_state.snapshot()
    if change == 'select':
        wm.resources.select_context(rid, 'new context')
    elif change == 'clear':
        wm.clear_selected_contexts()
    elif change == 'remove':
        wm.remove_resource(rid)
    else:
        wm.resources.select_context(rid, 'new context')
        wm.resources.select_context(rid, 'old context')
    assert wm.task_state.snapshot() == task_before
    before = wm.snapshot()
    with pytest.raises(StalePlanError):
        executor.accept_plan(pending)
    with pytest.raises(InvalidStateTransitionError):
        wm.begin_step(receipt.next_step.step_id)
    assert wm.snapshot() == before
    revised = executor.accept_plan(await proposal(wm, planner))
    wm.begin_step(revised.next_step.step_id)


async def test_resource_selection_change_during_step_rejects_result_then_allows_replan():
    wm, planner, executor = workspace()
    receipt = executor.accept_plan(await proposal(wm, planner))
    token = wm.begin_step(receipt.next_step.step_id)
    rid = wm.resources.register('file', ResourceType.FILE)
    wm.resources.select_context(rid, 'new selection')
    before = wm.snapshot()
    with pytest.raises(InvalidStateTransitionError):
        wm.commit_step_result(StepExecutionResult(token, 'old result'))
    assert wm.snapshot() == before
    wm.record_failure('context changed', (rid,))
    executor.accept_plan(await proposal(wm, planner))


async def test_resource_changes_cannot_bypass_final_acceptance():
    wm, planner, executor = workspace()
    receipt = executor.accept_plan(await proposal(wm, planner))
    for step in receipt.steps:
        token = wm.begin_step(step.step_id)
        wm.commit_step_result(StepExecutionResult(token, 'done'))
    rid = wm.resources.register('new source', ResourceType.FILE)
    wm.resources.select_context(rid, 'new requirement evidence')
    before = wm.snapshot()
    with pytest.raises(InvalidStateTransitionError):
        wm.complete_task()
    assert wm.snapshot() == before


async def test_goal_change_clears_context_and_historical_blocker_does_not_block_completion():
    wm, planner, executor = workspace()
    wm.task_state.add_open_question('old blocker', True)
    rid = wm.resources.register('file', ResourceType.FILE)
    wm.resources.select_context(rid, 'old selection')
    executor.accept_plan(await proposal(wm, planner))
    wm.update_goal('new goal')
    assert wm.resources.get(rid).selected_context is None
    assert wm.task_state.plan_revision == 1
    receipt = executor.accept_plan(await proposal(wm, planner))
    assert receipt.plan_revision == 2 and receipt.blocking_questions == ()
    for step in receipt.steps:
        token = wm.begin_step(step.step_id)
        wm.commit_step_result(StepExecutionResult(token, 'done'))
    wm.complete_task()
    assert wm.task_state.status == TaskStatus.COMPLETED
    assert wm.task_state.scratchpad.open_questions[0].resolved_at is None


@pytest.mark.parametrize('field,value', [
    ('task_id', 'another-task'), ('goal_revision', 123),
    ('base_state_version', 123), ('base_resource_context_version', 123),
    ('base_resource_context_version', True),
])
async def test_wrong_proposal_identity_is_atomic(field, value):
    wm, planner, executor = workspace()
    item = replace(await proposal(wm, planner), **{field: value})
    before = wm.snapshot()
    with pytest.raises(TypeError if value is True else StalePlanError):
        executor.accept_plan(item)
    assert wm.snapshot() == before


def test_failure_sources_validated_before_state_transition():
    wm, _, _ = workspace()
    wm.install_plan(['inspect'])
    wm.begin_step(wm.task_state.plan[0].step_id)
    rid = wm.resources.register('log', ResourceType.TOOL_RESULT)
    before = wm.snapshot()
    with pytest.raises(KeyError):
        wm.record_failure('failed', (rid, 'missing'))
    assert wm.snapshot() == before
    wm.record_failure('failed', (f' {rid} ',))
    assert wm.task_state.scratchpad.failure_history[-1].source_refs == (rid,)
    with pytest.raises(ResourceInUseError):
        wm.remove_resource(rid)


def test_snapshot_detects_dangling_references_from_bypassed_boundary():
    wm, _, _ = workspace()
    rid = wm.resources.register('file', ResourceType.FILE)
    wm.record_intermediate_result('finding', (rid,))
    wm.resources.remove(rid)  # Unsupported bypass of remove_resource.
    before = wm.task_state.snapshot()
    with pytest.raises(KeyError):
        wm.snapshot()
    assert wm.task_state.snapshot() == before


def test_replan_cannot_install_first_plan():
    wm, _, _ = workspace()
    before = wm.snapshot()
    with pytest.raises(InvalidStateTransitionError, match='set_plan'):
        wm.task_state.replan(['initial'])
    assert wm.snapshot() == before


def test_initial_goal_normalization_makes_unchanged_update_noop():
    wm = WorkingMemory(TaskState('user', ' goal '))
    rid = wm.resources.register('file', ResourceType.FILE)
    wm.resources.select_context(rid, 'keep')
    before = wm.snapshot()
    wm.update_goal('goal')
    assert wm.snapshot() == before
