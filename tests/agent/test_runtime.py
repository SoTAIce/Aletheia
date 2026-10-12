import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

from mcp import StdioServerParameters
import pytest

from app.agent.planner import Planner
from app.agent.planning_model import PlanningModel
from app.agent.runtime import AgentRuntime
from app.agent.step_models import ModelTurn, ToolCall, ToolDefinition, ToolMessage, ToolOutput
from app.agent.tool_calling_model import ModelResponseError
from app.models.conversation_memory import ConversationMemory, ConversationMessage, ConversationTurn, MessageRole
from app.models.taskstate import StepStatus, TaskState, TaskStatus
from app.models.working_memory import WorkingMemory
from app.tools.tool_loader import local_mcp_servers, open_tool_registry
from app.tools.tool_registry import ToolRegistry


def final(content="Done", status="succeeded", reason=None):
    return ModelTurn(json.dumps({"status": status, "content": content, "reason": reason}))


def plan(*steps):
    return json.dumps({"steps": list(steps), "reason": None})


def fixture(turns, plans=None, registry=None, **limits):
    memory = WorkingMemory(TaskState("user", "Complete the requested goal"))
    model, planner_model = AsyncMock(), AsyncMock()
    model.generate.side_effect = turns
    planner_model.generate.side_effect = plans or [plan("Execute the step")]
    runtime = AgentRuntime(Planner(planner_model), model, memory, registry or ToolRegistry(), **limits)
    return runtime, memory, model, planner_model


def web_server():
    # Production stdio module with only the HTTP transport replaced in its process.
    script = '''
import runpy
import httpx
original = httpx.AsyncClient
def respond(request):
    assert request.headers["Authorization"] == "Bearer test-key"
    return httpx.Response(200, json={"results": [{"title": "Reference", "url": "https://example.com/reference", "content": "Web evidence"}]})
class TestClient(original):
    def __init__(self, **kwargs):
        super().__init__(transport=httpx.MockTransport(respond), **kwargs)
httpx.AsyncClient = TestClient
runpy.run_module("app.tools.web_search.server", run_name="__main__")
'''
    return StdioServerParameters(command=sys.executable, args=["-c", script],
                                 cwd=str(Path(__file__).resolve().parents[2]),
                                 env={"TAVILY_API_KEY": "test-key"})


async def knowledge_search(query, history):
    return "[ref:1] Internal evidence", {"status": "ok", "citations": []}


@pytest.mark.asyncio
async def test_goal_to_completion_with_all_tool_sources_and_live_sessions(tmp_path):
    (tmp_path / "report.txt").write_text("File evidence", encoding="utf-8")
    servers = (*local_mcp_servers(filesystem_root=tmp_path), web_server())
    memory = WorkingMemory(TaskState("user", "Collect and summarize evidence"))
    conversation = ConversationMemory("user")
    planner_model, model = AsyncMock(), AsyncMock()
    planner_model.generate.return_value = plan("Collect evidence", "Summarize the verified findings")
    names = {"retrieve_knowledge", "get_current_time", "read_file", "list_directory", "search_web"}
    calls = (
        ToolCall("rag", "retrieve_knowledge", {"query": "internal"}),
        ToolCall("clock", "get_current_time", {"timezone": "UTC"}),
        ToolCall("file", "read_file", {"path": "report.txt"}),
        ToolCall("web", "search_web", {"query": "reference", "max_results": 1}),
    )
    count = 0
    async def generate(messages, tools):
        nonlocal count
        count += 1
        assert {tool.name for tool in tools} == names
        if count == 1:
            return ModelTurn(tool_calls=calls)
        if count == 2:
            replies = [json.loads(m.content) for m in messages if isinstance(m, ToolMessage)]
            assert len(replies) == 4 and all(reply["success"] for reply in replies)
            assert "Internal evidence" in replies[0]["content"]
            assert "File evidence" in replies[2]["content"]
            assert "Web evidence" in replies[3]["content"]
            return final("Evidence collected")
        context = json.loads(messages[1].content)
        assert context["intermediate_results"][0]["content"] == "Evidence collected"
        assert "Internal evidence" in context["last_observation"]["content"]
        assert "https://example.com/reference" in context["last_observation"]["content"]
        return final("Verified findings from the internal, file and web evidence")
    model.generate.side_effect = generate
    async with open_tool_registry(memory, mcp_servers=servers, conversation=conversation,
                                  knowledge_search=knowledge_search) as registry:
        runtime = AgentRuntime(Planner(planner_model), model, memory, registry, conversation=conversation)
        result = await runtime.run()
        assert result.status is TaskStatus.COMPLETED
        assert result.execution_count == 2 and result.replan_count == 0
        assert len(result.steps) == 2 and result.steps[0].tool_calls == calls
        assert result.steps[0].source_refs
        assert memory.resources.get(result.steps[0].source_refs[0]).loaded
        assert all(step.status is StepStatus.COMPLETED for step in memory.task_state.plan)
        assert memory.task_state.active_step_id is None
        assert len(conversation) == 1
        assert (await runtime.run()) == result
        assert len(conversation) == 1  # terminal reruns do not duplicate turns
        prompt = planner_model.generate.call_args.args[0]
        assert all(name in prompt for name in names)
    for name, arguments in [("read_file", {"path": "report.txt"}), ("search_web", {"query": "late"})]:
        with pytest.raises(RuntimeError, match="closed"):
            await registry.invoke(ToolCall("late", name, arguments))


@pytest.mark.asyncio
async def test_failed_tool_replans_and_preserves_prior_valid_results(tmp_path):
    (tmp_path / "actual.txt").write_text("Recovered evidence", encoding="utf-8")
    memory = WorkingMemory(TaskState("user", "Inspect and summarize a file"))
    planner_model, model = AsyncMock(), AsyncMock()
    planner_model.generate.side_effect = [
        plan("Establish context", "Read missing file", "Summarize"), plan("Read actual file", "Summarize"),
    ]
    model.generate.side_effect = [
        final("Valid prior finding"),
        ModelTurn(tool_calls=(ToolCall("bad", "read_file", {"path": "missing.txt"}),)),
        final("Could not read the required file", "failed", "File missing"),
        ModelTurn(tool_calls=(ToolCall("good", "read_file", {"path": "actual.txt"}),)),
        final("Recovered evidence"), final("Final verified summary"),
    ]
    async with open_tool_registry(memory, mcp_servers=local_mcp_servers(filesystem_root=tmp_path),
                                  include_knowledge=False) as registry:
        result = await AgentRuntime(Planner(planner_model), model, memory, registry).run()
    assert result.status is TaskStatus.COMPLETED
    assert result.execution_count == 4 and result.replan_count == 1
    assert len(result.steps) == 3
    assert "Valid prior finding" in result.content
    assert "Could not read" not in result.content
    failures = memory.task_state.scratchpad.failure_history
    assert len(failures) == 1 and "File missing" in failures[0].message
    assert "File missing" in planner_model.generate.call_args_list[1].args[0]
    assert "Valid prior finding" in planner_model.generate.call_args_list[1].args[0]
    assert "Valid prior finding" in model.generate.call_args_list[3].args[0][1].content


@pytest.mark.asyncio
async def test_raw_tool_exception_is_a_step_failure_then_replanned():
    registry = ToolRegistry()
    tool = SimpleNamespace(definition=ToolDefinition("external", "External", {"type": "object"}),
                           invoke=AsyncMock(side_effect=[ConnectionError("disconnected"), ToolOutput(True, "recovered")]))
    registry.register(tool)
    call = ModelTurn(tool_calls=(ToolCall("call", "external", {}),))
    runtime, memory, model, planner_model = fixture(
        [call, call, final("Recovered")], [plan("Try external"), plan("Use recovered connection")], registry,
    )
    result = await runtime.run()
    assert result.status is TaskStatus.COMPLETED and result.replan_count == 1
    assert "disconnected" in memory.task_state.scratchpad.failure_history[0].message
    assert len(memory.task_state.scratchpad.intermediate_results) == 1
    assert tool.invoke.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("limits,executions,replans", [
    ({"max_replans": 0}, 1, 0), ({"max_replans": 2}, 3, 2),
    ({"max_executions": 2, "max_replans": 5}, 2, 1),
])
async def test_persistent_failure_stops_at_limits(limits, executions, replans):
    runtime, memory, model, planner_model = fixture([], **limits)
    model.generate.side_effect = None
    model.generate.return_value = final("Still failed", "failed", "No evidence")
    planner_model.generate.side_effect = None
    planner_model.generate.return_value = plan("Try again")
    result = await runtime.run()
    assert result.status is TaskStatus.FAILED
    assert result.execution_count == executions and result.replan_count == replans
    assert memory.task_state.active_step_id is None
    assert memory.task_state.scratchpad.intermediate_results == ()
    assert "limit" in result.content


@pytest.mark.asyncio
async def test_blocked_step_waits_for_answer_then_resumes_with_same_budget():
    runtime, memory, model, planner_model = fixture(
        [final("Need a filename", "blocked", "Which file should be inspected?"), final("Inspected actual.txt")],
        [plan("Inspect the chosen file", "Summarize"), plan("Inspect actual.txt")],
    )
    result = await runtime.run()
    assert result.status is TaskStatus.BLOCKED
    assert memory.task_state.plan[0].status is StepStatus.BLOCKED
    assert memory.task_state.plan[1].status is StepStatus.PENDING
    assert memory.task_state.scratchpad.intermediate_results == ()
    assert memory.task_state.scratchpad.failure_history == ()
    await runtime.run()
    assert model.generate.await_count == planner_model.generate.await_count == 1
    question = memory.task_state.scratchpad.open_questions[0]
    memory.task_state.resolve_question(question.question_id, "actual.txt")
    result = await runtime.run()
    assert result.status is TaskStatus.COMPLETED
    assert result.execution_count == 2 and result.replan_count == 1
    assert "actual.txt" in planner_model.generate.call_args_list[1].args[0]
    context = json.loads(model.generate.call_args_list[1].args[0][1].content)
    assert context["resolved_questions"][0]["answer"] == "actual.txt"


@pytest.mark.asyncio
async def test_initial_blocker_prevents_planning_and_tool_calls():
    runtime, memory, model, planner_model = fixture([final()])
    question = memory.task_state.add_open_question("Need input", True)
    assert (await runtime.run()).status is TaskStatus.BLOCKED
    model.generate.assert_not_awaited()
    planner_model.generate.assert_not_awaited()
    memory.task_state.resolve_question(question, "Input provided")
    assert (await runtime.run()).status is TaskStatus.COMPLETED


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    "Search failed; I cannot complete this step.",
    '{"status":"failed","content":"failed","reason":null}',
    '{"status":"succeeded","content":"done","reason":"error"}',
    '{"status":"unknown","content":"done","reason":null}',
    '{"status":"failed","status":"succeeded","content":"done","reason":null}',
])
async def test_unstructured_or_invalid_final_text_cannot_complete_step(response):
    runtime, memory, _, _ = fixture([ModelTurn(response)], max_replans=0)
    result = await runtime.run()
    assert result.status is TaskStatus.FAILED
    assert memory.task_state.plan[0].status is StepStatus.FAILED
    assert memory.task_state.scratchpad.intermediate_results == ()


@pytest.mark.asyncio
async def test_tool_failure_can_be_recovered_within_the_same_step():
    registry = ToolRegistry()
    registry.register(SimpleNamespace(
        definition=ToolDefinition("read", "Read", {"type": "object"}),
        invoke=AsyncMock(side_effect=[ToolOutput(False, "", error="missing"), ToolOutput(True, "evidence")]),
    ))
    runtime, memory, _, _ = fixture([
        ModelTurn(tool_calls=(ToolCall("bad", "read", {}),)),
        ModelTurn(tool_calls=(ToolCall("good", "read", {}),)), final("Recovered within step"),
    ], registry=registry)
    result = await runtime.run()
    assert result.status is TaskStatus.COMPLETED and result.replan_count == 0
    assert [r.success for r in result.steps[0].tool_results] == [False, True]


@pytest.mark.asyncio
async def test_step_timeout_replans_and_clears_active_execution():
    runtime, memory, model, _ = fixture([], [plan("Timed step"), plan("Replacement step")],
                                      step_timeout_seconds=0.02)
    count = 0
    async def generate(*args):
        nonlocal count
        count += 1
        if count == 1:
            await asyncio.Event().wait()
        return final("Recovered after timeout")
    model.generate.side_effect = generate
    result = await runtime.run()
    assert result.status is TaskStatus.COMPLETED and result.replan_count == 1
    assert "timed out" in memory.task_state.scratchpad.failure_history[0].message


@pytest.mark.asyncio
async def test_cancellation_cleans_state_and_propagates():
    runtime, memory, model, _ = fixture([])
    model.generate.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await runtime.run()
    assert memory.task_state.status is TaskStatus.FAILED
    assert memory.task_state.active_step_id is None
    assert memory.task_state.scratchpad.intermediate_results == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", [ValueError("Invalid plan"), TimeoutError("Planner timed out")])
async def test_planning_failure_does_not_start_a_step(cause):
    runtime, memory, model, planner_model = fixture([])
    planner_model.generate.side_effect = cause
    result = await runtime.run()
    assert result.status is TaskStatus.FAILED and result.execution_count == 0
    assert not result.steps and "Planning failed" in result.content
    model.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_conversation_is_imported_as_data_and_saved_as_a_complete_turn():
    conversation = ConversationMemory("user")
    await conversation.add(ConversationTurn([
        ConversationMessage(MessageRole.USER, "Inspect report A"),
        ConversationMessage(MessageRole.ASSISTANT, "Report A contains evidence"),
    ]))
    runtime, memory, model, planner_model = fixture([final("Follow-up completed")], conversation=conversation)
    result = await runtime.run()
    assert result.status is TaskStatus.COMPLETED and len(conversation) == 2
    assert "Report A contains evidence" in planner_model.generate.call_args.args[0]
    assert "Report A contains evidence" in model.generate.call_args.args[0][1].content
    turn = conversation.retrieve(k=1)[0]
    assert [m.role for m in turn.messages] == [MessageRole.USER, MessageRole.ASSISTANT]


@pytest.mark.asyncio
async def test_planning_adapter_reuses_model_without_executing_tools():
    model = AsyncMock()
    model.generate.return_value = ModelTurn(plan("Inspect"))
    result = await PlanningModel(model).generate("Planning prompt")
    assert result == plan("Inspect")
    assert model.generate.call_args.args[1] == ()
    model.generate.return_value = ModelTurn(tool_calls=(ToolCall("bad", "read", {}),))
    with pytest.raises(ModelResponseError, match="without tool calls"):
        await PlanningModel(model).generate("Planning prompt")


@pytest.mark.asyncio
async def test_exact_execution_budget_can_complete_the_last_step():
    runtime, memory, _, _ = fixture([final("Done within budget")], max_executions=1, max_replans=0)
    result = await runtime.run()
    assert result.status is TaskStatus.COMPLETED and result.execution_count == 1


@pytest.mark.asyncio
async def test_planning_timeout_cancels_wait_and_terminates_task():
    runtime, memory, model, planner_model = fixture([], planning_timeout_seconds=0.02)
    async def generate(prompt):
        await asyncio.Event().wait()
    planner_model.generate.side_effect = generate
    result = await runtime.run()
    assert result.status is TaskStatus.FAILED and "TimeoutError" in result.content
    assert result.execution_count == 0
    model.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_with_real_mcp_session_closes_tools(tmp_path):
    memory = WorkingMemory(TaskState("user", "goal"))
    model, planner_model = AsyncMock(), AsyncMock()
    planner_model.generate.return_value = plan("Inspect file")
    model.generate.side_effect = asyncio.CancelledError()
    async with open_tool_registry(memory, mcp_servers=local_mcp_servers(filesystem_root=tmp_path),
                                  include_knowledge=False) as registry:
        runtime = AgentRuntime(Planner(planner_model), model, memory, registry)
        with pytest.raises(asyncio.CancelledError):
            await runtime.run()
    assert memory.task_state.status is TaskStatus.FAILED
    with pytest.raises(RuntimeError, match="closed"):
        await registry.invoke(ToolCall("late", "read_file", {"path": "report.txt"}))
