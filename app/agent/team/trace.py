"""Safe team execution trace for diagnosis (team-diagnosis-001).

Only host-validated or host-authored values are included: action names (enum
checked by the COMMAND contract), member names (regex checked), event kinds,
host rejection reasons, review decisions (enum checked), tool names (host
registered), counters and exception type names. Instructions, payloads, tool
content, feedback and any other model text are never included.
"""

import json
import re

from app.agent.team.diagnostics import failure_types

_NAME = re.compile(r'[a-z][a-z0-9_-]{0,31}')
_ACTIONS = {'create_worker', 'create_reviewer', 'assign', 'run_member', 'review', 'finish', 'ask_user', 'fail'}
_EVENTS = {'member_created', 'assignment_changed', 'candidate_submitted', 'coordination_requested',
           'review_completed', 'command_rejected', 'completed', 'finished', 'member_failed'}


# Fixed host-authored TeamError texts; anything else (e.g. a Lead `fail` reason) is model text.
_HOST_RAISES = frozenset({
    'member name must be unique and valid', 'team member limit reached', 'member assignment must be nonempty',
    'member tool permission exceeds host allowlist',
    'member tool permission: write and execution tools are granted to workers only',
    'team action limit reached', 'stale or invalid review', 'unsupported team command',
    'question must be nonempty', 'team model call budget exhausted', 'team estimated token budget exhausted',
    'team business tool budget exhausted', 'team tool permission denied'})
_HOST_PREFIXES = ('team member failure limit reached', 'team tool failed')


def raised_outcome(exc) -> dict:
    out = {'event': 'raised', 'type': type(exc).__name__}
    text = str(exc)
    if text in _HOST_RAISES:
        out['reason'] = text
    else:
        prefix = next((p for p in _HOST_PREFIXES if text.startswith(p)), None)
        if prefix:
            out['reason'] = prefix
    return out


def _name(value):
    return value if isinstance(value, str) and _NAME.fullmatch(value) else ('' if not value else '?')


def outcome_of(event: dict) -> dict:
    """Reduce a coordinator event to safe fields."""
    kind = event.get('event') if isinstance(event, dict) else None
    out = {'event': kind if kind in _EVENTS else 'other'}
    if kind == 'command_rejected':
        out['reason'] = str(event.get('reason', ''))[:160]  # host-authored gate messages
    if kind == 'member_failed':
        out['failure'] = str(event.get('failure') or '')[:160]  # exception type names only
    if kind == 'review_completed':
        decision = (event.get('review') or {}).get('decision')
        out['decision'] = decision if decision in ('approve', 'revise') else '?'
    return out


def _session(session, role, tools=()):
    kinds = {}
    for event in session.events:
        kinds[event.kind] = kinds.get(event.kind, 0) + 1
    return {
        'role': role, 'status': session.status, 'version': session.version,
        'tools': sorted(tools), 'model_calls': session.model_calls, 'tool_calls': session.tool_calls,
        'format_repairs': session.format_repairs, 'argument_repairs': getattr(session, 'argument_repairs', 0),
        'max_format_repairs': session.max_format_repairs,
        'event_counts': {k: v for k, v in sorted(kinds.items()) if _NAME.fullmatch(k)},
        'tool_sequence': [[r.tool_name, bool(r.success)] for r in session.tool_results][-40:],
        'argument_rejections': _argument_rejections(session),
        'unknown_tools': _unknown_tools(session),
        'failure': _failure_types(session.failure),
    }


def _failure_types(detail):
    # Nested groups repeat the same type name, so dedupe must not stop traversal.
    if not isinstance(detail, dict) or 'type' not in detail:
        return None
    return failure_types(detail) or None


def _unknown_tools(session):
    names = []
    for event in session.events:
        if event.kind == 'unknown_tool_rejected':
            try:
                name = json.loads(event.detail).get('tool_name')
            except (TypeError, ValueError, AttributeError):
                name = None
            names.append(name if isinstance(name, str) and _NAME.fullmatch(name) else None)
    return names[-8:]


def _argument_rejections(session):
    """Safe metadata of undecodable tool arguments: host-checked tool name, reason enum, size."""
    out = []
    for event in session.events:
        if event.kind != 'tool_arguments_rejected':
            continue
        try:
            detail = json.loads(event.detail)
        except (TypeError, ValueError):
            detail = {}
        node = detail if isinstance(detail, dict) else {}
        name = node.get('tool_name')
        chars = node.get('argument_chars')
        out.append({'tool': name if isinstance(name, str) and _NAME.fullmatch(name) else None,
                    'reason': node.get('reason') if node.get('reason') in
                    ('not_string', 'invalid_json', 'not_object') else None,
                    'chars': chars if type(chars) is int else None})
    return out[-8:]


def team_trace(state, outcomes) -> dict:
    actions = []
    for index, command in enumerate(state.history):
        action = command.get('action')
        entry = {'action': action if action in _ACTIONS else '?',
                 'member': _name(command.get('member_id')), 'target': _name(command.get('target_id'))}
        if index < len(outcomes):
            entry.update(outcomes[index])
        actions.append(entry)
    sessions = {}
    if state.lead is not None:
        sessions['lead'] = _session(state.lead, 'lead')
    for name, member in state.members.items():
        sessions[_name(name)] = _session(member.session, member.role, member.tools)
    budget = state.budget
    return {'status': state.status, 'actions': actions, 'sessions': sessions,
            'budget': {'model_calls': budget.model_calls, 'tool_calls': budget.tool_calls,
                       'active_seconds': round(budget.active_seconds, 1),
                       'format_repairs': budget.format_repairs, 'member_failures': budget.member_failures},
            'failure': _failure_types(state.failure)}
