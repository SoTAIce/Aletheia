"""Model usage and the token/time budget of one request (DECISIONS D-034).

The caller opens a meter around a request (`with metering(RunMeter(...))`); every model call made
inside it, in this task or a task it starts, adds its usage. The runtime reads the active meter to
stop at the budget between model calls, so a budget can be exceeded by at most one call. Without
an open meter the runtime opens an unlimited one for each run, so usage is still reported.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import math
import time

from app.config import config


MAX_CALL_LOG = 200


def _non_negative_int(value):
    return value if type(value) is int and value >= 0 else None


def _cached_tokens(usage):
    """Cached prompt tokens as OpenAI-compatible providers report them
    (usage.prompt_tokens_details.cached_tokens), or None when not reported."""
    details = getattr(usage, 'prompt_tokens_details', None)
    if isinstance(details, dict):
        return _non_negative_int(details.get('cached_tokens'))
    return _non_negative_int(getattr(details, 'cached_tokens', None))


@dataclass(slots=True)
class RunMeter:
    max_total_tokens: int | None = None
    timeout_seconds: float | None = None
    model_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls_without_usage: int = 0
    # Prompt tokens the provider served from its prompt cache (part of prompt_tokens), and how
    # many calls reported that figure at all; 0 hits and "not reported" differ (D-054).
    cached_prompt_tokens: int = 0
    calls_with_cache_report: int = 0
    # One entry per call, in order, to see which calls missed the cache (D-056): prompt,
    # cached (None = not reported), completion, and how much of the previous request to the
    # same model this one starts with unchanged (shared_prefix, 0..1; None = unknown).
    call_log: list = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)

    def __post_init__(self):
        if self.max_total_tokens is not None and (type(self.max_total_tokens) is not int or self.max_total_tokens < 1):
            raise ValueError('max_total_tokens must be a positive integer')
        if self.timeout_seconds is not None and (
                isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (int, float))
                or not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0):
            raise ValueError('timeout_seconds must be finite and positive')

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def record(self, usage, shared_prefix: float | None = None) -> None:
        """Add one provider call; usage is the provider's usage object, or None."""
        self.model_calls += 1
        prompt = _non_negative_int(getattr(usage, 'prompt_tokens', None))
        completion = _non_negative_int(getattr(usage, 'completion_tokens', None))
        cached = None if prompt is None and completion is None else _cached_tokens(usage)
        if cached is not None and prompt is not None:
            cached = min(cached, prompt)
        if len(self.call_log) < MAX_CALL_LOG:
            share = shared_prefix if isinstance(shared_prefix, float) and 0 <= shared_prefix <= 1 else None
            self.call_log.append({'prompt': prompt, 'cached': cached, 'completion': completion,
                                  'shared_prefix': None if share is None else round(share, 3)})
        if prompt is None and completion is None:
            self.calls_without_usage += 1
            return
        self.prompt_tokens += prompt or 0
        self.completion_tokens += completion or 0
        if cached is not None:
            self.calls_with_cache_report += 1
            self.cached_prompt_tokens += cached

    def tag_calls_since(self, index: int, requested: list[str]) -> None:
        """Mark the calls logged from index on with the tool names the model asked for in
        reply (host-checked names only, loop-trial-001), to see where calls go."""
        for entry in self.call_log[index:]:
            entry['requested'] = list(requested)

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def remaining_seconds(self) -> float | None:
        return None if self.timeout_seconds is None else self.timeout_seconds - self.elapsed()

    def budget_problem(self) -> str | None:
        """Host-authored reason the budget is spent, or None."""
        if self.max_total_tokens is not None and self.total_tokens >= self.max_total_tokens:
            return f'Agent token budget reached ({self.total_tokens} of {self.max_total_tokens} tokens)'
        remaining = self.remaining_seconds()
        if remaining is not None and remaining <= 0:
            return f'Agent time budget reached ({self.timeout_seconds:g} seconds)'
        return None

    def bound(self, seconds: float) -> float:
        """A per-call deadline that also ends with the time budget (at least a moment)."""
        remaining = self.remaining_seconds()
        return seconds if remaining is None else max(0.001, min(seconds, remaining))

    def snapshot(self) -> dict:
        usage = {'model_calls': self.model_calls, 'prompt_tokens': self.prompt_tokens,
                 'completion_tokens': self.completion_tokens, 'total_tokens': self.total_tokens,
                 'calls_without_usage': self.calls_without_usage,
                 'cached_prompt_tokens': self.cached_prompt_tokens,
                 'calls_with_cache_report': self.calls_with_cache_report,
                 'seconds': round(self.elapsed(), 3),
                 'calls': [dict(entry) for entry in self.call_log]}
        usage['estimated_cost'] = estimated_cost(self.prompt_tokens, self.completion_tokens)
        return usage


def cache_text(usage: dict) -> str:
    """Log text for the cache figures of a usage snapshot (D-054)."""
    reported, calls = usage.get('calls_with_cache_report', 0), usage.get('model_calls', 0)
    if not reported:
        return 'not reported'
    prompt, cached = usage.get('prompt_tokens', 0), usage.get('cached_prompt_tokens', 0)
    share = f'{cached * 100 // prompt}%' if prompt else '0%'
    return f'{cached} ({share} of prompt; reported by {reported}/{calls} calls)'


def call_text(entry: dict) -> str:
    """Log text for one call of a usage snapshot (D-056)."""
    cached = 'not reported' if entry.get('cached') is None else entry['cached']
    share = entry.get('shared_prefix')
    same = 'unknown' if share is None else f'{int(share * 100)}%'
    return f"prompt {entry.get('prompt')}, cached {cached}, previous request kept {same}"


def estimated_cost(prompt_tokens: int, completion_tokens: int) -> float | None:
    """Cost in the configured currency, or None when no price is configured."""
    prices = (config.model_input_price_per_million_tokens, config.model_output_price_per_million_tokens)
    if any(price is None for price in prices):
        return None
    return round((prompt_tokens * prices[0] + completion_tokens * prices[1]) / 1_000_000, 6)


_current: ContextVar[RunMeter | None] = ContextVar('aletheia_run_meter', default=None)


def current_meter() -> RunMeter | None:
    return _current.get()


@contextmanager
def metering(meter: RunMeter):
    token = _current.set(meter)
    try:
        yield meter
    finally:
        _current.reset(token)


def record_model_call(usage, shared_prefix: float | None = None) -> None:
    meter = _current.get()
    if meter is not None:
        meter.record(usage, shared_prefix)
