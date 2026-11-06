"""Choose one task's execution mode: host rules first, then one bounded model call (DECISIONS D-026).

The choice is made once per task, before planning, because the mode decides the planner too: the
team owns a whole task through a single fixed step (D-019). Rules come from host configuration and
the model can never widen them; any model failure falls back to the cheapest mode, single.
The same call says whether the task changes files; when the model says it does not, the host hides
write_file and the run tools for that task (D-027), so a read-only question cannot change the workspace;
it gets run_python_code instead, which sees the workspace read-only, and other tasks never see it (D-028).
Only host values are logged: the mode, where it came from, the rule name and failure type names.
"""

import asyncio
import json
import logging
import os
from dataclasses import asdict, dataclass

from app.agent.json_output import load_json, unwrap_json_fence
from app.agent.step_models import ModelTurn, SystemMessage, UserMessage
from app.agent.team.diagnostics import failure_details, failure_types
from app.agent.team.models import WORKER_ONLY_TOOLS
from app.tools.tool_registry import ToolRegistry

logger = logging.getLogger(__name__)

SINGLE, TEAM = 'single', 'team'
FALLBACK_MODE = SINGLE
DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_GOAL_CHARS = 4000
READ_ONLY_HIDDEN = WORKER_ONLY_TOOLS      # write_file, run_tests, run_python
READ_ONLY_ONLY = frozenset({'run_python_code'})   # read-only workspace runs, for read-only tasks (D-028)

_PROMPT = (
    'You route one user task to an execution mode. Reply with one JSON object only, no Markdown: '
    '{"route": "single" or "team", "writes": true or false, "reason": "at most one short sentence"}. '
    'Choose "team" only when the task needs writing or changing code files and then running tests and '
    'fixing failures over several rounds. Choose "single" for questions, lookups, explanations, summaries, '
    'small edits and everything else. When unsure, choose "single". '
    '"writes" is false only when the task can be answered without creating or changing any file, for example '
    'a question about existing code; it is true for any task that asks to create, write, edit or fix files. '
    'When unsure, use true. Do not solve the task.'
)


@dataclass(frozen=True, slots=True)
class ModeChoice:
    """Host record of one routing decision."""
    mode: str
    source: str                 # 'user' | 'rule' | 'model' | 'fallback'
    rule: str                   # host rule that bounded the choice
    failure: str | None = None  # exception type chain when the model step failed
    read_only: bool = False     # host hides READ_ONLY_HIDDEN for this task (D-027)

    def trace(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None and v is not False}


def allowed_modes(team_options) -> tuple[frozenset, str]:
    """Host rules: the team is only a candidate where it has evidence, i.e. code delivery in the sandbox."""
    if team_options is None:
        return frozenset({SINGLE}), 'team_unavailable'
    if 'write_file' not in team_options.allowed_tools:
        return frozenset({SINGLE}), 'read_only'
    if not team_options.require_passing_run:
        return frozenset({SINGLE}), 'no_sandbox'
    return frozenset({SINGLE, TEAM}), 'code_delivery_with_sandbox'


def _parse(turn, allowed) -> tuple[str, bool]:
    if not isinstance(turn, ModelTurn) or turn.tool_calls:
        raise ValueError('route reply must be text without tool calls')
    payload = load_json(unwrap_json_fence(turn.content or ''))
    if not isinstance(payload, dict) or 'route' not in payload or not set(payload) <= {'route', 'writes', 'reason'}:
        raise ValueError('route reply must contain route and optionally writes and reason')
    route, writes = payload['route'], payload.get('writes', True)   # missing: keep every tool
    if route not in allowed:
        raise ValueError('route is not allowed by the host rules')
    if type(writes) is not bool:
        raise ValueError('writes must be true or false')
    if route == TEAM and not writes:
        raise ValueError('the team route always writes files')
    return route, not writes


async def choose_mode(goal: str, team_options, model, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> ModeChoice:
    """Rules decide when they leave one mode; otherwise one tool-free model call, validated by the host."""
    allowed, rule = allowed_modes(team_options)
    if len(allowed) == 1:
        choice = ModeChoice(next(iter(allowed)), 'rule', rule)
    else:
        messages = (SystemMessage(_PROMPT),
                    UserMessage(json.dumps({'task': goal[:MAX_GOAL_CHARS]}, ensure_ascii=False)))
        try:
            async with asyncio.timeout(timeout_seconds):
                turn = await model.generate(messages, ())
            route, read_only = _parse(turn, allowed)
            choice = ModeChoice(route, 'model', rule, read_only=read_only)
        except Exception as exc:  # cancellation still propagates
            choice = ModeChoice(FALLBACK_MODE, 'fallback', rule, failure_types(failure_details(exc)))
    log_choice(choice)
    return choice


def _without(registry: ToolRegistry, hidden) -> ToolRegistry:
    view = ToolRegistry()
    for definition in registry.list_definitions():
        if definition.name not in hidden:
            view.register(registry.get(definition.name))
    return view


def read_only_registry(registry: ToolRegistry) -> ToolRegistry:
    """A registry holding the same tool objects minus write_file and the read-write run tools."""
    return _without(registry, READ_ONLY_HIDDEN)


def task_registry(registry: ToolRegistry, read_only: bool) -> ToolRegistry:
    """The tools one task sees: read-only tasks lose write tools; the others lose run_python_code."""
    if read_only:
        return read_only_registry(registry)
    names = {d.name for d in registry.list_definitions()}
    return _without(registry, READ_ONLY_ONLY) if names & READ_ONLY_ONLY else registry


def log_choice(choice: ModeChoice) -> None:
    loud = os.environ.get('ALETHEIA_TEAM_TRACE') == '1'
    (logger.warning if loud else logger.info)('Route decision: %s', json.dumps(choice.trace()))
