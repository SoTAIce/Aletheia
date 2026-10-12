"""Session-facing service using Aletheia's single registry and AgentRuntime."""

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from mcp import StdioServerParameters
from openai import AsyncOpenAI

from app.agent.openai_compatible_tool_calling_model import OpenAICompatibleToolCallingModel
from app.agent.planner import Planner
from app.agent.planning_model import PlanningModel
from app.agent.runtime import AgentRunResult, AgentRuntime
from app.config import config
from app.models.conversation_memory import ConversationMemory
from app.models.taskstate import TaskState, TaskStatus
from app.models.working_memory import WorkingMemory
from app.tools.tool_loader import local_mcp_servers, open_tool_registry


class RagAgentService:
    """Each query is a task; complete conversation turns persist by session.

    Inject a ToolCallingModel and optionally a Planner text model for tests.
    query_stream keeps the event API but emits the final result, not model tokens.
    """

    def __init__(
        self, streaming: bool = True, model: Any | None = None, *, planner_model=None,
        mcp_servers: Sequence[StdioServerParameters] | None = None,
        knowledge_search=None, include_knowledge: bool = True,
        max_executions: int = 16, max_replans: int = 2,
        step_timeout_seconds: float = 60.0, planning_timeout_seconds: float = 60.0,
    ) -> None:
        self.streaming = streaming
        self.model = model
        self._planner_model = planner_model
        self._mcp_servers = tuple(mcp_servers) if mcp_servers is not None else local_mcp_servers(
            filesystem_root=config.mcp_filesystem_root or None,
            write_root=config.mcp_filesystem_write_root or None,
            tavily_api_key=config.tavily_api_key,
        )
        self._knowledge_search = knowledge_search
        self._include_knowledge = include_knowledge
        self._limits = dict(max_executions=max_executions, max_replans=max_replans,
                            step_timeout_seconds=step_timeout_seconds,
                            planning_timeout_seconds=planning_timeout_seconds)
        self._conversations: dict[str, ConversationMemory] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def _models(self):
        if self.model is not None:
            yield self._planner_model or PlanningModel(self.model), self.model
            return
        async with AsyncOpenAI(
            api_key=config.dashscope_api_key, base_url=config.dashscope_base_url,
            timeout=30.0, max_retries=0,
        ) as client:
            model = OpenAICompatibleToolCallingModel(client, config.rag_model, temperature=0)
            yield self._planner_model or PlanningModel(model), model

    @staticmethod
    def _validate_input(question: str, session_id: str) -> None:
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question must not be empty")
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must not be empty")

    async def query_result(self, question: str, session_id: str) -> AgentRunResult:
        self._validate_input(question, session_id)
        async with self._locks.setdefault(session_id, asyncio.Lock()):
            conversation = self._conversations.setdefault(session_id, ConversationMemory(session_id))
            memory = WorkingMemory(TaskState(session_id, question))
            async with self._models() as (planner_model, model):
                async with open_tool_registry(
                    memory, mcp_servers=self._mcp_servers, conversation=conversation,
                    knowledge_search=self._knowledge_search, include_knowledge=self._include_knowledge,
                ) as registry:
                    runtime = AgentRuntime(
                        Planner(planner_model), model, memory, registry,
                        conversation=conversation, **self._limits,
                    )
                    return await runtime.run()

    async def query(self, question: str, session_id: str) -> str:
        return (await self.query_result(question, session_id)).content

    async def query_stream(self, question: str, session_id: str) -> AsyncIterator[dict[str, Any]]:
        try:
            result = await self.query_result(question, session_id)
            yield {"type": "content", "data": result.content, "node": "agent_runtime"}
            terminal = "complete" if result.status is TaskStatus.COMPLETED else (
                "blocked" if result.status is TaskStatus.BLOCKED else "error"
            )
            yield {"type": terminal, "status": result.status.value, "data": result.content}
        except Exception as exc:
            yield {"type": "error", "data": f"{type(exc).__name__}: {exc}"}
