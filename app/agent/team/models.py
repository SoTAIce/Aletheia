"""Small task-scoped records and trusted limits for sequential teams."""
import asyncio
from dataclasses import dataclass, field
import math
from collections.abc import Callable

from app.agent.step_runner import StepModelError
from app.agent.team.contracts import JsonContract, ContractError
from app.agent.team.session import TeamSession
from app.models.working_memory import WorkingMemory


class TeamError(StepModelError):
    """Terminal team/config/protocol failure; never replay a team by replanning."""


def _answer_check(payload):
    if not payload['answer'].strip():
        raise ContractError('team answer must be nonempty')


@dataclass(frozen=True, slots=True)
class TeamOptions:
    max_members: int = 4
    max_actions: int = 32
    max_model_calls: int = 48
    max_tool_calls: int = 32
    max_estimated_tokens: int = 500000
    max_revisions: int = 12
    timeout_seconds: float = 180
    allowed_tools: frozenset[str] = frozenset({
        'get_current_time', 'read_file', 'list_directory', 'retrieve_knowledge', 'search_web'})
    output_contract: JsonContract = field(default_factory=lambda: JsonContract('team-final', (('answer', str),), _answer_check))
    # When true, finish needs a passing run_tests/run_python after the worker's last write_file.
    require_passing_run: bool = False

    def __post_init__(self):
        for name in ('max_members', 'max_actions', 'max_model_calls', 'max_tool_calls',
                     'max_estimated_tokens', 'max_revisions'):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f'{name} must be a positive integer')
        if (type(self.timeout_seconds) not in (int, float) or not math.isfinite(self.timeout_seconds)
                or self.timeout_seconds <= 0):
            raise ValueError('timeout_seconds must be finite and positive')
        if not isinstance(self.allowed_tools, frozenset) or any(
                not isinstance(n, str) or not n.strip() or n != n.strip() for n in self.allowed_tools):
            raise ValueError('allowed_tools must be a frozenset of nonempty tool names')
        if type(self.require_passing_run) is not bool:
            raise TypeError('require_passing_run must be a bool')
        if not isinstance(self.output_contract, JsonContract):
            raise TypeError('output_contract must be a host-owned JsonContract')


# Tools that change files or execute code; the coordinator grants them to workers only.
WORKER_ONLY_TOOLS = frozenset({'write_file', 'run_tests', 'run_python'})
RUN_TOOLS = frozenset({'run_tests', 'run_python'})


def has_passing_run_after_last_write(tool_results) -> bool:
    """True when a sandbox run with exit code 0 succeeded after the last successful write_file."""
    last_write = last_pass = -1
    for index, result in enumerate(tool_results):
        if not result.success:
            continue
        if result.tool_name == 'write_file':
            last_write = index
        elif result.tool_name in RUN_TOOLS and result.content.startswith('exit code 0;'):
            last_pass = index
    return last_pass > last_write


@dataclass(slots=True)
class Budget:
    model_calls: int = 0
    tool_calls: int = 0
    estimated_tokens: int = 0
    active_seconds: float = 0


@dataclass(slots=True)
class Member:
    name: str
    role: str
    session: TeamSession
    memory: WorkingMemory
    tools: frozenset[str]
    candidate: str | None = None
    review: tuple[str, int, dict] | None = None
    failure: str | None = None


@dataclass(slots=True)
class TeamState:
    key: tuple
    options: TeamOptions
    members: dict[str, Member] = field(default_factory=dict)
    lead: TeamSession | None = None
    budget: Budget = field(default_factory=Budget)
    actions: int = 0
    status: str = 'ready'
    pending_question: str | None = None
    used_answers: set[str] = field(default_factory=set)
    last_event: dict = field(default_factory=lambda: {'event': 'team_started'})
    history: list[dict] = field(default_factory=list)
    output: object = None
    owner_user_id: str | None = None
    failure: dict | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class TeamStore:
    """Bounded same-process state; factories may be rebound, state may not migrate users."""
    def __init__(self, max_teams=128):
        if type(max_teams) is not int or max_teams < 1:
            raise ValueError('max_teams must be positive')
        self.max_teams = max_teams
        self.states: dict[tuple, TeamState] = {}

    def get(self, context, options):
        key = (context.task_id, context.goal_revision, context.plan_revision, context.current_step.step_id)
        if key not in self.states:
            # Existing TaskState resolves questions by requiring a replacement
            # plan. Only the exact answered team question authorizes rebinding
            # the suspended team to that replacement plan's first team step.
            matches = [state for old, state in self.states.items() if old[:2] == key[:2]
                and state.status == 'blocked' and state.pending_question is not None and
                any(q.content == state.pending_question and q.step_id == old[3] and
                    q.question_id not in state.used_answers for q in context.resolved_questions)]
            if len(matches) > 1:
                raise TeamError('ambiguous team resume identity')
            if matches:
                state = matches[0]
                if state.options != options or state.lock.locked():
                    raise TeamError('cannot rebind incompatible or active team state')
                del self.states[state.key]
                state.key = key
                self.states[key] = state
            else:
                if len(self.states) >= self.max_teams:
                    raise TeamError('team state capacity reached')
                self.states[key] = TeamState(key, options)
        state = self.states[key]
        if state.options != options:
            raise TeamError('cannot change trusted team options during resume')
        return state

    def discard_task(self, task_id):
        for key in tuple(self.states):
            if key[0] == task_id:
                del self.states[key]


@dataclass(frozen=True, slots=True)
class TeamEnvironment:
    user_id: str
    tools_factory: Callable
    import_sources: Callable
    capture_guard: Callable | None = None
