"""Bounded, sequential composition of the existing planning/execution components."""

import asyncio
from dataclasses import asdict, dataclass
import json
import math

from app.agent.executor import Executor
from app.agent.planner import Planner
from app.agent.step_executor import StepExecutor
from app.agent.step_models import StepExecutionReceipt
from app.agent.step_runner import StepBlockedError, StepRunError
from app.agent.tool_calling_model import ToolCallingModel
from app.agent.tool_calling_step_runner import ToolCallingStepRunner
from app.models.conversation_memory import (
    ConversationMemory, ConversationMessage, ConversationTurn, MessageRole,
)
from app.models.taskstate import StepStatus, TaskStatus
from app.models.working_memory import StaleExecutionError, WorkingMemory
from app.tools.tool_registry import ToolRegistry


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    task_id: str
    status: TaskStatus
    content: str
    steps: tuple[StepExecutionReceipt, ...]
    execution_count: int
    replan_count: int


class AgentRuntime:
    """One runtime per task. Keep tool sessions open for run() and any resume.

    Counters survive blocked/resumed runs. Resolve questions through TaskState
    between runs; do not mutate memory concurrently with an active run.
    """

    def __init__(
        self, planner: Planner, model: ToolCallingModel, memory: WorkingMemory,
        registry: ToolRegistry, *, conversation: ConversationMemory | None = None,
        max_executions: int = 16, max_replans: int = 2,
        step_timeout_seconds: float = 60.0, planning_timeout_seconds: float = 60.0,
        max_tool_rounds: int = 4, max_total_tool_calls: int = 8,
    ) -> None:
        for name, value, minimum in (("max_executions", max_executions, 1), ("max_replans", max_replans, 0)):
            if type(value) is not int or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")
        for name, value in (("step_timeout_seconds", step_timeout_seconds),
                            ("planning_timeout_seconds", planning_timeout_seconds)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        self.memory = memory
        self._planner = planner
        self._executor = Executor(memory)
        self._registry = registry
        self._step_executor = StepExecutor(memory, ToolCallingStepRunner(
            model, registry, max_tool_rounds, max_total_tool_calls, require_step_outcome=True,
        ))
        self._conversation = conversation
        self._max_executions, self._max_replans = max_executions, max_replans
        self._step_timeout, self._planning_timeout = step_timeout_seconds, planning_timeout_seconds
        self._execution_count = self._replan_count = 0
        self._steps: list[StepExecutionReceipt] = []
        self._lock = asyncio.Lock()
        self._reported_version: int | None = None

    async def run(self) -> AgentRunResult:
        async with self._lock:
            try:
                result = await self._run()
            except asyncio.CancelledError:
                if self.memory.task_state.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                    self.memory.task_state.fail_task("Agent execution cancelled; tool side effects may already exist")
                raise
            if self._conversation is not None and self._reported_version != self.memory.task_state.state_version:
                await self._conversation.add(ConversationTurn([
                    ConversationMessage(MessageRole.USER, self.memory.task_state.active_goal),
                    ConversationMessage(MessageRole.ASSISTANT, f"[{result.status.value}] {result.content}"),
                ]))
                self._reported_version = self.memory.task_state.state_version
            return result

    async def _run(self) -> AgentRunResult:
        task = self.memory.task_state
        if task.status is TaskStatus.CREATED:
            if self._conversation is not None:
                history = {
                    "summary": self._conversation.summary,
                    "messages": [{"role": message.role.value, "content": message.content}
                                 for message in self._conversation.retrieve_messages(k=3)
                                 if message.role in (MessageRole.USER, MessageRole.ASSISTANT)],
                }
                if history["summary"] or history["messages"]:
                    task.pin_content(json.dumps(history, ensure_ascii=False)[:12000], "conversation_memory")
            task.pin_content(json.dumps(
                [asdict(tool) for tool in self._registry.list_definitions()], ensure_ascii=False
            ), "available_tools")
            task.start_planning()

        while task.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            blockers = [q for q in task.scratchpad.open_questions
                        if q.goal_revision == task.goal_revision and q.blocking and q.resolved_at is None]
            if blockers:
                task.pause_for_questions()
                return self._result("\n".join(q.content for q in blockers))
            if task.status is TaskStatus.PLANNING:
                if self._execution_count >= self._max_executions:
                    return self._fail("Agent execution limit reached")
                if task.plan_revision:
                    if self._replan_count >= self._max_replans:
                        return self._fail("Agent replanning limit reached")
                    self._replan_count += 1
                try:
                    context = self._planner.build_context(self.memory)
                    async with asyncio.timeout(self._planning_timeout):
                        proposal = await (self._planner.replan(context) if context.plan_revision
                                          else self._planner.plan(context))
                    self._executor.accept_plan(proposal)
                except Exception as exc:
                    return self._fail(f"Planning failed: {type(exc).__name__}: {exc}")
            next_step = next((step for step in task.plan if step.status is StepStatus.PENDING), None)
            if next_step is None:
                try:
                    self.memory.complete_task()
                except Exception as exc:
                    return self._fail(f"Task completion rejected: {exc}")
                break
            if self._execution_count >= self._max_executions:
                return self._fail("Agent execution limit reached")
            self._execution_count += 1
            try:
                async with asyncio.timeout(self._step_timeout):
                    receipt = await self._step_executor.execute(next_step.step_id)
                self._steps.append(receipt)
            except StepBlockedError as exc:
                return self._result(exc.message)
            except StepRunError:
                # StepExecutor has recorded the failure and returned to PLANNING.
                continue
            except StaleExecutionError:
                # Never overwrite evidence or lifecycle belonging to newer work.
                raise
            except TimeoutError:
                self.memory.record_failure("Step execution timed out; side effects may already exist")
            except Exception as exc:
                # Contract/implementation errors are terminal, not retry instructions.
                return self._fail(f"Execution failed: {type(exc).__name__}: {exc}")

        if task.status is TaskStatus.FAILED:
            return self._result(task.scratchpad.failure_history[-1].message)
        results = [r.content for r in task.scratchpad.intermediate_results
                   if r.valid and r.goal_revision == task.goal_revision]
        return self._result("\n\n".join(results))

    def _fail(self, message: str) -> AgentRunResult:
        self.memory.task_state.fail_task(message)
        return self._result(message)

    def _result(self, content: str) -> AgentRunResult:
        task = self.memory.task_state
        return AgentRunResult(task.task_id, task.status, content, tuple(self._steps),
                              self._execution_count, self._replan_count)
