"""Stage 4a: RunAssembler chooses and assembles one task run for the service (docs/stage4_plan.md)."""

import inspect
from types import SimpleNamespace

from app.agent.agent_loop import AgentLoopRunner, LoopOptions, LoopPlanner, TeamPlanner
from app.agent.delegation import DelegatingLoopRunner, DelegationOptions, ExploreLoopRunner
from app.agent.planner import Planner
from app.agent.step_runner import RunnerDependencies, RunnerLimits
from app.services.rag_agent_service import RagAgentService
from app.services.run_assembler import RunAssembler
from app.tools.tool_registry import ToolRegistry

LIMITS = {'max_executions': 16, 'max_replans': 2, 'step_timeout_seconds': 60, 'planning_timeout_seconds': 60}


class _Runtime:
    """Stands in for AgentRuntime: records how it was built and rebound."""

    def __init__(self, planner, model, memory, registry, **kwargs):
        self.planner, self.kwargs, self.rebinds = planner, kwargs, []

    def rebind(self, planner, model, registry, context_manager=None):
        self.planner = planner
        self.rebinds.append(planner)

    async def run(self, on_event=None):
        return SimpleNamespace(status='done')


def _assembler(delegate=False, team=None):
    events = []
    routes, task_options = {}, {}
    assembler = RunAssembler(team_options=team, loop_options=LoopOptions(), delegate=delegate,
                             limits=dict(LIMITS), planner_model_injected=True, routes=routes,
                             task_options=task_options, default_workspace=None,
                             emit=lambda sink, event: events.append(event))
    return assembler, events, routes, task_options


async def _run(assembler, mode='single', runner='loop', write=True, runtime=None):
    task = SimpleNamespace(task_id='t1')
    return await assembler.run(key='k', task=task, question='q', runtime=runtime,
                               options={'mode': mode, 'runner': runner, 'write': write, 'memory': False},
                               planner_model=object(), model=object(), memory=object(), registry=ToolRegistry(),
                               context_manager=None, audit=None, on_event=None, runtime_factory=_Runtime)


def _runner(runtime):
    deps = RunnerDependencies(SimpleNamespace(generate=lambda *a: None), ToolRegistry(), None, RunnerLimits())
    return runtime.kwargs['runner_factory'](deps)


async def test_a_loop_task_gets_the_loop_planner_and_one_attempt():
    assembler, events, routes, task_options = _assembler()
    runtime, result = await _run(assembler)
    assert isinstance(runtime.planner, LoopPlanner) and isinstance(_runner(runtime), AgentLoopRunner)
    assert runtime.kwargs['max_replans'] == 0
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
    assert isinstance(runtime.planner, TeamPlanner) and isinstance(_runner(runtime), DelegatingLoopRunner)


async def test_plan_mode_keeps_the_service_limits():
    assembler, *_ = _assembler()
    runtime, _ = await _run(assembler, runner='plan')
    assert isinstance(runtime.planner, Planner) and 'runner_factory' not in runtime.kwargs
    assert runtime.kwargs == LIMITS | {'context_manager': None}


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
