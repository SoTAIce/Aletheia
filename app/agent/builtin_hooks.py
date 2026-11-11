"""Built-in hooks (docs/hooks_design.md section 9): the audit log and the conversation token cap (D-060)."""

from __future__ import annotations

from datetime import datetime, UTC
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import time

from app.agent.hooks import ALLOW, BEFORE_MODEL, TASK_END, Decision
from app.agent.run_meter import current_meter

logger = logging.getLogger(__name__)

# Version 2: events carry span ids, so a trace can be built from them (D-073).
AUDIT_VERSION = 2
MAX_AUDIT_EVENTS = 2000


class AuditHook:
    """Keeps one run's hook events and writes them as one JSON Lines file when the run ends.

    The events are host-authored (D-059), so the file holds no model text, tool output or
    file contents. Writing once at the end keeps observe() to an in-memory append; a run
    with more than MAX_AUDIT_EVENTS events keeps the first ones and counts the rest.
    """

    name = 'audit'

    def __init__(self, archive, *, max_events: int = MAX_AUDIT_EVENTS) -> None:
        if not callable(getattr(archive, 'save_audit', None)):
            raise TypeError('archive must provide save_audit()')
        self._archive, self._max_events = archive, max_events
        self._started_wall = datetime.now(UTC)
        self._started = time.monotonic()
        self._events: list[dict] = []
        self._dropped = 0
        self._notes: dict = {}
        self.path = None

    def note(self, **fields) -> None:
        """Run facts the runtime cannot see (execution mode, read-only), for the header."""
        self._notes.update({k: v for k, v in fields.items() if isinstance(v, (str, bool, int, type(None)))})

    def observe(self, event: dict) -> None:
        if len(self._events) < self._max_events:
            self._events.append({'t': round(time.monotonic() - self._started, 3), **event})
        else:
            self._dropped += 1
        if event.get('type') == TASK_END:
            self._flush(event.get('task_id'))

    def _flush(self, task_id) -> None:
        header = {'type': 'audit', 'version': AUDIT_VERSION, 'started_at': self._started_wall.isoformat(),
                  'trace': True, 'events': len(self._events), 'dropped': self._dropped, **self._notes}
        stamp = self._started_wall.strftime('%Y%m%dT%H%M%S%fZ')
        task = re.sub(r'[^0-9A-Za-z_-]', '', str(task_id or 'task'))[:64] or 'task'
        self.path = self._save(f'{stamp}-{task}', [header, *self._events])
        # A hook kept for a later run (TraceFileHook in a long-lived service) starts it afresh.
        self._events, self._dropped = [], 0
        self._started_wall, self._started = datetime.now(UTC), time.monotonic()

    def _save(self, name: str, records: list[dict]):
        return self._archive.save_audit(name, records)


class _Directory:
    """Stands in for an archive: TraceFileHook writes to a plain directory."""

    def save_audit(self, name, records):          # pragma: no cover - replaced by TraceFileHook._save
        raise NotImplementedError


class TraceFileHook(AuditHook):
    """The audit records of one run, written to a directory the caller names (D-073). The
    benchmark uses it because its runs keep no conversation archive. prefix (letters, digits,
    '_' and '-') starts the file name, so a file can be matched with its report row."""

    name = 'trace_file'

    def __init__(self, directory, *, prefix: str = '', max_events: int = MAX_AUDIT_EVENTS) -> None:
        super().__init__(_Directory(), max_events=max_events)
        self._directory = Path(directory)
        self._prefix = re.sub(r'[^0-9A-Za-z_-]', '', str(prefix))[:80]

    def _save(self, name: str, records: list[dict]):
        name = f'{self._prefix}-{name}' if self._prefix else name
        lines = ''.join(json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n' for record in records)
        self._directory.mkdir(parents=True, exist_ok=True)
        path = self._directory / f'{name}.jsonl'
        fd, temporary = tempfile.mkstemp(prefix='.trace-', suffix='.tmp', dir=self._directory)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as handle:
                handle.write(lines)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return path


class ConversationTokenCap:
    """Refuses model calls once a conversation has used its token allowance.

    prior_tokens: what earlier runs of this conversation used. The current run's usage
    comes from the run meter, so a run may go over by at most the call already under way.
    """

    name = 'conversation_token_cap'
    events = frozenset({BEFORE_MODEL})

    def __init__(self, cap: int, prior_tokens: int = 0) -> None:
        if type(cap) is not int or cap < 1:
            raise ValueError('cap must be a positive integer')
        if type(prior_tokens) is not int or prior_tokens < 0:
            raise ValueError('prior_tokens must be a non-negative integer')
        self.cap, self.prior_tokens = cap, prior_tokens

    def used(self) -> int:
        meter = current_meter()
        return self.prior_tokens + (meter.total_tokens if meter is not None else 0)

    async def check(self, event: dict) -> Decision:
        used = self.used()
        if used >= self.cap:
            return Decision.deny(f'this conversation has used its token allowance ({used} of {self.cap} tokens)')
        return ALLOW


class PassingRunGate:
    """Team completion uses only evidence observed by the host in this run."""

    name = 'passing_run'
    events = frozenset({'before_finish'})

    def __init__(self, *, require_passing_run=False, review=False):
        self.require_passing_run, self.review = require_passing_run, review

    async def check(self, event: dict) -> Decision:
        if self.require_passing_run and event['writes'] > 0 and not event['passing_run_after_last_write']:
            return Decision.deny('completion requires a passing run after the last write')
        if self.review and not event['review_after_last_write']:
            return Decision.deny('completion requires reviewer approval after the last write')
        return ALLOW
