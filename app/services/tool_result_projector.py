"""What the model sees of a tool result: inline, or a bounded preview of an archived original (D-052).

Moved out of ContextManager in stage 5c without changing a byte of any view.
"""

from dataclasses import asdict

import asyncio
import hashlib
import json

from app.services.context_budget import ContextBudget


class ToolResultProjector:
    # Tool results inline up to about a third of the input budget (at ~3 characters per token),
    # never below the old fixed 8000 or above 120000 characters; the preview of an archived
    # result is a seventh of that. A 32K window keeps the old 8000/1200 (D-052).
    TOOL_RESULT_MIN_CHARS, TOOL_RESULT_MAX_CHARS = 8000, 120000
    TOOL_PREVIEW_MIN_CHARS, TOOL_PREVIEW_MAX_CHARS = 1200, 8000

    # Its output is already bounded by tool_result_chars (see ContextArchiveTool), so archiving it
    # again would only wrap the same text in another reference (D-052).
    UNARCHIVED_TOOLS = frozenset({'read_context_artifact'})

    def __init__(self, budget: ContextBudget, *, archive=None, tool_result_chars=None, tool_preview_chars=None):
        self.budget = budget
        derived_result, derived_preview = self.tool_budgets(budget.window_tokens, budget.output_tokens,
                                                            budget.safety_tokens)
        tool_result_chars = derived_result if tool_result_chars is None else tool_result_chars
        tool_preview_chars = derived_preview if tool_preview_chars is None else tool_preview_chars
        for name, value in [('tool_result_chars', tool_result_chars), ('tool_preview_chars', tool_preview_chars)]:
            if type(value) is not int or value <= 0:
                raise ValueError(f'{name} must be a positive integer')
        self.archive = archive
        self.tool_result_chars = tool_result_chars
        self.tool_preview_chars = tool_preview_chars
        self._views = {}

    @classmethod
    def tool_budgets(cls, window_tokens, output_tokens, safety_tokens):
        """(tool_result_chars, tool_preview_chars) for a model window (D-052)."""
        input_tokens = max(1, window_tokens - output_tokens - safety_tokens)
        result = min(cls.TOOL_RESULT_MAX_CHARS, max(cls.TOOL_RESULT_MIN_CHARS, input_tokens // 3))
        preview = min(cls.TOOL_PREVIEW_MAX_CHARS, max(cls.TOOL_PREVIEW_MIN_CHARS, result // 7))
        return result, preview

    async def project(self, result):
        raw = json.dumps(asdict(result), ensure_ascii=False)
        key = hashlib.sha256(raw.encode('utf-8')).hexdigest()
        if key in self._views:
            return json.loads(self._views[key])
        view = {name: getattr(result, name) for name in ('call_id', 'tool_name', 'success', 'content', 'error', 'source_refs')}
        # Measured on the text itself: JSON escaping of code would otherwise halve the budget.
        size = len(result.content or '') + len(result.error or '')
        if (self.archive is not None and result.tool_name not in self.UNARCHIVED_TOOLS
                and size > min(self.tool_result_chars, self.budget.window_tokens)):
            # The archive keeps the tool's own text, so read_context_artifact offsets match the
            # file or page that was read; a failure keeps the whole record (D-052).
            text = result.content if result.success and not result.error else raw
            ref = await asyncio.to_thread(self.archive.save_text, text, kind='tool',
                metadata={'call_id': result.call_id, 'tool_name': result.tool_name,
                          'success': result.success, 'format': 'text' if text is result.content else 'json'})
            view.update(content=result.content[:self.tool_preview_chars], storage_ref=ref, truncated=True,
                        total_chars=len(text))
            if view['error']:
                view['error'] = view['error'][:self.tool_preview_chars]
        self._views[key] = json.dumps(view, ensure_ascii=False)
        return json.loads(self._views[key])
