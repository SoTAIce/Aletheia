"""Standalone query rewriting with strict structured-output and fallback."""

import asyncio
import json
from typing import Any, Sequence

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from app.schemas.rag import QueryRewriteResult


class QueryRewriteService:
    """Rewrite a question for retrieval without deciding whether to retrieve."""

    MAX_REWRITES = 2
    MAX_HISTORY_MESSAGES = 6

    def __init__(
        self,
        llm: Any,
        max_history_messages: int = MAX_HISTORY_MESSAGES,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.llm = llm
        self.max_history_messages = max(0, int(max_history_messages))
        self.timeout_seconds = max(0.01, float(timeout_seconds))

    @staticmethod
    def _text(value: Any) -> str:
        content = getattr(value, "content", value)
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                block if isinstance(block, str) else str(block.get("text", ""))
                for block in content
                if isinstance(block, (str, dict))
            )
        return str(content) if content is not None else ""

    def _history_text(self, chat_history: Sequence[BaseMessage | str] | None) -> str:
        recent = list(chat_history or [])[-self.max_history_messages :]
        lines: list[str] = []
        for message in recent:
            role = getattr(message, "type", "message")
            lines.append(f"{role}: {self._text(message)}")
        return "\n".join(lines)

    @staticmethod
    def _parse(value: Any) -> QueryRewriteResult:
        if isinstance(value, QueryRewriteResult):
            return value
        if isinstance(value, dict):
            return QueryRewriteResult.model_validate(value)
        if isinstance(value, str):
            parsed = json.loads(value)
            if not isinstance(parsed, dict):
                raise ValueError("structured rewrite output must be an object")
            return QueryRewriteResult.model_validate(parsed)
        raise TypeError("unsupported structured rewrite output")

    async def rewrite(
        self,
        question: str,
        chat_history: Sequence[BaseMessage | str] | None = None,
    ) -> QueryRewriteResult:
        original = (question or "").strip()
        if not original:
            return QueryRewriteResult()

        history = self._history_text(chat_history)
        prompt = [
            SystemMessage(
                content=(
                    "Rewrite the user's question into at most two distinct search queries. "
                    "Resolve pronouns using only the supplied recent history. Preserve every "
                    "explicit constraint. Rewrites may focus on different semantic aspects and "
                    "may omit exact identifiers because the original query is always searched. "
                    "Never alter an error code or version, or replace an explicit class/function "
                    "name with a different entity. Never return the original question verbatim. "
                    "Return only the structured QueryRewriteResult."
                )
            ),
            HumanMessage(
                content=f"Recent chat history:\n{history or '(none)'}\n\nQuestion:\n{original}"
            ),
        ]
        try:
            structured_llm = self.llm.with_structured_output(QueryRewriteResult)
            raw = await asyncio.wait_for(
                structured_llm.ainvoke(prompt), timeout=self.timeout_seconds
            )
            parsed = self._parse(raw)
            cleaned: list[str] = []
            for query in parsed.rewritten_queries:
                if not isinstance(query, str):
                    raise TypeError("rewritten query must be a string")
                normalized = query.strip()
                if normalized and normalized != original and normalized not in cleaned:
                    cleaned.append(normalized)
            return QueryRewriteResult(
                rewritten_queries=cleaned[: self.MAX_REWRITES], reason=parsed.reason
            )
        except Exception as exc:
            return QueryRewriteResult(warning=f"query rewrite fallback: {type(exc).__name__}")
