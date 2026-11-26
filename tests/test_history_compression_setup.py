"""Offline tests for background history compression setup, probe, web wiring and limits (D-100)."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import openai
import pytest
from fastapi.testclient import TestClient

from loop_fakes import loop_reply

from app.agent import __main__ as cli
from app.agent.run_limits import RunLimits
from app.api import __main__ as web_cli
from app.api.app import create_app
from app.config import Settings
from app.core import dependencies
from app.models.conversation_memory import ConversationMessage, ConversationTurn, MessageRole
from app.models.taskstate import TaskStatus
from app.services.conversation_archive import ConversationArchive
from app.services.memory_compression.llm_compressor import LLMCompressor
from app.services.rag_agent_service import RagAgentService


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, dashscope_api_key="k", agent_model_base_url="",
                    agent_model_api_key="", agent_model="", **kw)


def _fake_client_cls(*, models=None, error=None, delay=0.0, closed=None, captured=None):
    class FakeClient:
        def __init__(self, **kwargs):
            if captured is not None:
                captured.append(kwargs)
            self.base_url = kwargs.get("base_url", "http://127.0.0.1:11434/v1")

            async def list_models():
                if delay:
                    await asyncio.sleep(delay)
                if error is not None:
                    raise error
                return SimpleNamespace(data=[SimpleNamespace(id=m) for m in (models or ())])

            self.models = SimpleNamespace(list=list_models)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            if closed is not None:
                closed.append("client")

    return FakeClient


# ---- Settings & loopback validation ----

def test_default_summary_model_settings_and_loopback_validation():
    s = _settings()
    assert s.summary_model_base_url == "http://127.0.0.1:11434/v1"
    assert s.summary_model == "qwen3:4b-instruct"
    assert s.summary_model_problem() is None

    for url in (
        "http://127.0.0.1:11434/v1",
        "http://localhost:11434/v1",
        "http://[::1]:11434/v1",
        "https://127.0.0.1:8443/v1",
    ):
        assert _settings(summary_model_base_url=url).summary_model_problem() is None


@pytest.mark.parametrize("bad_url", [
    "https://api.openai.com/v1",
    "http://192.168.1.5:11434/v1",
    "http://0.0.0.0:11434/v1",
    "http://127.0.0.2:11434/v1",
])
def test_non_loopback_summary_url_is_rejected_without_failing_import(bad_url):
    # Not a field validator: Settings constructs cleanly; summary_model_problem() reports it.
    s = _settings(summary_model_base_url=bad_url)
    problem = s.summary_model_problem()
    assert problem is not None
    assert "本机回环地址" in problem
    assert "summarizer" in problem


@pytest.mark.parametrize("bad_kwargs", [
    {"summary_model_base_url": "ftp://127.0.0.1:11434/v1"},
    {"summary_model_base_url": ""},
    {"summary_model_base_url": "   "},
    {"summary_model_base_url": "http://127.0.0.1:notaport/v1"},
    {"summary_model": ""},
    {"summary_model": "   "},
])
def test_invalid_scheme_port_or_empty_summary_settings(bad_kwargs):
    s = _settings(**bad_kwargs)
    assert s.summary_model_problem() is not None


# ---- dependencies.open_conversation_compressor ----

async def test_open_conversation_compressor_defaults_and_overrides(monkeypatch):
    captured = []
    monkeypatch.setattr(dependencies, "AsyncOpenAI", _fake_client_cls(captured=captured))
    monkeypatch.setattr(dependencies.config, "summary_model_base_url", "http://127.0.0.1:11434/v1")
    monkeypatch.setattr(dependencies.config, "summary_model", "qwen3:4b-instruct")

    async with dependencies.open_conversation_compressor() as compressor:
        assert compressor._model == "qwen3:4b-instruct"
    assert captured[-1] == {
        "base_url": "http://127.0.0.1:11434/v1",
        "api_key": "ollama",
        "timeout": 120.0,
        "max_retries": 0,
    }

    monkeypatch.setattr(dependencies.config, "summary_model_base_url", "http://localhost:11435/v1")
    monkeypatch.setattr(dependencies.config, "summary_model", "qwen3:8b")
    async with dependencies.open_conversation_compressor() as compressor:
        assert compressor._model == "qwen3:8b"
    assert captured[-1]["base_url"] == "http://localhost:11435/v1"
    assert captured[-1]["api_key"] == "ollama"
    assert captured[-1]["timeout"] == 120.0
    assert captured[-1]["max_retries"] == 0


# ---- check_conversation_compressor ----

async def test_check_compressor_passes_when_exact_model_id_present():
    client = _fake_client_cls(models=["qwen3:4b-instruct", "llama3:8b"])(base_url="http://127.0.0.1:11434/v1")
    compressor = LLMCompressor(client=client, model="qwen3:4b-instruct")
    assert await dependencies.check_conversation_compressor(compressor) is None


async def test_check_compressor_reports_missing_model_exact_match():
    client = _fake_client_cls(models=["qwen3:4b-instruct"])(base_url="http://127.0.0.1:11434/v1")
    compressor = LLMCompressor(client=client, model="qwen3")
    problem = await dependencies.check_conversation_compressor(compressor)
    assert problem is not None
    assert "本机没有模型 qwen3" in problem
    assert "ollama pull qwen3" in problem
    assert "完整模型名" in problem


@pytest.mark.parametrize("exc, expected_type", [
    (httpx.ConnectError("SECRET_DETAIL_123"), "ConnectError"),
    (openai.APIConnectionError(message="SECRET_DETAIL_456", request=httpx.Request("GET", "http://127.0.0.1:11434/v1/models")),
     "APIConnectionError"),
])
async def test_check_compressor_reports_connection_error_without_raw_message(exc, expected_type):
    client = _fake_client_cls(error=exc)(base_url="http://127.0.0.1:11434/v1")
    compressor = LLMCompressor(client=client, model="qwen3:4b-instruct")
    problem = await dependencies.check_conversation_compressor(compressor)
    assert problem is not None
    assert "127.0.0.1:11434" in problem
    assert "/v1" not in problem
    assert expected_type in problem
    assert "SECRET_DETAIL" not in problem
    assert "Ollama" in problem


async def test_check_compressor_reports_timeout():
    client = _fake_client_cls(models=["qwen3:4b-instruct"], delay=0.2)(base_url="http://[::1]:11434/v1")
    compressor = LLMCompressor(client=client, model="qwen3:4b-instruct")
    problem = await dependencies.check_conversation_compressor(compressor, timeout_seconds=0.02)
    assert problem is not None
    assert "[::1]:11434" in problem
    assert "TimeoutError" in problem
    assert "/v1" not in problem


# ---- open_history_compressor ----

async def test_open_history_compressor_disabled_does_nothing(monkeypatch):
    called = []
    monkeypatch.setattr(dependencies, "AsyncOpenAI", _fake_client_cls(captured=called))
    async with AsyncExitStack() as stack:
        compressor, status = await cli.open_history_compressor(False, stack)
    assert compressor is None and status == "off"
    assert called == []


async def test_open_history_compressor_requires_storage_dir(monkeypatch):
    monkeypatch.setattr(cli.config, "conversation_storage_dir", "")
    async with AsyncExitStack() as stack:
        with pytest.raises(ValueError, match="CONVERSATION_STORAGE_DIR"):
            await cli.open_history_compressor(True, stack)


async def test_open_history_compressor_rejects_non_loopback(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.config, "conversation_storage_dir", str(tmp_path))
    monkeypatch.setattr(cli.config, "summary_model_base_url", "https://remote.example.com/v1")
    async with AsyncExitStack() as stack:
        with pytest.raises(cli.SummaryModelError, match="本机回环地址"):
            await cli.open_history_compressor(True, stack)


async def test_open_history_compressor_unavailable_degrades_and_closes_client(monkeypatch, tmp_path, capsys):
    closed = []
    monkeypatch.setattr(cli.config, "conversation_storage_dir", str(tmp_path))
    monkeypatch.setattr(cli.config, "summary_model_base_url", "http://127.0.0.1:11434/v1")
    monkeypatch.setattr(cli.config, "summary_model", "qwen3:4b-instruct")
    monkeypatch.setattr(
        dependencies, "AsyncOpenAI",
        _fake_client_cls(error=httpx.ConnectError("raw-secret"), closed=closed),
    )
    async with AsyncExitStack() as stack:
        compressor, status = await cli.open_history_compressor(True, stack)
        assert compressor is None and status == "unavailable"
        assert closed == []
    assert closed == ["client"]
    out = capsys.readouterr().out
    assert "历史压缩不可用：" in out
    assert "这次不压缩历史。" in out
    assert "raw-secret" not in out


async def test_open_history_compressor_available_returns_compressor_and_closes(monkeypatch, tmp_path):
    closed = []
    monkeypatch.setattr(cli.config, "conversation_storage_dir", str(tmp_path))
    monkeypatch.setattr(cli.config, "summary_model_base_url", "http://127.0.0.1:11434/v1")
    monkeypatch.setattr(cli.config, "summary_model", "qwen3:4b-instruct")
    monkeypatch.setattr(
        dependencies, "AsyncOpenAI",
        _fake_client_cls(models=["qwen3:4b-instruct"], closed=closed),
    )
    async with AsyncExitStack() as stack:
        compressor, status = await cli.open_history_compressor(True, stack)
        assert compressor is not None and status == "on"
        assert closed == []
    assert closed == ["client"]


# ---- CLI startup integration ----

async def test_cli_rejects_non_loopback_summary_url_with_exit_2(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli.config, "conversation_storage_dir", str(tmp_path))
    monkeypatch.setattr(cli.config, "summary_model_base_url", "https://remote.example.com/v1")
    args = SimpleNamespace(compress_history=True, filesystem_root=None, write_root=None,
                           no_knowledge=True, max_executions=1, goal="goal", session_id="s")
    assert await cli.main(args) == 2
    out = capsys.readouterr().out
    assert "SummaryModelError:" in out
    assert "本机回环地址" in out


async def test_cli_starts_without_compressor_when_probe_fails(monkeypatch, tmp_path, capsys):
    closed = []
    received = []

    class FakeService:
        def __init__(self, **kwargs):
            received.append(kwargs["conversation_compressor"])

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            closed.append("service")

        async def query_result(self, *args, **kwargs):
            return SimpleNamespace(content="done", status=TaskStatus.COMPLETED, execution_count=1, replan_count=0)

    monkeypatch.setattr(cli, "RagAgentService", FakeService)
    monkeypatch.setattr(cli.config, "conversation_storage_dir", str(tmp_path))
    monkeypatch.setattr(cli.config, "summary_model_base_url", "http://127.0.0.1:11434/v1")
    monkeypatch.setattr(cli.config, "summary_model", "qwen3:4b-instruct")
    monkeypatch.setattr(dependencies, "AsyncOpenAI", _fake_client_cls(models=[], closed=closed))

    args = SimpleNamespace(compress_history=True, filesystem_root=None, write_root=None,
                           no_knowledge=True, max_executions=1, goal="goal", session_id="s")
    assert await cli.main(args) == 0
    assert received == [None]
    assert closed == ["service", "client"]
    assert "这次不压缩历史。" in capsys.readouterr().out


# ---- Web backend startup & /api/health ----

def test_health_endpoint_reports_history_compression_states(tmp_path):
    for status in ("off", "on", "unavailable"):
        service = RagAgentService(model=AsyncMock(), mcp_servers=(), include_knowledge=False,
                                  memory_directory=tmp_path)
        app = create_app(service, user_id="local", access_token="tok", history_compression=status)
        with TestClient(app) as client:
            resp = client.get("/api/health", headers={"Authorization": "Bearer tok"})
            assert resp.status_code == 200
            assert resp.json()["history_compression"] == status


def _patch_web_serve(monkeypatch, tmp_path, *, received_service, received_app, closed=None):
    import uvicorn

    class FakeService:
        def __init__(self, **kwargs):
            received_service.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            if closed is not None:
                closed.append("service")

    class FakeServer:
        def __init__(self, cfg):
            self.cfg = cfg

        async def serve(self):
            return None

    def fake_create_app(service, **kwargs):
        received_app.append(kwargs)
        return object()

    monkeypatch.setattr(web_cli, "RagAgentService", FakeService)
    monkeypatch.setattr("app.api.app.create_app", fake_create_app)
    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    monkeypatch.setattr(web_cli.config, "web_host", "127.0.0.1")
    monkeypatch.setattr(web_cli.config, "web_port", 8000)
    monkeypatch.setattr(web_cli.config, "web_access_token", "tok")
    monkeypatch.setattr(web_cli.config, "conversation_storage_dir", str(tmp_path))
    monkeypatch.setattr(web_cli.config, "summary_model_base_url", "http://127.0.0.1:11434/v1")
    monkeypatch.setattr(web_cli.config, "summary_model", "qwen3:4b-instruct")
    monkeypatch.setattr(web_cli.config, "longterm_memory_dsn", "")
    monkeypatch.setattr(Settings, "permission_rules", lambda self: None)
    monkeypatch.setattr(Settings, "user_mcp_config", lambda self: None)


async def test_web_serve_off_on_and_unavailable(monkeypatch, tmp_path, capsys):
    # 1. Off: no probe, compressor is None, health is 'off'.
    called, svc_kw, app_kw = [], [], []
    _patch_web_serve(monkeypatch, tmp_path, received_service=svc_kw, received_app=app_kw)
    monkeypatch.setattr(dependencies, "AsyncOpenAI", _fake_client_cls(captured=called))
    args = web_cli.build_parser().parse_args(["--no-knowledge"])
    assert await web_cli.serve(args) == 0
    assert called == []
    assert svc_kw[0]["conversation_compressor"] is None
    assert app_kw[0]["history_compression"] == "off"

    # 2. On & available: compressor passed, health is 'on', closed in reverse order.
    closed, svc_kw, app_kw = [], [], []
    _patch_web_serve(monkeypatch, tmp_path, received_service=svc_kw, received_app=app_kw, closed=closed)
    monkeypatch.setattr(dependencies, "AsyncOpenAI", _fake_client_cls(models=["qwen3:4b-instruct"], closed=closed))
    args = web_cli.build_parser().parse_args(["--no-knowledge", "--compress-history"])
    assert await web_cli.serve(args) == 0
    assert svc_kw[0]["conversation_compressor"] is not None
    assert app_kw[0]["history_compression"] == "on"
    assert closed == ["service", "client"]

    # 3. On & unavailable: still starts, compressor is None, health is 'unavailable'.
    closed, svc_kw, app_kw = [], [], []
    _patch_web_serve(monkeypatch, tmp_path, received_service=svc_kw, received_app=app_kw, closed=closed)
    monkeypatch.setattr(dependencies, "AsyncOpenAI", _fake_client_cls(error=httpx.ConnectError("down"), closed=closed))
    capsys.readouterr()
    assert await web_cli.serve(args) == 0
    assert svc_kw[0]["conversation_compressor"] is None
    assert app_kw[0]["history_compression"] == "unavailable"
    assert closed == ["service", "client"]
    assert "这次不压缩历史。" in capsys.readouterr().out


async def test_web_serve_refuses_missing_storage_dir_and_non_loopback(monkeypatch, tmp_path, capsys):
    svc_kw, app_kw = [], []
    _patch_web_serve(monkeypatch, tmp_path, received_service=svc_kw, received_app=app_kw)
    args = web_cli.build_parser().parse_args(["--no-knowledge", "--compress-history"])

    monkeypatch.setattr(web_cli.config, "conversation_storage_dir", "")
    assert await web_cli.serve(args) == 2
    assert "CONVERSATION_STORAGE_DIR" in capsys.readouterr().out

    monkeypatch.setattr(web_cli.config, "conversation_storage_dir", str(tmp_path))
    monkeypatch.setattr(web_cli.config, "summary_model_base_url", "https://remote.example.com/v1")
    assert await web_cli.serve(args) == 2
    out = capsys.readouterr().out
    assert "SummaryModelError:" in out and "本机回环地址" in out
    assert svc_kw == []


# ---- Web end-to-end with fake compressor: scheduler notified, /turns keeps originals ----

async def test_web_with_compressor_notifies_scheduler_and_turns_return_originals(tmp_path):
    entered = asyncio.Event()

    async def compress(**kwargs):
        entered.set()
        return (
            "## Goals and constraints\nNone\n"
            "## Confirmed information\nNone\n"
            "## Completed work\nNone\n"
            "## Failures and cancellations\nNone\n"
            "## Open questions\nNone\n"
            "## Sources\nNone"
        )

    model = AsyncMock()
    model.generate.return_value = loop_reply("latest reply")
    service = RagAgentService(
        model=model, mcp_servers=(), include_knowledge=False,
        memory_directory=tmp_path,
        conversation_compressor=SimpleNamespace(compress=compress),
    )
    archive = ConversationArchive(service._archive_root("local"), service._archive_label("local", "s1"))
    for i in range(26):
        archive.save_turn(ConversationTurn([
            ConversationMessage(MessageRole.USER, f"original user turn {i} " * 400),
            ConversationMessage(MessageRole.ASSISTANT, f"original reply {i}"),
        ]))
    app = create_app(service, user_id="local", access_token="tok", history_compression="on")
    async with service:
        lifespan = app.router.lifespan_context(app)
        async with lifespan:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers={"Authorization": "Bearer tok"},
            ) as client:
                resp = await client.post("/api/sessions/s1/messages",
                                         json={"content": "latest question", "request_id": "r1"})
                assert resp.status_code == 202
                run = app.state.runs.get("r1")
                await asyncio.wait_for(asyncio.shield(run.task), 5)
                await asyncio.wait_for(entered.wait(), 3)
                turns = (await client.get("/api/sessions/s1/turns?limit=5")).json()["turns"]
                assert turns[-1]["user"] == "latest question"
                assert turns[-1]["reply"] == "latest reply"
                assert any("original user turn" in t["user"] for t in turns[:-1])
                assert all("## Goals and constraints" not in t["user"] for t in turns)


# ---- --show-limits & static UI ----

def test_show_limits_includes_summary_model_row_without_url():
    limits = RunLimits.current(settings=_settings())
    rows = {(layer, name): value for layer, name, value in limits.rows()}
    key = ("后台摘要", "历史压缩模型（--compress-history）")
    assert rows[key] == "本机 qwen3:4b-instruct，单次 30 秒，最多试 2 次"
    assert "127.0.0.1" not in rows[key]

    custom = RunLimits.current(settings=_settings(summary_model="qwen3:8b"))
    custom_rows = {(layer, name): value for layer, name, value in custom.rows()}
    assert custom_rows[key] == "本机 qwen3:8b，单次 30 秒，最多试 2 次"


def test_static_ui_shows_history_compression_status_safely():
    root = Path(__file__).resolve().parents[1] / "app" / "api" / "static"
    html = (root / "index.html").read_text(encoding="utf-8")
    js = (root / "app.js").read_text(encoding="utf-8")
    assert 'id="status-compression"' in html
    assert 'id="dot-compression"' in html
    assert "历史压缩" in html
    assert "history_compression" in js
    assert "$('status-compression').textContent" in js
    assert "unavailable" in js and "warn" in js
