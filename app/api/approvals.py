"""Approval of side-effect tool calls on the web page (docs/hooks_design.md section 8, D-061).

The run waits where it is: the tool has not run yet, so after the user answers the step goes on
without asking the model again. The waiting time is given back to the step and team deadlines.
"""

import asyncio
import time
from uuid import uuid4

from app.agent.hooks import ALLOW, BEFORE_TOOL, Decision, extend_deadlines

DECISIONS = frozenset({'allow', 'deny', 'allow_all'})


class ApprovalGate:
    """A before_tool gate that asks the person on the page about the listed tools.

    One question at a time (team members queue). 'allow_all' allows the rest of this run.
    No answer within timeout_seconds is a denial; cancelling the run cancels the wait.
    """

    name = 'approval'
    events = frozenset({BEFORE_TOOL})

    def __init__(self, run, tools: frozenset[str], wait_seconds: float) -> None:
        if not tools:
            raise ValueError('approval needs at least one tool')
        self._run, self._tools, self._wait = run, frozenset(tools), float(wait_seconds)
        self.timeout_seconds = self._wait + 5          # the HookSet's own limit for this gate
        self._lock = asyncio.Lock()
        self._allow_all = False

    async def check(self, event: dict) -> Decision:
        if event.get('tool') not in self._tools or self._allow_all:
            return ALLOW
        async with self._lock:
            if self._allow_all:
                return ALLOW
            approval_id = uuid4().hex
            future = asyncio.get_running_loop().create_future()
            self._run.approvals[approval_id] = future
            self._run.add({'type': 'approval_required', 'approval_id': approval_id, 'tool': event['tool'],
                           'args_summary': event.get('args_summary', {}), 'timeout_seconds': self._wait})
            # Move the deadlines out by the longest wait first, so a step deadline cannot fire
            # while the person decides; afterwards take back the part of it that was not used.
            started = time.monotonic()
            extend_deadlines(self._wait)
            try:
                decision = await asyncio.wait_for(asyncio.shield(future), self._wait)
            except TimeoutError:
                decision = 'timeout'
            finally:
                self._run.approvals.pop(approval_id, None)
                if not future.done():
                    future.cancel()
                extend_deadlines((time.monotonic() - started) - self._wait)
            self._run.add({'type': 'approval_resolved', 'approval_id': approval_id, 'decision': decision})
            if decision == 'allow_all':
                self._allow_all = True
            if decision in ('allow', 'allow_all'):
                return ALLOW
            if decision == 'timeout':
                return Decision.deny(f'the user did not answer within {self._wait:g} seconds')
            return Decision.deny('the user did not allow this call')
