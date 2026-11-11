"""RunState contracts, including error compatibility with TaskState."""

from dataclasses import FrozenInstanceError, fields, replace
from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.models.run_state import RunMemory, RunState, RunStateSnapshot
from app.models.resources import Resources
from app.models.taskstate import (
    Constraint, FailureRecord, InvalidStateTransitionError, OpenQuestion,
    PinnedContext, Scratchpad, TaskState, TaskStatus,
)


@pytest.fixture
def state():
    return RunState(" user ", " goal ")


def rejected(state, method, args, error, message):
    """Rejected mutations must preserve the entire observable state."""
    before = state.snapshot()
    with pytest.raises(error) as caught:
        getattr(state, method)(*args)
    assert type(caught.value) is error
    assert str(caught.value) == message
    assert state.snapshot() == before


def parity(state, legacy, method, args):
    with pytest.raises(Exception) as expected:
        getattr(legacy, method)(*args)
    rejected(state, method, args, type(expected.value), str(expected.value))


def changed(state, method, *args):
    before = state.snapshot()
    result = getattr(state, method)(*args)
    assert state.state_version == before.state_version + 1
    assert state.updated_at >= before.updated_at
    assert state.updated_at.tzinfo is UTC
    assert state.created_at == before.created_at
    assert state.goal_revision == 1
    assert state.scratchpad.intermediate_results == ()
    if method == "block":
        assert state.scratchpad.last_observation.content == args[0].strip()
    else:
        assert state.scratchpad.last_observation == before.scratchpad.last_observation
    return result


def questions(state, *ids):
    # Multiple unresolved questions cannot currently be created by the public
    # loop API. Seed immutable records to exercise the aggregate's guard.
    state._scratchpad = Scratchpad(open_questions=tuple(
        OpenQuestion(qid, "question", "step", 1, True, datetime.now(UTC))
        for qid in ids
    ))


def test_constructor_and_independent_instances(state):
    assert str(UUID(state.task_id)) == state.task_id
    assert state.task_id != RunState("user", "goal").task_id
    assert state.user_id == " user "
    assert state.active_goal == "goal"
    assert state.status is TaskStatus.CREATED
    assert state.goal_revision == 1
    assert state.state_version == 0
    assert state.created_at == state.updated_at
    assert state.created_at.tzinfo is UTC
    assert state.active_step_id is None
    assert state.scratchpad == Scratchpad()
    assert state.constraints == state.pinned_contexts == ()


@pytest.mark.parametrize("value", [None, 7, "", "  "])
@pytest.mark.parametrize("field", ["user_id", "active_goal"])
def test_constructor_validation_matches_taskstate(field, value):
    args = {"user_id": "user", "active_goal": "goal", field: value}
    with pytest.raises(ValueError) as expected:
        TaskState(**args)
    with pytest.raises(ValueError) as actual:
        RunState(**args)
    assert str(actual.value) == str(expected.value)


@pytest.mark.parametrize("method,args,record_type", [
    ("pin_content", (" content ", " source "), PinnedContext),
    ("add_constraint", (" constraint ",), Constraint),
])
@pytest.mark.parametrize("status", [TaskStatus.CREATED, TaskStatus.READY, TaskStatus.BLOCKED])
def test_planning_inputs_append_without_changing_status(state, method, args, record_type, status):
    state._status = status
    first = changed(state, method, *args)
    old = state.snapshot()
    second = changed(state, method, *args)
    records = state.pinned_contexts if method == "pin_content" else state.constraints
    assert first != second
    assert isinstance(records[0], record_type)
    assert records[0].content == args[0]
    assert records[0].created_at.tzinfo is UTC
    if method == "pin_content":
        assert [r.context_id for r in records] == [first, second]
        assert records[0].source == args[1]
        assert len(old.pinned_contexts) == 1
    else:
        assert [r.constraint_id for r in records] == [first, second]
        assert len(old.constraints) == 1
    assert state.status is status


@pytest.mark.parametrize("method,args", [
    ("pin_content", (None, "source")), ("pin_content", ("", "source")),
    ("pin_content", ("  ", "source")), ("pin_content", (3, "source")),
    ("pin_content", ("content", None)), ("pin_content", ("content", "")),
    ("pin_content", ("content", "  ")), ("pin_content", ("content", 3)),
    ("add_constraint", (None,)), ("add_constraint", ("",)),
    ("add_constraint", ("  ",)), ("add_constraint", (3,)),
    ("resolve_question", ("q", None)), ("resolve_question", ("q", "")),
    ("resolve_question", ("q", "  ")), ("resolve_question", ("q", 3)),
    ("add_failure", (None,)), ("add_failure", ("",)),
    ("add_failure", ("  ",)), ("add_failure", (3,)),
    ("add_failure", ("failure", [])), ("add_failure", ("failure", "ref")),
    ("add_failure", ("failure", (1,))), ("add_failure", ("failure", None)),
    ("fail_task", (None,)), ("fail_task", ("",)),
    ("fail_task", ("  ",)), ("fail_task", (3,)),
])
@pytest.mark.parametrize("status", [TaskStatus.CREATED, TaskStatus.RUNNING, TaskStatus.COMPLETED])
def test_argument_errors_match_taskstate_and_precede_status(state, method, args, status):
    legacy = TaskState("user", "goal")
    state._status = legacy._status = status
    state._active_step_id = legacy._active_step_id = "step" if status is TaskStatus.RUNNING else None
    parity(state, legacy, method, args)


@pytest.mark.parametrize("status", [TaskStatus.COMPLETED, TaskStatus.FAILED])
@pytest.mark.parametrize("method,args", [
    ("pin_content", ("content", "source")), ("add_constraint", ("constraint",)),
    ("resolve_question", ("missing", "answer")), ("fail_task", ("failure",)),
])
def test_terminal_errors_match_taskstate(state, status, method, args):
    legacy = TaskState("user", "goal")
    state._status = legacy._status = status
    parity(state, legacy, method, args)


@pytest.mark.parametrize("method,args", [
    ("pin_content", ("content", "source")), ("add_constraint", ("constraint",)),
    ("resolve_question", ("q", "answer")),
])
def test_active_step_planning_errors_match_taskstate(state, method, args):
    legacy = TaskState("user", "goal")
    questions(state, "q")
    legacy._scratchpad = state.scratchpad
    state.start("step")
    legacy.start_planning()
    legacy.set_plan(["work"])
    legacy.start_step(legacy.plan[0].step_id)
    parity(state, legacy, method, args)


def test_start_from_created_and_ready(state):
    changed(state, "start", "step-1")
    assert state.status is TaskStatus.RUNNING
    assert state.active_step_id == "step-1"
    changed(state, "add_failure", "retry")
    changed(state, "start", "step-2")
    assert state.status is TaskStatus.RUNNING
    assert state.active_step_id == "step-2"


@pytest.mark.parametrize("status", [TaskStatus.PLANNING, TaskStatus.RUNNING, TaskStatus.BLOCKED,
                                    TaskStatus.COMPLETED, TaskStatus.FAILED])
def test_start_rejects_other_statuses(state, status):
    state._status = status
    rejected(state, "start", ("step",), InvalidStateTransitionError,
             "Task must be created or ready to start")


@pytest.mark.parametrize("value", [None, 7, "", "  "])
def test_start_validates_step_id(state, value):
    rejected(state, "start", (value,), ValueError, "step_id must be a non-empty string")


def test_block_and_resolve_to_ready(state):
    changed(state, "start", "step")
    qid = changed(state, "block", " question? ", "step")
    q = state.scratchpad.open_questions[0]
    assert isinstance(q, OpenQuestion)
    assert q.question_id == qid
    assert q.content == "question?"
    assert q.step_id == "step" and q.goal_revision == 1
    assert q.blocking and q.answer is None and q.resolved_at is None
    assert q.created_at == state.updated_at
    assert state.status is TaskStatus.BLOCKED and state.active_step_id is None
    changed(state, "resolve_question", qid, " answer ")
    resolved = state.scratchpad.open_questions[0]
    assert not resolved.blocking and resolved.answer == " answer "
    assert resolved.resolved_at == state.updated_at
    assert q.blocking and q.resolved_at is None
    assert state.status is TaskStatus.READY and state.active_step_id is None
    changed(state, "start", "next")
    changed(state, "complete")
    assert state.status is TaskStatus.COMPLETED and state.active_step_id is None


@pytest.mark.parametrize("status", [s for s in TaskStatus if s is not TaskStatus.RUNNING])
def test_block_requires_running(state, status):
    state._status = status
    rejected(state, "block", ("question", "step"), InvalidStateTransitionError,
             "Task must be running to block")


def test_block_rejects_wrong_step(state):
    state.start("step")
    rejected(state, "block", ("question", "other"), InvalidStateTransitionError,
             "Step other is not the active step")


@pytest.mark.parametrize("value", [None, 7, "", "  "])
def test_block_validates_question(state, value):
    state.start("step")
    rejected(state, "block", (value, "step"), ValueError,
             "question must be a non-empty string")


def test_resolve_keeps_blocked_until_all_questions_resolved(state):
    questions(state, "q1", "q2")
    state._status = TaskStatus.BLOCKED
    changed(state, "resolve_question", "q1", "answer")
    assert state.status is TaskStatus.BLOCKED
    changed(state, "resolve_question", "q2", "answer")
    assert state.status is TaskStatus.READY


@pytest.mark.parametrize("status", [TaskStatus.CREATED, TaskStatus.READY])
def test_resolve_preserves_nonblocked_status(state, status):
    questions(state, "q")
    state._status = status
    changed(state, "resolve_question", "q", "answer")
    assert state.status is status


@pytest.mark.parametrize("qid", ["missing", "", None, 3])
def test_resolve_missing_matches_taskstate(state, qid):
    parity(state, TaskState("user", "goal"), "resolve_question", (qid, "answer"))


def test_resolve_already_resolved_matches_taskstate_before_active_guard(state):
    questions(state, "q")
    legacy = TaskState("user", "goal")
    legacy._scratchpad = state.scratchpad
    state.resolve_question("q", "answer")
    legacy.resolve_question("q", "answer")
    state.start("step")
    legacy._active_step_id = "step"
    parity(state, legacy, "resolve_question", ("q", "again"))
    parity(state, legacy, "resolve_question", ("missing", "answer"))


@pytest.mark.parametrize("refs", [(), ("ref-1", "ref-2")])
def test_add_failure_preserves_history_and_returns_ready(state, refs):
    state.start("step")
    first = changed(state, "add_failure", " failure ", refs)
    state.start("step")
    second = changed(state, "add_failure", "again")
    records = state.scratchpad.failure_history
    assert [r.failure_id for r in records] == [first, second]
    assert first != second
    assert isinstance(records[0], FailureRecord)
    assert records[0].message == " failure "
    assert records[0].source_refs == refs and records[1].source_refs == ()
    for record in records:
        assert record.step_id == "step" and record.goal_revision == 1
        assert record.plan_revision == record.retry_count == 0
        assert record.created_at.tzinfo is UTC
    assert state.status is TaskStatus.READY and state.active_step_id is None


@pytest.mark.parametrize("status", [s for s in TaskStatus if s is not TaskStatus.RUNNING])
def test_add_failure_requires_running_like_taskstate(state, status):
    legacy = TaskState("user", "goal")
    state._status = legacy._status = status
    parity(state, legacy, "add_failure", ("failure",))


@pytest.mark.parametrize("status", [s for s in TaskStatus if s is not TaskStatus.RUNNING])
def test_complete_requires_running(state, status):
    state._status = status
    rejected(state, "complete", (), InvalidStateTransitionError,
             "Task must be running to complete")


def test_complete_preserves_inputs_and_history(state):
    state.pin_content("content", "source")
    state.add_constraint("constraint")
    state.start("step")
    state.add_failure("retry")
    before = state.snapshot()
    state.start("step-2")
    changed(state, "complete")
    assert state.status is TaskStatus.COMPLETED and state.active_step_id is None
    assert state.scratchpad == before.scratchpad
    assert state.constraints == before.constraints
    assert state.pinned_contexts == before.pinned_contexts


def test_complete_rejects_unresolved_blocking_questions(state):
    questions(state, "q")
    state.start("step")
    rejected(state, "complete", (), InvalidStateTransitionError,
             "Cannot complete task with unresolved blocking questions")


def test_complete_allows_nonblocking_and_resolved_questions(state):
    questions(state, "q1", "q2")
    state._scratchpad = replace(state.scratchpad, open_questions=(
        replace(state.scratchpad.open_questions[0], blocking=False),
        replace(state.scratchpad.open_questions[1], resolved_at=datetime.now(UTC)),
    ))
    state.start("step")
    changed(state, "complete")
    assert state.status is TaskStatus.COMPLETED


@pytest.mark.parametrize("status", [TaskStatus.CREATED, TaskStatus.PLANNING, TaskStatus.READY,
                                    TaskStatus.RUNNING, TaskStatus.BLOCKED])
def test_fail_task_from_any_nonterminal_state(state, status):
    state.start("previous")
    state.add_failure("previous failure")
    state._status = status
    state._active_step_id = "step" if status is TaskStatus.RUNNING else None
    before = state.snapshot()
    fid = changed(state, "fail_task", " fatal ")
    record = state.scratchpad.failure_history[-1]
    assert record.failure_id == fid and record.message == " fatal "
    assert record.step_id == before.active_step_id
    assert (record.goal_revision, record.plan_revision, record.retry_count, record.source_refs) == (1, 0, 0, ())
    assert record.created_at == state.updated_at
    assert state.scratchpad.failure_history[:-1] == before.scratchpad.failure_history
    assert state.status is TaskStatus.FAILED and state.active_step_id is None


def test_snapshot_is_frozen_slotted_and_stable(state):
    state.pin_content("content", "source")
    state.add_constraint("constraint")
    snap = state.snapshot()
    assert isinstance(snap, RunStateSnapshot)
    assert not hasattr(snap, "__dict__")
    assert {f.name for f in fields(snap)} == {
        "task_id", "user_id", "status", "active_goal", "goal_revision", "state_version",
        "created_at", "updated_at", "scratchpad", "pinned_contexts", "constraints", "active_step_id",
    }
    for field in fields(snap):
        assert getattr(snap, field.name) == getattr(state, field.name)
        with pytest.raises(AttributeError):
            setattr(state, field.name, None)
        with pytest.raises(FrozenInstanceError):
            setattr(snap, field.name, None)
    assert state.snapshot() == snap
    changed(state, "start", "step")
    changed(state, "block", "question", "step")
    assert snap.status is TaskStatus.CREATED and snap.active_step_id is None
    assert snap.scratchpad.open_questions == ()
    assert snap.state_version == 2


def test_runmemory_defaults_and_supplied_resources(state):
    memory = RunMemory(state)
    other = RunMemory(state)
    assert memory.task_state is state
    assert isinstance(memory.resources, Resources)
    assert memory.resources is not other.resources
    resources = Resources()
    assert RunMemory(state, resources).resources is resources
    for name in ("task_state", "resources"):
        with pytest.raises(AttributeError):
            setattr(memory, name, None)


@pytest.mark.parametrize("value", [None, object(), "state", TaskState("user", "goal")])
def test_runmemory_rejects_other_state_types(value):
    with pytest.raises(TypeError, match="^task_state must be a RunState$"):
        RunMemory(value)


def test_block_retains_legacy_observation_for_resumed_loop():
    state = RunState('user', 'goal')
    state.start('step')
    state.block('  Which file?  ', 'step', plan_revision=2, source_refs=('resource',))
    observation = state.scratchpad.last_observation
    assert observation.content == 'Which file?'
    assert observation.step_id == 'step'
    assert observation.goal_revision == 1
    assert observation.plan_revision == 2
    assert observation.source_refs == ('resource',)
    state.resolve_question(state.scratchpad.open_questions[0].question_id, 'parser.py')
    assert state.scratchpad.last_observation == observation
