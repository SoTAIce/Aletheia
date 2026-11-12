"""Choose and assemble one task run without owning session lifecycle.

Every task runs in one LoopRun: the single loop (optionally with on-demand delegation) or the team's
lead loop. Plan mode and its planner went in stage 4c (D-076).
"""


def _without_writes(options):
    """Team options for a task whose writes are off: the same as starting without a write root (D-051)."""
    from dataclasses import replace
    from app.agent.route_selection import READ_ONLY_HIDDEN
    return replace(options, allowed_tools=options.allowed_tools - READ_ONLY_HIDDEN,
                   require_passing_run=False)


class RunAssembler:
    def __init__(self, *, team_options, loop_options, delegate, limits,
                 routes, task_options, default_workspace, emit):
        self._team_options = team_options
        self._loop_options = loop_options
        self._delegate = delegate
        self._limits = limits
        self._routes = routes
        self._task_options = task_options
        self._default_workspace = default_workspace
        self._emit = emit

    async def run(self, *, key, task, question, options, runtime, model, memory, registry,
                  context_manager, audit, on_event, loop_run_factory=None):
        # The mode is chosen once per task and kept when a blocked task resumes (D-026).
        choice = self._routes.get((key, task.task_id)) if runtime is not None else None
        team_options = self._team_options
        if team_options is not None and not options['write']:
            team_options = _without_writes(team_options)
        if choice is None:
            choice = await self._choose_route(question, model, options['mode'], team_options)
            if not options['write'] and not choice.read_only:
                # Writes off for this request: the D-027 read-only path, decided by the host.
                from dataclasses import replace
                choice = replace(choice, read_only=True)
        self._routes[(key, task.task_id)] = choice
        self._task_options[(key, task.task_id)] = options
        route = choice.mode
        if audit is not None:
            audit.note(mode=route, read_only=choice.read_only,
                       workspace=options.get('workspace') or self._default_workspace)
        self._emit(on_event, {'type': 'route', 'mode': route, 'read_only': choice.read_only,
                         'options': dict(options)})
        # Read-only tasks lose write tools and get read-only runs (D-027/D-028).
        from app.agent.route_selection import task_registry
        registry = task_registry(registry, choice.read_only)
        # The team owns the whole task through one fixed step (DECISIONS D-019); so does the loop.
        if runtime is None:
            limits = dict(self._limits)
            if route == 'team':
                from app.agent.delegation import DelegatingLoopRunner
                def factory(deps):
                    return DelegatingLoopRunner(deps.model, deps.tool_registry,
                                                team_options, deps.context_manager)
                runner_timeout = team_options.timeout_seconds
            else:
                from app.agent.agent_loop import AgentLoopRunner, LoopResumeStore
                store = LoopResumeStore()  # one per task run, retained by its factory on rebind
                if self._delegate:
                    from app.agent.delegation import ExploreLoopRunner
                    tally = {'delegations': 0}   # per task, kept across rebinds
                    def factory(deps):
                        return ExploreLoopRunner(deps.model, deps.tool_registry, self._loop_options,
                                                 deps.context_manager, store, tally=tally)
                else:
                    def factory(deps):
                        return AgentLoopRunner(deps.model, deps.tool_registry, self._loop_options,
                                               deps.context_manager, store)
                runner_timeout = self._loop_options.timeout_seconds
            # The task's one step must outlast its runner.
            limits['step_timeout_seconds'] = max(limits['step_timeout_seconds'], runner_timeout + 5)
            from app.agent.agent_loop import LOOP_STEP, TEAM_STEP
            from app.agent.loop_run import LoopRun
            build = loop_run_factory or LoopRun
            runtime = build(memory, model, registry, step_description=TEAM_STEP if route == 'team' else LOOP_STEP,
                            runner_factory=factory, **limits, context_manager=context_manager)
        else:
            runtime.rebind(model, registry, context_manager=context_manager)
        result = await runtime.run(on_event=on_event)
        return runtime, result

    async def _choose_route(self, question, model, mode, team_options):
        """single/team are the user's choice; auto applies host rules, then one model call."""
        from app.agent.route_selection import ModeChoice, choose_mode, log_choice
        if mode != 'auto':
            choice = ModeChoice(mode, 'user', mode)
            log_choice(choice)
            return choice
        from app.agent.hooks import model_purpose, trace_scope
        with model_purpose('route'), trace_scope('route'):     # before the task span, no parent (D-073)
            return await choose_mode(question, team_options, model)
