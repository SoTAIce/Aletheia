"""Knowledge base build command (D-068): dry run by default, --confirm writes, safe failures.

Real splitter, fake vector store: no Milvus connection and no embedding call.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.knowledge.__main__ import MAX_FAILURES_BEFORE_ANY_SUCCESS, build_parser, main, run
from app.services.document_splitter_service import DocumentSplitterService
from app.services.vector_index_service import EXCLUDED_DIRS, VectorIndexService


class FakeStore:
    def __init__(self, fail_with=None, sources=()):
        self.deleted, self.added, self.closed, self.fail_with = [], [], 0, fail_with
        self.replaced, self.events, self.listed = [], [], 0
        self.sources = set(sources)

    def replace_source(self, source, documents):
        self.events.append(("替换", source))
        if self.fail_with is not None:
            raise self.fail_with
        self.replaced.append(source)
        if documents:
            self.add_document(documents)
        return len(documents)

    def list_sources(self):
        self.events.append(("列来源",))
        self.listed += 1
        if self.fail_with is not None:
            raise self.fail_with
        return self.sources

    def delete_by_source(self, source):
        if self.fail_with is not None:
            raise self.fail_with
        self.deleted.append(source)
        self.events.append(("删除", source))
        return 7

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
    assert all('/.venv/' not in s and '/.git/' not in s for s in store.replaced)


def test_the_default_is_a_dry_run_that_touches_no_store(tmp_path):
    store, lines = FakeStore(), []
    code = run(args(str(workspace(tmp_path))), service(store), lines.append)
    assert code == 0 and store.deleted == [] and store.added == [] and store.closed == 0 and store.listed == 0
    assert lines[0].startswith('  guide/intro.md：') and lines[1].startswith('  notes.txt：')
    assert '预演：2 个文件' in lines[-1] and '--confirm' in lines[-1]


def test_confirm_replaces_each_files_chunks_and_closes_the_store(tmp_path):
    root = workspace(tmp_path)
    store, lines = FakeStore(), []
    code = run(args(str(root), '--confirm'), service(store), lines.append)
    assert code == 0 and store.closed == 1
    assert store.replaced == [p.as_posix() for p in VectorIndexService.supported_files(root)]
    assert store.deleted == [] and store.listed == 0
    assert len(store.added) == 2 and all(store.added)
    assert all(d.metadata['_source'] in store.replaced for batch in store.added for d in batch)
    assert lines[-1].startswith('建库：2 个文件，成功 2 个') and '失败 0 个' in lines[-1]


def test_failures_name_the_file_and_exception_type_only(tmp_path):
    store, lines = FakeStore(fail_with=ConnectionError('secret provider text at C:/private')), []
    code = run(args(str(workspace(tmp_path)), '--confirm'), service(store), lines.append)
    text = '\n'.join(lines)
    assert code == 1 and store.closed == 1
    assert '失败：guide/intro.md（写入 Milvus 失败，ConnectionError）' in text
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


# 新测试使用真实服务编排，只有外部向量库、连接和嵌入客户端是替身。
def store_manager(*, write_error=None, delete_error=None, rows=None):
    from app.services.vector_store_manager import VectorStoreManager
    events = []
    class Collection:
        num_entities = len(rows or [])
        def delete(self, expr):
            events.append(('删除', expr))
            if delete_error:
                raise delete_error
            return SimpleNamespace(delete_count=2)
        def load(self):
            events.append(('加载',))
        def query(self, **kwargs):
            events.append(('查询', kwargs))
            offset, limit = kwargs['offset'], kwargs['limit']
            return (rows or [])[offset:offset + limit]
        def query_iterator(self, *, expr, output_fields, batch_size):
            collection = self
            class Iterator:
                offset = 0
                def next(self):
                    page = collection.query(expr=expr, output_fields=output_fields,
                                            limit=batch_size, offset=self.offset)
                    self.offset += len(page)
                    return page
                def close(self):
                    events.append(('关闭迭代器',))
            return Iterator()
    class VectorStore:
        def add_documents(self, documents, ids):
            events.append(('写入', documents, ids))
            if write_error:
                raise write_error
            return ids
    class Client:
        COLLECTION_NAME = '测试'
        def connect(self, **kwargs):
            events.append(('连接',))
        def get_collection(self):
            return Collection()
    manager = VectorStoreManager(Client(), vector_store_factory=lambda **kw: VectorStore())
    return manager, events


def test_replace_source_writes_before_deleting_only_old_ids():
    from langchain_core.documents import Document
    manager, events = store_manager()
    assert manager.replace_source('/docs/a.md', [Document(page_content='新块')]) == 1
    write = next(e for e in events if e[0] == '写入')
    delete = next(e for e in events if e[0] == '删除')
    assert events.index(write) < events.index(delete)
    assert delete[1] == 'metadata["_source"] == "/docs/a.md" and id not in ["' + write[2][0] + '"]'


def test_replace_source_write_failure_never_deletes_old_chunks():
    from langchain_core.documents import Document
    manager, events = store_manager(write_error=RuntimeError('写入失败'))
    with pytest.raises(RuntimeError):
        manager.replace_source('/docs/a.md', [Document(page_content='新块')])
    assert not any(e[0] == '删除' for e in events)


def test_replace_source_escapes_source_and_new_ids(monkeypatch):
    import app.services.vector_store_manager as module
    from langchain_core.documents import Document
    monkeypatch.setattr(module, 'uuid4', lambda: 'id"\\new')
    manager, events = store_manager()
    manager.replace_source('C:/docs/a"\\b.md', [Document(page_content='新块')])
    assert events[-1] == ('删除', r'metadata["_source"] == "C:/docs/a\"\\b.md" and id not in ["id\"\\new"]')


def test_replace_source_empty_document_only_deletes():
    manager, events = store_manager()
    assert manager.replace_source('/docs/a.md', []) == 0
    assert events == [('连接',), ('删除', 'metadata["_source"] == "/docs/a.md"')]


def test_replace_source_delete_failure_leaves_new_chunks_written():
    from langchain_core.documents import Document
    manager, events = store_manager(delete_error=RuntimeError('删除失败'))
    with pytest.raises(RuntimeError):
        manager.replace_source('/docs/a.md', [Document(page_content='新块')])
    assert [e[0] for e in events if e[0] in {'写入', '删除'}] == ['写入', '删除']


def test_list_sources_pages_metadata_and_deduplicates():
    rows = [{'metadata': {'_source': '/docs/a.md'}}, {'metadata': {'_source': '/docs/a.md'}},
            {'metadata': {'_source': '/docs/b.md'}}, {'metadata': {}}, {'metadata': None}]
    manager, events = store_manager(rows=rows)
    assert manager.list_sources(batch_size=2) == {'/docs/a.md', '/docs/b.md'}
    queries = [e[1] for e in events if e[0] == '查询']
    assert [q['offset'] for q in queries] == [0, 2, 4, 5]
    assert events[-1] == ('关闭迭代器',)
    assert all(q['output_fields'] == ['metadata'] for q in queries)


@pytest.mark.parametrize('reported_total', [0, 2])
def test_list_sources_uses_actual_rows_instead_of_entity_statistics(monkeypatch, reported_total):
    manager, events = store_manager(rows=[{'metadata': {'_source': '/docs/a.md'}}])
    collection = manager._client_manager.get_collection()
    collection.num_entities = reported_total
    monkeypatch.setattr(manager._client_manager, 'get_collection', lambda: collection)
    assert manager.list_sources() == {'/docs/a.md'}


def test_list_sources_propagates_page_failure_instead_of_partial_snapshot(monkeypatch):
    manager, events = store_manager(rows=[{'metadata': {'_source': '/docs/a.md'}}])
    collection = manager._client_manager.get_collection()
    def query(**kwargs):
        if kwargs['offset']:
            raise RuntimeError('分页失败')
        return [{'metadata': {'_source': '/docs/a.md'}}]
    monkeypatch.setattr(collection, 'query', query)
    monkeypatch.setattr(manager._client_manager, 'get_collection', lambda: collection)
    with pytest.raises(RuntimeError, match='分页失败'):
        manager.list_sources(batch_size=1)
    assert events[-1] == ('关闭迭代器',)


@pytest.mark.parametrize('kind,label,original', [
    ('read', '读取或切块失败', 'JSONDecodeError'),
    ('connect', '连接 Milvus 失败', 'ConnectionError'),
    ('collection', 'Milvus 集合不可用', 'MilvusCollectionError'),
    ('embed_config', '没有配置嵌入密钥', 'EmbeddingConfigError'),
    ('embed', '调用嵌入接口失败', 'TimeoutError'),
    ('response', '调用嵌入接口失败', 'EmbeddingResponseError'),
    ('write', '写入 Milvus 失败', 'OSError'),
])
def test_stage_labels_and_original_type_without_private_text(tmp_path, kind, label, original):
    from app.core.milvus_client import MilvusCollectionError, MilvusConnectError
    from app.services.vector_embedding_service import EmbeddingConfigError, EmbeddingRequestError, EmbeddingResponseError
    root = tmp_path / 'docs'
    root.mkdir()
    path = root / ('bad.json' if kind == 'read' else 'a.txt')
    path.write_text('{secret private' if kind == 'read' else '文档', encoding='utf-8')
    error = {'connect': MilvusConnectError('secret private'),
             'collection': MilvusCollectionError('secret private'),
             'embed_config': EmbeddingConfigError('secret private'),
             'embed': EmbeddingRequestError('secret private'),
             'response': EmbeddingResponseError('secret private'),
             'write': OSError('secret private')}.get(kind)
    if kind == 'connect':
        error.__cause__ = ConnectionError('secret private')
    if kind == 'embed':
        error.__cause__ = TimeoutError('secret private')
    store, lines = FakeStore(fail_with=error), []
    assert run(args(str(root), '--confirm'), service(store), lines.append) == 1
    text = '\n'.join(lines)
    assert f'（{label}，{original}）' in text
    assert 'secret' not in text and 'private' not in text


@pytest.mark.parametrize('chain_attr', ['__cause__', '__context__'])
def test_stage_classification_traverses_wrapped_exception_chain(tmp_path, chain_attr):
    from app.services.vector_embedding_service import EmbeddingRequestError
    from app.services.vector_index_service import IndexStageError
    error = RuntimeError('外层')
    inner = RuntimeError('中层')
    setattr(error, chain_attr, inner)
    setattr(inner, chain_attr, EmbeddingRequestError('secret private'))
    # 异常图里有环时仍须终止。
    inner.__cause__ = error if chain_attr == '__context__' else inner.__cause__
    path = tmp_path / 'a.txt'
    path.write_text('文档', encoding='utf-8')
    with pytest.raises(IndexStageError) as info:
        service(FakeStore(fail_with=error)).index_single_file(path)
    assert info.value.stage == 'embed' and info.value.__cause__ is error
    assert str(info.value).startswith(f'failed to index {path.resolve()}: 外层')


@pytest.mark.parametrize('bad_file', ['missing.txt', 'unsupported.png'])
def test_missing_or_unsupported_file_is_a_read_stage(tmp_path, bad_file):
    from app.services.vector_index_service import IndexStageError
    path = tmp_path / bad_file
    if bad_file.endswith('.png'):
        path.write_bytes(b'image')
    with pytest.raises(IndexStageError) as info:
        service(FakeStore()).index_single_file(path)
    assert info.value.stage == 'read' and isinstance(info.value.__cause__, ValueError)


@pytest.mark.parametrize('stage,hint', [
    ('connect', 'docker compose -f deploy/milvus/compose.yaml start'),
    ('collection', 'EMBEDDING_DIMENSIONS'),
    ('embed_config', '请在 .env 里配置 DASHSCOPE_API_KEY'),
    ('embed', '嵌入接口调用失败，请检查密钥、网络和额度'),
    ('write', '请先检查 Milvus 是否在运行、DASHSCOPE_API_KEY 是否已配置'),
])
def test_early_stop_hint_follows_stage(tmp_path, stage, hint):
    from app.core.milvus_client import MilvusCollectionError, MilvusConnectError
    from app.services.vector_embedding_service import EmbeddingConfigError, EmbeddingRequestError
    for n in range(5):
        (tmp_path / f'{n}.txt').write_text('文档', encoding='utf-8')
    error = {'connect': MilvusConnectError, 'collection': MilvusCollectionError,
             'embed_config': EmbeddingConfigError,
             'embed': EmbeddingRequestError, 'write': RuntimeError}[stage]('secret')
    lines = []
    assert run(args(str(tmp_path), '--confirm'), service(FakeStore(fail_with=error)), lines.append) == 1
    assert hint in lines[-1] and '剩下 2 个没有处理' in lines[-1]


def test_prune_dry_run_only_lists_sources_with_directory_boundary(tmp_path):
    root = workspace(tmp_path)
    stale = root.as_posix() + '/gone.md'
    excluded = root.as_posix() + '/.venv/old.py'
    unsupported = root.as_posix() + '/image.png'
    store = FakeStore(sources=[stale, excluded, unsupported, root.as_posix() + '2/gone.md',
                              (root / 'notes.txt').as_posix(), '/other/gone.md'])
    lines = []
    assert run(args(str(root), '--prune'), service(store), lines.append) == 0
    text = '\n'.join(lines)
    assert store.listed == 1 and store.closed == 1 and not store.added and not store.deleted
    assert not store.replaced
    assert all(s in text for s in ['gone.md', '.venv/old.py', 'image.png'])
    assert 'docs2' not in text and '/other/' not in text
    assert '只读连接 Milvus' in text and '3 个' in text


def test_prune_dry_run_connection_failure_is_safe_and_nonzero(tmp_path):
    from app.core.milvus_client import MilvusConnectError
    store, lines = FakeStore(fail_with=MilvusConnectError('secret private')), []
    assert run(args(str(workspace(tmp_path)), '--prune'), service(store), lines.append) == 1
    text = '\n'.join(lines)
    assert store.closed == 1 and not store.added and not store.deleted
    assert '连接 Milvus 失败' in text and 'secret' not in text and 'private' not in text


def test_confirm_prune_deletes_only_after_all_files_processed(tmp_path):
    root = workspace(tmp_path)
    stale = (root / 'gone.md').as_posix()
    store, lines = FakeStore(sources=[stale, (root / 'notes.txt').as_posix()]), []
    assert run(args(str(root), '--confirm', '--prune'), service(store), lines.append) == 0
    assert [e[0] for e in store.events] == ['替换', '替换', '列来源', '删除']
    assert store.deleted == [stale] and store.closed == 1
    assert any('已清理：gone.md，7 块' in line for line in lines)
    assert '清理：1 个已删除文件，共 7 块' in '\n'.join(lines)


def test_prune_is_skipped_when_indexing_stops_early(tmp_path):
    for n in range(5):
        (tmp_path / f'{n}.txt').write_text('文档', encoding='utf-8')
    store, lines = FakeStore(fail_with=RuntimeError('secret')), []
    assert run(args(str(tmp_path), '--confirm', '--prune'), service(store), lines.append) == 1
    assert not store.deleted and store.listed == 0 and store.closed == 1
    assert '提前停止，已跳过清理' in '\n'.join(lines)


@pytest.mark.parametrize('confirm', [False, True])
def test_prune_can_process_an_empty_directory(tmp_path, confirm):
    stale = (tmp_path / 'gone.md').as_posix()
    store, lines = FakeStore(sources=[stale]), []
    argv = [str(tmp_path), '--prune'] + (['--confirm'] if confirm else [])
    assert run(args(*argv), service(store), lines.append) == 0
    assert store.listed == 1 and store.closed == 1
    assert store.deleted == ([stale] if confirm else [])


@pytest.mark.parametrize('method', ['embed_documents', 'embed_query'])
def test_embedding_request_subclass_preserves_message_and_cause(method):
    from app.services.vector_embedding_service import DashScopeEmbeddings, EmbeddingRequestError
    error = OSError('secret private')
    def create(**kw):
        raise error
    embedding = DashScopeEmbeddings('测试', client=SimpleNamespace(embeddings=SimpleNamespace(create=create)))
    with pytest.raises(EmbeddingRequestError) as info:
        getattr(embedding, method)(['文本'] if method == 'embed_documents' else '文本')
    prefix = 'batch' if method == 'embed_documents' else 'query'
    assert isinstance(info.value, RuntimeError) and info.value.__cause__ is error
    assert str(info.value) == f'{prefix} embedding request failed: secret private'


def test_embedding_config_subclass_keeps_value_error_and_message():
    from app.services.vector_embedding_service import DashScopeEmbeddings, EmbeddingConfigError
    with pytest.raises(EmbeddingConfigError) as info:
        DashScopeEmbeddings('').embed_query('文本')
    assert isinstance(info.value, ValueError)
    assert str(info.value) == 'DASHSCOPE_API_KEY is not configured'


@pytest.mark.parametrize('vectors,message', [
    ([], 'embedding response count mismatch: expected 1, got 0'),
    ([[1.0]], 'embedding dimension mismatch at index 0: expected 2, got 1'),
])
def test_embedding_response_subclass_keeps_value_error_and_message(vectors, message):
    from app.services.vector_embedding_service import DashScopeEmbeddings, EmbeddingResponseError
    response = SimpleNamespace(data=[SimpleNamespace(embedding=v) for v in vectors])
    embedding = DashScopeEmbeddings('测试', dimensions=2,
        client=SimpleNamespace(embeddings=SimpleNamespace(create=lambda **kw: response)))
    with pytest.raises(EmbeddingResponseError) as info:
        embedding.embed_query('文本')
    assert isinstance(info.value, ValueError) and str(info.value) == message


@pytest.mark.parametrize('error', [OSError('secret private'), RuntimeError('secret private')])
def test_milvus_connect_subclass_preserves_old_messages(monkeypatch, error):
    from app.core import milvus_client as module
    def connect(**kw):
        raise error
    monkeypatch.setattr(module.connections, 'connect', connect)
    monkeypatch.setattr(module.MilvusClientManager, 'close', lambda self: None)
    with pytest.raises(module.MilvusConnectError) as info:
        module.MilvusClientManager().connect()
    assert isinstance(info.value, RuntimeError) and info.value.__cause__ is error
    expected = 'secret private' if isinstance(error, RuntimeError) else 'Milvus initialization failed: secret private'
    assert str(info.value) == expected


@pytest.mark.parametrize('ids', [[], ['重复', '重复'], ['单个']])
def test_replace_source_rejects_incomplete_ids_before_deleting(monkeypatch, ids):
    from langchain_core.documents import Document
    manager, events = store_manager()
    monkeypatch.setattr(manager, 'add_document', lambda documents: ids)
    with pytest.raises(RuntimeError):
        manager.replace_source('/docs/a.md', [Document(page_content='甲'), Document(page_content='乙')])
    assert not any(e[0] == '删除' for e in events)


@pytest.mark.parametrize('exists', [False, True])
def test_readonly_milvus_connect_never_creates_or_recreates_collection(monkeypatch, exists):
    from app.core import milvus_client as module
    manager = module.MilvusClientManager()
    events = []
    monkeypatch.setattr(module.connections, 'connect', lambda **kw: None)
    monkeypatch.setattr(module, 'MilvusClient', lambda **kw: object())
    monkeypatch.setattr(module.utility, 'has_collection', lambda name: exists)
    monkeypatch.setattr(module, 'Collection', lambda name: object())
    monkeypatch.setattr(manager, '_get_existing_vector_dimension', lambda: 1)
    monkeypatch.setattr(manager, '_handle_dimension_mismatch', lambda dim: events.append('重建'))
    monkeypatch.setattr(manager, '_create_collection', lambda: events.append('创建'))
    monkeypatch.setattr(manager, '_load_collection', lambda: None)
    monkeypatch.setattr(manager, 'close', lambda: None)
    if exists:
        manager.connect(read_only=True)
    else:
        with pytest.raises(module.MilvusCollectionError) as info:
            manager.connect(read_only=True)
        assert not isinstance(info.value, module.MilvusConnectError)   # Milvus was reached
    assert events == []


def test_a_dimension_mismatch_is_a_collection_error_not_a_connection_error(monkeypatch):
    from app.core import milvus_client as module
    manager = module.MilvusClientManager()
    monkeypatch.setattr(module.connections, 'connect', lambda **kw: None)
    monkeypatch.setattr(module, 'MilvusClient', lambda **kw: object())
    monkeypatch.setattr(module.utility, 'has_collection', lambda name: True)
    monkeypatch.setattr(module, 'Collection', lambda name: object())
    monkeypatch.setattr(manager, '_get_existing_vector_dimension', lambda: manager.vector_dim + 1)
    monkeypatch.setattr(module.config, 'milvus_allow_drop_on_dimension_mismatch', False)
    monkeypatch.setattr(manager, 'close', lambda: None)
    with pytest.raises(module.MilvusCollectionError) as info:
        manager.connect()
    assert isinstance(info.value, RuntimeError) and not isinstance(info.value, module.MilvusConnectError)
    assert str(info.value).startswith('Milvus vector dimension mismatch')


def test_prune_check_names_an_unusable_collection(tmp_path):
    from app.core.milvus_client import MilvusCollectionError

    class Store(FakeStore):
        def list_sources(self):
            raise MilvusCollectionError('secret private')
    lines = []
    assert run(args(str(workspace(tmp_path)), '--prune'), service(Store()), lines.append) == 1
    assert any('清理检查失败：Milvus 集合不可用（MilvusCollectionError）' in line for line in lines)
    assert 'secret' not in '\n'.join(lines)


def test_list_sources_requests_readonly_connection(monkeypatch):
    manager, events = store_manager()
    flags = []
    monkeypatch.setattr(manager._client_manager, 'connect', lambda **kw: flags.append(kw))
    assert manager.list_sources() == set()
    assert flags == [{'read_only': True}]


def test_mixed_stage_early_stop_keeps_general_hint(tmp_path):
    from app.core.milvus_client import MilvusConnectError
    from app.services.vector_embedding_service import EmbeddingConfigError
    for n in range(5):
        (tmp_path / f'{n}.txt').write_text('文档', encoding='utf-8')
    store, lines = FakeStore(), []
    errors = iter([MilvusConnectError('secret'), EmbeddingConfigError('private'), OSError('secret')])
    def replace(source, documents):
        raise next(errors)
    store.replace_source = replace
    assert run(args(str(tmp_path), '--confirm'), service(store), lines.append) == 1
    assert '请先检查 Milvus 是否在运行、DASHSCOPE_API_KEY 是否已配置' in lines[-1]


def test_prune_delete_failure_does_not_leak_message_and_sets_exit_code(tmp_path):
    root = workspace(tmp_path)
    store = FakeStore(sources=[(root / 'gone.md').as_posix()])
    def delete(source):
        raise OSError('secret private')
    store.delete_by_source = delete
    lines = []
    assert run(args(str(root), '--confirm', '--prune'), service(store), lines.append) == 1
    text = '\n'.join(lines)
    assert '清理失败：gone.md（写入 Milvus 失败，OSError）' in text
    assert '清理：0 个已删除文件，共 0 块，失败 1 个' in text
    assert 'secret' not in text and 'private' not in text and store.closed == 1


def test_prune_runs_after_complete_iteration_with_individual_file_failure(tmp_path):
    root = workspace(tmp_path)
    store = FakeStore(sources=[(root / 'gone.md').as_posix()])
    replace = store.replace_source
    def sometimes_fail(source, documents):
        if source.endswith('intro.md'):
            raise OSError('secret')
        return replace(source, documents)
    store.replace_source = sometimes_fail
    lines = []
    assert run(args(str(root), '--confirm', '--prune'), service(store), lines.append) == 1
    assert store.deleted == [(root / 'gone.md').as_posix()] and store.closed == 1


def test_prune_keeps_the_resolved_source_of_an_indexed_alias(tmp_path, monkeypatch):
    root = tmp_path / 'docs'
    (root / '.venv').mkdir(parents=True)
    alias, target = root / 'alias.md', root / '.venv' / 'target.md'
    alias.write_text('别名', encoding='utf-8')
    target.write_text('目标', encoding='utf-8')
    original_resolve = Path.resolve
    def resolve(path, *args, **kwargs):
        return target if path == alias else original_resolve(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'resolve', resolve)
    store, lines = FakeStore(sources=[target.as_posix()]), []
    assert run(args(str(root), '--confirm', '--prune'), service(store), lines.append) == 0
    assert store.replaced == [target.as_posix()] and store.deleted == []


@pytest.mark.parametrize('fails', [False, True])
def test_main_hides_dependency_logs_and_then_restores_the_public_switches(tmp_path, monkeypatch, capsys, fails):
    import logging
    from loguru import logger
    from app.services import vector_index_service as module
    root = workspace(tmp_path)
    index_service = service(FakeStore())
    original_index = index_service.index_single_file

    def index(path):
        logging.getLogger('pymilvus').error('secret private provider')
        logger.error('secret private document')
        if fails:
            raise OSError('secret private')
        return original_index(path)
    monkeypatch.setattr(index_service, 'index_single_file', index)
    monkeypatch.setattr(module, 'vector_index_service', index_service)
    logs = []

    class Handler(logging.Handler):
        def emit(self, record):
            logs.append(record.getMessage())
    handler = Handler()
    logging.getLogger('pymilvus').addHandler(handler)
    sink = logger.add(lambda message: logs.append(str(message)))
    previous_disable = logging.root.manager.disable
    try:
        logging.disable(logging.INFO)
        assert main([str(root), '--confirm']) == (1 if fails else 0)
        assert logs == []                                         # nothing leaked while it ran
        assert logging.root.manager.disable == logging.INFO      # the standard switch is restored
        logging.getLogger('pymilvus').error('恢复后的日志')
        logger.error('恢复后的日志')
        assert len(logs) == 2                                     # both log systems work again
        out = capsys.readouterr().out
        assert 'secret' not in out and 'private' not in out
    finally:
        logging.disable(previous_disable)
        logger.remove(sink)
        logging.getLogger('pymilvus').removeHandler(handler)


def test_run_itself_leaves_logging_alone(tmp_path):
    from loguru import logger
    logs = []
    sink = logger.add(lambda message: logs.append(str(message)))
    store = FakeStore()
    index_service = service(store)
    original_index = index_service.index_single_file

    def index(path):
        logger.error('run 期间的日志')
        return original_index(path)
    index_service.index_single_file = index
    try:
        assert run(args(str(workspace(tmp_path)), '--confirm'), index_service, [].append) == 0
    finally:
        logger.remove(sink)
    assert logs and all('run 期间的日志' in line for line in logs)   # only main() silences logs
