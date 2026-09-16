from dataclasses import FrozenInstanceError, fields
import subprocess
import sys
from typing import get_type_hints

import pytest

from app.agent.step_models import ModelTurn, ToolCall, ToolDefinition, ToolOutput
from app.agent.step_runner import StepRunError, StepRunnerContractError
from app.agent.tool_calling_model import ToolCallingModel
from app.tools.tool import Tool
from app.tools.tool_registry import ToolRegistry


def test_shared_models_and_protocol_annotations():
    output = ToolOutput(True, 'result')
    assert output.source_refs == () and output.error is None
    assert {field.name for field in fields(output)} == {
        'success', 'content', 'source_refs', 'error'
    }
    call = ToolCall('call', 'read', {})
    turn = ModelTurn(tool_calls=(call,))
    assert turn.content is None
    with pytest.raises(FrozenInstanceError):
        output.content = 'changed'
    with pytest.raises(FrozenInstanceError):
        turn.content = 'changed'
    assert get_type_hints(Tool.invoke)['return'] is ToolOutput
    assert get_type_hints(Tool.definition.fget)['return'] is ToolDefinition
    assert get_type_hints(ToolRegistry.get)['return'] is Tool
    assert get_type_hints(ToolCallingModel.generate)['return'] is ModelTurn
    assert not issubclass(StepRunnerContractError, StepRunError)


@pytest.mark.parametrize('modules', [
    ['app.tools.tool', 'app.tools.tool_registry', 'app.agent.step_runner',
     'app.agent.tool_calling_model', 'app.agent.tool_calling_step_runner'],
    ['app.agent.step_runner', 'app.agent.tool_calling_step_runner',
     'app.agent.tool_calling_model', 'app.tools.tool_registry', 'app.tools.tool'],
])
def test_boundaries_import_without_concrete_tools(modules):
    script = '\n'.join(f'import {module}' for module in modules)
    script += '\nimport sys\nassert "app.tools.knowledge_tool" not in sys.modules'
    script += '\nassert "app.services.vector_store_manager" not in sys.modules'
    subprocess.run([sys.executable, '-c', script], check=True)


def test_legacy_tool_exports_remain_available_without_eager_loading():
    # Stub concrete modules to test exports without initializing RAG services.
    script = '''
import sys
from types import ModuleType
import app.tools
knowledge = ModuleType('app.tools.knowledge_tool')
clock = ModuleType('app.tools.time_tool')
knowledge.retrieve_knowledge = object()
clock.get_current_time = object()
sys.modules[knowledge.__name__] = knowledge
sys.modules[clock.__name__] = clock
from app.tools import DEFAULT_LOCAL_AGENT_TOOLS, retrieve_knowledge, get_current_time
assert DEFAULT_LOCAL_AGENT_TOOLS == (knowledge.retrieve_knowledge, clock.get_current_time)
assert retrieve_knowledge is knowledge.retrieve_knowledge
assert get_current_time is clock.get_current_time
'''
    subprocess.run([sys.executable, '-c', script], check=True)
