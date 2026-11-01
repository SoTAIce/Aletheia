"""Trusted Runner declarations and request-scoped dependency binding."""
from collections.abc import Callable
from dataclasses import dataclass
import inspect

from app.agent.step_runner import StepRunner
from app.agent.tool_calling_model import ToolCallingModel
from app.agent.tool_calling_step_runner import ToolCallingStepRunner
from app.services.context_manager import ContextManager
from app.tools.tool_registry import ToolRegistry


def validate_identifier(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f'{field} must be a nonempty string without surrounding whitespace')


def validate_capabilities(value: frozenset[str]) -> None:
    if not isinstance(value, frozenset):
        raise TypeError('capabilities must be a frozenset')
    for item in value:
        validate_identifier(item, 'capability')


@dataclass(frozen=True, slots=True)
class RunnerDefinition:
    runner_id: str
    description: str
    capabilities: frozenset[str] = frozenset()

    def __post_init__(self):
        validate_identifier(self.runner_id, 'runner_id')
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError('description must not be empty')
        validate_capabilities(self.capabilities)


@dataclass(frozen=True, slots=True)
class RunnerLimits:
    max_tool_rounds: int = 4
    max_total_tool_calls: int = 8

    def __post_init__(self):
        for name, value in [('max_tool_rounds', self.max_tool_rounds), ('max_total_tool_calls', self.max_total_tool_calls)]:
            if type(value) is not int:
                raise TypeError(f'{name} must be an integer')
            if value < 0:
                raise ValueError(f'{name} must be non-negative')


@dataclass(frozen=True, slots=True)
class RunnerDependencies:
    model: ToolCallingModel
    tool_registry: ToolRegistry
    context_manager: ContextManager
    limits: RunnerLimits


@dataclass(frozen=True, slots=True)
class BoundRunner:
    definition: RunnerDefinition
    runner: StepRunner


RunnerFactory = Callable[[RunnerDependencies], StepRunner]


class RunnerRegistry:
    """Composition-time registration; snapshots exclude later registrations.

    Factories are synchronous constructors, not owners of network resources.
    Models/tool connections remain owned by the existing service contexts.
    """

    def __init__(self):
        self._entries: dict[str, tuple[RunnerDefinition, RunnerFactory]] = {}
        self._frozen = False

    def register(self, definition: RunnerDefinition, factory: RunnerFactory) -> None:
        if self._frozen:
            raise RuntimeError('cannot modify a RunnerRegistry snapshot')
        if not isinstance(definition, RunnerDefinition):
            raise TypeError('definition must be RunnerDefinition')
        if not callable(factory) or inspect.iscoroutinefunction(factory) or inspect.isasyncgenfunction(factory):
            raise TypeError('factory must be a synchronous callable')
        if definition.runner_id in self._entries:
            raise ValueError(f'Runner already registered: {definition.runner_id}')
        self._entries[definition.runner_id] = definition, factory

    def definitions(self) -> tuple[RunnerDefinition, ...]:
        return tuple(definition for definition, _ in self._entries.values())

    def snapshot(self) -> 'RunnerRegistry':
        snapshot = RunnerRegistry()
        snapshot._entries = dict(self._entries)
        snapshot._frozen = True
        return snapshot

    def bind(self, dependencies: RunnerDependencies) -> tuple[BoundRunner, ...]:
        if not isinstance(dependencies, RunnerDependencies):
            raise TypeError('dependencies must be RunnerDependencies')
        bindings = []
        for definition, factory in self._entries.values():
            runner = factory(dependencies)
            if inspect.isawaitable(runner):
                if inspect.iscoroutine(runner):
                    runner.close()
                raise TypeError('factory must return a Runner synchronously')
            if not callable(getattr(runner, 'run', None)):
                raise TypeError('Runner must provide run(context)')
            bindings.append(BoundRunner(definition, runner))
        return tuple(bindings)


def _tool_calling_factory(deps: RunnerDependencies) -> StepRunner:
    return ToolCallingStepRunner(deps.model, deps.tool_registry,
        deps.limits.max_tool_rounds, deps.limits.max_total_tool_calls,
        require_step_outcome=True, context_manager=deps.context_manager)


def default_runner_registry() -> RunnerRegistry:
    registry = RunnerRegistry()
    registry.register(RunnerDefinition('tool_calling', 'Existing tool-calling step execution',
                                      frozenset({'tool_calling'})), _tool_calling_factory)
    return registry
