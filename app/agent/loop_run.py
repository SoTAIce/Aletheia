"""Run one task as one agent loop and own its host guarantees (stage 4c, D-076).

LoopRun holds the lock, rebind invalidation and the cancel guard, the version check, the budget, the step
timeout with a pausable deadline, the side-effect watch, the execution limit, the failure kinds, and the
plan/step events and trace spans. Runners only return output; LoopRun commits it to RunState.
"""

import asyncio
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, UTC
import math
import time
from uuid import uuid4

from loguru import logger

from app.agent.runtime import AgentRunResult, FAILURE_MESSAGES, _policy_denial, _int_or_none
from app.agent.run_meter import RunMeter, cache_text as _cache_text, call_text as _call_text, current_meter, metering
from app.agent.side_effects import SideEffectWatch
from app.agent.step_runner import (RunnerDependencies, RunnerLimits, StepBlockedError, StepRunError,
    StepBusinessError, StepFormatError, StepModelError, StepToolError, StepRunner)
from app.agent.step_models import (StepExecutionContext, StepExecutionReceipt, StepRunOutput,
    StepOutcome, ToolCall, ToolResult)
from app.models.run_state import RunMemory, StaleExecutionError
from app.models.taskstate import PlanStep, StepStatus, TaskStatus, InvalidStateTransitionError
from app.services.context_manager import ContextManager


def step_context(memory: RunMemory, step: PlanStep, plan_revision: int) -> StepExecutionContext:
    """The runner's view of a started step: what LoopRun hands its runner (tests build it the same way)."""
    snapshot = memory.task_state.snapshot()
    scratch = snapshot.scratchpad
    return StepExecutionContext(
        task_id=snapshot.task_id, active_goal=snapshot.active_goal,
        goal_revision=snapshot.goal_revision, plan_revision=plan_revision,
        current_step=step, constraints=snapshot.constraints, pinned_contexts=snapshot.pinned_contexts,
        intermediate_results=tuple(r for r in scratch.intermediate_results
                                   if r.valid and r.goal_revision == snapshot.goal_revision),
        last_observation=(scratch.last_observation if scratch.last_observation is not None
                          and scratch.last_observation.goal_revision == snapshot.goal_revision else None),
        open_question=tuple(q for q in scratch.open_questions if q.resolved_at is None
                            and q.goal_revision == snapshot.goal_revision),
        resource_contexts=tuple(r.selected_context for r in memory.resources.list_all()
                                if r.selected_context is not None),
        resolved_questions=tuple(q for q in scratch.open_questions if q.resolved_at is not None
                                 and q.goal_revision == snapshot.goal_revision), final_step=True)


class _BoundRunner:
    """LoopRun's binding of one runner, with two guards:

    - after rebind() the old binding is invalid, so a late result or failure from it is stale;
    - when cancellation was requested while the runner ran, neither its output nor a declared
      failure may be committed as if the step had finished normally.
    """

    def __init__(self, runner: StepRunner):
        self.runner, self._valid = runner, True      # the bound runner itself, read-only for callers
        self.runner_id = getattr(runner, 'runner_id', 'tool_calling')

    @property
    def format_repair_count(self) -> int:
        return getattr(self.runner, 'format_repair_count', 0)

    def invalidate(self) -> None:
        self._valid = False

    async def run(self, context):
        if not self._valid:
            raise StaleExecutionError('Runner binding is no longer current')
        task = asyncio.current_task()
        cancellations = task.cancelling()
        try:
            result = await self.runner.run(context)
        except BaseException as exc:
            if not self._valid:
                raise StaleExecutionError('Late failure belongs to an invalid Runner binding') from exc
            if task.cancelling() > cancellations and not isinstance(exc, asyncio.CancelledError):
                raise asyncio.CancelledError('Runner failed after cancellation was requested') from exc
            raise
        if task.cancelling() > cancellations:
            raise asyncio.CancelledError('Runner returned after cancellation was requested')
        if not self._valid:
            raise StaleExecutionError('Late output belongs to an invalid Runner binding')
        return result


class LoopRun:
    """One task's loop binding. Counters and receipts survive blocked/resumed runs."""

    def __init__(self, memory: RunMemory, model, registry, *, runner_factory,
                 step_description: str, max_executions=16, step_timeout_seconds=60.0,
                 max_tool_rounds=4, max_total_tool_calls=8, context_manager=None):
        if type(max_executions) is not int or max_executions < 1:
            raise ValueError("max_executions must be an integer >= 1")
        if (isinstance(step_timeout_seconds, bool) or not isinstance(step_timeout_seconds, (int, float))
                or not math.isfinite(step_timeout_seconds) or step_timeout_seconds <= 0):
            raise ValueError("step_timeout_seconds must be finite and positive")
        self.memory = memory
        self._registry = registry
        self._context_manager = context_manager or ContextManager()
        self._runner_limits = RunnerLimits(max_tool_rounds, max_total_tool_calls)
        self._runner_factory = runner_factory
        self._runner = self._assemble_runner(model, registry, self._context_manager)
        self._step_description = step_description
        self._max_executions = max_executions
        # A loop never replays a failed task: its one step is the whole task (D-076).
        self._max_replans = 0
        self._step_timeout = step_timeout_seconds
        self._execution_count = self._replan_count = self._plan_revision = 0
        self._answers_seen = 0
        self._steps = []
        self._step_log = []
        self._meter = None
        self._final = None
        self._lock = asyncio.Lock()
        self._on_event = None

    def _assemble_runner(self, model, registry, context_manager):
        self._watch = SideEffectWatch(registry)
        dependencies = RunnerDependencies(model, self._watch, context_manager, self._runner_limits)
        runner = _BoundRunner(self._runner_factory(dependencies))
        if not callable(getattr(runner.runner, 'run', None)):
            raise TypeError("runner must provide an async run(context) method")
        return runner

    def rebind(self, model, registry, *, context_manager):
        if self._lock.locked() or self.memory.task_state.status is TaskStatus.RUNNING:
            raise RuntimeError('cannot rebind a running task')
        runner = self._assemble_runner(model, registry, context_manager)
        self._runner.invalidate()
        self._registry, self._context_manager, self._runner = registry, context_manager, runner

    @property
    def format_repair_count(self):
        return self._runner.format_repair_count

    async def run(self, *, on_event=None) -> AgentRunResult:
        """on_event, if given, is called synchronously with host-authored progress
        records ('plan', 'step'); it must not block, and its errors never affect the task."""
        from app.agent.hooks import CallbackHook, TASK_END, TASK_START, current_hooks, root_span, using_hooks
        async with self._lock:
            # The callback becomes one observer among the run's hooks (D-059).
            hooks = current_hooks()
            if on_event is not None:
                hooks = hooks.with_observers(CallbackHook(on_event))
            # The run is the root span of its trace (D-073).
            with using_hooks(hooks), root_span() as span_id:
                span = {'span_id': span_id, 'parent_id': None}
                self._on_event = on_event
                hooks.emit({'type': TASK_START, 'task_id': self.memory.task_state.task_id, **span})
                try:
                    result = await self._run_locked()
                except BaseException as exc:
                    hooks.emit({'type': TASK_END, 'task_id': self.memory.task_state.task_id,
                                'status': 'cancelled' if isinstance(exc, asyncio.CancelledError) else 'error',
                                'error': type(exc).__name__, **span})
                    raise
                finally:
                    self._on_event = None
                usage = result.usage or {}
                hooks.emit({'type': TASK_END, 'task_id': result.task_id, 'status': result.status.value,
                            'failure_kind': result.failure_kind, 'model_calls': usage.get('model_calls'),
                            'total_tokens': usage.get('total_tokens'),
                            'cached_prompt_tokens': usage.get('cached_prompt_tokens'), **span})
                return result

    def _emit(self, event: dict) -> None:
        from app.agent.hooks import current_hooks
        current_hooks().emit(event)          # observer errors are logged by type only (D-045, D-059)

    async def _run_locked(self):
        task = self.memory.task_state
        try:
            with self._meter_scope():
                result = await self._run()
            logger.info('Task {}: executions {}, replans {}, model calls {}, tokens {}, cached prompt tokens {}',
                        result.status.value, result.execution_count, result.replan_count,
                        result.usage['model_calls'], result.usage['total_tokens'],
                        _cache_text(result.usage))
            for number, entry in enumerate(result.usage.get('calls', ()), 1):
                logger.info('Task model call {}: {}', number, _call_text(entry))
        except StaleExecutionError:
            raise
        except (Exception, asyncio.CancelledError) as exc:
            content = ('Agent execution cancelled; tool side effects may already exist'
                       if isinstance(exc, asyncio.CancelledError) else f'Agent execution failed: {type(exc).__name__}')
            if task.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                task.fail_task(content)
            raise
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

    async def _run(self) -> AgentRunResult:
        task = self.memory.task_state
        if self._final is not None and task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            return self._final
        needs_plan = True
        content = None

        while task.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            blockers = [q for q in task.scratchpad.open_questions
                        if q.goal_revision == task.goal_revision and q.blocking and q.resolved_at is None]
            if blockers:
                # The state was blocked by the previous attempt.
                return self._result("\n".join(q.content for q in blockers))
            spent = self._meter.budget_problem()
            if spent:
                return self._fail(spent, 'budget')
            if needs_plan:
                if self._execution_count >= self._max_executions:
                    return self._fail("Agent execution limit reached")
                answers = sum(1 for q in task.scratchpad.open_questions
                              if q.goal_revision == task.goal_revision and q.resolved_at is not None)
                if self._plan_revision and answers > self._answers_seen:
                    # Planning again after the user answered is the normal flow, not a failure (D-033).
                    self._answers_seen = answers
                elif self._plan_revision:
                    if self._replan_count >= self._max_replans:
                        return self._fail("Agent replanning limit reached" + self._last_failure(), 'limit')
                    self._replan_count += 1
                try:
                    from app.agent.hooks import trace_scope
                    revision = _int_or_none(self._plan_revision)
                    with trace_scope('plan', revision=None if revision is None else revision + 1):
                        self._plan_revision += 1
                        next_step = PlanStep(str(uuid4()), self._step_description, StepStatus.PENDING,
                                             task.goal_revision, self._plan_revision, datetime.now(UTC))
                        self._resource_version = self.memory.resources.context_version
                    needs_plan = False
                    logger.info('Plan revision {}: {} step(s)', self._plan_revision, 1)
                    self._emit({'type': 'plan', 'revision': self._plan_revision,
                                'steps': [self._step_description],
                                'step_ids': [next_step.step_id]})
                except Exception as exc:
                    spent = self._meter.budget_problem()
                    if spent:
                        return self._fail(spent, 'budget')
                    denied = _policy_denial(exc)
                    if denied is not None:
                        return self._fail(f'Model call denied by {denied.hook}: {denied.reason}', 'policy')
                    # Planner validation messages are host-authored (see bench _SAFE_FAILURE).
                    return self._fail(f"Planning failed: {type(exc).__name__}: {exc}", 'planning')
            if content is not None and self._steps and not needs_plan:
                try:
                    self._check_resource_context()
                    task.complete()
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
                    from app.agent.hooks import pausable_deadline, trace_scope
                    # Time spent waiting for the user's approval does not count (D-061).
                    with trace_scope('step', index=self._execution_count,
                                     revision=_int_or_none(self._plan_revision)):
                        async with asyncio.timeout(self._meter.bound(self._step_timeout)) as deadline:
                            with pausable_deadline(deadline):
                                receipt = await self._execute(next_step)
                except BaseException as exc:
                    self._log_step(next_step.step_id, started, exc)
                    raise
                self._log_step(next_step.step_id, started, None)
                self._steps.append(receipt)
                content = receipt.result_summary
            except StepBlockedError as exc:
                return self._result(exc.message)
            except (StepFormatError, StepModelError) as exc:
                # Local format repair is exhausted, or the provider failed.
                # Replanning would repeat completed tools without fixing either.
                denied = _policy_denial(exc)
                if denied is not None:
                    return self._fail(f'Model call denied by {denied.hook}: {denied.reason}', 'policy')
                kind = 'format' if isinstance(exc, StepFormatError) else 'model'
                return self._fail(f"{type(exc).__name__}: {exc.message}", kind)
            except StepToolError as exc:
                if self._watch.uncertain:
                    # A write or run raised: it may have taken effect, so never replay it (D-032).
                    return self._fail(f"{type(exc).__name__}: {exc.message}; automatic replanning is disabled",
                                      'side_effect')
                needs_plan = True
                continue
            except StepRunError:
                needs_plan = True
                continue
            except StaleExecutionError:
                # Never overwrite evidence or lifecycle belonging to newer work.
                raise
            except TimeoutError:
                if self._runner.runner_id == 'agent_team':
                    # A team owns many side effects; an outer deadline must not replay it.
                    return self._fail('Team execution timed out; automatic replanning is disabled', 'timeout')
                if self._context_manager.overflow_recovery_interrupted:
                    return self._fail("Context overflow recovery timed out; completed tools will not be repeated",
                                      'timeout')
                if self._watch.touched:
                    # The step started a write or run before the deadline (D-032).
                    return self._fail("Step execution timed out after a write or run; automatic replanning "
                                      "is disabled", 'side_effect')
                task.add_failure("Step execution timed out; only read-only tools had run")
                needs_plan = True
            except Exception as exc:
                # Contract/implementation errors are terminal, not retry instructions.
                # Only the type name: arbitrary exception text may carry host paths or provider output.
                return self._fail(f"Execution failed: {type(exc).__name__}", 'internal')

        if task.status is TaskStatus.FAILED:
            return self._result(task.scratchpad.failure_history[-1].message, 'business')
        return self._result(content or "")

    def _check_resource_context(self):
        if self.memory.resources.context_version != self._resource_version:
            raise InvalidStateTransitionError(
                "Plan is unbound or selected context changed; install a fresh plan")

    def _source_refs(self, refs):
        if not isinstance(refs, tuple) or not all(isinstance(ref, str) for ref in refs):
            raise TypeError("source_refs must be a tuple of strings")
        return tuple(self.memory.resources.get(ref).resource_id for ref in refs)

    async def _execute(self, step):
        task = self.memory.task_state
        self._check_resource_context()
        task.start(step.step_id)
        version = task.state_version
        current_step = replace(step, status=StepStatus.ACTIVE, started_at=datetime.now(UTC))
        context = step_context(self.memory, current_step, self._plan_revision)

        def current():
            return (task.state_version == version and task.status is TaskStatus.RUNNING
                    and task.active_step_id == step.step_id)

        try:
            output = await self._runner.run(context)
            output = self._validate_runner_output(output)
            if output.outcome is StepOutcome.BLOCKED:
                raise StepBlockedError(output.reason.strip(), output.source_refs)
            if output.outcome is StepOutcome.FAILED:
                details = output.reason.strip() + "\n" + output.content
                if output.observation:
                    details += "\nTool evidence: " + output.observation
                raise StepBusinessError(details, output.source_refs)
        except asyncio.CancelledError as exc:
            if not current():
                raise StaleExecutionError('Cancelled execution is no longer current') from exc
            raise
        except StepRunError as exc:
            if not current():
                raise StaleExecutionError('Failed execution is no longer current') from exc
            refs = self._source_refs(exc.source_refs)
            if isinstance(exc, StepBlockedError):
                task.block(exc.message, step.step_id, plan_revision=self._plan_revision, source_refs=refs)
            else:
                message = exc.message
                if isinstance(exc, (StepBusinessError, StepFormatError, StepModelError, StepToolError)):
                    message = f"{type(exc).__name__}: {message}"
                task.add_failure(message, source_refs=refs)
            raise
        except Exception as exc:
            if not current():
                raise StaleExecutionError('Invalid execution is no longer current') from exc
            raise
        if not current():
            raise StaleExecutionError('Execution is no longer current')
        self._check_resource_context()
        return StepExecutionReceipt(
            task_id=task.task_id, step_id=step.step_id, goal_revision=task.goal_revision,
            plan_revision=self._plan_revision, state_version=task.state_version, result_id=str(uuid4()),
            result_summary=output.content, source_refs=output.source_refs,
            tool_calls=output.tool_calls, tool_results=output.tool_results)

    def _validate_runner_output(self, output: StepRunOutput) -> StepRunOutput:
        """Validate and normalize with the legacy StepExecutor contract, without mutating memory."""
        if not isinstance(output, StepRunOutput):
            raise TypeError("output must be a StepRunOutput")
        if not isinstance(output.outcome, StepOutcome):
            raise TypeError("output.outcome must be a StepOutcome")
        if output.outcome is StepOutcome.SUCCEEDED:
            if output.reason is not None:
                raise ValueError("successful output must not contain a reason")
        elif not isinstance(output.reason, str) or not output.reason.strip():
            raise ValueError("failed/blocked output must contain a reason")

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
                normalized_refs.append(self.memory.resources.get(ref).resource_id)
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

    def _log_step(self, step_id, started, exc):
        """Record one step attempt: outcome, runner and tool calls, without model or tool text."""
        outcome = ('succeeded' if exc is None else 'blocked' if isinstance(exc, StepBlockedError)
                   else 'timeout' if isinstance(exc, TimeoutError) else 'failed')
        entry = {'step': self._execution_count, 'step_id': step_id, 'outcome': outcome,
                 'error': None if exc is None or outcome == 'blocked' else type(exc).__name__,
                 'runner': self._runner.runner_id,
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
