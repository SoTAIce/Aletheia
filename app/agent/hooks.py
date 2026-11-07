"""Hooks: fixed extension points around tasks, plans, model calls, tool calls and steps (D-058, D-059).

Two kinds:
- observers (``Hook.observe``) receive host-authored event dicts; they are synchronous, must
  not block, and their errors never affect the task;
- gates (``Gate.check``) run before a model call or a tool call and may only allow or deny;
  an error or a timeout counts as a denial.

The hooks of a run travel in a context variable (as the run meter does), so the wrappers
around tool objects (``HookedTool``) and the base model (``HookedModel``) reach them without
passing them through every function; single mode, team members, planning and wrap-up calls
all pass through the same wrappers. Safety checks stay inside the tools: hooks can only add
restrictions. Events never carry tool output, model text, file contents or exception text.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import logging
import time
from typing import Any, Callable, Protocol

from app.agent.run_meter import current_meter
from app.agent.step_models import ToolOutput, ToolsShownOnly
from app.agent.tool_calling_model import ModelRequestError
from app.tools.tool_registry import ToolRegistry, current_call_id

logger = logging.getLogger(__name__)

TASK_START, TASK_END = 'task_start', 'task_end'
BEFORE_MODEL, AFTER_MODEL = 'before_model', 'after_model'
BEFORE_TOOL, AFTER_TOOL = 'before_tool', 'after_tool'
PLAN, STEP = 'plan', 'step'
EVENT_TYPES = frozenset({TASK_START, TASK_END, BEFORE_MODEL, AFTER_MODEL, BEFORE_TOOL, AFTER_TOOL, PLAN, STEP})
GATE_EVENTS = frozenset({BEFORE_MODEL, BEFORE_TOOL})

GATE_TIMEOUT_SECONDS = 5.0
# Argument values kept in a tool event when they are short scalars: locations and bounds, never
# text the model wrote (contents, patterns, queries), which is reduced to its length.
_SUMMARY_VALUE_KEYS = frozenset({'path', 'start_line', 'max_lines', 'offset', 'limit', 'max_results',
                                 'glob', 'regex', 'ignore_case', 'overwrite', 'timeout_seconds'})
_SUMMARY_VALUE_CHARS = 200


@dataclass(frozen=True, slots=True)
class Decision:
    allow: bool
    reason: str | None = None        # host-authored; shown to the model or the user when denied

    @classmethod
    def deny(cls, reason: str) -> 'Decision':
        return cls(False, reason)


ALLOW = Decision(True)


class Hook(Protocol):
    name: str

    def observe(self, event: dict) -> None: ...


class Gate(Protocol):
    name: str
    events: frozenset[str]

    async def check(self, event: dict) -> Decision: ...


class PolicyDeniedError(ModelRequestError):
    """A gate refused a model call; nothing was sent to the provider."""

    def __init__(self, hook: str, reason: str) -> None:
        super().__init__(f'model call denied by {hook}: {reason}')
        self.hook, self.reason = hook, reason
        self.retryable = False


class HookSet:
    """The hooks of one run; immutable, assembled when the run starts."""

    def __init__(self, observers=(), gates=(), *, gate_timeout: float = GATE_TIMEOUT_SECONDS) -> None:
        self.observers, self.gates = tuple(observers), tuple(gates)
        for hook in self.observers:
            if not callable(getattr(hook, 'observe', None)):
                raise TypeError('an observer needs observe(event)')
        for gate in self.gates:
            if not callable(getattr(gate, 'check', None)):
                raise TypeError('a gate needs check(event)')
            if not set(getattr(gate, 'events', ())) <= GATE_EVENTS or not gate.events:
                raise ValueError(f'gates run only on {sorted(GATE_EVENTS)}')
        self.gate_timeout = gate_timeout

    def with_observers(self, *observers) -> 'HookSet':
        return HookSet((*self.observers, *observers), self.gates, gate_timeout=self.gate_timeout)

    def emit(self, event: dict) -> None:
        """Tell every observer, each with its own copy; failures are logged by type only."""
        for hook in self.observers:
            try:
                hook.observe(dict(event))
            except Exception as exc:
                logger.warning('Hook %s failed: %s', _name(hook), type(exc).__name__)

    async def gate(self, event: dict) -> tuple[Decision, str | None]:
        """Ask every gate for this event in order; the first denial wins. Returns the decision
        and the denying gate's name. An error or a timeout is a denial."""
        for gate in self.gates:
            if event['type'] not in gate.events:
                continue
            try:
                async with asyncio.timeout(self.gate_timeout_for(gate)):
                    decision = await gate.check(dict(event))
            except Exception as exc:          # includes TimeoutError; cancellation propagates
                logger.warning('Gate %s failed: %s', _name(gate), type(exc).__name__)
                return Decision.deny(f'{_name(gate)} could not decide'), _name(gate)
            if not isinstance(decision, Decision):
                return Decision.deny(f'{_name(gate)} gave no decision'), _name(gate)
            if not decision.allow:
                return Decision(False, decision.reason or f'denied by {_name(gate)}'), _name(gate)
        return ALLOW, None

    def gate_timeout_for(self, gate) -> float:
        """A gate may ask for longer (a person deciding); others get the default."""
        own = getattr(gate, 'timeout_seconds', None)
        return own if isinstance(own, (int, float)) and not isinstance(own, bool) and own > 0 else self.gate_timeout


def _name(hook) -> str:
    return getattr(hook, 'name', None) or type(hook).__name__


EMPTY = HookSet()
_current: ContextVar[HookSet] = ContextVar('aletheia_hooks', default=EMPTY)
_purpose: ContextVar[str] = ContextVar('aletheia_model_purpose', default='step')


def current_hooks() -> HookSet:
    return _current.get()


@contextmanager
def using_hooks(hooks: HookSet):
    if not isinstance(hooks, HookSet):
        raise TypeError('hooks must be a HookSet')
    token = _current.set(hooks)
    try:
        yield hooks
    finally:
        _current.reset(token)


@contextmanager
def model_purpose(purpose: str):
    """Label the model calls made inside (plan, route, ...); unlabelled calls are steps."""
    token = _purpose.set(purpose)
    try:
        yield
    finally:
        _purpose.reset(token)


_deadlines: ContextVar[tuple] = ContextVar('aletheia_pausable_deadlines', default=())


@contextmanager
def pausable_deadline(timeout):
    """Register an asyncio.Timeout that time spent waiting for a person should not use up (D-061)."""
    token = _deadlines.set((*_deadlines.get(), timeout))
    try:
        yield timeout
    finally:
        _deadlines.reset(token)


def extend_deadlines(seconds: float) -> None:
    """Move every registered deadline and the run meter's time budget by seconds (D-061):
    out before waiting for a person, back by whatever of that wait was not used."""
    if not seconds:
        return
    for timeout in _deadlines.get():
        when = timeout.when()
        if when is not None and not timeout.expired():
            timeout.reschedule(when + seconds)
    meter = current_meter()
    if meter is not None and meter.timeout_seconds is not None:
        meter.started += seconds


class CallbackHook:
    """The old progress callback (D-045) as an observer: it still receives only the
    'plan' and 'step' records, unchanged."""

    name = 'progress'
    FORWARDED = frozenset({PLAN, STEP})

    def __init__(self, callback: Callable[[dict], Any]) -> None:
        if not callable(callback):
            raise TypeError('callback must be callable')
        self._callback = callback

    def observe(self, event: dict) -> None:
        if event.get('type') in self.FORWARDED:
            self._callback(event)


def summarize_arguments(arguments: dict) -> dict:
    """Host view of tool arguments: short location and bound values, otherwise only a length."""
    summary = {}
    for key, value in arguments.items() if isinstance(arguments, dict) else ():
        if not isinstance(key, str):
            continue
        if key in _SUMMARY_VALUE_KEYS and (value is None or isinstance(value, (bool, int, float))):
            summary[key] = value
        elif key in _SUMMARY_VALUE_KEYS and isinstance(value, str) and len(value) <= _SUMMARY_VALUE_CHARS:
            summary[key] = value
        elif isinstance(value, str):
            summary[key] = {'chars': len(value)}
        elif isinstance(value, (list, dict)):
            summary[key] = {'items': len(value)}
        else:
            summary[key] = {'type': type(value).__name__}
    return summary


class HookedTool:
    """A tool object whose calls pass the run's hooks. Wrapping the object, not a registry,
    keeps team members (who copy tool objects into their own registries) covered."""

    def __init__(self, tool) -> None:
        self._tool = tool
        self.definition = tool.definition

    def __getattr__(self, name):
        return getattr(self._tool, name)

    async def invoke(self, arguments: dict) -> ToolOutput:
        hooks = current_hooks()
        if not hooks.observers and not hooks.gates:
            return await self._tool.invoke(arguments)
        from app.agent.side_effects import SIDE_EFFECT_TOOLS
        name = self.definition.name
        base = {'tool': name, 'call_id': current_call_id(), 'side_effect': name in SIDE_EFFECT_TOOLS}
        hooks.emit({'type': BEFORE_TOOL, **base, 'args_summary': summarize_arguments(arguments)})
        decision, denied_by = await hooks.gate({'type': BEFORE_TOOL, **base,
                                                'args_summary': summarize_arguments(arguments)})
        if not decision.allow:
            hooks.emit({'type': AFTER_TOOL, **base, 'success': False, 'chars': 0, 'seconds': 0.0,
                        'denied_by': denied_by})
            return ToolOutput(False, '', error=f'not executed: {decision.reason}')
        started = time.monotonic()
        try:
            output = await self._tool.invoke(arguments)
        except BaseException as exc:
            hooks.emit({'type': AFTER_TOOL, **base, 'success': False, 'chars': 0,
                        'seconds': round(time.monotonic() - started, 3), 'error': type(exc).__name__})
            raise
        success = getattr(output, 'success', None)
        content = getattr(output, 'content', '')
        hooks.emit({'type': AFTER_TOOL, **base, 'success': success is True,
                    'chars': len(content) if isinstance(content, str) else 0,
                    'seconds': round(time.monotonic() - started, 3)})
        return output


class HookedToolRegistry(ToolRegistry):
    """The task's registry: every tool registered here is wrapped once in HookedTool."""

    def register(self, tool) -> None:
        super().register(tool if isinstance(tool, HookedTool) else HookedTool(tool))


class HookedModel:
    """The base model with the run's hooks around every call (planning, steps, team members,
    routing, wrap-up and repairs all reach the base model)."""

    def __init__(self, model) -> None:
        self._model = model

    def __getattr__(self, name):
        return getattr(self._model, name)

    async def generate(self, messages, tools):
        hooks = current_hooks()
        if not hooks.observers and not hooks.gates:
            return await self._model.generate(messages, tools)
        purpose = 'wrap_up' if isinstance(tools, ToolsShownOnly) else _purpose.get()
        callable_tools = 0 if isinstance(tools, ToolsShownOnly) else len(tools)
        before = {'type': BEFORE_MODEL, 'purpose': purpose, 'message_count': len(messages),
                  'tool_count': len(tools), 'callable_tools': callable_tools}
        hooks.emit(before)
        decision, denied_by = await hooks.gate(before)
        if not decision.allow:
            hooks.emit({'type': AFTER_MODEL, 'purpose': purpose, 'denied_by': denied_by, 'success': False})
            raise PolicyDeniedError(denied_by, decision.reason)
        meter = current_meter()
        logged = len(meter.call_log) if meter is not None else None
        started = time.monotonic()
        try:
            turn = await self._model.generate(messages, tools)
        except BaseException as exc:
            hooks.emit({'type': AFTER_MODEL, 'purpose': purpose, 'success': False,
                        'seconds': round(time.monotonic() - started, 3), 'error': type(exc).__name__,
                        **_usage_since(meter, logged)})
            raise
        hooks.emit({'type': AFTER_MODEL, 'purpose': purpose, 'success': True,
                    'seconds': round(time.monotonic() - started, 3),
                    'tool_calls': len(getattr(turn, 'tool_calls', ()) or ()),
                    **_usage_since(meter, logged)})
        return turn


def _usage_since(meter, logged) -> dict:
    """Provider usage of the calls this model call made (retries included), from the meter."""
    if meter is None or logged is None:
        return {'provider_calls': None, 'prompt_tokens': None, 'completion_tokens': None, 'cached_tokens': None}
    entries = meter.call_log[logged:]

    def total(key):
        values = [entry.get(key) for entry in entries if entry.get(key) is not None]
        return sum(values) if values else None
    return {'provider_calls': len(entries), 'prompt_tokens': total('prompt'),
            'completion_tokens': total('completion'), 'cached_tokens': total('cached')}
