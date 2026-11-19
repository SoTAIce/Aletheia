"""Standalone query rewriting with strict structured-output and fallback."""

import asyncio
import json
from typing import Any, Sequence

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from app.schemas.rag import QueryRewriteResult


class _UsageCatcher(BaseCallbackHandler):
    """Collects the rewrite call's token usage, which structured output does not return, and books
    it under 'query_rewrite' (docs/cost_measurement_plan.md). Reading usage never fails the call."""

    def __init__(self) -> None:
        self.prompt = self.completion = None

    def on_llm_end(self, response, **kwargs) -> None:
        try:
            from app.agent.run_meter import usage_tokens
            usage = (getattr(response, "llm_output", None) or {}).get("token_usage")
            if not usage:
                message = getattr(response.generations[0][0], "message", None)
                usage = getattr(message, "usage_metadata", None)
            self.prompt, self.completion = usage_tokens(usage)
        except Exception:
            pass

    def book(self) -> None:
        try:
            from app.agent.run_meter import record_auxiliary
            record_auxiliary("query_rewrite", self.prompt, self.completion)
        except Exception:
            pass


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

    @staticmethod
    def build_prompt(original: str, history: str):
        return [
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

    @classmethod
    def budget_history(cls, manager, question: str, packet: str) -> str:
        """Project a history packet against the complete rewrite request estimate."""
        from app.agent.step_models import UserMessage, SystemMessage as AgentSystem, ToolDefinition
        from app.services.context_manager import ContextBudgetError
        schema = (ToolDefinition('QueryRewriteResult', QueryRewriteResult.__doc__ or 'Structured query rewrite result',
                                 QueryRewriteResult.model_json_schema()),)
        message = UserMessage(packet)
        while True:
            prompt = cls.build_prompt(question, 'human: ' + message.content)
            request = (AgentSystem(prompt[0].content), UserMessage(prompt[1].content))
            if manager.count_request(request, schema) <= manager.window_tokens:
                return message.content
            smaller = manager._drop_oldest_turn(message)
            if smaller is None:
                raise ContextBudgetError('Required RAG rewrite context exceeds configured token budget')
            message = smaller

    async def rewrite(
        self,
        question: str,
        chat_history: Sequence[BaseMessage | str] | None = None,
    ) -> QueryRewriteResult:
        original = (question or "").strip()
        if not original:
            return QueryRewriteResult()

        history = self._history_text(chat_history)
        prompt = self.build_prompt(original, history)
        usage = _UsageCatcher()
        try:
            structured_llm = self.llm.with_structured_output(QueryRewriteResult)
            try:
                raw = await asyncio.wait_for(
                    structured_llm.ainvoke(prompt, config={"callbacks": [usage]}),
                    timeout=self.timeout_seconds,
                )
            finally:
                usage.book()
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
