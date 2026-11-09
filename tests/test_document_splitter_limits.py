"""超长块的字节限制、顺序、元数据及读取阶段回归（D-071）。"""
import inspect
import json

import pytest
from langchain_core.documents import Document

from app.services.document_splitter_service import DocumentSplitterService
from app.services.vector_index_service import IndexStageError, VectorIndexService


def test_default_byte_limit_matches_store_schema():
    from app.core.milvus_client import MilvusClientManager
    assert inspect.signature(DocumentSplitterService).parameters['max_chunk_bytes'].default == MilvusClientManager.CONTENT_MAX_LENGTH == 8000


def test_long_json_is_resplit_in_order_with_source_metadata():
    splitter = DocumentSplitterService()
    content = json.dumps({'before': '前', 'long': ''.join(f'{i:05d}' for i in range(6000)), 'after': '后'})
    original = splitter.split_json(content, '目录/long.json')
    documents = splitter.split_document(content, '目录/long.json')
    expected = [(text, doc.metadata) for doc in original for text in
                (splitter.text_splitter.split_text(doc.page_content)
                 if len(doc.page_content.encode('utf-8')) > 8000 else [doc.page_content])]
    assert len(documents) > len(original)
    assert [(d.page_content, d.metadata) for d in documents] == expected
    assert all(len(d.page_content.encode('utf-8')) <= 8000 for d in documents)
    assert all(d.metadata['_source'] == '目录/long.json' for d in documents)
    assert all(d.metadata['_extension'] == '.json' and d.metadata['_file_name'] == 'long.json' for d in documents)


@pytest.mark.parametrize('extension,method', [('.md', 'split_markdown'), ('.py', 'split_code'), ('.json', 'split_json'), ('.txt', 'split_text')])
def test_chinese_byte_overflow_is_checked_for_every_type(monkeypatch, extension, method):
    splitter = DocumentSplitterService()
    path = '长文' + extension
    metadata = {**splitter._source_metadata(path), 'h1': '标题', 'start_index': 42}
    long = Document(page_content='中文内容' * 800, metadata=metadata)
    before, after = Document(page_content='前块'), Document(page_content='后块')
    assert len(long.page_content) < 8000 < len(long.page_content.encode('utf-8'))
    monkeypatch.setattr(splitter, method, lambda *args: [before, long, after])
    result = splitter.split_document('占位', path)
    assert result[0] is before and result[-1] is after
    assert len(result) > 3
    assert all(len(d.page_content.encode('utf-8')) <= 8000 for d in result)
    assert [d.page_content for d in result[1:-1]] == splitter.text_splitter.split_text(long.page_content)
    assert all(d.metadata == metadata and d.metadata is not long.metadata for d in result[1:-1])


def test_normal_markdown_is_unchanged():
    splitter = DocumentSplitterService()
    content = '# 一级\n\n' + '普通中文段落。' * 100 + '\n\n## 二级\n\n' + '另一段文字。' * 100
    before = splitter.split_markdown(content, 'normal.md')
    assert splitter.split_document(content, 'normal.md') == before


def test_still_oversized_chunk_raises_before_store_access(tmp_path):
    splitter = DocumentSplitterService(max_chunk_bytes=1)
    path = tmp_path / '过长.txt'
    path.write_text('中文', encoding='utf-8')
    with pytest.raises(ValueError, match='过长.txt.*块超过长度上限'):
        splitter.split_document('中文', str(path))
    class NoStore:
        def replace_source(self, *args):
            pytest.fail('读取失败时不应接触向量库')
    service = VectorIndexService(splitter=splitter, store_manager=NoStore())
    with pytest.raises(IndexStageError) as error:
        service.index_single_file(path)
    assert error.value.stage == 'read'
    assert isinstance(error.value.__cause__, ValueError)
