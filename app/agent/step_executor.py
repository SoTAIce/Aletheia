"""Coordinate single-step execution and declared runner failures."""

from dataclasses import replace

from app.agent.step_runner import StepRunner, StepRunError
from app.models.working_memory import WorkingMemory, StepExecutionResult, StaleExecutionError
from app.models.taskstate import StepStatus, TaskStatus, InvalidStateTransitionError
from app.agent.step_models import (
    StepExecutionContext,
    StepRunOutput,
    ToolCall,
    ToolResult,
    StepExecutionReceipt,
)


class StepExecutor:
    def __init__(self, working_memory: WorkingMemory, runner: StepRunner) -> None:
        if not isinstance(working_memory, WorkingMemory):
            raise TypeError(
                "working_memory must be a WorkingMemory"
            )

        if not callable(getattr(runner, "run", None)):
            raise TypeError(
                "runner must provide an async run(context) method"
            )
        self._working_memory = working_memory
        self._runner = runner

    def build_context(self, step_id: str):
        work_snapshot = self._working_memory.snapshot()

        if work_snapshot.task_state.active_step_id != step_id:
            raise InvalidStateTransitionError("Requested step is not the active step")

        task, resourcs = work_snapshot.task_state, work_snapshot.resources
        current_step = next(
        (
            step
            for step in task.plan
            if step.step_id == step_id
        ),
        None,
        )
        if current_step is None:
            raise KeyError(f"Step {step_id} does not exist")

        intermediate_results = tuple(
            result
            for result in task.scratchpad.intermediate_results
            if result.valid
            and result.goal_revision == task.goal_revision
        )

        last_observation = task.scratchpad.last_observation

        if (
            last_observation is not None
            and last_observation.goal_revision != task.goal_revision
        ):
            last_observation = None

        open_question = tuple(
            q
            for q in task.scratchpad.open_questions
            if q.resolved_at is None
            and q.goal_revision == task.goal_revision
        )

        resource_contexts = tuple(
            resource.selected_context
            for resource in resourcs
            if resource.selected_context is not None
        )

        return StepExecutionContext(
            task_id = task.task_id,
            active_goal = task.active_goal,
            goal_revision = task.goal_revision,
            plan_revision = task.plan_revision,
            current_step = current_step,
            constraints = task.constraints,
            pinned_contexts = task.pinned_contexts,
            intermediate_results = intermediate_results,
            last_observation = last_observation,
            open_question = open_question,
            resource_contexts = resource_contexts
        )

    def _validate_runner_output(self, output: StepRunOutput) -> StepRunOutput:
        """Return validated, normalized output without changing the input or memory.

        Tool failures may be valid evidence for a successful step. Empty tool
        result content is allowed; the final step content must be non-empty.
        Trim descriptive text and identifiers, but preserve raw tool content
        and argument values, where whitespace can be meaningful.
        """
        if not isinstance(output, StepRunOutput):
            raise TypeError("output must be a StepRunOutput")

        def require_text(value: object, field: str) -> None:
            if not isinstance(value, str):
                raise TypeError(f"{field} must be a string")
            if not value.strip():
                raise ValueError(f"{field} must not be empty")

        def validate_refs(refs: tuple[str, ...], field: str) -> tuple[str, ...]:
            if not isinstance(refs, tuple):
                raise TypeError(f"{field} must be a tuple of resource IDs")
            normalized_refs = []
            for index, ref in enumerate(refs):
                require_text(ref, f"{field}[{index}]")
                normalized_refs.append(self._working_memory.resources.get(ref).resource_id)
            return tuple(normalized_refs)

        require_text(output.content, "content")
        if output.observation is not None:
            require_text(output.observation, "observation")
        source_refs = validate_refs(output.source_refs, "source_refs")

        if not isinstance(output.tool_calls, tuple):
            raise TypeError("tool_calls must be a tuple of ToolCall objects")
        tool_calls = []
        for index, call in enumerate(output.tool_calls):
            field = f"tool_calls[{index}]"
            if not isinstance(call, ToolCall):
                raise TypeError(f"{field} must be a ToolCall")
            require_text(call.call_id, f"{field}.call_id")
            require_text(call.tool_name, f"{field}.tool_name")
            if not isinstance(call.arguments, dict) or not all(
                isinstance(key, str) for key in call.arguments
            ):
                raise TypeError(f"{field}.arguments must be a dict with string keys")
            tool_calls.append(replace(
                call, call_id=call.call_id.strip(), tool_name=call.tool_name.strip()
            ))

        if not isinstance(output.tool_results, tuple):
            raise TypeError("tool_results must be a tuple of ToolResult objects")
        tool_results = []
        for index, result in enumerate(output.tool_results):
            field = f"tool_results[{index}]"
            if not isinstance(result, ToolResult):
                raise TypeError(f"{field} must be a ToolResult")
            require_text(result.call_id, f"{field}.call_id")
            require_text(result.tool_name, f"{field}.tool_name")
            if not isinstance(result.success, bool):
                raise TypeError(f"{field}.success must be a bool")
            if not isinstance(result.content, str):
                raise TypeError(f"{field}.content must be a string")
            result_refs = validate_refs(result.source_refs, f"{field}.source_refs")
            if result.error is not None:
                require_text(result.error, f"{field}.error")
            tool_results.append(replace(
                result,
                call_id=result.call_id.strip(),
                tool_name=result.tool_name.strip(),
                source_refs=result_refs,
                error=result.error.strip() if result.error is not None else None,
            ))

        return replace(
            output,
            content=output.content.strip(),
            observation=output.observation.strip() if output.observation is not None else None,
            source_refs=source_refs,
            tool_calls=tuple(tool_calls),
            tool_results=tuple(tool_results),
        )

    async def execute(self, step_id: str) -> StepExecutionReceipt:
        def require_text(value: object, field: str) -> None:
            if not isinstance(value, str):
                raise TypeError(f"{field} must be a string")
            if not value.strip():
                raise ValueError(f"{field} must not be empty")
        require_text(step_id, "step_id")
        step_id = step_id.strip()

        token = self._working_memory.begin_step(step_id)

        context = self.build_context(step_id)

        try:
            output = await self._runner.run(context)
        except StepRunError as exc:
            # The runner awaited external work: the task may have changed meanwhile.
            task = self._working_memory.task_state.snapshot()
            if (
                task.task_id != token.task_id
                or task.goal_revision != token.goal_revision
                or task.plan_revision != token.plan_revision
                or task.active_step_id != token.step_id
                or task.state_version != token.state_version
                or task.status is not TaskStatus.RUNNING
            ):
                raise StaleExecutionError("Failed execution is no longer current") from exc
            # This API validates message and refs before changing task state.
            # Validation errors retain exc as their exception context.
            self._working_memory.record_failure(exc.message, source_refs=exc.source_refs)
            raise

        normalized_output = self._validate_runner_output(output)

        result = StepExecutionResult(
            token = token,
            content = normalized_output.content,
            observation = normalized_output.observation,
            source_refs = normalized_output.source_refs,
            )

        result_id = self._working_memory.commit_step_result(result)

        task = self._working_memory.task_state.snapshot()
        completed_step = next(
            (step for step in task.plan if step.step_id == token.step_id), None
        )
        if completed_step is None or completed_step.status is not StepStatus.COMPLETED:
            raise RuntimeError(f"Committed step {token.step_id} is missing or not completed")
        if completed_step.result_summary is None:
            raise RuntimeError(f"Committed step {token.step_id} has no result summary")

        return StepExecutionReceipt(
            task_id=task.task_id,
            step_id=completed_step.step_id,
            goal_revision=completed_step.goal_revision,
            plan_revision=completed_step.plan_revision,
            state_version=task.state_version,
            result_id=result_id,
            result_summary=completed_step.result_summary,
            source_refs=normalized_output.source_refs,
            tool_calls=normalized_output.tool_calls,
            tool_results=normalized_output.tool_results,
        )
