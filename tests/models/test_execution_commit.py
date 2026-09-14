from dataclasses import replace

import pytest

from app.models.resources import ResourceType
from app.models.taskstate import TaskState, TaskStatus, StepStatus
from app.models.working_memory import (
    WorkingMemory, StepExecutionResult, StaleExecutionError, ResourceInUseError,
)


def executing():
    wm = WorkingMemory(TaskState('user', 'goal'))
    wm.task_state.start_planning()
    wm.install_plan(['inspect', 'deliver'])
    token = wm.begin_step(wm.task_state.plan[0].step_id)
    return wm, token


def test_commit_publishes_evidence_and_completion_with_one_version_change():
    wm, token = executing()
    resource = wm.resources.register('tool://output', ResourceType.TOOL_RESULT)
    output = StepExecutionResult(token, 'finding', 'observed', (f' {resource} ',))
    before = wm.snapshot()
    result_id = wm.commit_step_result(output)
    task = wm.task_state
    assert task.state_version == token.state_version + 1
    assert task.status == TaskStatus.READY and task.active_step_id is None
    assert task.plan[0].status == StepStatus.COMPLETED
    assert task.plan[1].status == StepStatus.PENDING
    assert task.plan[0].result_summary == 'finding'
    result = task.scratchpad.intermediate_results[-1]
    observation = task.scratchpad.last_observation
    assert result.result_id == result_id
    assert result.step_id == observation.step_id == token.step_id
    assert result.source_refs == observation.source_refs == (resource,)
    assert result.goal_revision == token.goal_revision
    assert result.plan_revision == token.plan_revision
    assert before.task_state.plan[0].status == StepStatus.ACTIVE
    with pytest.raises(ResourceInUseError):
        wm.remove_resource(resource)
    after = wm.snapshot()
    with pytest.raises(StaleExecutionError):
        wm.commit_step_result(output)
    assert wm.snapshot() == after


@pytest.mark.parametrize('changes,error', [
    ({'content': ''}, ValueError),
    ({'content': None}, ValueError),
    ({'observation': ' '}, ValueError),
    ({'source_refs': ('missing',)}, KeyError),
    ({'source_refs': ['missing']}, TypeError),
])
def test_invalid_output_is_atomic_and_can_be_corrected(changes, error):
    wm, token = executing()
    result = StepExecutionResult(token, 'finding', 'observation')
    before = wm.snapshot()
    with pytest.raises(error):
        wm.commit_step_result(replace(result, **changes))
    assert wm.snapshot() == before
    wm.commit_step_result(result)


@pytest.mark.parametrize('field,value', [
    ('task_id', 'other'), ('goal_revision', 9), ('plan_revision', 9),
    ('step_id', 'other'), ('execution_id', 'other'), ('state_version', 999),
])
def test_wrong_identity_does_not_change_memory(field, value):
    wm, token = executing()
    before = wm.snapshot()
    with pytest.raises(StaleExecutionError):
        wm.commit_step_result(StepExecutionResult(replace(token, **{field: value}), 'x'))
    assert wm.snapshot() == before


@pytest.mark.parametrize('change', ['evidence', 'replan', 'goal', 'terminal', 'next_step'])
def test_late_result_cannot_write_into_new_state(change):
    wm, token = executing()
    if change == 'evidence':
        wm.record_observation('new evidence')
    elif change == 'terminal':
        wm.task_state.fail_task('stop')
    elif change == 'next_step':
        wm.commit_step_result(StepExecutionResult(token, 'first'))
        wm.begin_step(wm.task_state.plan[1].step_id)
    else:
        wm.task_state.add_failure('failed')
        if change == 'goal':
            wm.update_goal('new goal')
        wm.install_plan(['retry'])
        wm.begin_step(wm.task_state.plan[0].step_id)
    before = wm.snapshot()
    with pytest.raises(StaleExecutionError):
        wm.commit_step_result(StepExecutionResult(token, 'late'))
    assert wm.snapshot() == before


def test_other_workspace_rejects_token():
    _, token = executing()
    other, _ = executing()
    before = other.snapshot()
    with pytest.raises(StaleExecutionError):
        other.commit_step_result(StepExecutionResult(token, 'wrong task'))
    assert other.snapshot() == before


def test_task_staging_failure_does_not_publish_observation_or_result():
    wm, token = executing()
    before = wm.snapshot()
    with pytest.raises(KeyError):
        wm.task_state.commit_step_result('missing', token.state_version, 'x', 'observed')
    assert wm.snapshot() == before


def test_commit_without_observation_preserves_previous_observation():
    wm, token = executing()
    wm.commit_step_result(StepExecutionResult(token, 'first', 'first observation'))
    observation = wm.task_state.scratchpad.last_observation
    token = wm.begin_step(wm.task_state.plan[1].step_id)
    wm.commit_step_result(StepExecutionResult(token, 'second'))
    assert wm.task_state.scratchpad.last_observation == observation
    assert wm.task_state.status == TaskStatus.READY
