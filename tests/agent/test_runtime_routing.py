import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.planner import Planner
from app.agent.runtime import AgentRuntime
from app.agent.step_models import ModelTurn, StepRunOutput
from app.models.taskstate import TaskState, TaskStatus, StepStatus
from app.models.working_memory import WorkingMemory, StaleExecutionError
from app.services.context_manager import ContextManager
from app.tools.tool_registry import ToolRegistry


def fixture(**kwargs):
    model, planner_model = AsyncMock(), AsyncMock()
    model.generate.return_value = ModelTurn('{"status":"succeeded","content":"done","reason":null}')
    planner_model.generate.return_value = '{"steps":["Do work"],"reason":null}'
    memory = WorkingMemory(TaskState('u', 'goal'))
    runtime = AgentRuntime(Planner(planner_model), model, memory, ToolRegistry(), **kwargs)
    return runtime, memory, model, planner_model


def fake_registry(run):
    from app.agent.runner_registry import RunnerRegistry, RunnerDefinition
    registry = RunnerRegistry()
    registry.register(RunnerDefinition('tool_calling', 'Test only', frozenset({'tool_calling'})),
                      lambda deps: SimpleNamespace(run=run))
    return registry


async def test_routing_error_marks_step_failed_and_terminates_without_replan():
    from app.agent.runner_router import RunnerRequirements
    runtime, memory, model, planner = fixture(
        runner_requirements=lambda ctx: RunnerRequirements(frozenset({'not_registered'})))
    result = await runtime.run()
    assert result.status is TaskStatus.FAILED and result.replan_count == 0
    assert result.execution_count == 1
    assert memory.task_state.plan[0].status is StepStatus.FAILED
    assert memory.task_state.active_step_id is None
    assert 'StepRoutingError' in result.content
    assert 'StepRoutingError' in memory.task_state.scratchpad.failure_history[0].message
    planner.generate.assert_awaited_once()
    model.generate.assert_not_awaited()


def test_init_and_rebind_use_same_factory_inputs_and_atomic_replacement():
    from app.agent.runner_registry import RunnerRegistry, RunnerDefinition
    seen = []
    reject = False
    def factory(deps):
        seen.append(deps)
        if reject:
            raise ValueError('binding rejected')
        return SimpleNamespace(run=AsyncMock(return_value=StepRunOutput('done')))
    registry = RunnerRegistry()
    registry.register(RunnerDefinition('tool_calling', 'Test only'), factory)
    runtime, memory, model, planner = fixture(runner_registry=registry, max_tool_rounds=2, max_total_tool_calls=3)
    old = runtime._runner
    replacement_model, tools, manager = AsyncMock(), ToolRegistry(), ContextManager()
    reject = True
    with pytest.raises(ValueError):
        runtime.rebind(Planner(planner), replacement_model, tools, context_manager=manager)
    assert runtime._runner is old
    reject = False
    runtime.rebind(Planner(planner), replacement_model, tools, context_manager=manager)
    assert seen[-1].model is replacement_model and seen[-1].tool_registry is tools
    assert seen[-1].context_manager is manager and seen[-1].limits == seen[0].limits
    assert seen[-1].limits.max_tool_rounds == 2
    assert seen[-1].limits.max_total_tool_calls == 3
    assert old is not runtime._runner


async def test_rebind_after_blocked_uses_new_clients_preserves_counts_and_diagnostics():
    from app.agent.routing_step_runner import RoutingStepRunner
    runtime, memory, model, planner = fixture()
    model.generate.return_value = ModelTurn('{"status":"blocked","content":"Which file?","reason":"Need file"}')
    blocked = await runtime.run()
    assert blocked.status is TaskStatus.BLOCKED
    question = memory.task_state.scratchpad.open_questions[0]
    memory.task_state.resolve_question(question.question_id, 'report.txt')
    replacement = AsyncMock()
    replacement.generate.return_value = ModelTurn('{"status":"succeeded","content":"new client result","reason":null}')
    runtime.rebind(Planner(planner), replacement, ToolRegistry(), context_manager=ContextManager())
    result = await runtime.run()
    assert result.status is TaskStatus.COMPLETED and result.execution_count == 2 and result.replan_count == 1
    assert model.generate.await_count == 1 and replacement.generate.await_count == 1
    assert isinstance(runtime._runner, RoutingStepRunner)
    assert [d.runner_id for d in runtime.routing_decisions] == ['tool_calling', 'tool_calling']
    assert all(d.task_id == result.task_id for d in runtime.routing_decisions)


@pytest.mark.parametrize('late_failure', [False, True])
async def test_cancelled_runner_cannot_commit_even_when_it_swallows_cancellation(late_failure):
    from app.agent.step_runner import StepToolError
    entered, release = asyncio.Event(), asyncio.Event()
    async def run(context):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        if late_failure:
            raise StepToolError('late failed output')
        return StepRunOutput('late success')
    runtime, memory, model, planner = fixture(runner_registry=fake_registry(run))
    task = asyncio.create_task(runtime.run())
    await entered.wait()
    with pytest.raises(RuntimeError):
        runtime.rebind(Planner(planner), AsyncMock(), ToolRegistry(), context_manager=ContextManager())
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert memory.task_state.status is TaskStatus.FAILED
    assert memory.task_state.plan[0].status is StepStatus.FAILED
    assert memory.task_state.active_step_id is None
    assert memory.task_state.scratchpad.intermediate_results == ()


async def test_timeout_rejects_suppressed_cancellation_result():
    entered = asyncio.Event()
    async def run(context):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return StepRunOutput('must not commit timed-out output')
    runtime, memory, model, planner = fixture(runner_registry=fake_registry(run),
                                            step_timeout_seconds=.02, max_replans=0)
    result = await runtime.run()
    assert result.status is TaskStatus.FAILED
    assert memory.task_state.scratchpad.intermediate_results == ()
    assert memory.task_state.active_step_id is None


@pytest.mark.parametrize('mode', ['success', 'cancel', 'declared_failure', 'exception', 'invalid_output'])
async def test_stale_output_or_cancel_cleanup_never_fails_new_goal(mode):
    entered, release = asyncio.Event(), asyncio.Event()
    async def run(context):
        entered.set()
        await release.wait()
        if mode == 'declared_failure':
            from app.agent.step_runner import StepToolError
            raise StepToolError('old declared failure')
        if mode == 'exception':
            raise ValueError('old unexpected failure')
        if mode == 'invalid_output':
            return object()
        return StepRunOutput('old goal result')
    runtime, memory, model, planner = fixture(runner_registry=fake_registry(run))
    task = asyncio.create_task(runtime.run())
    await entered.wait()
    memory.record_failure('Old execution superseded')
    memory.update_goal('new goal')
    before = memory.snapshot()
    if mode == 'cancel':
        task.cancel()
    release.set()
    with pytest.raises(StaleExecutionError):
        await task
    assert memory.snapshot() == before


async def test_old_binding_is_invalid_after_rebind():
    runtime, memory, model, planner = fixture()
    old = runtime._runner
    runtime.rebind(Planner(planner), AsyncMock(), ToolRegistry(), context_manager=ContextManager())
    with pytest.raises(StaleExecutionError):
        await old.run(None)


async def test_service_injects_trusted_requirements_not_from_model(tmp_path):
    from app.agent.runner_router import RunnerRequirements
    from app.services.rag_agent_service import RagAgentService
    model, planner = AsyncMock(), AsyncMock()
    planner.generate.return_value = '{"steps":["Do work"],"reason":null}'
    async with RagAgentService(model=model, planner_model=planner, mcp_servers=(), include_knowledge=False,
                                memory_directory=tmp_path,
                                runner_requirements=lambda ctx: RunnerRequirements(frozenset({'unavailable'}))) as service:
        result = await service.query_result('goal', 's')
    assert result.status is TaskStatus.FAILED and result.replan_count == 0
    model.generate.assert_not_awaited()


async def test_service_stale_cleanup_preserves_new_task_and_records_old_turn(tmp_path, monkeypatch):
    import app.services.rag_agent_service as module
    from app.models.conversation_memory import TurnStatus
    entered, release = asyncio.Event(), asyncio.Event()
    captured = []
    class Runtime(AgentRuntime):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            captured.append(self.memory)
    async def run(context):
        entered.set()
        await release.wait()
        raise ValueError('late invalid implementation result')
    monkeypatch.setattr(module, 'AgentRuntime', Runtime)
    planner = AsyncMock()
    planner.generate.return_value = '{"steps":["Do work"],"reason":null}'
    async with module.RagAgentService(model=AsyncMock(), planner_model=planner, mcp_servers=(),
                                      include_knowledge=False, memory_directory=tmp_path,
                                      runner_registry=fake_registry(run)) as service:
        task = asyncio.create_task(service.query_result('original goal', 's', request_id='old'))
        await entered.wait()
        memory = captured[0]
        memory.record_failure('Superseded by the application')
        memory.update_goal('new goal')
        before = memory.snapshot()
        release.set()
        with pytest.raises(StaleExecutionError):
            await task
        assert memory.snapshot() == before
        turn = service._conversations['s'].archive.read_turn('request:old')
        assert turn.status is TurnStatus.FAILED and turn.messages[0].content == 'original goal'
