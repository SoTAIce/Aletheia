"""Real stdio MCP and the agent loop; HTTP and model responses are deterministic."""

import json
from pathlib import Path
import sys
from textwrap import dedent
from unittest.mock import AsyncMock

from mcp import StdioServerParameters
import pytest

from app.agent.agent_loop import AgentLoopRunner, LOOP_STEP, LoopResumeStore
from app.agent.loop_run import LoopRun
from app.agent.step_models import ModelTurn, ToolCall, ToolMessage
from app.models.run_state import RunMemory, RunState
from app.models.taskstate import TaskStatus
from app.tools.mcp_client import MCPClient
from app.tools.mcp_tool_loader import register_mcp_tools
from app.tools.tool_registry import ToolRegistry
from app.tools.web_search.server import create_service


def server_parameters(status=200, empty=False):
    # Replace only the HTTP transport inside the child process. Execute the
    # production server module, including its environment-based stdio entry.
    bootstrap = dedent(f"""
        import json
        import runpy
        import httpx

        original_client = httpx.AsyncClient
        def respond(request):
            assert request.method == "POST"
            assert str(request.url) == "https://api.tavily.com/search"
            assert request.headers["Authorization"] == "Bearer test-tavily-key"
            assert json.loads(request.content) == {{
                "query": "Aletheia MCP", "max_results": 1, "search_depth": "basic"
            }}
            return httpx.Response({status}, json={{"results": [] if {empty!r} else [{{
                "title": "Aletheia 文档", "url": "https://example.com/aletheia",
                "content": "MCP connects tools to the agent."
            }}]}})
        class TestClient(original_client):
            def __init__(self, **kwargs):
                super().__init__(transport=httpx.MockTransport(respond), **kwargs)
        httpx.AsyncClient = TestClient
        runpy.run_module("app.tools.web_search.server", run_name="__main__")
    """)
    return StdioServerParameters(
        command=sys.executable, args=["-c", bootstrap],
        cwd=str(Path(__file__).resolve().parents[2]),
        env={"TAVILY_API_KEY": "test-tavily-key", "PYTHONDONTWRITEBYTECODE": "1"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status,empty", [(200, False), (200, True), (401, False)])
async def test_search_stdio_runner_and_cleanup(status, empty):
    registry = ToolRegistry()
    model = AsyncMock()
    call = ToolCall("search-1", "search_web", {"query": "Aletheia MCP", "max_results": 1})

    async def generate(messages, tools):
        [search] = [tool for tool in tools if tool.name == "search_web"]   # next to the loop's own controls
        assert search.description
        assert search.input_schema["required"] == ["query"]
        if not isinstance(messages[-1], ToolMessage):
            return ModelTurn(tool_calls=(call,))
        # The second model turn must actually receive the MCP tool evidence.
        assert messages[-1].call_id == call.call_id
        reply = json.loads(messages[-1].content)
        assert reply["success"] is (status == 200)
        if status == 200:
            results = json.loads(reply["content"])
            if empty:
                assert results == []
                return ModelTurn("No search results found.")
            assert results == [{
                "title": "Aletheia 文档", "url": "https://example.com/aletheia",
                "snippet": "MCP connects tools to the agent.",
            }]
            return ModelTurn(f"MCP connects tools: {results[0]['url']}")
        assert "401" in reply["error"]
        return ModelTurn("Search failed; evidence could not be verified.")

    model.generate.side_effect = generate
    memory = RunMemory(RunState("user", "Find Aletheia MCP documentation"))
    async with MCPClient.connect(server_parameters(status, empty)) as client:
        await register_mcp_tools(client, registry)
        run = LoopRun(memory, model, registry, step_description=LOOP_STEP, runner_factory=lambda deps:
                      AgentLoopRunner(deps.model, deps.tool_registry, None, deps.context_manager, LoopResumeStore()))
        result = await run.run()
        assert result.status is TaskStatus.COMPLETED and model.generate.await_count == 2
        [receipt] = result.steps
        assert receipt.tool_calls == (call,)
        assert receipt.tool_results[0].success is (status == 200)
        if status == 200 and not empty:
            assert "https://example.com/aletheia" in result.content
        invalid = await registry.invoke(ToolCall("invalid", "search_web", {"query": " "}))
        assert not invalid.success and "query" in invalid.error
    with pytest.raises(RuntimeError, match="closed"):
        await registry.invoke(call)


def test_missing_environment_key(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    with pytest.raises(ValueError, match="TAVILY_API_KEY"):
        create_service()


@pytest.mark.asyncio
async def test_demo_runs_step_inside_connection(monkeypatch, capsys):
    from app.agent.mcp_demo import web_search_client as demo

    connect = MCPClient.connect
    def connect_test_server(parameters):
        assert parameters.args == ["-m", "app.tools.web_search.server"]
        assert parameters.env["TAVILY_API_KEY"] == "test-tavily-key"
        return connect(server_parameters())

    model = AsyncMock()
    model.generate.side_effect = [
        ModelTurn(tool_calls=(ToolCall(
            "demo-search", "search_web", {"query": "Aletheia MCP", "max_results": 1}
        ),)),
        ModelTurn("Search complete."),
    ]
    monkeypatch.setenv("TAVILY_API_KEY", "test-tavily-key")
    monkeypatch.setattr(demo.MCPClient, "connect", connect_test_server)
    monkeypatch.setattr(demo, "AsyncOpenAI", lambda **kwargs: AsyncMock())
    monkeypatch.setattr(demo, "OpenAICompatibleToolCallingModel", lambda *args: model)
    await demo.main("Aletheia MCP")
    assert "Search complete." in capsys.readouterr().out
    reply = json.loads(model.generate.call_args_list[1].args[0][-1].content)
    assert reply["success"] and "https://example.com/aletheia" in reply["content"]
