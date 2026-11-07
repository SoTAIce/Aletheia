"""Reading long files: window-scaled tool budgets, raw archives, line ranges (D-052)."""

import inspect

import pytest

from app.agent.planner import Planner
from app.agent.step_models import ToolResult
from app.config import Settings
from app.services.context_manager import ContextManager
from app.services.conversation_archive import ConversationArchive
from app.tools.context_archive_tool import ContextArchiveTool
from app.tools.filesystem.file_access import FileAccess

SOURCE = ''.join(f'line {n}: x = "{n}"\n' for n in range(1, 2001))     # ~40K characters of code


@pytest.mark.parametrize('window, result, preview', [
    (32768, 8000, 1200),            # the old fixed values stay for the default window
    (128000, 39594, 5656),
    (400000, 120000, 8000),         # capped
])
def test_tool_budgets_follow_the_model_window(window, result, preview):
    manager = ContextManager(window_tokens=window, output_tokens=8192, safety_tokens=1024)
    assert (manager.tool_result_chars, manager.tool_preview_chars) == (result, preview)
    explicit = ContextManager(window_tokens=window, output_tokens=8192, safety_tokens=1024,
                              tool_result_chars=300, tool_preview_chars=80)
    assert (explicit.tool_result_chars, explicit.tool_preview_chars) == (300, 80)


def test_the_agent_model_can_declare_its_own_window():
    base = dict(_env_file=None, context_window_tokens=32768, agent_model_base_url='', agent_model_api_key='',
                agent_model='')
    assert Settings(**base, agent_model_context_tokens=None).agent_model_window_tokens == 32768
    assert Settings(**base, agent_model_context_tokens=400000).agent_model_window_tokens == 400000


async def test_a_large_window_keeps_a_source_file_inline(tmp_path):
    archive = ConversationArchive(tmp_path, 's')
    small = ContextManager(archive=archive, window_tokens=32768, output_tokens=8192)
    large = ContextManager(archive=archive, window_tokens=400000, output_tokens=8192)
    result = ToolResult('c', 'read_file', True, SOURCE)
    assert (await small.project_tool_result(result))['truncated'] is True
    view = await large.project_tool_result(result)
    assert 'storage_ref' not in view and view['content'] == SOURCE


async def test_archived_text_is_the_file_and_reads_continue_without_rewrapping(tmp_path):
    archive = ConversationArchive(tmp_path, 's')
    manager = ContextManager(archive=archive, window_tokens=32768, output_tokens=8192)
    view = await manager.project_tool_result(ToolResult('c', 'read_file', True, SOURCE))
    assert view['total_chars'] == len(SOURCE)
    tool = ContextArchiveTool(archive, manager.tool_result_chars)
    assert tool.definition.input_schema['properties']['limit']['maximum'] == 8000
    pieces, offset = [], 0
    while True:
        out = await tool.invoke({'ref': view['storage_ref'], 'offset': offset})
        projected = await manager.project_tool_result(ToolResult('r', 'read_context_artifact', True, out.content))
        assert 'storage_ref' not in projected                  # never archived a second time
        text, sep, note = out.content.partition('\n[more text follows; continue with offset=')
        pieces.append(text)
        if not sep:
            break
        offset = int(note.rstrip(']'))
    assert ''.join(pieces) == SOURCE and len(pieces) == -(-len(SOURCE) // 8000)   # 8000-character reads


async def test_a_failed_large_result_keeps_the_whole_record(tmp_path):
    archive = ConversationArchive(tmp_path, 's')
    manager = ContextManager(archive=archive, tool_result_chars=100, tool_preview_chars=50)
    view = await manager.project_tool_result(ToolResult('c', 'read_file', False, 'x' * 500, error='boom'))
    assert '"error": "boom"' in archive.read_text(view['storage_ref'], limit=2000)


def test_read_file_reads_line_ranges_with_a_continuation_note(tmp_path):
    (tmp_path / 'big.py').write_bytes(SOURCE.encode('utf-8'))
    files = FileAccess(tmp_path)
    assert files.read_file('big.py') == SOURCE
    head = files.read_file('big.py', max_lines=2)
    assert head == 'line 1: x = "1"\nline 2: x = "2"\n[lines 1-2 of 2000; continue with start_line=3]'
    tail = files.read_file('big.py', start_line=1999)
    assert tail.endswith('line 2000: x = "2000"\n[lines 1999-2000 of 2000; end of file]')
    with pytest.raises(ValueError, match='past the end'):
        files.read_file('big.py', start_line=2001)
    for bad in ({'start_line': 0}, {'max_lines': True}, {'max_lines': -1}):
        with pytest.raises(ValueError):
            files.read_file('big.py', **bad)


@pytest.mark.parametrize('text, structured, expected', [
    ('print(1)\n', {'result': 'print(1)\n'}, 'print(1)\n'),                      # FastMCP str result
    ('{\n  "total": 2\n}', {'total': 2}, '{\n  "total": 2\n}'),                   # FastMCP dict result
    ('hello', {'count': 1}, 'hello\n{"count": 1}'),                               # extra data is kept
    ('', {'count': 1}, '{"count": 1}'),
])
async def test_mcp_results_are_not_doubled_by_their_structured_copy(text, structured, expected):
    from unittest.mock import AsyncMock
    from mcp.types import CallToolResult, TextContent, Tool
    from app.tools.mcp_tool_adapter import MCPToolAdapter
    client = AsyncMock()
    client.call_tool.return_value = CallToolResult(
        content=[TextContent(type='text', text=text)] if text else [], structuredContent=structured)
    out = await MCPToolAdapter(client, Tool(name='t', inputSchema={'type': 'object'})).invoke({})
    assert out.content == expected


async def test_the_real_filesystem_server_returns_a_file_once(tmp_path):
    from app.tools.filesystem.server import create_server
    (tmp_path / 'a.py').write_bytes(SOURCE.encode('utf-8'))
    server = create_server(tmp_path)
    blocks, structured = await server.call_tool('read_file', {'path': 'a.py'})
    from app.tools.mcp_tool_adapter import _same_as_text
    assert _same_as_text(structured, '\n'.join(b.text for b in blocks))


def test_the_planner_keeps_reading_and_analysis_in_one_step():
    source = inspect.getsource(Planner._build_prompt)
    assert '读取文件或资料与基于它的分析放在同一步' in source
