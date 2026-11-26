"""Model profile: everything the host needs to call one chat model (stage 4d, docs/stage4d_plan.md).

One place for the endpoint, the model name, its context window and output cap, request options,
capabilities that used to be inferred from "is this DashScope" (enable_thinking, prompt_cache_key)
and prices. A profile names the setting that holds its API key (``api_key_env``) and never holds
the key itself, so a profile can be logged or shown safely.

4d-1 builds a single ``default`` profile from the existing settings (``Settings.agent_model_profile``);
requests stay byte-for-byte the same. 4d-2 adds an optional TOML profile file and roles
(``ModelProfiles``, ``load_profiles``; plan section 10): main, route, member and explorer, each
falling back to main. Without a file every role uses the ``default`` profile.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
import math
import os
from pathlib import Path
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


ROLES = ('main', 'route', 'member', 'explorer')
OPTIONAL_ROLES = ('summarizer',)
# Taken from the default profile when a profile in the file leaves them out. Capabilities and prices
# belong to one model and provider, so they are never inherited (plan section 10.1).
INHERITED = ('context_tokens', 'max_output_tokens', 'timeout_seconds', 'temperature', 'json_mode')
REQUIRED = ('base_url', 'api_key_env', 'model')


class ModelProfileError(ValueError):
    """A profile file the host refuses at start-up; the message never holds a key."""


@dataclass(frozen=True, slots=True)
class ModelProfiles:
    """Profiles by name and the profile of each role (stage 4d-2)."""
    profiles: dict
    roles: dict
    summarizer: str | None = None

    def __post_init__(self) -> None:
        if set(self.roles) != set(ROLES):
            raise ValueError(f'roles must be exactly {ROLES}')
        for role, name in self.roles.items():
            if name not in self.profiles:
                raise ValueError(f'role {role} names an unknown profile')
        if self.summarizer is not None and (not isinstance(self.summarizer, str) or self.summarizer not in self.profiles):
            raise ValueError('role summarizer names an unknown profile')

    @classmethod
    def single(cls, profile: ModelProfile) -> 'ModelProfiles':
        """Every role on one profile: the settings without a profile file."""
        return cls({profile.name: profile}, dict.fromkeys(ROLES, profile.name))

    def role(self, role: str) -> ModelProfile:
        return self.profiles[self.roles[role]]

    def summarizer_profile(self) -> ModelProfile | None:
        return None if self.summarizer is None else self.profiles[self.summarizer]

    def used(self) -> tuple[ModelProfile, ...]:
        """The profiles some role uses, main first, each once."""
        return tuple(self.profiles[name] for name in dict.fromkeys(self.roles[r] for r in ROLES))

    def describe(self) -> list[tuple[str, str, str, int]]:
        """(role, profile, model, context tokens) per role: names and numbers only, never a key."""
        return [(role, p.name, p.model, p.context_tokens) for role in ROLES for p in (self.role(role),)]


_FIELDS = {f.name for f in fields(ModelProfile)} - {'name'}


def load_profiles(path, default: ModelProfile, settings: Any = None) -> ModelProfiles:
    """Read a profile file (plan section 10.1). Every error is a ModelProfileError in Chinese."""
    import tomllib
    source = Path(path)
    try:
        data = tomllib.loads(source.read_text(encoding='utf-8'))
    except FileNotFoundError:
        raise ModelProfileError(f'找不到模型档案文件 {source}') from None
    except (OSError, UnicodeDecodeError) as exc:
        raise ModelProfileError(f'读不了模型档案文件 {source}（{type(exc).__name__}）') from None
    except tomllib.TOMLDecodeError as exc:
        raise ModelProfileError(f'模型档案文件 {source} 不是合法的 TOML：{exc}') from None
    unknown = set(data) - {'profiles', 'roles'}
    if unknown:
        raise ModelProfileError(f'模型档案文件里有不认识的段：{", ".join(sorted(unknown))}（只认 profiles 和 roles）')
    raw_profiles, raw_roles = data.get('profiles'), data.get('roles')
    if not isinstance(raw_profiles, dict) or not raw_profiles:
        raise ModelProfileError('模型档案文件至少要有一个 [profiles.<名字>]')
    if not isinstance(raw_roles, dict):
        raise ModelProfileError('模型档案文件缺少 [roles] 段')
    profiles = {}
    for name, body in raw_profiles.items():
        if not isinstance(body, dict):
            raise ModelProfileError(f'档案 {name!r} 必须是一个表（[profiles.{name}]）')
        extra = set(body) - _FIELDS
        if extra:
            raise ModelProfileError(f'档案 {name!r} 有不认识的字段：{", ".join(sorted(extra))}')
        missing = [field for field in REQUIRED if field not in body]
        if missing:
            raise ModelProfileError(f'档案 {name!r} 必须写明：{", ".join(missing)}')
        values = {field: getattr(default, field) for field in INHERITED}
        values.update(body)
        try:
            profiles[name] = ModelProfile(name=name, **values)
        except (TypeError, ValueError) as exc:
            raise ModelProfileError(f'档案 {name!r} 的取值不对：{exc}') from None
    all_roles = ROLES + OPTIONAL_ROLES
    extra = set(raw_roles) - set(all_roles)
    if extra:
        raise ModelProfileError(f'[roles] 里有不认识的角色：{", ".join(sorted(extra))}（只认 {", ".join(all_roles)}）')
    if 'main' not in raw_roles:
        raise ModelProfileError('[roles] 必须写 main')
    roles = {}
    for role in ROLES:
        name = raw_roles.get(role, raw_roles['main'])
        if not isinstance(name, str) or name not in profiles:
            raise ModelProfileError(f'角色 {role} 指向不存在的档案 {name!r}')
        roles[role] = name
    summarizer = None
    if 'summarizer' in raw_roles:
        name = raw_roles['summarizer']
        if not isinstance(name, str) or name not in profiles:
            raise ModelProfileError(f'角色 summarizer 指向不存在的档案 {name!r}')
        summarizer = name
    result = ModelProfiles(profiles, roles, summarizer=summarizer)
    used_profiles = result.used()
    for profile in used_profiles:
        if not profile.api_key(settings):
            raise ModelProfileError(f'档案 {profile.name!r} 的密钥变量 {profile.api_key_env} 是空的')
    summary = result.summarizer_profile()
    if summary is not None and summary not in used_profiles:
        if not summary.api_key(settings):
            raise ModelProfileError(f'档案 {summary.name!r} 的密钥变量 {summary.api_key_env} 是空的')
    return result
