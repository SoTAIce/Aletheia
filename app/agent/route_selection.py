"""Choose one task's execution mode: host rules first, then one bounded model call (DECISIONS D-026).

The choice is made once per task, before planning, because the mode decides the planner too: the
team owns a whole task through a single fixed step (D-019). Rules come from host configuration and
the model can never widen them; any model failure falls back to the cheapest mode, single.
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

logger = logging.getLogger(__name__)

SINGLE, TEAM = 'single', 'team'
FALLBACK_MODE = SINGLE
DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_GOAL_CHARS = 4000

_PROMPT = (
    'You route one user task to an execution mode. Reply with one JSON object only, no Markdown: '
    '{"route": "single" or "team", "reason": "at most one short sentence"}. '
    'Choose "team" only when the task needs writing or changing code files and then running tests and '
    'fixing failures over several rounds. Choose "single" for questions, lookups, explanations, summaries, '
    'small edits and everything else. When unsure, choose "single". Do not solve the task.'
)


@dataclass(frozen=True, slots=True)
class ModeChoice:
    """Host record of one routing decision."""
    mode: str
    source: str                 # 'user' | 'rule' | 'model' | 'fallback'
    rule: str                   # host rule that bounded the choice
    failure: str | None = None  # exception type chain when the model step failed

    def trace(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


def allowed_modes(team_options) -> tuple[frozenset, str]:
    """Host rules: the team is only a candidate where it has evidence, i.e. code delivery in the sandbox."""
    if team_options is None:
        return frozenset({SINGLE}), 'team_unavailable'
    if 'write_file' not in team_options.allowed_tools:
        return frozenset({SINGLE}), 'read_only'
    if not team_options.require_passing_run:
        return frozenset({SINGLE}), 'no_sandbox'
    return frozenset({SINGLE, TEAM}), 'code_delivery_with_sandbox'


def _parse(turn, allowed) -> str:
    if not isinstance(turn, ModelTurn) or turn.tool_calls:
        raise ValueError('route reply must be text without tool calls')
    payload = load_json(unwrap_json_fence(turn.content or ''))
    if not isinstance(payload, dict) or 'route' not in payload or not set(payload) <= {'route', 'reason'}:
        raise ValueError('route reply must contain route and optionally reason')
    route = payload['route']
    if route not in allowed:
        raise ValueError('route is not allowed by the host rules')
    return route


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
            choice = ModeChoice(_parse(turn, allowed), 'model', rule)
        except Exception as exc:  # cancellation still propagates
            choice = ModeChoice(FALLBACK_MODE, 'fallback', rule, failure_types(failure_details(exc)))
    log_choice(choice)
    return choice


def log_choice(choice: ModeChoice) -> None:
    loud = os.environ.get('ALETHEIA_TEAM_TRACE') == '1'
    (logger.warning if loud else logger.info)('Route decision: %s', json.dumps(choice.trace()))
