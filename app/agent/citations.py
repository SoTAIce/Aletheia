"""Task-wide citation ledger, [ref:n] parsing and repair instructions (D-098)."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from typing import Any

CITATION_PATTERN = re.compile(r"\[ref:(\d+(?:\s*,\s*\d+)*)\]")
_HEADER_LINE = re.compile(r"^\[ref:(\d+)\]$")
MAX_SNIPPET_CHARS = 500
MAX_LISTED_REFS = 30


def cited_refs(text: str) -> list[int]:
    """Return unique referenced numbers in order of first appearance."""
    if not isinstance(text, str) or not text:
        return []
    seen: set[int] = set()
    ordered: list[int] = []
    for match in CITATION_PATTERN.finditer(text):
        for part in match.group(1).split(","):
            ref = int(part.strip())
            if ref not in seen:
                seen.add(ref)
                ordered.append(ref)
    return ordered


def _standard_header_indices(lines: list[str]) -> list[int]:
    """Find ContextBuilder block headers: [ref:n], file:, source:, heading:, content:."""
    indices: list[int] = []
    limit = len(lines) - 4
    for i in range(limit):
        if (
            _HEADER_LINE.match(lines[i])
            and lines[i + 1].startswith("file: ")
            and lines[i + 2].startswith("source: ")
            and lines[i + 3].startswith("heading: ")
            and lines[i + 4] == "content:"
        ):
            indices.append(i)
    return indices


def _fallback_header_indices(lines: list[str]) -> list[int]:
    """Fallback when synthetic evidence uses bare [ref:n] lines without ContextBuilder headers."""
    return [i for i, line in enumerate(lines) if _HEADER_LINE.match(line)]


def _extract_block_contents(lines: list[str], headers: list[int], standard: bool) -> list[str]:
    contents: list[str] = []
    for idx, start in enumerate(headers):
        body_start = start + 5 if standard else start + 1
        if idx + 1 < len(headers):
            end = headers[idx + 1]
            # ContextBuilder joins blocks with "\n\n", leaving one empty separator line.
            if end > body_start and lines[end - 1] == "":
                end -= 1
        else:
            end = len(lines)
        contents.append("\n".join(lines[body_start:end]))
    return contents


class CitationLedger:
    """Task-scoped registry mapping retrieved evidence blocks to stable [ref:n] numbers."""

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, int | None, int | None, str], int] = {}
        self._entries: dict[int, dict[str, Any]] = {}
        self._next_id = 1

    @staticmethod
    def _identity(citation: dict[str, Any], content: str) -> tuple[str, int | None, int | None, str]:
        source = str(citation.get("source") or "")
        start_line = citation.get("start_line")
        end_line = citation.get("end_line")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return (source, start_line, end_line, digest)

    @staticmethod
    def _build_entry(ref: int, citation: dict[str, Any], content: str) -> dict[str, Any]:
        return {
            "ref": ref,
            "file_name": str(citation.get("file_name") or ""),
            "source": str(citation.get("source") or ""),
            "heading_path": str(citation.get("heading_path") or ""),
            "start_line": citation.get("start_line"),
            "end_line": citation.get("end_line"),
            "snippet": content[:MAX_SNIPPET_CHARS],
        }

    def assign(self, citations: Sequence[dict[str, Any]], blocks: Sequence[str]) -> dict[int, int]:
        """Assign task-wide ref numbers for aligned (citations, block contents).

        Returns a mapping from each citation's original ref_id to its assigned task-wide ref_id.
        """
        if len(citations) != len(blocks):
            raise ValueError("citations and blocks must have the same length")
        mapping: dict[int, int] = {}
        for citation, content in zip(citations, blocks, strict=True):
            old_ref = int(citation["ref_id"])
            key = self._identity(citation, content)
            assigned = self._by_key.get(key)
            if assigned is None:
                assigned = self._next_id
                self._next_id += 1
                self._by_key[key] = assigned
                self._entries[assigned] = self._build_entry(assigned, citation, content)
            mapping[old_ref] = assigned
        return mapping

    def register_raw(self, citations: Sequence[dict[str, Any]], snippet: str = "") -> None:
        """Fallback registration keeping original ref_ids when evidence blocks do not align."""
        for citation in citations:
            ref = citation.get("ref_id")
            if type(ref) is not int or ref < 1:
                continue
            if ref not in self._entries:
                self._entries[ref] = self._build_entry(ref, citation, snippet)
            if ref >= self._next_id:
                self._next_id = ref + 1

    def renumber_evidence(
        self, evidence: str, citations: Sequence[dict[str, Any]]
    ) -> tuple[str, list[dict[str, Any]], bool]:
        """Renumber ContextBuilder evidence and citations consistently.

        Returns (evidence, updated_citations, aligned). On block/citation mismatch, leaves
        evidence and ref_ids unchanged and registers the raw citations instead.
        """
        if not citations:
            return evidence, [], True
        lines = evidence.split("\n")
        headers = _standard_header_indices(lines)
        standard = bool(headers)
        if not standard:
            headers = _fallback_header_indices(lines)
        if len(headers) != len(citations):
            self.register_raw(citations)
            return evidence, [dict(c) for c in citations], False

        blocks = _extract_block_contents(lines, headers, standard)
        updated: list[dict[str, Any]] = []
        for header_idx, citation, content in zip(headers, citations, blocks, strict=True):
            key = self._identity(citation, content)
            assigned = self._by_key.get(key)
            if assigned is None:
                assigned = self._next_id
                self._next_id += 1
                self._by_key[key] = assigned
                self._entries[assigned] = self._build_entry(assigned, citation, content)
            lines[header_idx] = f"[ref:{assigned}]"
            item = dict(citation)
            item["ref_id"] = assigned
            updated.append(item)
        return "\n".join(lines), updated, True

    def valid(self) -> set[int]:
        return set(self._entries)

    def entry(self, ref: int) -> dict[str, Any] | None:
        item = self._entries.get(ref)
        return dict(item) if item is not None else None

    def resolve(self, text: str) -> tuple[tuple[dict[str, Any], ...], tuple[int, ...]]:
        """Split citations in text into (valid_entries, invalid_ref_ids), preserving first-seen order."""
        valid_entries: list[dict[str, Any]] = []
        invalid_refs: list[int] = []
        for ref in cited_refs(text):
            item = self.entry(ref)
            if item is not None:
                valid_entries.append(item)
            else:
                invalid_refs.append(ref)
        return tuple(valid_entries), tuple(invalid_refs)


def citation_repair_instruction(invalid: Sequence[int], valid: set[int]) -> str:
    """Host-authored English instruction asking the model to fix invalid [ref:n] citations."""
    bad = ", ".join(f"[ref:{n}]" for n in invalid)
    if not valid:
        return (
            f"HOST: your answer cites invalid reference(s): {bad}. "
            "No knowledge base references were retrieved in this task. "
            "Rewrite your answer removing those invalid citations. "
            "Do not call any tools and do not change other content."
        )
    ordered = sorted(valid)
    if len(ordered) > MAX_LISTED_REFS:
        valid_text = f"{ordered[0]}-{ordered[-1]}"
    else:
        valid_text = ", ".join(str(n) for n in ordered)
    return (
        f"HOST: your answer cites invalid reference(s): {bad}. "
        f"Valid references in this task are: {valid_text}. "
        "Rewrite your answer using only valid [ref:n] references (or remove invalid ones). "
        "Do not call any tools and do not change other content."
    )
