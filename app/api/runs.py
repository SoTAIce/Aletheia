"""Background runs decoupled from HTTP requests, with replayable progress events (D-046).

A run is one accepted message. It lives as an asyncio task in this process; its events
are kept in memory until a retention period after it finishes. The conversation archive
stays the authoritative history. Events carry host-authored records only: never tool
output, model text other than the final reply or the blocked question, or exception text.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field

from app.models.taskstate import TaskStatus

logger = logging.getLogger(__name__)

CANCELLED_MESSAGE = '已取消；工具可能已经产生了副作用。'
FAILED_MESSAGE = '任务没有完成。'


class ApiError(Exception):
    """A refusal with a stable code and a Chinese message; never carries exception text."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(code)
        self.status, self.code, self.message = status, code, message


def service_error(exc: Exception) -> ApiError:
    """Map the service's host-authored refusals to API codes; anything else is internal."""
    text = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else ''
    if isinstance(exc, ValueError):
        if 'request ID conflict' in text or 'archived result conflicts' in text:
            return ApiError(409, 'request_conflict', '这个请求编号已经用于另一条消息。')
        if 'No resumable task' in text or 'not an unresolved blocking question' in text:
            return ApiError(409, 'task_not_resumable', '这个任务已经不能接着回答了，请重新提问。')
        if 'question_id is required' in text:
            return ApiError(400, 'question_required', '这个任务有多个待回答的问题，需要指明回答哪一个。')
        return ApiError(400, 'invalid_input', '请求内容不符合要求。')
    if isinstance(exc, RuntimeError) and 'automatic execution replay is unsafe' in text:
        return ApiError(409, 'request_unfinished', '这个请求上次没有正常结束，为避免重复执行不会再运行。')
    if isinstance(exc, RuntimeError) and 'service is closed' in text:
        return ApiError(503, 'shutting_down', '服务正在关闭。')
    return ApiError(500, 'internal_error', '服务内部错误。')


@dataclass
class Run:
    run_id: str
    session_id: str
    content: str
    resume_task_id: str | None
    question_id: str | None
    requested: dict = field(default_factory=dict)   # options as submitted, for idempotency (D-051)
    options: dict | None = None                     # options in effect: mode, write, memory (D-051)
    events: list[dict] = field(default_factory=list)
    status: str = 'running'          # running | completed | blocked | failed | cancelled
    finished_at: float | None = None
    task: asyncio.Task | None = None
    approvals: dict = field(default_factory=dict)   # approval_id -> future awaiting the user (D-061)
    _changed: asyncio.Event = field(default_factory=asyncio.Event)

    def add(self, event: dict) -> None:
        if self.finished_at is not None:
            return                    # a late record after the terminal event is dropped
        if event.get('type') == 'route' and isinstance(event.get('options'), dict):
            self.options = dict(event['options'])   # the service's record is the one in effect
        self.events.append({'id': len(self.events) + 1, **event})
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    def finish(self, status: str, event: dict, now: float) -> None:
        self.add(event)
        self.status, self.finished_at = status, now
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    def snapshot(self) -> dict:
        terminal = self.events[-1] if self.finished_at is not None and self.events else None
        return {'run_id': self.run_id, 'session_id': self.session_id, 'status': self.status,
                'options': self.options, 'event_count': len(self.events), 'result': terminal}

    async def follow(self, after: int = 0):
        """Yield events with id > after, then stop once the run has finished."""
        while True:
            changed = self._changed
            while after < len(self.events):
                after += 1
                yield self.events[after - 1]
            if self.finished_at is not None:
                return
            await changed.wait()


def terminal_event(result) -> tuple[str, dict]:
    if result.status is TaskStatus.COMPLETED:
        return 'completed', {'type': 'completed', 'task_id': result.task_id, 'content': result.content,
                             'usage': result.usage}
    if result.status is TaskStatus.BLOCKED:
        return 'blocked', {'type': 'blocked', 'task_id': result.task_id, 'question': result.content}
    return 'failed', {'type': 'failed', 'task_id': result.task_id, 'code': 'task_failed',
                      'failure_kind': result.failure_kind,
                      'message': result.user_message or FAILED_MESSAGE, 'usage': result.usage}


class RunManager:
    """Owns every run of one service: limits, idempotency, cancellation and shutdown."""

    def __init__(self, service, *, user_id: str, max_concurrent: int, retention_seconds: float,
                 clock=time.monotonic, approval_tools: frozenset[str] = frozenset(),
                 approval_wait_seconds: float = 300.0) -> None:
        self._service, self._user_id = service, user_id
        self._approval_tools, self._approval_wait = frozenset(approval_tools), approval_wait_seconds
        self._max_concurrent, self._retention = max_concurrent, retention_seconds
        self._clock = clock
        self._runs: dict[str, Run] = {}
        self._active_by_session: dict[str, str] = {}
        self._closing = False

    @property
    def active_count(self) -> int:
        return len(self._active_by_session)

    def _prune(self) -> None:
        now = self._clock()
        for run_id in [r.run_id for r in self._runs.values()
                       if r.finished_at is not None and now - r.finished_at > self._retention]:
            del self._runs[run_id]

    def get(self, run_id: str) -> Run:
        self._prune()
        run = self._runs.get(run_id)
        if run is None:
            raise ApiError(404, 'run_not_found', '找不到这次运行，可能已经过期。')
        return run

    def start(self, session_id: str, content: str, request_id: str, *,
              resume_task_id: str | None = None, question_id: str | None = None,
              options: dict | None = None, requested: dict | None = None) -> tuple[Run, bool]:
        """Start a run, or return the same run for a repeated request_id. Returns (run, created).

        options are the resolved choices of a new task; a resume ignores them and keeps the
        task's own (D-051). requested is what the client sent, compared for a repeated request_id."""
        if self._closing:
            raise ApiError(503, 'shutting_down', '服务正在关闭。')
        self._prune()
        existing = self._runs.get(request_id)
        if existing is not None:
            if (existing.session_id, existing.content, existing.resume_task_id, existing.question_id,
                    existing.requested) != (session_id, content, resume_task_id, question_id, requested or {}):
                raise ApiError(409, 'request_conflict', '这个请求编号已经用于另一条消息。')
            return existing, False
        if session_id in self._active_by_session:
            raise ApiError(409, 'session_busy', '这个会话还有任务在运行，请等它结束或先取消。')
        if self.active_count >= self._max_concurrent:
            raise ApiError(429, 'too_many_runs', '同时运行的任务已达上限，请稍后再试。')
        if resume_task_id is not None and not self._service.resumable(
                session_id, resume_task_id, user_id=self._user_id):
            raise ApiError(409, 'task_not_resumable', '这个任务已经不能接着回答了，请重新提问。')
        if resume_task_id is not None:
            options = self._service.task_options(session_id, resume_task_id, user_id=self._user_id)
        run = Run(request_id, session_id, content, resume_task_id, question_id, dict(requested or {}),
                  dict(options) if options is not None else None)
        self._runs[request_id] = run
        self._active_by_session[session_id] = request_id
        run.add({'type': 'accepted', 'run_id': request_id, 'session_id': session_id})
        run.task = asyncio.create_task(self._execute(run), name=f'web-run-{request_id}')
        return run, True

    async def _execute(self, run: Run) -> None:
        choices = {}
        if run.resume_task_id is None and run.options is not None:
            choices = {'execution_mode': run.options['mode'], 'allow_writes': run.options['write'],
                       'use_memory': run.options['memory']}
        if self._approval_tools:
            # Side-effect tools wait for the person on the page (D-061).
            from app.agent.hooks import HookSet
            from app.api.approvals import ApprovalGate
            choices['hooks'] = HookSet(gates=[ApprovalGate(run, self._approval_tools, self._approval_wait)])
        try:
            result = await self._service.query_result(
                run.content, run.session_id, user_id=self._user_id, request_id=run.run_id,
                resume_task_id=run.resume_task_id, question_id=run.question_id, on_event=run.add, **choices)
        except asyncio.CancelledError:
            run.finish('cancelled', {'type': 'cancelled', 'message': CANCELLED_MESSAGE}, self._clock())
        except Exception as exc:
            error = service_error(exc)
            if error.code == 'internal_error':
                logger.warning('Web run failed: %s', type(exc).__name__)
            run.finish('failed', {'type': 'failed', 'code': error.code, 'message': error.message}, self._clock())
        else:
            status, event = terminal_event(result)
            run.finish(status, event, self._clock())
        finally:
            if self._active_by_session.get(run.session_id) == run.run_id:
                del self._active_by_session[run.session_id]

    def answer_approval(self, run_id: str, approval_id: str, decision: str) -> Run:
        """Pass the user's answer to a waiting approval (D-061)."""
        from app.api.approvals import DECISIONS
        run = self.get(run_id)
        if decision not in DECISIONS:
            raise ApiError(400, 'invalid_input', '回答只能是允许、拒绝或本次运行都允许。')
        future = run.approvals.get(approval_id) if isinstance(approval_id, str) else None
        if future is None or future.done():
            raise ApiError(409, 'approval_not_pending', '这个确认已经处理过或已经过期。')
        future.set_result(decision)
        return run

    async def cancel(self, run_id: str) -> Run:
        """Cancel a running run and wait for its cancelled event; a finished run is unchanged."""
        run = self.get(run_id)
        if run.finished_at is None and run.task is not None:
            run.task.cancel()
            await asyncio.gather(run.task, return_exceptions=True)
        return run

    async def aclose(self, timeout_seconds: float) -> None:
        """Refuse new runs, let running ones finish within the timeout, then cancel the rest."""
        self._closing = True
        tasks = [r.task for r in self._runs.values() if r.task is not None and not r.task.done()]
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=timeout_seconds)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
