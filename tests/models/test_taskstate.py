from dataclasses import FrozenInstanceError, fields
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from app.models.taskstate import InvalidStateTransitionError, StepStatus, TaskState, TaskStatus


def ready():
    task = TaskState('user', 'goal')
    task.start_planning()
    task.set_plan(['first', 'second'])
    return task


def rejected(task, error, operation):
    before = task.snapshot()
    with pytest.raises(error):
        operation()
    assert task.snapshot() == before


def test_lifecycle_versions_and_timestamps():
    task = TaskState('user', 'goal')
    operations = [task.start_planning, lambda: task.set_plan(['step']),
                  lambda: task.start_step(task.plan[0].step_id),
                  lambda: task.complete_step(task.plan[0].step_id), task.complete_task]
    for index, operation in enumerate(operations, 1):
        now = datetime(2026, 9, 9, tzinfo=UTC) + timedelta(seconds=index)
        with patch('app.models.taskstate.datetime') as clock:
            clock.now.return_value = now
            operation()
        assert task.updated_at == now
        assert task.state_version == index
        if index == 4:
            assert task.status is TaskStatus.READY
    assert task.status is TaskStatus.COMPLETED
    assert task.plan_revision == task.plan[0].plan_revision == 1


def test_blocking_question_requires_resolution_before_completion():
    task = ready()
    question = task.add_open_question('approval?', True)
    for step in task.plan:
        task.start_step(step.step_id)
        task.complete_step(step.step_id)
    rejected(task, InvalidStateTransitionError, task.complete_task)
    task.resolve_question(question, 'approved')
    task.complete_task()
    assert task.status is TaskStatus.COMPLETED


@pytest.mark.parametrize('value', [[], [''], ['  '], [None], [3], 'abc', None, ('step',)])
def test_initial_plan_invalid_input_is_atomic(value):
    task = TaskState('u', 'g')
    task.start_planning()
    rejected(task, (ValueError, TypeError), lambda: task.set_plan(value))


@pytest.mark.parametrize('value', [[], [''], ['  '], [None], 'abc', None])
def test_replan_invalid_input_is_atomic(value):
    task = ready()
    rejected(task, (ValueError, TypeError), lambda: task.replan(value))


@pytest.mark.parametrize('failed', [True, False])
def test_terminal_state_rejects_all_writes(failed):
    task = ready()
    if failed:
        task.fail_task('stop')
    else:
        for step in task.plan:
            task.start_step(step.step_id)
            task.complete_step(step.step_id)
        task.complete_task()
    operations = [task.start_planning, lambda: task.set_plan(['x']),
                  lambda: task.replan(['x']), lambda: task.start_step(task.plan[0].step_id),
                  lambda: task.complete_step(task.plan[0].step_id),
                  lambda: task.update_goal('new'), lambda: task.add_intermediate_result('r'),
                  lambda: task.set_last_observation('o'), lambda: task.add_open_question('q', True),
                  lambda: task.resolve_question('unknown', 'a'), lambda: task.add_failure('f'),
                  lambda: task.fail(task.plan[0].step_id, 'f'), lambda: task.pin_content('p', 's'),
                  lambda: task.add_constraint('c'), task.complete_task, lambda: task.fail_task('f')]
    for operation in operations:
        rejected(task, InvalidStateTransitionError, operation)


def test_failure_replan_and_legacy_entry_point():
    task = ready()
    task.start_step(task.plan[0].step_id)
    rejected(task, InvalidStateTransitionError, lambda: task.replan(['replacement']))
    version = task.state_version
    old_step = task.plan[0]
    fid = task.fail(old_step.step_id, 'timeout')
    assert task.state_version == version + 1
    assert task.status is TaskStatus.PLANNING
    assert task.scratchpad.failure_history[-1].failure_id == fid
    assert task.plan[0].status is StepStatus.FAILED
    assert old_step.status is StepStatus.ACTIVE
    task.replan(['replacement'])
    assert task.plan_revision == task.plan[0].plan_revision == 2
    assert task.plan[0].step_id != old_step.step_id
    assert len(task.scratchpad.failure_history) == 1


def test_goal_change_retains_context_and_requires_new_plan():
    task = ready()
    task.add_constraint('budget')
    task.pin_content('context', 'user')
    before = task.snapshot()
    task.update_goal('new goal')
    assert task.plan == () and task.plan_goal_revision is None
    assert task.goal_revision == 2
    assert task.constraints == before.constraints
    assert task.pinned_contexts == before.pinned_contexts
    rejected(task, InvalidStateTransitionError, task.complete_task)
    task.replan(['new step'])
    assert task.plan_goal_revision == task.plan[0].goal_revision == 2
    assert task.plan_revision == 2
    unchanged = task.snapshot()
    task.update_goal('new goal')
    assert task.snapshot() == unchanged


@pytest.mark.parametrize('refs', [['mutable'], 'text', ([],), (3,), None])
def test_mutable_or_invalid_sources_rejected(refs):
    task = ready()
    rejected(task, TypeError, lambda: task.add_intermediate_result('result', refs))


def test_snapshot_remains_stable():
    task = ready()
    task.add_intermediate_result('result', ('source',))
    snapshot = task.snapshot()
    assert all(getattr(snapshot, f.name) == getattr(task, f.name) for f in fields(snapshot))
    with pytest.raises(FrozenInstanceError):
        snapshot.status = TaskStatus.FAILED
    task.start_step(task.plan[0].step_id)
    task.set_last_observation('observation')
    assert snapshot.plan[0].status is StepStatus.PENDING
    assert snapshot.scratchpad.last_observation is None
    assert snapshot.state_version < task.state_version


def test_task_failure_cancels_pending_and_preserves_history():
    task = ready()
    task.start_step(task.plan[0].step_id)
    task.fail_task('stop')
    assert [s.status for s in task.plan] == [StepStatus.FAILED, StepStatus.CANCELLED]
    assert task.active_step_id is None
    assert task.plan[0].finished_at == task.updated_at
    assert task.scratchpad.failure_history[-1].created_at == task.updated_at


@pytest.mark.parametrize('value', ['false', 0, None])
def test_blocking_must_be_boolean(value):
    task = ready()
    rejected(task, TypeError, lambda: task.add_open_question('q', value))
