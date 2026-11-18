"""D-087: edit_file replaces exact text in one existing UTF-8 file under the write root.

Only temporary directories; no model, network or Docker.
"""

import codecs

import pytest

from app.tools.filesystem.file_access import DENIED_MESSAGE, PROTECTED_MESSAGE, FileAccess
from app.tools.filesystem.server import create_server


def access(tmp_path, **kwargs):
    root = tmp_path / 'root'
    root.mkdir(exist_ok=True)
    return root, FileAccess(root, write_root=root, **kwargs)


def test_one_occurrence_is_replaced_and_the_line_is_reported(tmp_path):
    root, files = access(tmp_path)
    (root / 'a.py').write_bytes(b'one\ntwo\nthree\n')
    assert files.edit_file('a.py', 'two\n', 'TWO\n') == {'replacements': 1, 'line': 2}
    assert (root / 'a.py').read_bytes() == b'one\nTWO\nthree\n'


def test_new_may_be_empty_to_delete(tmp_path):
    root, files = access(tmp_path)
    (root / 'a.py').write_bytes(b'keep\ndrop\n')
    files.edit_file('a.py', 'drop\n', '')
    assert (root / 'a.py').read_bytes() == b'keep\n'


def test_several_occurrences_need_more_context_or_replace_all(tmp_path):
    root, files = access(tmp_path)
    (root / 'a.py').write_bytes(b'x = 1\ny = 1\n')
    with pytest.raises(ValueError, match='appears 2 times; include more surrounding lines'):
        files.edit_file('a.py', '= 1', '= 2')
    assert (root / 'a.py').read_bytes() == b'x = 1\ny = 1\n'
    assert files.edit_file('a.py', '= 1', '= 2', replace_all=True) == {'replacements': 2, 'line': 1}
    assert (root / 'a.py').read_bytes() == b'x = 2\ny = 2\n'


@pytest.mark.parametrize('old, new, message', [
    ('missing', 'x', 'old text not found'),
    ('', 'x', 'old must not be empty'),
    ('same', 'same', 'nothing to change'),
])
def test_bad_old_text_is_refused_and_the_file_is_unchanged(tmp_path, old, new, message):
    root, files = access(tmp_path)
    (root / 'a.py').write_bytes(b'same\n')
    with pytest.raises(ValueError, match=message):
        files.edit_file('a.py', old, new)
    assert (root / 'a.py').read_bytes() == b'same\n'


@pytest.mark.parametrize('arguments', [{'old': 1, 'new': 'x'}, {'old': 'a', 'new': None},
                                       {'old': 'a', 'new': 'b', 'replace_all': 'yes'}])
def test_argument_types_are_checked(tmp_path, arguments):
    root, files = access(tmp_path)
    (root / 'a.py').write_bytes(b'a\n')
    with pytest.raises(TypeError):
        files.edit_file('a.py', **arguments)


def test_a_crlf_file_matches_text_written_with_lf(tmp_path):
    root, files = access(tmp_path)
    (root / 'a.py').write_bytes(b'def f():\r\n    return 1\r\n')
    files.edit_file('a.py', 'def f():\n    return 1\n', 'def f():\n    return 2\n')
    assert (root / 'a.py').read_bytes() == b'def f():\r\n    return 2\r\n'


def test_a_bom_is_kept(tmp_path):
    root, files = access(tmp_path)
    (root / 'a.txt').write_bytes(codecs.BOM_UTF8 + '中文\n'.encode('utf-8'))
    files.edit_file('a.txt', '中文', '汉字')
    assert (root / 'a.txt').read_bytes() == codecs.BOM_UTF8 + '汉字\n'.encode('utf-8')


def test_a_file_in_another_encoding_is_refused_not_rewritten(tmp_path):
    root, files = access(tmp_path)
    data = '中文说明\n'.encode('gb18030')
    (root / 'a.txt').write_bytes(data)
    with pytest.raises(ValueError, match='only edits UTF-8 files'):
        files.edit_file('a.txt', '说明', '注释')
    assert (root / 'a.txt').read_bytes() == data


def test_the_write_limit_bounds_the_file_and_the_result(tmp_path):
    root, files = access(tmp_path, max_write_bytes=16)
    (root / 'big.txt').write_bytes(b'x' * 17)
    with pytest.raises(ValueError, match='exceeds the write limit'):
        files.edit_file('big.txt', 'x', 'y', replace_all=True)
    (root / 'small.txt').write_bytes(b'abc')
    with pytest.raises(ValueError, match='would exceed the write limit'):
        files.edit_file('small.txt', 'b', 'b' * 20)
    assert (root / 'small.txt').read_bytes() == b'abc'


def test_only_existing_files_are_edited(tmp_path):
    root, files = access(tmp_path)
    with pytest.raises(FileNotFoundError, match='create new files with write_file'):
        files.edit_file('new.py', 'a', 'b')
    (root / 'pkg').mkdir()
    with pytest.raises(ValueError):
        files.edit_file('pkg', 'a', 'b')


def test_it_edits_the_file_under_the_write_root(tmp_path):
    read_root, write_root = tmp_path / 'read', tmp_path / 'write'
    read_root.mkdir()
    write_root.mkdir()
    (read_root / 'a.txt').write_bytes(b'read copy\n')
    (write_root / 'a.txt').write_bytes(b'write copy\n')
    FileAccess(read_root, write_root=write_root).edit_file('a.txt', 'copy', 'edited')
    assert (read_root / 'a.txt').read_bytes() == b'read copy\n'
    assert (write_root / 'a.txt').read_bytes() == b'write edited\n'


def test_without_a_write_root_editing_is_off(tmp_path):
    (tmp_path / 'a.txt').write_bytes(b'a\n')
    with pytest.raises(PermissionError, match='writing is disabled'):
        FileAccess(tmp_path).edit_file('a.txt', 'a', 'b')


@pytest.mark.parametrize('path, error, message', [
    ('../outside.txt', ValueError, 'parent traversal'),
    ('.env', PermissionError, PROTECTED_MESSAGE),
    ('closed/a.txt', PermissionError, DENIED_MESSAGE),
])
def test_the_same_paths_are_refused_as_for_write_file(tmp_path, path, error, message):
    root = tmp_path / 'root'
    (root / 'closed').mkdir(parents=True)
    (root / 'closed' / 'a.txt').write_bytes(b'a\n')
    (root / '.env').write_bytes(b'a\n')
    files = FileAccess(root, write_root=root, denied=(root / 'closed',))
    with pytest.raises(error, match=message.replace('(', r'\(').replace(')', r'\)')):
        files.edit_file(path, 'a', 'b')


def test_a_link_is_refused(tmp_path):
    root, files = access(tmp_path)
    (tmp_path / 'target.txt').write_bytes(b'a\n')
    try:
        (root / 'link.txt').symlink_to(tmp_path / 'target.txt')
    except OSError:
        pytest.skip('symbolic links are not available here')
    with pytest.raises(PermissionError, match='links'):
        files.edit_file('link.txt', 'a', 'b')
    assert (tmp_path / 'target.txt').read_bytes() == b'a\n'


# ---- the MCP tool ----

async def test_the_server_offers_edit_file_only_with_a_write_root(tmp_path):
    names = {t.name for t in await create_server(tmp_path).list_tools()}
    assert 'edit_file' not in names and 'write_file' not in names
    names = {t.name for t in await create_server(tmp_path, write_root=tmp_path).list_tools()}
    assert {'edit_file', 'write_file'} <= names


async def test_the_tool_reports_where_to_read_the_change(tmp_path):
    (tmp_path / 'a.py').write_bytes(b'\n' * 10 + b'old\n')
    server = create_server(tmp_path, write_root=tmp_path)
    result = await server.call_tool('edit_file', {'path': 'a.py', 'old': 'old', 'new': 'new'})
    text = ' '.join(block.text for block in (result[0] if isinstance(result, tuple) else result))
    assert text.startswith('Edited a.py: replaced 1 occurrence(s) starting at line 11.')
    assert "read_file(path='a.py', start_line=6, max_lines=40)" in text
    assert (tmp_path / 'a.py').read_bytes() == b'\n' * 10 + b'new\n'
