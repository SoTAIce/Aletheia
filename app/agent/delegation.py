"""Stage 3a: a team lead is an agent loop with host delegation tools.

The v2 contracts for authorization, options and reports remain shared during the
comparison period. Members are fresh loops, never nested runtimes.
"""
import asyncio
import json
from dataclasses import replace

from app.agent.agent_loop import AgentLoopRunner, HostTool, HostToolResult, LoopOptions
from app.agent.builtin_hooks import PassingRunGate
from app.agent.hooks import HookSet, current_hooks, pausable_deadline, trace_scope, using_hooks
from app.agent.runner_registry import RunnerDefinition
from app.agent.step_models import StepOutcome, ToolDefinition
from app.agent.step_runner import StepRunError
from app.agent.team.diagnostics import failure_details, failure_types
from app.agent.team_v2 import (
    DelegationOptions, DelegatingTeamRunner, READ_TOOLS, RUNNER_ID,
    _Observed, _Run, _Seen, _evidence, _TOOL_NAME, _UNAVAILABLE,
)
from app.tools.tool_registry import ToolRegistry


class DelegatingLoopRunner(DelegatingTeamRunner):
    """Shares v2's host contracts, replaces its lead and member loops only."""

    def __init__(self, model, tool_registry, options: DelegationOptions, context_manager=None):
        if isinstance(options, DelegationOptions) and options.acceptance_tests:
            raise ValueError('team_impl loop does not support acceptance_tests')
        super().__init__(model, tool_registry, options, context_manager)
        # Control tools can never become grants, even with a custom parent registry.
        self.worker_tools -= {'ask_user', 'todo', 'delegate', 'review', 'review_decision'}
        self.review_tools &= self.worker_tools

    def _worker_brief(self, task, role):
        brief = super()._worker_brief(task, role)
        if role == 'reviewer':
            brief = brief.replace(
                'Final status "succeeded" means you approve; "failed" with reason means revise, and the '
                'reason must list the concrete problems.',
                'Call review_decision(decision, problems) with decision "approve" or "revise" and a '
                'list of concrete problems. Without this call the host treats the review as revise.')
        return brief

    def _state(self, run):
        return {'writes': run.writes, 'passing_run_after_last_write': self._gate(run),
                'review_after_last_write': run.approved_at == run.writes}

    async def _delegate(self, run, context, task, tools, role):
        o = self.options
        run.members += 1
        run.budget['delegations'] += 1
        name = f'{role}-{run.members}'
        registry, seen = ToolRegistry(), []
        for tool_name in sorted(tools):
            registry.register(_Observed(self.registry.get(tool_name), tool_name, seen))
        decision = {'value': 'revise', 'problems': []}

        async def decide(arguments):
            value, problems = arguments.get('decision'), arguments.get('problems')
            if value not in ('approve', 'revise') or not isinstance(problems, list) or not all(
                    isinstance(p, str) for p in problems):
                return HostToolResult('HOST: decision must be approve or revise; problems must be a list of strings')
            decision.update(value=value, problems=problems)
            return HostToolResult('HOST: review decision recorded')

        host_tools = ()
        if role == 'reviewer':
            definition = ToolDefinition('review_decision', 'Record the review conclusion.',
                {'type': 'object', 'properties': {'decision': {'type': 'string', 'enum': ['approve', 'revise']},
                 'problems': {'type': 'array', 'items': {'type': 'string'}}},
                 'required': ['decision', 'problems']})
            host_tools = (HostTool(definition, decide),)
        runner = AgentLoopRunner(self.model, registry,
            LoopOptions(o.worker_tool_rounds, o.worker_tool_calls, o.timeout_seconds), self.context_manager,
            host_tools=host_tools, allow_ask_user=False, instructions=self._worker_brief(task, role),
            finish_hooks=False)
        entry = {'member': name, 'role': role, 'tools': sorted(tools)}
        try:
            with trace_scope('member', member=name):
                output = await runner.run(context)
        except StepRunError as exc:
            failure = failure_types(failure_details(exc))
            report = {'member': name, 'status': 'error', 'failure': failure}
            entry.update(status='error', failure=failure)
            if seen:
                report['note'] = 'the worker ended without a valid report; the host kept the tool results it observed'
            # Only accept the host-shaped unavailable-tool message and a bounded identifier.
            message = exc.message
            if message.startswith(_UNAVAILABLE) and _TOOL_NAME.fullmatch(message[len(_UNAVAILABLE):]):
                report['unavailable_tool'] = entry['unavailable_tool'] = message[len(_UNAVAILABLE):]
                report['note'] = 'the worker called a tool it was not granted; grant it or tell the next worker not to use it'
        else:
            report = {'member': name, 'status': 'succeeded', 'result': output.content[:o.report_chars]}
            entry['status'] = 'succeeded'
            for call, result in zip(output.tool_calls, output.tool_results):
                run.calls.append(replace(call, call_id=f'{name}/{call.call_id}'))
                run.results.append(replace(result, call_id=f'{name}/{result.call_id}'))
            run.refs.extend(r for r in output.source_refs if r not in run.refs)
            if role == 'worker':
                run.last_success = output.content
        finally:
            self.format_repair_count += runner.format_repair_count
        run.gate_results.extend(seen)
        run.writes += sum(r.tool_name == 'write_file' and r.success for r in seen)
        run.budget['tool_calls'] += sum(isinstance(r, _Seen) for r in seen)
        written, last_run = _evidence(seen)
        if written:
            report['files_written'] = written
        if last_run:
            report['last_run'] = last_run
        if role == 'reviewer':
            approved = report['status'] == 'succeeded' and decision['value'] == 'approve'
            run.approved_at = run.writes if approved else None
            report['decision'] = entry['decision'] = 'approve' if approved else 'revise'
            report['problems'] = decision['problems']
        entry.update(tool_calls=len(seen), tool_sequence=[[r.tool_name, bool(r.success)] for r in seen][-40:])
        run.trace.append(entry)
        return self._finish_report(run, report)

    async def _call_host(self, run, context, name, arguments, gate):
        role = 'worker' if name == 'delegate' else 'reviewer'
        if run.budget['delegations'] >= self.options.max_delegations:
            return HostToolResult('HOST: delegation limit reached; give your final answer now.')
        key = 'task' if role == 'worker' else 'focus'
        task = arguments.get(key)
        if not isinstance(task, str) or not task.strip():
            return HostToolResult(f'HOST: {key} must be a nonempty string.')
        tools, problem = self._grant(arguments, role)
        if problem:
            return HostToolResult('HOST: ' + problem)
        if role == 'reviewer' and run.last_success is not None:
            task += '\nLatest worker report:\n' + run.last_success[:self.options.report_chars]
        report = await self._delegate(run, context, task, tools, role)
        accepted = await gate.check(self._state(run))
        # Read-only assignments need the lead to format the user's final answer.
        answer = None
        if (run.writes > 0 and report['status'] == 'succeeded' and accepted.allow
                and (self.options.require_passing_run or self.options.review) and run.last_success is not None):
            answer = run.last_success
        return HostToolResult(json.dumps(report, ensure_ascii=False), answer)

    async def run(self, context):
        run = _Run()
        self.format_repair_count = 0
        gate = PassingRunGate(require_passing_run=self.options.require_passing_run, review=self.options.review)
        parent = current_hooks()
        hooks = HookSet(parent.observers, (*parent.gates, gate), gate_timeout=parent.gate_timeout)
        tools = []
        for definition in self._lead_tools():
            async def invoke(arguments, name=definition.name):
                return await self._call_host(run, context, name, arguments, gate)
            tools.append(HostTool(definition, invoke))
        # A fresh lead has no workspace tools; its only actions are host delegation and todo.
        lead = AgentLoopRunner(self.model, ToolRegistry(),
            LoopOptions(max_tool_rounds=self.options.max_lead_turns,
                        max_tool_calls=self.options.max_lead_turns, timeout_seconds=self.options.timeout_seconds),
            self.context_manager, host_tools=tuple(tools), allow_ask_user=False,
            instructions=(self._lead_system().split(' When the work is done,')[0] +
                          ' Automatic completion requires a successful write in this run; without writes, '
                          'use the reports to answer the active goal yourself in its requested format. '
                          'Reply with the final answer as plain text, without a status envelope.'),
            finish_state=lambda: self._state(run))
        try:
            with using_hooks(hooks):
                async with asyncio.timeout(self.options.timeout_seconds) as deadline:
                    with pausable_deadline(deadline):
                        output = await lead.run(context)
            return replace(output, observation=json.dumps({'team': 'loop',
                'delegations': run.budget['delegations'], 'members': [e['member'] for e in run.trace]}),
                tool_calls=tuple(run.calls), tool_results=tuple(run.results), source_refs=tuple(run.refs))
        finally:
            self.format_repair_count += lead.format_repair_count


def register_delegating_loop_runner(registry, *, options):
    def bind(deps):
        return DelegatingLoopRunner(deps.model, deps.tool_registry, options, deps.context_manager)
    registry.register(RunnerDefinition(RUNNER_ID, 'Agent loop with host delegation tools',
                                       frozenset({'agent_team'})), bind)
