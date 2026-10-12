from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from app.agent.step_executor import StepExecutor
from app.agent.step_models import StepOutcome, StepRunOutput
from app.agent.step_runner import StepBlockedError, StepRunError
from app.models.resources import ResourceType
from app.models.taskstate import StepStatus, TaskState, TaskStatus
from app.models.working_memory import WorkingMemory


def setup():
    memory = WorkingMemory(TaskState("user", "goal"))
    memory.task_state.start_planning()
    memory.install_plan(["Inspect"])
    runner = AsyncMock()
    return memory, runner, StepExecutor(memory, runner)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome,error,status", [
    (StepOutcome.FAILED, StepRunError, StepStatus.FAILED),
    (StepOutcome.BLOCKED, StepBlockedError, StepStatus.BLOCKED),
])
async def test_executor_routes_unsuccessful_outcomes_without_success_commit(outcome, error, status):
    memory, runner, executor = setup()
    rid = memory.resources.register("evidence", ResourceType.TOOL_RESULT)
    runner.run.return_value = StepRunOutput("Work incomplete", source_refs=(f" {rid} ",),
                                            outcome=outcome, reason="Missing information")
    with pytest.raises(error):
        await executor.execute(memory.task_state.plan[0].step_id)
    assert memory.task_state.plan[0].status is status
    assert memory.task_state.active_step_id is None
    assert memory.task_state.scratchpad.intermediate_results == ()
    if outcome is StepOutcome.BLOCKED:
        assert memory.task_state.status is TaskStatus.BLOCKED
        assert memory.task_state.scratchpad.last_observation.source_refs == (rid,)
        assert memory.task_state.scratchpad.open_questions[0].blocking
    else:
        assert memory.task_state.status is TaskStatus.PLANNING
        assert memory.task_state.scratchpad.failure_history[0].source_refs == (rid,)


@pytest.mark.parametrize("changes", [
    {"outcome": "failed", "reason": "error"}, {"reason": "error"},
    {"outcome": StepOutcome.FAILED}, {"outcome": StepOutcome.BLOCKED, "reason": " "},
])
def test_outcome_contract_rejected_without_mutating_memory(changes):
    memory, _, executor = setup()
    before = memory.snapshot()
    with pytest.raises((TypeError, ValueError)):
        executor._validate_runner_output(replace(StepRunOutput("text"), **changes))
    assert memory.snapshot() == before
