"""Approval of side-effect tool calls on the web page (docs/hooks_design.md section 8, D-061).

The run waits where it is: the tool has not run yet, so after the user answers the step goes on
without asking the model again. The waiting time is given back to the step and team deadlines.
"""

import asyncio
import time
from uuid import uuid4

from app.agent.hooks import ALLOW, BEFORE_TOOL, Decision, extend_deadlines

DECISIONS = frozenset({'allow', 'deny', 'allow_all'})


class WebAsker:
    """Ask the person on the web page about a tool call and wait for an answer (D-061, D-094).

    One question at a time (team members queue). 'allow_all' allows the rest of this run.
    No answer within wait_seconds is a denial; cancelling the run cancels the wait.
    """

    def __init__(self, run, wait_seconds: float) -> None:
        self._run, self.wait_seconds = run, float(wait_seconds)
        self._lock = asyncio.Lock()
        self._allow_all = False

    async def ask(self, event: dict, *, rule: int | None = None) -> Decision:
        if self._allow_all:
            return ALLOW
        async with self._lock:
            if self._allow_all:
                return ALLOW
            approval_id = uuid4().hex
            future = asyncio.get_running_loop().create_future()
            self._run.approvals[approval_id] = future
            payload = {'type': 'approval_required', 'approval_id': approval_id, 'tool': event['tool'],
                       'args_summary': event.get('args_summary', {}),
                       'args_preview': event.get('args_preview', {}), 'timeout_seconds': self.wait_seconds}
            if rule is not None:
                payload['rule'] = rule
            self._run.add(payload)
            # Move the deadlines out by the longest wait first, so a step deadline cannot fire
            # while the person decides; afterwards take back the part of it that was not used.
            started = time.monotonic()
            extend_deadlines(self.wait_seconds)
            try:
                decision = await asyncio.wait_for(asyncio.shield(future), self.wait_seconds)
            except TimeoutError:
                decision = 'timeout'
            finally:
                self._run.approvals.pop(approval_id, None)
                if not future.done():
                    future.cancel()
                extend_deadlines((time.monotonic() - started) - self.wait_seconds)
            self._run.add({'type': 'approval_resolved', 'approval_id': approval_id, 'decision': decision})
            if decision == 'allow_all':
                self._allow_all = True
            if decision in ('allow', 'allow_all'):
                return ALLOW
            suffix = f' (permission rule {rule})' if rule is not None else ''
            if decision == 'timeout':
                return Decision.deny(f'the user did not answer within {self.wait_seconds:g} seconds{suffix}', rule=rule)
            return Decision.deny(f'the user did not allow this call{suffix}', rule=rule)


class DenyAsker:
    """Immediately deny any tool call that asks for user confirmation (unattended runs, D-102)."""

    async def ask(self, event: dict, *, rule: int | None = None) -> Decision:
        suffix = f' (permission rule {rule})' if rule is not None else ''
        return Decision.deny(f'scheduled run cannot ask the user for confirmation{suffix}', rule=rule)


class ApprovalGate:
    """A before_tool gate that asks the person on the page about the listed tools.

    One question at a time (team members queue). 'allow_all' allows the rest of this run.
    No answer within timeout_seconds is a denial; cancelling the run cancels the wait.
    """

    # Gate-then-invoke relies on sequential tool calls; recheck gates if calls ever run in parallel.
    name = 'approval'
    events = frozenset({BEFORE_TOOL})

    def __init__(self, run, tools: frozenset[str], wait_seconds: float) -> None:
        if not tools:
            raise ValueError('approval needs at least one tool')
        self._tools = frozenset(tools)
        self._asker = WebAsker(run, wait_seconds)
        self.timeout_seconds = self._asker.wait_seconds + 5  # the HookSet's own limit for this gate

    async def check(self, event: dict) -> Decision:
        # The listed tools, and any tool whose effect is not known (D-087): it may act outside the workspace.
        if event.get('tool') not in self._tools and event.get('effect') != 'external':
            return ALLOW
        return await self._asker.ask(event)
