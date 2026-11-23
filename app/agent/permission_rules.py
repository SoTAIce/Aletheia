"""Rule-based tool permission control (D-094, docs/permission_rules_plan.md).

Rules are read once from a TOML file (PERMISSIONS_FILE) at startup and enforced by RuleGate
before every tool call. Path-only deny rules are also forwarded to the filesystem and sandbox
tool servers via --deny so searches, directory listings and sandbox code cannot reach them.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path, PureWindowsPath
import tomllib
from typing import Any

from app.agent.hooks import ALLOW, BEFORE_TOOL, Decision
from app.agent.run_meter import current_meter
from app.agent.tool_effects import EFFECTS

ACTIONS = ('allow', 'ask', 'deny')
MAX_RULES = 200
_RULE_KEYS = frozenset({'action', 'tool', 'effect', 'path', 'max_calls'})


class PermissionRulesError(ValueError):
    """A permission rules file the host refuses at startup; Chinese message, never file contents."""


@dataclass(frozen=True, slots=True)
class PermissionRule:
    index: int                               # 1-based position in the file
    action: str                              # allow | ask | deny
    tools: tuple[str, ...] | None = None     # None: matches all tools
    effects: tuple[str, ...] | None = None   # None: matches all effects
    path: str | None = None                  # normalized display path, or None
    path_parts: tuple[str, ...] = ()         # relative path segments for prefix matching
    max_calls: int | None = None             # per-task cap across single loop and team members

    @property
    def for_tool_server(self) -> bool:
        """True when this rule is also enforced inside the file and sandbox tool servers."""
        return (self.action == 'deny' and self.path is not None
                and self.tools is None and self.effects is None and self.max_calls is None)


@dataclass(frozen=True, slots=True)
class PermissionRules:
    rules: tuple[PermissionRule, ...]

    def __len__(self) -> int:
        return len(self.rules)

    def __bool__(self) -> bool:
        return bool(self.rules)

    def __iter__(self):
        return iter(self.rules)

    def deny_paths_for(self, *roots) -> tuple[Path, ...]:
        """Absolute paths for path-only deny rules under each given workspace root."""
        paths = []
        for root in roots:
            if not root:
                continue
            base = Path(root).resolve()
            for rule in self.rules:
                if rule.for_tool_server:
                    paths.append(base.joinpath(*rule.path_parts))
        return tuple(dict.fromkeys(paths))

    def format_table(self) -> str:
        """Chinese table of rules for --show-permissions."""
        if not self.rules:
            return '权限规则文件为空（0 条规则）。'
        headers = ('序号', '动作', '工具', '属性', '路径', '上限', '交给工具')
        rows = []
        for rule in self.rules:
            rows.append((
                str(rule.index),
                rule.action,
                ', '.join(rule.tools) if rule.tools is not None else '全部',
                ', '.join(rule.effects) if rule.effects is not None else '全部',
                rule.path if rule.path is not None else '—',
                str(rule.max_calls) if rule.max_calls is not None else '—',
                '是' if rule.for_tool_server else '否',
            ))
        lines = ['权限规则（共 {} 条）：'.format(len(self.rules))]
        lines.append('  ' + '  |  '.join(headers))
        lines.append('  ' + '-' * 60)
        for row in rows:
            lines.append('  ' + '  |  '.join(row))
        lines.append('说明：“交给工具”为“是”的规则会同时作为 --deny 传给文件和沙箱工具；其余规则只在门控上生效。')
        return '\n'.join(lines)


def _parse_string_or_list(value: Any, *, field: str, index: int) -> tuple[str, ...]:
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        if not value:
            raise PermissionRulesError(f'第 {index} 条规则的 {field} 不能为空列表')
        items = value
    else:
        raise PermissionRulesError(f'第 {index} 条规则的 {field} 必须是字符串或字符串列表')
    cleaned = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise PermissionRulesError(f'第 {index} 条规则的 {field} 必须是非空字符串')
        cleaned.append(item.strip())
    return tuple(dict.fromkeys(cleaned))


def _parse_rule_path(value: Any, *, index: int) -> tuple[str, tuple[str, ...]]:
    if not isinstance(value, str):
        raise PermissionRulesError(f'第 {index} 条规则的 path 必须是字符串')
    if '\x00' in value or not value.strip():
        raise PermissionRulesError(f'第 {index} 条规则的 path 不能为空')
    raw = value.strip()
    if raw.startswith(('/', '\\')) or bool(PureWindowsPath(raw).drive) or (len(raw) >= 2 and raw[1] == ':'):
        raise PermissionRulesError(f'第 {index} 条规则的 path 不能是绝对路径')
    normalized = raw.replace('\\', '/')
    parts = []
    for segment in normalized.split('/'):
        if segment == '..':
            raise PermissionRulesError(f'第 {index} 条规则的 path 不能包含 ..')
        if segment in ('', '.'):
            continue
        parts.append(segment)
    if not parts:
        raise PermissionRulesError(f'第 {index} 条规则的 path 不能是空或 .')
    display = '/'.join(parts) + ('/' if normalized.endswith('/') else '')
    return display, tuple(parts)


def parse_permission_rules(data: dict) -> PermissionRules:
    """Validate a parsed TOML mapping and return PermissionRules."""
    if not isinstance(data, dict):
        raise PermissionRulesError('权限规则文件内容必须是表结构')
    unknown = set(data) - {'rule'}
    if unknown:
        raise PermissionRulesError(f'权限规则文件里有不认识的段：{", ".join(sorted(unknown))}（只认 [[rule]]）')
    raw_rules = data.get('rule', [])
    if not isinstance(raw_rules, list):
        raise PermissionRulesError('权限规则文件的 rule 必须是数组表（[[rule]]）')
    if len(raw_rules) > MAX_RULES:
        raise PermissionRulesError(f'权限规则最多 {MAX_RULES} 条（当前 {len(raw_rules)} 条）')
    rules = []
    for index, body in enumerate(raw_rules, start=1):
        if not isinstance(body, dict):
            raise PermissionRulesError(f'第 {index} 条规则必须是一个表（[[rule]]）')
        extra = set(body) - _RULE_KEYS
        if extra:
            raise PermissionRulesError(f'第 {index} 条规则有不认识的字段：{", ".join(sorted(extra))}')
        if 'action' not in body:
            raise PermissionRulesError(f'第 {index} 条规则缺少必填字段 action')
        action = body['action']
        if not isinstance(action, str) or action not in ACTIONS:
            raise PermissionRulesError(f'第 {index} 条规则的 action 取值不对（只认 {", ".join(ACTIONS)}）')
        tools = _parse_string_or_list(body['tool'], field='tool', index=index) if 'tool' in body else None
        effects = None
        if 'effect' in body:
            effects = _parse_string_or_list(body['effect'], field='effect', index=index)
            bad = [e for e in effects if e not in EFFECTS]
            if bad:
                raise PermissionRulesError(
                    f'第 {index} 条规则的 effect 取值不对（只认 {", ".join(EFFECTS)}）')
        path_display, path_parts = (None, ())
        if 'path' in body:
            path_display, path_parts = _parse_rule_path(body['path'], index=index)
        max_calls = None
        if 'max_calls' in body:
            value = body['max_calls']
            if type(value) is not int or value <= 0:
                raise PermissionRulesError(f'第 {index} 条规则的 max_calls 必须是正整数')
            if action == 'deny':
                raise PermissionRulesError(f'第 {index} 条规则的 max_calls 不能和 deny 一起用')
            max_calls = value
        rules.append(PermissionRule(
            index=index, action=action, tools=tools, effects=effects,
            path=path_display, path_parts=path_parts, max_calls=max_calls,
        ))
    return PermissionRules(tuple(rules))


def load_permission_rules(path: str | Path) -> PermissionRules:
    """Read and parse a permission rules TOML file; errors are Chinese PermissionRulesError."""
    source = Path(path)
    try:
        data = tomllib.loads(source.read_text(encoding='utf-8'))
    except FileNotFoundError:
        raise PermissionRulesError(f'找不到权限规则文件 {source}') from None
    except (OSError, UnicodeDecodeError) as exc:
        raise PermissionRulesError(f'读不了权限规则文件 {source}（{type(exc).__name__}）') from None
    except tomllib.TOMLDecodeError:
        raise PermissionRulesError(f'权限规则文件 {source} 不是合法的 TOML') from None
    return parse_permission_rules(data)


def _collapse_segments(segments: list[str]) -> tuple[str, ...] | None:
    """Resolve '.' and '..' literally; return None if '..' escapes above root."""
    stack: list[str] = []
    for seg in segments:
        if seg in ('', '.'):
            continue
        if seg == '..':
            if not stack:
                return None
            stack.pop()
        else:
            stack.append(seg)
    return tuple(stack)


def _split_absolute(text: str) -> tuple[str | None, tuple[str, ...] | None]:
    """Parse an absolute literal path into (root_key, parts), or (None, None) if invalid.

    root_key is 'c:' (drive) or '/' (POSIX root). Returns (None, None) for UNC or drive-relative.
    """
    normalized = text.replace('\\', '/')
    if normalized.startswith('//'):
        return None, None
    if len(normalized) >= 2 and normalized[1] == ':' and normalized[0].isalpha():
        if len(normalized) < 3 or normalized[2] != '/':
            return None, None
        parts = _collapse_segments(normalized[3:].split('/'))
        return (normalized[:2], parts) if parts is not None else (None, None)
    if normalized.startswith('/'):
        parts = _collapse_segments(normalized[1:].split('/'))
        return ('/', parts) if parts is not None else (None, None)
    return None, None


def _is_absolute_literal(text: str) -> bool:
    normalized = text.replace('\\', '/')
    if normalized.startswith('/'):
        return True
    if len(normalized) >= 2 and normalized[1] == ':' and normalized[0].isalpha():
        return True
    return False


def _normalize_call_path(raw: Any, workspace: str | Path | None, *, case_insensitive: bool) -> tuple[str, tuple[str, ...]]:
    """Normalize a path argument from args_preview without touching the filesystem.

    Returns ('parts', tuple_of_segments) or ('unknown', ()).
    """
    if isinstance(raw, dict):
        if raw.get('omitted_chars', 0) > 0:
            return 'unknown', ()
        text = raw.get('text')
        if not isinstance(text, str):
            return 'unknown', ()
    elif isinstance(raw, str):
        text = raw
    else:
        return 'unknown', ()
    if '\x00' in text:
        return 'unknown', ()
    if _is_absolute_literal(text):
        if not workspace:
            return 'unknown', ()
        ws_root, ws_parts = _split_absolute(str(workspace))
        call_root, call_parts = _split_absolute(text)
        if ws_root is None or call_root is None or ws_parts is None or call_parts is None:
            return 'unknown', ()
        if case_insensitive:
            if ws_root.lower() != call_root.lower():
                return 'unknown', ()
            ws_cmp = tuple(p.lower() for p in ws_parts)
            call_cmp = tuple(p.lower() for p in call_parts)
        else:
            if ws_root != call_root:
                return 'unknown', ()
            ws_cmp, call_cmp = ws_parts, call_parts
        if len(call_cmp) < len(ws_cmp) or call_cmp[:len(ws_cmp)] != ws_cmp:
            return 'unknown', ()
        return 'parts', call_parts[len(ws_parts):]
    parts = _collapse_segments(text.replace('\\', '/').split('/'))
    if parts is None:
        return 'unknown', ()
    return 'parts', parts


def _path_prefix_matches(call_parts: tuple[str, ...], rule_parts: tuple[str, ...], *, case_insensitive: bool) -> bool:
    if len(call_parts) < len(rule_parts):
        return False
    if case_insensitive:
        return tuple(p.lower() for p in call_parts[:len(rule_parts)]) == tuple(p.lower() for p in rule_parts)
    return call_parts[:len(rule_parts)] == rule_parts


class RuleGate:
    """A before_tool gate that enforces PermissionRules for one run (D-094)."""

    name = 'permissions'
    events = frozenset({BEFORE_TOOL})

    def __init__(
        self,
        rules: PermissionRules,
        *,
        workspace: str | Path | None = None,
        asker=None,
        default_ask_tools: frozenset[str] = frozenset(),
        ask_external: bool = False,
        case_insensitive: bool | None = None,
    ) -> None:
        self._rules = rules
        self._workspace = str(workspace) if workspace else None
        self._asker = asker
        self._default_ask_tools = frozenset(default_ask_tools)
        self._ask_external = bool(ask_external)
        self._case_insensitive = (os.name == 'nt') if case_insensitive is None else bool(case_insensitive)
        self._counts: dict[int, int] = {}
        wait = getattr(asker, 'wait_seconds', None)
        if isinstance(wait, (int, float)) and not isinstance(wait, bool) and wait > 0:
            self.timeout_seconds = float(wait) + 5

    def use_workspace(self, workspace) -> None:
        """Set the task's working directory when the caller could not know it (the web run)."""
        if self._workspace is None and workspace:
            self._workspace = str(workspace)

    def _matched_rules(self, event: dict) -> list[PermissionRule]:
        tool = event.get('tool')
        effect = event.get('effect')
        preview = event.get('args_preview')
        if isinstance(preview, dict) and 'path' in preview:
            path_state, path_parts = _normalize_call_path(
                preview['path'], self._workspace, case_insensitive=self._case_insensitive)
        else:
            path_state, path_parts = 'absent', ()
        matched = []
        for rule in self._rules.rules:
            if rule.tools is not None and tool not in rule.tools:
                continue
            if rule.effects is not None and effect not in rule.effects:
                continue
            if rule.path is not None:
                if path_state == 'absent':
                    continue
                if path_state == 'unknown':
                    if rule.action not in ('deny', 'ask'):
                        continue
                elif not _path_prefix_matches(path_parts, rule.path_parts, case_insensitive=self._case_insensitive):
                    continue
            matched.append(rule)
        return matched

    def _record_allowed(self, matched: list[PermissionRule]) -> None:
        for rule in matched:
            if rule.max_calls is not None:
                self._counts[rule.index] = self._counts.get(rule.index, 0) + 1

    async def check(self, event: dict) -> Decision:
        meter = current_meter()
        if meter is not None and self._rules:
            meter.enable_permissions()
        matched = self._matched_rules(event)

        # Priority 1: deny wins over ask and allow.
        for rule in matched:
            if rule.action == 'deny':
                if meter is not None:
                    meter.record_permission(denied=True)
                return Decision.deny(f'denied by permission rule {rule.index}', rule=rule.index)

        # Priority 2: per-task call limit on any matched rule.
        for rule in matched:
            if rule.max_calls is not None and self._counts.get(rule.index, 0) >= rule.max_calls:
                if meter is not None:
                    meter.record_permission(denied=True)
                return Decision.deny(
                    f'denied by permission rule {rule.index}: this task already made '
                    f'{rule.max_calls} calls (limit {rule.max_calls})',
                    rule=rule.index,
                )

        # Priority 3: ask wins over allow.
        for rule in matched:
            if rule.action == 'ask':
                if self._asker is None:
                    if meter is not None:
                        meter.record_permission(denied=True)
                    return Decision.deny(
                        f'permission rule {rule.index} asks the user, but no one can answer here',
                        rule=rule.index,
                    )
                if meter is not None:
                    meter.record_permission(asked=True)
                decision = await self._asker.ask(event, rule=rule.index)
                if decision.allow:
                    self._record_allowed(matched)
                    return ALLOW
                if meter is not None:
                    meter.record_permission(denied=True)
                return decision

        # Priority 4: allow.
        if matched:
            self._record_allowed(matched)
            return ALLOW

        # No rule matched: fall back to default (web asks for default_ask_tools + external;
        # ask_external also asks for external tools even when default_ask_tools is empty, D-095).
        ask_by_default = (
            (self._default_ask_tools and (
                event.get('tool') in self._default_ask_tools or event.get('effect') == 'external'
            ))
            or (self._ask_external and event.get('effect') == 'external')
        )
        if ask_by_default:
            if self._asker is None:
                if meter is not None:
                    meter.record_permission(denied=True)
                return Decision.deny(
                    'external tool requires user confirmation, but no one can answer here',
                    rule=None,
                )
            if meter is not None:
                meter.record_permission(asked=True)
            decision = await self._asker.ask(event, rule=None)
            if not decision.allow and meter is not None:
                meter.record_permission(denied=True)
            return decision
        return ALLOW
