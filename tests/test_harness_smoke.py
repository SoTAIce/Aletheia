"""Minimal deterministic harness tests: in-memory only, no models, MCP or DB.

init.ps1 requires this file. It proves the test framework runs and guards one core invariant of the
task state, RunState (stage 4c: it guarded WorkingMemory's plan commit before plan mode was removed).
"""

import pytest

from app.models.run_state import RunState
from app.models.taskstate import InvalidStateTransitionError, TaskStatus


def test_start_and_complete() -> None:
    state = RunState("smoke-user", "smoke goal")
    state.start("step")
    state.complete()
    assert state.status == TaskStatus.COMPLETED and state.active_step_id is None


def test_a_finished_task_rejects_writes_without_state_change() -> None:
    state = RunState("smoke-user", "smoke goal")
    state.start("step")
    state.complete()
    before = state.snapshot()
    with pytest.raises(InvalidStateTransitionError):
        state.pin_content("late", "conversation_memory")
    assert state.snapshot() == before
