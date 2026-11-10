"""Session-facing service using Aletheia's single registry and AgentRuntime."""

import asyncio
import hashlib
import json
import logging
import math
import re
from dataclasses import asdict
from datetime import datetime, timezone
from uuid import uuid4
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from typing import Any, TYPE_CHECKING
if TYPE_CHECKING:
    from app.services.memory_service import LongTermMemoryService

from mcp import StdioServerParameters
from openai import AsyncOpenAI

from app.agent.openai_compatible_tool_calling_model import OpenAICompatibleToolCallingModel
from app.agent.planner import Planner
from app.agent.planning_model import PlanningModel
from app.agent.runtime import AgentRunResult, AgentRuntime
from app.agent.runner_registry import RunnerRegistry
from app.agent.runner_router import RoutingPolicy, RequirementsProvider
from app.config import config
from app.models.conversation_memory import (
    ConversationMemory, ConversationMessage, ConversationTurn, MessageRole, TurnStatus,
    finish_conversation_write as _durable,
)
from app.agent.step_models import StepExecutionReceipt, ToolCall, ToolResult
from app.models.taskstate import TaskState, TaskStatus
from app.models.working_memory import WorkingMemory, StaleExecutionError
from app.tools.tool_loader import local_mcp_servers, open_tool_registry
from app.services.conversation_archive import ConversationArchive
from app.services.context_manager import ContextManager, ContextManagedModel
from app.services.summary_scheduler import SummaryScheduler
from app.tools.context_archive_tool import ContextArchiveTool


logger = logging.getLogger(__name__)


def _restore_result(data: dict) -> AgentRunResult:
    """Reconstruct an immutable execution receipt; never execute archived work."""
    data = dict(data)
    steps = []
    for item in data.pop('steps'):
        item = dict(item)
        item['source_refs'] = tuple(item['source_refs'])
        item['tool_calls'] = tuple(ToolCall(**c) for c in item['tool_calls'])
        item['tool_results'] = tuple(ToolResult(**(r | {'source_refs': tuple(r['source_refs'])}))
                                     for r in item['tool_results'])
        steps.append(StepExecutionReceipt(**item))
    data['status'] = TaskStatus(data['status'])
    data['step_log'] = tuple(data.get('step_log', ()))   # older archives have no run log (D-034)
    return AgentRunResult(steps=tuple(steps), **data)

def _total_tokens(usage) -> int:
    value = (usage or {}).get('total_tokens') if isinstance(usage, dict) else None
    return value if type(value) is int and value > 0 else 0


def _archived_tokens(archive) -> int:
    """Tokens the archived runs of a conversation used, from their stored results (D-060)."""
    total = 0
    for turn in archive.load_turns():
        try:
            result = archive.read_turn_result(turn.turn_id)
        except (KeyError, ValueError):
            continue
        total += _total_tokens((result or {}).get('usage'))
    return total


def _emit(on_event, event: dict) -> None:
    """Report host-authored progress; a failing callback never affects the request (D-045)."""
    if on_event is None:
        return
    try:
        on_event(event)
    except Exception as exc:
        logger.warning('Progress callback failed: %s', type(exc).__name__)


def _turn_view(turn: ConversationTurn, resumable: bool) -> dict:
    """The user-facing fields of one archived turn; never tool output or run details."""
    reply = turn.messages[-1].content if len(turn.messages) > 1 and turn.messages[-1].role is MessageRole.ASSISTANT else None
    return {'turn_id': turn.turn_id, 'task_id': turn.task_id,
            'status': turn.status.value if turn.status is not None else None,
            'question_id': turn.question_id, 'resumable': resumable,
            'created_at': turn.created_at.isoformat(),
            'finished_at': turn.finished_at.isoformat() if turn.finished_at else None,
            'user': turn.messages[0].content, 'reply': reply}


def _check_limit(limit) -> None:
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError('limit must be an integer between 1 and 200')


MODES = ('single', 'team', 'auto')


def _check_choice(name, value) -> None:
    if value is not None and type(value) is not bool:
        raise ValueError(f'{name} must be true, false or None')


def _without_writes(options):
    """Team options for a task whose writes are off: the same as starting without a write root (D-051)."""
    from dataclasses import replace
    from app.agent.route_selection import READ_ONLY_HIDDEN
    return replace(options, allowed_tools=options.allowed_tools - READ_ONLY_HIDDEN,
                   require_passing_run=False, acceptance_tests=False)


def _team_requirements(context):
    from app.agent.runner_router import RunnerRequirements
    return RunnerRequirements(frozenset({'agent_team'}))


class RagAgentService:
    """Each accepted request is a turn; explicit resumes retain the same task.

    Inject a ToolCallingModel and optionally a Planner text model for tests.
    query_stream keeps the event API but emits the final result, not model tokens.
    """

    def __init__(
        self, streaming: bool = True, model: Any | None = None, *, planner_model=None,
        mcp_servers: Sequence[StdioServerParameters] | None = None,
        knowledge_search=None, include_knowledge: bool = True,
        max_executions: int = 16, max_replans: int = 2,
        step_timeout_seconds: float | None = None, planning_timeout_seconds: float | None = None,
        memory_directory=None, conversation_compressor=None,
        longterm_memory:"LongTermMemoryService | None" = None,
        longterm_recall_limit: int = 5,
        longterm_timeout_seconds: float = 3.0,
        runner_registry: RunnerRegistry | None = None, routing_policy: RoutingPolicy | None = None,
        runner_requirements: RequirementsProvider | None = None,
        execution_mode: str = 'single', team_options=None, hooks=None,
        single_runner: str = 'plan', loop_options=None,
    ) -> None:
        self.streaming = streaming
        self.model = model
        # Hooks of every run of this service (D-059); None keeps the caller's.
        from app.agent.hooks import HookSet
        if hooks is not None and not isinstance(hooks, HookSet):
            raise TypeError('hooks must be a HookSet')
        self._hooks = hooks
        self._spent_tokens: dict = {}       # conversation key -> tokens used, for the cap (D-060)
        self._memory_directory = config.conversation_storage_dir if memory_directory is None else memory_directory
        self._conversation_compressor = conversation_compressor
        self._planner_model = planner_model
        self._runner_options = dict(runner_registry=runner_registry, routing_policy=routing_policy,
                                    runner_requirements=runner_requirements)
        if execution_mode not in ('single', 'team', 'auto'):
            raise ValueError('execution_mode must be single, team or auto')
        from app.agent.team_v2 import DelegationOptions
        if team_options is not None and not isinstance(team_options, DelegationOptions):
            raise TypeError('team_options must be DelegationOptions')
        if (execution_mode != 'single' or team_options is not None) and runner_registry is not None:
            raise ValueError('use register_delegating_team_runner for custom registry assembly')
        self._execution_mode = execution_mode
        # How single mode runs (loop-trial-001): 'plan' plans then executes steps; 'loop' is one
        # agent loop entered through a fixed one-step plan (docs/architecture_review.md stage 1).
        # The library default stays 'plan'; the CLI, web and bench entry points default to 'loop' (D-072).
        if single_runner not in ('plan', 'loop'):
            raise ValueError('single_runner must be plan or loop')
        if single_runner == 'loop' and runner_registry is not None:
            raise ValueError('single_runner loop assembles its own runner registry')
        from app.agent.agent_loop import LoopOptions
        if loop_options is not None and not isinstance(loop_options, LoopOptions):
            raise TypeError('loop_options must be LoopOptions')
        self._single_runner = single_runner
        self._loop_options = loop_options or LoopOptions()
        self._team_options = team_options or (DelegationOptions() if execution_mode == 'team' else None)
        self._routes: dict[tuple[object, str], object] = {}   # task -> ModeChoice made at its start (D-026)
        self._task_options: dict[tuple[object, str], dict] = {}   # task -> run options chosen at its start (D-051)
        self._mcp_servers = tuple(mcp_servers) if mcp_servers is not None else local_mcp_servers(
            filesystem_root=config.mcp_filesystem_root or None,
            write_root=config.mcp_filesystem_write_root or None,
            tavily_api_key=config.tavily_api_key,
        )
        self._knowledge_search = knowledge_search
        self._include_knowledge = include_knowledge
        self._limits = dict(max_executions=max_executions, max_replans=max_replans,
                            step_timeout_seconds=(config.agent_step_timeout_seconds
                                                  if step_timeout_seconds is None else step_timeout_seconds),
                            planning_timeout_seconds=(config.agent_planning_timeout_seconds
                                                      if planning_timeout_seconds is None else planning_timeout_seconds))
        self._conversations: dict[object, ConversationMemory] = {}
        self._locks: dict[object, asyncio.Lock] = {}
        self._tasks: dict[tuple[object, str], AgentRuntime] = {}
        self._pending_outcomes: dict[tuple[object, str], tuple] = {}
        self._summary_scheduler = SummaryScheduler(self._conversations)
        self._closed = False
        if type(longterm_recall_limit) is not int or not 1 <= longterm_recall_limit <=100:
            raise ValueError("longterm_recall_limit must be between 1 and 100")

        if (
            isinstance(longterm_timeout_seconds, bool)
            or not isinstance(longterm_timeout_seconds, (int, float))
            or not math.isfinite(longterm_timeout_seconds)
            or longterm_timeout_seconds <= 0
        ):
            raise ValueError("longterm_timeout_seconds must be positive and finite")

        self._longterm_memory = longterm_memory
        self._longterm_recall_limit = longterm_recall_limit
        self._longterm_timeout_seconds = longterm_timeout_seconds

    def _validate_identity(self, user_id: str | None) -> None:
        if user_id is None:
            if self._longterm_memory is not None:
                raise ValueError("user_id is required when long-term memory is enabled")
            return

        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("user_id must be non-empty")
        if len(user_id.encode("utf-8")) > 256:
            raise ValueError("user_id is too long")

    @staticmethod
    def _session_key(user_id: str | None, session_id: str):
        if user_id is None:
            return session_id
        return ("user", user_id, session_id)

    @staticmethod
    def _archive_label(user_id: str | None, session_id: str) -> str:
        if user_id is None:
            return session_id
        return json.dumps(
            ["user", user_id, session_id],
            ensure_ascii=False,
            separators=(",", ":"),
        )
    @staticmethod
    def _prompt_cache_key(label: str) -> str | None:
        """Per-conversation cache routing key for the separate endpoint (D-057): a hash, so
        the session and user names never leave the machine; DashScope gets none."""
        if not (config.agent_model_base_url and config.agent_model_prompt_cache_key):
            return None
        return 'aletheia-' + hashlib.sha256(label.encode('utf-8')).hexdigest()[:24]

    @asynccontextmanager
    async def _models(self, cache_key: str | None = None, hooks=None):
        """The run's models, each wrapped once in HookedModel, inside the run's hooks (D-059)."""
        from app.agent.hooks import HookedModel, using_hooks
        hooks = hooks if hooks is not None else self._hooks
        with using_hooks(hooks) if hooks is not None else nullcontext():
            async with self._base_models(cache_key) as (planner_model, model):
                yield planner_model, HookedModel(model)

    @asynccontextmanager
    async def _base_models(self, cache_key: str | None = None):
        if self.model is not None:
            yield self._planner_model or PlanningModel(self.model), self.model
            return
        base_url, api_key, model_name = config.agent_model_endpoint      # D-050
        async with AsyncOpenAI(
            api_key=api_key, base_url=base_url,
            timeout=config.rag_model_timeout_seconds, max_retries=0,
        ) as client:
            model = OpenAICompatibleToolCallingModel(
                client, model_name, temperature=0,
                json_mode=config.rag_json_mode,
                # enable_thinking is a DashScope parameter; other endpoints may reject it.
                enable_thinking=None if config.agent_model_base_url else config.rag_enable_thinking,
                max_output_tokens=config.context_output_tokens,
                prompt_cache_key=cache_key,
            )
            yield self._planner_model or PlanningModel(model), model

    async def _run_hooks(self, key, conversation, extra=None):
        """This run's hooks: the service's (or the caller's), the request's own (extra, e.g. the
        web page's approval gate, D-061), plus the audit log and the conversation token cap when
        configured (D-060). Returns (hooks, audit or None)."""
        from app.agent.builtin_hooks import AuditHook, ConversationTokenCap
        from app.agent.hooks import HookSet, current_hooks
        hooks = self._hooks if self._hooks is not None else current_hooks()
        if extra is not None:
            hooks = HookSet((*hooks.observers, *extra.observers), (*hooks.gates, *extra.gates),
                            gate_timeout=hooks.gate_timeout)
        audit = None
        if config.hooks_audit and conversation.archive is not None:
            audit = AuditHook(conversation.archive)
            hooks = hooks.with_observers(audit)
        if config.conversation_max_tokens is not None:
            if key not in self._spent_tokens:
                self._spent_tokens[key] = (await asyncio.to_thread(_archived_tokens, conversation.archive)
                                           if conversation.archive is not None else 0)
            cap = ConversationTokenCap(config.conversation_max_tokens, self._spent_tokens[key])
            hooks = HookSet(hooks.observers, (*hooks.gates, cap), gate_timeout=hooks.gate_timeout)
        return hooks, audit

    def run_options(self) -> dict:
        """Per-request choices this service accepts (D-051). A mode outside these is refused; the
        default mode is always accepted. Team and auto need the team prepared at startup."""
        accepted = {'single', self._execution_mode} | ({'team', 'auto'} if self._team_options is not None else set())
        return {'modes': [m for m in MODES if m in accepted], 'default_mode': self._execution_mode,
                'runners': ['plan', 'loop'] if self._runner_options['runner_registry'] is None else ['plan'],
                'default_runner': self._single_runner,
                'memory_available': self._longterm_memory is not None}

    def task_options(self, session_id: str, task_id: str, *, user_id: str | None = None) -> dict | None:
        """The run options a live blocked task started with; a resume keeps them (D-051)."""
        options = self._task_options.get((self._session_key(user_id, session_id), task_id))
        return dict(options) if options is not None else None

    def _forget_task(self, key, task_id) -> None:
        self._tasks.pop((key, task_id), None)
        self._routes.pop((key, task_id), None)
        self._task_options.pop((key, task_id), None)

    @staticmethod
    def _validate_input(question: str, session_id: str) -> None:
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question must not be empty")
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must not be empty")

    async def query_result(
        self, question: str, session_id: str, *, user_id: str | None = None,
        request_id: str | None = None, resume_task_id: str | None = None,
        question_id: str | None = None, on_event=None,
        execution_mode: str | None = None, allow_writes: bool | None = None, use_memory: bool | None = None,
        single_runner: str | None = None,
        hooks=None,
    ) -> AgentRunResult:
        """on_event, if given, receives host-authored progress records ('route',
        then the runtime's 'plan' and 'step'); see AgentRuntime.run (D-045).

        execution_mode, single_runner, allow_writes and use_memory choose for this request only; None keeps the
        service default (D-051). allow_writes=False hides the write and run tools as for a read-only
        task (D-027); use_memory=False skips long-term recall. A resumed task keeps its own choices."""
        if self._closed:
            raise RuntimeError('service is closed')
        self._validate_input(question, session_id)
        self._validate_identity(user_id)
        if execution_mode is not None and execution_mode not in self.run_options()['modes']:
            raise ValueError('execution mode is not available')
        if single_runner is not None and single_runner not in ('plan', 'loop'):
            raise ValueError('single_runner must be plan or loop')
        if (resume_task_id is None and single_runner == 'loop'
                and self._runner_options['runner_registry'] is not None):
            raise ValueError('single_runner loop assembles its own runner registry')
        _check_choice('allow_writes', allow_writes)
        _check_choice('use_memory', use_memory)
        if use_memory and self._longterm_memory is None:
            raise ValueError('long-term memory is not configured')
        for name, value in (('request_id', request_id), ('resume_task_id', resume_task_id), ('question_id', question_id)):
            if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 256):
                raise ValueError(f'{name} must be a nonempty string of at most 256 characters')
        if question_id is not None and resume_task_id is None:
            raise ValueError('question_id requires resume_task_id')
        from app.agent.hooks import HookSet
        if hooks is not None and not isinstance(hooks, HookSet):
            raise TypeError('hooks must be a HookSet')
        stamp = datetime.now(timezone.utc)
        key = self._session_key(user_id, session_id)
        async with self._locks.setdefault(key, asyncio.Lock()):
            conversation = self._conversations.get(key)
            if conversation is None:
                label = self._archive_label(user_id, session_id)
                archive = None
                if self._memory_directory:
                    archive = await asyncio.to_thread(
                        ConversationArchive, self._archive_root(user_id), label
                    )
                conversation = ConversationMemory(label, compressor=self._conversation_compressor, archive=archive,
                                                  background_compression=archive is not None)
                await conversation.restore()
                self._conversations[key] = conversation
                # Reconstruct old backlog on session load, including restart.
                self._notify_summary(key)

            turn_id = f'request:{request_id}' if request_id is not None else str(uuid4())
            outcome_key = (key, turn_id)
            try:
                existing = await conversation.read_turn(turn_id)
            except KeyError:
                existing = None
            if existing is not None:
                if (existing.messages[0].content != question or existing.status is None
                        or (resume_task_id is None) != (existing.question_id is None)
                        or (resume_task_id is not None and existing.task_id != resume_task_id)
                        or (question_id is not None and existing.question_id != question_id)):
                    raise ValueError('request ID conflict')
                if outcome_key in self._pending_outcomes:
                    await _durable(self._publish_outcome(conversation, outcome_key))
                    existing = await conversation.read_turn(turn_id)
                payload = await conversation.turn_result(turn_id)
                if payload is not None:
                    restored = _restore_result(payload)
                    if (restored.task_id != existing.task_id or restored.status.value != existing.status.value
                            or restored.content != existing.messages[-1].content):
                        raise ValueError('archived result conflicts with turn')
                    return restored
                raise RuntimeError(f'request already {existing.status.value}; automatic execution replay is unsafe')

            runtime = None
            if resume_task_id is not None:
                runtime = self._tasks.get((key, resume_task_id))
                if runtime is None or runtime.memory.task_state.status is not TaskStatus.BLOCKED:
                    raise ValueError('No resumable task in this user/session; restart restores history, not live tasks')
                task = runtime.memory.task_state
                questions = [q for q in task.scratchpad.open_questions
                             if q.goal_revision == task.goal_revision and q.blocking and q.resolved_at is None]
                if question_id is None:
                    if len(questions) != 1:
                        raise ValueError('question_id is required when there is not exactly one blocking question')
                    question_id = questions[0].question_id
                if not any(q.question_id == question_id for q in questions):
                    raise ValueError('question_id is not an unresolved blocking question of this task')
                memory = runtime.memory
                options = self._task_options.get((key, resume_task_id))
            else:
                memory = WorkingMemory(TaskState(user_id or session_id, question))
                options = None
            if options is None:
                options = {'mode': execution_mode or self._execution_mode,
                           'runner': single_runner or self._single_runner,
                           'write': True if allow_writes is None else allow_writes,
                           'memory': (self._longterm_memory is not None) if use_memory is None else use_memory}
            task = memory.task_state
            turn = ConversationTurn([ConversationMessage(MessageRole.USER, question, stamp)], turn_id, stamp,
                                    task_id=task.task_id, status=TurnStatus.RECEIVED, question_id=question_id)

            # Every external call is after the original input has been published.
            try:
                await _durable(conversation.add(turn))
            except asyncio.CancelledError:
                try:
                    await conversation.read_turn(turn_id)
                except KeyError:
                    raise
                self._pending_outcomes[outcome_key] = (TurnStatus.CANCELLED, 'Request cancelled before execution', None)
                await _durable(self._publish_outcome(conversation, outcome_key))
                raise
            except Exception as exc:
                # Publication may have succeeded before a cache reload failed.
                # Preserve that accepted input, but never start external work.
                try:
                    await conversation.read_turn(turn_id)
                except KeyError:
                    pass
                else:
                    self._pending_outcomes[outcome_key] = (TurnStatus.FAILED, f'Input persistence failed: {type(exc).__name__}', None)
                    await _durable(self._publish_outcome(conversation, outcome_key))
                raise
            try:
                context_manager = ContextManager(
                    window_tokens=config.agent_model_window_tokens, output_tokens=config.context_output_tokens,
                    safety_tokens=config.context_safety_tokens, archive=conversation.archive,
                )
                if runtime is not None:
                    task.resolve_question(question_id, question)
                else:
                    history = await context_manager.prepare_conversation(conversation, task.active_goal)
                    task.pin_content(history, 'conversation_memory')
                    if self._longterm_memory is not None and options['memory']:
                        try:
                            async with asyncio.timeout(self._longterm_timeout_seconds):
                                hits = await self._longterm_memory.recall(
                                    user_id=user_id, query=question, limit=self._longterm_recall_limit)
                            payload = context_manager.longterm_context(
                                hits=hits, user_id=user_id, token_budget=config.longterm_context_tokens)
                            if json.loads(payload)['entries']:
                                task.pin_content(payload, 'longterm_memory')
                        except Exception as exc:
                            logger.warning('Long-term recall unavailable: %s', type(exc).__name__)
                cache_key = self._prompt_cache_key(self._archive_label(user_id, session_id))
                run_hooks, audit = await self._run_hooks(key, conversation, hooks)
                async with self._models(cache_key, run_hooks) as (planner_model, model):
                    if self._planner_model is None:
                        planner_model = PlanningModel(ContextManagedModel(model, context_manager))
                    async with open_tool_registry(
                        memory, mcp_servers=self._mcp_servers, conversation=conversation,
                        knowledge_search=self._knowledge_search, include_knowledge=self._include_knowledge,
                        local_tools=(ContextArchiveTool(conversation.archive, context_manager.tool_result_chars),)
                                    if conversation.archive else (),
                        context_manager=context_manager,
                    ) as registry:
                        # The mode is chosen once per task and kept when a blocked task resumes (D-026).
                        choice = self._routes.get((key, task.task_id)) if runtime is not None else None
                        team_options = self._team_options
                        if team_options is not None and not options['write']:
                            team_options = _without_writes(team_options)
                        if choice is None:
                            choice = await self._choose_route(question, model, options['mode'], team_options)
                            if not options['write'] and not choice.read_only:
                                # Writes off for this request: the D-027 read-only path, decided by the host.
                                from dataclasses import replace
                                choice = replace(choice, read_only=True)
                        self._routes[(key, task.task_id)] = choice
                        self._task_options[(key, task.task_id)] = options
                        route = choice.mode
                        if audit is not None:
                            audit.note(mode=route, read_only=choice.read_only)
                        _emit(on_event, {'type': 'route', 'mode': route, 'read_only': choice.read_only,
                                         'options': dict(options)})
                        # Read-only tasks lose write tools and get read-only runs (D-027/D-028).
                        from app.agent.route_selection import task_registry
                        registry = task_registry(registry, choice.read_only)
                        # Team V2 owns the whole task: one fixed plan step, one team (DECISIONS D-019).
                        loop = route == 'single' and options['runner'] == 'loop'
                        if route == 'team':
                            from app.agent.team_v2 import SingleStepPlanner
                            planner = SingleStepPlanner(planner_model, max_steps=1, context_manager=context_manager)
                        elif loop:
                            from app.agent.agent_loop import LoopPlanner
                            planner = LoopPlanner(planner_model, max_steps=1, context_manager=context_manager)
                        else:
                            planner = Planner(planner_model, context_manager=context_manager)
                        if runtime is None:
                            runner_options, limits = dict(self._runner_options), dict(self._limits)
                            if route == 'team':
                                runner_options['runner_registry'] = self._team_registry(team_options)
                                if runner_options['runner_requirements'] is None:
                                    runner_options['runner_requirements'] = _team_requirements
                                # A team step must outlast the team's own timeout.
                                limits['step_timeout_seconds'] = max(limits['step_timeout_seconds'],
                                                                     team_options.timeout_seconds + 5)
                            elif loop:
                                from app.agent.agent_loop import loop_requirements, register_agent_loop_runner
                                from app.agent.runner_registry import default_runner_registry
                                loop_registry = default_runner_registry()
                                register_agent_loop_runner(loop_registry, options=self._loop_options)
                                runner_options['runner_registry'] = loop_registry
                                if runner_options['runner_requirements'] is None:
                                    runner_options['runner_requirements'] = loop_requirements
                                # The whole task is one step; restarting the loop after a failure
                                # would repeat its work, so the runtime does not replan it.
                                limits['step_timeout_seconds'] = max(limits['step_timeout_seconds'],
                                                                     self._loop_options.timeout_seconds + 5)
                                limits['max_replans'] = 0
                            runtime = AgentRuntime(planner, model, memory, registry,
                                                   **limits, **runner_options, context_manager=context_manager)
                        else:
                            runtime.rebind(planner, model, registry, context_manager=context_manager)
                        result = await runtime.run(on_event=on_event)
                        if key in self._spent_tokens:
                            self._spent_tokens[key] += _total_tokens(result.usage)
                        # Retain the result before resource cleanup or durable completion can fail.
                        self._pending_outcomes[outcome_key] = (TurnStatus(result.status.value), result.content, asdict(result))
                        if result.status is TaskStatus.BLOCKED:
                            self._tasks[(key, task.task_id)] = runtime
                        else:
                            self._forget_task(key, task.task_id)
            except StaleExecutionError:
                # This interaction no longer owns task state. Finish only its
                # own durable turn; do not fail or remove the newer execution.
                self._pending_outcomes[outcome_key] = (TurnStatus.FAILED,
                    'Execution superseded; stale output was not committed', None)
                await _durable(self._publish_outcome(conversation, outcome_key))
                raise
            except (Exception, asyncio.CancelledError) as exc:
                if outcome_key not in self._pending_outcomes:
                    status = TurnStatus.CANCELLED if isinstance(exc, asyncio.CancelledError) else TurnStatus.FAILED
                    content = ('Request cancelled; tool side effects may already exist' if status is TurnStatus.CANCELLED
                               else f'Request failed: {type(exc).__name__}')
                    if task.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                        task.fail_task(content)
                    self._forget_task(key, task.task_id)
                    self._pending_outcomes[outcome_key] = (status, content, None)
                await _durable(self._publish_outcome(conversation, outcome_key))
                raise
            await _durable(self._publish_outcome(conversation, outcome_key))
            return result

    def _archive_root(self, user_id: str | None) -> Path:
        root = Path(self._memory_directory)
        return root / "user-scoped" if user_id is not None else root

    def _resumable(self, key, turn: ConversationTurn) -> bool:
        return turn.status is TurnStatus.BLOCKED and turn.task_id is not None and self._live_blocked(key, turn.task_id)

    def _live_blocked(self, key, task_id: str) -> bool:
        runtime = self._tasks.get((key, task_id))
        return runtime is not None and runtime.memory.task_state.status is TaskStatus.BLOCKED

    def resumable(self, session_id: str, task_id: str, *, user_id: str | None = None) -> bool:
        """Whether this process still holds that blocked task, so an answer can resume it (D-046)."""
        return self._live_blocked(self._session_key(user_id, session_id), task_id)

    async def list_sessions(self, *, user_id: str | None = None, limit: int = 50) -> list[dict]:
        """This user's sessions, newest first, with the latest user message (D-045)."""
        self._validate_identity(user_id)
        _check_limit(limit)
        sessions = []
        if self._memory_directory:
            root = self._archive_root(user_id)
            for label in await asyncio.to_thread(ConversationArchive.session_ids, root):
                if user_id is None:
                    session_id = label
                else:
                    try:
                        kind, owner, session_id = json.loads(label)
                    except (ValueError, TypeError):
                        continue
                    if kind != 'user' or owner != user_id or self._archive_label(user_id, session_id) != label:
                        continue
                try:
                    turns = await asyncio.to_thread(ConversationArchive(root, label).load_turns, 1)
                except ValueError as exc:
                    logger.warning('Skipping unreadable session archive: %s', type(exc).__name__)
                    continue
                if turns:
                    sessions.append((session_id, turns[-1]))
        else:
            for key, conversation in self._conversations.items():
                if user_id is None:
                    session_id = key if isinstance(key, str) else None
                else:
                    session_id = key[2] if isinstance(key, tuple) and key[1] == user_id else None
                turns = conversation.retrieve_turns(1) if session_id is not None else []
                if turns:
                    sessions.append((session_id, turns[-1]))
        sessions.sort(key=lambda item: item[1].finished_at or item[1].created_at, reverse=True)
        return [{'session_id': session_id,
                 'updated_at': (turn.finished_at or turn.created_at).isoformat(),
                 'last_message': turn.messages[0].content[:200]}
                for session_id, turn in sessions[:limit]]

    async def session_turns(self, session_id: str, *, user_id: str | None = None, limit: int = 20) -> list[dict]:
        """The latest turns of one session, oldest first; an unknown session has none (D-045)."""
        self._validate_identity(user_id)
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must not be empty")
        _check_limit(limit)
        key = self._session_key(user_id, session_id)
        if self._memory_directory:
            root, label = self._archive_root(user_id), self._archive_label(user_id, session_id)
            if not await asyncio.to_thread(ConversationArchive.exists, root, label):
                return []
            turns = await asyncio.to_thread(ConversationArchive(root, label).load_turns, limit)
        else:
            conversation = self._conversations.get(key)
            turns = conversation.retrieve_turns(limit) if conversation is not None else []
        return [_turn_view(turn, self._resumable(key, turn)) for turn in turns]

    async def task_traces(self, session_id: str, task_id: str, *, user_id: str | None = None) -> list[dict]:
        """The traces of one task's runs, oldest first, from the session's audit files (trace-001,
        D-073). A blocked task and its resume are two runs. Without an archive, an audit file or a
        matching run the list is empty. Read-only: no run path changes."""
        self._validate_identity(user_id)
        for name, value in (('session_id', session_id), ('task_id', task_id)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must not be empty")
        if not self._memory_directory:
            return []
        root, label = self._archive_root(user_id), self._archive_label(user_id, session_id)
        suffix = '-' + (re.sub(r'[^0-9A-Za-z_-]', '', task_id)[:64] or 'task')

        def read():
            if not ConversationArchive.exists(root, label):
                return []
            archive = ConversationArchive(root, label)
            from app.agent.trace_view import build_trace
            traces = []
            for name in archive.list_audits():
                if not name.endswith(suffix):
                    continue
                try:
                    records, skipped = archive.load_audit(name)
                except (KeyError, ValueError, OSError):
                    continue
                trace = build_trace(records)
                if trace.get('task_id') not in (None, task_id):
                    continue
                trace['skipped_lines'] = skipped
                if skipped:
                    trace['complete'] = False
                traces.append(trace)
            return sorted(traces, key=lambda t: str(t.get('started_at') or ''))
        return await asyncio.to_thread(read)

    async def _choose_route(self, question, model, mode, team_options):
        """single/team are the user's choice; auto applies host rules, then one model call."""
        from app.agent.route_selection import ModeChoice, choose_mode, log_choice
        if mode != 'auto':
            choice = ModeChoice(mode, 'user', mode)
            log_choice(choice)
            return choice
        from app.agent.hooks import model_purpose, trace_scope
        with model_purpose('route'), trace_scope('route'):     # before the task span, no parent (D-073)
            return await choose_mode(question, team_options, model)

    def _team_registry(self, options=None):
        from app.agent.runner_registry import default_runner_registry
        from app.agent.team_v2 import register_delegating_team_runner
        registry = default_runner_registry()
        register_delegating_team_runner(registry, options=options or self._team_options)
        return registry

    async def _publish_outcome(self, conversation, outcome_key):
        status, content, payload = self._pending_outcomes[outcome_key]
        await conversation.complete_turn(outcome_key[1], status, content, result=payload)
        self._pending_outcomes.pop(outcome_key, None)
        self._notify_summary(outcome_key[0])

    def _notify_summary(self, key):
        try:
            self._summary_scheduler.notify(key)
        except Exception as exc:
            # Scheduling is an optimization over durable history, never part
            # of the accepted request's execution outcome.
            logger.warning('Summary scheduling unavailable: %s', type(exc).__name__)

    async def __aenter__(self):
        if self._closed:
            raise RuntimeError('service is closed')
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    async def aclose(self, *, timeout_seconds=5.0):
        self._closed = True
        await self._summary_scheduler.aclose(timeout_seconds=timeout_seconds)

    async def query(self, question: str, session_id: str, *, user_id: str | None = None,
                    request_id: str | None = None, resume_task_id: str | None = None,
                    question_id: str | None = None) -> str:
        return (await self.query_result(question, session_id, user_id=user_id, request_id=request_id,
                                       resume_task_id=resume_task_id, question_id=question_id)).content

    async def query_stream(self, question: str, session_id: str, *, user_id: str | None = None,
                           request_id: str | None = None, resume_task_id: str | None = None,
                           question_id: str | None = None) -> AsyncIterator[dict[str, Any]]:
        try:
            result = await self.query_result(question, session_id, user_id=user_id, request_id=request_id,
                                             resume_task_id=resume_task_id, question_id=question_id)
            yield {"type": "content", "data": result.content, "node": "agent_runtime"}
            terminal = "complete" if result.status is TaskStatus.COMPLETED else (
                "blocked" if result.status is TaskStatus.BLOCKED else "error"
            )
            yield {"type": terminal, "status": result.status.value, "data": result.content}
        except Exception as exc:
            yield {"type": "error", "data": f"{type(exc).__name__}: {exc}"}
