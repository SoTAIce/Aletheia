"""Fit one model request into the window, in one place (stage 5c, docs/stage5_plan.md).

For a loop the order is: compact the history's model view above 60 % of the window, and again,
harder, above 85 % (app/agent/model_view.py; formerly CompactingManager); then shrink archived tool
results, long-term memories and old conversation turns until the request fits (ContextBudgetError
when it cannot); after a provider's context-overflow refusal, retry once with the optional history
removed. Only the messages sent change; the caller's records and tool receipts are never modified.
"""

from dataclasses import replace

import asyncio
import json

from app.agent.openai_compatible_tool_calling_model import OpenAICompatibleToolCallingModel
from app.agent.step_models import ToolMessage, UserMessage
from app.agent.tool_calling_model import ModelContextOverflowError, ModelRequestError
from app.services.context_budget import ContextBudget


class ContextBudgetError(ModelRequestError):
    """Required model context does not fit the configured estimated budget."""


class RequestFitter:
    # The compaction tiers for a loop's history (D-019 follow-up): superseded write_file bodies and
    # older outputs above VIEW_COMPACT_SHARE; every write_file body but the latest above STRONG_SHARE.
    STRONG_SHARE = 0.85

    def __init__(self, budget: ContextBudget):
        self.budget = budget
        self.overflow_recovery_interrupted = False

    @property
    def window_tokens(self) -> int:              # read by model_view.compact_model_view
        return self.budget.window_tokens

    def count_request(self, messages, tools):
        payload = {
            'messages': OpenAICompatibleToolCallingModel._build_message_payload(messages),
            'tools': OpenAICompatibleToolCallingModel._build_tool_payload(tools),
        }
        return (self.budget.count(json.dumps(payload, ensure_ascii=False, allow_nan=False))
                + self.budget.output_tokens + self.budget.safety_tokens)

    def compact(self, messages, tools):
        """The two compaction tiers of a loop's model view."""
        from app.agent.model_view import compact_model_view
        view = compact_model_view(messages, tools, self)
        if self.count_request(view, tools) > self.budget.window_tokens * self.STRONG_SHARE:
            view = compact_model_view(view, tools, self, share=0, all_but_last_write=True)
        return view

    @staticmethod
    def drop_longterm_entry(message):
        if not isinstance(message, UserMessage):
            return None
        text = message.content
        if text.lstrip().startswith("{"):
            start = len(text) - len(text.lstrip())
        else:
            pos = text.find("\n{")
            if pos < 0:
                return None
            start = pos + 1
        try:
            data, end = json.JSONDecoder().raw_decode(text[start:])
            if not isinstance(data, dict):
                return None
            for pin in data.get("pinned_contexts", []):
                if pin.get("source") != "longterm_memory":
                    continue
                payload = json.loads(pin["content"])
                entries = payload.get("entries")
                if not isinstance(entries, list) or not entries:
                    continue
                entries.pop()
                pin["content"] = json.dumps(payload, ensure_ascii=False)
                return replace(message, content=text[:start] + json.dumps(data, ensure_ascii=False)
                               + text[start + end:])
        except (ValueError, TypeError, KeyError):
            return None
        return None

    @staticmethod
    def drop_oldest_turn(message):
        if not isinstance(message, UserMessage):
            return None
        # Runner context is one JSON document. Instructions may come before the JSON document;
        # format-repair suffixes are preserved too.
        text = message.content
        start = 0 if text.lstrip().startswith('{') else text.find('\n{')
        if start < 0:
            return None
        try:
            data, end = json.JSONDecoder().raw_decode(text[start:].lstrip())
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        for pin in data.get('pinned_contexts', []):
            if pin.get('source') != 'conversation_memory':
                continue
            try:
                history = json.loads(pin['content'])
            except (ValueError, KeyError):
                continue
            if not isinstance(history, dict):
                continue
            for field in ('turns', 'summary_bodies', 'raw_index', 'summary_index'):
                if isinstance(history.get(field), list) and history[field]:
                    # Keep two recent turns ahead of optional summary bodies;
                    # older raw history is the first reducible conversation data.
                    if field == 'turns' and len(history[field]) <= 2 and history.get('summary_bodies'):
                        continue
                    history[field].pop(0 if field == 'turns' else -1)
                    break
            else:
                continue
            pin['content'] = json.dumps(history, ensure_ascii=False)
            leading = len(text[start:]) - len(text[start:].lstrip())
            return replace(message, content=text[:start + leading] + json.dumps(data, ensure_ascii=False)
                           + text[start + leading + end:])
        return None

    @staticmethod
    def shrink_tool(message):
        if not isinstance(message, ToolMessage):
            return None
        try:
            payload = json.loads(message.content)
        except ValueError:
            return None
        if not isinstance(payload, dict) or not payload.get('storage_ref'):
            return None
        if not payload.get('content') and len(payload.get('error') or '') <= 200:
            return None
        payload['content'] = ''
        if payload.get('error'):
            payload['error'] = payload['error'][:200]
        return replace(message, content=json.dumps(payload, ensure_ascii=False))

    def prepare(self, messages, tools, *, aggressive=False):
        projected = tuple(messages)
        if aggressive:
            # Only the optional historical view changes; the caller's messages
            # and completed tool receipts remain intact.
            while True:
                for index, message in enumerate(projected):
                    smaller = self.drop_oldest_turn(message)
                    if smaller is not None:
                        projected = (*projected[:index], smaller, *projected[index + 1:])
                        break
                else:
                    break
            projected = tuple(self.shrink_tool(m) or m for m in projected)
        while self.count_request(projected, tools) > self.budget.window_tokens:
            for reduction in (self.shrink_tool, self.drop_longterm_entry, self.drop_oldest_turn):
                for index, message in enumerate(projected):
                    smaller = reduction(message)
                    if smaller is not None:
                        projected = (*projected[:index], smaller, *projected[index + 1:])
                        break
                else:
                    continue
                break
            else:
                raise ContextBudgetError('Required context exceeds configured token budget')
        return projected

    async def generate(self, model, messages, tools, *, compact=False):
        """One model call. compact=True (the agent loops) compacts the history's view first."""
        if compact:
            messages = self.compact(tuple(messages), tools)
        self.overflow_recovery_interrupted = False
        projected = self.prepare(messages, tools)
        try:
            return await model.generate(projected, tools)
        except ModelContextOverflowError:
            try:
                return await model.generate(self.prepare(projected, tools, aggressive=True), tools)
            except asyncio.CancelledError:
                # asyncio.timeout translates this cancellation outside the runner.
                # Retain its cause so LoopRun does not retry and replay side effects.
                self.overflow_recovery_interrupted = True
                raise
