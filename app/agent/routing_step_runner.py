"""One dispatch per attempt, using trusted requirements and bounded bindings."""
import asyncio
from dataclasses import dataclass
import inspect

from app.agent.runner_registry import BoundRunner
from app.agent.runner_router import (CapabilityRoutingPolicy, RouteDecision, RunnerRequirements,
                                     RequirementsProvider, RoutingPolicy, no_runner_requirements)
from app.agent.step_models import StepExecutionContext, StepRunOutput
from app.agent.step_runner import StepRoutingError
from app.models.working_memory import StaleExecutionError


@dataclass(frozen=True, slots=True)
class RoutingEvent:
    task_id: str
    step_id: str
    goal_revision: int
    plan_revision: int
    runner_id: str
    required_capabilities: frozenset[str]
    reason: str


def _synchronous(value, source):
    if inspect.isawaitable(value):
        if inspect.iscoroutine(value):
            value.close()
        raise StepRoutingError(f'{source} must be synchronous')
    return value


class RoutingStepRunner:
    def __init__(self, bindings: tuple[BoundRunner, ...], *, routing_policy: RoutingPolicy | None = None,
                 requirements_provider: RequirementsProvider | None = None, events: list[RoutingEvent] | None = None):
        self._bindings = {b.definition.runner_id: b for b in bindings}
        if len(self._bindings) != len(bindings):
            raise ValueError('duplicate bound Runner IDs')
        self._policy = routing_policy if routing_policy is not None else CapabilityRoutingPolicy()
        self._requirements = requirements_provider if requirements_provider is not None else no_runner_requirements
        if not callable(getattr(self._policy, 'select', None)) or not callable(self._requirements):
            raise TypeError('routing policy/provider must be callable')
        self._events = events if events is not None else []
        self._valid = True

    @property
    def decisions(self) -> tuple[RoutingEvent, ...]:
        return tuple(self._events)

    @property
    def format_repair_count(self) -> int:
        return sum(getattr(binding.runner, 'format_repair_count', 0) for binding in self._bindings.values())

    def invalidate(self) -> None:
        self._valid = False

    async def run(self, context: StepExecutionContext) -> StepRunOutput:
        if not self._valid:
            raise StaleExecutionError('Runner binding is no longer current')
        task = asyncio.current_task()
        cancellations = task.cancelling()
        try:
            required = _synchronous(self._requirements(context), 'requirements provider')
            if not isinstance(required, RunnerRequirements):
                raise StepRoutingError('requirements provider must return RunnerRequirements')
            # Capture constraints BEFORE calling the policy. Decisions cannot
            # supply or relax this independently trusted capability set.
            capabilities = frozenset(required.capabilities)
            definitions = tuple(b.definition for b in self._bindings.values())
            decision = _synchronous(self._policy.select(context, definitions, RunnerRequirements(capabilities)), 'routing policy')
            if not isinstance(decision, RouteDecision):
                raise StepRoutingError('routing policy must return RouteDecision')
            binding = self._bindings.get(decision.runner_id)
            if binding is None:
                raise StepRoutingError(f'unknown Runner: {decision.runner_id}')
            if not capabilities <= binding.definition.capabilities:
                raise StepRoutingError('selected Runner does not satisfy trusted capabilities')
        except StepRoutingError:
            raise
        except Exception as exc:
            raise StepRoutingError(f'Runner routing failed: {type(exc).__name__}') from exc
        self._events.append(RoutingEvent(context.task_id, context.current_step.step_id,
            context.goal_revision, context.plan_revision, decision.runner_id, capabilities, decision.reason))
        # Do not catch declared Runner failures or dispatch to another Runner.
        try:
            result = await binding.runner.run(context)
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
