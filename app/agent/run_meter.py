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


def _non_negative_int(value):
    return value if type(value) is int and value >= 0 else None


@dataclass(slots=True)
class RunMeter:
    max_total_tokens: int | None = None
    timeout_seconds: float | None = None
    model_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls_without_usage: int = 0
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

    def record(self, usage) -> None:
        """Add one provider call; usage is the provider's usage object, or None."""
        self.model_calls += 1
        prompt = _non_negative_int(getattr(usage, 'prompt_tokens', None))
        completion = _non_negative_int(getattr(usage, 'completion_tokens', None))
        if prompt is None and completion is None:
            self.calls_without_usage += 1
            return
        self.prompt_tokens += prompt or 0
        self.completion_tokens += completion or 0

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
                 'calls_without_usage': self.calls_without_usage, 'seconds': round(self.elapsed(), 3)}
        usage['estimated_cost'] = estimated_cost(self.prompt_tokens, self.completion_tokens)
        return usage


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


def record_model_call(usage) -> None:
    meter = _current.get()
    if meter is not None:
        meter.record(usage)
