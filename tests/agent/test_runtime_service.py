import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion
import pytest

from app.agent.openai_compatible_tool_calling_model import OpenAICompatibleToolCallingModel
from app.agent.step_models import ModelTurn, UserMessage
from app.config import config
from app.models.taskstate import TaskStatus
from app.services.rag_agent_service import RagAgentService


@pytest.mark.asyncio
async def test_configured_timeout_reaches_provider_and_runtime(monkeypatch):
    import app.services.rag_agent_service as module

    def respond(request):
        assert request.extensions["timeout"]["read"] == 90.0
        return httpx.Response(200, json=response("Verified").model_dump())

    monkeypatch.setattr(config, "rag_model_timeout_seconds", 90.0)
    monkeypatch.setattr(config, "agent_step_timeout_seconds", 240.0)
    monkeypatch.setattr(config, "agent_planning_timeout_seconds", 120.0)
    monkeypatch.setattr(module, "AsyncOpenAI", lambda **kwargs: AsyncOpenAI(
        **(kwargs | {"api_key": "test-key", "base_url": "https://example.com/v1"}),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    ))
    service = RagAgentService(mcp_servers=(), include_knowledge=False)
    assert service._limits["step_timeout_seconds"] == 240.0
    assert service._limits["planning_timeout_seconds"] == 120.0
    async with service._models() as (_, model):
        turn = await model.generate((UserMessage("Verify timeout configuration"),), ())
        assert turn.content == "Verified"
    override = RagAgentService(step_timeout_seconds=5, planning_timeout_seconds=6)
    assert override._limits["step_timeout_seconds"] == 5
    assert override._limits["planning_timeout_seconds"] == 6


def response(content=None, calls=None):
    return ChatCompletion(id="test", created=0, model="test", object="chat.completion", choices=[{
        "index": 0, "finish_reason": "tool_calls" if calls else "stop",
        "message": {"role": "assistant", "content": content, "tool_calls": calls},
    }])


@pytest.mark.asyncio
async def test_service_provider_adapter_planning_tools_and_session_history():
    create = AsyncMock(side_effect=[
        response('{"steps":["Get current time","Summarize"],"reason":null}'),
        response(calls=[{"id": "clock", "type": "function", "function": {
            "name": "get_current_time", "arguments": '{"timezone":"UTC"}',
        }}]),
        response('{"status":"succeeded","content":"Time verified","reason":null}'),
        response('{"status":"succeeded","content":"Summary based on verified time","reason":null}'),
        response('{"steps":["Use previous answer"],"reason":null}'),
        response('{"status":"succeeded","content":"Follow-up answer","reason":null}'),
    ])
    model = OpenAICompatibleToolCallingModel(
        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), "test",
    )
    service = RagAgentService(model=model, mcp_servers=(), include_knowledge=False)
    first = await service.query_result("Get the time and summarize", "one")
    assert first.status is TaskStatus.COMPLETED and first.execution_count == 2
    assert first.steps[0].tool_results[0].success
    assert "tools" not in create.call_args_list[0].kwargs  # Planner does not execute tools.
    assert create.call_args_list[1].kwargs["tools"][0]["function"]["name"] == "get_current_time"
    messages = create.call_args_list[2].kwargs["messages"]
    assert messages[-1]["role"] == "tool" and messages[-1]["tool_call_id"] == "clock"
    second = await service.query_result("Use your previous answer", "one")
    assert second.status is TaskStatus.COMPLETED and second.task_id != first.task_id
    assert "Summary based on verified time" in create.call_args_list[4].kwargs["messages"][1]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status,event", [("failed", "error"), ("blocked", "blocked")])
async def test_service_stream_does_not_emit_completion_for_failed_or_blocked_work(status, event):
    planner_model, model = AsyncMock(), AsyncMock()
    planner_model.generate.return_value = '{"steps":["Do work"],"reason":null}'
    model.generate.return_value = ModelTurn(json.dumps({
        "status": status, "content": "Work not completed", "reason": "Need evidence or input",
    }))
    service = RagAgentService(model=model, planner_model=planner_model, mcp_servers=(),
                              include_knowledge=False, max_replans=0)
    events = [item async for item in service.query_stream("Do work", "session")]
    assert events[-1]["type"] == event
    assert all(item["type"] != "complete" for item in events)


@pytest.mark.asyncio
async def test_service_conversation_isolation_between_sessions():
    planner_model, model = AsyncMock(), AsyncMock()
    planner_model.generate.return_value = '{"steps":["Answer"],"reason":null}'
    model.generate.return_value = ModelTurn('{"status":"succeeded","content":"Private evidence","reason":null}')
    service = RagAgentService(model=model, planner_model=planner_model, mcp_servers=(), include_knowledge=False)
    await service.query("First goal", "one")
    await service.query("Second goal", "two")
    assert "Private evidence" not in planner_model.generate.call_args.args[0]
    assert len(service._conversations["one"]) == len(service._conversations["two"]) == 1
