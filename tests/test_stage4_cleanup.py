"""Stage 4c boundaries (D-076): plan mode and the plan/execute skeleton stay removed."""
import ast
import inspect
from importlib.util import find_spec
from pathlib import Path

import pytest

REMOVED = ('app.agent.planner', 'app.agent.executor', 'app.agent.step_executor',
           'app.agent.tool_calling_step_runner', 'app.agent.planning_model', 'app.models.working_memory')
REMOVED_NAMES = {'app.agent.runtime': ('AgentRuntime', '_BoundRunner', '_tool_calling_factory'),
                 'app.agent.agent_loop': ('LoopPlanner', 'TeamPlanner'),
                 'app.agent.step_runner': ('StepRoutingError',),
                 'app.models.taskstate': ('TaskState', 'TaskStateSnapshot')}
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('name', REMOVED)
def test_the_skeleton_module_is_removed(name):
    assert not (ROOT / (name.replace('.', '/') + '.py')).exists()
    assert find_spec(name) is None


@pytest.mark.parametrize('module, names', REMOVED_NAMES.items())
def test_removed_names_stay_removed(module, names):
    import importlib
    loaded = importlib.import_module(module)
    assert [n for n in names if hasattr(loaded, n)] == []


@pytest.mark.parametrize('directory', ['app', 'bench', 'tests'])
def test_nothing_imports_the_removed_modules(directory):
    found = []
    for path in (ROOT / directory).rglob('*.py'):
        for node in ast.walk(ast.parse(path.read_bytes())):
            modules = ([node.module or ''] if isinstance(node, ast.ImportFrom) else
                       [a.name for a in node.names] if isinstance(node, ast.Import) else [])
            if any(m == name or m.startswith(name + '.') for m in modules for name in REMOVED):
                found.append(str(path.relative_to(ROOT)))
    assert found == []


def test_the_service_and_loop_run_lost_their_plan_parameters():
    from app.agent.loop_run import LoopRun
    from app.services.rag_agent_service import RagAgentService
    assert {'planner_model', 'max_replans', 'planning_timeout_seconds'}.isdisjoint(
        inspect.signature(RagAgentService).parameters)
    assert {'max_replans', 'planning_timeout_seconds'}.isdisjoint(inspect.signature(LoopRun).parameters)
    assert list(inspect.signature(LoopRun.rebind).parameters) == ['self', 'model', 'registry', 'context_manager']


def test_plan_mode_timeout_setting_is_gone() -> None:
    from app.config import Settings

    assert 'agent_planning_timeout_seconds' not in Settings.model_fields
    # A stale .env line must still load: extra settings are ignored, not rejected.
    assert 'agent_planning_timeout_seconds' not in Settings(agent_planning_timeout_seconds=5).model_dump()
