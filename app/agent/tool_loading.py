"""On-demand loading of deferred user MCP tools: stable tool list + volatile end-of-request definitions (D-099).

When a task has more than DEFER_THRESHOLD user MCP tools, those tools are omitted from the
fixed tool list and replaced by two fixed tools:
- find_tools: host tool that searches deferred tools (BM25 over name, description, server
  with Chinese bigrams) and loads matches into the task's loaded set;
- call_tool: loop alias that validates a call against the loaded tool's JSON Schema and
  dispatches the real tool name through the same ToolRegistry.

Loaded tool definitions are rendered on every model request as a trailing SystemMessage
(the volatile zone) and are never appended to the message history, keeping the prompt prefix
and tool list byte-for-byte identical across the task for provider prompt caching.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import json
import re
from typing import Any

from jsonschema import Draft202012Validator
from rank_bm25 import BM25Okapi
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from app.agent.hooks import current_hooks, current_span
from app.agent.step_models import ToolCall, ToolDefinition
from app.agent.tool_effects import effect_of, has_side_effect
from app.services.keyword_retriever import tokenize_for_bm25
from app.tools.mcp_tool_adapter import UserMCPToolAdapter
from app.tools.tool_registry import ToolRegistry

DEFER_THRESHOLD = 10
MAX_LOADED_TOOLS = 10
MAX_VOLATILE_CHARS = 8000
MAX_FIND_RESULTS = 5

FIND_TOOLS = 'find_tools'
CALL_TOOL = 'call_tool'
TOOL_SEARCH_EVENT = 'tool_search'

MAX_SCHEMA_ERRORS = 5
MAX_SCHEMA_ERROR_CHARS = 200

_VOLATILE_HEADER = 'HOST: Loaded tools (call them with call_tool):'
_CJK_RUN = re.compile(r'[\u3400-\u9fff]{2,}')


def _unwrap_tool(tool: Any) -> Any:
    """Follow HookedTool / wrapper chains to the underlying tool object."""
    seen: set[int] = set()
    while tool is not None and id(tool) not in seen:
        seen.add(id(tool))
        inner = getattr(tool, '_tool', None)
        if inner is None:
            inner = getattr(tool, 'tool', None)
        if inner is None:
            break
        tool = inner
    return tool


def is_user_mcp_tool(tool: Any) -> bool:
    """True when the registered tool (possibly wrapped by HookedTool) is a user MCP tool."""
    inner = _unwrap_tool(tool)
    return isinstance(inner, UserMCPToolAdapter) or getattr(inner, 'is_user_mcp_tool', False) is True


def _server_name_for(tool: Any, tool_name: str) -> str:
    inner = _unwrap_tool(tool)
    explicit = getattr(inner, 'server_name', None)
    if isinstance(explicit, str) and explicit:
        return explicit
    return tool_name.split('__', 1)[0]


def tokenize_tool_text(text: str) -> list[str]:
    """Tokenize tool metadata for BM25: reuse tokenize_for_bm25 and add Chinese bigrams."""
    tokens = tokenize_for_bm25(text)
    for run in _CJK_RUN.findall(text or ''):
        for index in range(len(run) - 1):
            tokens.append(run[index:index + 2])
    return tokens


def _json_pointer(path) -> str:
    parts = list(path)
    if not parts:
        return '/'
    return '/' + '/'.join(str(p).replace('~', '~0').replace('/', '~1') for p in parts)


def _offline_validator(schema: dict[str, Any]) -> Draft202012Validator:
    """Build a Draft 2020-12 validator that never resolves remote $ref over the network."""
    try:
        copy = json.loads(json.dumps(schema, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError):
        copy = {'type': 'object'}
    if not isinstance(copy, dict):
        copy = {'type': 'object'}
    resource = Resource.from_contents(copy, default_specification=DRAFT202012)
    registry = Registry().with_resource(resource.id() or '', resource)
    return Draft202012Validator(copy, registry=registry)


def _format_tool_block(definition: ToolDefinition) -> str:
    schema_json = json.dumps(definition.input_schema, ensure_ascii=False, sort_keys=True)
    return (
        f'Tool: {definition.name}\n'
        f'Effect: {effect_of(definition)}\n'
        f'Description: {definition.description}\n'
        f'Parameters: {schema_json}'
    )


class LoadedTools:
    """Ordered set of deferred tool names loaded in one task; survives resume/rebind on RunMemory."""

    def __init__(self, names: Sequence[str] = ()) -> None:
        self._names: list[str] = []
        for name in names:
            self.add(name)

    def add(self, name: str) -> bool:
        if name in self._names:
            return False
        self._names.append(name)
        return True

    def names(self) -> tuple[str, ...]:
        return tuple(self._names)

    def __contains__(self, name: object) -> bool:
        return name in self._names

    def __len__(self) -> int:
        return len(self._names)

    def __iter__(self):
        return iter(self._names)


@dataclass(frozen=True, slots=True)
class _DeferredEntry:
    definition: ToolDefinition
    server: str
    effect: str
    tokens: tuple[str, ...]
    block: str


class ToolLoading:
    """Deferred user MCP tool catalog, BM25 search, volatile prompt section and call_tool validation."""

    def __init__(
        self,
        entries: Sequence[_DeferredEntry],
        loaded: LoadedTools | None = None,
        *,
        max_loaded: int = MAX_LOADED_TOOLS,
        max_volatile_chars: int = MAX_VOLATILE_CHARS,
        max_find_results: int = MAX_FIND_RESULTS,
    ) -> None:
        self._entries: tuple[_DeferredEntry, ...] = tuple(entries)
        self._by_name: dict[str, _DeferredEntry] = {e.definition.name: e for e in self._entries}
        self._deferred_names: frozenset[str] = frozenset(self._by_name)
        self._loaded: LoadedTools = loaded if loaded is not None else LoadedTools()
        self._max_loaded = max_loaded
        self._max_volatile_chars = max_volatile_chars
        self._max_find_results = max_find_results
        self._validators: dict[str, Draft202012Validator] = {}

        counts: dict[str, int] = {}
        for entry in self._entries:
            counts[entry.server] = counts.get(entry.server, 0) + 1
        self._server_counts: dict[str, int] = dict(sorted(counts.items()))

        self._bm25 = BM25Okapi([list(e.tokens) for e in self._entries]) if self._entries else None

        self.find_tools_definition = ToolDefinition(
            FIND_TOOLS,
            'Search deferred user MCP tools by keywords (and optionally server name) and load '
            'the top matches. Full parameter schemas for loaded tools appear at the end of each '
            'request; invoke loaded tools with call_tool.',
            {
                'type': 'object',
                'properties': {
                    'query': {'type': 'string', 'minLength': 1, 'description': 'Keywords describing the tool needed'},
                    'server': {'type': 'string', 'minLength': 1, 'description': 'Optional server name to filter by'},
                },
                'required': ['query'],
                'additionalProperties': False,
            },
            effect='read',
        )
        self.call_tool_definition = ToolDefinition(
            CALL_TOOL,
            'Invoke a deferred user MCP tool that has already been loaded via find_tools. '
            'Pass the exact tool name and a JSON object matching its schema shown at the end of this request.',
            {
                'type': 'object',
                'properties': {
                    'name': {'type': 'string', 'minLength': 1, 'description': 'Exact name of a loaded deferred tool'},
                    'arguments': {'type': 'object', 'description': 'Arguments object matching the tool input schema'},
                },
                'required': ['name', 'arguments'],
                'additionalProperties': False,
            },
            effect='external',
        )

    @classmethod
    def for_registry(
        cls,
        registry: ToolRegistry,
        loaded: LoadedTools | None = None,
        *,
        threshold: int = DEFER_THRESHOLD,
        read_only: bool = False,
        max_loaded: int = MAX_LOADED_TOOLS,
        max_volatile_chars: int = MAX_VOLATILE_CHARS,
        max_find_results: int = MAX_FIND_RESULTS,
    ) -> 'ToolLoading | None':
        """Return a ToolLoading controller when the task registry has more than threshold user MCP tools."""
        entries: list[_DeferredEntry] = []
        for definition in registry.list_definitions():
            tool = registry.get(definition.name)
            if not is_user_mcp_tool(tool):
                continue
            effect = effect_of(definition)
            if read_only and has_side_effect(effect):
                continue
            server = _server_name_for(tool, definition.name)
            search_text = f'{definition.name} {server} {definition.description}'
            entries.append(_DeferredEntry(
                definition=definition,
                server=server,
                effect=effect,
                tokens=tuple(tokenize_tool_text(search_text)),
                block=_format_tool_block(definition),
            ))
        if len(entries) <= threshold:
            return None
        return cls(
            entries,
            loaded,
            max_loaded=max_loaded,
            max_volatile_chars=max_volatile_chars,
            max_find_results=max_find_results,
        )

    @property
    def deferred_names(self) -> frozenset[str]:
        return self._deferred_names

    @property
    def loaded_tools(self) -> LoadedTools:
        return self._loaded

    def system_note(self) -> str:
        servers = ', '.join(f'{server} ({count})' for server, count in self._server_counts.items())
        return (
            f'{len(self._entries)} more tools are available from these servers: {servers}. '
            f'Find them with {FIND_TOOLS}, then call them with {CALL_TOOL}.'
        )

    def _active_loaded_names(self) -> list[str]:
        return [name for name in self._loaded if name in self._by_name]

    def _render_volatile(self, names: Sequence[str]) -> str:
        blocks = [self._by_name[name].block for name in names]
        return _VOLATILE_HEADER + '\n\n' + '\n\n'.join(blocks)

    def volatile_message(self) -> str | None:
        active = self._active_loaded_names()
        if not active:
            return None
        return self._render_volatile(active)

    def _search(self, query: str, server: str | None) -> list[_DeferredEntry]:
        query_tokens = tokenize_tool_text(query)
        if not query_tokens or self._bm25 is None:
            return []
        query_set = set(query_tokens)
        bigram_set = {tok for tok in query_set if len(tok) == 2 and not tok.isascii()}
        scores = self._bm25.get_scores(query_tokens)
        scored: list[tuple[float, int, int, int, _DeferredEntry]] = []
        for idx, entry in enumerate(self._entries):
            if server is not None and entry.server != server:
                continue
            entry_set = set(entry.tokens)
            overlap = entry_set & query_set
            if not overlap:
                continue
            bigram_hits = len(overlap & bigram_set) if bigram_set else 0
            bm25_score = max(0.0, float(scores[idx]))
            scored.append((bm25_score, bigram_hits, len(overlap), -idx, entry))
        scored.sort(key=lambda item: (-item[0], -item[1], -item[2], -item[3]))
        return [item[4] for item in scored[:self._max_find_results]]

    @staticmethod
    def _emit_search(found: int, loaded: int, skipped: int) -> None:
        current_hooks().emit({
            'type': TOOL_SEARCH_EVENT,
            'found': found,
            'loaded': loaded,
            'skipped': skipped,
            'parent_id': current_span(),
        })

    async def find_tools(self, arguments: dict[str, Any]):
        """Host tool handler for find_tools: search, auto-load within caps, emit tool_search."""
        from app.agent.agent_loop import HostToolResult

        query = arguments.get('query') if isinstance(arguments, dict) else None
        server = arguments.get('server') if isinstance(arguments, dict) else None
        if not isinstance(query, str) or not query.strip():
            self._emit_search(0, 0, 0)
            payload = {
                'loaded': [],
                'skipped_by_limit': 0,
                'note': 'Provide a non-empty query string to search available tools.',
            }
            return HostToolResult(json.dumps(payload, ensure_ascii=False))
        if server is not None:
            if not isinstance(server, str) or not server.strip():
                server = None
            else:
                server = server.strip()

        matches = self._search(query.strip(), server)
        active = self._active_loaded_names()
        returned: list[dict[str, str]] = []
        newly_loaded = 0
        skipped = 0

        for entry in matches:
            name = entry.definition.name
            if name in active:
                returned.append({
                    'name': name,
                    'description': entry.definition.description,
                    'effect': entry.effect,
                })
                continue
            if len(active) + 1 > self._max_loaded:
                skipped += 1
                continue
            candidate_names = [*active, name]
            if len(self._render_volatile(candidate_names)) > self._max_volatile_chars:
                skipped += 1
                continue
            self._loaded.add(name)
            active.append(name)
            newly_loaded += 1
            returned.append({
                'name': name,
                'description': entry.definition.description,
                'effect': entry.effect,
            })

        if not matches:
            note = 'No matching tools found. Try different keywords or pass the server parameter.'
        elif skipped > 0:
            note = (
                'Loaded tool definitions appear at the end of this request; call them with call_tool. '
                'Some matches were skipped due to the loaded tool or size limit; try a more specific query.'
            )
        else:
            note = 'Loaded tool definitions appear at the end of this request; call them with call_tool.'

        self._emit_search(len(matches), newly_loaded, skipped)
        payload = {'loaded': returned, 'skipped_by_limit': skipped, 'note': note}
        return HostToolResult(json.dumps(payload, ensure_ascii=False))

    def _validator_for(self, name: str) -> Draft202012Validator:
        validator = self._validators.get(name)
        if validator is None:
            validator = _offline_validator(self._by_name[name].definition.input_schema)
            self._validators[name] = validator
        return validator

    def resolve_call(self, call: ToolCall) -> tuple[ToolCall | None, str | None]:
        """Validate a call_tool invocation and rewrite it to a real ToolCall, or return an error."""
        name = call.arguments.get('name')
        if not isinstance(name, str) or not name.strip():
            return None, 'HOST: call_tool requires a non-empty tool name.'
        name = name.strip()
        if name not in self._by_name:
            return None, (
                f"HOST: '{name}' is not an available deferred tool. "
                f'Call built-in tools directly, or use {FIND_TOOLS} to search deferred tools.'
            )
        if name not in self._loaded:
            return None, (
                f"HOST: deferred tool '{name}' has not been loaded yet. "
                f'Call {FIND_TOOLS} first to load its definition.'
            )
        tool_args = call.arguments.get('arguments')
        if not isinstance(tool_args, dict):
            return None, f"HOST: arguments for '{name}' must be a JSON object."

        try:
            validator = self._validator_for(name)
            raw_errors = sorted(
                validator.iter_errors(tool_args),
                key=lambda err: ([str(p) for p in err.absolute_path], err.message),
            )
        except Exception as exc:
            return None, f"HOST: schema validation failed for '{name}': {type(exc).__name__}"

        if raw_errors:
            formatted = [
                f'{_json_pointer(err.absolute_path)}: {err.message}'[:MAX_SCHEMA_ERROR_CHARS]
                for err in raw_errors[:MAX_SCHEMA_ERRORS]
            ]
            lines = '\n'.join(f'- {item}' for item in formatted)
            return None, f"HOST: invalid arguments for '{name}':\n{lines}"

        return ToolCall(call.call_id, name, tool_args), None
