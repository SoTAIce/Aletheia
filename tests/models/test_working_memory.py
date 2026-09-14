from dataclasses import FrozenInstanceError, replace

import pytest

from app.models.resources import Resources, ResourceType
from app.models.taskstate import InvalidStateTransitionError, TaskState, TaskStatus
from app.models.working_memory import ResourceInUseError, WorkingMemory


def workspace() -> WorkingMemory:
    return WorkingMemory(TaskState('user', 'goal'))


def test_composition_snapshot_and_isolation():
    task, resources = TaskState('user', 'goal'), Resources()
    wm = WorkingMemory(task, resources)
    assert wm.task_state is task and wm.resources is resources
    resource_id = resources.register('file.txt', ResourceType.FILE)
    before = wm.snapshot()
    assert wm.snapshot() == before
    with pytest.raises(FrozenInstanceError):
        before.resources = ()
    with pytest.raises(FrozenInstanceError):
        before.task_state.active_goal = 'changed'
    with pytest.raises(FrozenInstanceError):
        before.resources[0].loaded = True
    with pytest.raises(AttributeError):
        wm.task_state = TaskState('other', 'other')
    with pytest.raises(AttributeError):
        wm.resources = Resources()
    resources.select_context(resource_id, 'selected')
    wm.update_goal('new goal')
    assert before.task_state.active_goal == 'goal'
    assert before.resources[0].selected_context is None
    assert workspace().resources.list_all() == []


def test_lifecycle_and_goal_change():
    wm = workspace()
    task = wm.task_state
    task.start_planning()
    task.set_plan(['inspect'])
    step_id = task.plan[0].step_id
    task.start_step(step_id)
    resource_id = wm.resources.register('store://result', ResourceType.TOOL_RESULT)
    wm.resources.mark_loaded(resource_id, 'summary')
    wm.resources.select_context(resource_id, 'evidence')
    observation_id = wm.record_observation('observed', (f' {resource_id} ',))
    result_id = wm.record_intermediate_result('conclusion', (resource_id,))
    assert task.scratchpad.last_observation.observation_id == observation_id
    assert task.scratchpad.last_observation.source_refs == (resource_id,)
    assert task.scratchpad.intermediate_results[0].result_id == result_id
    assert task.scratchpad.intermediate_results[0].step_id == step_id
    task.complete_step(step_id)
    before = wm.snapshot()
    wm.update_goal(' goal ')
    assert wm.snapshot() == before
    wm.update_goal('new goal')
    assert task.status is TaskStatus.PLANNING
    assert task.goal_revision == 2 and task.plan == ()
    resource = wm.resources.get(resource_id)
    assert resource.loaded and resource.summary == 'summary'
    assert resource.selected_context is None
    assert resource.created_at == before.resources[0].created_at
    with pytest.raises(ResourceInUseError):
        wm.remove_resource(resource_id)
    task.replan(['finish'])
    task.start_step(task.plan[0].step_id)
    task.complete_step(task.plan[0].step_id)
    task.complete_task()
    assert wm.snapshot().task_state.status is TaskStatus.COMPLETED


@pytest.mark.parametrize('state', ['active', 'completed', 'failed', 'invalid'])
def test_rejected_goal_change_preserves_both_children(state):
    wm = workspace()
    rid = wm.resources.register('file', ResourceType.FILE)
    wm.resources.select_context(rid, 'keep')
    task = wm.task_state
    if state == 'failed':
        task.fail_task('stop')
    elif state != 'invalid':
        task.start_planning()
        task.set_plan(['step'])
        task.start_step(task.plan[0].step_id)
        if state == 'completed':
            task.complete_step(task.plan[0].step_id)
            task.complete_task()
    before = wm.snapshot()
    with pytest.raises(ValueError if state == 'invalid' else InvalidStateTransitionError):
        wm.update_goal(' ' if state == 'invalid' else 'new')
    assert wm.snapshot() == before


@pytest.mark.parametrize('method', ['record_observation', 'record_intermediate_result'])
def test_record_validation_is_atomic(method):
    wm = workspace()
    rid = wm.resources.register('file', ResourceType.FILE)
    write = getattr(wm, method)
    for refs, error in [((rid, 'missing'), KeyError), (('',), ValueError),
                        (['id'], TypeError), ('id', TypeError),
                        ((None,), TypeError), (None, TypeError)]:
        before = wm.snapshot()
        with pytest.raises(error):
            write('content', refs)
        assert wm.snapshot() == before
    before = wm.snapshot()
    with pytest.raises(ValueError):
        write(' ', (rid,))
    assert wm.snapshot() == before
    assert isinstance(write('no sources'), str)
    wm.task_state.fail_task('done')
    before = wm.snapshot()
    with pytest.raises(InvalidStateTransitionError):
        write('content', (rid,))
    assert wm.snapshot() == before


@pytest.mark.parametrize('kind', ['observation', 'result', 'invalid_result', 'failure'])
def test_removal_protects_all_retained_records(kind):
    wm = workspace()
    rid = wm.resources.register('file', ResourceType.FILE)
    if kind == 'observation':
        wm.record_observation('evidence', (rid,))
    elif kind in ('result', 'invalid_result'):
        wm.record_intermediate_result('result', (rid,))
        if kind == 'invalid_result':
            # No public invalidation API: simulate a retained historical record.
            scratchpad = wm.task_state.scratchpad
            wm.task_state._scratchpad = replace(scratchpad, intermediate_results=(
                replace(scratchpad.intermediate_results[0], valid=False),
            ))
    else:
        task = wm.task_state
        task.start_planning()
        task.set_plan(['step'])
        task.start_step(task.plan[0].step_id)
        wm.record_failure('failed', (rid,))
    before = wm.snapshot()
    with pytest.raises(ResourceInUseError):
        wm.remove_resource(f' {rid} ')
    assert wm.snapshot() == before


def test_unreferenced_removal_and_selection_clear():
    wm = workspace()
    rid = wm.resources.register('file', ResourceType.FILE)
    wm.resources.mark_loaded(rid, 'summary')
    wm.resources.select_context(rid, 'selected')
    task_before = wm.task_state.snapshot()
    assert wm.clear_selected_contexts() == 1
    assert wm.clear_selected_contexts() == 0
    assert wm.task_state.snapshot() == task_before
    assert wm.resources.get(rid).summary == 'summary'
    wm.record_observation('old', (rid,))
    wm.record_observation('replacement')
    assert wm.remove_resource(f' {rid} ').resource_id == rid
    with pytest.raises(KeyError):
        wm.remove_resource(rid)
    assert wm.resources.list_all() == []


def test_constructor_rejects_invalid_children_and_dangling_refs():
    with pytest.raises(TypeError):
        WorkingMemory(None)
    with pytest.raises(TypeError):
        WorkingMemory(TaskState('user', 'goal'), [])
    task = TaskState('user', 'goal')
    resources = Resources()
    rid = resources.register('file', ResourceType.FILE)
    task.set_last_observation('existing', (rid,))
    before = task.snapshot()
    with pytest.raises(KeyError):
        WorkingMemory(task)
    assert task.snapshot() == before
    assert WorkingMemory(task, resources).snapshot().task_state == before
