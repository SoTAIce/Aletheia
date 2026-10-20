"""Expose the existing RAG service through the application Tool protocol."""

import hashlib
import json
from dataclasses import asdict
from typing import Any

from app.agent.step_models import ToolDefinition, ToolOutput
from app.models.conversation_memory import ConversationMemory, MessageRole
from app.models.resources import ResourceType
from app.models.working_memory import WorkingMemory


class KnowledgeRegistryTool:
    def __init__(self, memory: WorkingMemory, conversation: ConversationMemory | None = None,
                 search=None, *, context_manager=None) -> None:
        self._memory = memory
        self._conversation = conversation
        self._search = search
        self._context_manager = context_manager

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            "retrieve_knowledge",
            "Search the internal knowledge base. Use returned evidence and actual [ref:n] citations; "
            "empty results do not support factual claims.",
            {"type": "object", "properties": {"query": {"type": "string", "minLength": 1}},
             "required": ["query"], "additionalProperties": False},
        )

    async def invoke(self, arguments: dict[str, Any]) -> ToolOutput:
        query = arguments.get("query")
        if set(arguments) != {"query"} or not isinstance(query, str) or not query.strip():
            return ToolOutput(False, "", error="query must be the only argument and a non-empty string")
        search = self._search
        if search is None:
            # Import services only when retrieval is requested, not during registration.
            from app.tools.knowledge_tool import asearch_knowledge
            search = asearch_knowledge
        from langchain_core.messages import AIMessage, HumanMessage
        history = []
        if self._conversation is not None:
            for message in self._conversation.retrieve_messages(k=3):
                if message.role is MessageRole.USER:
                    history.append(HumanMessage(message.content))
                elif message.role is MessageRole.ASSISTANT:
                    history.append(AIMessage(message.content))
        task = self._memory.task_state
        if self._context_manager is not None:
            from app.agent.step_models import UserMessage
            packet = {
                "active_goal": query.strip(),
                "constraints": [asdict(item) for item in task.constraints],
                "intermediate_results": [asdict(result) for result in task.scratchpad.intermediate_results
                                         if result.valid and result.goal_revision == task.goal_revision],
                "pinned_contexts": [],
            }
            if self._conversation is not None:
                packet["pinned_contexts"].append({"source": "conversation_memory", "content":
                    await self._context_manager.prepare_conversation(self._conversation, query)})
            projected = self._context_manager.prepare((UserMessage(json.dumps(
                packet, ensure_ascii=False, default=lambda value: value.isoformat())),), ())
            # One bounded reference packet also survives the rewrite service's
            # legacy message-count cap without silently selecting only 3 turns.
            from app.services.rag_query_rewrite_service import QueryRewriteService
            history = [HumanMessage(QueryRewriteService.budget_history(
                self._context_manager, query.strip(), projected[0].content))]
        else:
            history.extend(AIMessage(result.content) for result in task.scratchpad.intermediate_results
                           if result.valid and result.goal_revision == task.goal_revision)
        content, artifact = await search(query.strip(), history[-6:])
        status = artifact["status"]
        if status == "error":
            return ToolOutput(False, content, error=artifact.get("error") or "Knowledge retrieval failed")
        if status not in ("ok", "empty"):
            raise ValueError("Invalid RAG artifact status")
        citations = [asdict(citation) for citation in artifact.get("citations", [])]
        payload = json.dumps(
            {"status": status, "evidence": content, "citations": citations}, ensure_ascii=False
        )
        refs = ()
        if status == "ok":
            # Register the exact evidence bundle, without altering selected planning
            # context (which would invalidate the current execution token).
            digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            ref = f"rag://{digest}"
            existing = next((r for r in self._memory.resources.list_all() if r.ref == ref), None)
            rid = existing.resource_id if existing else self._memory.resources.register(
                ref, ResourceType.RETRIEVED_DOCUMENT, source="retrieve_knowledge"
            )
            if existing is None:
                self._memory.resources.mark_loaded(rid, summary=payload)
            refs = (rid,)
        return ToolOutput(True, payload, refs)
