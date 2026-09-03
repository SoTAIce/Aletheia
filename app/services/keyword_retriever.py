"""Small in-memory BM25 retriever with identifier-aware tokenization."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Protocol, Sequence

from langchain_core.documents import Document
from rank_bm25 import BM25Okapi


_BASE_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)*|[\u3400-\u9fff]")
_CAMEL_PART = re.compile(r"[A-Z]+(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|\d+")


def tokenize_for_bm25(text: str) -> list[str]:
    """Lowercase words while retaining full code identifiers and useful subparts."""
    tokens: list[str] = []
    for raw in _BASE_TOKEN.findall(text or ""):
        normalized = raw.lower()
        tokens.append(normalized)
        if raw.isascii() and ("_" in raw or any(char.isupper() for char in raw[1:])):
            parts: list[str] = []
            for segment in raw.split("_"):
                parts.extend(_CAMEL_PART.findall(segment))
            tokens.extend(part.lower() for part in parts if part and part.lower() != normalized)
    return tokens


@dataclass(frozen=True)
class KeywordSearchResult:
    document: Document
    score: float
    rank: int


class KeywordRetriever(Protocol):
    def search(self, query: str, k: int = 20) -> Sequence[KeywordSearchResult]: ...


class InMemoryBM25Retriever:
    """Immutable BM25Okapi index over an existing document snapshot."""

    def __init__(self, documents: Sequence[Document]) -> None:
        self.documents = list(documents)
        self._tokenized_corpus = [
            tokenize_for_bm25(document.page_content) for document in self.documents
        ]
        self._vocabulary = {
            token for document_tokens in self._tokenized_corpus for token in document_tokens
        }
        self._index = BM25Okapi(self._tokenized_corpus) if self.documents else None

    def search(self, query: str, k: int = 20) -> list[KeywordSearchResult]:
        tokens = tokenize_for_bm25(query)
        if self._index is None or not tokens or not self._vocabulary.intersection(tokens):
            return []
        scores = self._index.get_scores(tokens)
        ranked_indexes = sorted(
            range(len(self.documents)), key=lambda index: (-float(scores[index]), index)
        )
        results: list[KeywordSearchResult] = []
        for index in ranked_indexes:
            score = float(scores[index])
            results.append(
                KeywordSearchResult(
                    document=self.documents[index], score=score, rank=len(results) + 1
                )
            )
            if len(results) >= max(1, int(k)):
                break
        return results
