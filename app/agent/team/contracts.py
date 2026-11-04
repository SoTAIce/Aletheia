"""Host-owned exact JSON contracts for small scalar result records."""
from dataclasses import dataclass
from collections.abc import Callable
import json


class ContractError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class JsonContract:
    name: str
    fields: tuple[tuple[str, type], ...]
    check: Callable[[dict], None] | None = None

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError('contract name must be nonempty')
        if not isinstance(self.fields, tuple) or not self.fields:
            raise ValueError('fields must be a nonempty immutable tuple')
        names = set()
        for pair in self.fields:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise ValueError('each field must be a (name, scalar type) tuple')
            name, kind = pair
            if not isinstance(name, str) or not name or name in names or kind not in (str, int, bool):
                raise ValueError('invalid or duplicate scalar field')
            names.add(name)
        if self.check is not None and not callable(self.check):
            raise TypeError('check must be callable')

    def validate(self, payload):
        if type(payload) is not dict or set(payload) != {name for name, _ in self.fields}:
            raise ContractError(f'{self.name}: exact fields required: {[n for n, _ in self.fields]}')
        for name, kind in self.fields:
            if type(payload[name]) is not kind:
                raise ContractError(f'{self.name}: {name} must be {kind.__name__}')
        if self.check is not None:
            self.check(dict(payload))

    def serialize(self, payload):
        self.validate(payload)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)

    def schema(self):
        kinds = {str: 'string', int: 'integer', bool: 'boolean'}
        return {'type': 'object', 'properties': {n: {'type': kinds[t]} for n, t in self.fields},
                'required': [n for n, _ in self.fields], 'additionalProperties': False}
