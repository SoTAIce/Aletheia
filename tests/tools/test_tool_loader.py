from contextlib import asynccontextmanager
import json
from unittest.mock import AsyncMock

from mcp import StdioServerParameters
from mcp.types import Tool
import pytest

from app.models.taskstate import TaskState
from app.models.working_memory import WorkingMemory
from app.schemas.rag import Citation
from app.tools.knowledge_registry_tool import KnowledgeRegistryTool
from app.tools.tool_loader import local_mcp_servers, open_tool_registry


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["ok", "empty", "error"])
async def test_rag_artifact_status_and_resource_identity(status):
    memory = WorkingMemory(TaskState("user", "goal"))
    search = AsyncMock(return_value=("evidence", {"status": status, "error": "unavailable", "citations": []}))
    tool = KnowledgeRegistryTool(memory, search=search)
    result = await tool.invoke({"query": "query"})
    assert result.success is (status != "error")
    if status == "ok":
        again = await tool.invoke({"query": "query"})
        assert result.source_refs == again.source_refs
        assert len(memory.resources.list_all()) == 1
        assert memory.resources.context_version == 0
    else:
        assert result.source_refs == () and memory.resources.list_all() == []
    invalid = await tool.invoke({"query": " "})
    assert not invalid.success


@pytest.mark.asyncio
async def test_duplicate_registration_closes_all_opened_clients(monkeypatch):
    from app.tools import tool_loader
    opened, closed = [], []
    @asynccontextmanager
    async def connect(parameters):
        opened.append(parameters.command)
        client = AsyncMock()
        client.list_tools.return_value = (Tool(name="shared", inputSchema={"type": "object"}),)
        try:
            yield client
        finally:
            closed.append(parameters.command)
    monkeypatch.setattr(tool_loader.MCPClient, "connect", connect)
    memory = WorkingMemory(TaskState("user", "goal"))
    with pytest.raises(ValueError, match="already registered"):
        async with open_tool_registry(memory, mcp_servers=[
            StdioServerParameters(command="one"), StdioServerParameters(command="two"),
        ]):
            pytest.fail("A duplicate tool must reject initialization")
    assert opened == ["one", "two"] and closed == ["two", "one"]


def test_configured_mcp_servers_do_not_expose_unrequested_write_access(tmp_path):
    servers = local_mcp_servers(filesystem_root=tmp_path, tavily_api_key="test-key")
    assert len(servers) == 2
    assert "--write-root" not in servers[0].args
    assert servers[1].env == {"TAVILY_API_KEY": "test-key"}
    with pytest.raises(ValueError, match="filesystem_root"):
        local_mcp_servers(write_root=tmp_path)


@pytest.mark.asyncio
async def test_rag_uses_existing_dataclass_citations_and_preserves_provenance():
    memory = WorkingMemory(TaskState("user", "goal"))
    citation = Citation(1, "docs/report.md", "report.md", "Findings", 3, 8, 0.1)
    search = AsyncMock(return_value=("[ref:1] Finding", {"status": "ok", "citations": [citation]}))
    tool = KnowledgeRegistryTool(memory, search=search)
    result = await tool.invoke({"query": "finding"})
    payload = json.loads(result.content)
    assert payload["citations"][0]["source"] == "docs/report.md"
    assert payload["citations"][0]["ref_id"] == 1
    resource = memory.resources.get(result.source_refs[0])
    assert "docs/report.md" in resource.summary
    assert memory.resources.context_version == 0
