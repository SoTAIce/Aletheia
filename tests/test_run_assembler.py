"""RunAssembler chooses and assembles one task run for the service (stage 4a; loop only since 4c)."""

import inspect
from types import SimpleNamespace

from app.agent.agent_loop import AgentLoopRunner, LOOP_STEP, LoopOptions, TEAM_STEP
from app.agent.delegation import DelegatingLoopRunner, DelegationOptions, ExploreLoopRunner
from app.agent.step_runner import RunnerDependencies, RunnerLimits
from app.services.rag_agent_service import RagAgentService
from app.services.run_assembler import RunAssembler
from app.tools.tool_registry import ToolRegistry

LIMITS = {'max_executions': 16, 'step_timeout_seconds': 60}


class _Run:
    """Stands in for LoopRun: records how it was built and rebound."""

    def __init__(self, memory, model, registry, **kwargs):
        self.kwargs, self.rebinds = kwargs, []

    def rebind(self, model, registry, *, context_manager=None):
        self.rebinds.append(model)

    async def run(self, on_event=None):
        return SimpleNamespace(status='done')


def _assembler(delegate=False, team=None):
    events = []
    routes, task_options = {}, {}
    assembler = RunAssembler(team_options=team, loop_options=LoopOptions(), delegate=delegate,
                             limits=dict(LIMITS), routes=routes, task_options=task_options,
                             default_workspace=None, emit=lambda sink, event: events.append(event))
    return assembler, events, routes, task_options


async def _run(assembler, mode='single', write=True, runtime=None):
    task = SimpleNamespace(task_id='t1')
    return await assembler.run(key='k', task=task, question='q', runtime=runtime,
                               options={'mode': mode, 'runner': 'loop', 'write': write, 'memory': False},
                               model=object(), memory=object(), registry=ToolRegistry(),
                               context_manager=None, audit=None, on_event=None, loop_run_factory=_Run)


def _runner(runtime):
    deps = RunnerDependencies(SimpleNamespace(generate=lambda *a: None), ToolRegistry(), None, RunnerLimits())
    return runtime.kwargs['runner_factory'](deps)


async def test_a_single_task_is_one_loop_with_one_attempt():
    assembler, events, routes, task_options = _assembler()
    runtime, result = await _run(assembler)
    assert runtime.kwargs['step_description'] == LOOP_STEP and isinstance(_runner(runtime), AgentLoopRunner)
    assert 'max_replans' not in runtime.kwargs and runtime.kwargs['max_executions'] == 16
    assert runtime.kwargs['step_timeout_seconds'] == LoopOptions().timeout_seconds + 5
    assert events == [{'type': 'route', 'mode': 'single', 'read_only': False,
                       'options': {'mode': 'single', 'runner': 'loop', 'write': True, 'memory': False}}]
    assert routes[('k', 't1')].mode == 'single' and task_options[('k', 't1')]['runner'] == 'loop'


async def test_delegate_and_team_choose_their_runners():
    assembler, *_ = _assembler(delegate=True)
    runtime, _ = await _run(assembler)
    assert isinstance(_runner(runtime), ExploreLoopRunner)
    assembler, *_ = _assembler(team=DelegationOptions())
    runtime, _ = await _run(assembler, mode='team')
    assert runtime.kwargs['step_description'] == TEAM_STEP and isinstance(_runner(runtime), DelegatingLoopRunner)


async def test_writes_off_makes_the_task_read_only():
    assembler, events, routes, _ = _assembler()
    await _run(assembler, write=False)
    assert routes[('k', 't1')].read_only is True and events[0]['read_only'] is True


async def test_a_resumed_task_keeps_its_route_and_rebinds():
    assembler, events, routes, _ = _assembler()
    first, _ = await _run(assembler)
    routes[('k', 't1')] = routes[('k', 't1')].__class__('single', 'user', 'kept')
    again, _ = await _run(assembler, runtime=first)
    assert again is first and len(first.rebinds) == 1 and routes[('k', 't1')].rule == 'kept'


def test_query_result_stays_small():
    lines = inspect.getsource(RagAgentService.query_result).splitlines()
    assert len(lines) <= 80, len(lines)


async def test_the_service_runs_loop_and_team_tasks_in_loop_run(monkeypatch):
    """Every task owns a RunMemory and runs in LoopRun (stage 4b wiring; plan mode removed in 4c)."""
    from unittest.mock import AsyncMock
    from app.agent.loop_run import LoopRun
    from app.agent.step_models import ModelTurn
    from app.models.run_state import RunMemory
    import app.services.rag_agent_service as module
    built = []
    def spy(*args, **kwargs):
        instance = LoopRun(*args, **kwargs)
        built.append(instance)
        return instance
    monkeypatch.setattr(module, 'LoopRun', spy)
    for kwargs in ({}, {'execution_mode': 'team', 'team_options': DelegationOptions()}):
        model = AsyncMock()
        model.generate.return_value = ModelTurn('answer')
        async with RagAgentService(model=model, mcp_servers=(), include_knowledge=False,
                                   memory_directory='', **kwargs) as service:
            result = await service.query_result('Explain it', 's')
        assert result.content == 'answer', kwargs
    loop, team = built
    assert isinstance(loop.memory, RunMemory) and isinstance(team.memory, RunMemory)
    assert loop._step_description == LOOP_STEP and team._step_description == TEAM_STEP
