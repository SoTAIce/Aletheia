"""Two-experiment harness, intentionally not a general TeamCoordinator."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import json

from app.agent.step_models import ToolDefinition, ToolOutput
from app.agent.team_poc.session import TeamSession, SessionError
from app.tools.tool_registry import ToolRegistry


@dataclass(frozen=True, slots=True)
class TrialResult:
    worker: object
    helper: object
    lead: object


class _LeadTool:
    def __init__(self, name, description, schema, action):
        self.definition = ToolDefinition(name, description, schema)
        self.action = action

    async def invoke(self, arguments):
        return ToolOutput(True, self.action(arguments))


class CoordinationTrial:
    """One Worker report -> one Lead decision -> one helper + revised Worker.

    The helper's initial assignment is authored by the Lead, not this harness.
    Revision is prepared during the Lead turn and committed after its decision,
    so no Worker action races an incompletely specified adjustment. No task
    board, persistent bus, recursive spawning, or production API is provided.
    """

    def __init__(self, worker, lead_model, helper_factory, *, timeout_seconds=120):
        self.worker, self.lead_model, self.helper_factory = worker, lead_model, helper_factory
        self.timeout_seconds = timeout_seconds
        self.lead = self.helper = None
        self.worker_unfinished_at_report = False
        self.helper_started_before_worker_completion = False
        self.report = None
        self._pending_revision = None
        self._tasks = []
        self._worker_task = None
        self._event_reader = None
        self._used = False

    def _revise(self, args):
        if set(args) != {'expected_version', 'instruction'}:
            raise SessionError('revise_worker requires expected_version and instruction')
        if type(args['expected_version']) is not int or args['expected_version'] != self.worker.version:
            raise SessionError('stale Lead revision')
        if self._pending_revision is not None:
            raise SessionError('only one revision is admitted in this trial')
        if not isinstance(args['instruction'], str) or not args['instruction'].strip():
            raise SessionError('instruction must be nonempty')
        self._pending_revision = (args['expected_version'], args['instruction'])
        return 'Revision prepared; Worker remains paused until Lead decision completes.'

    def _start_helper(self, args):
        if set(args) != {'instruction'} or not isinstance(args['instruction'], str) or not args['instruction'].strip():
            raise SessionError('start_helper requires only a nonempty instruction')
        if self.helper is not None:
            raise SessionError('one helper limit reached')
        self.helper = self.helper_factory(args['instruction'])
        if not isinstance(self.helper, TeamSession) or self.helper.session_id in (self.worker.session_id, 'lead'):
            raise SessionError('helper must be an independently named TeamSession')
        if self.helper.event_queue is not self.worker.event_queue:
            raise SessionError('helper must use the trial event queue')
        self.helper_started_before_worker_completion = not self._worker_task.done()
        self._helper_task = asyncio.create_task(self.helper.run())
        self._tasks.append(self._helper_task)
        return 'Helper started. Do not wait for its final result in this Lead decision.'

    @asynccontextmanager
    async def _lead_tools(self):
        registry = ToolRegistry()
        registry.register(_LeadTool('revise_worker', 'Prepare revised instructions for the paused Worker.',
            {'type': 'object', 'properties': {'expected_version': {'type': 'integer'},
                                           'instruction': {'type': 'string'}},
             'required': ['expected_version', 'instruction'], 'additionalProperties': False}, self._revise))
        registry.register(_LeadTool('start_helper', 'Start one independent helper with a focused assignment.',
            {'type': 'object', 'properties': {'instruction': {'type': 'string'}},
             'required': ['instruction'], 'additionalProperties': False}, self._start_helper))
        yield registry

    async def _wait_report(self):
        while True:
            self._event_reader = asyncio.create_task(self.worker.event_queue.get())
            done, _ = await asyncio.wait((self._worker_task, self._event_reader),
                                         return_when=asyncio.FIRST_COMPLETED)
            if self._worker_task in done:
                self._worker_task.result()  # Propagate actual Worker failure.
                raise SessionError('Worker finished without requesting coordination')
            event = self._event_reader.result()
            if event.session_id == self.worker.session_id and event.kind == 'coordination_requested':
                return event

    async def _finish_members(self):
        pending = {self._worker_task, self._helper_task}
        while pending:
            self._event_reader = asyncio.create_task(self.worker.event_queue.get())
            done, _ = await asyncio.wait((*pending, self._event_reader),
                                         return_when=asyncio.FIRST_COMPLETED)
            for task in done & pending:
                task.result()
                pending.remove(task)
            if self._event_reader in done:
                event = self._event_reader.result()
                if event.kind == 'coordination_requested':
                    raise SessionError('additional coordination requires a multi-cycle coordinator; outside this PoC')
            else:
                self._event_reader.cancel()
                await asyncio.gather(self._event_reader, return_exceptions=True)
        return self._worker_task.result(), self._helper_task.result()

    async def run(self):
        if self._used:
            raise SessionError('coordination trial is single-use')
        self._used = True
        self._worker_task = asyncio.create_task(self.worker.run())
        self._tasks.append(self._worker_task)
        try:
            async with asyncio.timeout(self.timeout_seconds):
                self.report = await self._wait_report()
                self.worker_unfinished_at_report = not self._worker_task.done() and self.worker.status == 'waiting'
                prompt = (
                    'You are the independent Team Lead. A Worker is paused mid-execution. '
                    'Analyze its report, call revise_worker with a concrete change and start_helper '
                    'with an independent verification assignment, then finish this decision. '
                    'Do not call request_coordination yourself. Worker completed evidence is retained. '
                    'Preserve the original task scope and exact output contract. Do not invent additional '
                    'business operations or deliverables. The helper checks independently in parallel; '
                    'its output is compared by the host, not relayed to the Worker in this bounded trial. '
                    'Do not make Worker completion depend on an undelivered helper message. '
                    'Do not order it to repeat completed reads. Host task and Worker report:\n' +
                    json.dumps({'worker': self.worker.session_id, 'version': self.report.version,
                                'original_task': self.worker.goal,
                                'issue': self.report.detail}, ensure_ascii=False))
                self.lead = TeamSession('lead', self.lead_model, self._lead_tools,
                                        self.worker.event_queue, prompt, max_model_calls=6,
                                        max_tool_calls=4, timeout_seconds=self.timeout_seconds)
                lead_task = asyncio.create_task(self.lead.run())
                self._tasks.append(lead_task)
                lead_result = await lead_task
                if self._pending_revision is None or self.helper is None:
                    raise SessionError('Lead must revise Worker and start a helper')
                self.worker.revise(*self._pending_revision)
                worker_result, helper_result = await self._finish_members()
                return TrialResult(worker_result, helper_result, lead_result)
        finally:
            owned = self._tasks + ([self._event_reader] if self._event_reader is not None else [])
            for task in owned:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*owned, return_exceptions=True)
