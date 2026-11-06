"""Bounded, sequential composition of the existing planning/execution components."""

import asyncio
from dataclasses import asdict, dataclass
import json
import math

from app.agent.executor import Executor
from app.agent.planner import Planner
from app.agent.step_executor import StepExecutor
from app.agent.step_models import StepExecutionReceipt
from app.agent.step_runner import StepBlockedError, StepRunError, StepFormatError, StepModelError, StepRoutingError
from app.agent.tool_calling_model import ToolCallingModel
from app.agent.runner_registry import RunnerRegistry, RunnerDependencies, RunnerLimits, default_runner_registry
from app.agent.runner_router import RoutingPolicy, RequirementsProvider
from app.agent.routing_step_runner import RoutingStepRunner, RoutingEvent
from app.models.conversation_memory import (
    ConversationMemory, ConversationMessage, ConversationTurn, MessageRole, TurnStatus,
    finish_conversation_write,
)
from app.models.taskstate import StepStatus, TaskStatus
from app.models.working_memory import StaleExecutionError, WorkingMemory
from app.tools.tool_registry import ToolRegistry
from app.services.context_manager import ContextManager


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
        context_manager: ContextManager | None = None,
        runner_registry: RunnerRegistry | None = None, routing_policy: RoutingPolicy | None = None,
        runner_requirements: RequirementsProvider | None = None,
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
        self._context_manager = context_manager or ContextManager()
        if self._planner._context_manager is None:
            self._planner._context_manager = self._context_manager
        if runner_registry is not None and not isinstance(runner_registry, RunnerRegistry):
            raise TypeError('runner_registry must be RunnerRegistry')
        self._runner_limits = RunnerLimits(max_tool_rounds, max_total_tool_calls)
        self._runner_registry = (runner_registry if runner_registry is not None else default_runner_registry()).snapshot()
        self._routing_policy, self._runner_requirements = routing_policy, runner_requirements
        self._routing_events: list[RoutingEvent] = []
        self._runner, self._step_executor = self._assemble_runner(model, registry, self._context_manager)
        self._conversation = conversation
        self._max_executions, self._max_replans = max_executions, max_replans
        self._step_timeout, self._planning_timeout = step_timeout_seconds, planning_timeout_seconds
        self._execution_count = self._replan_count = 0
        self._steps: list[StepExecutionReceipt] = []
        self._lock = asyncio.Lock()
        self._reported_version: int | None = None
        self._active_turn: ConversationTurn | None = None
        self._conversation_outcome: tuple | None = None
        self._pending_result: AgentRunResult | None = None
        self._outcome_version: int | None = None

    def _assemble_runner(self, model, registry, context_manager):
        """The sole construction path for both initial and resumed bindings."""
        dependencies = RunnerDependencies(model, registry, context_manager, self._runner_limits)
        runner = RoutingStepRunner(self._runner_registry.bind(dependencies), routing_policy=self._routing_policy,
                                   requirements_provider=self._runner_requirements, events=self._routing_events)
        return runner, StepExecutor(self.memory, runner)

    @property
    def routing_decisions(self) -> tuple[RoutingEvent, ...]:
        return tuple(self._routing_events)

    def rebind(self, planner: Planner, model: ToolCallingModel, registry: ToolRegistry, *,
               context_manager: ContextManager) -> None:
        """Refresh request-scoped clients while retaining task state and limits."""
        if self._lock.locked() or self.memory.task_state.status is TaskStatus.RUNNING:
            raise RuntimeError('cannot rebind a running task')
        runner, step_executor = self._assemble_runner(model, registry, context_manager)
        if planner._context_manager is None:
            planner._context_manager = context_manager
        self._runner.invalidate()
        self._planner, self._registry, self._context_manager = planner, registry, context_manager
        self._runner = runner
        self._step_executor = step_executor

    @property
    def format_repair_count(self) -> int:
        return self._planner.format_repair_count + self._runner.format_repair_count

    async def run(self) -> AgentRunResult:
        async with self._lock:
            conversation = self._conversation
            task = self.memory.task_state
            # The service supplies exact inbound text itself. This optional,
            # low-level compatibility path can only see the task goal/answer.
            if conversation is not None and self._conversation_outcome is not None:
                pending = self._pending_result
                await self._finish_turn()
                return pending if pending is not None else self._result('Previous interaction did not complete')
            if conversation is not None and self._reported_version != task.state_version:
                if self._active_turn is None:
                    answers = [q for q in task.scratchpad.open_questions
                               if q.resolved_at is not None and q.goal_revision == task.goal_revision]
                    text = answers[-1].answer if answers and self._reported_version is not None else task.active_goal
                    self._active_turn = ConversationTurn([ConversationMessage(MessageRole.USER, text)],
                                                        task_id=task.task_id, status=TurnStatus.RECEIVED)
            try:
                if conversation is not None and self._active_turn is not None:
                    await finish_conversation_write(conversation.add(self._active_turn))
                result = await self._run()
            except StaleExecutionError:
                # A newer task/step owns this workspace. Even exception cleanup
                # must not fail that state or publish an old interaction result.
                raise
            except (Exception, asyncio.CancelledError) as exc:
                status = TurnStatus.CANCELLED if isinstance(exc, asyncio.CancelledError) else TurnStatus.FAILED
                content = ('Agent execution cancelled; tool side effects may already exist'
                           if status is TurnStatus.CANCELLED else f'Agent execution failed: {type(exc).__name__}')
                if task.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                    task.fail_task(content)
                if conversation is not None and self._active_turn is not None:
                    try:
                        await conversation.read_turn(self._active_turn.turn_id)
                    except KeyError:
                        self._active_turn = None  # Original publication failed; nothing to finish.
                    else:
                        self._conversation_outcome = (status, content, None)
                        self._outcome_version = task.state_version
                        await self._finish_turn()
                raise
            if conversation is not None and self._active_turn is not None:
                self._pending_result = result
                self._outcome_version = task.state_version
                self._conversation_outcome = (TurnStatus(result.status.value), result.content, asdict(result))
                await self._finish_turn()
            return result

    async def _finish_turn(self):
        status, content, payload = self._conversation_outcome
        await finish_conversation_write(self._conversation.complete_turn(
            self._active_turn.turn_id, status, content, result=payload))
        self._active_turn = self._conversation_outcome = None
        self._pending_result = None
        self._reported_version = self._outcome_version

    async def _run(self) -> AgentRunResult:
        task = self.memory.task_state
        if task.status is TaskStatus.CREATED:
            if self._conversation is not None:
                history = await self._context_manager.prepare_conversation(self._conversation, task.active_goal)
                task.pin_content(history, "conversation_memory")
            task.pin_content(json.dumps(
                [{"name": tool.name, "description": tool.description}
                 for tool in self._registry.list_definitions()], ensure_ascii=False
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
            except (StepFormatError, StepModelError, StepRoutingError) as exc:
                # Local format repair is exhausted, or the provider failed.
                # Replanning would repeat completed tools without fixing either.
                return self._fail(f"{type(exc).__name__}: {exc.message}")
            except StepRunError:
                # StepExecutor has recorded the failure and returned to PLANNING.
                continue
            except StaleExecutionError:
                # Never overwrite evidence or lifecycle belonging to newer work.
                raise
            except TimeoutError:
                if self.routing_decisions and self.routing_decisions[-1].runner_id == 'agent_team':
                    # A team owns many side effects; an outer deadline must not replay it.
                    return self._fail('Team execution timed out; automatic replanning is disabled')
                if self._context_manager.overflow_recovery_interrupted:
                    return self._fail("Context overflow recovery timed out; completed tools will not be repeated")
                self.memory.record_failure("Step execution timed out; side effects may already exist")
            except Exception as exc:
                # Contract/implementation errors are terminal, not retry instructions.
                return self._fail(f"Execution failed: {type(exc).__name__}: {exc}")

        if task.status is TaskStatus.FAILED:
            return self._result(task.scratchpad.failure_history[-1].message)
        # The planner ends with one combined delivery step, so its result is the answer;
        # earlier step results stay in the scratchpad and the trace (D-027).
        results = [r.content for r in task.scratchpad.intermediate_results
                   if r.valid and r.goal_revision == task.goal_revision]
        return self._result(results[-1] if results else "")

    def _fail(self, message: str) -> AgentRunResult:
        self.memory.task_state.fail_task(message)
        return self._result(message)

    def _result(self, content: str) -> AgentRunResult:
        task = self.memory.task_state
        return AgentRunResult(task.task_id, task.status, content, tuple(self._steps),
                              self._execution_count, self._replan_count)
