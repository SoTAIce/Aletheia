"""Model-context projections. Task state and archived originals are never mutated."""

from dataclasses import asdict, replace
import asyncio
import hashlib
import json
from typing import Callable

from app.agent.openai_compatible_tool_calling_model import OpenAICompatibleToolCallingModel
from app.agent.step_models import UserMessage
from app.agent.step_models import ToolMessage
from app.agent.tool_calling_model import ModelRequestError, ModelContextOverflowError
from app.services.rag_context_builder import ContextBuilder


class ContextBudgetError(ModelRequestError):
    """Required model context does not fit the configured estimated budget."""


class ContextManager:
    """Count provider-shaped messages, schemas, output reserve and safety margin.

    The default counter is a mixed-language estimate, not a provider tokenizer.
    Configure the window explicitly; no model window is inferred from its name.
    Only conversation turns are optional in the first deterministic projection.
    """

    def __init__(self, *, window_tokens=32768, output_tokens=2048,
                 safety_tokens=1024, token_counter: Callable[[str], int] | None = None,
                 archive=None, tool_result_chars=8000, tool_preview_chars=1200):
        for name, value, minimum in [('window_tokens', window_tokens, 1),
                                     ('output_tokens', output_tokens, 1),
                                     ('safety_tokens', safety_tokens, 0)]:
            if type(value) is not int or value < minimum:
                raise ValueError(f'{name} must be an integer >= {minimum}')
        if output_tokens + safety_tokens >= window_tokens:
            raise ValueError('Output reserve and safety must be smaller than window_tokens')
        self.window_tokens = window_tokens
        self.output_tokens = output_tokens
        self.safety_tokens = safety_tokens
        self._count = token_counter or ContextBuilder.estimate_tokens
        for name, value in [('tool_result_chars',tool_result_chars), ('tool_preview_chars',tool_preview_chars)]:
            if type(value) is not int or value <= 0:
                raise ValueError(f'{name} must be a positive integer')
        self.archive = archive
        self.tool_result_chars = tool_result_chars
        self.tool_preview_chars = tool_preview_chars
        self._tool_views = {}
        self.overflow_recovery_interrupted = False

    async def project_tool_result(self, result):
        raw = json.dumps(asdict(result), ensure_ascii=False)
        key = hashlib.sha256(raw.encode('utf-8')).hexdigest()
        if key in self._tool_views:
            return json.loads(self._tool_views[key])
        view = {name:getattr(result,name) for name in ('call_id','tool_name','success','content','error','source_refs')}
        if self.archive is not None and len(raw) > min(self.tool_result_chars, self.window_tokens):
            ref = await asyncio.to_thread(self.archive.save_text, raw, kind='tool',
                metadata={'call_id':result.call_id,'tool_name':result.tool_name})
            view.update(content=result.content[:self.tool_preview_chars], storage_ref=ref, truncated=True)
            if view['error']:
                view['error'] = view['error'][:self.tool_preview_chars]
        self._tool_views[key] = json.dumps(view, ensure_ascii=False)
        return json.loads(self._tool_views[key])

    def count_request(self, messages, tools):
        payload = {
            'messages': OpenAICompatibleToolCallingModel._build_message_payload(messages),
            'tools': OpenAICompatibleToolCallingModel._build_tool_payload(tools),
        }
        return self._count(json.dumps(payload, ensure_ascii=False, allow_nan=False)) + self.output_tokens + self.safety_tokens

    def conversation_context(self, memory, query=''):
        return json.dumps({
            # Archive summaries are discovered through bounded, source-linked
            # entries, rather than duplicating an unrelated global summary.
            'summary': None if memory.archive is not None else memory.summary,
            'turns': [asdict(turn) for turn in memory.retrieve_turns(k=len(memory))],
            'summary_index': [asdict(entry) for entry in memory.find_summaries(query)]
                            if memory.archive is not None else [],
        }, ensure_ascii=False, default=lambda value: value.isoformat())

    async def prepare_conversation(self, memory, query=''):
        history = json.loads(self.conversation_context(memory, query))
        if memory.archive is not None:
            history['raw_index'] = await memory.find_turns(query)
        return json.dumps(history, ensure_ascii=False)

    @staticmethod
    def _drop_oldest_turn(message):
        if not isinstance(message, UserMessage):
            return None
        # Runner context is one JSON document. Planner keeps its instructions
        # before the JSON document; format-repair suffixes are preserved too.
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
            for field in ('turns', 'raw_index', 'summary_index'):
                if isinstance(history.get(field), list) and history[field]:
                    history[field].pop(0 if field == 'turns' else -1)
                    break
            else:
                continue
            pin['content'] = json.dumps(history, ensure_ascii=False)
            leading = len(text[start:]) - len(text[start:].lstrip())
            return replace(message, content=text[:start+leading] + json.dumps(data, ensure_ascii=False) + text[start+leading+end:])
        return None

    @staticmethod
    def _shrink_tool(message):
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
                    smaller = self._drop_oldest_turn(message)
                    if smaller is not None:
                        projected = (*projected[:index], smaller, *projected[index+1:])
                        break
                else:
                    break
            projected = tuple(self._shrink_tool(m) or m for m in projected)
        while self.count_request(projected, tools) > self.window_tokens:
            for reduction in (self._shrink_tool, self._drop_oldest_turn):
                for index, message in enumerate(projected):
                    smaller = reduction(message)
                    if smaller is not None:
                        projected = (*projected[:index], smaller, *projected[index+1:])
                        break
                else:
                    continue
                break
            else:
                raise ContextBudgetError('Required context exceeds configured token budget')
        return projected

    async def generate(self, model, messages, tools):
        self.overflow_recovery_interrupted = False
        projected = self.prepare(messages, tools)
        if getattr(model, 'context_overflow_managed', False) is True:
            try:
                return await model.generate(projected, tools)
            except asyncio.CancelledError:
                manager = getattr(model, 'manager', None)
                self.overflow_recovery_interrupted = bool(
                    getattr(manager, 'overflow_recovery_interrupted', False))
                raise
        try:
            return await model.generate(projected, tools)
        except ModelContextOverflowError:
            try:
                return await model.generate(self.prepare(projected, tools, aggressive=True), tools)
            except asyncio.CancelledError:
                # asyncio.timeout translates this cancellation outside the runner.
                # Retain its cause so Runtime cannot replan and replay side effects.
                self.overflow_recovery_interrupted = True
                raise


class ContextManagedModel:
    """Keep the existing generate protocol, checking every model/repair request."""

    def __init__(self, model, manager):
        self._model = model
        self.manager = manager

    async def generate(self, messages, tools):
        return await self.manager.generate(self._model, messages, tools)
    context_overflow_managed = True

