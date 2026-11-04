import json
from unittest.mock import AsyncMock
import pytest

from app.agent.planner import Planner
from app.agent.runtime import AgentRuntime
from app.agent.step_models import ModelTurn
from app.agent.tool_calling_model import ModelRequestError
from app.models.taskstate import TaskState
from app.models.working_memory import WorkingMemory
from app.services.context_manager import ContextManager


async def test_planner_budget_rejects_required_state_before_model():
    model = AsyncMock()
    manager = ContextManager(window_tokens=300, output_tokens=20, safety_tokens=10, token_counter=len)
    planner = Planner(model, context_manager=manager)
    memory = WorkingMemory(TaskState('user', 'x'*500))
    before = memory.snapshot()
    with pytest.raises(ModelRequestError):
        await planner.plan(planner.build_context(memory))
    assert memory.snapshot() == before
    model.generate.assert_not_awaited()


async def test_runtime_budget_rejects_required_step_without_running_tools():
    manager = ContextManager(window_tokens=700, output_tokens=20, safety_tokens=10, token_counter=len)
    from app.tools.tool_registry import ToolRegistry
    memory = WorkingMemory(TaskState('user','goal'))
    planner_model, model = AsyncMock(), AsyncMock()
    planner_model.generate.return_value = '{"steps":["run"],"reason":null}'
    model.generate.return_value = ModelTurn('{"status":"succeeded","content":"done","reason":null}')
    runtime = AgentRuntime(Planner(planner_model), model, memory, ToolRegistry(), context_manager=manager)
    result = await runtime.run()
    assert result.status.value == 'failed'
    model.generate.assert_not_awaited()

async def test_low_level_runtime_budgets_planner_as_well_as_runner():
    from app.tools.tool_registry import ToolRegistry
    model, planner_model = AsyncMock(), AsyncMock()
    manager = ContextManager(window_tokens=500, output_tokens=20, safety_tokens=10, token_counter=len)
    runtime = AgentRuntime(Planner(planner_model), model,
        WorkingMemory(TaskState('user', 'x'*700)), ToolRegistry(), context_manager=manager)
    result = await runtime.run()
    assert result.status.value == 'failed'
    planner_model.generate.assert_not_awaited()

async def test_runner_overflow_retry_does_not_execute_completed_tool_twice(tmp_path):
    from app.agent.step_models import ToolCall, ToolDefinition, ToolOutput
    from app.agent.tool_calling_model import ModelContextOverflowError
    from app.services.conversation_archive import ConversationArchive
    from app.tools.tool_registry import ToolRegistry
    registry=ToolRegistry()
    class ReadTool:
        definition=ToolDefinition('read','Read evidence',{'type':'object','properties':{}})
        invoke=AsyncMock(return_value=ToolOutput(True,'raw evidence '+'x'*20000))
    tool=ReadTool()
    registry.register(tool)
    memory=WorkingMemory(TaskState('user','read and answer'))
    model,planning=AsyncMock(),AsyncMock()
    planning.generate.return_value='{"steps":["read"],"reason":null}'
    model.generate.side_effect=[ModelTurn(tool_calls=(ToolCall('read-once','read',{}),)),
        ModelContextOverflowError('overflow'),
        ModelTurn('{"status":"succeeded","content":"verified","reason":null}')]
    manager=ContextManager(archive=ConversationArchive(tmp_path,'user'),tool_result_chars=200)
    result=await AgentRuntime(Planner(planning),model,memory,registry,context_manager=manager).run()
    assert result.status.value=='completed'
    assert result.replan_count==0 and result.execution_count==1
    tool.invoke.assert_awaited_once()
    assert len(result.steps[0].tool_results[0].content)>20000
    messages=model.generate.call_args_list[1].args[0]
    assert 'storage_ref' in messages[-1].content
    assert 'x'*20000 not in messages[-1].content

async def test_overflow_recovery_deadline_does_not_replan_or_replay_tool(tmp_path):
    import asyncio
    from app.agent.step_models import ToolCall,ToolDefinition,ToolOutput
    from app.agent.tool_calling_model import ModelContextOverflowError
    from app.services.conversation_archive import ConversationArchive
    from app.tools.tool_registry import ToolRegistry
    class WriteTool:
        definition=ToolDefinition('write','write once',{'type':'object','properties':{}})
        invoke=AsyncMock(return_value=ToolOutput(True,'written'))
    tool=WriteTool()
    registry=ToolRegistry()
    registry.register(tool)
    model,planning=AsyncMock(),AsyncMock()
    planning.generate.return_value='{"steps":["write"],"reason":null}'
    calls=0
    async def generate(messages,tools):
        nonlocal calls
        calls+=1
        if calls in (1,4):
            return ModelTurn(tool_calls=(ToolCall('write-'+str(calls),'write',{}),))
        if calls==2:
            raise ModelContextOverflowError('too long')
        if calls==3:
            await asyncio.sleep(10)
        return ModelTurn('{"status":"succeeded","content":"done","reason":null}')
    model.generate.side_effect=generate
    manager=ContextManager(archive=ConversationArchive(tmp_path,'s'))
    result=await AgentRuntime(Planner(planning),model,WorkingMemory(TaskState('s','write')),
        registry,context_manager=manager,step_timeout_seconds=0.05).run()
    assert result.status.value=='failed'
    assert result.replan_count==0
    tool.invoke.assert_awaited_once()
