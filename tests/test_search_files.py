"""search_files: find lines, then read around them (D-055)."""

import json

import pytest

from app.tools.filesystem.file_access import FileAccess


def tree(root):
    files = {
        'app/agent/runtime.py': 'import asyncio\n\nclass AgentRuntime:\n    async def run(self):\n        return await self._run_locked()\n',
        'app/agent/planner.py': 'class Planner:\n    def plan(self):\n        pass\n',
        'docs/notes.md': 'AgentRuntime is described here.\n',
        '.env': 'SECRET_KEY=AgentRuntime\n',
        'keys/server.pem': 'AgentRuntime\n',
        '.git/config': 'AgentRuntime\n',
        '.hidden/x.py': 'AgentRuntime\n',
        'app/__pycache__/runtime.cpython.pyc': 'AgentRuntime\n',
        'node_modules/pkg/index.js': 'AgentRuntime\n',
    }
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode('utf-8'))
    (root / 'app' / 'blob.bin').write_bytes(b'AgentRuntime\x00\x01')
    (root / 'app' / 'big.py').write_bytes(b'AgentRuntime\n' * 10000)   # over the read limit below
    return FileAccess(root, max_read_bytes=64 * 1024)


def test_matches_give_path_line_and_text_and_skip_what_must_not_be_read(tmp_path):
    result = tree(tmp_path).search_files('AgentRuntime')
    assert [(m['path'], m['line']) for m in result['matches']] == [
        ('app/agent/runtime.py', 3), ('docs/notes.md', 1)]
    assert result['matches'][0]['text'] == 'class AgentRuntime:'
    assert result['files_skipped'] == 2                 # the binary and the oversized file
    assert result['truncated'] is False


def test_glob_path_case_and_regex(tmp_path):
    files = tree(tmp_path)
    assert [m['path'] for m in files.search_files('AgentRuntime', glob='*.py')['matches']] == ['app/agent/runtime.py']
    assert [m['path'] for m in files.search_files('agentruntime', path='docs', ignore_case=True)['matches']] == ['docs/notes.md']
    assert files.search_files('agentruntime')['matches'] == []
    found = files.search_files(r'def (plan|run)\b', regex=True)['matches']
    assert sorted((m['path'], m['line']) for m in found) == [('app/agent/planner.py', 2), ('app/agent/runtime.py', 4)]
    assert files.search_files('class', path='app/agent/planner.py')['matches'][0]['line'] == 1


def test_results_are_bounded(tmp_path):
    for n in range(5):
        (tmp_path / f'f{n}.txt').write_bytes(b'hit\n' * 10)
    result = FileAccess(tmp_path).search_files('hit', max_results=7)
    assert len(result['matches']) == 7 and result['truncated'] is True


@pytest.mark.parametrize('kwargs', [
    {'pattern': ''}, {'pattern': 'x' * 201}, {'pattern': '(', 'regex': True},
    {'pattern': 'x', 'max_results': 0}, {'pattern': 'x', 'max_results': 201},
    {'pattern': 'x', 'regex': 'yes'}, {'pattern': 'x', 'glob': ''},
])
def test_bad_arguments_are_refused(tmp_path, kwargs):
    with pytest.raises(ValueError):
        tree(tmp_path).search_files(**kwargs)


@pytest.mark.parametrize('path', ['..', '../x', '.env', '.git'])
def test_paths_outside_the_root_or_protected_are_refused(tmp_path, path):
    (tmp_path / 'inner').mkdir()
    tree(tmp_path / 'inner')
    with pytest.raises((ValueError, PermissionError, FileNotFoundError)):
        FileAccess(tmp_path / 'inner').search_files('AgentRuntime', path=path)


async def test_the_mcp_server_offers_it_and_the_result_reaches_the_model_once(tmp_path):
    from app.tools.filesystem.server import create_server
    from app.tools.mcp_tool_adapter import _same_as_text
    tree(tmp_path)
    server = create_server(tmp_path)
    assert 'search_files' in {tool.name for tool in await server.list_tools()}
    result = await server.call_tool('search_files', {'pattern': 'class AgentRuntime'})
    blocks, structured = result if isinstance(result, tuple) else (result, None)
    text = '\n'.join(block.text for block in blocks)
    assert json.loads(text)['matches'][0]['line'] == 3
    # Over a transport FastMCP may add the same dict as structured content; the adapter drops it.
    assert _same_as_text(json.loads(text), text)
