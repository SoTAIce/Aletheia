"""Minimal deterministic harness tests: in-memory only, no models, MCP or DB.

Restored test coverage is still absent (see docs/project_cleanup.md); this file
only proves the test framework runs and guards one core WorkingMemory invariant.
"""

import pytest

from app.models.taskstate import TaskState, TaskStatus
from app.models.working_memory import (
    StaleExecutionError,
    StepExecutionResult,
    WorkingMemory,
)


def _planned_memory(steps: list[str]) -> WorkingMemory:
    memory = WorkingMemory(TaskState("smoke-user", "smoke goal"))
    memory.task_state.start_planning()
    memory.install_plan(steps)
    return memory


def test_plan_commit_and_complete() -> None:
    memory = _planned_memory(["step one", "step two"])
    for step in memory.task_state.plan:
        token = memory.begin_step(step.step_id)
        memory.commit_step_result(StepExecutionResult(token=token, content="ok"))
    memory.complete_task()
    assert memory.task_state.status == TaskStatus.COMPLETED


def test_consumed_token_is_rejected_without_state_change() -> None:
    memory = _planned_memory(["step one", "step two"])
    first = memory.task_state.plan[0].step_id
    token = memory.begin_step(first)
    memory.commit_step_result(StepExecutionResult(token=token, content="ok"))
    version = memory.task_state.state_version

    with pytest.raises(StaleExecutionError):
        memory.commit_step_result(StepExecutionResult(token=token, content="replay"))
    assert memory.task_state.state_version == version
