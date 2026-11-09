"""Knowledge base build command (D-068): dry run by default, --confirm writes, safe failures.

Real splitter, fake vector store: no Milvus connection and no embedding call.
"""

from pathlib import Path

import pytest

from app.knowledge.__main__ import MAX_FAILURES_BEFORE_ANY_SUCCESS, build_parser, main, run
from app.services.document_splitter_service import DocumentSplitterService
from app.services.vector_index_service import EXCLUDED_DIRS, VectorIndexService


class FakeStore:
    def __init__(self, fail_with=None):
        self.deleted, self.added, self.closed, self.fail_with = [], [], 0, fail_with

    def delete_by_source(self, source):
        if self.fail_with is not None:
            raise self.fail_with
        self.deleted.append(source)
        return 0

    def add_document(self, documents):
        self.added.append(list(documents))
        return [str(n) for n in range(len(documents))]

    def close(self):
        self.closed += 1


def workspace(tmp_path: Path) -> Path:
    root = tmp_path / 'docs'
    (root / 'guide').mkdir(parents=True)
    (root / 'guide' / 'intro.md').write_text('# 介绍\n\n' + '知识库说明。' * 40, encoding='utf-8')
    (root / 'notes.txt').write_text('第一段。\n\n第二段。', encoding='utf-8')
    (root / 'image.png').write_bytes(b'\x89PNG')                      # unsupported extension
    for skipped in ('.git', '.venv', 'node_modules', '__pycache__'):
        (root / skipped).mkdir()
        (root / skipped / 'hidden.py').write_text('x = 1\n', encoding='utf-8')
    return root


def service(store):
    return VectorIndexService(splitter=DocumentSplitterService(), store_manager=store)


def args(*argv):
    return build_parser().parse_args(list(argv))


def test_supported_files_skip_unsupported_files_and_excluded_directories(tmp_path):
    root = workspace(tmp_path)
    names = [p.relative_to(root.resolve()).as_posix() for p in VectorIndexService.supported_files(root)]
    assert names == ['guide/intro.md', 'notes.txt']
    assert {'.git', '.venv', 'node_modules', '__pycache__'} <= EXCLUDED_DIRS


def test_index_directory_uses_the_same_file_list(tmp_path):
    store = FakeStore()
    result = service(store).index_directory(str(workspace(tmp_path)))
    assert result.success and result.total_files == 2 and result.success_count == 2
    assert all('/.venv/' not in s and '/.git/' not in s for s in store.deleted)


def test_the_default_is_a_dry_run_that_touches_no_store(tmp_path):
    store, lines = FakeStore(), []
    code = run(args(str(workspace(tmp_path))), service(store), lines.append)
    assert code == 0 and store.deleted == [] and store.added == [] and store.closed == 0
    assert lines[0].startswith('  guide/intro.md：') and lines[1].startswith('  notes.txt：')
    assert '预演：2 个文件' in lines[-1] and '--confirm' in lines[-1]


def test_confirm_replaces_each_files_chunks_and_closes_the_store(tmp_path):
    root = workspace(tmp_path)
    store, lines = FakeStore(), []
    code = run(args(str(root), '--confirm'), service(store), lines.append)
    assert code == 0 and store.closed == 1
    assert store.deleted == [p.as_posix() for p in VectorIndexService.supported_files(root)]
    assert len(store.added) == 2 and all(store.added)
    assert all(d.metadata['_source'] in store.deleted for batch in store.added for d in batch)
    assert lines[-1].startswith('建库：2 个文件，成功 2 个') and '失败 0 个' in lines[-1]


def test_failures_name_the_file_and_exception_type_only(tmp_path):
    store, lines = FakeStore(fail_with=ConnectionError('secret provider text at C:/private')), []
    code = run(args(str(workspace(tmp_path)), '--confirm'), service(store), lines.append)
    text = '\n'.join(lines)
    assert code == 1 and store.closed == 1
    assert '失败：guide/intro.md（ConnectionError）' in text
    assert 'secret' not in text and 'private' not in text


def test_the_run_stops_when_the_first_files_all_fail(tmp_path):
    root = tmp_path / 'many'
    root.mkdir()
    for n in range(MAX_FAILURES_BEFORE_ANY_SUCCESS + 2):
        (root / f'f{n}.txt').write_text(f'内容 {n}', encoding='utf-8')
    store, lines = FakeStore(fail_with=RuntimeError('down')), []
    code = run(args(str(root), '--confirm'), service(store), lines.append)
    assert code == 1 and store.closed == 1
    assert sum(line.startswith('  失败：') for line in lines) == MAX_FAILURES_BEFORE_ANY_SUCCESS
    assert '已停止，剩下 2 个没有处理' in lines[-1]


def test_a_dry_run_reports_files_that_cannot_be_split(tmp_path):
    root = tmp_path / 'bad'
    root.mkdir()
    (root / 'broken.json').write_text('{not json', encoding='utf-8')
    (root / 'ok.txt').write_text('可以切块。', encoding='utf-8')
    store, lines = FakeStore(), []
    code = run(args(str(root)), service(store), lines.append)
    assert code == 1 and '无法切块：broken.json（JSONDecodeError）' in lines[0]
    assert '1 个无法切块' in lines[-1] and store.added == []


def test_missing_directory_and_empty_directory(tmp_path):
    lines = []
    assert run(args(str(tmp_path / 'nope')), service(FakeStore()), lines.append) == 2
    assert lines == [f'目录不存在：{tmp_path / "nope"}']
    lines.clear()
    assert run(args(str(tmp_path)), service(FakeStore()), lines.append) == 0
    assert lines[0].startswith('没有找到可以建库的文件')


def test_the_parser_requires_a_directory_and_defaults_to_a_dry_run():
    assert build_parser().parse_args(['docs']).confirm is False
    assert build_parser().parse_args(['docs', '--confirm']).confirm is True
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_main_runs_a_dry_run_through_the_module_service(tmp_path, monkeypatch, capsys):
    from app.services import vector_index_service as module
    store = FakeStore()
    monkeypatch.setattr(module, 'vector_index_service', service(store))
    assert main([str(workspace(tmp_path))]) == 0
    assert '预演：2 个文件' in capsys.readouterr().out and store.closed == 0
