"""Model-context projections. Task state and archived originals are never mutated.

Since stage 5c this is a facade over three parts sharing one ContextBudget:

- ConversationContext (conversation_context.py): the session's pinned history and long-term memories;
- ToolResultProjector (tool_result_projector.py): what the model sees of a tool result;
- RequestFitter (request_fitter.py): fitting one request into the window, including the loops'
  history compaction (formerly CompactingManager) and the retry after a context overflow.

Callers keep using ContextManager; its names and behaviour are unchanged
(tests/test_context_split.py compares every request with one recorded before the split).
"""

from typing import Callable

from app.services.context_budget import ContextBudget
from app.services.conversation_context import LONGTERM_POLICY, ConversationContext
from app.services.request_fitter import ContextBudgetError, RequestFitter
from app.services.tool_result_projector import ToolResultProjector

__all__ = ['LONGTERM_POLICY', 'ContextBudgetError', 'ContextManager']


def _forward(part, name):
    return property(lambda self: getattr(getattr(self, part), name),
                    lambda self, value: setattr(getattr(self, part), name, value))


class ContextManager:
    """Count provider-shaped messages, schemas, output reserve and safety margin.

    The default counter is a mixed-language estimate, not a provider tokenizer.
    Configure the window explicitly; no model window is inferred from its name.
    Only conversation turns are optional in the first deterministic projection.
    """

    TOOL_RESULT_MIN_CHARS = ToolResultProjector.TOOL_RESULT_MIN_CHARS
    TOOL_RESULT_MAX_CHARS = ToolResultProjector.TOOL_RESULT_MAX_CHARS
    TOOL_PREVIEW_MIN_CHARS = ToolResultProjector.TOOL_PREVIEW_MIN_CHARS
    TOOL_PREVIEW_MAX_CHARS = ToolResultProjector.TOOL_PREVIEW_MAX_CHARS
    UNARCHIVED_TOOLS = ToolResultProjector.UNARCHIVED_TOOLS
    tool_budgets = ToolResultProjector.tool_budgets
    # Reductions other services reuse (rag_query_rewrite_service) and tests call directly.
    _drop_longterm_entry = staticmethod(RequestFitter.drop_longterm_entry)
    _drop_oldest_turn = staticmethod(RequestFitter.drop_oldest_turn)
    _shrink_tool = staticmethod(RequestFitter.shrink_tool)

    def __init__(self, *, window_tokens=32768, output_tokens=2048,
                 safety_tokens=1024, token_counter: Callable[[str], int] | None = None,
                 archive=None, tool_result_chars=None, tool_preview_chars=None,
                 summary_body_tokens=2048, summary_body_limit=3):
        budget = ContextBudget(window_tokens, output_tokens, safety_tokens)
        if token_counter is not None:
            budget.count = token_counter
        self.budget = budget
        self.conversation = ConversationContext(budget, summary_body_tokens=summary_body_tokens,
                                                summary_body_limit=summary_body_limit)
        self.tool_results = ToolResultProjector(budget, archive=archive, tool_result_chars=tool_result_chars,
                                                tool_preview_chars=tool_preview_chars)
        self.fitter = RequestFitter(budget)

    # The shared budget and each part's settings, under their old names (readable and, as before,
    # assignable; an assignment changes the part that uses the value).
    window_tokens = _forward('budget', 'window_tokens')
    output_tokens = _forward('budget', 'output_tokens')
    safety_tokens = _forward('budget', 'safety_tokens')
    _count = _forward('budget', 'count')
    archive = _forward('tool_results', 'archive')
    tool_result_chars = _forward('tool_results', 'tool_result_chars')
    tool_preview_chars = _forward('tool_results', 'tool_preview_chars')
    summary_body_tokens = _forward('conversation', 'summary_body_tokens')
    summary_body_limit = _forward('conversation', 'summary_body_limit')

    @property
    def overflow_recovery_interrupted(self) -> bool:
        return self.fitter.overflow_recovery_interrupted

    @overflow_recovery_interrupted.setter
    def overflow_recovery_interrupted(self, value: bool) -> None:
        self.fitter.overflow_recovery_interrupted = value

    async def project_tool_result(self, result):
        return await self.tool_results.project(result)

    def longterm_context(self, hits, *, user_id: str, token_budget: int = 600) -> str:
        return self.conversation.longterm(hits, user_id=user_id, token_budget=token_budget)

    def conversation_context(self, memory, query=''):
        return self.conversation.history(memory, query)

    async def prepare_conversation(self, memory, query=''):
        return await self.conversation.prepare(memory, query)

    def count_request(self, messages, tools):
        return self.fitter.count_request(messages, tools)

    def prepare(self, messages, tools, *, aggressive=False):
        return self.fitter.prepare(messages, tools, aggressive=aggressive)

    async def generate(self, model, messages, tools, *, compact=False):
        return await self.fitter.generate(model, messages, tools, compact=compact)
