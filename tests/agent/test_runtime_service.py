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
    monkeypatch.setattr(module, "AsyncOpenAI", lambda **kwargs: AsyncOpenAI(
        **(kwargs | {"api_key": "test-key", "base_url": "https://example.com/v1"}),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    ))
    service = RagAgentService(mcp_servers=(), include_knowledge=False)
    assert service._limits["step_timeout_seconds"] == 240.0
    assert "planning_timeout_seconds" not in service._limits    # plan mode went in stage 4c
    async with service._models() as model:
        turn = await model.generate((UserMessage("Verify timeout configuration"),), ())
        assert turn.content == "Verified"
    override = RagAgentService(step_timeout_seconds=5)
    assert override._limits["step_timeout_seconds"] == 5


def response(content=None, calls=None):
    return ChatCompletion(id="test", created=0, model="test", object="chat.completion", choices=[{
        "index": 0, "finish_reason": "tool_calls" if calls else "stop",
        "message": {"role": "assistant", "content": content, "tool_calls": calls},
    }])


@pytest.mark.asyncio
async def test_service_provider_adapter_tools_and_session_history():
    create = AsyncMock(side_effect=[
        response(calls=[{"id": "clock", "type": "function", "function": {
            "name": "get_current_time", "arguments": '{"timezone":"UTC"}',
        }}]),
        response("Summary based on verified time"),
        response("Follow-up answer"),
    ])
    model = OpenAICompatibleToolCallingModel(
        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), "test",
    )
    service = RagAgentService(model=model, mcp_servers=(), include_knowledge=False)
    first = await service.query_result("Get the time and summarize", "one")
    assert first.status is TaskStatus.COMPLETED and first.execution_count == 1
    assert first.content == "Summary based on verified time" and first.steps[0].tool_results[0].success
    names = [t["function"]["name"] for t in create.call_args_list[0].kwargs["tools"]]
    assert "get_current_time" in names
    messages = create.call_args_list[1].kwargs["messages"]
    assert messages[-1]["role"] == "tool" and messages[-1]["tool_call_id"] == "clock"
    second = await service.query_result("Use your previous answer", "one")
    assert second.status is TaskStatus.COMPLETED and second.task_id != first.task_id
    request = json.dumps(create.call_args_list[2].kwargs["messages"], ensure_ascii=False)
    assert "Summary based on verified time" in request          # the earlier turn reaches the next task


@pytest.mark.asyncio
@pytest.mark.parametrize("status,event", [("failed", "error"), ("blocked", "blocked")])
async def test_service_stream_does_not_emit_completion_for_failed_or_blocked_work(status, event):
    from app.agent.step_models import ToolCall
    from app.agent.tool_calling_model import ModelRequestError
    model = AsyncMock()
    if status == "blocked":
        model.generate.return_value = ModelTurn(tool_calls=(ToolCall("q", "ask_user", {"question": "Which input?"}),))
    else:
        model.generate.side_effect = ModelRequestError("provider refused")
    service = RagAgentService(model=model, mcp_servers=(), include_knowledge=False)
    events = [item async for item in service.query_stream("Do work", "session")]
    assert events[-1]["type"] == event
    assert all(item["type"] != "complete" for item in events)


@pytest.mark.asyncio
async def test_service_conversation_isolation_between_sessions():
    model = AsyncMock()
    model.generate.return_value = ModelTurn('Private evidence')
    service = RagAgentService(model=model, mcp_servers=(), include_knowledge=False)
    await service.query("First goal", "one")
    await service.query("Second goal", "two")
    second_request = json.dumps([m.content for m in model.generate.call_args.args[0]], ensure_ascii=False)
    assert "Private evidence" not in second_request and "First goal" not in second_request
    assert len(service._conversations["one"]) == len(service._conversations["two"]) == 1
