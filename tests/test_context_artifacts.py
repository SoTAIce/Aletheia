import json
from unittest.mock import AsyncMock
import pytest
from app.agent.step_models import ToolResult, ToolMessage, UserMessage, ModelTurn
from app.agent.tool_calling_model import ModelContextOverflowError
from app.services.context_manager import ContextManager, ContextBudgetError
from app.services.conversation_archive import ConversationArchive
from app.tools.context_archive_tool import ContextArchiveTool


async def test_large_tool_result_retains_original_with_recoverable_preview(tmp_path):
    archive = ConversationArchive(tmp_path,'session')
    manager = ContextManager(archive=archive, tool_result_chars=300, tool_preview_chars=80)
    result = ToolResult('call','read_file',True,'needle '+'x'*5000)
    view = await manager.project_tool_result(result)
    assert view['success'] is True and view['truncated'] is True
    assert len(view['content']) <= 80
    # The archive keeps the tool's own text, so offsets match what was read (D-052).
    assert archive.read_text(view['storage_ref'],limit=10000) == result.content
    assert view['total_chars'] == len(result.content)
    assert len(result.content) > 5000
    assert await manager.project_tool_result(result) == view


async def test_archive_read_tool_is_bounded_and_session_scoped(tmp_path):
    a,b = ConversationArchive(tmp_path,'a'),ConversationArchive(tmp_path,'b')
    ref = a.save_text('0123456789')
    tool = ContextArchiveTool(a)
    result = await tool.invoke({'ref':ref,'offset':2,'limit':4})
    assert result.success and result.content == '2345\n[more text follows; continue with offset=6]'   # D-052
    # A limit above the tool's maximum is served up to that maximum (D-052).
    assert (await tool.invoke({'ref':ref,'limit':16001})).content == '0123456789'
    for args in [{'ref':ref,'limit':0},{'ref':'../../outside'}, {'ref':ref,'offset':True}]:
        assert not (await tool.invoke(args)).success
    assert not (await ContextArchiveTool(b).invoke({'ref':ref})).success


async def test_compaction_preserves_tool_message_identity_and_required_status(tmp_path):
    manager = ContextManager(archive=ConversationArchive(tmp_path,'s'), tool_result_chars=100,
        tool_preview_chars=500, window_tokens=650, output_tokens=20,safety_tokens=10,token_counter=len)
    view = await manager.project_tool_result(ToolResult('same-id','read',False,'x'*4000,error='failed'))
    raw_message = ToolMessage('same-id',json.dumps(view))
    messages = manager.prepare((UserMessage('current goal'),raw_message),())
    assert messages[-1].call_id == 'same-id'
    payload=json.loads(messages[-1].content)
    assert payload['success'] is False and payload['error'] == 'failed'
    assert payload['storage_ref'] == view['storage_ref']
    assert raw_message.content == json.dumps(view)


async def test_overflow_retry_only_regenerates_and_stops_after_one_retry():
    backend=AsyncMock()
    manager=ContextManager()
    backend.generate.side_effect=[ModelContextOverflowError('too large'), ModelTurn('done')]
    assert (await manager.generate(backend,(UserMessage('goal'),),())).content == 'done'
    assert backend.generate.await_count == 2
    backend.generate.reset_mock()
    backend.generate.side_effect=ModelContextOverflowError('still too large')
    with pytest.raises(ModelContextOverflowError):
        await manager.generate(backend,(UserMessage('goal'),),())
    assert backend.generate.await_count == 2

async def test_optional_history_is_not_dropped_before_archived_tool_preview(tmp_path):
    manager=ContextManager(archive=ConversationArchive(tmp_path,'s'),tool_result_chars=100,
                           tool_preview_chars=800,token_counter=len)
    view=await manager.project_tool_result(ToolResult('call','read',True,'x'*5000))
    context={'active_goal':'keep goal','pinned_contexts':[{'source':'conversation_memory',
        'content':json.dumps({'turns':[{'turn_id':'history','messages':[{'role':'user','content':'prior context'}]}]})}]}
    messages=(UserMessage(json.dumps(context)),ToolMessage('call',json.dumps(view)))
    manager.window_tokens=manager.count_request((messages[0],manager._shrink_tool(messages[1])),())
    prepared=manager.prepare(messages,())
    assert 'history' in prepared[0].content
    assert json.loads(prepared[1].content)['content']==''

async def test_observation_views_keep_identity_even_for_small_results():
    result=ToolResult('identity','lookup',True,'small')
    view=await ContextManager().project_tool_result(result)
    assert view['call_id']=='identity' and view['tool_name']=='lookup'

