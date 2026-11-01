from dataclasses import FrozenInstanceError
from unittest.mock import AsyncMock

import pytest

from app.services.context_manager import ContextManager
from app.tools.tool_registry import ToolRegistry


def dependencies():
    from app.agent.runner_registry import RunnerDependencies, RunnerLimits
    return RunnerDependencies(AsyncMock(), ToolRegistry(), ContextManager(), RunnerLimits(2, 3))


def test_registry_freezes_definitions_and_snapshots_factories():
    from app.agent.runner_registry import RunnerDefinition, RunnerRegistry
    definition = RunnerDefinition('fake', 'Test only', frozenset({'test'}))
    runner = AsyncMock()
    registry = RunnerRegistry()
    registry.register(definition, lambda deps: runner)
    snapshot = registry.snapshot()
    registry.register(RunnerDefinition('other', 'Other test'), lambda deps: AsyncMock())
    assert snapshot.definitions() == (definition,)
    assert snapshot.bind(dependencies())[0].runner is runner
    with pytest.raises(FrozenInstanceError):
        definition.runner_id = 'changed'
    with pytest.raises(RuntimeError, match='snapshot'):
        snapshot.register(definition, lambda deps: runner)
    with pytest.raises(ValueError, match='registered'):
        registry.register(definition, lambda deps: runner)


@pytest.mark.parametrize('args', [('', 'description'), (' bad ', 'description'), ('id', ''),
                                  ('id', 'description', {'mutable'}), ('id', 'description', frozenset({''}))])
def test_invalid_definition_is_rejected(args):
    from app.agent.runner_registry import RunnerDefinition
    with pytest.raises((TypeError, ValueError)):
        RunnerDefinition(*args)


def test_factory_uses_explicit_binding_dependencies_and_default_has_one_runner():
    from app.agent.runner_registry import default_runner_registry
    from app.agent.tool_calling_step_runner import ToolCallingStepRunner
    deps = dependencies()
    registry = default_runner_registry()
    assert [d.runner_id for d in registry.definitions()] == ['tool_calling']
    bound = registry.bind(deps)[0]
    assert isinstance(bound.runner, ToolCallingStepRunner)
    assert bound.runner._model is deps.model and bound.runner._tool_registry is deps.tool_registry
    assert bound.runner._context_manager is deps.context_manager
    assert bound.runner._max_tool_rounds == 2 and bound.runner._max_total_tool_calls == 3
    assert bound.runner._require_step_outcome is True


def test_bad_factories_fail_during_binding_and_never_open_resources():
    from app.agent.runner_registry import RunnerRegistry, RunnerDefinition
    registry = RunnerRegistry()
    with pytest.raises(TypeError):
        registry.register(RunnerDefinition('bad', 'bad'), None)
    async def async_factory(deps):
        return AsyncMock()
    with pytest.raises(TypeError):
        registry.register(RunnerDefinition('bad', 'bad'), async_factory)
    registry.register(RunnerDefinition('bad', 'bad'), lambda deps: object())
    with pytest.raises(TypeError, match='run'):
        registry.bind(dependencies())


@pytest.mark.parametrize('args', [(True, 1), (1, False), (-1, 1), (1, -1)])
def test_limits_keep_original_strict_validation(args):
    from app.agent.runner_registry import RunnerLimits
    with pytest.raises((TypeError, ValueError)):
        RunnerLimits(*args)
