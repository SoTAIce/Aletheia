"""Agent Team execution through the existing Runner contract."""
from app.agent.runner_registry import RunnerDefinition
from app.agent.team.coordinator import TeamCoordinator
from app.agent.team.models import TeamOptions, TeamStore, TeamEnvironment
from dataclasses import replace


class AgentTeamRunner:
    def __init__(self, model, options: TeamOptions, store: TeamStore,
                 environment: TeamEnvironment, context_manager=None):
        if not callable(environment.capture_guard):
            raise ValueError('production team runner requires a parent execution ownership guard')
        self.model, self.options, self.store = model, options, store
        self.environment, self.context_manager = environment, context_manager
        self._repairs = 0

    @property
    def format_repair_count(self):
        return self._repairs

    async def run(self, context):
        state = self.store.get(context, self.options)
        try:
            return await TeamCoordinator(state, self.model, self.environment,
                                         context, self.context_manager).run()
        finally:
            self._repairs = sum((s.lead.format_repairs if s.lead is not None else 0) + sum(
                m.session.format_repairs for m in s.members.values())
                for s in self.store.states.values() if s.key[0] == context.task_id)


def register_team_runner(registry, *, options, store, environment):
    """Synchronous lightweight binding; no model/MCP resources opened here."""
    def bind(deps):
        available = frozenset(d.name for d in deps.tool_registry.list_definitions())
        effective = replace(options, allowed_tools=options.allowed_tools & available)
        return AgentTeamRunner(deps.model, effective, store, environment, deps.context_manager)
    registry.register(RunnerDefinition('agent_team', 'Independent Lead and sequential reviewed team',
        frozenset({'agent_team'})), bind)
