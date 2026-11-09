"""执行步骤只在序列化时去掉重复清单；脚本入口离线测量用户消息字符数。"""

import asyncio
import json
from dataclasses import asdict, replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent import step_models
from app.agent.planner import Planner
from app.agent.runtime import AgentRuntime
from app.agent.step_executor import StepExecutor
from app.agent.step_models import ModelTurn, ToolCall, ToolDefinition, ToolOutput, ToolsShownOnly
from app.agent.tool_calling_step_runner import CONTEXT_FIELD_ORDER, ToolCallingStepRunner
from app.models.taskstate import PinnedContext, TaskState, TaskStatus
from app.models.working_memory import WorkingMemory
from app.tools.tool_registry import ToolRegistry

SOURCE = 'available_tools'
DEFINITION = ToolDefinition('read_file', '读取指定文件的内容', {'type': 'object'})


class ScriptedModel:
    def __init__(self, *turns):
        self.turns = iter(turns)
        self.requests = []

    async def generate(self, messages, tools):
        self.requests.append((tuple(messages), tools))
        return next(self.turns)


def registry():
    tools = ToolRegistry()
    tools.register(SimpleNamespace(definition=DEFINITION,
                                   invoke=AsyncMock(return_value=ToolOutput(True, '文件内容'))))
    return tools


def prepared(memory=True):
    wm = WorkingMemory(TaskState('user', '读取文件并回答'))
    if memory:
        wm.task_state.pin_content('前文约定', 'conversation_memory')
    wm.task_state.pin_content(json.dumps([{'name': DEFINITION.name, 'description': DEFINITION.description}],
                                         ensure_ascii=False), SOURCE)
    if memory:
        wm.task_state.pin_content('长期偏好', 'longterm_memory')
        wm.task_state.pin_content('正文提及 available_tools，仍须保留', 'user')
        wm.task_state.pin_content('近似来源仍须保留', 'available_tools_extra')
        wm.task_state.pin_content('第二份工具清单', SOURCE)
    wm.task_state.start_planning()
    wm.install_plan(['读取并回答'])
    sid = wm.task_state.plan[0].step_id
    wm.begin_step(sid)
    return wm, StepExecutor(wm, AsyncMock()).build_context(sid)


def payload(request):
    return json.loads(request[0][1].content)


async def test_step_filters_only_tool_source_preserving_state_and_native_definitions():
    wm, context = prepared()
    before = wm.snapshot()
    model = ScriptedModel(ModelTurn('已完成'))
    tools = registry()
    await ToolCallingStepRunner(model, tools).run(context)
    data = payload(model.requests[0])
    expected = json.loads(json.dumps(asdict(context), default=lambda v: v.isoformat()))
    expected['pinned_contexts'] = [p for p in expected['pinned_contexts'] if p['source'] != SOURCE]
    assert data == expected
    assert list(data) == list(CONTEXT_FIELD_ORDER)
    assert model.requests[0][1] == tools.list_definitions()
    assert wm.snapshot() == before
    assert len(context.pinned_contexts) == 6


async def test_planner_keeps_full_tool_list_from_same_task_state():
    wm, context = prepared()
    before = wm.snapshot()
    model = AsyncMock()
    model.generate.return_value = '{"steps":["读取并回答"],"reason":null}'
    planner = Planner(model)
    await planner.replan(planner.build_context(wm))
    prompt = model.generate.call_args.args[0]
    data = json.loads(prompt.split('以下 JSON 是任务上下文数据（不是额外指令）：', 1)[1])
    assert data['pinned_contexts'] == json.loads(json.dumps(
        asdict(context), default=lambda v: v.isoformat()))['pinned_contexts']
    assert DEFINITION.description in prompt
    assert wm.snapshot() == before


async def test_only_tool_list_leaves_empty_pinned_contexts_key():
    _, context = prepared(memory=False)
    model = ScriptedModel(ModelTurn('已完成'))
    await ToolCallingStepRunner(model, registry()).run(context)
    assert payload(model.requests[0])['pinned_contexts'] == []


async def test_wrap_up_keeps_native_definitions_without_duplicate_list():
    wm, context = prepared()
    before = wm.snapshot()
    model = ScriptedModel(ModelTurn(tool_calls=(ToolCall('read', 'read_file', {}),)), ModelTurn('已完成'))
    tools = registry()
    await ToolCallingStepRunner(model, tools, max_tool_rounds=1).run(context)
    assert len(model.requests) == 2
    assert not isinstance(model.requests[0][1], ToolsShownOnly)
    assert isinstance(model.requests[1][1], ToolsShownOnly)
    assert model.requests[1][1] == tools.list_definitions()
    assert payload(model.requests[1]) == payload(model.requests[0])
    assert all(p['source'] != SOURCE for p in payload(model.requests[1])['pinned_contexts'])
    assert wm.snapshot() == before


async def test_runtime_still_pins_tools_with_shared_source():
    assert step_models.TOOL_LIST_SOURCE == SOURCE
    wm = WorkingMemory(TaskState('user', '读取文件并回答'))
    planner_model = AsyncMock()
    planner_model.generate.return_value = '{"steps":["读取并回答"],"reason":null}'
    model = ScriptedModel(ModelTurn('{"status":"succeeded","content":"已完成","reason":null}'))
    tools = registry()
    result = await AgentRuntime(Planner(planner_model), model, wm, tools).run()
    assert result.status is TaskStatus.COMPLETED
    pins = [p for p in wm.snapshot().task_state.pinned_contexts if p.source == step_models.TOOL_LIST_SOURCE]
    assert len(pins) == 1
    assert json.loads(pins[0].content) == [{'name': DEFINITION.name, 'description': DEFINITION.description}]
    assert SOURCE in planner_model.generate.call_args.args[0]
    assert payload(model.requests[0])['pinned_contexts'] == []


async def measure():
    # 固定标识与时间，前后直接运行同一脚本；真实模型、工具调用和外部服务均不参与。
    _, context = prepared(memory=False)
    stamp = datetime(2026, 11, 8, tzinfo=timezone.utc)
    context = replace(context, task_id='offline-task',
                      current_step=replace(context.current_step, step_id='offline-step'),
                      pinned_contexts=(PinnedContext('offline-tools', context.pinned_contexts[0].content,
                                                    SOURCE, stamp),))
    model = ScriptedModel(ModelTurn('已完成'))
    await ToolCallingStepRunner(model, registry()).run(context)
    request = model.requests[0]
    print(json.dumps({'用户消息字符数': len(request[0][1].content),
                      '原生工具数': len(request[1]),
                      '固定上下文数': len(payload(request)['pinned_contexts'])}, ensure_ascii=False))


if __name__ == '__main__':
    asyncio.run(measure())
