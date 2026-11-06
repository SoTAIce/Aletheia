"""Bounded, sequential composition of the existing planning/execution components."""

import asyncio
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import json
import math
import time

from loguru import logger

from app.agent.executor import Executor
from app.agent.planner import Planner
from app.agent.step_executor import StepExecutor
from app.agent.step_models import StepExecutionReceipt
from app.agent.side_effects import SideEffectWatch
from app.agent.run_meter import RunMeter, current_meter, metering
from app.agent.step_runner import (StepBlockedError, StepRunError, StepFormatError, StepModelError, StepRoutingError,
                                   StepToolError)
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


# Why a task failed, for callers and for the user-facing message (D-033).
FAILURE_MESSAGES = {
    'limit': '已达到执行步数或重新规划次数的上限，任务停止。',
    'planning': '模型没有给出可用的计划，任务停止。',
    'format': '模型多次没有按要求的格式给出步骤结果，任务停止。',
    'model': '调用模型失败（服务出错或请求超时），任务停止，可以稍后重试。',
    'routing': '这一步没有可用的执行方式，任务停止。',
    'side_effect': '一个会修改文件或运行代码的操作没有正常结束，结果不确定。为避免重复执行，任务已停止，请先检查工作区。',
    'timeout': '执行超时，为避免重复已完成的操作，任务已停止。',
    'rejected': '任务结果没有通过完成检查，任务停止。',
    'business': '任务无法完成。',
    'budget': '已用完本次任务的令牌或时间预算，任务停止。',
    'internal': '运行时内部出错，任务停止。',
}


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    task_id: str
    status: TaskStatus
    content: str
    steps: tuple[StepExecutionReceipt, ...]
    execution_count: int
    replan_count: int
    failure_kind: str | None = None    # a FAILURE_MESSAGES key when status is FAILED
    usage: dict | None = None          # model calls, tokens and seconds of this run (D-034)
    step_log: tuple[dict, ...] = ()    # host-authored record of every step attempt (D-034)

    @property
    def user_message(self) -> str | None:
        """A Chinese explanation for the user; content keeps the English diagnostic."""
        return FAILURE_MESSAGES.get(self.failure_kind) if self.failure_kind else None


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
        self._answers_seen = 0      # resolved questions already planned for (D-033)
        self._steps: list[StepExecutionReceipt] = []
        self._step_log: list[dict] = []
        self._meter: RunMeter | None = None
        self._final: AgentRunResult | None = None   # a finished task reports the same result again
        self._lock = asyncio.Lock()
        self._reported_version: int | None = None
        self._active_turn: ConversationTurn | None = None
        self._conversation_outcome: tuple | None = None
        self._pending_result: AgentRunResult | None = None
        self._outcome_version: int | None = None
        self._on_event = None       # progress callback of the current run (D-045)

    def _assemble_runner(self, model, registry, context_manager):
        """The sole construction path for both initial and resumed bindings."""
        self._watch = SideEffectWatch(registry)   # side effects of the current step (D-032)
        dependencies = RunnerDependencies(model, self._watch, context_manager, self._runner_limits)
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

    async def run(self, *, on_event=None) -> AgentRunResult:
        """on_event, if given, is called synchronously with host-authored progress
        records ('plan', 'step'); it must not block, and its errors never affect the task."""
        async with self._lock:
            self._on_event = on_event
            try:
                return await self._run_locked()
            finally:
                self._on_event = None

    def _emit(self, event: dict) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event)
        except Exception as exc:
            logger.warning('Progress callback failed: {}', type(exc).__name__)

    async def _run_locked(self) -> AgentRunResult:
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
            with self._meter_scope():
                result = await self._run()
            logger.info('Task {}: executions {}, replans {}, model calls {}, tokens {}',
                        result.status.value, result.execution_count, result.replan_count,
                        result.usage['model_calls'], result.usage['total_tokens'])
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

    @contextmanager
    def _meter_scope(self):
        """Use the caller's meter and budget, or an unlimited meter for this run (D-034)."""
        meter = current_meter()
        if meter is not None:
            self._meter = meter
            yield
            return
        with metering(RunMeter()) as meter:
            self._meter = meter
            yield

    async def _finish_turn(self):
        status, content, payload = self._conversation_outcome
        await finish_conversation_write(self._conversation.complete_turn(
            self._active_turn.turn_id, status, content, result=payload))
        self._active_turn = self._conversation_outcome = None
        self._pending_result = None
        self._reported_version = self._outcome_version

    async def _run(self) -> AgentRunResult:
        task = self.memory.task_state
        if self._final is not None and task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            return self._final
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
            spent = self._meter.budget_problem()
            if spent:
                return self._fail(spent, 'budget')
            if task.status is TaskStatus.PLANNING:
                if self._execution_count >= self._max_executions:
                    return self._fail("Agent execution limit reached")
                answers = sum(1 for q in task.scratchpad.open_questions
                              if q.goal_revision == task.goal_revision and q.resolved_at is not None)
                if task.plan_revision and answers > self._answers_seen:
                    # Planning again after the user answered is the normal flow, not a failure (D-033).
                    self._answers_seen = answers
                elif task.plan_revision:
                    if self._replan_count >= self._max_replans:
                        return self._fail("Agent replanning limit reached" + self._last_failure(), 'limit')
                    self._replan_count += 1
                try:
                    context = self._planner.build_context(self.memory)
                    async with asyncio.timeout(self._meter.bound(self._planning_timeout)):
                        proposal = await (self._planner.replan(context) if context.plan_revision
                                          else self._planner.plan(context))
                    self._executor.accept_plan(proposal)
                    logger.info('Plan revision {}: {} step(s)', task.plan_revision, len(task.plan))
                    self._emit({'type': 'plan', 'revision': task.plan_revision,
                                'steps': [step.description for step in task.plan]})
                except Exception as exc:
                    spent = self._meter.budget_problem()
                    if spent:
                        return self._fail(spent, 'budget')
                    # Planner validation messages are host-authored (see bench _SAFE_FAILURE).
                    return self._fail(f"Planning failed: {type(exc).__name__}: {exc}", 'planning')
            next_step = next((step for step in task.plan if step.status is StepStatus.PENDING), None)
            if next_step is None:
                try:
                    self.memory.complete_task()
                except Exception as exc:
                    return self._fail(f"Task completion rejected: {type(exc).__name__}", 'rejected')
                break
            if self._execution_count >= self._max_executions:
                return self._fail("Agent execution limit reached", 'limit')
            self._execution_count += 1
            self._watch.reset()
            started = time.monotonic()
            try:
                try:
                    async with asyncio.timeout(self._meter.bound(self._step_timeout)):
                        receipt = await self._step_executor.execute(next_step.step_id)
                except BaseException as exc:
                    self._log_step(next_step.step_id, started, exc)
                    raise
                self._log_step(next_step.step_id, started, None)
                self._steps.append(receipt)
            except StepBlockedError as exc:
                return self._result(exc.message)
            except (StepFormatError, StepModelError, StepRoutingError) as exc:
                # Local format repair is exhausted, or the provider failed.
                # Replanning would repeat completed tools without fixing either.
                kind = ('format' if isinstance(exc, StepFormatError) else
                        'routing' if isinstance(exc, StepRoutingError) else 'model')
                return self._fail(f"{type(exc).__name__}: {exc.message}", kind)
            except StepToolError as exc:
                if self._watch.uncertain:
                    # A write or run raised: it may have taken effect, so never replay it (D-032).
                    return self._fail(f"{type(exc).__name__}: {exc.message}; automatic replanning is disabled",
                                      'side_effect')
                continue          # a read-only tool failed; StepExecutor recorded it for replanning
            except StepRunError:
                # StepExecutor has recorded the failure and returned to PLANNING.
                continue
            except StaleExecutionError:
                # Never overwrite evidence or lifecycle belonging to newer work.
                raise
            except TimeoutError:
                if self.routing_decisions and self.routing_decisions[-1].runner_id == 'agent_team':
                    # A team owns many side effects; an outer deadline must not replay it.
                    return self._fail('Team execution timed out; automatic replanning is disabled', 'timeout')
                if self._context_manager.overflow_recovery_interrupted:
                    return self._fail("Context overflow recovery timed out; completed tools will not be repeated",
                                      'timeout')
                if self._watch.touched:
                    # The step started a write or run before the deadline (D-032).
                    return self._fail("Step execution timed out after a write or run; automatic replanning "
                                      "is disabled", 'side_effect')
                self.memory.record_failure("Step execution timed out; only read-only tools had run")
            except Exception as exc:
                # Contract/implementation errors are terminal, not retry instructions.
                # Only the type name: arbitrary exception text may carry host paths or provider output.
                return self._fail(f"Execution failed: {type(exc).__name__}", 'internal')

        if task.status is TaskStatus.FAILED:
            return self._result(task.scratchpad.failure_history[-1].message, 'business')
        # The planner ends with one combined delivery step, so its result is the answer;
        # earlier step results stay in the scratchpad and the trace (D-027).
        results = [r.content for r in task.scratchpad.intermediate_results
                   if r.valid and r.goal_revision == task.goal_revision]
        return self._result(results[-1] if results else "")

    def _log_step(self, step_id, started, exc):
        """Record one step attempt: outcome, runner and tool calls, without model or tool text."""
        outcome = ('succeeded' if exc is None else 'blocked' if isinstance(exc, StepBlockedError)
                   else 'timeout' if isinstance(exc, TimeoutError) else 'failed')
        decisions = self.routing_decisions
        entry = {'step': self._execution_count, 'step_id': step_id, 'outcome': outcome,
                 'error': None if exc is None or outcome == 'blocked' else type(exc).__name__,
                 'runner': decisions[-1].runner_id if decisions else None,
                 'seconds': round(time.monotonic() - started, 3), 'tools': [dict(c) for c in self._watch.calls]}
        self._step_log.append(entry)
        tools = ', '.join(f"{c['tool']} {'ok' if c['success'] else 'failed'}" for c in entry['tools']) or 'none'
        logger.info('Step {} {} in {:.1f}s; tools: {}', entry['step'], outcome, entry['seconds'], tools)
        self._emit({'type': 'step', **entry, 'tools': [dict(c) for c in entry['tools']]})

    def _last_failure(self) -> str:
        """The latest recorded step failure, so a limit message keeps its root cause (D-033)."""
        history = self.memory.task_state.scratchpad.failure_history
        if not history:
            return ""
        text = " ".join(history[-1].message.split())
        return "; last failure: " + (text[:300] + "..." if len(text) > 300 else text)

    def _fail(self, message: str, kind: str = 'internal') -> AgentRunResult:
        self.memory.task_state.fail_task(message)
        return self._result(message, kind)

    def _result(self, content: str, failure_kind: str | None = None) -> AgentRunResult:
        task = self.memory.task_state
        kind = failure_kind if task.status is TaskStatus.FAILED else None
        usage = self._meter.snapshot() if self._meter is not None else None
        result = AgentRunResult(task.task_id, task.status, content, tuple(self._steps),
                                self._execution_count, self._replan_count, kind, usage, tuple(self._step_log))
        if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            self._final = result
        return result
