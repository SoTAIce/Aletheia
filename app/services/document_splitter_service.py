"""Document splitting by file type with Markdown heading boundaries."""

import json
from pathlib import Path

from langchain_core.documents import Document
from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    PythonCodeTextSplitter,
    RecursiveCharacterTextSplitter,
    RecursiveJsonSplitter,
)
from loguru import logger

from app.config import config


class DocumentSplitterService:
    def __init__(self) -> None:
        self.chunk_size = config.chunk_max_size
        self.chunk_overlap = config.chunk_overlap
        self.markdown_splitter = MarkdownHeaderTextSplitter(
            headers_to_split_on=[("#", "h1"), ("##", "h2"), ("###", "h3")],
            strip_headers=False,
        )
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=self.chunk_size,
            chunk_overlap=self.chunk_overlap,
            length_function=len,
            separators=[
                "\n\n",
                "\n",
                "。",
                "！",
                "？",
                "；",
                ".",
                "!",
                "?",
                ";",
                "，",
                ",",
                " ",
                "",
            ],
            keep_separator=True,
            is_separator_regex=False,
            add_start_index=True,
        )
        self.code_splitter = PythonCodeTextSplitter(
            chunk_size=self.chunk_size,
            chunk_overlap=self.chunk_overlap,
            length_function=len,
            add_start_index=True,
        )
        self.json_splitter = RecursiveJsonSplitter(
            max_chunk_size=self.chunk_size,
            min_chunk_size=min(300, self.chunk_size),
        )

    def split_document(self, content: str, file_path: str = "") -> list[Document]:
        suffix = Path(file_path).suffix.lower()
        if suffix == ".md":
            return self.split_markdown(content, file_path)
        if suffix == ".py":
            return self.split_code(content, file_path)
        if suffix == ".json":
            return self.split_json(content, file_path)
        return self.split_text(content, file_path)

    @staticmethod
    def _source_metadata(file_path: str) -> dict[str, str]:
        path = Path(file_path)
        return {
            "_source": file_path,
            "_extension": path.suffix.lower(),
            "_file_name": path.name,
        }

    def split_json(self, content: str, file_path: str = "") -> list[Document]:
        if not content or not content.strip():
            return []
        try:
            json_data = json.loads(content)
            documents = self.json_splitter.create_documents(texts=[json_data])
            metadata = self._source_metadata(file_path)
            for document in documents:
                document.metadata.update(metadata)
            return documents
        except Exception:
            logger.exception("JSON document split failed: {}", file_path)
            raise

    def split_code(self, content: str, file_path: str = "") -> list[Document]:
        if not content or not content.strip():
            return []
        try:
            return self.code_splitter.create_documents(
                texts=[content],
                metadatas=[self._source_metadata(file_path)],
            )
        except Exception:
            logger.exception("Python document split failed: {}", file_path)
            raise

    def split_markdown(self, content: str, file_path: str = "") -> list[Document]:
        if not content or not content.strip():
            return []
        try:
            heading_documents = self.markdown_splitter.split_text(content)
            split_documents = self.text_splitter.split_documents(heading_documents)
            final_documents = self._merge_small_chunks(split_documents)
            metadata = self._source_metadata(file_path)
            for document in final_documents:
                document.metadata.update(metadata)
            return final_documents
        except Exception:
            logger.exception("Markdown document split failed: {}", file_path)
            raise

    def split_text(self, content: str, file_path: str = "") -> list[Document]:
        if not content or not content.strip():
            return []
        try:
            return self.text_splitter.create_documents(
                texts=[content],
                metadatas=[self._source_metadata(file_path)],
            )
        except Exception:
            logger.exception("Text document split failed: {}", file_path)
            raise

    def _join_without_duplicate_overlap(self, left: str, right: str) -> str:
        """Join adjacent chunks without repeating splitter overlap text."""
        max_overlap = min(self.chunk_overlap, len(left), len(right))
        for overlap in range(max_overlap, 0, -1):
            if left[-overlap:] == right[:overlap]:
                return left + right[overlap:]
        return left + "\n\n" + right

    def _merge_small_chunks(
        self,
        documents: list[Document],
        min_size: int = 300,
    ) -> list[Document]:
        if not documents:
            return []

        effective_min_size = min(min_size, self.chunk_size)
        merged_documents: list[Document] = []
        current = documents[0]

        for following in documents[1:]:
            current_heading = tuple(current.metadata.get(key) for key in ("h1", "h2", "h3"))
            following_heading = tuple(
                following.metadata.get(key) for key in ("h1", "h2", "h3")
            )
            combined = self._join_without_duplicate_overlap(
                current.page_content,
                following.page_content,
            )
            has_small_chunk = (
                len(current.page_content) < effective_min_size
                or len(following.page_content) < effective_min_size
            )

            if (
                current_heading == following_heading
                and has_small_chunk
                and len(combined) <= self.chunk_size
            ):
                current = Document(
                    page_content=combined,
                    metadata=current.metadata.copy(),
                )
            else:
                merged_documents.append(current)
                current = following

        merged_documents.append(current)
        return merged_documents


document_splitter_service = DocumentSplitterService()
