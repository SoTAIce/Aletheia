"""Deterministic tests for sandbox step 3: run tools wired into both modes (sandbox-step3-001).

No real model, MCP process or Docker: the sandbox and tools are faked.
"""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.agent.__main__ import sandbox_problem, team_options_for
from app.agent.step_models import ModelTurn, ToolCall, ToolDefinition, ToolOutput, ToolResult
from app.agent.team.models import has_passing_run_after_last_write
from app.agent.delegation import READ_TOOLS
from app.tools.sandbox import server as sandbox_server
from app.tools.sandbox.executor import DockerSandbox, RunResult, SandboxError
from app.tools.tool_loader import local_mcp_servers
from app.tools.tool_registry import ToolRegistry
from test_sandbox_step1 import _context


def test_cli_options_without_sandbox_are_unchanged():
    assert team_options_for('team', 'w').require_passing_run is False
    assert 'run_tests' not in team_options_for('team', 'w').allowed_tools
    assert team_options_for('single', 'w', True) is None
    assert sandbox_problem('w', False) is None


def test_cli_sandbox_enables_run_tools_gate_and_timeout():
    options = team_options_for('team', 'w', True, 400)
    assert {'write_file', 'run_tests', 'run_python'} <= options.allowed_tools
    assert options.require_passing_run is True and options.timeout_seconds == 400
    assert team_options_for('team', None, False, 300).allowed_tools == READ_TOOLS


def test_sandbox_is_refused_before_any_model_call(tmp_path, monkeypatch):
    assert 'requires --write-root' in sandbox_problem(None, True)
    monkeypatch.setattr(DockerSandbox, 'available', lambda self: False)
    assert 'image not available' in sandbox_problem(str(tmp_path), True)
    assert 'invalid' in sandbox_problem(str(tmp_path / 'missing'), True)
    monkeypatch.setattr(DockerSandbox, 'available', lambda self: True)
    assert sandbox_problem(str(tmp_path), True) is None


def test_loader_adds_sandbox_server_without_secrets(tmp_path):
    servers = local_mcp_servers(filesystem_root=tmp_path, write_root=tmp_path, sandbox=True,
                                tavily_api_key='')
    sandbox = [s for s in servers if 'app.tools.sandbox.server' in s.args]
    assert len(sandbox) == 1
    assert sandbox[0].args[-2:] == ['--workspace', str(tmp_path.resolve())]
    assert sandbox[0].env == {'PYTHONDONTWRITEBYTECODE': '1'}
    assert not any('app.tools.sandbox.server' in s.args for s in local_mcp_servers(
        filesystem_root=tmp_path, write_root=tmp_path))
    with pytest.raises(ValueError, match='write_root'):
        local_mcp_servers(filesystem_root=tmp_path, sandbox=True)


class FakeSandbox:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, []

    def run_tests(self, path):
        self.calls.append(('tests', path))
        if self.error:
            raise self.error
        return self.result

    def run_python(self, path, args):
        self.calls.append(('python', path, tuple(args)))
        if self.error:
            raise self.error
        return self.result


def test_server_tools_return_summaries_and_turn_refusals_into_tool_errors():
    ok = FakeSandbox(RunResult(0, False, '2 passed', False, 1.25))
    assert sandbox_server.run_tests_text(ok, '') == 'exit code 0; 1.2s\n2 passed'
    assert ok.calls == [('tests', None)]
    sandbox_server.run_python_text(ok, 'main.py', ['a'])
    assert ok.calls[-1] == ('python', 'main.py', ('a',))
    bad = FakeSandbox(error=SandboxError('path must stay inside the workspace'))
    with pytest.raises(ValueError, match='inside the workspace'):
        sandbox_server.run_python_text(bad, '../x.py')
    assert sandbox_server.RUN_TOOLS == {'run_tests', 'run_python'}
    assert sandbox_server.READ_ONLY_RUN_TOOL == 'run_python_code'


def _result(name, content='', success=True):
    return ToolResult('c', name, success, content)


@pytest.mark.parametrize('results, expected', [
    ([], False),
    ([_result('run_tests', 'exit code 0; 1s')], True),
    ([_result('write_file'), _result('run_tests', 'exit code 1; 1s')], False),
    ([_result('run_tests', 'exit code 0; 1s'), _result('write_file')], False),
    ([_result('write_file'), _result('run_python', 'exit code 0; 1s')], True),
    ([_result('write_file'), _result('run_tests', 'timed out (limit reached, process killed); 30s')], False),
    ([_result('write_file'), _result('run_tests', 'exit code 0; 1s', success=False)], False),
    ([_result('write_file'), _result('run_tests', 'exit code 0; 1s'), _result('write_file', success=False)], True),
])
def test_passing_run_must_follow_last_write(results, expected):
    assert has_passing_run_after_last_write(results) is expected


class Scripted:
    def __init__(self, *turns):
        self.turns, self.requests = list(turns), []

    async def generate(self, messages, tools):
        self.requests.append(tuple(messages))
        return self.turns.pop(0)


def call(identity, name, **args):
    return ModelTurn(tool_calls=(ToolCall(identity, name, args),))


def act(identity, action, member='', target='', instruction='x', tools='', version=0):
    return call(identity, 'submit_result', payload={'action': action, 'member_id': member, 'target_id': target,
                'instruction': instruction, 'tools': tools, 'expected_version': version})


class FakeTool:
    def __init__(self, name, content):
        self.definition = ToolDefinition(name, name, {'type': 'object'})
        self.content, self.calls = content, []

    async def invoke(self, arguments):
        self.calls.append(arguments)
        return ToolOutput(True, self.content)


