"""Host-enforced JSON Schema output contract for the final answer (D-093)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import json
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from app.agent.json_output import JsonFormatError, load_json, unwrap_json_fence

MAX_SCHEMA_CHARS = 8000
MAX_ERRORS = 5
MAX_ERROR_CHARS = 200

_CONSTRAINT_PREFIX = (
    'HOST OUTPUT CONTRACT: Your final answer must be a single JSON document that '
    'conforms to the following JSON Schema. Reply with the JSON document only, '
    'without Markdown fences, explanations, or extra text.\n\n'
)


def _json_pointer(path) -> str:
    """RFC 6901 JSON Pointer for a jsonschema error path; '/' for the document root."""
    parts = list(path)
    if not parts:
        return '/'
    return '/' + '/'.join(str(p).replace('~', '~0').replace('/', '~1') for p in parts)


def _check_refs(node: Any, resolver) -> None:
    """Reject remote http(s) $ref and any $ref that cannot resolve offline."""
    if isinstance(node, dict):
        sub_id = node.get('$id')
        if isinstance(sub_id, str) and sub_id:
            try:
                resolver = resolver.in_subresource(
                    Resource.from_contents(node, default_specification=DRAFT202012)
                )
            except Exception as exc:
                raise ValueError('output_schema contains an invalid subschema $id') from exc
        ref = node.get('$ref')
        if isinstance(ref, str):
            if ref.lower().startswith(('http://', 'https://')):
                raise ValueError('output_schema must not contain remote $ref')
            try:
                resolver.lookup(ref)
            except Exception as exc:
                raise ValueError('output_schema contains an unresolvable $ref') from exc
        for key, value in node.items():
            if key != '$ref':
                _check_refs(value, resolver)
    elif isinstance(node, list):
        for item in node:
            _check_refs(item, resolver)


@dataclass(frozen=True, slots=True)
class ContractCheck:
    """Result of checking one final answer against an OutputContract."""
    ok: bool
    value: Any = None
    text: str = ''
    errors: tuple[str, ...] = ()


class OutputContract:
    """Validated JSON Schema (Draft 2020-12) checked by the host on the final answer."""

    def __init__(self, schema: dict[str, Any]) -> None:
        if not isinstance(schema, dict):
            raise ValueError('output_schema must be a JSON object')
        try:
            serialized = json.dumps(schema, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError('output_schema must be JSON-serializable') from exc
        if len(serialized) > MAX_SCHEMA_CHARS:
            raise ValueError(f'output_schema exceeds {MAX_SCHEMA_CHARS} characters')
        # Keep an isolated copy so caller mutations cannot alter the validated schema.
        copy = json.loads(serialized)
        try:
            Draft202012Validator.check_schema(copy)
        except SchemaError as exc:
            raise ValueError('output_schema is not a valid JSON Schema (Draft 2020-12)') from exc
        # Offline registry: never retrieve remote resources over the network.
        resource = Resource.from_contents(copy, default_specification=DRAFT202012)
        registry = Registry().with_resource(resource.id() or '', resource)
        _check_refs(copy, registry.resolver_with_root(resource))
        self._schema: dict[str, Any] = copy
        self._serialized: str = serialized
        self._validator = Draft202012Validator(copy, registry=registry)

    @property
    def schema(self) -> dict[str, Any]:
        return json.loads(self._serialized)

    def constraint_text(self) -> str:
        """Host constraint added to a new task so the model knows the required schema."""
        return _CONSTRAINT_PREFIX + self._serialized

    def check(self, text: str) -> ContractCheck:
        """Parse and validate text; strips a single outer ```json fence when present."""
        unwrapped = unwrap_json_fence(text).strip()
        try:
            value = load_json(unwrapped)
        except (JsonFormatError, ValueError) as exc:
            message = f'/: {exc}'[:MAX_ERROR_CHARS]
            return ContractCheck(ok=False, errors=(message,))
        raw_errors = sorted(
            self._validator.iter_errors(value),
            key=lambda e: ([str(p) for p in e.absolute_path], e.message),
        )
        if raw_errors:
            formatted = tuple(
                f'{_json_pointer(err.absolute_path)}: {err.message}'[:MAX_ERROR_CHARS]
                for err in raw_errors[:MAX_ERRORS]
            )
            return ContractCheck(ok=False, errors=formatted)
        return ContractCheck(ok=True, value=value, text=unwrapped)

    @staticmethod
    def repair_instruction(errors: Sequence[str]) -> str:
        """One-shot host repair prompt listing up to MAX_ERRORS pointer+message items."""
        items = [str(err)[:MAX_ERROR_CHARS] for err in list(errors)[:MAX_ERRORS]]
        lines = '\n'.join(f'- {item}' for item in items)
        return (
            'HOST: your previous answer did not conform to the required JSON Schema:\n'
            f'{lines}\n'
            'Reply with a single JSON document only, with no other text or Markdown fences, '
            'and do not call any tools.'
        )
