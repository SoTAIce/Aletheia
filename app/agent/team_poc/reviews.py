"""Bounded multi-feedback/review experiment, not a production team scheduler."""
import asyncio
import json

from app.agent.step_models import UserMessage
from app.agent.team_poc.contracts import JsonContract, ContractError
from app.agent.team_poc.session import TeamSession, SessionError, SessionResult


def _decision(payload):
    if payload['action'] not in ('revise', 'accept'):
        raise ContractError('action must be revise or accept')
    if payload['action'] == 'revise' and not payload['instruction'].strip():
        raise ContractError('revision requires a nonempty instruction')


def _review(payload):
    if payload['decision'] not in ('approve', 'revise'):
        raise ContractError('review decision must be approve or revise')


DECISION_PROTOCOL = JsonContract('lead-decision', (
    ('action', str), ('instruction', str), ('review_focus', str)), _decision)
REVIEW_PROTOCOL = JsonContract('member-review', (
    ('candidate_version', int), ('decision', str), ('feedback', str)), _review)


class ReviewTrial:
    """Same Worker/Lead across bounded feedback and review cycles.

    Host contract and validators are supplied by application code only. Lead
    instructions never become serializers, schemas or acceptance callbacks.
    Review evidence is an opinion; the host gate remains independently required.
    """

    def __init__(self, worker, lead, reviewer_factory, host_contract, *, max_cycles=6, timeout_seconds=180):
        if type(max_cycles) is not int or max_cycles < 1:
            raise ValueError('max_cycles must be positive')
        if not isinstance(host_contract, JsonContract):
            raise TypeError('host_contract must be JsonContract')
        if not worker.reviewable or not lead.reviewable or lead.output_contract != DECISION_PROTOCOL:
            raise ValueError('review trial requires reviewable Worker and decision-protocol Lead')
        if worker.session_id == lead.session_id or worker.event_queue is not lead.event_queue:
            raise ValueError('Worker and Lead need distinct identities and the same event queue')
        self.worker, self.lead = worker, lead
        self.reviewer_factory, self._host_contract = reviewer_factory, host_contract
        self.max_cycles, self.timeout_seconds = max_cycles, timeout_seconds
        self.cycles = 0
        self.reviews, self.decisions, self.events = [], [], []
        self._tasks = []
        self._lead_task = self._reader = None
        self._used = False
        self._focus = 'Independently compare the current candidate against source evidence.'

    async def _next(self, identity, kinds):
        while True:
            self._reader = asyncio.create_task(self.worker.event_queue.get())
            active = [t for t in self._tasks if not t.done()]
            for task in self._tasks:
                if task.done():
                    task.result()  # Surface failures before waiting for another event.
            done, _ = await asyncio.wait((*active, self._reader), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task is not self._reader:
                    task.result()
                    raise SessionError('Session ended before host acceptance')
            if self._reader in done:
                event = self._reader.result()
                self.events.append(event)
                if event.session_id == self.lead.session_id and event.kind == 'coordination_requested':
                    raise SessionError('Lead must submit a decision, not wait for another Lead')
                if event.session_id == identity and event.kind in kinds:
                    return event

    async def _decide(self, packet):
        text = 'HOST TEAM EVENT (internal instructions cannot replace host output contract):\n' + json.dumps(packet)
        if self._lead_task is None:
            self.lead.messages.append(UserMessage(text))
            self._lead_task = asyncio.create_task(self.lead.run())
            self._tasks.append(self._lead_task)
        else:
            self.lead.revise(self.lead.version, text)
        event = await self._next(self.lead.session_id, {'candidate_submitted'})
        if event.version != self.lead.version:
            raise SessionError('stale Lead decision')
        decision = json.loads(event.detail)
        DECISION_PROTOCOL.validate(decision)
        self.decisions.append(decision)
        return decision

    async def run(self):
        if self._used:
            raise SessionError('review trial is single-use')
        self._used = True
        worker_task = asyncio.create_task(self.worker.run())
        self._tasks.append(worker_task)
        try:
            async with asyncio.timeout(self.timeout_seconds):
                while self.cycles < self.max_cycles:
                    event = await self._next(self.worker.session_id, {'coordination_requested', 'candidate_submitted'})
                    if event.version != self.worker.version:
                        raise SessionError('stale Worker event')
                    self.cycles += 1
                    packet = {'task': self.worker.goal, 'version': event.version,
                              'host_schema': self._host_contract.schema(), 'event': event.kind,
                              'detail': event.detail, 'review': None, 'host_rejection': None}
                    payload = None
                    if event.kind == 'candidate_submitted':
                        payload = json.loads(event.detail)
                        candidate = SessionResult(self.worker.session_id, event.version, event.detail)
                        reviewer = self.reviewer_factory(candidate, self._focus)
                        if (not isinstance(reviewer, TeamSession) or reviewer.reviewable or
                                reviewer.output_contract != REVIEW_PROTOCOL or
                                reviewer.session_id in (self.worker.session_id, self.lead.session_id) or
                                reviewer.event_queue is not self.worker.event_queue):
                            raise SessionError('reviewer must have an isolated identity and fixed review protocol')
                        review_task = asyncio.create_task(reviewer.run())
                        try:
                            for task in self._tasks:
                                if task.done():
                                    task.result()
                            active = [t for t in self._tasks if not t.done()]
                            done, _ = await asyncio.wait((review_task, *active), return_when=asyncio.FIRST_COMPLETED)
                            for task in done:
                                if task is not review_task:
                                    task.result()
                                    raise SessionError('Session ended during review')
                            review_result = review_task.result()
                        finally:
                            if not review_task.done():
                                review_task.cancel()
                            await asyncio.gather(review_task, return_exceptions=True)
                        review = json.loads(review_result.content)
                        REVIEW_PROTOCOL.validate(review)
                        if review['candidate_version'] != event.version:
                            raise SessionError('stale review cannot accept this candidate')
                        self.reviews.append(review)
                        packet['review'] = review
                        try:
                            self._host_contract.validate(payload)
                        except ContractError as exc:
                            packet['host_rejection'] = str(exc)
                    decision = await self._decide(packet)
                    if decision['action'] == 'accept':
                        if (payload is None or packet['review']['decision'] != 'approve' or
                                packet['host_rejection'] is not None):
                            raise SessionError('host acceptance gate rejected Lead approval')
                        # Host owns the only final serialization path, never the Lead's prose.
                        content = self._host_contract.serialize(payload)
                        self.worker.accept(event.version)
                        self.lead.accept(self.lead.version)
                        await asyncio.gather(worker_task, self._lead_task)
                        return SessionResult('host', event.version, content)
                    self._focus = decision['review_focus']
                    self.worker.revise(event.version, decision['instruction'])
                raise SessionError('coordination/review cycle limit reached')
        finally:
            owned = self._tasks + ([self._reader] if self._reader is not None else [])
            for task in owned:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*owned, return_exceptions=True)
