"""Stage 3b boundaries: removed routing and independent team implementations stay removed."""
import ast
import inspect
from importlib.util import find_spec
from pathlib import Path

import pytest

REMOVED = ('team_v2', 'runner_registry', 'runner_router', 'routing_step_runner')
ROOT = Path(__file__).resolve().parents[1]

@pytest.mark.parametrize('name', REMOVED)
def test_legacy_module_is_removed(name):
    assert not (ROOT / 'app' / 'agent' / (name + '.py')).exists()
    assert find_spec('app.agent.' + name) is None

@pytest.mark.parametrize('directory', ['app', 'bench', 'tests'])
def test_no_imports_of_removed_modules(directory):
    found = []
    for path in (ROOT / directory).rglob('*.py'):
        for node in ast.walk(ast.parse(path.read_bytes())):
            modules = ([node.module or ''] if isinstance(node, ast.ImportFrom) else
                       [a.name for a in node.names] if isinstance(node, ast.Import) else [])
            if any(m == 'app.agent.' + name or m.startswith('app.agent.' + name + '.')
                   for m in modules for name in REMOVED):
                found.append(str(path.relative_to(ROOT)))
            if isinstance(node, ast.ImportFrom) and node.module == 'app.agent':
                if any(a.name in REMOVED for a in node.names):
                    found.append(str(path.relative_to(ROOT)))
    assert found == []

@pytest.mark.parametrize('target', ['service', 'runtime'])
def test_removed_constructor_parameters(target):
    from app.agent.runtime import AgentRuntime
    from app.services.rag_agent_service import RagAgentService
    cls = RagAgentService if target == 'service' else AgentRuntime
    removed = {'runner_registry', 'routing_policy', 'runner_requirements', 'team_impl'}
    assert removed.isdisjoint(inspect.signature(cls).parameters)
