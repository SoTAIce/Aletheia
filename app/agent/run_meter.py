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
import threading
import time

from app.config import config


MAX_CALL_LOG = 200


def _non_negative_int(value):
    return value if type(value) is int and value >= 0 else None


_MODEL_NAME_CHARS = 100


def _model_name(value):
    """A provider's model name as a short label: printable ASCII only, or None."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > _MODEL_NAME_CHARS or not all(' ' < ch <= '~' for ch in value):
        return None
    return value


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
    # The model name each logged call's response reported (None = not reported), kept apart from
    # call_log so usage snapshots and reports stay as they were; read by the trace (D-073).
    response_models: list = field(default_factory=list)
    # Per logged call, the host's own estimate of the request's prompt tokens and the model name it
    # asked for; with the provider's report they give the provider state (provider-state-001).
    host_estimates: list = field(default_factory=list)
    requested_models: list = field(default_factory=list)
    # Usage per model profile (stage 4d-2): name -> counts, and name -> the ModelProfile for its
    # prices. Calls of a model built without a profile are not booked here.
    profile_usage: dict = field(default_factory=dict)
    profile_objects: dict = field(default_factory=dict)
    # Usage per purpose (docs/cost_measurement_plan.md): the label the call ran under (step, route,
    # wrap_up, lead, member, reviewer, explorer) -> counts and the cost at that call's own prices.
    purpose_usage: dict = field(default_factory=dict)
    # Calls outside the agent model during this request (query rewrite, embeddings): purpose ->
    # counts. They are reported only; they never count toward the token budget.
    auxiliary: dict = field(default_factory=dict)
    # What the loops did about going round in circles (D-092): kind (reminder, wrap_up,
    # unchanged_read) -> count, for every loop of the run, members included.
    stall: dict = field(default_factory=dict)
    # Output schema contract repairs (D-093): None when no contract was active, else >= 0.
    contract_repairs: int | None = None
    # Permission rule counts (D-094): None when no permission rules were active, else {'denied', 'asked'}.
    permissions: dict | None = None
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

    def record(self, usage, shared_prefix: float | None = None, model: str | None = None,
               host_estimate: int | None = None, requested_model: str | None = None,
               profile=None, purpose: str | None = None) -> None:
        """Add one provider call; usage is the provider's usage object, or None; model is the
        model name the response reported; host_estimate and requested_model describe what the
        host sent (provider-state-001); profile is the ModelProfile of the model (stage 4d-2);
        purpose is the label the call ran under, by default the current one (cost-measurement)."""
        self.model_calls += 1
        name = getattr(profile, 'name', None)
        if not isinstance(name, str):
            name = None
        prompt = _non_negative_int(getattr(usage, 'prompt_tokens', None))
        completion = _non_negative_int(getattr(usage, 'completion_tokens', None))
        cached = None if prompt is None and completion is None else _cached_tokens(usage)
        if cached is not None and prompt is not None:
            cached = min(cached, prompt)
        purpose = _purpose_label(purpose)
        self._book_purpose(purpose, prompt, completion, cached, profile if name is not None else None)
        if len(self.call_log) < MAX_CALL_LOG:
            share = shared_prefix if isinstance(shared_prefix, float) and 0 <= shared_prefix <= 1 else None
            self.call_log.append({'prompt': prompt, 'cached': cached, 'completion': completion,
                                  'shared_prefix': None if share is None else round(share, 3),
                                  'purpose': purpose})
            if name is not None:
                self.call_log[-1]['profile'] = name
            self.response_models.append(_model_name(model))
            self.host_estimates.append(host_estimate if type(host_estimate) is int and host_estimate > 0 else None)
            self.requested_models.append(_model_name(requested_model))
        booked = None
        if name is not None:
            self.profile_objects.setdefault(name, profile)
            booked = self.profile_usage.setdefault(name, {'model_calls': 0, 'prompt_tokens': 0,
                                                          'completion_tokens': 0, 'cached_prompt_tokens': 0})
            booked['model_calls'] += 1
        if prompt is None and completion is None:
            self.calls_without_usage += 1
            return
        self.prompt_tokens += prompt or 0
        self.completion_tokens += completion or 0
        if cached is not None:
            self.calls_with_cache_report += 1
            self.cached_prompt_tokens += cached
        if booked is not None:
            booked['prompt_tokens'] += prompt or 0
            booked['completion_tokens'] += completion or 0
            booked['cached_prompt_tokens'] += cached or 0

    def _book_purpose(self, purpose, prompt, completion, cached, profile) -> None:
        booked = self.purpose_usage.setdefault(purpose, {
            'model_calls': 0, 'prompt_tokens': 0, 'completion_tokens': 0, 'cached_prompt_tokens': 0,
            'estimated_cost': 0.0})
        booked['model_calls'] += 1
        if prompt is None and completion is None:
            return
        booked['prompt_tokens'] += prompt or 0
        booked['completion_tokens'] += completion or 0
        booked['cached_prompt_tokens'] += cached or 0
        if booked['estimated_cost'] is not None:
            # This call at its own profile's prices; one unpriced call makes the purpose's cost unknown.
            cost = (profile.estimated_cost(prompt or 0, completion or 0) if profile is not None
                    else estimated_cost(prompt or 0, completion or 0))
            booked['estimated_cost'] = None if cost is None else booked['estimated_cost'] + cost

    def add_auxiliary(self, purpose, prompt=None, completion=None) -> None:
        """One call outside the agent model made for this request; reported, never budgeted."""
        _book_auxiliary(self.auxiliary, purpose, prompt, completion)

    def count_stall(self, kind: str) -> None:
        self.stall[kind] = self.stall.get(kind, 0) + 1

    def record_contract(self, *, repaired: bool = False) -> None:
        if self.contract_repairs is None:
            self.contract_repairs = 0
        if repaired:
            self.contract_repairs += 1

    def enable_permissions(self) -> None:
        if self.permissions is None:
            self.permissions = {'denied': 0, 'asked': 0}

    def record_permission(self, *, denied: bool = False, asked: bool = False) -> None:
        if self.permissions is None:
            self.permissions = {'denied': 0, 'asked': 0}
        if denied:
            self.permissions['denied'] += 1
        if asked:
            self.permissions['asked'] += 1

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
        usage['estimated_cost'] = self.estimated_cost()
        if self.profile_usage:
            usage['by_profile'] = {
                name: {**counts, 'estimated_cost': self.profile_objects[name].estimated_cost(
                    counts['prompt_tokens'], counts['completion_tokens'])}
                for name, counts in self.profile_usage.items()}
        usage['provider'] = self.provider_summary()
        if self.purpose_usage:
            usage['by_purpose'] = _rounded(self.purpose_usage)
        if self.auxiliary:
            usage['auxiliary'] = _rounded(self.auxiliary)
        if self.stall:
            usage['stall'] = dict(self.stall)
        if self.contract_repairs is not None:
            usage['contract'] = {'repairs': self.contract_repairs}
        if self.permissions is not None:
            usage['permissions'] = dict(self.permissions)
        return usage

    def estimated_cost(self) -> float | None:
        """Each profile's tokens at its own prices, summed; None when any used profile has no price.
        Tokens of calls without a profile are priced as before, with the agent model's profile.
        With only the default profile this is the old figure (stage 4d-2)."""
        prompt, completion, total = self.prompt_tokens, self.completion_tokens, 0.0
        for name, counts in self.profile_usage.items():
            cost = self.profile_objects[name].estimated_cost(counts['prompt_tokens'], counts['completion_tokens'])
            if cost is None:
                return None
            total += cost
            prompt -= counts['prompt_tokens']
            completion -= counts['completion_tokens']
        if not self.profile_usage:
            return estimated_cost(prompt, completion)
        if prompt or completion:
            rest = estimated_cost(prompt, completion)
            if rest is None:
                return None
            total += rest
        return round(total, 6)

    def provider_summary(self) -> dict:
        """The provider state of this run's logged calls (provider-state-001)."""
        from app.agent.provider_state import ratio, summary
        ratios = [ratio(entry.get('prompt'), estimate)
                  for entry, estimate in zip(self.call_log, self.host_estimates)]
        return summary(ratios, self.requested_models, self.response_models)


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
    """Cost in the configured currency, or None when no price is configured; prices come
    from the agent model's profile (stage 4d-1)."""
    return config.agent_model_profile.estimated_cost(prompt_tokens, completion_tokens)


def _purpose_label(purpose) -> str:
    if not isinstance(purpose, str) or not purpose:
        from app.agent.hooks import current_purpose
        purpose = current_purpose()
    return purpose[:40]


def _rounded(table: dict) -> dict:
    with _AUX_LOCK:
        return {name: {k: (round(v, 6) if isinstance(v, float) else v) for k, v in counts.items()}
                for name, counts in table.items()}


_AUX_LOCK = threading.Lock()


def _book_auxiliary(table: dict, purpose, prompt, completion) -> None:
    prompt, completion = _non_negative_int(prompt), _non_negative_int(completion)
    with _AUX_LOCK:
        booked = table.setdefault(str(purpose)[:40], {'calls': 0, 'calls_without_usage': 0,
                                                      'prompt_tokens': 0, 'completion_tokens': 0})
        booked['calls'] += 1
        if prompt is None and completion is None:
            booked['calls_without_usage'] += 1
            return
        booked['prompt_tokens'] += prompt or 0
        booked['completion_tokens'] += completion or 0


class AuxiliaryLedger:
    """Calls outside the agent model made outside any request (background summaries, memory
    writes): purpose -> counts, for the whole process. These models have no configured prices,
    so only calls and tokens are kept (docs/cost_measurement_plan.md)."""

    def __init__(self) -> None:
        self._table: dict = {}

    def add(self, purpose, prompt=None, completion=None) -> None:
        _book_auxiliary(self._table, purpose, prompt, completion)

    def snapshot(self) -> dict:
        return _rounded(self._table)

    def clear(self) -> None:
        with _AUX_LOCK:
            self._table.clear()


LEDGER = AuxiliaryLedger()


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


def record_auxiliary(purpose: str, prompt=None, completion=None, *, in_request: bool = True) -> None:
    """Book a call outside the agent model (query rewrite, embeddings, summaries): to the open
    request's meter when there is one and in_request, else to the process ledger."""
    meter = _current.get() if in_request else None
    if meter is not None:
        meter.add_auxiliary(purpose, prompt, completion)
    else:
        LEDGER.add(purpose, prompt, completion)


def usage_tokens(usage) -> tuple:
    """(prompt, completion) from an OpenAI-style or LangChain usage object or dict; None where
    not reported. An embedding reports only prompt (or total) tokens."""
    def read(*names):
        for name in names:
            value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
            if _non_negative_int(value) is not None:
                return value
        return None
    if usage is None:
        return None, None
    return read('prompt_tokens', 'input_tokens', 'total_tokens'), read('completion_tokens', 'output_tokens')


def record_model_call(usage, shared_prefix: float | None = None, model: str | None = None,
                      host_estimate: int | None = None, requested_model: str | None = None,
                      profile=None) -> None:
    meter = _current.get()
    if meter is not None:
        meter.record(usage, shared_prefix, model, host_estimate, requested_model, profile)
