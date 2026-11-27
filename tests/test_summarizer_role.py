"""Offline tests for the optional summarizer model profile role (D-103)."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from types import SimpleNamespace

import httpx
import pytest

from app.agent import __main__ as cli
from app.agent.run_limits import RunLimits
from app.api import __main__ as web_cli
from app.config import Settings
from app.core import dependencies
from app.models.conversation_memory import ConversationMessage, ConversationTurn, MessageRole
from app.models.model_profile import ModelProfile, ModelProfileError, ModelProfiles, OPTIONAL_ROLES, ROLES
from app.services.memory_compression.llm_compressor import LLMCompressor

FAKE_SECRET = "sk-test-summarizer-secret-never-print"

MAIN_PROFILE = """
[profiles.main]
base_url = "https://relay.test/v1"
api_key_env = "AGENT_MODEL_API_KEY"
model = "main-model"
context_tokens = 128000
"""

SUM_PROFILE = """
[profiles.sum]
base_url = "https://summary.example.com/secret-path/v1"
api_key_env = "SUMMARIZER_TEST_KEY"
model = "sum-model"
context_tokens = 32000
max_output_tokens = 2048
timeout_seconds = 45.0
temperature = 0.9
enable_thinking = false
"""

VALID_SUMMARY = (
    "## Goals and constraints\nNone\n"
    "## Confirmed information\nNone\n"
    "## Completed work\nNone\n"
    "## Failures and cancellations\nNone\n"
    "## Open questions\nNone\n"
    "## Sources\nNone"
)


def _settings(path="", **kw) -> Settings:
    values = dict(
        _env_file=None,
        dashscope_api_key="ds-key",
        agent_model_base_url="https://relay.test/v1",
        agent_model_api_key="main-key",
        agent_model="gpt-main",
        model_profiles_file=str(path),
    )
    values.update(kw)
    return Settings(**values)


def _write(tmp_path, text: str):
    path = tmp_path / "models.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _fake_client_cls(*, models=None, error=None, delay=0.0, closed=None, captured=None, create_calls=None):
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

            async def create(**ckw):
                if create_calls is not None:
                    create_calls.append(ckw)
                msg = SimpleNamespace(
                    role="assistant", content=VALID_SUMMARY,
                    refusal=None, tool_calls=None, function_call=None,
                )
                return SimpleNamespace(
                    choices=[SimpleNamespace(finish_reason="stop", message=msg)],
                    usage=None,
                )

            self.models = SimpleNamespace(list=list_models)
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            if closed is not None:
                closed.append("client")

    return FakeClient


def _sample_turns() -> list[ConversationTurn]:
    return [
        ConversationTurn([
            ConversationMessage(MessageRole.USER, "hello"),
            ConversationMessage(MessageRole.ASSISTANT, "hi"),
        ])
    ]


# ---- model_profile.py ----

def test_summarizer_omitted_does_not_fall_back_to_main(tmp_path):
    assert OPTIONAL_ROLES == ("summarizer",)
    no_file = _settings("").model_profiles()
    assert no_file.summarizer is None
    assert no_file.summarizer_profile() is None

    path = _write(tmp_path, MAIN_PROFILE + '[roles]\nmain = "main"\n')
    profiles = _settings(path).model_profiles()
    assert profiles.summarizer is None
    assert profiles.summarizer_profile() is None
    assert tuple(profiles.roles) == ROLES


def test_summarizer_role_parsed_without_changing_main_roles_or_used(tmp_path, monkeypatch):
    monkeypatch.setenv("SUMMARIZER_TEST_KEY", FAKE_SECRET)
    path = _write(tmp_path, MAIN_PROFILE + SUM_PROFILE + '[roles]\nmain = "main"\nsummarizer = "sum"\n')
    profiles = _settings(path).model_profiles()
    assert set(profiles.roles) == set(ROLES)
    assert [p.name for p in profiles.used()] == ["main"]
    assert [row[0] for row in profiles.describe()] == list(ROLES)
    assert profiles.summarizer == "sum"
    summary = profiles.summarizer_profile()
    assert summary is not None
    assert summary.name == "sum"
    assert summary.model == "sum-model"
    assert summary.enable_thinking is False
    assert FAKE_SECRET not in repr(profiles)


def test_summarizer_validation_errors(tmp_path, monkeypatch):
    monkeypatch.delenv("SUMMARIZER_TEST_KEY", raising=False)

    # Missing profile target
    bad_target = _write(tmp_path, MAIN_PROFILE + '[roles]\nmain = "main"\nsummarizer = "nowhere"\n')
    with pytest.raises(ModelProfileError, match="角色 summarizer 指向不存在的档案"):
        _settings(bad_target).model_profiles()

    # Empty key on summarizer-only profile
    empty_key = _write(tmp_path, MAIN_PROFILE + SUM_PROFILE + '[roles]\nmain = "main"\nsummarizer = "sum"\n')
    with pytest.raises(ModelProfileError) as caught:
        _settings(empty_key).model_profiles()
    assert "SUMMARIZER_TEST_KEY" in str(caught.value)
    assert "main-key" not in str(caught.value)

    # Unused profile without key is still fine when summarizer is omitted
    unused = _write(tmp_path, MAIN_PROFILE + SUM_PROFILE + '[roles]\nmain = "main"\n')
    assert _settings(unused).model_profiles().summarizer_profile() is None

    # Unknown role still refused and lists summarizer
    unknown = _write(tmp_path, MAIN_PROFILE + '[roles]\nmain = "main"\nboss = "main"\n')
    with pytest.raises(ModelProfileError, match="summarizer"):
        _settings(unknown).model_profiles()

    # Direct ModelProfiles construction validates summarizer
    p = ModelProfile(name="main", base_url="https://x.test/v1", api_key_env="K", model="m",
                     context_tokens=8000, max_output_tokens=500, timeout_seconds=30.0)
    with pytest.raises(ValueError, match="summarizer"):
        ModelProfiles({"main": p}, dict.fromkeys(ROLES, "main"), summarizer="missing")


# ---- LLMCompressor extra_body & open_conversation_compressor ----

async def test_llm_compressor_extra_body_omitted_vs_sent():
    calls_plain, calls_none, calls_extra = [], [], []
    turns = _sample_turns()

    c_plain = LLMCompressor(client=_fake_client_cls(create_calls=calls_plain)(), model="m")
    c_none = LLMCompressor(client=_fake_client_cls(create_calls=calls_none)(), model="m", extra_body=None)
    c_extra = LLMCompressor(
        client=_fake_client_cls(create_calls=calls_extra)(),
        model="m",
        extra_body={"enable_thinking": False},
    )

    await c_plain.compress(None, turns)
    await c_none.compress(None, turns)
    await c_extra.compress(None, turns)

    assert "extra_body" not in calls_plain[0]
    assert "extra_body" not in calls_none[0]
    assert calls_none[0] == calls_plain[0]
    assert calls_extra[0]["extra_body"] == {"enable_thinking": False}


async def test_open_conversation_compressor_with_profile_uses_selected_fields(monkeypatch):
    monkeypatch.setenv("SUMMARIZER_TEST_KEY", FAKE_SECRET)
    captured, create_calls = [], []
    monkeypatch.setattr(
        dependencies, "AsyncOpenAI",
        _fake_client_cls(captured=captured, create_calls=create_calls),
    )

    with_thinking = ModelProfile(
        name="sum",
        base_url="https://summary.example.com/v1",
        api_key_env="SUMMARIZER_TEST_KEY",
        model="sum-model",
        context_tokens=64000,
        max_output_tokens=4096,
        timeout_seconds=45.0,
        temperature=0.8,
        enable_thinking=False,
    )
    async with dependencies.open_conversation_compressor(with_thinking) as comp:
        assert comp._model == "sum-model"
        assert comp._temperature == 0.1
        assert comp._max_output_tokens == 1536
        assert comp._max_input_tokens == 41600
        assert comp.batch_token_budget == 6000
        await comp.compress(None, _sample_turns())
    assert captured[-1] == {
        "base_url": "https://summary.example.com/v1",
        "api_key": FAKE_SECRET,
        "timeout": 45.0,
        "max_retries": 0,
    }
    assert create_calls[-1]["extra_body"] == {"enable_thinking": False}

    without_thinking = ModelProfile(
        name="sum2",
        base_url="https://summary.example.com/v1",
        api_key_env="SUMMARIZER_TEST_KEY",
        model="sum-model-2",
        context_tokens=64000,
        max_output_tokens=4096,
        timeout_seconds=60.0,
        enable_thinking=None,
    )
    async with dependencies.open_conversation_compressor(without_thinking) as comp:
        await comp.compress(None, _sample_turns())
    assert "extra_body" not in create_calls[-1]


# ---- check_conversation_compressor source='profile' ----

async def test_check_compressor_profile_messages_omit_ollama_and_raw_error():
    err_client = _fake_client_cls(error=httpx.ConnectError("RAW_SECRET_BODY"))(
        base_url="https://summary.example.com/secret-path/v1"
    )
    comp = LLMCompressor(client=err_client, model="sum-model")
    problem = await dependencies.check_conversation_compressor(comp, source="profile")
    assert problem == "历史压缩不可用：连不上摘要服务（summary.example.com:443，ConnectError）"
    assert "Ollama" not in problem and "ollama" not in problem
    assert "RAW_SECRET_BODY" not in problem and "secret-path" not in problem

    missing_client = _fake_client_cls(models=["other-model"])(
        base_url="https://summary.example.com/v1"
    )
    comp2 = LLMCompressor(client=missing_client, model="sum-model")
    problem2 = await dependencies.check_conversation_compressor(comp2, source="profile")
    assert problem2 == "历史压缩不可用：摘要服务上没有模型 sum-model（请写完整模型名）"
    assert "Ollama" not in problem2 and "ollama" not in problem2

    ok_client = _fake_client_cls(models=["sum-model"])(base_url="https://summary.example.com/v1")
    comp3 = LLMCompressor(client=ok_client, model="sum-model")
    assert await dependencies.check_conversation_compressor(comp3, source="profile") is None


# ---- open_history_compressor branches ----

async def test_open_history_compressor_disabled_ignores_broken_profile_file(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.config, "model_profiles_file", str(tmp_path / "missing.toml"))
    async with AsyncExitStack() as stack:
        compressor, status = await cli.open_history_compressor(False, stack)
    assert compressor is None and status == "off"


async def test_open_history_compressor_remote_profile_warns_and_skips_loopback_check(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("SUMMARIZER_TEST_KEY", FAKE_SECRET)
    path = _write(tmp_path, MAIN_PROFILE + SUM_PROFILE + '[roles]\nmain = "main"\nsummarizer = "sum"\n')
    captured, closed = [], []
    # Even an invalid local summary_model_base_url is skipped when summarizer profile is set,
    # and passing it explicitly puts it in model_fields_set.
    cfg = _settings(
        path,
        conversation_storage_dir=str(tmp_path),
        summary_model_base_url="https://not-loopback.invalid/v1",
    )
    monkeypatch.setattr(cli, "config", cfg)
    monkeypatch.setattr(
        dependencies, "AsyncOpenAI",
        _fake_client_cls(models=["sum-model"], captured=captured, closed=closed),
    )

    async with AsyncExitStack() as stack:
        compressor, status = await cli.open_history_compressor(True, stack)
        assert status == "on" and compressor is not None
        assert compressor._model == "sum-model"
        assert closed == []
    assert closed == ["client"]
    assert captured[-1]["base_url"] == "https://summary.example.com/secret-path/v1"
    assert captured[-1]["api_key"] == FAKE_SECRET
    assert captured[-1]["timeout"] == 45.0

    out = capsys.readouterr().out
    assert "历史压缩：已用档案 sum，SUMMARY_MODEL_BASE_URL / SUMMARY_MODEL 这次不用。" in out
    assert "历史压缩：会话原文会发往 summary.example.com（档案 sum）。" in out
    assert "secret-path" not in out
    assert FAKE_SECRET not in out


@pytest.mark.parametrize("loopback_url", [
    "http://127.0.0.1:11434/v1",
    "http://localhost:11434/v1",
    "http://[::1]:11434/v1",
])
async def test_open_history_compressor_loopback_profile_does_not_warn(monkeypatch, tmp_path, capsys, loopback_url):
    monkeypatch.setenv("SUMMARIZER_TEST_KEY", FAKE_SECRET)
    local_profile = SUM_PROFILE.replace("https://summary.example.com/secret-path/v1", loopback_url)
    path = _write(tmp_path, MAIN_PROFILE + local_profile + '[roles]\nmain = "main"\nsummarizer = "sum"\n')
    cfg = _settings(path, conversation_storage_dir=str(tmp_path))
    monkeypatch.setattr(cli, "config", cfg)
    monkeypatch.setattr(dependencies, "AsyncOpenAI", _fake_client_cls(models=["sum-model"]))

    async with AsyncExitStack() as stack:
        compressor, status = await cli.open_history_compressor(True, stack)
    assert status == "on" and compressor is not None
    out = capsys.readouterr().out
    assert "会话原文会发往" not in out
    assert "SUMMARY_MODEL_BASE_URL" not in out


async def test_open_history_compressor_without_summarizer_uses_local_unchanged(monkeypatch, tmp_path, capsys):
    path = _write(tmp_path, MAIN_PROFILE + '[roles]\nmain = "main"\n')
    captured = []
    monkeypatch.setattr(cli.config, "conversation_storage_dir", str(tmp_path))
    monkeypatch.setattr(cli.config, "model_profiles_file", str(path))
    monkeypatch.setattr(cli.config, "agent_model_api_key", "main-test-key")   # independent of .env
    monkeypatch.setattr(cli.config, "summary_model_base_url", "http://127.0.0.1:11434/v1")
    monkeypatch.setattr(cli.config, "summary_model", "qwen3:4b-instruct")
    monkeypatch.setattr(
        dependencies, "AsyncOpenAI",
        _fake_client_cls(models=["qwen3:4b-instruct"], captured=captured),
    )

    async with AsyncExitStack() as stack:
        compressor, status = await cli.open_history_compressor(True, stack)
    assert status == "on" and compressor is not None
    assert captured[-1] == {
        "base_url": "http://127.0.0.1:11434/v1",
        "api_key": "ollama",
        "timeout": 120.0,
        "max_retries": 0,
    }
    assert capsys.readouterr().out == ""


async def test_open_history_compressor_propagates_profile_error_and_entrypoints_exit_2(monkeypatch, tmp_path, capsys):
    bad = _write(tmp_path, MAIN_PROFILE + '[roles]\nmain = "main"\nsummarizer = "missing"\n')
    monkeypatch.setattr(cli.config, "conversation_storage_dir", str(tmp_path))
    monkeypatch.setattr(cli.config, "model_profiles_file", str(bad))

    async with AsyncExitStack() as stack:
        with pytest.raises(ModelProfileError, match="角色 summarizer 指向不存在的档案"):
            await cli.open_history_compressor(True, stack)

    # CLI entry returns 2
    args = SimpleNamespace(compress_history=True, filesystem_root=None, write_root=None,
                           no_knowledge=True, max_executions=1, goal="goal", session_id="s")
    assert await cli.main(args) == 2
    assert "ModelProfileError:" in capsys.readouterr().out

    # Web entry returns 2
    monkeypatch.setattr(web_cli.config, "web_host", "127.0.0.1")
    monkeypatch.setattr(web_cli.config, "web_port", 8000)
    monkeypatch.setattr(web_cli.config, "web_access_token", "tok")
    monkeypatch.setattr(web_cli.config, "conversation_storage_dir", str(tmp_path))
    monkeypatch.setattr(web_cli.config, "model_profiles_file", str(bad))
    web_args = web_cli.build_parser().parse_args(["--no-knowledge", "--compress-history"])
    assert await web_cli.serve(web_args) == 2
    web_out = capsys.readouterr().out
    assert "ModelProfileError:" in web_out
    assert "HistoryCompressionError:" not in web_out


async def test_open_history_compressor_profile_unavailable_degrades_cleanly(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("SUMMARIZER_TEST_KEY", FAKE_SECRET)
    path = _write(tmp_path, MAIN_PROFILE + SUM_PROFILE + '[roles]\nmain = "main"\nsummarizer = "sum"\n')
    closed = []
    monkeypatch.setattr(cli.config, "conversation_storage_dir", str(tmp_path))
    monkeypatch.setattr(cli.config, "model_profiles_file", str(path))
    monkeypatch.setattr(cli.config, "agent_model_api_key", "main-test-key")   # independent of .env
    monkeypatch.setattr(
        dependencies, "AsyncOpenAI",
        _fake_client_cls(error=httpx.ConnectError("secret-err"), closed=closed),
    )

    async with AsyncExitStack() as stack:
        compressor, status = await cli.open_history_compressor(True, stack)
        assert compressor is None and status == "unavailable"
    assert closed == ["client"]
    out = capsys.readouterr().out
    assert "历史压缩不可用：连不上摘要服务（summary.example.com:443，ConnectError），这次不压缩历史。" in out
    assert "Ollama" not in out and "secret-err" not in out and FAKE_SECRET not in out


# ---- --show-limits ----

def test_show_limits_summary_row_local_and_profile(tmp_path, monkeypatch):
    key = ("后台摘要", "历史压缩模型（--compress-history）")

    local_rows = {(layer, name): val for layer, name, val in RunLimits.current(settings=_settings()).rows()}
    assert local_rows[key] == "本机 qwen3:4b-instruct，单次 30 秒，最多试 2 次"

    monkeypatch.setenv("SUMMARIZER_TEST_KEY", FAKE_SECRET)
    path = _write(tmp_path, MAIN_PROFILE + SUM_PROFILE + '[roles]\nmain = "main"\nsummarizer = "sum"\n')
    prof_rows = {(layer, name): val for layer, name, val in RunLimits.current(settings=_settings(path)).rows()}
    assert prof_rows[key] == "档案 sum，模型 sum-model，单次 30 秒，最多试 2 次"
    assert "summary.example.com" not in prof_rows[key]
    assert "https://" not in prof_rows[key]
    assert FAKE_SECRET not in prof_rows[key]

    # Broken profile file does not crash RunLimits.current()
    bad_path = _write(tmp_path, MAIN_PROFILE + '[roles]\nmain = "main"\nsummarizer = "missing"\n')
    fallback_rows = {(layer, name): val for layer, name, val in RunLimits.current(settings=_settings(bad_path)).rows()}
    assert fallback_rows[key] == "本机 qwen3:4b-instruct，单次 30 秒，最多试 2 次"


# ---- Web wiring with summarizer profile ----

async def test_web_serve_with_summarizer_profile_passes_profile_compressor(monkeypatch, tmp_path, capsys):
    import uvicorn

    monkeypatch.setenv("SUMMARIZER_TEST_KEY", FAKE_SECRET)
    path = _write(tmp_path, MAIN_PROFILE + SUM_PROFILE + '[roles]\nmain = "main"\nsummarizer = "sum"\n')
    received_service, received_app, closed = [], [], []

    class FakeService:
        def __init__(self, **kwargs):
            received_service.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
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
    monkeypatch.setattr(web_cli.config, "model_profiles_file", str(path))
    monkeypatch.setattr(web_cli.config, "agent_model_api_key", "main-test-key")   # independent of .env
    monkeypatch.setattr(web_cli.config, "longterm_memory_dsn", "")
    monkeypatch.setattr(Settings, "permission_rules", lambda self: None)
    monkeypatch.setattr(Settings, "user_mcp_config", lambda self: None)
    monkeypatch.setattr(Settings, "schedules", lambda self: ())
    monkeypatch.setattr(dependencies, "AsyncOpenAI", _fake_client_cls(models=["sum-model"], closed=closed))

    args = web_cli.build_parser().parse_args(["--no-knowledge", "--compress-history"])
    assert await web_cli.serve(args) == 0
    comp = received_service[0]["conversation_compressor"]
    assert comp is not None and comp._model == "sum-model"
    assert received_app[0]["history_compression"] == "on"
    assert closed == ["service", "client"]
    out = capsys.readouterr().out
    assert "summary.example.com" in out
    assert FAKE_SECRET not in out
