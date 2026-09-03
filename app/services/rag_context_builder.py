"""Build bounded evidence context and citations from ranked candidates."""

from math import ceil
from typing import Callable, Sequence

from app.schemas.rag import BuiltContext, Citation, RetrievedDocument


class ContextBuilder:
    """Select ranked evidence within a token budget without inventing metadata."""

    def __init__(
        self,
        token_budget: int = 1800,
        token_counter: Callable[[str], int] | None = None,
    ) -> None:
        self.token_budget = max(1, int(token_budget))
        self.token_counter = token_counter or self.estimate_tokens

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """Conservative mixed-language estimate: CJK chars cost one token each."""
        cjk = sum(
            1
            for char in text
            if (
                0x3400 <= ord(char) <= 0x4DBF
                or 0x4E00 <= ord(char) <= 0x9FFF
                or 0xF900 <= ord(char) <= 0xFAFF
            )
        )
        other = len(text) - cjk
        return cjk + ceil(other / 4)

    def build(
        self,
        ranked: Sequence[RetrievedDocument],
        final_top_k: int | None = None,
    ) -> BuiltContext:
        selected: list[RetrievedDocument] = []
        citations: list[Citation] = []
        blocks: list[str] = []
        used_tokens = 0
        skipped = 0
        limit = max(1, int(final_top_k or len(ranked) or 1))

        for candidate in ranked:
            if len(selected) >= limit:
                skipped += 1
                continue
            metadata = candidate.document.metadata or {}
            source = str(metadata.get("_source") or metadata.get("source") or "")
            file_name = str(metadata.get("_file_name") or metadata.get("file_name") or "")
            heading_path = " > ".join(
                str(metadata[key]) for key in ("h1", "h2", "h3") if metadata.get(key)
            )
            ref_id = len(selected) + 1
            block = (
                f"[ref:{ref_id}]\n"
                f"file: {file_name}\n"
                f"source: {source}\n"
                f"heading: {heading_path}\n"
                f"content:\n{candidate.document.page_content}"
            )
            block_tokens = self.token_counter(block)
            if used_tokens + block_tokens > self.token_budget:
                skipped += 1
                continue
            selected.append(candidate)
            blocks.append(block)
            used_tokens += block_tokens
            citations.append(
                Citation(
                    ref_id=ref_id,
                    source=source,
                    file_name=file_name,
                    heading_path=heading_path,
                    start_line=self._line_value(metadata, "start_line"),
                    end_line=self._line_value(metadata, "end_line"),
                    distance=candidate.distance,
                    bm25_score=candidate.bm25_score,
                    rrf_score=candidate.rrf_score,
                    rerank_score=candidate.rerank_score,
                )
            )

        return BuiltContext(
            context="\n\n".join(blocks),
            citations=citations,
            token_count=used_tokens,
            selected_documents=[candidate.document for candidate in selected],
            skipped_documents=skipped,
        )

    @staticmethod
    def _line_value(metadata: dict, key: str) -> int | None:
        value = metadata.get(key)
        return int(value) if isinstance(value, int) and not isinstance(value, bool) else None
