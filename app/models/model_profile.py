"""Model profile: everything the host needs to call one chat model (stage 4d, docs/stage4d_plan.md).

One place for the endpoint, the model name, its context window and output cap, request options,
capabilities that used to be inferred from "is this DashScope" (enable_thinking, prompt_cache_key)
and prices. A profile names the setting that holds its API key (``api_key_env``) and never holds
the key itself, so a profile can be logged or shown safely.

4d-1 builds a single ``default`` profile from the existing settings (``Settings.agent_model_profile``);
requests stay byte-for-byte the same. Profile files and roles (4d-2) come later.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
import re
from typing import Any

_NAME = re.compile(r'^[A-Za-z0-9_.-]{1,64}$')
_ENV = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,127}$')


@dataclass(frozen=True, slots=True)
class ModelProfile:
    name: str
    base_url: str
    api_key_env: str                      # the setting or environment variable holding the key
    model: str
    context_tokens: int
    max_output_tokens: int
    timeout_seconds: float
    temperature: float | None = 0         # int 0, as the request always sent (keeps bytes identical)
    json_mode: bool = True
    enable_thinking: bool | None = None   # None: the parameter is not sent
    prompt_cache_key: bool = False        # send a per-conversation prompt_cache_key (D-057)
    input_price_per_million: float | None = None
    output_price_per_million: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _NAME.match(self.name):
            raise ValueError('profile name must be 1-64 letters, digits, _ . or -')
        if not isinstance(self.api_key_env, str) or not _ENV.match(self.api_key_env):
            raise ValueError('api_key_env must be a setting or environment variable name')
        for field in ('base_url', 'model'):
            if not isinstance(getattr(self, field), str):
                raise TypeError(f'{field} must be a string')
        for field in ('context_tokens', 'max_output_tokens'):
            value = getattr(self, field)
            if type(value) is not int or value <= 0:
                raise ValueError(f'{field} must be a positive integer')
        if not _finite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError('timeout_seconds must be finite and positive')
        if self.temperature is not None and (not _finite(self.temperature) or not 0 <= self.temperature <= 2):
            raise ValueError('temperature must be between 0 and 2 or None')
        if type(self.json_mode) is not bool or type(self.prompt_cache_key) is not bool:
            raise TypeError('json_mode and prompt_cache_key must be bools')
        if self.enable_thinking is not None and type(self.enable_thinking) is not bool:
            raise TypeError('enable_thinking must be a bool or None')
        for field in ('input_price_per_million', 'output_price_per_million'):
            value = getattr(self, field)
            if value is not None and (not _finite(value) or value < 0):
                raise ValueError(f'{field} must be a non-negative number or None')

    def api_key(self, settings: Any = None) -> str:
        """The key, read when needed: from the settings field of that name, else the environment."""
        field = self.api_key_env.lower()
        if settings is not None and field in type(settings).model_fields:
            return str(getattr(settings, field) or '').strip()
        return os.environ.get(self.api_key_env, '').strip()

    def estimated_cost(self, prompt_tokens: int, completion_tokens: int) -> float | None:
        """Cost in the configured currency, or None when either price is missing (D-034)."""
        if self.input_price_per_million is None or self.output_price_per_million is None:
            return None
        return round((prompt_tokens * self.input_price_per_million
                      + completion_tokens * self.output_price_per_million) / 1_000_000, 6)


def _finite(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
