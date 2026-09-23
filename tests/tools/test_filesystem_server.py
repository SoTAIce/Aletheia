from pathlib import Path
import sys

from mcp import StdioServerParameters
import pytest

from app.agent.step_models import ToolCall
from app.tools.mcp_client import MCPClient
from app.tools.mcp_tool_adapter import MCPToolAdapter
from app.tools.tool_registry import ToolRegistry
from app.tools.filesystem.server import create_server


@pytest.mark.asyncio
@pytest.mark.parametrize("writable", [False, True])
async def test_filesystem_stdio_registry(tmp_path, writable):
    output = tmp_path / "outputs"
    output.mkdir()
    (tmp_path / "input.txt").write_text("hello", encoding="utf-8")
    args = ["-m", "app.tools.filesystem.server", "--root", str(tmp_path)]
    if writable:
        args += ["--write-root", str(output), "--max-write-bytes", "32"]
    parameters = StdioServerParameters(
        command=sys.executable, args=args,
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    registry = ToolRegistry()
    async with MCPClient.connect(parameters) as client:
        tools = await client.list_tools()
        assert {tool.name for tool in tools} == ({"read_file", "write_file"} if writable else {"read_file"})
        assert all(tool.description for tool in tools)
        for tool in tools:
            registry.register(MCPToolAdapter(client, tool))
        async def invoke(name, arguments):
            return await registry.invoke(ToolCall("test", name, arguments))
        read = await invoke("read_file", {"path": "input.txt"})
        assert read.success and "hello" in read.content
        for path in ("../outside.txt", "missing.txt"):
            assert not (await invoke("read_file", {"path": path})).success
        if writable:
            text = "Hello \u4e2d\u6587"
            result = await invoke("write_file", {"path": "new.txt", "content": text})
            assert result.success and "outputs/new.txt" in result.content
            assert (output / "new.txt").read_text(encoding="utf-8") == text
            assert text in (await invoke("read_file", {"path": "outputs/new.txt"})).content
            assert not (await invoke("write_file", {"path": "new.txt", "content": "replace"})).success
            assert (output / "new.txt").read_text(encoding="utf-8") == text
            assert (await invoke("write_file", {"path": "new.txt", "content": "", "overwrite": True})).success
            assert (output / "new.txt").read_bytes() == b""
            for path, content in [("../escape.txt", "x"), ("missing/file.txt", "x"), ("large.txt", "x" * 33)]:
                assert not (await invoke("write_file", {"path": path, "content": content})).success
            assert sorted(p.name for p in output.iterdir()) == ["new.txt"]


def test_invalid_configuration_fails_before_serving(tmp_path):
    with pytest.raises(FileNotFoundError):
        create_server(tmp_path / "missing")
    with pytest.raises(ValueError):
        create_server(tmp_path, max_read_bytes=0)
