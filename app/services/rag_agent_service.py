"""RAG agent service built on LangChain ``create_agent``."""

import asyncio
from textwrap import dedent
from typing import Annotated, Any, AsyncGenerator, Sequence, TypedDict

from langchain.agents import create_agent
from langchain.agents.middleware import before_model
from langchain_core.messages import BaseMessage, HumanMessage, RemoveMessage
from langchain_qwq import ChatQwen
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.message import REMOVE_ALL_MESSAGES, add_messages
from loguru import logger

from app.agent.mcp_client import (
    format_exception_chain,
    get_mcp_client_with_retry,
    load_mcp_tools_safe,
    suggest_mcp_transport,
)
from app.config import config
from app.tools import DEFAULT_LOCAL_AGENT_TOOLS


class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]


def select_recent_messages(
    messages: Sequence[BaseMessage],
    max_messages: int = 6,
) -> list[BaseMessage]:
    """Keep a recent slice that starts at a complete user turn."""
    if len(messages) <= max_messages:
        return list(messages)

    recent = list(messages[-max_messages:])
    for index, message in enumerate(recent):
        if isinstance(message, HumanMessage):
            return recent[index:]
    return recent


@before_model
def trim_messages_middleware(state: AgentState, runtime: Any) -> dict[str, Any] | None:
    messages = state["messages"]
    if len(messages) <= 6:
        return None
    recent_messages = select_recent_messages(messages)
    return {
        "messages": [
            RemoveMessage(id=REMOVE_ALL_MESSAGES),
            *recent_messages,
        ]
    }


def _message_text(message: Any) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") in {"text", "output_text"}:
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return str(content) if content is not None else ""


class RagAgentService:
    def __init__(self, streaming: bool = True, model: Any | None = None) -> None:
        self.model_name = config.rag_model
        self.streaming = streaming
        self.system_prompt = self._build_system_prompt()
        self.model = model or ChatQwen(
            model=self.model_name,
            api_key=config.dashscope_api_key,
            base_url=config.dashscope_base_url,
            temperature=0.7,
            streaming=streaming,
        )
        self.tools = list(DEFAULT_LOCAL_AGENT_TOOLS)
        self.mcp_tools: list[Any] = []
        self.checkpointer = MemorySaver()
        self.agent: Any | None = None
        self._agent_initialized = False
        self._initialization_lock = asyncio.Lock()

    async def _initialize_agent(self) -> None:
        if self._agent_initialized:
            return
        async with self._initialization_lock:
            if self._agent_initialized:
                return

            for name, server in config.mcp_servers.items():
                hint = suggest_mcp_transport(
                    str(server.get("url", "")),
                    str(server.get("transport", "")),
                )
                if hint:
                    logger.warning("MCP configuration [{}]: {}", name, hint)

            try:
                mcp_client = await get_mcp_client_with_retry()
                mcp_tools, mcp_error = await load_mcp_tools_safe(mcp_client)
                if mcp_error:
                    logger.warning("MCP tools unavailable; using local tools: {}", mcp_error)
                    self.mcp_tools = []
                else:
                    self.mcp_tools = list(mcp_tools)
            except Exception:
                logger.exception("MCP initialization failed; using local tools")
                self.mcp_tools = []

            all_tools = self.tools + self.mcp_tools
            self.agent = create_agent(
                model=self.model,
                tools=all_tools,
                system_prompt=self.system_prompt,
                middleware=[trim_messages_middleware],
                checkpointer=self.checkpointer,
            )
            self._agent_initialized = True

    @staticmethod
    def _build_system_prompt() -> str:
        return dedent(
            """
            你是一名专业的 AI 助手，可以使用工具帮助用户解决问题。

            工作原则：
            1. 理解用户需求，并在需要专业知识或外部信息时选择合适工具。
            2. 基于工具返回的内容回答，不编造资料中不存在的信息。
            3. 工具没有足够信息或暂时不可用时，明确说明限制。
            4. 回答保持简洁、准确、专业。

            使用知识库证据回答时：
            - 优先依据 retrieve_knowledge 返回的 evidence。
            - 关键事实使用对应的 [ref:n]，只引用实际返回的 ref。
            - 不编造文件名、接口名、配置项、行号或 ref。
            - 证据不足时明确说明当前知识库证据不足。
            - 有限推断必须明确说明是基于现有资料的推断。
            """
        ).strip()

    @staticmethod
    def _validate_input(question: str, session_id: str) -> None:
        if not question or not question.strip():
            raise ValueError("question must not be empty")
        if not session_id or not session_id.strip():
            raise ValueError("session_id must not be empty")

    async def query(self, question: str, session_id: str) -> str:
        self._validate_input(question, session_id)
        await self._initialize_agent()
        if self.agent is None:
            raise RuntimeError("agent initialization did not produce an agent")

        try:
            result = await self.agent.ainvoke(
                {"messages": [HumanMessage(content=question)]},
                config={"configurable": {"thread_id": session_id}},
            )
            messages = result.get("messages", [])
            return _message_text(messages[-1]) if messages else ""
        except Exception as exc:
            logger.error(
                "RAG query failed for session {}: {}",
                session_id,
                format_exception_chain(exc),
            )
            raise

    async def query_stream(
        self,
        question: str,
        session_id: str,
    ) -> AsyncGenerator[dict[str, Any], None]:
        try:
            self._validate_input(question, session_id)
            await self._initialize_agent()
            if self.agent is None:
                raise RuntimeError("agent initialization did not produce an agent")

            async for token, metadata in self.agent.astream(
                {"messages": [HumanMessage(content=question)]},
                config={"configurable": {"thread_id": session_id}},
                stream_mode="messages",
            ):
                text = _message_text(token)
                if text:
                    node = metadata.get("langgraph_node", "unknown") if isinstance(metadata, dict) else "unknown"
                    yield {"type": "content", "data": text, "node": node}

            yield {"type": "complete"}
        except Exception as exc:
            detail = format_exception_chain(exc)
            logger.error("RAG stream failed for session {}: {}", session_id, detail)
            yield {"type": "error", "data": detail}
