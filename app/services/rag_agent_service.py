"""Session-facing service using Aletheia's single registry and AgentRuntime."""

import asyncio
import json
import logging
import math
from dataclasses import asdict
from datetime import datetime, timezone
from uuid import uuid4
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
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
        execution_mode: str = 'single', team_options=None,
    ) -> None:
        self.streaming = streaming
        self.model = model
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
        self._team_options = team_options or (DelegationOptions() if execution_mode == 'team' else None)
        if execution_mode == 'team' and runner_requirements is None:
            self._runner_options['runner_requirements'] = _team_requirements
        self._routes: dict[tuple[object, str], object] = {}   # task -> ModeChoice made at its start (D-026)
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
        if self._team_options is not None and step_timeout_seconds is None:
            self._limits['step_timeout_seconds'] = max(
                self._limits['step_timeout_seconds'], self._team_options.timeout_seconds + 5)
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
    @asynccontextmanager
    async def _models(self):
        if self.model is not None:
            yield self._planner_model or PlanningModel(self.model), self.model
            return
        async with AsyncOpenAI(
            api_key=config.dashscope_api_key, base_url=config.dashscope_base_url,
            timeout=config.rag_model_timeout_seconds, max_retries=0,
        ) as client:
            model = OpenAICompatibleToolCallingModel(
                client, config.rag_model, temperature=0,
                json_mode=config.rag_json_mode, enable_thinking=config.rag_enable_thinking,
                max_output_tokens=config.context_output_tokens,
            )
            yield self._planner_model or PlanningModel(model), model

    @staticmethod
    def _validate_input(question: str, session_id: str) -> None:
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question must not be empty")
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must not be empty")

    async def query_result(
        self, question: str, session_id: str, *, user_id: str | None = None,
        request_id: str | None = None, resume_task_id: str | None = None,
        question_id: str | None = None,
    ) -> AgentRunResult:
        if self._closed:
            raise RuntimeError('service is closed')
        self._validate_input(question, session_id)
        self._validate_identity(user_id)
        for name, value in (('request_id', request_id), ('resume_task_id', resume_task_id), ('question_id', question_id)):
            if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 256):
                raise ValueError(f'{name} must be a nonempty string of at most 256 characters')
        if question_id is not None and resume_task_id is None:
            raise ValueError('question_id requires resume_task_id')
        stamp = datetime.now(timezone.utc)
        key = self._session_key(user_id, session_id)
        async with self._locks.setdefault(key, asyncio.Lock()):
            conversation = self._conversations.get(key)
            if conversation is None:
                label = self._archive_label(user_id, session_id)
                archive = None
                if self._memory_directory:
                    root = Path(self._memory_directory)
                    if user_id is not None:
                        root = root / "user-scoped"
                    archive = await asyncio.to_thread(
                        ConversationArchive, root, label
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
            else:
                memory = WorkingMemory(TaskState(user_id or session_id, question))
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
                    window_tokens=config.context_window_tokens, output_tokens=config.context_output_tokens,
                    safety_tokens=config.context_safety_tokens, archive=conversation.archive,
                )
                if runtime is not None:
                    task.resolve_question(question_id, question)
                else:
                    history = await context_manager.prepare_conversation(conversation, task.active_goal)
                    task.pin_content(history, 'conversation_memory')
                    if self._longterm_memory is not None:
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
                async with self._models() as (planner_model, model):
                    if self._planner_model is None:
                        planner_model = PlanningModel(ContextManagedModel(model, context_manager))
                    async with open_tool_registry(
                        memory, mcp_servers=self._mcp_servers, conversation=conversation,
                        knowledge_search=self._knowledge_search, include_knowledge=self._include_knowledge,
                        local_tools=(ContextArchiveTool(conversation.archive),) if conversation.archive else (),
                        context_manager=context_manager,
                    ) as registry:
                        # The mode is chosen once per task and kept when a blocked task resumes (D-026).
                        choice = self._routes.get((key, task.task_id)) if runtime is not None else None
                        if choice is None:
                            choice = await self._choose_route(question, model)
                        self._routes[(key, task.task_id)] = choice
                        route = choice.mode
                        # Read-only tasks lose write tools and get read-only runs (D-027/D-028).
                        from app.agent.route_selection import task_registry
                        registry = task_registry(registry, choice.read_only)
                        # Team V2 owns the whole task: one fixed plan step, one team (DECISIONS D-019).
                        if route == 'team':
                            from app.agent.team_v2 import SingleStepPlanner
                            planner = SingleStepPlanner(planner_model, max_steps=1, context_manager=context_manager)
                        else:
                            planner = Planner(planner_model, context_manager=context_manager)
                        if runtime is None:
                            runner_options = dict(self._runner_options)
                            if route == 'team':
                                runner_options['runner_registry'] = self._team_registry()
                                if runner_options['runner_requirements'] is None:
                                    runner_options['runner_requirements'] = _team_requirements
                            runtime = AgentRuntime(planner, model, memory, registry,
                                                   **self._limits, **runner_options, context_manager=context_manager)
                        else:
                            runtime.rebind(planner, model, registry, context_manager=context_manager)
                        result = await runtime.run()
                        # Retain the result before resource cleanup or durable completion can fail.
                        self._pending_outcomes[outcome_key] = (TurnStatus(result.status.value), result.content, asdict(result))
                        if result.status is TaskStatus.BLOCKED:
                            self._tasks[(key, task.task_id)] = runtime
                        else:
                            self._tasks.pop((key, task.task_id), None)
                            self._routes.pop((key, task.task_id), None)
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
                    self._tasks.pop((key, task.task_id), None)
                    self._routes.pop((key, task.task_id), None)
                    self._pending_outcomes[outcome_key] = (status, content, None)
                await _durable(self._publish_outcome(conversation, outcome_key))
                raise
            await _durable(self._publish_outcome(conversation, outcome_key))
            return result

    async def _choose_route(self, question, model):
        """single/team keep the configured runner; auto applies host rules, then one model call."""
        from app.agent.route_selection import SINGLE, TEAM, ModeChoice, choose_mode, log_choice
        if self._execution_mode != 'auto':
            choice = ModeChoice(TEAM if self._team_options is not None else SINGLE, 'user', self._execution_mode)
            log_choice(choice)
            return choice
        return await choose_mode(question, self._team_options, model)

    def _team_registry(self):
        from app.agent.runner_registry import default_runner_registry
        from app.agent.team_v2 import register_delegating_team_runner
        registry = default_runner_registry()
        register_delegating_team_runner(registry, options=self._team_options)
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
