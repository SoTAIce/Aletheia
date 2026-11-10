"""FastAPI application: sessions, runs with progress events, long-term memory (D-046).

The caller owns the RagAgentService (and the long-term memory service); the app owns
the runs and waits for or cancels them on shutdown. user_id is fixed by the server,
never taken from a request. Every error body is {"code", "message"} without exception text.
"""

import asyncio
import hmac
import json
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, StrictBool
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.runs import ApiError, RunManager, service_error
from app.services.workspace import system_drive

logger = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 20000
_ID = re.compile(r'[A-Za-z0-9._:-]{1,128}')
_STATUS_CODES = {404: 'not_found', 405: 'method_not_allowed'}
STATIC = Path(__file__).with_name('static')
# The page loads only its own script and stylesheet and talks only to this origin (D-047).
PAGE_FILES = {'app.js': 'text/javascript; charset=utf-8', 'app.css': 'text/css; charset=utf-8'}
SECURITY_HEADERS = {
    'Content-Security-Policy': ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                                "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"),
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'no-referrer',
    'X-Frame-Options': 'DENY',
}


class RunOptionsIn(BaseModel):
    """Choices for one message; an omitted one takes the server default (D-051)."""
    model_config = ConfigDict(extra='forbid')
    mode: Literal['single', 'team', 'auto'] | None = None
    runner: Literal['plan', 'loop'] | None = None
    write: StrictBool | None = None
    memory: StrictBool | None = None
    workspace: str | None = None                 # workspace-001: the working directory for this task
    workspace_confirmed: StrictBool | None = None  # the user confirmed a directory on the system drive


class MessageIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    content: str
    request_id: str
    resume_task_id: str | None = None
    question_id: str | None = None
    options: RunOptionsIn | None = None
    attachments: list[str] | None = None         # refs from POST .../attachments (workspace-001)


class WorkspaceIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    path: str


class AttachmentIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str
    content: str


class ApprovalIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    approval_id: str
    decision: str


class MemoryIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    content: str


def _check_id(name: str, value: str | None, *, required: bool = True) -> None:
    if value is None and not required:
        return
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ApiError(400, 'invalid_input', f'{name} 只能包含字母、数字和 ._:-，长度 1～128。')


def _memory_view(record) -> dict:
    return {'memory_id': record.memory_id, 'category': record.category.value, 'content': record.content,
            'created_at': record.created_at.isoformat()}


def create_app(service, *, user_id: str, access_token: str = '', memory=None, write_enabled: bool = False,
               write_default: bool | None = None,
               max_concurrent_runs: int = 2, event_retention_seconds: float = 1800.0,
               shutdown_timeout_seconds: float = 30.0, clock=None,
               approval_tools: frozenset[str] = frozenset(), approval_wait_seconds: float = 300.0) -> FastAPI:
    """An empty access_token disables authentication; the launcher only allows that on loopback."""
    options = {} if clock is None else {'clock': clock}
    runs = RunManager(service, user_id=user_id, max_concurrent=max_concurrent_runs,
                      retention_seconds=event_retention_seconds, approval_tools=frozenset(approval_tools),
                      approval_wait_seconds=approval_wait_seconds, **options)

    @asynccontextmanager
    async def lifespan(app):
        yield
        await runs.aclose(shutdown_timeout_seconds)

    app = FastAPI(title='Aletheia', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runs = runs

    @app.middleware('http')
    async def security_headers(request, call_next):
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        response.headers.setdefault('Cache-Control', 'no-store')
        return response

    @app.get('/', include_in_schema=False)
    async def page():
        return FileResponse(STATIC / 'index.html', media_type='text/html; charset=utf-8')

    @app.get('/static/{name}', include_in_schema=False)
    async def page_file(name: str):
        if name not in PAGE_FILES:
            raise ApiError(404, 'not_found', '请求无法处理。')
        return FileResponse(STATIC / name, media_type=PAGE_FILES[name])

    @app.exception_handler(ApiError)
    async def api_error(request, exc: ApiError):
        return JSONResponse({'code': exc.code, 'message': exc.message}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({'code': 'invalid_input', 'message': '请求格式不对。'}, status_code=400)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, exc):
        return JSONResponse({'code': _STATUS_CODES.get(exc.status_code, 'http_error'), 'message': '请求无法处理。'},
                            status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def unexpected(request, exc):
        logger.warning('Web request failed: %s', type(exc).__name__)
        return JSONResponse({'code': 'internal_error', 'message': '服务内部错误。'}, status_code=500)

    def authorize(authorization: str | None = Header(default=None)) -> None:
        if not access_token:
            return
        scheme, _, token = (authorization or '').partition(' ')
        if scheme.lower() != 'bearer' or not hmac.compare_digest(token.encode(), access_token.encode()):
            raise ApiError(401, 'unauthorized', '缺少或错误的访问令牌。')

    def need_memory():
        if memory is None:
            raise ApiError(404, 'memory_disabled', '长期记忆没有开启。')
        return memory

    async def call(coro):
        try:
            return await coro
        except ApiError:
            raise
        except Exception as exc:
            error = service_error(exc)
            if error.code == 'internal_error':
                logger.warning('Web call failed: %s', type(exc).__name__)
            raise error from None

    guarded = [Depends(authorize)]

    def run_options() -> dict:
        offered = service.run_options()
        return {'modes': list(offered['modes']), 'default_mode': offered['default_mode'],
                'runners': list(offered['runners']), 'default_runner': offered['default_runner'],
                'write_available': write_enabled,
                'write_default': write_enabled if write_default is None else bool(write_default and write_enabled),
                'memory_available': offered['memory_available'], 'memory_default': offered['memory_available'],
                **({'workspace_available': True, 'default_workspace': offered.get('default_workspace'),
                    'system_drive': system_drive()} if offered.get('workspace_available') else {})}

    def resolve(requested: dict) -> dict:
        """Fill omitted choices with the defaults; refuse ones this server cannot honour (D-051)."""
        offered = run_options()
        if requested.get('mode') is not None and requested['mode'] not in offered['modes']:
            raise ApiError(400, 'mode_unavailable', '服务启动时没有准备团队能力，只能使用普通模式。')
        if requested.get('runner') is not None and requested['runner'] not in offered['runners']:
            raise ApiError(400, 'runner_unavailable', '这台服务不能使用单一循环。')
        if requested.get('write') and not offered['write_available']:
            raise ApiError(400, 'write_unavailable', '服务启动时没有指定写入目录，不能写文件。')
        if requested.get('memory') and not offered['memory_available']:
            raise ApiError(400, 'memory_unavailable', '长期记忆没有开启，不能使用。')
        resolved_place = {}
        if requested.get('workspace') is not None:
            if not offered.get('workspace_available'):
                raise ApiError(400, 'workspace_unavailable', '这台服务不能更换工作目录。')
            check = service.check_workspace(requested['workspace'])
            if check.problem is not None:
                raise ApiError(400, 'workspace_invalid', check.message)
            if check.system_drive and not requested.get('workspace_confirmed'):
                raise ApiError(409, 'workspace_confirmation_required', '这个目录在系统盘上，需要你确认后才能使用。')
            resolved_place = {'workspace': check.path, 'workspace_confirmed': bool(check.system_drive)}
        return {**resolved_place, 'mode': requested.get('mode') or offered['default_mode'],
                'runner': requested.get('runner') or offered['default_runner'],
                'write': offered['write_default'] if requested.get('write') is None else requested['write'],
                'memory': offered['memory_default'] if requested.get('memory') is None else requested['memory']}

    @app.get('/api/health', dependencies=guarded)
    async def health():
        return {'status': 'ok', 'longterm_memory': memory is not None, 'write_enabled': write_enabled,
                'active_runs': runs.active_count, 'options': run_options(),
                'approval_tools': sorted(approval_tools)}

    @app.get('/api/sessions', dependencies=guarded)
    async def sessions(limit: int = 50):
        return {'sessions': await call(service.list_sessions(user_id=user_id, limit=limit))}

    @app.get('/api/sessions/{session_id}/turns', dependencies=guarded)
    async def turns(session_id: str, limit: int = 20):
        _check_id('session_id', session_id)
        return {'turns': await call(service.session_turns(session_id, user_id=user_id, limit=limit))}

    @app.get('/api/sessions/{session_id}/tasks/{task_id}/trace', dependencies=guarded)
    async def trace(session_id: str, task_id: str):
        """The runs of one task as span trees, from its audit files (trace-001, D-073)."""
        _check_id('session_id', session_id)
        _check_id('task_id', task_id)
        runs = await call(service.task_traces(session_id, task_id, user_id=user_id))
        if not runs:
            raise ApiError(404, 'trace_unavailable', '这次任务没有执行记录（可能是未开启归档或审计）。')
        return {'runs': runs}

    @app.post('/api/sessions/{session_id}/messages', status_code=202, dependencies=guarded)
    async def message(session_id: str, body: MessageIn):
        _check_id('session_id', session_id)
        _check_id('request_id', body.request_id)
        _check_id('resume_task_id', body.resume_task_id, required=False)
        _check_id('question_id', body.question_id, required=False)
        if not body.content.strip() or len(body.content) > MAX_MESSAGE_CHARS:
            raise ApiError(400, 'invalid_input', f'消息不能为空，最长 {MAX_MESSAGE_CHARS} 字。')
        if body.question_id is not None and body.resume_task_id is None:
            raise ApiError(400, 'invalid_input', '回答问题时需要同时给出任务编号。')
        requested = body.options.model_dump(exclude_none=True) if body.options is not None else {}
        attachments = tuple(body.attachments or ())
        if len(attachments) > 5 or not all(isinstance(r, str) and 0 < len(r) <= 200 for r in attachments):
            raise ApiError(400, 'invalid_input', '一条消息最多附带 5 个文件。')
        if attachments and body.resume_task_id is not None:
            raise ApiError(400, 'invalid_input', '回答问题时不能附带文件，请在新消息里附带。')
        if attachments:
            requested['attachments'] = list(attachments)     # part of the request, for idempotency
        # A resumed task keeps the choices it started with, so the request's own are ignored (D-051).
        options = resolve(requested) if body.resume_task_id is None else None
        run, created = runs.start(session_id, body.content, body.request_id,
                                  resume_task_id=body.resume_task_id, question_id=body.question_id,
                                  options=options, requested=requested, attachments=attachments)
        payload = {'run_id': run.run_id, 'events': f'/api/runs/{run.run_id}/events', 'created': created}
        return JSONResponse(payload, status_code=202 if created else 200)

    @app.post('/api/workspace/check', dependencies=guarded)
    async def workspace_check(body: WorkspaceIn):
        """Whether a directory can be the working directory (workspace-001); lists nothing."""
        if not run_options().get('workspace_available'):
            raise ApiError(404, 'workspace_unavailable', '这台服务不能更换工作目录。')
        return (await asyncio.to_thread(service.check_workspace, body.path)).view()

    @app.post('/api/sessions/{session_id}/attachments', status_code=201, dependencies=guarded)
    async def attach(session_id: str, body: AttachmentIn):
        """Keep a text file dropped into the message box; the next message names its ref."""
        _check_id('session_id', session_id)
        from app.services.rag_agent_service import MAX_ATTACHMENT_CHARS
        if not body.content or len(body.content) > MAX_ATTACHMENT_CHARS or '\x00' in body.content:
            raise ApiError(400, 'invalid_input', f'只能附带文本文件，最长 {MAX_ATTACHMENT_CHARS:,} 字符。')
        if not body.name.strip() or len(body.name) > 500:
            raise ApiError(400, 'invalid_input', '文件名不对。')
        try:
            return await service.save_attachment(session_id, body.name, body.content, user_id=user_id)
        except ValueError as exc:
            if 'archive' in str(exc):
                raise ApiError(400, 'attachments_unavailable', '服务没有开启对话归档，不能附带文件。') from None
            raise ApiError(400, 'invalid_input', '附件不符合要求。') from None

    @app.get('/api/runs/{run_id}', dependencies=guarded)
    async def run_state(run_id: str):
        return runs.get(run_id).snapshot()

    @app.get('/api/runs/{run_id}/events', dependencies=guarded)
    async def run_events(run_id: str, last_event_id: str | None = Header(default=None)):
        run = runs.get(run_id)
        after = int(last_event_id) if last_event_id and last_event_id.isdigit() else 0

        async def stream():
            async for event in run.follow(after):
                data = json.dumps(event, ensure_ascii=False)
                yield f"id: {event['id']}\nevent: {event['type']}\ndata: {data}\n\n"

        return StreamingResponse(stream(), media_type='text/event-stream',
                                 headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})

    @app.post('/api/runs/{run_id}/approvals', dependencies=guarded)
    async def approve(run_id: str, body: ApprovalIn):
        # The user's answer to a waiting side-effect tool call (D-061).
        return runs.answer_approval(run_id, body.approval_id, body.decision).snapshot()

    @app.post('/api/runs/{run_id}/cancel', dependencies=guarded)
    async def cancel(run_id: str):
        return (await runs.cancel(run_id)).snapshot()

    @app.get('/api/memories', dependencies=guarded)
    async def memories(limit: int = 20, offset: int = 0):
        records = await call(need_memory().list_memories(user_id, limit=limit, offset=offset))
        return {'memories': [_memory_view(r) for r in records]}

    @app.post('/api/memories', status_code=201, dependencies=guarded)
    async def remember(body: MemoryIn):
        store = need_memory()
        if not body.content.strip():
            raise ApiError(400, 'invalid_input', '记忆内容不能为空。')
        return _memory_view(await call(store.remember_explicit(user_id, body.content)))

    @app.delete('/api/memories/{memory_id}', dependencies=guarded)
    async def forget(memory_id: str):
        store = need_memory()
        _check_id('memory_id', memory_id)
        if not await call(store.forget(user_id, memory_id)):
            raise ApiError(404, 'memory_not_found', '没有这条记忆。')
        return {'deleted': memory_id}

    return app
