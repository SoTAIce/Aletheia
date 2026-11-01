"""Synchronous routing decisions; capability requirements come from application code."""
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from app.agent.runner_registry import RunnerDefinition, validate_capabilities, validate_identifier
from app.agent.step_models import StepExecutionContext
from app.agent.step_runner import StepRoutingError


@dataclass(frozen=True, slots=True)
class RunnerRequirements:
    capabilities: frozenset[str] = frozenset()

    def __post_init__(self):
        validate_capabilities(self.capabilities)


@dataclass(frozen=True, slots=True)
class RouteDecision:
    runner_id: str
    reason: str

    def __post_init__(self):
        validate_identifier(self.runner_id, 'runner_id')
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError('route reason must not be empty')


RequirementsProvider = Callable[[StepExecutionContext], RunnerRequirements]


def no_runner_requirements(context: StepExecutionContext) -> RunnerRequirements:
    return RunnerRequirements()


class RoutingPolicy(Protocol):
    def select(self, context: StepExecutionContext, definitions: tuple[RunnerDefinition, ...],
               requirements: RunnerRequirements) -> RouteDecision: ...


class CapabilityRoutingPolicy:
    def __init__(self, default_runner_id='tool_calling', *, preferred_runner_ids: tuple[str, ...] = ()):
        validate_identifier(default_runner_id, 'default_runner_id')
        if not isinstance(preferred_runner_ids, tuple):
            raise TypeError('preferred_runner_ids must be a tuple')
        for runner_id in preferred_runner_ids:
            validate_identifier(runner_id, 'preferred runner_id')
        self.default_runner_id = default_runner_id
        self.preferred_runner_ids = preferred_runner_ids

    def select(self, context, definitions, requirements):
        candidates = {d.runner_id for d in definitions if requirements.capabilities <= d.capabilities}
        if not requirements.capabilities:
            if self.default_runner_id not in candidates:
                raise StepRoutingError('default Runner is unavailable')
            return RouteDecision(self.default_runner_id, 'Default execution path')
        for runner_id in (*self.preferred_runner_ids, self.default_runner_id):
            if runner_id in candidates:
                return RouteDecision(runner_id, 'Configured preference satisfies required capabilities')
        if len(candidates) == 1:
            return RouteDecision(next(iter(candidates)), 'Unique capability match')
        if not candidates:
            raise StepRoutingError('no Runner satisfies required capabilities')
        raise StepRoutingError('ambiguous Runner capability match; configure a preference')
