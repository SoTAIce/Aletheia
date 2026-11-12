import json
from unittest.mock import AsyncMock
import pytest
from app.models.conversation_memory import ConversationMemory,ConversationTurn,ConversationMessage,MessageRole
from app.models.run_state import RunMemory, RunState
from app.services.context_manager import ContextManager
from app.tools.knowledge_registry_tool import KnowledgeRegistryTool


async def test_rag_reserves_rewrite_prompt_and_schema_before_passing_history():
    from app.services.context_manager import ContextBudgetError
    search = AsyncMock()
    manager = ContextManager(window_tokens=800, output_tokens=100, safety_tokens=0,
                             token_counter=len)
    tool = KnowledgeRegistryTool(RunMemory(RunState('s', 'query')), search=search,
                                 context_manager=manager)
    with pytest.raises(ContextBudgetError):
        await tool.invoke({'query': 'facts'})
    search.assert_not_awaited()


async def test_rag_history_uses_shared_projection_including_earlier_cached_turns():
    conversation=ConversationMemory('s')
    for i in range(5):
        await conversation.add(ConversationTurn([ConversationMessage(MessageRole.USER,f'fact-{i}')]))
    search=AsyncMock(return_value=('no results',{'status':'empty','citations':[]}))
    memory=RunMemory(RunState('s','query'))
    tool=KnowledgeRegistryTool(memory,conversation,search,context_manager=ContextManager())
    output=await tool.invoke({'query':'facts'})
    assert output.success
    history=search.call_args.args[1]
    assert any('fact-0' in message.content for message in history)
    assert memory.task_state.snapshot().active_goal=='query'
